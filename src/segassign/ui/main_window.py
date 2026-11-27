"""Responsive PySide6 desktop shell backed by the public SegAssign services."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
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
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from .. import composite, segment, zonal
from ..models import (
    CompositeBatchRequest,
    CompositeRequest,
    ResourceBudget,
    SegmentBatchRequest,
    SegmentRequest,
    TaskStatus,
    ZonalBatchRequest,
    ZonalOptions,
)
from ..runtime import (
    ResourceScheduler,
    build_resource_budget,
    cache_usage_bytes,
    clear_cache,
    detect_hardware,
    prune_expired_cache,
)
from .controller import TaskController


def _format_bytes(size: int) -> str:
    if size < 1024**2:
        return f"{size / 1024:.1f} KB"
    if size < 1024**3:
        return f"{size / 1024**2:.1f} MB"
    return f"{size / 1024**3:.2f} GB"


class PathField(QWidget):
    """Path entry supporting file, directory, file/directory, or save/directory input."""

    def __init__(self, mode: str, file_filter: str = "所有文件 (*)", placeholder: str = "") -> None:
        super().__init__()
        self.mode = mode
        self.file_filter = file_filter
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.edit = QLineEdit()
        self.edit.setClearButtonEnabled(True)
        self.edit.setMinimumHeight(30)
        if placeholder:
            self.edit.setPlaceholderText(placeholder)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.edit, 1)

        if mode == "file-or-directory":
            file_button = self._make_button("选择文件", self._browse_file)
            folder_button = self._make_button("选择文件夹", self._browse_directory)
            layout.addWidget(file_button)
            layout.addWidget(folder_button)
        elif mode == "save-or-directory":
            save_button = self._make_button("保存 SHP", self._browse_save)
            folder_button = self._make_button("选择文件夹", self._browse_directory)
            layout.addWidget(save_button)
            layout.addWidget(folder_button)
        else:
            label = "选择文件夹" if mode == "directory" else ("保存 SHP" if mode == "save" else "选择文件")
            button = self._make_button(label, self._browse)
            layout.addWidget(button)

    def _make_button(self, text: str, handler) -> QPushButton:
        button = QPushButton(text)
        button.setMinimumHeight(30)
        button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        button.clicked.connect(handler)
        return button

    def raw_text(self) -> str:
        return self.edit.text().strip()

    def path(self) -> Path:
        value = self.raw_text()
        if not value:
            raise ValueError("存在尚未填写的路径")
        return Path(value).expanduser().resolve()

    def _initial(self) -> str:
        value = self.raw_text()
        if not value:
            return str(Path.cwd())
        path = Path(value).expanduser()
        return str(path if path.is_dir() else path.parent)

    def _browse(self) -> None:
        if self.mode == "directory":
            self._browse_directory()
        elif self.mode == "save":
            self._browse_save()
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

    def _browse_save(self) -> None:
        value, _ = QFileDialog.getSaveFileName(self, "选择输出 Shapefile", self._initial(), self.file_filter)
        if value:
            self.edit.setText(value)


class TaskPage(QWidget):
    def __init__(
        self,
        title: str,
        caption: str,
        input_fields: tuple[tuple[str, str, str, str, str], ...],
        output_fields: tuple[tuple[str, str, str, str, str], ...],
        note_text: str,
        *,
        with_values: bool = False,
        with_recursive: bool = True,
    ) -> None:
        super().__init__()
        self.setObjectName("scrollContent")
        self.fields: dict[str, PathField] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 8, 6)
        layout.setSpacing(8)

        header = QVBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(2)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        header.addWidget(heading)
        description = QLabel(caption)
        description.setObjectName("subtle")
        description.setWordWrap(True)
        header.addWidget(description)
        layout.addLayout(header)

        layout.addWidget(self._path_card("第一步：选择输入数据", input_fields))

        self.parameter_card = QGroupBox("赋值与批量参数" if with_values else "批量选项")
        self.parameter_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        parameter_form = QFormLayout(self.parameter_card)
        parameter_form.setContentsMargins(12, 12, 12, 9)
        parameter_form.setHorizontalSpacing(12)
        parameter_form.setVerticalSpacing(7)
        parameter_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        parameter_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.values = QLineEdit()
        self.values.setMinimumHeight(30)
        self.values.setPlaceholderText("例如：1,2,3；留空则自动识别")
        self.threshold = QSpinBox()
        self.threshold.setMinimumHeight(30)
        self.threshold.setRange(0, 100)
        self.threshold.setValue(65)
        self.threshold.setSuffix(" %")
        self.recursive = QCheckBox("递归扫描子文件夹")
        self.recursive.setChecked(True)

        if with_values:
            parameter_form.addRow("分类像元值", self.values)
            parameter_form.addRow("主类别阈值", self.threshold)
        if with_recursive:
            parameter_form.addRow("批量发现", self.recursive)
        if with_values or with_recursive:
            layout.addWidget(self.parameter_card)
        else:
            self.parameter_card.hide()

        layout.addWidget(self._path_card("第二步：选择输出与软件", output_fields))

        note = QLabel(note_text)
        note.setObjectName("subtle")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)

    def _path_card(self, title: str, fields: tuple[tuple[str, str, str, str, str], ...]) -> QGroupBox:
        card = QGroupBox(title)
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        form = QFormLayout(card)
        form.setContentsMargins(12, 12, 12, 9)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(7)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        for key, label, mode, file_filter, placeholder in fields:
            field = PathField(mode, file_filter, placeholder)
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
        self.hardware = detect_hardware()
        self._custom_cache_dir: Path | None = None
        pruned_dirs, pruned_bytes = prune_expired_cache(self.hardware.recommended_cache_dir, max_age_days=7.0)
        self._apply_initial_geometry()
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
        self._refresh_performance_hint()
        self._refresh_cache_status()
        self._on_tool_tab(0)
        if pruned_dirs:
            self.status.setText(f"已自动回收 {pruned_dirs} 个过期缓存（释放 {_format_bytes(pruned_bytes)}）")

    def _apply_initial_geometry(self) -> None:
        self.setMinimumSize(960, 640)
        target_w, target_h = 1240, 860
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            target_w = min(target_w, max(960, int(available.width() * 0.90)))
            target_h = min(target_h, max(640, int(available.height() * 0.90)))
        self.resize(target_w, target_h)

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(20, 9, 20, 9)
        title = QLabel("SegAssign")
        title.setObjectName("appTitle")
        layout.addWidget(title)
        subtitle = QLabel("影像分割与分类赋值自动化工具")
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
        layout.setContentsMargins(16, 0, 16, 0)
        self.mode = QTabBar()
        self.mode.setExpanding(False)
        self.mode.addTab("00_分割并赋值")
        self.mode.addTab("01_单独分割")
        self.mode.addTab("02_单独赋值")
        self.mode.currentChanged.connect(self._on_tool_tab)
        layout.addWidget(self.mode)
        layout.addStretch(1)
        return frame

    def _wrap_scroll(self, widget: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setObjectName("scrollArea")
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setWidget(widget)
        area.viewport().setObjectName("scrollViewport")
        return area

    def _build_body(self) -> QWidget:
        body = QWidget()
        body.setObjectName("body")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(14, 10, 14, 8)
        body_layout.setSpacing(8)

        self.upper_pane = QWidget()
        self.upper_pane.setMinimumHeight(220)
        upper = QHBoxLayout(self.upper_pane)
        upper.setContentsMargins(0, 0, 0, 0)
        upper.setSpacing(12)

        self.pages = QStackedWidget()
        self.composite_page = TaskPage(
            "分割并赋值",
            "先分割生成面矢量，再使用分类影像完成赋值；输入支持单景 GeoTIFF 或文件夹批量处理。",
            (
                (
                    "process",
                    "亚米级影像",
                    "file-or-directory",
                    "GeoTIFF (*.tif *.tiff)",
                    "选择单幅 .tif 或影像文件夹",
                ),
                (
                    "base",
                    "2 米基础影像",
                    "file-or-directory",
                    "GeoTIFF (*.tif *.tiff)",
                    "选择单幅共用基础影像或对应文件夹",
                ),
                (
                    "classification",
                    "分类影像",
                    "file-or-directory",
                    "GeoTIFF (*.tif *.tiff)",
                    "选择单幅共用分类影像或对应文件夹",
                ),
            ),
            (
                (
                    "output",
                    "输出 SHP / 目录",
                    "save-or-directory",
                    "Shapefile (*.shp)",
                    "单景保存为 .shp；批量请选择输出文件夹",
                ),
                (
                    "ecognition",
                    "eCognition 安装目录",
                    "directory",
                    "",
                    "选择 eCognition Developer 9 安装目录",
                ),
            ),
            "提示：批量模式下，基础/分类影像选单文件时全局共用，选文件夹时优先按相对路径/同名匹配，其次按空间重叠自动匹配。",
            with_values=True,
            with_recursive=True,
        )
        self.segment_page = TaskPage(
            "单独分割",
            "将亚米级影像分割为面矢量；支持单景 GeoTIFF 或文件夹批量处理。",
            (
                (
                    "process",
                    "亚米级影像",
                    "file-or-directory",
                    "GeoTIFF (*.tif *.tiff)",
                    "选择单幅 .tif 或影像文件夹",
                ),
                (
                    "base",
                    "2 米基础影像",
                    "file-or-directory",
                    "GeoTIFF (*.tif *.tiff)",
                    "选择单幅共用基础影像或对应文件夹",
                ),
            ),
            (
                (
                    "output",
                    "输出 SHP / 目录",
                    "save-or-directory",
                    "Shapefile (*.shp)",
                    "单景保存为 .shp；批量请选择输出文件夹",
                ),
                (
                    "ecognition",
                    "eCognition 安装目录",
                    "directory",
                    "",
                    "选择 eCognition Developer 9 安装目录",
                ),
            ),
            "提示：批量模式下保留原文件名与相对子目录结构生成 .shp；输出目录不可位于输入目录内。",
            with_values=False,
            with_recursive=True,
        )
        self.zonal_page = TaskPage(
            "单独赋值",
            "使用一幅区域分类栅格，为单个或一批面矢量更新统计字段。",
            (
                (
                    "vector_source",
                    "输入矢量或文件夹",
                    "file-or-directory",
                    "Shapefile (*.shp)",
                    "选择单个 .shp 或矢量文件夹",
                ),
                (
                    "classification",
                    "区域分类影像",
                    "file",
                    "GeoTIFF (*.tif *.tiff)",
                    "选择区域分类 GeoTIFF",
                ),
            ),
            (("output_directory", "输出 Shapefile 目录", "directory", "", "选择成果输出文件夹"),),
            "提示：支持分区域二次赋值；保留同名 .gtz.npz 旁车文件即可跨非重叠区域精确累加像元计数。",
            with_values=True,
            with_recursive=True,
        )
        self._task_pages = (self.composite_page, self.segment_page, self.zonal_page)
        for page in self._task_pages:
            self.pages.addWidget(self._wrap_scroll(page))

        upper.addWidget(self.pages, 1)
        sidebar_scroll = self._wrap_scroll(self._build_sidebar())
        sidebar_scroll.setFixedWidth(295)
        sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        upper.addWidget(sidebar_scroll)

        self.lower_pane = QWidget()
        self.lower_pane.setMinimumHeight(145)
        lower_layout = QVBoxLayout(self.lower_pane)
        lower_layout.setContentsMargins(0, 6, 0, 0)
        lower_layout.setSpacing(6)
        lower_layout.addLayout(self._build_actions())
        lower_layout.addWidget(self.progress)
        lower_layout.addWidget(self.log, 1)

        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.upper_pane)
        self.splitter.addWidget(self.lower_pane)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([585, 175])

        body_layout.addWidget(self.splitter, 1)
        return body

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("scrollContent")
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(2, 2, 4, 6)
        layout.setSpacing(8)

        current = QGroupBox("当前任务")
        current.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        current_layout = QVBoxLayout(current)
        current_layout.setContentsMargins(12, 12, 12, 8)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        current_layout.addWidget(self.summary)
        layout.addWidget(current)

        flow = QGroupBox("执行流程")
        flow.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        flow_layout = QVBoxLayout(flow)
        flow_layout.setContentsMargins(12, 12, 12, 8)
        self.flow = QLabel()
        self.flow.setWordWrap(True)
        flow_layout.addWidget(self.flow)
        layout.addWidget(flow)

        performance = QGroupBox("硬件自适应调优")
        performance.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        performance_layout = QVBoxLayout(performance)
        performance_layout.setContentsMargins(12, 12, 12, 8)
        performance_layout.setSpacing(4)
        self.performance_group = QButtonGroup(self)
        self.performance_modes: dict[str, QRadioButton] = {}
        for key, title in (
            ("low", "省资源模式（后台友好）"),
            ("auto", "自动最优（匹配本机配置）"),
            ("high", "满载模式（最高并发）"),
        ):
            radio = QRadioButton(title)
            radio.setChecked(key == "auto")
            radio.toggled.connect(self._refresh_performance_hint)
            self.performance_group.addButton(radio)
            self.performance_modes[key] = radio
            performance_layout.addWidget(radio)

        hw = self.hardware
        hardware_label = QLabel(
            f"硬件：{hw.physical_cores} 物理核 / {hw.logical_cpus} 线程 · "
            f"可用 {hw.available_memory_gb:.1f}/{hw.total_memory_gb:.1f} GB"
        )
        hardware_label.setObjectName("subtle")
        hardware_label.setWordWrap(True)
        performance_layout.addWidget(hardware_label)

        self.worker_hint = QLabel()
        self.worker_hint.setObjectName("subtle")
        self.worker_hint.setWordWrap(True)
        performance_layout.addWidget(self.worker_hint)
        layout.addWidget(performance)

        cache_box = QGroupBox("高速缓存管理")
        cache_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        cache_layout = QVBoxLayout(cache_box)
        cache_layout.setContentsMargins(12, 12, 12, 8)
        cache_layout.setSpacing(6)
        self.cache_label = QLabel()
        self.cache_label.setObjectName("subtle")
        self.cache_label.setWordWrap(True)
        cache_layout.addWidget(self.cache_label)
        cache_buttons = QHBoxLayout()
        cache_buttons.setSpacing(6)
        self.change_cache_button = QPushButton("更改缓存盘")
        self.change_cache_button.setMinimumHeight(28)
        self.change_cache_button.clicked.connect(self._choose_cache_dir)
        self.clear_cache_button = QPushButton("清理缓存")
        self.clear_cache_button.setMinimumHeight(28)
        self.clear_cache_button.clicked.connect(self._clear_cache)
        cache_buttons.addWidget(self.change_cache_button)
        cache_buttons.addWidget(self.clear_cache_button)
        cache_layout.addLayout(cache_buttons)
        layout.addWidget(cache_box)

        layout.addStretch(1)
        return sidebar

    def _build_actions(self) -> QHBoxLayout:
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.resume = QCheckBox("断点续跑")
        self.resume.setChecked(True)
        self.auto_cleanup = QCheckBox("完成后自动清理缓存")
        self.auto_cleanup.setChecked(True)
        self.open_output_button = QPushButton("打开成果目录")
        self.open_output_button.setMinimumHeight(32)
        self.plan_button = QPushButton("检查配置")
        self.plan_button.setMinimumHeight(32)
        self.run_button = QPushButton("一键执行：分割并赋值")
        self.run_button.setObjectName("primaryButton")
        self.run_button.setMinimumHeight(34)
        self.cancel_button = QPushButton("安全取消")
        self.cancel_button.setMinimumHeight(32)
        self.cancel_button.setEnabled(False)

        self.open_output_button.clicked.connect(self._open_output)
        self.plan_button.clicked.connect(self._show_plan)
        self.run_button.clicked.connect(self._run)
        self.cancel_button.clicked.connect(self._cancel)

        actions.addWidget(self.resume)
        actions.addWidget(self.auto_cleanup)
        actions.addStretch(1)
        actions.addWidget(self.open_output_button)
        actions.addWidget(self.plan_button)
        actions.addWidget(self.run_button)
        actions.addWidget(self.cancel_button)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setMinimumHeight(22)
        self.progress.setTextVisible(True)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("任务事件、配置检查详情和运行报告将显示在这里。")
        self.log.setMaximumBlockCount(4000)
        self.log.setMinimumHeight(72)

        self.status = QLabel("就绪")
        return actions

    def _build_footer(self) -> QWidget:
        footer = QFrame()
        footer.setObjectName("footer")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(18, 6, 18, 6)
        layout.addWidget(self.status)
        layout.addStretch(1)
        layout.addWidget(QLabel("SegAssign"))
        return footer

    def _connect_summary_updates(self) -> None:
        for page in self._task_pages:
            for field in page.required_fields():
                field.edit.textChanged.connect(self._refresh_summary)

    def _cache_root(self) -> Path:
        return self._custom_cache_dir or self.hardware.recommended_cache_dir

    def _refresh_cache_status(self) -> None:
        if not hasattr(self, "cache_label"):
            return
        root = self._cache_root()
        used = _format_bytes(cache_usage_bytes(root))
        location = str(root) if self._custom_cache_dir else f"{self.hardware.cache_drive_label}"
        self.cache_label.setText(f"缓存盘：{location}\n当前占用：{used}（失败自动保留断点，超 7 天自动回收）")

    def _choose_cache_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择高速缓存目录", str(self._cache_root().parent))
        if chosen:
            self._custom_cache_dir = Path(chosen).expanduser().resolve()
            self._refresh_cache_status()

    def _clear_cache(self) -> None:
        if self.controller.running:
            return
        removed, freed = clear_cache(self._cache_root())
        self._refresh_cache_status()
        if removed:
            self.status.setText(f"已清理缓存，释放 {_format_bytes(freed)}")
        else:
            self.status.setText("当前无可清理的临时缓存")

    def _on_tool_tab(self, index: int) -> None:
        if not hasattr(self, "pages"):
            return
        self.pages.setCurrentIndex(index)
        titles = ("一键执行：分割并赋值", "开始单独分割", "开始批量赋值")
        flows = (
            "① 发现并配对影像（单景/文件夹）\n② 分割并生成面矢量\n③ 使用分类影像完成赋值",
            "① 发现并配对影像（单景/文件夹）\n② 准备分块与有效区\n③ eCognition 分割与后处理拼接",
            "① 自动发现全部 Shapefile\n② 使用区域分类影像逐项赋值\n③ 原名发布到输出目录",
        )
        self.run_button.setText(titles[index])
        self.flow.setText(flows[index])
        self._refresh_summary()

    def _current_page(self) -> TaskPage:
        return self._task_pages[self.pages.currentIndex()]

    def _is_batch_mode(self, page: TaskPage) -> bool:
        if page is self.zonal_page:
            return True
        keys = ("process", "base", "classification") if page is self.composite_page else ("process", "base")
        for key in keys:
            raw = page.fields[key].raw_text()
            if raw and Path(raw).expanduser().is_dir():
                return True
        output_raw = page.fields["output"].raw_text()
        if output_raw and Path(output_raw).suffix.casefold() != ".shp":
            return True
        return False

    def _refresh_summary(self) -> None:
        if not hasattr(self, "summary"):
            return
        page = self._current_page()
        fields = page.required_fields()
        filled = sum(bool(field.raw_text()) for field in fields)
        mode_text = "文件夹批处理模式" if self._is_batch_mode(page) else "单景文件模式"
        if filled == len(fields):
            tail = "配置完整，可以检查或执行。"
        else:
            tail = f"还需填写 {len(fields) - filled} 项必填内容。"
        self.summary.setText(f"已填写 {filled}/{len(fields)} 项\n识别模式：{mode_text}\n{tail}")

    def _selected_performance_mode(self) -> str:
        for key, radio in self.performance_modes.items():
            if radio.isChecked():
                return key
        return "auto"

    def _refresh_performance_hint(self) -> None:
        if not hasattr(self, "worker_hint"):
            return
        budget = self._resource_budget()
        scheduler = ResourceScheduler(budget)
        eco = scheduler.ecognition_workers()
        post = scheduler.postprocess_workers()
        zonal_w = scheduler.zonal_workers(1024, 3072**2 * 16)
        mem_pct = int(budget.memory_fraction * 100)
        self.worker_hint.setText(
            f"当前并发：eCognition {eco} · 后处理 {post} · 赋值 {zonal_w}（内存上限 {mem_pct}%）"
        )

    def _backend(self):
        page = self._current_page()
        work_dir = self._cache_root()
        if page is self.zonal_page:
            options = ZonalOptions(valid_values=page.valid_values(), class_threshold=page.threshold.value())
            request = ZonalBatchRequest(
                page.path("vector_source"),
                page.path("classification"),
                page.path("output_directory"),
                options,
                recursive=page.recursive.isChecked(),
            )
            return zonal.plan_batch, zonal.run_batch, request, work_dir, request.output_directory

        if page is self.segment_page:
            process = page.path("process")
            base = page.path("base")
            output = page.path("output")
            ecognition = page.path("ecognition")
            if self._is_batch_mode(page):
                request = SegmentBatchRequest(
                    process,
                    base,
                    output,
                    ecognition,
                    recursive=page.recursive.isChecked(),
                )
                return segment.plan_batch, segment.run_batch, request, work_dir, output
            request = SegmentRequest(process, base, output, ecognition)
            return segment.plan, segment.run, request, work_dir, output.parent

        process = page.path("process")
        base = page.path("base")
        classification = page.path("classification")
        output = page.path("output")
        ecognition = page.path("ecognition")
        options = ZonalOptions(valid_values=page.valid_values(), class_threshold=page.threshold.value())
        if self._is_batch_mode(page):
            request = CompositeBatchRequest(
                process,
                base,
                classification,
                output,
                ecognition,
                zonal_options=options,
                recursive=page.recursive.isChecked(),
            )
            return composite.plan_batch, composite.run_batch, request, work_dir, output
        request = CompositeRequest(
            process,
            base,
            classification,
            output,
            ecognition,
            zonal_options=options,
        )
        return composite.plan, composite.run, request, work_dir, output.parent

    def _resource_budget(self) -> ResourceBudget:
        return build_resource_budget(self.hardware, self._selected_performance_mode())

    def _show_plan(self) -> None:
        try:
            planner, _, request, _, output_directory = self._backend()
            execution_plan = planner(request)
        except Exception as error:
            QMessageBox.warning(self, "配置错误", str(error))
            return
        self._last_output_directory = output_directory
        summary = {
            "tool": execution_plan.tool,
            "cache_dir": str(self._cache_root()),
            "stages": execution_plan.stages,
            "metrics": dict(execution_plan.metrics),
        }
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
                cleanup_on_success=self.auto_cleanup.isChecked(),
            )
        except Exception as error:
            QMessageBox.warning(self, "无法启动", str(error))
            return
        self._last_output_directory = output_directory
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
        self._refresh_cache_status()

    def _on_failure(self, detail: str) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText("任务线程异常")
        self.header_status.setText("失败")
        self.log.appendPlainText(detail)
        self._refresh_cache_status()

    def _set_running(self, running: bool) -> None:
        self.mode.setEnabled(not running)
        self.pages.setEnabled(not running)
        self.resume.setEnabled(not running)
        self.auto_cleanup.setEnabled(not running)
        self.change_cache_button.setEnabled(not running)
        self.clear_cache_button.setEnabled(not running)
        self.plan_button.setEnabled(not running)
        self.run_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        for radio in self.performance_modes.values():
            radio.setEnabled(not running)
        if not running and self._close_when_stopped:
            self._close_when_stopped = False
            self.close()

    def _open_output(self) -> None:
        target = self._last_output_directory
        if target is None:
            page = self._current_page()
            key = "output_directory" if page is self.zonal_page else "output"
            raw = page.fields[key].raw_text()
            if raw:
                candidate = Path(raw).expanduser().resolve()
                target = candidate if candidate.is_dir() else candidate.parent
        if target is None or not target.is_dir():
            self.status.setText("尚无可打开的成果目录")
            return
        QDesktopServices.openUrl(target.as_uri())

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
            "QMainWindow, #body, #scrollArea, #scrollViewport, #scrollContent { background: #f5f7fa; }"
            "QWidget { font-family: 'Microsoft YaHei UI'; font-size: 13px; color: #1f2937; }"
            "#header, #tabsFrame, #footer { background: white; }"
            "#header { border-bottom: 1px solid #e5e7eb; }"
            "#tabsFrame { border-bottom: 1px solid #e5e7eb; }"
            "#footer { border-top: 1px solid #e5e7eb; }"
            "#appTitle { font-size: 20px; font-weight: 700; color: #111827; }"
            "#pageTitle { font-size: 19px; font-weight: 700; color: #111827; }"
            "#subtle { color: #667085; }"
            "#statusPill { background: #eef2ff; color: #3949ab; padding: 5px 12px; "
            "border-radius: 4px; font-weight: 600; }"
            "QTabBar::tab { background: white; padding: 9px 20px; border: 1px solid #e5e7eb; "
            "border-bottom: 0; margin-right: 4px; }"
            "QTabBar::tab:selected { color: #1d4ed8; border-bottom: 3px solid #2563eb; font-weight: 700; }"
            "QGroupBox { background: white; border: 1px solid #dfe3e8; border-radius: 6px; margin-top: 10px; "
            "padding: 10px 10px 8px 10px; font-weight: 700; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }"
            "QLineEdit, QSpinBox, QPlainTextEdit { background: white; border: 1px solid #cfd5dd; "
            "border-radius: 4px; padding: 4px 8px; font-weight: 400; }"
            "QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus { border-color: #2563eb; }"
            "QPushButton { background: white; border: 1px solid #c9d0d9; border-radius: 4px; padding: 5px 12px; }"
            "QPushButton:hover { background: #f0f4f8; }"
            "#primaryButton { background: #2563eb; color: white; border-color: #2563eb; font-weight: 700; "
            "padding: 6px 18px; }"
            "#primaryButton:hover { background: #1d4ed8; }"
            "#primaryButton:disabled { background: #93a8d8; border-color: #93a8d8; }"
            "QProgressBar { border: 1px solid #cfd5dd; border-radius: 4px; text-align: center; background: white; }"
            "QProgressBar::chunk { background: #2563eb; border-radius: 3px; }"
            "QSplitter::handle { background: #e5e7eb; height: 4px; }"
            "QSplitter::handle:hover { background: #93c5fd; }"
        )
