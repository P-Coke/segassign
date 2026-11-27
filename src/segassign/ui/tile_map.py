"""Ultra-lightweight QPainter GIS tile-footprint viewport for real-time spatial progress visualization."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolTip, QVBoxLayout, QWidget


@dataclass(slots=True)
class TileCell:
    tile_id: str
    col_off: float
    row_off: float
    width: float
    height: float
    state: str = "pending"  # "pending" | "running" | "done" | "skipped" | "failed"


@dataclass(slots=True)
class SceneTileMap:
    item_id: str
    name: str
    width: float
    height: float
    crs: str
    bounds: tuple[float, float, float, float] | None
    cells: list[TileCell]
    by_id: dict[str, TileCell]


_STATE_COLORS: dict[str, tuple[QColor, QColor]] = {
    # (fill, border) — refined cartographic palette matching SegAssign's light slate theme.
    "pending": (QColor("#fee2e2"), QColor("#ef4444")),  # 未处理：红
    "running": (QColor("#bfdbfe"), QColor("#2563eb")),  # 处理中：蓝
    "done": (QColor("#bbf7d0"), QColor("#16a34a")),  # 已完成：绿
    "skipped": (QColor("#f1f5f9"), QColor("#cbd5e1")),  # 空区跳过：浅灰
    "failed": (QColor("#fecaca"), QColor("#b91c1c")),  # 失败：深红
}

_STATE_CN = {
    "pending": "未处理",
    "running": "处理中",
    "done": "已完成",
    "skipped": "空区跳过",
    "failed": "失败",
}


class _TileCanvas(QWidget):
    """Pure QPainter map canvas; renders hundreds of vector tile footprints in <0.3 ms."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(260, 220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self._scene: SceneTileMap | None = None
        self._transform_cache: tuple[float, float, float] | None = None  # (scale, origin_x, origin_y)

    def set_scene(self, scene: SceneTileMap | None) -> None:
        self._scene = scene
        self.update()

    def _compute_transform(self, scene: SceneTileMap) -> tuple[float, float, float, QRectF]:
        pad_l, pad_t, pad_r, pad_b = 18.0, 22.0, 18.0, 22.0
        avail_w = max(20.0, self.width() - pad_l - pad_r)
        avail_h = max(20.0, self.height() - pad_t - pad_b)
        scale = min(avail_w / max(1.0, scene.width), avail_h / max(1.0, scene.height))
        draw_w = scene.width * scale
        draw_h = scene.height * scale
        origin_x = pad_l + (avail_w - draw_w) * 0.5
        origin_y = pad_t + (avail_h - draw_h) * 0.5
        return scale, origin_x, origin_y, QRectF(origin_x, origin_y, draw_w, draw_h)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)

        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.fillRect(rect, QColor("#f8fafc"))

        # Subtle cartographic graticule background
        grid_pen = QPen(QColor("#e2e8f0"), 1, Qt.DotLine)
        painter.setPen(grid_pen)
        step = 48
        for x in range(rect.left() + step, rect.right(), step):
            painter.drawLine(x, rect.top(), x, rect.bottom())
        for y in range(rect.top() + step, rect.bottom(), step):
            painter.drawLine(rect.left(), y, rect.right(), y)

        # Outer map frame
        painter.setPen(QPen(QColor("#cbd5e1"), 1))
        painter.drawRect(rect)

        scene = self._scene
        if scene is None or not scene.cells:
            self._transform_cache = None
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(rect, Qt.AlignCenter, "等待侦测影像范围与切片边界…")
            painter.end()
            return

        scale, ox, oy, extent_rect = self._compute_transform(scene)
        self._transform_cache = (scale, ox, oy)

        # Draw scene footprint backdrop
        painter.fillRect(extent_rect, QColor("#ffffff"))

        # Draw tile vector boundaries grouped cleanly
        inset = 0.6 if len(scene.cells) <= 400 else 0.25
        border_width = 1.0 if len(scene.cells) <= 400 else 0.75
        for cell in scene.cells:
            fill_color, border_color = _STATE_COLORS.get(cell.state, _STATE_COLORS["pending"])
            rx = ox + cell.col_off * scale
            ry = oy + cell.row_off * scale
            rw = max(1.5, cell.width * scale)
            rh = max(1.5, cell.height * scale)
            tile_rect = QRectF(rx + inset, ry + inset, max(1.0, rw - inset * 2), max(1.0, rh - inset * 2))
            painter.fillRect(tile_rect, fill_color)
            pen_style = Qt.DashLine if cell.state == "skipped" else Qt.SolidLine
            painter.setPen(QPen(border_color, border_width, pen_style))
            painter.drawRect(tile_rect)

        # Crisp outer extent frame
        painter.setPen(QPen(QColor("#475569"), 1.2))
        painter.drawRect(extent_rect)

        # Cartographic micro-overlays: North arrow (top-right) & CRS/bounds ticks
        font = QFont("Microsoft YaHei UI", 8)
        painter.setFont(font)
        painter.setPen(QColor("#64748b"))

        # Top-left & bottom-right coordinate hints
        if scene.bounds is not None:
            minx, miny, maxx, maxy = scene.bounds
            tl = f"{minx:.2f}, {maxy:.2f}" if abs(minx) < 1000 else f"{minx:.0f}, {maxy:.0f}"
            br = f"{maxx:.2f}, {miny:.2f}" if abs(maxx) < 1000 else f"{maxx:.0f}, {miny:.0f}"
        else:
            tl = "0, 0"
            br = f"{int(scene.width)} × {int(scene.height)} px"

        painter.drawText(QRectF(rect.left() + 8, rect.top() + 3, rect.width() - 46, 16), Qt.AlignLeft, tl)
        painter.drawText(QRectF(rect.left() + 8, rect.bottom() - 18, rect.width() - 16, 16), Qt.AlignRight, br)
        if scene.crs:
            painter.drawText(
                QRectF(rect.left() + 8, rect.bottom() - 18, rect.width() * 0.55, 16),
                Qt.AlignLeft,
                scene.crs[:24],
            )

        # Minimal North indicator in top-right corner
        nx = rect.right() - 16
        ny = rect.top() + 18
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(QColor("#334155"), 1.2))
        painter.drawLine(QPointF(nx, ny + 6), QPointF(nx, ny - 6))
        painter.drawLine(QPointF(nx, ny - 6), QPointF(nx - 3.5, ny - 1.5))
        painter.drawLine(QPointF(nx, ny - 6), QPointF(nx + 3.5, ny - 1.5))
        painter.drawText(QRectF(nx - 8, rect.top() + 1, 16, 12), Qt.AlignCenter, "N")
        painter.end()

    def mouseMoveEvent(self, event) -> None:
        scene = self._scene
        if scene is None or self._transform_cache is None:
            return
        scale, ox, oy = self._transform_cache
        pos = event.position()
        px = (pos.x() - ox) / scale
        py = (pos.y() - oy) / scale
        if 0.0 <= px <= scene.width and 0.0 <= py <= scene.height:
            for cell in scene.cells:
                if cell.col_off <= px <= cell.col_off + cell.width and cell.row_off <= py <= cell.row_off + cell.height:
                    state_cn = _STATE_CN.get(cell.state, cell.state)
                    tip = (
                        f"切片：{cell.tile_id}\n"
                        f"状态：{state_cn}\n"
                        f"范围：{int(cell.col_off)},{int(cell.row_off)} ({int(cell.width)}×{int(cell.height)} px)"
                    )
                    QToolTip.showText(event.globalPosition().toPoint(), tip, self)
                    return
        QToolTip.hideText()


class TileMapPanel(QFrame):
    """Self-contained GIS spatial tile-preview card with scene header, canvas, and live legend."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("tileMapCard")
        self.setMinimumWidth(300)
        self._scenes: dict[str, SceneTileMap] = {}
        self._active_item_id: str | None = None
        self._pinned_item_id: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        self.title_label = QLabel("空间切片实时预览")
        self.title_label.setObjectName("sectionStrong")
        self.scene_label = QLabel("待运行")
        self.scene_label.setObjectName("subtle")
        self.scene_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        head.addWidget(self.title_label)
        head.addWidget(self.scene_label, 1)
        layout.addLayout(head)

        self.canvas = _TileCanvas(self)
        layout.addWidget(self.canvas, 1)

        legend = QHBoxLayout()
        legend.setContentsMargins(0, 0, 0, 0)
        legend.setSpacing(10)
        self._legend_labels: dict[str, QLabel] = {}
        for key, dot_color, text in (
            ("pending", "#ef4444", "未处理"),
            ("running", "#2563eb", "处理中"),
            ("done", "#16a34a", "已完成"),
            ("skipped", "#94a3b8", "空区"),
        ):
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {dot_color}; font-size: 12px;")
            lbl = QLabel(f"{text} 0")
            lbl.setObjectName("legendText")
            pair = QHBoxLayout()
            pair.setContentsMargins(0, 0, 0, 0)
            pair.setSpacing(3)
            pair.addWidget(dot)
            pair.addWidget(lbl)
            legend.addLayout(pair)
            self._legend_labels[key] = lbl
        legend.addStretch(1)
        layout.addLayout(legend)

    def clear(self) -> None:
        self._scenes.clear()
        self._active_item_id = None
        self._pinned_item_id = None
        self.canvas.set_scene(None)
        self.scene_label.setText("等待扫描切片范围…")
        self._update_legend(None)

    def select_item(self, item_id: str | None) -> None:
        """Let the user click a row in the monitor table to inspect that scene's tile map."""
        key = item_id.casefold() if item_id else None
        self._pinned_item_id = key
        target = self._scenes.get(key) if key else None
        if target is not None:
            self._active_item_id = key
            self._refresh_display(target)
        elif key is not None:
            self.canvas.set_scene(None)
            self.scene_label.setText("该项尚未生成切片网格")
            self._update_legend(None)

    def register_grid(self, item_id: str, name: str, data: dict) -> None:
        raw_tiles = data.get("tiles") or ()
        width = float(data.get("width") or 1.0)
        height = float(data.get("height") or 1.0)
        crs = str(data.get("crs") or "")
        raw_bounds = data.get("bounds")
        bounds = tuple(float(v) for v in raw_bounds) if raw_bounds and len(raw_bounds) == 4 else None
        cells: list[TileCell] = []
        by_id: dict[str, TileCell] = {}
        for entry in raw_tiles:
            if len(entry) >= 6:
                tid, cx, cy, cw, ch, state = entry[:6]
            else:
                tid, cx, cy, cw, ch = entry[:5]
                state = "pending"
            cell = TileCell(str(tid), float(cx), float(cy), float(cw), float(ch), str(state))
            cells.append(cell)
            by_id[cell.tile_id] = cell
        key = item_id.casefold()
        scene = SceneTileMap(item_id, name, width, height, crs, bounds, cells, by_id)
        self._scenes[key] = scene
        if self._pinned_item_id is None or self._pinned_item_id == key:
            self._active_item_id = key
            self._refresh_display(scene)

    def update_tiles(self, item_id: str, data: dict) -> bool:
        """Apply single or batch tile state updates; returns True if active display changed."""
        key = item_id.casefold()
        scene = self._scenes.get(key)
        if scene is None:
            return False
        state = str(data.get("state") or "")
        changed = False
        tile_id = data.get("tile_id")
        tile_ids = data.get("tile_ids")
        ids = [str(tile_id)] if tile_id is not None else [str(t) for t in (tile_ids or ())]
        for tid in ids:
            cell = scene.by_id.get(tid)
            if cell is not None and state and cell.state != state:
                # Never downgrade a completed tile back to running/pending.
                if cell.state == "done" and state in ("pending", "running"):
                    continue
                cell.state = state
                changed = True
        if changed and self._pinned_item_id is None:
            self._active_item_id = key
        return changed and self._active_item_id == key

    def mark_item_finished(self, item_id: str, succeeded: bool) -> None:
        key = item_id.casefold()
        scene = self._scenes.get(key)
        if scene is None:
            return
        if succeeded:
            for cell in scene.cells:
                if cell.state in ("pending", "running"):
                    cell.state = "done"
        else:
            for cell in scene.cells:
                if cell.state == "running":
                    cell.state = "failed"
        if self._active_item_id == key:
            self._refresh_display(scene)

    def flush_repaint(self) -> None:
        """Coalesced single repaint after a batch of tile events."""
        if self._active_item_id is None:
            return
        scene = self._scenes.get(self._active_item_id)
        if scene is not None:
            self._refresh_display(scene)

    def _refresh_display(self, scene: SceneTileMap) -> None:
        self.canvas.set_scene(scene)
        total = len(scene.cells)
        short = scene.name if len(scene.name) <= 24 else f"{scene.name[:21]}…"
        self.scene_label.setText(f"{short} · {total} 块")
        self._update_legend(scene)

    def _update_legend(self, scene: SceneTileMap | None) -> None:
        counts = {"pending": 0, "running": 0, "done": 0, "skipped": 0}
        if scene is not None:
            for cell in scene.cells:
                if cell.state in counts:
                    counts[cell.state] += 1
                elif cell.state == "failed":
                    counts["pending"] += 1
        names = {"pending": "未处理", "running": "处理中", "done": "已完成", "skipped": "空区"}
        for key, label in self._legend_labels.items():
            text = f"{names[key]} {counts[key]}"
            if label.text() != text:
                label.setText(text)
