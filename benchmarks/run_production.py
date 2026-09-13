"""Reproducible full production cold run using only the Geo-Workspace environment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from segassign import CompositeRequest, ResourceBudget, RunContext, SegmentOptions, ZonalOptions, composite


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--process", type=Path, required=True)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--classification", type=Path, required=True)
    parser.add_argument("--ecognition-bin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--ecognition-workers", type=int, default=5)
    parser.add_argument("--raster-workers", type=int, default=7)
    arguments = parser.parse_args()
    request = CompositeRequest(
        process_raster=arguments.process,
        base_raster=arguments.base,
        classification_raster=arguments.classification,
        output_vector=arguments.output,
        ecognition_bin=arguments.ecognition_bin,
        segment_options=SegmentOptions(
            tile_size=3072,
            buffer_pixels=256,
            ecognition_workers=arguments.ecognition_workers,
            postprocess_workers=4,
        ),
        zonal_options=ZonalOptions(
            valid_values=tuple(range(1, 14)),
            tile_size=3072,
            workers=arguments.raster_workers,
        ),
    )

    def emit(event):
        print(json.dumps(event.serializable(), ensure_ascii=False, default=str), flush=True)

    result = composite.run(
        request,
        RunContext(
            arguments.work,
            event_sink=emit,
            resources=ResourceBudget(
                ecognition_workers=arguments.ecognition_workers,
                raster_workers=arguments.raster_workers,
            ),
            resume=arguments.resume,
        ),
    )
    print(result, flush=True)
    return 0 if result.status.value == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
