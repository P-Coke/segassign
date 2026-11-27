"""Shared GeoTIFF discovery and flexible multi-folder pairing for 00/01 batch runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import rasterio
from pyproj import CRS
from rasterio.warp import transform_bounds

RASTER_SUFFIXES = frozenset({".tif", ".tiff"})


@dataclass(frozen=True, slots=True)
class RasterFootprint:
    path: Path
    crs: CRS
    bounds: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class MatchedRaster:
    path: Path
    method: str
    warning: str | None = None


def _is_hidden_work_part(part: str) -> bool:
    lowered = part.casefold()
    return lowered.startswith(".geotoolbox") or lowered.startswith(".segassign")


def discover_rasters(source: Path, recursive: bool = True, *, label: str = "影像") -> tuple[Path, ...]:
    """Discover a deterministic tuple of GeoTIFF files from a single file or directory."""
    source = Path(source).expanduser().resolve()
    if source.is_file():
        if source.suffix.casefold() not in RASTER_SUFFIXES:
            raise ValueError(f"{label}输入必须是 GeoTIFF (.tif/.tiff) 文件：{source}")
        return (source,)
    if not source.is_dir():
        raise ValueError(f"{label}输入不存在：{source}")
    iterator = source.rglob("*") if recursive else source.iterdir()
    rasters = tuple(
        sorted(
            path.resolve()
            for path in iterator
            if path.is_file()
            and path.suffix.casefold() in RASTER_SUFFIXES
            and not any(_is_hidden_work_part(part) for part in path.relative_to(source).parts[:-1])
        )
    )
    if not rasters:
        raise ValueError(f"在目录中未找到任何 GeoTIFF (.tif/.tiff) 文件（{label}）：{source}")
    return rasters


def validate_output_directory(output_directory: Path, input_sources: tuple[Path, ...]) -> None:
    """Prevent writing batch results into an input directory or a .shp file path."""
    output_directory = Path(output_directory).expanduser().resolve()
    if output_directory.suffix.casefold() == ".shp":
        raise ValueError("批量处理模式下输出位置必须是文件夹，不能是单个 .shp 文件")
    if output_directory.exists() and not output_directory.is_dir():
        raise ValueError(f"输出目录路径已存在且不是文件夹：{output_directory}")
    for source in input_sources:
        resolved = Path(source).expanduser().resolve()
        if resolved.is_dir() and (output_directory == resolved or resolved in output_directory.parents):
            raise ValueError(f"批处理输出目录不能与输入目录相同或位于其子目录内：{output_directory}")


def output_vector_for(process_path: Path, process_source: Path, output_directory: Path) -> Path:
    """Compute the destination Shapefile path preserving relative structure and stem."""
    if process_source.is_dir():
        relative = process_path.relative_to(process_source)
        return (output_directory / relative).with_suffix(".shp")
    return output_directory / process_path.with_suffix(".shp").name


def _footprint(path: Path, default_crs: str) -> RasterFootprint:
    with rasterio.open(path) as dataset:
        crs = CRS.from_user_input(dataset.crs) if dataset.crs is not None else CRS.from_user_input(default_crs)
        return RasterFootprint(path=path, crs=crs, bounds=tuple(float(v) for v in dataset.bounds))


def _overlap_area(
    process_fp: RasterFootprint,
    candidate_fp: RasterFootprint,
) -> float:
    bounds = process_fp.bounds
    if process_fp.crs != candidate_fp.crs:
        try:
            bounds = transform_bounds(process_fp.crs, candidate_fp.crs, *bounds, densify_pts=21)
        except Exception:
            return 0.0
    left = max(bounds[0], candidate_fp.bounds[0])
    bottom = max(bounds[1], candidate_fp.bounds[1])
    right = min(bounds[2], candidate_fp.bounds[2])
    top = min(bounds[3], candidate_fp.bounds[3])
    if left >= right or bottom >= top:
        return 0.0
    return (right - left) * (top - bottom)


class RasterMatcher:
    """Pair discovered process rasters with a shared file or a candidate folder.

    Matching order when candidate_source is a directory:
      1. Exact relative path stem (case-insensitive, ignoring .tif/.tiff suffix).
      2. Unique filename stem (case-insensitive).
      3. Largest spatial overlap area (reprojecting bounds if CRS differs).
    """

    def __init__(
        self,
        candidate_source: Path,
        *,
        recursive: bool,
        label: str,
        default_crs: str = "EPSG:4490",
    ) -> None:
        self.source = Path(candidate_source).expanduser().resolve()
        self.label = label
        self.default_crs = default_crs
        self.shared_file = self.source.is_file()
        self.candidates = discover_rasters(self.source, recursive=recursive, label=label)
        self._by_rel: dict[str, Path] = {}
        self._by_stem: dict[str, list[Path]] = {}
        self._footprints: dict[Path, RasterFootprint] = {}
        if not self.shared_file:
            for path in self.candidates:
                rel_key = path.relative_to(self.source).with_suffix("").as_posix().casefold()
                self._by_rel[rel_key] = path
                self._by_stem.setdefault(path.stem.casefold(), []).append(path)

    def _get_footprint(self, path: Path) -> RasterFootprint:
        footprint = self._footprints.get(path)
        if footprint is None:
            footprint = _footprint(path, self.default_crs)
            self._footprints[path] = footprint
        return footprint

    def match(self, process_path: Path, process_source: Path) -> MatchedRaster:
        if self.shared_file:
            return MatchedRaster(self.candidates[0], "shared")
        if process_source.is_dir():
            rel_key = process_path.relative_to(process_source).with_suffix("").as_posix().casefold()
            exact = self._by_rel.get(rel_key)
            if exact is not None:
                return MatchedRaster(exact, "relative_path")
        stem_matches = self._by_stem.get(process_path.stem.casefold(), ())
        if len(stem_matches) == 1:
            return MatchedRaster(stem_matches[0], "stem")
        process_fp = self._get_footprint(process_path)
        scored = [
            (area, candidate)
            for candidate in self.candidates
            if (area := _overlap_area(process_fp, self._get_footprint(candidate))) > 0.0
        ]
        if not scored:
            raise ValueError(
                f"无法为亚米级影像 {process_path.name} 匹配{self.label}：目录 {self.source} 中无同名且无空间重叠的影像"
            )
        scored.sort(key=lambda item: (-item[0], str(item[1])))
        best_path = scored[0][1]
        warning = None
        if len(scored) > 1:
            warning = (
                f"{process_path.name} 与 {len(scored)} 幅{self.label}存在空间重叠，"
                f"已自动选择重叠面积最大的 {best_path.name}"
            )
        return MatchedRaster(best_path, "spatial_overlap", warning)
