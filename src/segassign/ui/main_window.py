"""Old-workflow Qt shell backed only by the public v2 services."""

from __future__ import annotations

import json
import os
from pathlib import Path

import psutil
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from .. import composite, segment, zonal
from ..models import (
    CompositeRequest,
    ResourceBudget,
    SegmentRequest,
    TaskStatus,
    ZonalBatchRequest,
    ZonalOptions,
)
from .controller import TaskController


class PathField(QWidget):
    """Path entry supporting file, directory, save-file, or file/directory input."""

    def __init__(self, mode: str, file_filter: str = "所有文件 (*)") -> None:
        super().__init__()
        self.mode = mode
        self.file_filter = file_filter
        self.edit = QLineEdit()
        self.edit.setClearButtonEnabled(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.edit, 1)
        if mode == "file-or-directory":
            folder = QPushButton("选择文件夹")
            folder.clicked.connect(self._browse_directory)
            file = QPushButton("选择文件")
            file.clicked.connect(self._browse_file)
            layout.addWidget(folder)
            layout.addWidget(file)
        else:
            button = QPushButton("选择文件夹" if mode == "directory" else "选择文件")
            button.clicked.connect(self._browse)
            layout.addWidget(button)

    def path(self) -> Path:
        value = self.edit.text().strip()
        if not value:
            raise ValueError("存在尚未填写的路径")
        return Path(value).expanduser().resolve()

    def _initial(self) -> str:
        value = self.edit.text().strip()
        path = Path(value).expanduser() if value else Path.cwd()
        return str(path if path.is_dir() else path.parent)

    def _browse(self) -> None:
        if self.mode == "directory":
            self._browse_directory()
        elif self.mode == "save":
            value, _ = QFileDialog.getSaveFileName(self, "选择输出 Shapefile", self.edit.text(), self.file_filter)
            if value:
                self.edit.setText(value)
        else:
            self._browse_file()

    def _browse_directory(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "选择文件夹", self._initial())
        if value:
            self.edit.setText(value)

    def _browse_file(self) -> None:
        value, _ = QFileDialog.getOpenFileName(self, "选择文件", self._initial(), self.file_filter)
        if value:
            self.edit.setText(value)


class TaskPage(QWidget):
    def __init__(
        self,
        title: str,
        caption: str,
        input_fields: tuple[tuple[str, str, str, str], ...],
        output_fields: tuple[tuple[str, str, str, str], ...],
        with_values: bool = False,
    ) -> None:
        super().__init__()
        self.fields: dict[str, PathField] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        description = QLabel(caption)
        description.setObjectName("subtle")
        layout.addWidget(description)
        layout.addSpacing(8)
        layout.addWidget(self._path_card("第一步：选择输入数据", input_fields))

        self.parameter_card = QGroupBox("赋值参数")
        parameter_form = QFormLayout(self.parameter_card)
        self.values = QLineEdit()
        self.values.setPlaceholderText("例如：1,2,3；留空则自动识别")
        self.threshold = QSpinBox()
        self.threshold.setRange(0, 100)
        self.threshold.setValue(65)
        self.threshold.setSuffix(" %")
        self.recursive = QCheckBox("包含子文件夹")
        self.recursive.setChecked(True)
        if with_values:
            parameter_form.addRow("分类像元值", self.values)
            parameter_form.addRow("主类别阈值", self.threshold)
            parameter_form.addRow("批量发现", self.recursive)
            layout.addWidget(self.parameter_card)
        else:
            self.parameter_card.hide()

        layout.addWidget(self._path_card("第二步：选择输出和软件", output_fields))
        note = QLabel("带 * 的项目为必填；02 可选择单个 Shapefile 或文件夹进行原生批处理。")
        note.setObjectName("subtle")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)

    def _path_card(self, title: str, fields: tuple[tuple[str, str, str, str], ...]) -> QGroupBox:
        card = QGroupBox(title)
        form = QFormLayout(card)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        for key, label, mode, file_filter in fields:
            field = PathField(mode, file_filter)
            self.fields[key] = field
            form.addRow(f"* {label}", field)
        return card

    def path(self, key: str) -> Path:
        return self.fields[key].path()

    def valid_values(self) -> tuple[int, ...]:
        raw = self.values.text().replace("，", ",").strip()
        return tuple(sorted({int(item.strip()) for item in raw.split(",") if item.strip()})) if raw else ()

    def required_fields(self) -> tuple[PathField, ...]:
        return tuple(self.fields.values())


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SegAssign — 影像分割与分类赋值")
        self.resize(1180, 780)
        self.setMinimumSize(1024, 680)
        self._close_when_stopped = False
        self._progress_floor = 0
        self._last_output_directory: Path | None = None
        self.controller = TaskController(self)
        self.controller.event_received.connect(self._on_event)
        self.controller.result_ready.connect(self._on_result)
        self.controller.failed.connect(self._on_failure)
        self.controller.running_changed.connect(self._set_running)

        root = QWidget()
        shell = QVBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        shell.addWidget(self._build_header())
        shell.addWidget(self._build_tool_tabs())
        shell.addWidget(self._build_body(), 1)
        shell.addWidget(self._build_footer())
        self.setCentralWidget(root)
        self._apply_style()
        self._connect_summary_updates()
        self._on_tool_tab(0)

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(24, 13, 24, 13)
        title = QLabel("SegAssign")
        title.setObjectName("appTitle")
        layout.addWidget(title)
        subtitle = QLabel("影像分割与分类赋值")
        subtitle.setObjectName("subtle")
        layout.addWidget(subtitle)
        layout.addStretch(1)
        self.header_status = QLabel("就绪")
        self.header_status.setObjectName("statusPill")
        layout.addWidget(self.header_status)
        return header

    def _build_tool_tabs(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("tabsFrame")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(18, 0, 18, 0)
        self.mode = QTabBar()
        self.mode.setExpanding(False)
        self.mode.addTab("00_分割并赋值")
        self.mode.addTab("01_单独分割")
        self.mode.addTab("02_单独赋值")
        self.mode.currentChanged.connect(self._on_tool_tab)
        layout.addWidget(self.mode)
        layout.addStretch(1)
        return frame

    def _build_body(self) -> QWidget:
        body = QWidget()
        body.setObjectName("body")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(16, 12, 16, 12)
        body_layout.setSpacing(16)

        nav = QFrame()
        nav.setObjectName("navigation")
        nav.setFixedWidth(170)
        nav_layout = QVBoxLayout(nav)
        for index, title in enumerate(("一键运行", "任务进度", "成果记录", "高级配置")):
            button = QPushButton(title)
            button.setObjectName("navActive" if index == 0 else "navButton")
            if title == "任务进度":
                button.clicked.connect(lambda: self.log.setFocus())
            elif title == "成果记录":
                button.clicked.connect(self._open_output)
            elif title == "高级配置":
                button.clicked.connect(lambda: self.status.setText("当前使用已验证的推荐参数"))
            nav_layout.addWidget(button)
        nav_layout.addStretch(1)
        body_layout.addWidget(nav)

        content = QVBoxLayout()
        upper = QHBoxLayout()
        self.pages = QStackedWidget()
        self.composite_page = TaskPage(
            "分割并赋值",
            "先分割生成面矢量，再使用分类影像完成赋值。",
            (
                ("process", "亚米级影像", "file", "GeoTIFF (*.tif *.tiff)"),
                ("base", "2 米基础影像", "file", "GeoTIFF (*.tif *.tiff)"),
                ("classification", "分类影像", "file", "GeoTIFF (*.tif *.tiff)"),
            ),
            (
                ("output", "输出 Shapefile", "save", "Shapefile (*.shp)"),
                ("ecognition", "eCognition 安装目录", "directory", ""),
            ),
            with_values=True,
        )
        self.segment_page = TaskPage(
            "单独分割",
            "将亚米级影像分割为面矢量。",
            (
                ("process", "亚米级影像", "file", "GeoTIFF (*.tif *.tiff)"),
                ("base", "2 米基础影像", "file", "GeoTIFF (*.tif *.tiff)"),
            ),
            (
                ("output", "输出 Shapefile", "save", "Shapefile (*.shp)"),
                ("ecognition", "eCognition 安装目录", "directory", ""),
            ),
        )
        self.zonal_page = TaskPage(
            "单独赋值",
            "使用一幅区域分类栅格，为单个或一批面矢量更新统计字段。",
            (
                ("vector_source", "输入矢量或文件夹", "file-or-directory", "Shapefile (*.shp)"),
                ("classification", "区域分类影像", "file", "GeoTIFF (*.tif *.tiff)"),
            ),
            (("output_directory", "输出 Shapefile 目录", "directory", ""),),
            with_values=True,
        )
        for page in (self.composite_page, self.segment_page, self.zonal_page):
            self.pages.addWidget(page)
        upper.addWidget(self.pages, 3)
        upper.addWidget(self._build_sidebar(), 2)
        content.addLayout(upper, 1)
        content.addLayout(self._build_actions())
        content.addWidget(self.progress)
        content.addWidget(self.log, 1)
        body_layout.addLayout(content, 1)
        return body

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        current = QGroupBox("当前任务")
        current_layout = QVBoxLayout(current)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        current_layout.addWidget(self.summary)
        layout.addWidget(current)
        flow = QGroupBox("执行流程")
        flow_layout = QVBoxLayout(flow)
        self.flow = QLabel()
        self.flow.setWordWrap(True)
        flow_layout.addWidget(self.flow)
        layout.addWidget(flow)
        performance = QGroupBox("性能模式")
        performance_layout = QVBoxLayout(performance)
        self.performance_group = QButtonGroup(self)
        self.performance_modes = {}
        for key, title in (
            ("low", "低性能模式"),
            ("recommended", "推荐模式（已验证）"),
            ("high", "高性能模式"),
        ):
            radio = QRadioButton(title)
            radio.setChecked(key == "recommended")
            self.performance_group.addButton(radio)
            self.performance_modes[key] = radio
            performance_layout.addWidget(radio)
        hardware = QLabel(
            f"已识别：{os.cpu_count() or 1} 线程 · {psutil.virtual_memory().total / 1024**3:.1f} GB 内存"
        )
        hardware.setObjectName("subtle")
        performance_layout.addWidget(hardware)
        layout.addWidget(performance)
        layout.addStretch(1)
        return sidebar

    def _build_actions(self) -> QHBoxLayout:
        actions = QHBoxLayout()
        self.resume = QCheckBox("断点续跑")
        self.resume.setChecked(True)
        self.plan_button = QPushButton("检查配置")
        self.run_button = QPushButton("一键执行：分割并赋值")
        self.run_button.setObjectName("primaryButton")
        self.cancel_button = QPushButton("安全取消")
        self.cancel_button.setEnabled(False)
        self.plan_button.clicked.connect(self._show_plan)
        self.run_button.clicked.connect(self._run)
        self.cancel_button.clicked.connect(self._cancel)
        actions.addWidget(self.resume)
        actions.addStretch(1)
        actions.addWidget(self.plan_button)
        actions.addWidget(self.run_button)
        actions.addWidget(self.cancel_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("任务事件和运行报告将显示在这里。")
        self.log.setMaximumBlockCount(4000)
        self.log.setMinimumHeight(115)
        self.status = QLabel("就绪")
        return actions

    def _build_footer(self) -> QWidget:
        footer = QFrame()
        footer.setObjectName("footer")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(18, 7, 18, 7)
        layout.addWidget(self.status)
        layout.addStretch(1)
        layout.addWidget(QLabel("SegAssign"))
        return footer

    def _connect_summary_updates(self) -> None:
        for page in (self.composite_page, self.segment_page, self.zonal_page):
            for field in page.required_fields():
                field.edit.textChanged.connect(self._refresh_summary)

    def _on_tool_tab(self, index: int) -> None:
        if not hasattr(self, "pages"):
            return
        self.pages.setCurrentIndex(index)
        titles = ("一键执行：分割并赋值", "开始单独分割", "开始批量赋值")
        flows = (
            "① 分割并生成面矢量\n② 使用分类影像完成赋值",
            "① 准备分块与有效区\n② eCognition 分割\n③ 后处理并拼接",
            "① 自动发现全部 Shapefile\n② 使用区域分类影像逐项赋值\n③ 原名发布到输出目录",
        )
        self.run_button.setText(titles[index])
        self.flow.setText(flows[index])
        self._refresh_summary()

    def _current_page(self) -> TaskPage:
        return self.pages.currentWidget()

    def _refresh_summary(self) -> None:
        if not hasattr(self, "summary"):
            return
        fields = self._current_page().required_fields()
        filled = sum(bool(field.edit.text().strip()) for field in fields)
        if filled == len(fields):
            message = f"已填写 {filled}/{len(fields)} 项\n配置完整，可以开始检查。"
        else:
            message = f"已填写 {filled}/{len(fields)} 项\n还需填写 {len(fields) - filled} 项必填内容。"
        self.summary.setText(message)

    def _backend(self):
        page = self._current_page()
        if page is self.zonal_page:
            options = ZonalOptions(valid_values=page.valid_values(), class_threshold=page.threshold.value())
            request = ZonalBatchRequest(
                page.path("vector_source"),
                page.path("classification"),
                page.path("output_directory"),
                options,
                recursive=page.recursive.isChecked(),
            )
            output_directory = request.output_directory
            return zonal.plan_batch, zonal.run_batch, request, output_directory / ".geotoolbox-work", output_directory
        if page is self.segment_page:
            request = SegmentRequest(
                page.path("process"),
                page.path("base"),
                page.path("output"),
                page.path("ecognition"),
            )
            return segment.plan, segment.run, request, request.output_vector.parent / ".geotoolbox-work", None
        options = ZonalOptions(valid_values=page.valid_values(), class_threshold=page.threshold.value())
        request = CompositeRequest(
            page.path("process"),
            page.path("base"),
            page.path("classification"),
            page.path("output"),
            page.path("ecognition"),
            zonal_options=options,
        )
        return composite.plan, composite.run, request, request.output_vector.parent / ".geotoolbox-work", None

    def _resource_budget(self) -> ResourceBudget:
        cpus = max(1, os.cpu_count() or 1)
        if self.performance_modes["low"].isChecked():
            return ResourceBudget(
                logical_cpus=max(1, cpus // 2),
                memory_fraction=0.60,
                ecognition_workers=min(4, cpus),
                raster_workers=max(1, cpus // 4),
            )
        if self.performance_modes["high"].isChecked():
            return ResourceBudget(
                logical_cpus=cpus,
                memory_fraction=0.90,
                ecognition_workers=min(7, cpus),
                raster_workers=max(1, cpus - 2),
            )
        return ResourceBudget()

    def _show_plan(self) -> None:
        try:
            planner, _, request, _, output_directory = self._backend()
            execution_plan = planner(request)
        except Exception as error:
            QMessageBox.warning(self, "配置错误", str(error))
            return
        self._last_output_directory = output_directory
        summary = {"stages": execution_plan.stages, "metrics": dict(execution_plan.metrics)}
        self.log.setPlainText(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        self.status.setText("配置检查通过")
        self.header_status.setText("配置有效")

    def _run(self) -> None:
        try:
            planner, runner, request, work_dir, output_directory = self._backend()
            planner(request)
            self.controller.start(
                runner,
                request,
                work_dir,
                self.resume.isChecked(),
                self._resource_budget(),
            )
        except Exception as error:
            QMessageBox.warning(self, "无法启动", str(error))
            return
        self._last_output_directory = output_directory or getattr(request, "output_vector", None).parent
        self.log.clear()
        self._progress_floor = 0
        self.progress.setRange(0, 0)
        self.status.setText("任务启动中…")
        self.header_status.setText("运行中")

    def _cancel(self) -> None:
        self.controller.cancel()
        self.cancel_button.setEnabled(False)
        self.status.setText("正在安全取消…")
        self.header_status.setText("取消中")

    def _on_event(self, event: dict) -> None:
        self.status.setText(event["message"])
        self.header_status.setText(event["message"])
        data = event.get("data", {})
        completed, total = data.get("completed"), data.get("total")
        if completed is not None and total:
            ranges = {
                "prepare": (0, 20),
                "ecognition": (20, 75),
                "postprocess": (75, 90),
                "count": (10, 90),
                "batch": (0, 100),
            }
            start, stop = ranges.get(event["phase"], (0, 95))
            value = start + round((stop - start) * int(completed) / int(total))
            if event["phase"] == "batch":
                self._progress_floor = value
            else:
                self._progress_floor = max(self._progress_floor, value)
            self.progress.setRange(0, 100)
            self.progress.setValue(self._progress_floor)
        self.log.appendPlainText(f"[{event['phase']}] {event['message']}")

    def _on_result(self, result) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(100 if result.status is TaskStatus.SUCCEEDED else 0)
        self.status.setText(f"任务结束：{result.status.value}")
        self.header_status.setText(result.status.value)
        self.log.appendPlainText(f"报告：{result.report_path}")
        if hasattr(result, "items_total"):
            self.log.appendPlainText(f"批处理：成功 {result.items_succeeded}/{result.items_total}")
        if result.warnings:
            self.log.appendPlainText("\n".join(result.warnings))

    def _on_failure(self, detail: str) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText("任务线程异常")
        self.header_status.setText("失败")
        self.log.appendPlainText(detail)

    def _set_running(self, running: bool) -> None:
        self.mode.setEnabled(not running)
        self.pages.setEnabled(not running)
        self.resume.setEnabled(not running)
        self.plan_button.setEnabled(not running)
        self.run_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        for radio in self.performance_modes.values():
            radio.setEnabled(not running)
        if not running and self._close_when_stopped:
            self._close_when_stopped = False
            self.close()

    def _open_output(self) -> None:
        if self._last_output_directory is None or not self._last_output_directory.is_dir():
            self.status.setText("尚无可打开的成果目录")
            return
        QDesktopServices.openUrl(self._last_output_directory.as_uri())

    def closeEvent(self, event) -> None:
        if self.controller.running:
            answer = QMessageBox.question(self, "任务仍在运行", "是否安全取消任务并关闭窗口？")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.controller.cancel()
            self._close_when_stopped = True
            event.ignore()
            return
        event.accept()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            "QMainWindow, #body { background: #f5f7fa; }"
            "QWidget { font-family: 'Microsoft YaHei UI'; font-size: 14px; color: #1f2937; }"
            "#header, #tabsFrame, #footer { background: white; }"
            "#appTitle { font-size: 22px; font-weight: 700; color: #111827; }"
            "#pageTitle { font-size: 22px; font-weight: 700; }"
            "#subtle { color: #667085; }"
            "#statusPill { background: #eef2ff; color: #3949ab; padding: 7px 12px; border-radius: 4px; }"
            "QTabBar::tab { background: white; padding: 11px 22px; border: 1px solid #e5e7eb; }"
            "QTabBar::tab:selected { color: #1d4ed8; border-bottom: 3px solid #2563eb; font-weight: 700; }"
            "#navigation { background: #eef1f5; border-radius: 6px; }"
            "#navButton, #navActive { text-align: left; padding: 11px 15px; border: 0; border-radius: 4px; }"
            "#navActive { background: white; font-weight: 700; }"
            "#navButton:hover { background: #e2e8f0; }"
            "QGroupBox { background: white; border: 1px solid #dfe3e8; border-radius: 6px; margin-top: 13px; "
            "padding: 14px 12px 10px 12px; font-weight: 700; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }"
            "QLineEdit, QSpinBox, QPlainTextEdit { background: white; border: 1px solid #cfd5dd; "
            "border-radius: 4px; padding: 7px; font-weight: 400; }"
            "QPushButton { background: white; border: 1px solid #c9d0d9; border-radius: 4px; padding: 8px 13px; }"
            "QPushButton:hover { background: #f0f4f8; }"
            "#primaryButton { background: #2563eb; color: white; border-color: #2563eb; font-weight: 700; "
            "padding: 10px 20px; }"
            "#primaryButton:disabled { background: #93a8d8; }"
            "QProgressBar { border: 1px solid #cfd5dd; border-radius: 4px; text-align: center; background: white; }"
            "QProgressBar::chunk { background: #2563eb; }"
        )
