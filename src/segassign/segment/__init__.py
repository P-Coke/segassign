"""Public segmentation API."""

from __future__ import annotations

from ..models import ExecutionPlan, SegmentRequest, SegmentResult


def plan(request: SegmentRequest) -> ExecutionPlan:
    from .service import plan as build_plan

    return build_plan(request)


def run(request: SegmentRequest, context=None) -> SegmentResult:
    from .service import run as execute

    return execute(request, context)


__all__ = ["plan", "run"]
