"""Immutable requests plus shared plan, event, and result models."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .ecognition import normalize_ecognition_bin


def _absolute(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


def _paths(values: tuple[str | os.PathLike[str], ...]) -> tuple[Path, ...]:
    return tuple(_absolute(value) for value in values)


def repository_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS).resolve()
    return Path(__file__).resolve().parents[2]


def asset_path(*parts: str) -> Path:
    return repository_root().joinpath("assets", *parts).resolve()


class TaskStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    logical_cpus: int = field(default_factory=lambda: max(1, os.cpu_count() or 1))
    memory_fraction: float = 0.80
    ecognition_workers: int | None = None
    raster_workers: int | None = None

    def __post_init__(self) -> None:
        if self.logical_cpus < 1:
            raise ValueError("logical_cpus must be positive")
        if not 0.10 <= self.memory_fraction <= 0.95:
            raise ValueError("memory_fraction must be between 0.10 and 0.95")
        for value in (self.ecognition_workers, self.raster_workers):
            if value is not None and value < 1:
                raise ValueError("worker counts must be positive")


@dataclass(frozen=True, slots=True)
class Artifact:
    kind: str
    path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _absolute(self.path))


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    tool: str
    inputs: tuple[Path, ...]
    outputs: tuple[Path, ...]
    stages: tuple[str, ...]
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", _paths(self.inputs))
        object.__setattr__(self, "outputs", _paths(self.outputs))
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))


@dataclass(frozen=True, slots=True)
class TaskEvent:
    sequence: int
    kind: str
    phase: str
    message: str
    data: Mapping[str, Any] = field(default_factory=dict)
    task_id: str = ""
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))

    def serializable(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "phase": self.phase,
            "message": self.message,
            "data": dict(self.data),
            "task_id": self.task_id,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class SegmentOptions:
    tile_size: int = 3072
    buffer_pixels: int = 256
    ecognition_workers: int | None = None
    postprocess_workers: int | None = None
    invalid_pixel_values: tuple[int, ...] = (0, 65535, 65565)
    default_crs: str = "EPSG:4490"
    ruleset_mode: str = "invalid_classify"
    invalid_class_name: str = "new class"
    max_attempts: int = 3
    timeout_seconds: float = 1800.0
    tile_compression: str = "none"
    postprocess_max_tiles_per_worker: int = 8

    def __post_init__(self) -> None:
        if self.tile_size < 512:
            raise ValueError("tile_size must be at least 512")
        if not 0 <= self.buffer_pixels < self.tile_size:
            raise ValueError("buffer_pixels must be smaller than tile_size")
        if self.ecognition_workers is not None and self.ecognition_workers < 1:
            raise ValueError("ecognition_workers must be positive")
        if self.postprocess_workers is not None and self.postprocess_workers < 1:
            raise ValueError("postprocess_workers must be positive")
        if self.ruleset_mode not in {"plain", "invalid_classify"}:
            raise ValueError("ruleset_mode must be plain or invalid_classify")
        if self.postprocess_max_tiles_per_worker < 1:
            raise ValueError("postprocess_max_tiles_per_worker must be positive")


@dataclass(frozen=True, slots=True)
class SegmentRequest:
    process_raster: Path
    base_raster: Path
    output_vector: Path
    ecognition_bin: Path
    ruleset: Path = field(default_factory=lambda: asset_path("rulesets", "segment.dcp"))
    invalid_ruleset: Path = field(default_factory=lambda: asset_path("rulesets", "invalid-class.dcp"))
    bridge: Path = field(default_factory=lambda: asset_path("vendor", "ecognition-9", "bridge.exe"))
    options: SegmentOptions = field(default_factory=SegmentOptions)

    def __post_init__(self) -> None:
        for name in (
            "process_raster",
            "base_raster",
            "output_vector",
            "ecognition_bin",
            "ruleset",
            "invalid_ruleset",
            "bridge",
        ):
            object.__setattr__(self, name, _absolute(getattr(self, name)))
        object.__setattr__(self, "ecognition_bin", normalize_ecognition_bin(self.ecognition_bin))
        if self.output_vector.suffix.casefold() != ".shp":
            raise ValueError("segment output_vector must be a .shp path")


@dataclass(frozen=True, slots=True)
class SegmentBatchRequest:
    """Segment one raster or a directory tree of rasters into an output directory."""

    process_source: Path
    base_source: Path
    output_directory: Path
    ecognition_bin: Path
    ruleset: Path = field(default_factory=lambda: asset_path("rulesets", "segment.dcp"))
    invalid_ruleset: Path = field(default_factory=lambda: asset_path("rulesets", "invalid-class.dcp"))
    bridge: Path = field(default_factory=lambda: asset_path("vendor", "ecognition-9", "bridge.exe"))
    options: SegmentOptions = field(default_factory=SegmentOptions)
    recursive: bool = True
    selected_inputs: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "process_source",
            "base_source",
            "output_directory",
            "ecognition_bin",
            "ruleset",
            "invalid_ruleset",
            "bridge",
        ):
            object.__setattr__(self, name, _absolute(getattr(self, name)))
        object.__setattr__(self, "ecognition_bin", normalize_ecognition_bin(self.ecognition_bin))
        object.__setattr__(self, "selected_inputs", _paths(self.selected_inputs))


@dataclass(frozen=True, slots=True)
class ZonalOptions:
    valid_values: tuple[int, ...] = ()
    nodata: int = 0
    class_threshold: int = 65
    tile_size: int = 3072
    workers: int | None = None
    output_encoding: str = "UTF-8"
    vector_crs_override: str = ""
    raster_crs_override: str = ""

    def __post_init__(self) -> None:
        if not 0 <= self.class_threshold <= 100:
            raise ValueError("class_threshold must be between 0 and 100")
        if self.tile_size < 256:
            raise ValueError("tile_size must be at least 256")
        if self.workers is not None and self.workers < 1:
            raise ValueError("workers must be positive")


@dataclass(frozen=True, slots=True)
class ZonalRequest:
    vector: Path
    raster: Path
    output_vector: Path
    options: ZonalOptions = field(default_factory=ZonalOptions)

    def __post_init__(self) -> None:
        for name in ("vector", "raster", "output_vector"):
            object.__setattr__(self, name, _absolute(getattr(self, name)))
        if self.output_vector.suffix.casefold() != ".shp":
            raise ValueError("zonal output_vector must be a .shp path")


@dataclass(frozen=True, slots=True)
class ZonalBatchRequest:
    """Assign one classification raster to one Shapefile or a directory tree."""

    vector_source: Path
    raster: Path
    output_directory: Path
    options: ZonalOptions = field(default_factory=ZonalOptions)
    recursive: bool = True
    selected_inputs: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        for name in ("vector_source", "raster", "output_directory"):
            object.__setattr__(self, name, _absolute(getattr(self, name)))
        object.__setattr__(self, "selected_inputs", _paths(self.selected_inputs))


@dataclass(frozen=True, slots=True)
class CompositeRequest:
    process_raster: Path
    base_raster: Path
    classification_raster: Path
    output_vector: Path
    ecognition_bin: Path
    ruleset: Path = field(default_factory=lambda: asset_path("rulesets", "segment.dcp"))
    invalid_ruleset: Path = field(default_factory=lambda: asset_path("rulesets", "invalid-class.dcp"))
    bridge: Path = field(default_factory=lambda: asset_path("vendor", "ecognition-9", "bridge.exe"))
    segment_options: SegmentOptions = field(default_factory=SegmentOptions)
    zonal_options: ZonalOptions = field(default_factory=ZonalOptions)

    def __post_init__(self) -> None:
        for name in (
            "process_raster",
            "base_raster",
            "classification_raster",
            "output_vector",
            "ecognition_bin",
            "ruleset",
            "invalid_ruleset",
            "bridge",
        ):
            object.__setattr__(self, name, _absolute(getattr(self, name)))
        object.__setattr__(self, "ecognition_bin", normalize_ecognition_bin(self.ecognition_bin))
        if self.output_vector.suffix.casefold() != ".shp":
            raise ValueError("composite output_vector must be a .shp path")


@dataclass(frozen=True, slots=True)
class CompositeBatchRequest:
    """Segment and assign one raster or a directory tree of rasters into an output directory."""

    process_source: Path
    base_source: Path
    classification_source: Path
    output_directory: Path
    ecognition_bin: Path
    ruleset: Path = field(default_factory=lambda: asset_path("rulesets", "segment.dcp"))
    invalid_ruleset: Path = field(default_factory=lambda: asset_path("rulesets", "invalid-class.dcp"))
    bridge: Path = field(default_factory=lambda: asset_path("vendor", "ecognition-9", "bridge.exe"))
    segment_options: SegmentOptions = field(default_factory=SegmentOptions)
    zonal_options: ZonalOptions = field(default_factory=ZonalOptions)
    recursive: bool = True
    selected_inputs: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "process_source",
            "base_source",
            "classification_source",
            "output_directory",
            "ecognition_bin",
            "ruleset",
            "invalid_ruleset",
            "bridge",
        ):
            object.__setattr__(self, name, _absolute(getattr(self, name)))
        object.__setattr__(self, "ecognition_bin", normalize_ecognition_bin(self.ecognition_bin))
        object.__setattr__(self, "selected_inputs", _paths(self.selected_inputs))


@dataclass(frozen=True, slots=True)
class BackendResult:
    status: TaskStatus
    artifacts: tuple[Artifact, ...]
    warnings: tuple[str, ...]
    timings: Mapping[str, float]
    peak_memory_mb: float
    report_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "timings", MappingProxyType(dict(self.timings)))
        object.__setattr__(self, "report_path", _absolute(self.report_path))


@dataclass(frozen=True, slots=True)
class SegmentResult(BackendResult):
    primary_vector: Path
    tiles_total: int
    tiles_processed: int
    tiles_resumed: int = 0

    def __post_init__(self) -> None:
        BackendResult.__post_init__(self)
        object.__setattr__(self, "primary_vector", _absolute(self.primary_vector))


@dataclass(frozen=True, slots=True)
class SegmentBatchResult(BackendResult):
    primary_vectors: tuple[Path, ...]
    item_results: tuple[SegmentResult, ...]
    items_total: int
    items_succeeded: int

    def __post_init__(self) -> None:
        BackendResult.__post_init__(self)
        object.__setattr__(self, "primary_vectors", _paths(self.primary_vectors))
        object.__setattr__(self, "item_results", tuple(self.item_results))


@dataclass(frozen=True, slots=True)
class ZonalResult(BackendResult):
    primary_vector: Path
    features: int
    features_with_statistics: int

    def __post_init__(self) -> None:
        BackendResult.__post_init__(self)
        object.__setattr__(self, "primary_vector", _absolute(self.primary_vector))


@dataclass(frozen=True, slots=True)
class ZonalBatchResult(BackendResult):
    primary_vectors: tuple[Path, ...]
    item_results: tuple[ZonalResult, ...]
    items_total: int
    items_succeeded: int

    def __post_init__(self) -> None:
        BackendResult.__post_init__(self)
        object.__setattr__(self, "primary_vectors", _paths(self.primary_vectors))
        object.__setattr__(self, "item_results", tuple(self.item_results))


@dataclass(frozen=True, slots=True)
class CompositeResult(BackendResult):
    primary_vector: Path
    segment_result: SegmentResult | None
    zonal_result: ZonalResult | None

    def __post_init__(self) -> None:
        BackendResult.__post_init__(self)
        object.__setattr__(self, "primary_vector", _absolute(self.primary_vector))


@dataclass(frozen=True, slots=True)
class CompositeBatchResult(BackendResult):
    primary_vectors: tuple[Path, ...]
    item_results: tuple[CompositeResult, ...]
    items_total: int
    items_succeeded: int

    def __post_init__(self) -> None:
        BackendResult.__post_init__(self)
        object.__setattr__(self, "primary_vectors", _paths(self.primary_vectors))
        object.__setattr__(self, "item_results", tuple(self.item_results))
