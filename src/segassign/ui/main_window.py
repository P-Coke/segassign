"""Navigation-first 3-step PySide6 wizard shell backed by the public SegAssign services.

Wizard flow:
  Step 1 (配置参数): Choose task (00/01/02), select single files or folders, tune hardware/cache.
  Step 2 (清单确认): Inspect all discovered & paired files in an interactive checked table, verify parameters.
  Step 3 (进度监控): Monitor global progress + independent per-file progress bars + live CPU/RAM + log.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import psutil
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
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
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import composite, segment, zonal
from ..ecognition import detect_ecognition_bin
from ..models import (
    CompositeBatchRequest,
    CompositeRequest,
    ExecutionPlan,
    ResourceBudget,
    SegmentBatchRequest,
    SegmentRequest,
    ZonalBatchRequest,
    ZonalOptions,
)
from ..runtime import (
    ResourceScheduler,
    build_resource_budget,
    cache_usage_bytes,
    clear_cache,
    configure_gdal_cache,
    detect_hardware,
    prune_expired_cache,
)
from .controller import TaskController
from .progress import BatchMonitorModel, BatchMonitorSnapshot, ProgressSnapshot, ProgressTracker
from .tile_map import TileMapPanel

_STATE_TEXT_COLORS = {
    "queued": QColor("#64748b"),
    "running": QColor("#1d4ed8"),
    "succeeded": QColor("#15803d"),
    "cancelled": QColor("#b45309"),
    "failed": QColor("#b91c1c"),
}


def _format_bytes(size: int) -> str:
    if size < 1024**2:
        return f"{size / 1024:.1f} KB"
    if size < 1024**3:
        return f"{size / 1024**2:.1f} MB"
    return f"{size / 1024**3:.2f} GB"


class _DroppableLineEdit(QLineEdit):
    """QLineEdit supporting direct drag-and-drop of local files or folders from Explorer."""

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if urls:
            local = urls[0].toLocalFile()
            if local:
                self.setText(local)
                event.acceptProposedAction()
                return
        super().dropEvent(event)


class PathField(QWidget):
    """Path entry supporting file, directory, file/directory, or save/directory input."""

    def __init__(self, mode: str, file_filter: str = "所有文件 (*)", placeholder: str = "") -> None:
        super().__init__()
        self.mode = mode
        self.file_filter = file_filter
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.edit = _DroppableLineEdit()
        self.edit.setClearButtonEnabled(True)
        self.edit.setMinimumHeight(30)
        if placeholder:
            self.edit.setPlaceholderText(placeholder)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.edit, 1)

        if mode == "file-or-directory":
            layout.addWidget(self._make_button("选择文件", self._browse_file))
            layout.addWidget(self._make_button("选择文件夹", self._browse_directory))
        elif mode == "save-or-directory":
            layout.addWidget(self._make_button("保存 SHP", self._browse_save))
            layout.addWidget(self._make_button("选择文件夹", self._browse_directory))
        else:
            label = "选择文件夹" if mode == "directory" else ("保存 SHP" if mode == "save" else "选择文件")
            layout.addWidget(self._make_button(label, self._browse))

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
            raise ValueError("存在尚未填写的必填路径")
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

        layout.addWidget(self._path_card("一、选择输入数据（支持拖拽文件或文件夹）", input_fields))

        self.parameter_card = QGroupBox("二、赋值与批量选项" if with_values else "二、批量扫描选项")
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

        layout.addWidget(self._path_card("三、选择输出位置与软件环境", output_fields))

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
        configure_gdal_cache(self.hardware)
        self._detected_ecognition = detect_ecognition_bin()
        self._syncing_ecognition = False
        self._custom_cache_dir: Path | None = None
        pruned_dirs, pruned_bytes = prune_expired_cache(self.hardware.recommended_cache_dir, max_age_days=7.0)
        self._apply_initial_geometry()
        self._close_when_stopped = False
        self._progress_tracker = ProgressTracker()
        self._batch_monitor = BatchMonitorModel()
        self._using_batch_monitor = False
        self._progress_state = ""
        self._staged_plan: ExecutionPlan | None = None
        self._staged_backend: tuple | None = None
        self._staged_items: list[dict] = []
        self._monitor_item_ids: list[str] = []
        self._monitor_bars: list[QProgressBar] = []
        self._monitor_bar_states: list[str] = []
        self._last_output_directory: Path | None = None

        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(1000)
        self._progress_timer.timeout.connect(self._on_progress_tick)
        psutil.cpu_percent(interval=None)

        self.controller = TaskController(self)
        self.controller.events_batch.connect(self._on_events_batch)
        self.controller.result_ready.connect(self._on_result)
        self.controller.failed.connect(self._on_failure)
        self.controller.running_changed.connect(self._set_running)

        root = QWidget()
        shell = QVBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        shell.addWidget(self._build_header())
        shell.addWidget(self._build_wizard(), 1)
        shell.addWidget(self._build_footer())
        self.setCentralWidget(root)
        self._apply_style()
        self._connect_summary_updates()
        self._prefill_ecognition()
        self._refresh_performance_hint()
        self._refresh_cache_status()
        self._apply_progress_snapshot(self._progress_tracker.idle())
        self._on_tool_tab(0)
        self._set_wizard_step(0)

        startup_notes: list[str] = []
        if self._detected_ecognition is not None:
            startup_notes.append(f"已自动侦测 eCognition：{self._detected_ecognition}")
        if pruned_dirs:
            startup_notes.append(f"已自动回收 {pruned_dirs} 个过期缓存（释放 {_format_bytes(pruned_bytes)}）")
        if startup_notes:
            self.status.setText(" · ".join(startup_notes))

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
        layout.setContentsMargins(18, 8, 18, 8)
        layout.setSpacing(12)

        brand = QHBoxLayout()
        brand.setContentsMargins(0, 0, 0, 0)
        brand.setSpacing(8)
        title = QLabel("SegAssign")
        title.setObjectName("appTitle")
        brand.addWidget(title)
        subtitle = QLabel("影像分割与分类赋值")
        subtitle.setObjectName("subtle")
        brand.addWidget(subtitle)
        layout.addLayout(brand)
        layout.addStretch(1)

        stepper = QHBoxLayout()
        stepper.setContentsMargins(0, 0, 0, 0)
        stepper.setSpacing(6)
        self.step_buttons: list[QPushButton] = []
        step_specs = ("① 参数配置", "② 清单确认", "③ 进度监控")
        for idx, label in enumerate(step_specs):
            btn = QPushButton(label)
            btn.setObjectName("stepPill")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setMinimumHeight(30)
            btn.clicked.connect(lambda _checked=False, step=idx: self._on_step_clicked(step))
            self.step_buttons.append(btn)
            stepper.addWidget(btn)
            if idx < len(step_specs) - 1:
                arrow = QLabel("›")
                arrow.setObjectName("stepArrow")
                stepper.addWidget(arrow)
        layout.addLayout(stepper)
        layout.addStretch(1)

        self.header_status = QLabel("就绪")
        self.header_status.setObjectName("statusPill")
        layout.addWidget(self.header_status)
        return header

    def _wrap_scroll(self, widget: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setObjectName("scrollArea")
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setWidget(widget)
        area.viewport().setObjectName("scrollViewport")
        return area

    def _build_wizard(self) -> QWidget:
        self.wizard_stack = QStackedWidget()
        self.wizard_stack.setObjectName("body")
        self.wizard_stack.addWidget(self._build_step1_config())
        self.wizard_stack.addWidget(self._build_step2_confirm())
        self.wizard_stack.addWidget(self._build_step3_monitor())
        return self.wizard_stack

    # ------------------------------------------------------------------
    # Step 1: Configuration & Parameters
    # ------------------------------------------------------------------
    def _build_step1_config(self) -> QWidget:
        container = QWidget()
        container.setObjectName("body")
        root_layout = QVBoxLayout(container)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        tabs_frame = QFrame()
        tabs_frame.setObjectName("tabsFrame")
        tabs_layout = QHBoxLayout(tabs_frame)
        tabs_layout.setContentsMargins(16, 0, 16, 0)
        self.mode = QTabBar()
        self.mode.setExpanding(False)
        self.mode.addTab("00_分割并赋值")
        self.mode.addTab("01_单独分割")
        self.mode.addTab("02_单独赋值")
        self.mode.currentChanged.connect(self._on_tool_tab)
        tabs_layout.addWidget(self.mode)
        tabs_layout.addStretch(1)
        root_layout.addWidget(tabs_frame)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(14, 10, 14, 10)
        content_layout.setSpacing(8)

        self.upper_pane = QWidget()
        self.upper_pane.setMinimumHeight(240)
        upper = QHBoxLayout(self.upper_pane)
        upper.setContentsMargins(0, 0, 0, 0)
        upper.setSpacing(12)

        self.pages = QStackedWidget()
        self.composite_page = TaskPage(
            "00 · 分割并赋值",
            "先完成影像分割再写入分类统计字段；支持单景 GeoTIFF 或文件夹批量自动配对。",
            (
                (
                    "process",
                    "亚米级影像",
                    "file-or-directory",
                    "GeoTIFF (*.tif *.tiff)",
                    "选择单幅 .tif 或影像文件夹（可直接拖入）",
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
                    "eCognition 目录",
                    "directory",
                    "",
                    "自动侦测，或选择 eCognition Developer 9 安装目录 / bin 目录",
                ),
            ),
            "提示：点击右下角“下一步”将自动扫描全部待处理影像并进入清单确认界面，核对勾选后再开始计算。",
            with_values=True,
            with_recursive=True,
        )
        self.segment_page = TaskPage(
            "01 · 单独分割",
            "将亚米级影像分割为面矢量；支持单景 GeoTIFF 或文件夹批量自动配对与跨景尾段加速。",
            (
                (
                    "process",
                    "亚米级影像",
                    "file-or-directory",
                    "GeoTIFF (*.tif *.tiff)",
                    "选择单幅 .tif 或影像文件夹（可直接拖入）",
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
                    "eCognition 目录",
                    "directory",
                    "",
                    "自动侦测，或选择 eCognition Developer 9 安装目录 / bin 目录",
                ),
            ),
            "提示：批量模式下保留原文件名与相对子目录结构生成 .shp；输出目录不可位于输入目录内。",
            with_values=False,
            with_recursive=True,
        )
        self.zonal_page = TaskPage(
            "02 · 单独赋值",
            "使用一幅区域分类栅格，为单个或一批面矢量更新类别占比统计字段。",
            (
                (
                    "vector_source",
                    "输入矢量或文件夹",
                    "file-or-directory",
                    "Shapefile (*.shp)",
                    "选择单个 .shp 或矢量文件夹（可直接拖入）",
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
        sidebar_scroll.setFixedWidth(298)
        sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        upper.addWidget(sidebar_scroll)
        content_layout.addWidget(self.upper_pane, 1)

        # Bottom action bar of Step 1
        self.lower_pane = QFrame()
        self.lower_pane.setObjectName("wizardBar")
        action_bar = QHBoxLayout(self.lower_pane)
        action_bar.setContentsMargins(14, 10, 14, 10)
        action_bar.setSpacing(10)

        self.resume = QCheckBox("断点续跑")
        self.resume.setChecked(True)
        self.auto_cleanup = QCheckBox("完成后自动清理缓存")
        self.auto_cleanup.setChecked(True)
        self.plan_button = QPushButton("快速检查")
        self.plan_button.setMinimumHeight(34)
        self.plan_button.clicked.connect(self._show_plan)

        self.next_to_confirm_button = QPushButton("下一步：扫描并确认清单 →")
        self.next_to_confirm_button.setObjectName("primaryButton")
        self.next_to_confirm_button.setMinimumHeight(36)
        self.next_to_confirm_button.clicked.connect(self._go_to_confirm)

        action_bar.addWidget(self.resume)
        action_bar.addWidget(self.auto_cleanup)
        action_bar.addStretch(1)
        action_bar.addWidget(self.plan_button)
        action_bar.addWidget(self.next_to_confirm_button)
        content_layout.addWidget(self.lower_pane)

        root_layout.addWidget(content, 1)
        return container

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("scrollContent")
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(2, 2, 4, 6)
        layout.setSpacing(8)

        current = QGroupBox("配置概览")
        current.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        current_layout = QVBoxLayout(current)
        current_layout.setContentsMargins(12, 12, 12, 8)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        current_layout.addWidget(self.summary)
        layout.addWidget(current)

        performance = QGroupBox("动态 90% 调优引擎")
        performance.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        performance_layout = QVBoxLayout(performance)
        performance_layout.setContentsMargins(12, 12, 12, 8)
        performance_layout.setSpacing(4)
        self.performance_group = QButtonGroup(self)
        self.performance_modes: dict[str, QRadioButton] = {}
        for key, title in (
            ("low", "省资源模式（约 45% 占用）"),
            ("auto", "自动极速（动态 90% 调优）"),
            ("high", "极限满载（约 95% 占用）"),
        ):
            radio = QRadioButton(title)
            radio.setChecked(key == "auto")
            radio.toggled.connect(self._refresh_performance_hint)
            self.performance_group.addButton(radio)
            self.performance_modes[key] = radio
            performance_layout.addWidget(radio)

        self.hardware_label = QLabel()
        self.hardware_label.setObjectName("subtle")
        self.hardware_label.setWordWrap(True)
        performance_layout.addWidget(self.hardware_label)

        self.worker_hint = QLabel()
        self.worker_hint.setObjectName("subtle")
        self.worker_hint.setWordWrap(True)
        performance_layout.addWidget(self.worker_hint)

        tail_note = QLabel("批量调度：前序任务进入尾段或资源回落时自动提前启动下一景，保持高吞吐。")
        tail_note.setObjectName("subtle")
        tail_note.setWordWrap(True)
        performance_layout.addWidget(tail_note)
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

    # ------------------------------------------------------------------
    # Step 2: Confirmation Table (Detected Files + Parameter Summary)
    # ------------------------------------------------------------------
    def _build_step2_confirm(self) -> QWidget:
        page = QWidget()
        page.setObjectName("body")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(10)

        header_row = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        heading = QLabel("确认待处理文件与运行参数")
        heading.setObjectName("pageTitle")
        title_box.addWidget(heading)
        sub = QLabel("已自动侦测并配对以下文件，默认全部勾选；可取消不需要处理的条目后启动任务。")
        sub.setObjectName("subtle")
        title_box.addWidget(sub)
        header_row.addLayout(title_box, 1)
        layout.addLayout(header_row)

        summary_card = QGroupBox("本次运行参数核对")
        summary_layout = QVBoxLayout(summary_card)
        summary_layout.setContentsMargins(12, 12, 12, 10)
        summary_layout.setSpacing(6)
        self.confirm_params_label = QLabel()
        self.confirm_params_label.setWordWrap(True)
        summary_layout.addWidget(self.confirm_params_label)
        self.confirm_warnings_label = QLabel()
        self.confirm_warnings_label.setObjectName("warningBanner")
        self.confirm_warnings_label.setWordWrap(True)
        self.confirm_warnings_label.hide()
        summary_layout.addWidget(self.confirm_warnings_label)
        layout.addWidget(summary_card)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.confirm_count_label = QLabel("共侦测到 0 项，已勾选 0 项")
        self.confirm_count_label.setObjectName("sectionStrong")
        toolbar.addWidget(self.confirm_count_label)
        toolbar.addStretch(1)

        self.select_all_button = QPushButton("全选")
        self.select_all_button.setMinimumHeight(28)
        self.select_all_button.clicked.connect(lambda: self._set_all_confirm_checked(True))
        self.invert_select_button = QPushButton("反选")
        self.invert_select_button.setMinimumHeight(28)
        self.invert_select_button.clicked.connect(self._invert_confirm_checked)
        self.clear_select_button = QPushButton("全不选")
        self.clear_select_button.setMinimumHeight(28)
        self.clear_select_button.clicked.connect(lambda: self._set_all_confirm_checked(False))
        self.rescan_button = QPushButton("重新扫描")
        self.rescan_button.setMinimumHeight(28)
        self.rescan_button.clicked.connect(self._go_to_confirm)
        for btn in (self.select_all_button, self.invert_select_button, self.clear_select_button, self.rescan_button):
            toolbar.addWidget(btn)
        layout.addLayout(toolbar)

        self.confirm_table = QTableWidget(0, 7)
        self.confirm_table.setHorizontalHeaderLabels(
            ["勾选", "#", "待处理文件", "规格 / 要素", "配对辅助数据", "匹配方式", "输出成果"]
        )
        self.confirm_table.setAlternatingRowColors(True)
        self.confirm_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.confirm_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.confirm_table.verticalHeader().setVisible(False)
        self.confirm_table.verticalHeader().setDefaultSectionSize(36)
        c_header = self.confirm_table.horizontalHeader()
        c_header.setSectionResizeMode(0, QHeaderView.Fixed)
        c_header.setSectionResizeMode(1, QHeaderView.Fixed)
        c_header.setSectionResizeMode(2, QHeaderView.Stretch)
        c_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        c_header.setSectionResizeMode(4, QHeaderView.Stretch)
        c_header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        c_header.setSectionResizeMode(6, QHeaderView.Stretch)
        self.confirm_table.setColumnWidth(0, 52)
        self.confirm_table.setColumnWidth(1, 42)
        self.confirm_table.itemChanged.connect(self._on_confirm_item_changed)
        layout.addWidget(self.confirm_table, 1)

        nav_bar = QFrame()
        nav_bar.setObjectName("wizardBar")
        nav_layout = QHBoxLayout(nav_bar)
        nav_layout.setContentsMargins(14, 10, 14, 10)
        nav_layout.setSpacing(10)
        self.back_to_config_button = QPushButton("← 上一步：修改参数")
        self.back_to_config_button.setMinimumHeight(34)
        self.back_to_config_button.clicked.connect(lambda: self._set_wizard_step(0))

        self.run_button = QPushButton("开始执行（已选 0 项）→")
        self.run_button.setObjectName("primaryButton")
        self.run_button.setMinimumHeight(36)
        self.run_button.clicked.connect(self._run)

        nav_layout.addWidget(self.back_to_config_button)
        nav_layout.addStretch(1)
        nav_layout.addWidget(self.run_button)
        layout.addWidget(nav_bar)
        return page

    # ------------------------------------------------------------------
    # Step 3: Progress Monitor (Total + Per-File Progress Bars + Log)
    # ------------------------------------------------------------------
    def _build_step3_monitor(self) -> QWidget:
        page = QWidget()
        page.setObjectName("body")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(8)

        total_card = QGroupBox("总体执行进度")
        total_layout = QVBoxLayout(total_card)
        total_layout.setContentsMargins(12, 12, 12, 10)
        total_layout.setSpacing(6)

        info_row = QHBoxLayout()
        info_row.setSpacing(10)
        self.progress_detail = QLabel("就绪 — 配置路径后点击执行")
        self.progress_detail.setObjectName("progressDetail")
        self.resource_meter = QLabel("CPU --% · RAM --%")
        self.resource_meter.setObjectName("resourcePill")
        self.progress_time = QLabel("")
        self.progress_time.setObjectName("progressTime")
        self.progress_time.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        info_row.addWidget(self.progress_detail, 1)
        info_row.addWidget(self.resource_meter)
        info_row.addWidget(self.progress_time)
        total_layout.addLayout(info_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setMinimumHeight(26)
        self.progress.setTextVisible(True)
        self.progress.setFormat("0% · 就绪")
        total_layout.addWidget(self.progress)
        layout.addWidget(total_card)

        self.monitor_table = QTableWidget(0, 6)
        self.monitor_table.setHorizontalHeaderLabels(
            ["#", "待处理文件", "状态", "独立进度", "当前阶段详情", "耗时"]
        )
        self.monitor_table.setAlternatingRowColors(True)
        self.monitor_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.monitor_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.monitor_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.monitor_table.verticalHeader().setVisible(False)
        self.monitor_table.verticalHeader().setDefaultSectionSize(38)
        m_header = self.monitor_table.horizontalHeader()
        m_header.setSectionResizeMode(0, QHeaderView.Fixed)
        m_header.setSectionResizeMode(1, QHeaderView.Stretch)
        m_header.setSectionResizeMode(2, QHeaderView.Fixed)
        m_header.setSectionResizeMode(3, QHeaderView.Stretch)
        m_header.setSectionResizeMode(4, QHeaderView.Stretch)
        m_header.setSectionResizeMode(5, QHeaderView.Fixed)
        self.monitor_table.setColumnWidth(0, 42)
        self.monitor_table.setColumnWidth(2, 128)
        self.monitor_table.setColumnWidth(5, 76)
        self.monitor_table.cellClicked.connect(self._on_monitor_row_clicked)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("任务事件与运行报告详情将显示在这里。")
        self.log.setMaximumBlockCount(1500)
        self.log.setMinimumHeight(72)

        self.monitor_splitter = QSplitter(Qt.Vertical)
        self.monitor_splitter.setChildrenCollapsible(False)
        self.monitor_splitter.addWidget(self.monitor_table)
        self.monitor_splitter.addWidget(self.log)
        self.monitor_splitter.setStretchFactor(0, 3)
        self.monitor_splitter.setStretchFactor(1, 1)
        self.monitor_splitter.setSizes([380, 120])

        self.tile_map = TileMapPanel(self)

        self.monitor_hsplit = QSplitter(Qt.Horizontal)
        self.monitor_hsplit.setChildrenCollapsible(False)
        self.monitor_hsplit.addWidget(self.monitor_splitter)
        self.monitor_hsplit.addWidget(self.tile_map)
        self.monitor_hsplit.setStretchFactor(0, 3)
        self.monitor_hsplit.setStretchFactor(1, 2)
        self.monitor_hsplit.setSizes([720, 440])
        layout.addWidget(self.monitor_hsplit, 1)

        monitor_bar = QFrame()
        monitor_bar.setObjectName("wizardBar")
        m_bar_layout = QHBoxLayout(monitor_bar)
        m_bar_layout.setContentsMargins(14, 10, 14, 10)
        m_bar_layout.setSpacing(10)

        self.back_from_monitor_button = QPushButton("← 返回参数配置")
        self.back_from_monitor_button.setMinimumHeight(34)
        self.back_from_monitor_button.clicked.connect(lambda: self._set_wizard_step(0))
        self.back_to_confirm_from_monitor = QPushButton("返回清单重选")
        self.back_to_confirm_from_monitor.setMinimumHeight(34)
        self.back_to_confirm_from_monitor.clicked.connect(lambda: self._set_wizard_step(1))
        self.open_output_button = QPushButton("打开成果目录")
        self.open_output_button.setMinimumHeight(34)
        self.open_output_button.clicked.connect(self._open_output)
        self.cancel_button = QPushButton("安全取消")
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.setMinimumHeight(34)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)

        m_bar_layout.addWidget(self.back_from_monitor_button)
        m_bar_layout.addWidget(self.back_to_confirm_from_monitor)
        m_bar_layout.addStretch(1)
        m_bar_layout.addWidget(self.open_output_button)
        m_bar_layout.addWidget(self.cancel_button)
        layout.addWidget(monitor_bar)

        self.status = QLabel("就绪")
        return page

    def _build_footer(self) -> QWidget:
        footer = QFrame()
        footer.setObjectName("footer")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(18, 6, 18, 6)
        layout.addWidget(self.status, 1)
        layout.addWidget(QLabel("SegAssign"))
        return footer

    # ------------------------------------------------------------------
    # Stepper & Navigation State
    # ------------------------------------------------------------------
    def _set_wizard_step(self, step: int) -> None:
        self.wizard_stack.setCurrentIndex(step)
        for idx, btn in enumerate(self.step_buttons):
            if idx == step:
                state = "active"
            elif idx < step:
                state = "done"
            else:
                state = "idle"
            btn.setProperty("stepState", state)
            style = btn.style()
            if style is not None:
                style.unpolish(btn)
                style.polish(btn)
            if idx == 0:
                btn.setEnabled(not self.controller.running)
            elif idx == 1:
                btn.setEnabled(not self.controller.running and self._staged_plan is not None)
            else:
                btn.setEnabled(self.controller.running or bool(self._monitor_bars))

    def _on_step_clicked(self, step: int) -> None:
        if self.controller.running and step != 2:
            return
        if step == 1 and self._staged_plan is None:
            self._go_to_confirm()
            return
        self._set_wizard_step(step)

    # ------------------------------------------------------------------
    # Step 1 Helpers (Summary, eCognition Sync, Hardware & Cache)
    # ------------------------------------------------------------------
    def _connect_summary_updates(self) -> None:
        for page in self._task_pages:
            for field in page.required_fields():
                field.edit.textChanged.connect(self._refresh_summary)
        self.composite_page.fields["ecognition"].edit.textChanged.connect(
            lambda text: self._sync_ecognition_field(self.segment_page, text)
        )
        self.segment_page.fields["ecognition"].edit.textChanged.connect(
            lambda text: self._sync_ecognition_field(self.composite_page, text)
        )

    def _prefill_ecognition(self) -> None:
        tooltip = "自动从常用目录与注册表侦测 eCognition 9；支持选择安装根目录或 bin 目录。"
        for page in (self.composite_page, self.segment_page):
            page.fields["ecognition"].edit.setToolTip(tooltip)
        if self._detected_ecognition is not None:
            self._syncing_ecognition = True
            try:
                detected_text = str(self._detected_ecognition)
                self.composite_page.fields["ecognition"].edit.setText(detected_text)
                self.segment_page.fields["ecognition"].edit.setText(detected_text)
            finally:
                self._syncing_ecognition = False

    def _sync_ecognition_field(self, target_page: TaskPage, text: str) -> None:
        if self._syncing_ecognition:
            return
        target_edit = target_page.fields["ecognition"].edit
        if target_edit.text() == text:
            return
        self._syncing_ecognition = True
        try:
            target_edit.setText(text)
        finally:
            self._syncing_ecognition = False

    def _cache_root(self) -> Path:
        return self._custom_cache_dir or self.hardware.recommended_cache_dir

    def _refresh_cache_status(self) -> None:
        if not hasattr(self, "cache_label"):
            return
        root = self._cache_root()
        used = _format_bytes(cache_usage_bytes(root))
        location = str(root) if self._custom_cache_dir else f"{self.hardware.cache_drive_label}"
        self.cache_label.setText(f"缓存盘：{location}\n当前占用：{used}（失败保留断点，超 7 天自动回收）")

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
        self._staged_plan = None
        self._staged_backend = None
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
            tail = "配置完整，点击“下一步”扫描并确认清单。"
        else:
            tail = f"还需填写 {len(fields) - filled} 项必填内容。"
        self.summary.setText(f"已填写 {filled}/{len(fields)} 项\n识别模式：{mode_text}\n{tail}")

    def _selected_performance_mode(self) -> str:
        for key, radio in self.performance_modes.items():
            if radio.isChecked():
                return key
        return "auto"

    def _performance_mode_label(self) -> str:
        labels = {
            "low": "省资源模式（~45%）",
            "auto": "自动极速（动态 90% · 跨景尾段提前启动）",
            "high": "极限满载（~95% · 跨景尾段提前启动）",
        }
        return labels.get(self._selected_performance_mode(), "自动极速")

    def _refresh_performance_hint(self) -> None:
        if not hasattr(self, "worker_hint"):
            return
        hw = self.hardware
        if hasattr(self, "hardware_label"):
            self.hardware_label.setText(
                f"硬件：{hw.physical_cores} 物理核 / {hw.logical_cpus} 线程 · "
                f"可用 {hw.available_memory_gb:.1f}/{hw.total_memory_gb:.1f} GB"
            )
        budget = self._resource_budget()
        scheduler = ResourceScheduler(budget)
        eco = scheduler.ecognition_workers()
        eco_max = scheduler.ecognition_max_workers()
        post = scheduler.postprocess_workers()
        zonal_w = scheduler.zonal_workers(1024, 3072**2 * 16)
        mem_pct = int(budget.memory_fraction * 100)
        eco_text = f"{eco}→{eco_max}" if eco_max > eco else f"{eco}"
        self.worker_hint.setText(
            f"动态并发：分割 {eco_text} · 后处理 {post} · 赋值 {zonal_w}（目标上限 {mem_pct}%）"
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

    # ------------------------------------------------------------------
    # Step 1 -> Step 2: Scan, Pair, and Populate Confirmation Table
    # ------------------------------------------------------------------
    def _show_plan(self) -> None:
        self._go_to_confirm()

    def _go_to_confirm(self) -> None:
        try:
            self.hardware = detect_hardware()
            self._refresh_performance_hint()
            planner, runner, request, work_dir, output_directory = self._backend()
            execution_plan = planner(request)
        except Exception as error:
            QMessageBox.warning(self, "配置检查未通过", str(error))
            return

        self._staged_plan = execution_plan
        self._staged_backend = (planner, runner, request, work_dir, output_directory)
        self._last_output_directory = output_directory
        preview = list(execution_plan.metrics.get("items_preview") or ())
        self._staged_items = preview
        self._populate_confirm_page(execution_plan, request, output_directory, preview)
        self._set_wizard_step(1)
        self.status.setText(f"已扫描并配对 {len(preview)} 个待处理文件，请核对参数与勾选清单")
        self.header_status.setText("清单就绪")

    def _populate_confirm_page(
        self,
        execution_plan: ExecutionPlan,
        request,
        output_directory: Path,
        preview: list[dict],
    ) -> None:
        tool_names = {
            "composite": "00 分割并赋值（单景）",
            "composite-batch": "00 分割并赋值（批量）",
            "segment": "01 单独分割（单景）",
            "segment-batch": "01 单独分割（批量）",
            "zonal": "02 单独赋值（单文件）",
            "zonal-batch": "02 单独赋值（批量）",
        }
        mode_name = tool_names.get(execution_plan.tool, execution_plan.tool)
        eco_line = f" ｜ eCognition：{request.ecognition_bin}" if hasattr(request, "ecognition_bin") else ""
        opts = [
            f"断点续跑：{'开启' if self.resume.isChecked() else '关闭'}",
            f"完成后清理缓存：{'开启' if self.auto_cleanup.isChecked() else '关闭'}",
            f"缓存目录：{self._cache_root()}",
        ]
        self.confirm_params_label.setText(
            f"<b>任务类型：</b>{mode_name} ｜ <b>输出位置：</b>{output_directory}{eco_line}<br>"
            f"<b>性能调度：</b>{self._performance_mode_label()} ｜ {' ｜ '.join(opts)}"
        )

        warnings = list(execution_plan.metrics.get("warnings") or ())
        if warnings:
            self.confirm_warnings_label.setText("匹配提示：" + "；".join(warnings[:4]))
            self.confirm_warnings_label.show()
        else:
            self.confirm_warnings_label.hide()

        self.confirm_table.blockSignals(True)
        try:
            self.confirm_table.setRowCount(len(preview))
            for row, entry in enumerate(preview):
                check_item = QTableWidgetItem()
                check_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
                check_item.setCheckState(Qt.Checked)
                check_item.setTextAlignment(Qt.AlignCenter)
                self.confirm_table.setItem(row, 0, check_item)

                idx_item = QTableWidgetItem(str(row + 1))
                idx_item.setTextAlignment(Qt.AlignCenter)
                self.confirm_table.setItem(row, 1, idx_item)

                name_item = QTableWidgetItem(entry["relative_path"])
                name_item.setToolTip(entry["item_id"])
                self.confirm_table.setItem(row, 2, name_item)

                size_item = QTableWidgetItem(entry["size_summary"])
                size_item.setTextAlignment(Qt.AlignCenter)
                self.confirm_table.setItem(row, 3, size_item)

                aux_item = QTableWidgetItem(entry["auxiliary"])
                aux_item.setToolTip(entry["auxiliary"])
                self.confirm_table.setItem(row, 4, aux_item)

                match_item = QTableWidgetItem(entry["match_mode"])
                match_item.setTextAlignment(Qt.AlignCenter)
                self.confirm_table.setItem(row, 5, match_item)

                out_item = QTableWidgetItem(entry["output_name"])
                out_item.setToolTip(entry["output"])
                self.confirm_table.setItem(row, 6, out_item)
        finally:
            self.confirm_table.blockSignals(False)
        self._update_confirm_selection_count()

    def _on_confirm_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            self._update_confirm_selection_count()

    def _set_all_confirm_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        self.confirm_table.blockSignals(True)
        try:
            for row in range(self.confirm_table.rowCount()):
                cell = self.confirm_table.item(row, 0)
                if cell is not None:
                    cell.setCheckState(state)
        finally:
            self.confirm_table.blockSignals(False)
        self._update_confirm_selection_count()

    def _invert_confirm_checked(self) -> None:
        self.confirm_table.blockSignals(True)
        try:
            for row in range(self.confirm_table.rowCount()):
                cell = self.confirm_table.item(row, 0)
                if cell is not None:
                    cell.setCheckState(Qt.Unchecked if cell.checkState() == Qt.Checked else Qt.Checked)
        finally:
            self.confirm_table.blockSignals(False)
        self._update_confirm_selection_count()

    def _selected_preview_items(self) -> list[dict]:
        selected: list[dict] = []
        for row, entry in enumerate(self._staged_items):
            cell = self.confirm_table.item(row, 0)
            if cell is not None and cell.checkState() == Qt.Checked:
                selected.append(entry)
        return selected

    def _selected_inputs(self) -> tuple[Path, ...]:
        return tuple(Path(entry["item_id"]) for entry in self._selected_preview_items())

    def _update_confirm_selection_count(self) -> None:
        total = len(self._staged_items)
        selected = len(self._selected_preview_items())
        self.confirm_count_label.setText(f"共侦测到 {total} 个待处理文件，已勾选 {selected} 个")
        self.run_button.setText(f"开始执行（已选 {selected} 项）→")
        self.run_button.setEnabled(selected > 0 and not self.controller.running)

    # ------------------------------------------------------------------
    # Step 2 -> Step 3: Launch Selected Items & Live Per-Item Monitoring
    # ------------------------------------------------------------------
    def _build_monitor_rows(self, items: list[dict]) -> None:
        self._monitor_item_ids = [str(entry["item_id"]) for entry in items]
        self.tile_map.clear()
        self.monitor_table.setRowCount(len(items))
        self._monitor_bars = []
        self._monitor_bar_states = []
        for row, entry in enumerate(items):
            idx_item = QTableWidgetItem(str(row + 1))
            idx_item.setTextAlignment(Qt.AlignCenter)
            self.monitor_table.setItem(row, 0, idx_item)

            name_item = QTableWidgetItem(entry["relative_path"])
            name_item.setToolTip(entry["item_id"])
            self.monitor_table.setItem(row, 1, name_item)

            status_item = QTableWidgetItem("排队等待")
            status_item.setTextAlignment(Qt.AlignCenter)
            status_item.setForeground(_STATE_TEXT_COLORS["queued"])
            self.monitor_table.setItem(row, 2, status_item)

            bar = QProgressBar()
            bar.setObjectName("itemProgress")
            bar.setRange(0, 1000)
            bar.setValue(0)
            bar.setMinimumHeight(20)
            bar.setTextVisible(True)
            bar.setFormat("排队中")
            bar.setProperty("state", "queued")
            cell_wrap = QWidget()
            wrap_layout = QHBoxLayout(cell_wrap)
            wrap_layout.setContentsMargins(6, 4, 6, 4)
            wrap_layout.addWidget(bar)
            self.monitor_table.setCellWidget(row, 3, cell_wrap)
            self._monitor_bars.append(bar)
            self._monitor_bar_states.append("queued")

            detail_item = QTableWidgetItem("等待调度")
            self.monitor_table.setItem(row, 4, detail_item)

            time_item = QTableWidgetItem("--:--")
            time_item.setTextAlignment(Qt.AlignCenter)
            self.monitor_table.setItem(row, 5, time_item)

    def _apply_monitor_snapshot(self, monitor_snap: BatchMonitorSnapshot) -> None:
        self._apply_progress_snapshot(monitor_snap.overall)
        for row, item_snap in enumerate(monitor_snap.items):
            if row >= self.monitor_table.rowCount():
                break
            status_cell = self.monitor_table.item(row, 2)
            if status_cell is not None and status_cell.text() != item_snap.status_text:
                status_cell.setText(item_snap.status_text)
                status_cell.setForeground(_STATE_TEXT_COLORS.get(item_snap.state, _STATE_TEXT_COLORS["queued"]))

            bar = self._monitor_bars[row]
            if bar.value() != item_snap.permille:
                bar.setValue(item_snap.permille)
            if bar.format() != item_snap.bar_text:
                bar.setFormat(item_snap.bar_text)
            if self._monitor_bar_states[row] != item_snap.state:
                self._monitor_bar_states[row] = item_snap.state
                bar.setProperty("state", item_snap.state)
                style = bar.style()
                if style is not None:
                    style.unpolish(bar)
                    style.polish(bar)

            detail_cell = self.monitor_table.item(row, 4)
            if detail_cell is not None and detail_cell.text() != item_snap.detail:
                detail_cell.setText(item_snap.detail)

            time_cell = self.monitor_table.item(row, 5)
            if time_cell is not None and time_cell.text() != item_snap.elapsed_text:
                time_cell.setText(item_snap.elapsed_text)

    def _apply_progress_snapshot(self, snapshot: ProgressSnapshot) -> None:
        if self.progress_detail.text() != snapshot.headline:
            self.progress_detail.setText(snapshot.headline)
        if self.progress_time.text() != snapshot.time_text:
            self.progress_time.setText(snapshot.time_text)
        if self.header_status.text() != snapshot.pill_text:
            self.header_status.setText(snapshot.pill_text)
        if snapshot.indeterminate:
            if self.progress.maximum() != 0 or self.progress.minimum() != 0:
                self.progress.setRange(0, 0)
            if self.progress.format() != snapshot.bar_text:
                self.progress.setFormat(snapshot.bar_text)
        else:
            if self.progress.maximum() != 1000 or self.progress.minimum() != 0:
                self.progress.setRange(0, 1000)
            if self.progress.value() != snapshot.permille:
                self.progress.setValue(snapshot.permille)
            if self.progress.format() != snapshot.bar_text:
                self.progress.setFormat(snapshot.bar_text)
        if snapshot.pill_state != self._progress_state:
            self._progress_state = snapshot.pill_state
            for widget in (self.progress, self.header_status):
                widget.setProperty("state", snapshot.pill_state)
                style = widget.style()
                if style is not None:
                    style.unpolish(widget)
                    style.polish(widget)

    def _refresh_resource_meter(self) -> None:
        try:
            cpu_pct = psutil.cpu_percent(interval=None)
            ram_pct = psutil.virtual_memory().percent
            text = f"CPU {cpu_pct:.0f}% · RAM {ram_pct:.0f}%"
            if self.resource_meter.text() != text:
                self.resource_meter.setText(text)
        except Exception:
            pass

    def _on_progress_tick(self) -> None:
        if self.controller.running:
            self._refresh_resource_meter()
            if self._using_batch_monitor:
                self._apply_monitor_snapshot(self._batch_monitor.tick())
            else:
                self._apply_progress_snapshot(self._progress_tracker.tick())

    def _run(self) -> None:
        try:
            self.hardware = detect_hardware()
            self._refresh_performance_hint()
            if self._staged_backend is not None and self.wizard_stack.currentIndex() == 1:
                planner, runner, request, work_dir, output_directory = self._staged_backend
                selected_items = self._selected_preview_items()
                if not selected_items:
                    QMessageBox.warning(self, "未勾选文件", "请至少勾选 1 个待处理文件再启动执行。")
                    return
                if hasattr(request, "selected_inputs") and len(selected_items) < len(self._staged_items):
                    request = replace(request, selected_inputs=tuple(Path(e["item_id"]) for e in selected_items))
                execution_plan = planner(request)
                preview_items = list(execution_plan.metrics.get("items_preview") or selected_items)
            else:
                planner, runner, request, work_dir, output_directory = self._backend()
                execution_plan = planner(request)
                preview_items = list(execution_plan.metrics.get("items_preview") or ())
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
        self._build_monitor_rows(preview_items)
        self._using_batch_monitor = bool(preview_items)
        if self._using_batch_monitor:
            snap = self._batch_monitor.start(execution_plan.tool, preview_items)
            self._apply_monitor_snapshot(snap)
        else:
            total_items = int(execution_plan.metrics.get("items", 1) or 1)
            self._apply_progress_snapshot(self._progress_tracker.start(execution_plan.tool, total_items=total_items))
        self._set_wizard_step(2)
        self._refresh_resource_meter()
        self._progress_timer.start()
        self.status.setText("任务启动中…")

    def _cancel(self) -> None:
        self.controller.cancel()
        self.cancel_button.setEnabled(False)
        if self._using_batch_monitor:
            self._apply_monitor_snapshot(self._batch_monitor.mark_cancelling())
        else:
            self._apply_progress_snapshot(self._progress_tracker.mark_cancelling())
        self.status.setText("正在安全取消…")

    def _on_monitor_row_clicked(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._monitor_item_ids):
            self.tile_map.select_item(self._monitor_item_ids[row])

    def _on_events_batch(self, events: list) -> None:
        if not events:
            return
        lines: list[str] = []
        tile_map_dirty = False
        default_item_id = self._monitor_item_ids[0] if len(self._monitor_item_ids) == 1 else "__scene__"
        if self._using_batch_monitor:
            monitor_snap = None
            for event in events:
                kind = str(event.get("kind", ""))
                data = event.get("data") or {}
                item_id = str(data.get("item_id") or data.get("input") or default_item_id)
                if kind == "grid":
                    item_name = str(data.get("item_name") or Path(item_id).name)
                    self.tile_map.register_grid(item_id, item_name, data)
                    continue
                if kind == "tile":
                    if self.tile_map.update_tiles(item_id, data):
                        tile_map_dirty = True
                    continue
                if event.get("phase") == "batch" and "output" in data:
                    self.tile_map.mark_item_finished(item_id, str(data.get("item_status", "succeeded")) == "succeeded")
                monitor_snap = self._batch_monitor.update(event)
                lines.append(self._batch_monitor.format_log_line(event))
            if monitor_snap is not None:
                self._apply_monitor_snapshot(monitor_snap)
                if self.status.text() != monitor_snap.overall.headline:
                    self.status.setText(monitor_snap.overall.headline)
        else:
            snapshot = None
            for event in events:
                kind = str(event.get("kind", ""))
                data = event.get("data") or {}
                item_id = str(data.get("item_id") or default_item_id)
                if kind == "grid":
                    item_name = str(data.get("item_name") or Path(item_id).name)
                    self.tile_map.register_grid(item_id, item_name, data)
                    continue
                if kind == "tile":
                    if self.tile_map.update_tiles(item_id, data):
                        tile_map_dirty = True
                    continue
                snapshot = self._progress_tracker.update(event)
                lines.append(self._progress_tracker.format_log_line(event))
            if snapshot is not None:
                self._apply_progress_snapshot(snapshot)
                if self.status.text() != snapshot.headline:
                    self.status.setText(snapshot.headline)
        if tile_map_dirty:
            self.tile_map.flush_repaint()
        if lines:
            self.log.appendPlainText("\n".join(lines))

    def _on_event(self, event: dict) -> None:
        self._on_events_batch([event])

    def _on_result(self, result) -> None:
        self._progress_timer.stop()
        from ..models import TaskStatus

        succeeded = result.status is TaskStatus.SUCCEEDED
        for item_id in self._monitor_item_ids:
            self.tile_map.mark_item_finished(item_id, succeeded)
        if not self._monitor_item_ids:
            self.tile_map.mark_item_finished("__scene__", succeeded)
        if self._using_batch_monitor:
            monitor_snap = self._batch_monitor.finish(result.status)
            self._apply_monitor_snapshot(monitor_snap)
            overall = monitor_snap.overall
        else:
            overall = self._progress_tracker.finish(
                result.status,
                items_succeeded=getattr(result, "items_succeeded", None),
                items_total=getattr(result, "items_total", None),
            )
            self._apply_progress_snapshot(overall)
        self.status.setText(f"任务结束：{overall.headline}（{overall.time_text}）")
        self.log.appendPlainText(f"报告：{result.report_path}")
        if hasattr(result, "items_total"):
            self.log.appendPlainText(f"批处理：成功 {result.items_succeeded}/{result.items_total}")
        if result.warnings:
            self.log.appendPlainText("\n".join(result.warnings))
        self._refresh_cache_status()

    def _on_failure(self, detail: str) -> None:
        self._progress_timer.stop()
        if self._using_batch_monitor:
            from ..models import TaskStatus

            monitor_snap = self._batch_monitor.finish(TaskStatus.FAILED)
            self._apply_monitor_snapshot(monitor_snap)
        else:
            self._apply_progress_snapshot(self._progress_tracker.fail(detail))
        self.status.setText("任务线程异常")
        self.log.appendPlainText(detail)
        self._refresh_cache_status()

    def _set_running(self, running: bool) -> None:
        if not running:
            self._progress_timer.stop()
        self.mode.setEnabled(not running)
        self.pages.setEnabled(not running)
        self.resume.setEnabled(not running)
        self.auto_cleanup.setEnabled(not running)
        self.change_cache_button.setEnabled(not running)
        self.clear_cache_button.setEnabled(not running)
        self.plan_button.setEnabled(not running)
        self.next_to_confirm_button.setEnabled(not running)
        self.back_to_config_button.setEnabled(not running)
        self.back_from_monitor_button.setEnabled(not running)
        self.back_to_confirm_from_monitor.setEnabled(not running and self._staged_plan is not None)
        self.run_button.setEnabled(not running and (not self._staged_items or bool(self._selected_preview_items())))
        self.cancel_button.setEnabled(running)
        for radio in self.performance_modes.values():
            radio.setEnabled(not running)
        self._set_wizard_step(self.wizard_stack.currentIndex())
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
            "#wizardBar { background: white; border: 1px solid #dfe3e8; border-radius: 6px; }"
            "#tileMapCard { background: white; border: 1px solid #dfe3e8; border-radius: 6px; }"
            "#legendText { color: #475569; font-size: 11px; }"
            "#appTitle { font-size: 20px; font-weight: 700; color: #111827; }"
            "#pageTitle { font-size: 18px; font-weight: 700; color: #111827; }"
            "#sectionStrong { font-weight: 700; color: #111827; }"
            "#subtle { color: #667085; }"
            "#warningBanner { background: #fffbeb; color: #92400e; border: 1px solid #fde68a; "
            "border-radius: 4px; padding: 6px 10px; font-size: 12px; }"
            "#stepPill { background: #f1f5f9; color: #64748b; border: 1px solid #e2e8f0; "
            "border-radius: 15px; padding: 4px 14px; font-weight: 600; font-size: 12px; }"
            "#stepPill[stepState='active'] { background: #2563eb; color: white; border-color: #2563eb; }"
            "#stepPill[stepState='done'] { background: #dcfce7; color: #15803d; border-color: #bbf7d0; }"
            "#stepPill:disabled { color: #94a3b8; }"
            "#stepArrow { color: #94a3b8; font-size: 16px; font-weight: 700; }"
            "#statusPill { background: #eef2ff; color: #3949ab; padding: 5px 12px; "
            "border-radius: 4px; font-weight: 600; }"
            "#statusPill[state='running'] { background: #dbeafe; color: #1d4ed8; }"
            "#statusPill[state='succeeded'] { background: #dcfce7; color: #15803d; }"
            "#statusPill[state='cancelling'], #statusPill[state='cancelled'], #statusPill[state='partial'] "
            "{ background: #fef3c7; color: #b45309; }"
            "#statusPill[state='failed'] { background: #fee2e2; color: #b91c1c; }"
            "#resourcePill { background: #f1f5f9; color: #334155; border: 1px solid #e2e8f0; "
            "border-radius: 4px; padding: 2px 8px; font-size: 12px; font-weight: 600; }"
            "#progressDetail { color: #1f2937; font-weight: 600; font-size: 13px; }"
            "#progressTime { color: #64748b; font-size: 12px; }"
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
            "QPushButton:disabled { color: #94a3b8; background: #f8fafc; }"
            "#primaryButton { background: #2563eb; color: white; border-color: #2563eb; font-weight: 700; "
            "padding: 6px 18px; }"
            "#primaryButton:hover { background: #1d4ed8; }"
            "#primaryButton:disabled { background: #93a8d8; border-color: #93a8d8; color: white; }"
            "#dangerButton { background: #fff1f2; color: #be123c; border-color: #fecdd3; font-weight: 600; }"
            "#dangerButton:hover { background: #ffe4e6; }"
            "#dangerButton:disabled { background: #f8fafc; color: #94a3b8; border-color: #e2e8f0; }"
            "QTableWidget { background: white; alternate-background-color: #f8fafc; "
            "border: 1px solid #dfe3e8; border-radius: 6px; gridline-color: #eef2f6; }"
            "QHeaderView::section { background: #f8fafc; color: #334155; font-weight: 700; "
            "padding: 6px 8px; border: 0; border-bottom: 1px solid #dfe3e8; }"
            "QProgressBar { border: 1px solid #cbd5e1; border-radius: 5px; text-align: center; "
            "background: #f1f5f9; color: #0f172a; font-weight: 600; font-size: 12px; }"
            "QProgressBar::chunk { background: #60a5fa; border-radius: 4px; }"
            "QProgressBar[state='queued']::chunk { background: #cbd5e1; }"
            "QProgressBar[state='succeeded']::chunk { background: #86efac; }"
            "QProgressBar[state='cancelling']::chunk, QProgressBar[state='cancelled']::chunk, "
            "QProgressBar[state='partial']::chunk { background: #fcd34d; }"
            "QProgressBar[state='failed']::chunk { background: #fca5a5; }"
            "QProgressBar#itemProgress { font-size: 11px; border-radius: 4px; }"
            "QSplitter::handle { background: #e5e7eb; height: 4px; }"
            "QSplitter::handle:hover { background: #93c5fd; }"
        )
