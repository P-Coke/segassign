"""Reorder Shapefile records byte-for-byte for ordering diagnostics."""

from __future__ import annotations

import argparse
import json
import shutil
import struct
from pathlib import Path

import numpy as np
import pyogrio

from segassign.zonal.shapefile import read_polygon_bounds, write_envelope_index


def _record_index(path: Path) -> tuple[np.ndarray, np.ndarray]:
    raw = path.with_suffix(".shx").read_bytes()
    if len(raw) < 100 or (len(raw) - 100) % 8:
        raise ValueError(f"invalid SHX file: {path.with_suffix('.shx')}")
    index = np.frombuffer(raw[100:], dtype=">i4").reshape(-1, 2).astype(np.int64)
    return index[:, 0] * 2, index[:, 1]


def _write_shape_records(source: Path, destination: Path, order: np.ndarray) -> None:
    offsets, lengths = _record_index(source)
    source_shape = source.read_bytes()
    shape_header = bytearray(source_shape[:100])
    index_header = bytearray(source.with_suffix(".shx").read_bytes()[:100])
    shape_records: list[bytes] = []
    index_records: list[bytes] = []
    output_offset = 100
    for record_number, source_position in enumerate(order, 1):
        offset = int(offsets[source_position])
        content_words = int(lengths[source_position])
        content = source_shape[offset + 8 : offset + 8 + content_words * 2]
        shape_records.append(struct.pack(">2i", record_number, content_words) + content)
        index_records.append(struct.pack(">2i", output_offset // 2, content_words))
        output_offset += 8 + len(content)
    struct.pack_into(">i", shape_header, 24, output_offset // 2)
    index_size = 100 + 8 * len(order)
    struct.pack_into(">i", index_header, 24, index_size // 2)
    destination.write_bytes(bytes(shape_header) + b"".join(shape_records))
    destination.with_suffix(".shx").write_bytes(bytes(index_header) + b"".join(index_records))


def _write_dbf_records(source: Path, destination: Path, order: np.ndarray) -> None:
    raw = source.with_suffix(".dbf").read_bytes()
    feature_count = struct.unpack_from("<I", raw, 4)[0]
    header_length = struct.unpack_from("<H", raw, 8)[0]
    record_length = struct.unpack_from("<H", raw, 10)[0]
    if feature_count != len(order):
        raise ValueError("DBF and requested record counts differ")
    records_end = header_length + feature_count * record_length
    records = memoryview(raw)[header_length:records_end]
    output = bytearray(raw[:header_length])
    for source_position in order:
        start = int(source_position) * record_length
        output.extend(records[start : start + record_length])
    output.extend(raw[records_end:])
    destination.with_suffix(".dbf").write_bytes(output)


def reorder(source: Path, destination: Path, tile_order_path: Path | None = None) -> None:
    attributes = pyogrio.read_dataframe(
        source,
        columns=["tile_id", "gap_fill"],
        read_geometry=False,
    )
    tile_parts = attributes["tile_id"].str.extract(r"r(\d+)_c(\d+)").astype("int64")
    if tile_order_path is None:
        tile_rank = tile_parts[0].to_numpy() * 1_000_000 - tile_parts[1].to_numpy()
    else:
        configured = json.loads(tile_order_path.read_text(encoding="utf-8"))["tile_order"]
        ranks = {tile_id: rank for rank, tile_id in enumerate(configured)}
        tile_rank = attributes["tile_id"].map(ranks).to_numpy()
        if np.isnan(tile_rank).any():
            raise ValueError("tile-order baseline does not cover every feature")
    order = np.lexsort((tile_rank, attributes["gap_fill"].to_numpy()))
    destination.parent.mkdir(parents=True, exist_ok=True)
    for path in destination.parent.glob(destination.stem + ".*"):
        path.unlink(missing_ok=True)
    _write_shape_records(source, destination, order)
    _write_dbf_records(source, destination, order)
    for suffix in (".cpg", ".prj"):
        sidecar = source.with_suffix(suffix)
        if sidecar.is_file():
            shutil.copy2(sidecar, destination.with_suffix(suffix))
    write_envelope_index(destination, read_polygon_bounds(destination))
    if int(pyogrio.read_info(destination)["features"]) != len(order):
        raise RuntimeError("reordered Shapefile feature count changed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--tile-order", type=Path)
    arguments = parser.parse_args()
    reorder(
        arguments.source.resolve(),
        arguments.destination.resolve(),
        arguments.tile_order.resolve() if arguments.tile_order else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
