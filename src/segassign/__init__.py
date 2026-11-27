"""Public package for the SegAssign backend."""

from . import composite, segment, zonal
from .ecognition import detect_ecognition_bin, discover_ecognition_bins, resolve_ecognition_bin
from .models import (
    CompositeBatchRequest,
    CompositeBatchResult,
    CompositeRequest,
    CompositeResult,
    ExecutionPlan,
    ResourceBudget,
    SegmentBatchRequest,
    SegmentBatchResult,
    SegmentOptions,
    SegmentRequest,
    SegmentResult,
    TaskEvent,
    TaskStatus,
    ZonalBatchRequest,
    ZonalBatchResult,
    ZonalOptions,
    ZonalRequest,
    ZonalResult,
)
from .runtime import RunContext

__all__ = [
    "CompositeBatchRequest",
    "CompositeBatchResult",
    "CompositeRequest",
    "CompositeResult",
    "ExecutionPlan",
    "ResourceBudget",
    "RunContext",
    "SegmentBatchRequest",
    "SegmentBatchResult",
    "SegmentOptions",
    "SegmentRequest",
    "SegmentResult",
    "TaskEvent",
    "TaskStatus",
    "ZonalBatchRequest",
    "ZonalBatchResult",
    "ZonalOptions",
    "ZonalRequest",
    "ZonalResult",
    "composite",
    "detect_ecognition_bin",
    "discover_ecognition_bins",
    "resolve_ecognition_bin",
    "segment",
    "zonal",
]
