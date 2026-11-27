"""Hierarchical, strictly-monotonic progress and ETA tracking for the SegAssign UI."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..models import TaskStatus

_PHASE_LABELS: dict[str, str] = {
    "scan": "预扫描有效区",
    "prepare": "准备瓦片",
    "ecognition": "eCognition 分割",
    "postprocess": "瓦片后处理",
    "mosaic": "矢量拼接",
    "index": "构建空间索引",
    "count": "分类像元统计",
    "write": "写入统计属性",
    "publish": "发布矢量成果",
    "waiting": "等待引擎就绪",
    "segment": "影像分割",
    "segment-batch": "批量分割",
    "zonal": "分类赋值",
    "zonal-batch": "批量赋值",
    "composite": "分割并赋值",
    "composite-batch": "批量分割并赋值",
    "composite-segment": "步骤 1/2：影像分割",
    "composite-zonal": "步骤 2/2：分类赋值",
    "batch": "批处理进度",
}

_STATUS_LABELS: dict[TaskStatus, tuple[str, str]] = {
    TaskStatus.SUCCEEDED: ("已完成", "succeeded"),
    TaskStatus.PARTIAL: ("部分完成", "partial"),
    TaskStatus.CANCELLED: ("已取消", "cancelled"),
    TaskStatus.FAILED: ("执行失败", "failed"),
}


def format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _short_name(path_str: str, max_len: int = 26) -> str:
    name = Path(path_str).name if path_str else ""
    if len(name) <= max_len:
        return name
    stem = Path(name).stem
    suffix = Path(name).suffix
    keep = max(6, max_len - len(suffix) - 1)
    return f"{stem[:keep]}…{suffix}"


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass(frozen=True)
class ProgressSnapshot:
    percent: float
    indeterminate: bool
    headline: str
    bar_text: str
    pill_text: str
    pill_state: str
    elapsed_seconds: float
    eta_seconds: float | None
    time_text: str

    @property
    def permille(self) -> int:
        return max(0, min(1000, int(round(self.percent * 10.0))))


class ProgressTracker:
    """Converts backend TaskEvent streams into smooth, monotonic progress and ETA snapshots."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._reset_state()

    def _reset_state(self) -> None:
        self._running = False
        self._cancelling = False
        self._indeterminate = False
        self._started_at: float | None = None
        self._finished_elapsed: float | None = None
        self._percent = 0.0
        self._root_tool = ""
        self._is_batch = False
        self._batch_total = 1
        self._batch_completed = 0
        self._batch_active_index = 1
        self._current_item_name = ""
        self._composite_stage = "segment"
        self._stage_label = "就绪"
        self._stage_detail = ""
        self._pill_state = "idle"
        self._final_status: TaskStatus | None = None
        self._final_summary = ""
        self._seg = self._empty_seg()
        self._zon = self._empty_zon()
        self._eta_anchor_time: float | None = None
        self._eta_anchor_percent = 0.0
        self._smoothed_rate: float | None = None
        self._last_eta_seconds: float | None = None
        self._last_eta_at: float | None = None

    @staticmethod
    def _empty_seg() -> dict[str, float]:
        return {
            "scan": 0.0,
            "prepare": 0.0,
            "ecognition": 0.0,
            "postprocess": 0.0,
            "mosaic": 0.0,
            "publish": 0.0,
        }

    @staticmethod
    def _empty_zon() -> dict[str, float]:
        return {
            "scan": 0.0,
            "index": 0.0,
            "count": 0.0,
            "write": 0.0,
            "publish": 0.0,
        }

    def idle(self) -> ProgressSnapshot:
        self._reset_state()
        return ProgressSnapshot(
            percent=0.0,
            indeterminate=False,
            headline="就绪 — 配置路径后点击执行",
            bar_text="0% · 就绪",
            pill_text="就绪",
            pill_state="idle",
            elapsed_seconds=0.0,
            eta_seconds=None,
            time_text="",
        )

    def start(self, tool: str = "", total_items: int = 1) -> ProgressSnapshot:
        self._reset_state()
        now = self._clock()
        self._running = True
        self._indeterminate = True
        self._started_at = now
        self._eta_anchor_time = now
        self._eta_anchor_percent = 0.0
        self._root_tool = tool
        self._is_batch = tool.endswith("-batch") or total_items > 1
        self._batch_total = max(1, int(total_items))
        self._batch_completed = 0
        self._batch_active_index = 1
        self._stage_label = "正在初始化任务"
        self._pill_state = "running"
        return self.snapshot()

    def mark_cancelling(self) -> ProgressSnapshot:
        if self._running:
            self._cancelling = True
            self._pill_state = "cancelling"
            self._stage_label = "正在安全取消"
            self._stage_detail = "等待当前步骤安全退出并保存断点"
        return self.snapshot()

    def update(self, event: dict) -> ProgressSnapshot:
        now = self._clock()
        if self._started_at is None:
            self._started_at = now
            self._eta_anchor_time = now
            self._running = True
            self._pill_state = "running"

        kind = str(event.get("kind", ""))
        phase = str(event.get("phase", ""))
        message = str(event.get("message", ""))
        data = event.get("data") or {}

        if kind == "started":
            self._handle_started(phase, data)
        elif phase == "batch" and kind == "progress":
            self._handle_batch(data)
        elif kind in ("stage", "progress"):
            self._handle_stage_or_progress(phase, message, data)
        elif kind == "waiting":
            self._stage_label = _PHASE_LABELS["waiting"]
            self._stage_detail = "检测到其他 eCognition 进程，排队等待中…"
        elif kind == "completed" and phase in ("segment", "zonal"):
            if phase == "segment":
                self._seg = {key: 1.0 for key in self._seg}
            elif phase == "zonal":
                self._zon = {key: 1.0 for key in self._zon}

        new_percent = self._compute_percent()
        if new_percent > 0.0:
            self._indeterminate = False
        if new_percent > self._percent:
            prev = self._percent
            self._percent = new_percent
            self._update_eta_rate(now, prev, new_percent)

        return self.snapshot()

    def tick(self) -> ProgressSnapshot:
        """Advance elapsed clock smoothly between backend events."""
        return self.snapshot()

    def finish(
        self,
        status: TaskStatus,
        *,
        items_succeeded: int | None = None,
        items_total: int | None = None,
    ) -> ProgressSnapshot:
        now = self._clock()
        self._finished_elapsed = (now - self._started_at) if self._started_at is not None else 0.0
        self._running = False
        self._cancelling = False
        self._indeterminate = False
        self._final_status = status
        label, pill_state = _STATUS_LABELS.get(status, (status.value, "idle"))
        self._pill_state = pill_state
        if status is TaskStatus.SUCCEEDED:
            self._percent = 100.0
        if items_total is not None and items_succeeded is not None and items_total > 1:
            self._final_summary = f"{label} · 成功 {items_succeeded}/{items_total} 项"
        else:
            self._final_summary = label
        self._stage_label = label
        self._stage_detail = ""
        self._last_eta_seconds = None
        return self.snapshot()

    def fail(self, detail: str = "") -> ProgressSnapshot:
        snapshot = self.finish(TaskStatus.FAILED)
        if detail:
            first_line = detail.splitlines()[0][:64]
            self._final_summary = f"执行失败 · {first_line}"
            return self.snapshot()
        return snapshot

    def format_log_line(self, event: dict) -> str:
        phase = str(event.get("phase", ""))
        message = str(event.get("message", ""))
        data = event.get("data") or {}
        phase_cn = _PHASE_LABELS.get(phase, phase or "任务")
        stamp = datetime.now().strftime("%H:%M:%S")
        localized = self._localize_message(phase, message, data)
        return f"[{stamp}] [{phase_cn}] {localized}"

    def _localize_message(self, phase: str, message: str, data: dict) -> str:
        completed = data.get("completed")
        total = data.get("total")
        if phase == "prepare" and completed is not None and total:
            return f"已准备 {completed}/{total} 块瓦片"
        if phase == "ecognition" and completed is not None and total:
            return f"已完成 eCognition 分割 {completed}/{total} 块瓦片"
        if phase == "postprocess" and completed is not None and total:
            return f"已完成瓦片后处理 {completed}/{total} 块"
        if phase == "count" and completed is not None and total:
            return f"已完成 {completed}/{total} 个栅格统计窗口"
        if message == "Segmentation started":
            return "开始影像分割"
        if message == "Segmentation completed":
            return "影像分割完成"
        if message == "Zonal assignment started":
            return "开始分类赋值"
        if message == "Zonal assignment completed":
            return "分类赋值完成"
        if message == "Composite run started":
            return "开始分割并赋值"
        if message.startswith("Composite run "):
            return f"分割并赋值结束：{message.removeprefix('Composite run ')}"
        if message == "Waiting for the global eCognition execution lease":
            return "正在等待全局 eCognition 执行租约释放"
        return message

    def _handle_started(self, phase: str, data: dict) -> None:
        plan_metrics = data.get("plan") if isinstance(data.get("plan"), dict) else {}
        if phase in ("segment-batch", "zonal-batch", "composite-batch"):
            self._root_tool = phase
            self._is_batch = True
            items = int(plan_metrics.get("items", self._batch_total) or 1)
            self._batch_total = max(1, items)
            self._stage_label = _PHASE_LABELS[phase]
            self._stage_detail = f"共 {self._batch_total} 项" if self._batch_total > 1 else ""
        elif phase == "composite":
            if not self._is_batch:
                self._root_tool = "composite"
            self._composite_stage = "segment"
            self._seg = self._empty_seg()
            self._zon = self._empty_zon()
            self._stage_label = _PHASE_LABELS["composite-segment"]
            self._stage_detail = ""
        elif phase == "segment":
            if not self._is_batch and self._root_tool != "composite":
                self._root_tool = "segment"
            if self._root_tool in ("composite", "composite-batch"):
                self._composite_stage = "segment"
            self._seg = self._empty_seg()
            self._stage_label = _PHASE_LABELS["segment"]
            tiles = plan_metrics.get("tiles_total")
            self._stage_detail = f"共 {tiles} 块瓦片" if tiles else ""
        elif phase == "zonal":
            if not self._is_batch and self._root_tool != "composite":
                self._root_tool = "zonal"
            if self._root_tool in ("composite", "composite-batch"):
                self._composite_stage = "zonal"
                self._seg = {key: 1.0 for key in self._seg}
            self._zon = self._empty_zon()
            self._stage_label = _PHASE_LABELS["zonal"]
            self._stage_detail = ""

    def _handle_batch(self, data: dict) -> None:
        self._is_batch = True
        total = max(1, int(data.get("total", self._batch_total) or 1))
        completed = max(0, int(data.get("completed", self._batch_completed) or 0))
        self._batch_total = total
        self._batch_completed = max(self._batch_completed, min(total, completed))
        if "input" in data:
            self._current_item_name = _short_name(str(data["input"]))
            self._batch_active_index = min(total, self._batch_completed + 1)
            # Reset sub-stage trackers for the newly started item; monotonic clamp preserves smoothness.
            self._seg = self._empty_seg()
            self._zon = self._empty_zon()
            self._composite_stage = "segment"
            self._stage_label = "准备处理"
            self._stage_detail = self._current_item_name
        elif "output" in data:
            done_name = _short_name(str(data["output"]))
            self._stage_label = "完成单项"
            self._stage_detail = done_name
            if self._batch_completed >= self._batch_active_index:
                self._seg = self._empty_seg()
                self._zon = self._empty_zon()

    def _handle_stage_or_progress(self, phase: str, message: str, data: dict) -> None:
        completed = data.get("completed")
        total = data.get("total")
        ratio = _clamp01(float(completed) / float(total)) if (completed is not None and total) else None

        if phase == "composite-segment":
            self._composite_stage = "segment"
            self._stage_label = _PHASE_LABELS["composite-segment"]
            self._stage_detail = ""
            return
        if phase == "composite-zonal":
            self._composite_stage = "zonal"
            self._seg = {key: 1.0 for key in self._seg}
            self._stage_label = _PHASE_LABELS["composite-zonal"]
            self._stage_detail = ""
            return

        if phase == "scan":
            if self._is_zonal_active():
                self._zon["scan"] = max(self._zon["scan"], ratio if ratio is not None else 0.5)
            else:
                self._seg["scan"] = max(self._seg["scan"], ratio if ratio is not None else 0.5)
            self._stage_label = _PHASE_LABELS["scan"]
            self._stage_detail = f"共 {total} 块瓦片" if total else ""
        elif phase == "prepare":
            self._seg["scan"] = 1.0
            if ratio is not None:
                self._seg["prepare"] = max(self._seg["prepare"], ratio)
            # Keep ecognition as headline once segmentation is actively running alongside prepare.
            if self._seg["ecognition"] <= 0.0:
                self._stage_label = _PHASE_LABELS["prepare"]
                self._stage_detail = f"{completed}/{total} 瓦片" if (completed is not None and total) else ""
        elif phase == "ecognition":
            self._seg["scan"] = 1.0
            if ratio is not None:
                self._seg["ecognition"] = max(self._seg["ecognition"], ratio)
            self._stage_label = _PHASE_LABELS["ecognition"]
            self._stage_detail = f"{completed}/{total} 瓦片" if (completed is not None and total) else ""
        elif phase == "postprocess":
            self._seg["scan"] = 1.0
            self._seg["prepare"] = 1.0
            self._seg["ecognition"] = 1.0
            if ratio is not None:
                self._seg["postprocess"] = max(self._seg["postprocess"], ratio)
            self._stage_label = _PHASE_LABELS["postprocess"]
            self._stage_detail = f"{completed}/{total} 瓦片" if (completed is not None and total) else ""
        elif phase == "mosaic":
            self._seg["scan"] = 1.0
            self._seg["prepare"] = 1.0
            self._seg["ecognition"] = 1.0
            self._seg["postprocess"] = 1.0
            self._seg["mosaic"] = max(self._seg["mosaic"], 0.5)
            self._stage_label = _PHASE_LABELS["mosaic"]
            self._stage_detail = f"{total} 块瓦片" if total else ""
        elif phase == "index":
            self._zon["scan"] = 1.0
            self._zon["index"] = max(self._zon["index"], 0.5)
            self._stage_label = _PHASE_LABELS["index"]
            self._stage_detail = f"{total} 个面要素" if total else ""
        elif phase == "count":
            self._zon["scan"] = 1.0
            self._zon["index"] = 1.0
            if ratio is not None:
                self._zon["count"] = max(self._zon["count"], ratio)
            self._stage_label = _PHASE_LABELS["count"]
            self._stage_detail = f"{completed}/{total} 窗口" if (completed is not None and total) else ""
        elif phase == "write":
            self._zon["scan"] = 1.0
            self._zon["index"] = 1.0
            self._zon["count"] = 1.0
            self._zon["write"] = max(self._zon["write"], 0.5)
            self._stage_label = _PHASE_LABELS["write"]
            self._stage_detail = ""
        elif phase == "publish":
            if self._is_zonal_active():
                self._zon["scan"] = 1.0
                self._zon["index"] = 1.0
                self._zon["count"] = 1.0
                self._zon["write"] = 1.0
                self._zon["publish"] = max(self._zon["publish"], 0.5)
            else:
                self._seg["scan"] = 1.0
                self._seg["prepare"] = 1.0
                self._seg["ecognition"] = 1.0
                self._seg["postprocess"] = 1.0
                self._seg["mosaic"] = 1.0
                self._seg["publish"] = max(self._seg["publish"], 0.5)
            self._stage_label = _PHASE_LABELS["publish"]
            self._stage_detail = ""
        else:
            self._stage_label = _PHASE_LABELS.get(phase, phase or "处理中")
            self._stage_detail = message[:36] if message else ""

    def _is_zonal_active(self) -> bool:
        if self._root_tool in ("zonal", "zonal-batch"):
            return True
        return self._root_tool in ("composite", "composite-batch") and self._composite_stage == "zonal"

    def _compute_percent(self) -> float:
        seg_frac = (
            0.02 * self._seg["scan"]
            + 0.10 * self._seg["prepare"]
            + 0.70 * self._seg["ecognition"]
            + 0.10 * self._seg["postprocess"]
            + 0.05 * self._seg["mosaic"]
            + 0.02 * self._seg["publish"]
        )
        zon_frac = (
            0.04 * self._zon["scan"]
            + 0.10 * self._zon["index"]
            + 0.72 * self._zon["count"]
            + 0.11 * self._zon["write"]
            + 0.02 * self._zon["publish"]
        )
        if self._root_tool in ("composite", "composite-batch"):
            item_frac = (0.75 + 0.24 * zon_frac) if self._composite_stage == "zonal" else (0.75 * seg_frac)
        elif self._root_tool in ("zonal", "zonal-batch"):
            item_frac = zon_frac
        else:
            item_frac = seg_frac

        if self._is_batch:
            raw = 100.0 * min(float(self._batch_total), self._batch_completed + item_frac) / float(self._batch_total)
        else:
            raw = 100.0 * item_frac
        return max(self._percent, min(99.5, raw))

    def _update_eta_rate(self, now: float, prev_percent: float, new_percent: float) -> None:
        if self._started_at is None or self._eta_anchor_time is None:
            return
        # If resumed work causes an instant jump near start, re-anchor ETA to remaining work.
        if prev_percent == 0.0 and (now - self._started_at) <= 1.5 and new_percent >= 15.0:
            self._eta_anchor_time = now
            self._eta_anchor_percent = new_percent
            self._smoothed_rate = None
            self._last_eta_seconds = None
            self._last_eta_at = None
            return

        dt = now - self._eta_anchor_time
        earned = new_percent - self._eta_anchor_percent
        if dt >= 2.0 and earned >= 1.5 and new_percent < 99.5:
            observed_rate = earned / dt
            if self._smoothed_rate is None:
                self._smoothed_rate = observed_rate
            else:
                self._smoothed_rate = 0.75 * self._smoothed_rate + 0.25 * observed_rate
            if self._smoothed_rate > 1e-6:
                self._last_eta_seconds = max(1.0, (100.0 - new_percent) / self._smoothed_rate)
                self._last_eta_at = now

    def _current_eta(self, now: float) -> float | None:
        if not self._running or self._last_eta_seconds is None or self._last_eta_at is None:
            return None
        elapsed_since = max(0.0, now - self._last_eta_at)
        return max(1.0, self._last_eta_seconds - elapsed_since)

    def snapshot(self) -> ProgressSnapshot:
        now = self._clock()
        if self._finished_elapsed is not None:
            elapsed = self._finished_elapsed
        elif self._started_at is not None:
            elapsed = max(0.0, now - self._started_at)
        else:
            elapsed = 0.0

        if not self._running and self._final_status is not None:
            pct_int = int(round(self._percent))
            label = self._stage_label
            return ProgressSnapshot(
                percent=self._percent,
                indeterminate=False,
                headline=self._final_summary or label,
                bar_text=f"{pct_int}% · {label}",
                pill_text=label,
                pill_state=self._pill_state,
                elapsed_seconds=elapsed,
                eta_seconds=None,
                time_text=f"总耗时 {format_duration(elapsed)}",
            )

        if not self._running:
            return ProgressSnapshot(
                percent=0.0,
                indeterminate=False,
                headline="就绪 — 配置路径后点击执行",
                bar_text="0% · 就绪",
                pill_text="就绪",
                pill_state="idle",
                elapsed_seconds=0.0,
                eta_seconds=None,
                time_text="",
            )

        eta = self._current_eta(now)
        parts: list[str] = []
        if self._is_batch and self._batch_total > 1:
            idx = min(self._batch_total, max(1, self._batch_active_index))
            prefix = f"批处理 {idx}/{self._batch_total}"
            if self._current_item_name:
                prefix += f"（{self._current_item_name}）"
            parts.append(prefix)
        elif self._root_tool == "composite":
            step = "步骤 2/2 赋值" if self._composite_stage == "zonal" else "步骤 1/2 分割"
            parts.append(step)

        stage_chunk = self._stage_label
        if self._stage_detail and self._stage_detail != self._current_item_name:
            stage_chunk = f"{stage_chunk} · {self._stage_detail}"
        parts.append(stage_chunk)
        headline = " · ".join(part for part in parts if part)

        if self._cancelling:
            pill_text = "取消中"
            bar_text = f"{int(round(self._percent))}% · 正在安全取消…"
        elif self._indeterminate:
            pill_text = "启动中"
            bar_text = f"{self._stage_label}…"
        else:
            pct_int = int(round(self._percent))
            pill_text = f"运行中 {pct_int}%"
            pct_str = f"{self._percent:.1f}%" if self._percent < 99.9 else "100%"
            bar_text = f"{pct_str} · {self._stage_label}"

        if eta is not None:
            time_text = f"已用 {format_duration(elapsed)} · 预计剩余 {format_duration(eta)}"
        else:
            time_text = f"已用 {format_duration(elapsed)} · 正在估算剩余时间…"

        return ProgressSnapshot(
            percent=self._percent,
            indeterminate=self._indeterminate,
            headline=headline,
            bar_text=bar_text,
            pill_text=pill_text,
            pill_state=self._pill_state,
            elapsed_seconds=elapsed,
            eta_seconds=eta,
            time_text=time_text,
        )


@dataclass(frozen=True)
class ItemProgressSnapshot:
    item_id: str
    name: str
    output_name: str
    state: str  # "queued" | "running" | "succeeded" | "cancelled" | "failed"
    status_text: str
    percent: float
    bar_text: str
    detail: str
    elapsed_text: str

    @property
    def permille(self) -> int:
        return max(0, min(1000, int(round(self.percent * 10.0))))


@dataclass(frozen=True)
class BatchMonitorSnapshot:
    overall: ProgressSnapshot
    items: tuple[ItemProgressSnapshot, ...]
    active_count: int
    completed_count: int
    total_count: int


class _ItemTrack:
    def __init__(self, item_id: str, name: str, output_name: str, item_tool: str, clock: Callable[[], float]) -> None:
        self.item_id = item_id
        self.name = name
        self.output_name = output_name
        self.item_tool = item_tool
        self._clock = clock
        self.state = "queued"
        self.status_text = "排队等待"
        self.detail = "等待调度"
        self.percent = 0.0
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.tracker = ProgressTracker(clock=clock)

    def ensure_started(self) -> None:
        if self.state == "queued":
            self.state = "running"
            self.status_text = "启动中"
            self.started_at = self._clock()
            self.tracker.start(self.item_tool, total_items=1)

    def update(self, event: dict) -> None:
        if self.state in ("succeeded", "cancelled", "failed"):
            return
        self.ensure_started()
        kind = str(event.get("kind", ""))
        phase = str(event.get("phase", ""))
        data = event.get("data") or {}

        if phase == "batch" and "output" in data:
            item_status = str(data.get("item_status", "succeeded"))
            self.mark_done(item_status)
            return
        if kind in ("completed", "succeeded") and phase in ("segment", "zonal", "composite"):
            if self.item_tool != "composite" or phase == "composite":
                self.mark_done("succeeded")
                return
        if kind in ("failed", "cancelled") and phase in ("segment", "zonal", "composite"):
            self.mark_done(kind)
            return

        snap = self.tracker.update(event)
        self.percent = max(self.percent, min(99.5, snap.percent))
        if phase == "batch" and "input" in data:
            self.status_text = "初始化"
            self.detail = "已进入调度队列"
        else:
            self.status_text = self.tracker._stage_label
            self.detail = self.tracker._stage_detail or self.status_text

    def mark_done(self, status_value: str) -> None:
        now = self._clock()
        if self.started_at is None:
            self.started_at = now
        self.finished_at = now
        if status_value in ("succeeded", "completed"):
            self.state = "succeeded"
            self.status_text = "已完成"
            self.detail = self.output_name
            self.percent = 100.0
        elif status_value == "cancelled":
            self.state = "cancelled"
            self.status_text = "已取消"
            self.detail = "已保存断点"
        else:
            self.state = "failed"
            self.status_text = "失败"
            self.detail = "请查看下方日志"

    def snapshot(self) -> ItemProgressSnapshot:
        now = self._clock()
        if self.started_at is None:
            elapsed_text = "--:--"
        else:
            end = self.finished_at if self.finished_at is not None else now
            elapsed_text = format_duration(max(0.0, end - self.started_at))

        if self.state == "queued":
            bar_text = "排队中"
        elif self.state == "succeeded":
            bar_text = "100% · 已完成"
        elif self.state == "cancelled":
            bar_text = f"{int(round(self.percent))}% · 已取消"
        elif self.state == "failed":
            bar_text = f"{int(round(self.percent))}% · 失败"
        else:
            pct_str = f"{self.percent:.1f}%" if self.percent < 99.9 else "99.5%"
            bar_text = f"{pct_str} · {self.status_text}"

        return ItemProgressSnapshot(
            item_id=self.item_id,
            name=self.name,
            output_name=self.output_name,
            state=self.state,
            status_text=self.status_text,
            percent=self.percent,
            bar_text=bar_text,
            detail=self.detail,
            elapsed_text=elapsed_text,
        )


class BatchMonitorModel:
    """Tracks independent per-item progress bars and exact aggregate progress across overlapping scenes."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._tool = ""
        self._items: list[_ItemTrack] = []
        self._by_id: dict[str, _ItemTrack] = {}
        self._by_name: dict[str, _ItemTrack] = {}
        self._running = False
        self._cancelling = False
        self._started_at: float | None = None
        self._finished_elapsed: float | None = None
        self._final_status: TaskStatus | None = None
        self._percent = 0.0
        self._eta_anchor_time: float | None = None
        self._eta_anchor_percent = 0.0
        self._smoothed_rate: float | None = None
        self._last_eta_seconds: float | None = None
        self._last_eta_at: float | None = None
        self._log_formatter = ProgressTracker(clock=clock)

    def start(self, tool: str, items_preview: tuple[dict, ...] | list[dict]) -> BatchMonitorSnapshot:
        now = self._clock()
        self._tool = tool
        item_tool = "composite" if "composite" in tool else ("zonal" if "zonal" in tool else "segment")
        self._items = [
            _ItemTrack(
                item_id=str(entry["item_id"]),
                name=str(entry.get("name") or Path(str(entry["item_id"])).name),
                output_name=str(entry.get("output_name") or Path(str(entry.get("output", ""))).name),
                item_tool=item_tool,
                clock=self._clock,
            )
            for entry in items_preview
        ]
        self._by_id = {track.item_id.casefold(): track for track in self._items}
        self._by_name = {track.name.casefold(): track for track in self._items}
        self._running = True
        self._cancelling = False
        self._started_at = now
        self._finished_elapsed = None
        self._final_status = None
        self._percent = 0.0
        self._eta_anchor_time = now
        self._eta_anchor_percent = 0.0
        self._smoothed_rate = None
        self._last_eta_seconds = None
        self._last_eta_at = None
        if len(self._items) == 1:
            self._items[0].ensure_started()
        return self.snapshot()

    def mark_cancelling(self) -> BatchMonitorSnapshot:
        if self._running:
            self._cancelling = True
        return self.snapshot()

    def _resolve_track(self, event: dict) -> _ItemTrack | None:
        if not self._items:
            return None
        data = event.get("data") or {}
        item_id = data.get("item_id") or data.get("input")
        if item_id:
            found = self._by_id.get(str(item_id).casefold())
            if found is not None:
                return found
        item_name = data.get("item_name")
        if item_name:
            found = self._by_name.get(str(item_name).casefold())
            if found is not None:
                return found
        item_index = data.get("item_index")
        if item_index is not None:
            idx = int(item_index) - 1
            if 0 <= idx < len(self._items):
                return self._items[idx]
        if len(self._items) == 1:
            return self._items[0]
        return None

    def update(self, event: dict) -> BatchMonitorSnapshot:
        now = self._clock()
        if self._started_at is None:
            self._started_at = now
            self._eta_anchor_time = now
            self._running = True

        track = self._resolve_track(event)
        if track is not None:
            track.update(event)

        if self._items:
            raw = sum(item.percent for item in self._items) / len(self._items)
            new_percent = max(self._percent, min(99.5, raw))
            if new_percent > self._percent:
                prev = self._percent
                self._percent = new_percent
                self._update_eta_rate(now, prev, new_percent)

        return self.snapshot()

    def tick(self) -> BatchMonitorSnapshot:
        return self.snapshot()

    def finish(self, status: TaskStatus) -> BatchMonitorSnapshot:
        now = self._clock()
        self._finished_elapsed = (now - self._started_at) if self._started_at is not None else 0.0
        self._running = False
        self._cancelling = False
        self._final_status = status
        self._last_eta_seconds = None
        if status is TaskStatus.SUCCEEDED:
            for track in self._items:
                if track.state not in ("succeeded", "failed", "cancelled"):
                    track.mark_done("succeeded")
            self._percent = 100.0
        elif status is TaskStatus.CANCELLED:
            for track in self._items:
                if track.state == "running":
                    track.mark_done("cancelled")
        elif status is TaskStatus.FAILED:
            for track in self._items:
                if track.state == "running":
                    track.mark_done("failed")
        return self.snapshot()

    def format_log_line(self, event: dict) -> str:
        data = event.get("data") or {}
        base = self._log_formatter.format_log_line(event)
        item_name = data.get("item_name")
        if item_name and len(self._items) > 1 and str(item_name) not in base:
            return f"{base}（{_short_name(str(item_name), 24)}）"
        return base

    def _update_eta_rate(self, now: float, prev_percent: float, new_percent: float) -> None:
        if self._started_at is None or self._eta_anchor_time is None:
            return
        if prev_percent == 0.0 and (now - self._started_at) <= 1.5 and new_percent >= 15.0:
            self._eta_anchor_time = now
            self._eta_anchor_percent = new_percent
            self._smoothed_rate = None
            self._last_eta_seconds = None
            self._last_eta_at = None
            return
        dt = now - self._eta_anchor_time
        earned = new_percent - self._eta_anchor_percent
        if dt >= 2.0 and earned >= 1.0 and new_percent < 99.5:
            observed_rate = earned / dt
            self._smoothed_rate = (
                observed_rate if self._smoothed_rate is None else 0.75 * self._smoothed_rate + 0.25 * observed_rate
            )
            if self._smoothed_rate > 1e-6:
                self._last_eta_seconds = max(1.0, (100.0 - new_percent) / self._smoothed_rate)
                self._last_eta_at = now

    def _current_eta(self, now: float) -> float | None:
        if not self._running or self._last_eta_seconds is None or self._last_eta_at is None:
            return None
        return max(1.0, self._last_eta_seconds - max(0.0, now - self._last_eta_at))

    def snapshot(self) -> BatchMonitorSnapshot:
        now = self._clock()
        elapsed = (
            self._finished_elapsed
            if self._finished_elapsed is not None
            else (max(0.0, now - self._started_at) if self._started_at is not None else 0.0)
        )
        item_snaps = tuple(item.snapshot() for item in self._items)
        total = len(item_snaps)
        done = sum(1 for item in item_snaps if item.state == "succeeded")
        active = sum(1 for item in item_snaps if item.state == "running")

        if not self._running and self._final_status is not None:
            label, pill_state = _STATUS_LABELS.get(self._final_status, (self._final_status.value, "idle"))
            headline = f"{label} · 成功 {done}/{total} 项" if total > 1 else label
            overall = ProgressSnapshot(
                percent=self._percent,
                indeterminate=False,
                headline=headline,
                bar_text=f"{int(round(self._percent))}% · {label}",
                pill_text=label,
                pill_state=pill_state,
                elapsed_seconds=elapsed,
                eta_seconds=None,
                time_text=f"总耗时 {format_duration(elapsed)}",
            )
            return BatchMonitorSnapshot(overall, item_snaps, active, done, total)

        eta = self._current_eta(now)
        time_text = (
            f"已用 {format_duration(elapsed)} · 预计剩余 {format_duration(eta)}"
            if eta is not None
            else f"已用 {format_duration(elapsed)} · 正在估算剩余时间…"
        )
        if self._cancelling:
            headline = f"正在安全取消 · 已完成 {done}/{total} 项"
            bar_text = f"{int(round(self._percent))}% · 正在安全取消…"
            pill_text = "取消中"
            pill_state = "cancelling"
        else:
            tool_label = _PHASE_LABELS.get(self._tool, "任务执行")
            if total > 1:
                overlap_hint = "（尾段并行加速中）" if active >= 2 else ""
                headline = f"总进度 · 已完成 {done}/{total} 项 · 运行中 {active} 项{overlap_hint}"
            else:
                first_stage = item_snaps[0].status_text if item_snaps else tool_label
                headline = f"{tool_label} · {first_stage}"
            pct_str = f"{self._percent:.1f}%" if self._percent < 99.9 else "100%"
            bar_text = f"{pct_str} · {tool_label}"
            pill_text = f"运行中 {int(round(self._percent))}%" if self._percent > 0 else "启动中"
            pill_state = "running"

        overall = ProgressSnapshot(
            percent=self._percent,
            indeterminate=(self._percent <= 0.0 and active == 0 and self._running),
            headline=headline,
            bar_text=bar_text,
            pill_text=pill_text,
            pill_state=pill_state,
            elapsed_seconds=elapsed,
            eta_seconds=eta,
            time_text=time_text,
        )
        return BatchMonitorSnapshot(overall, item_snaps, active, done, total)
