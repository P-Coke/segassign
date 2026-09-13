from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import rasterio
from rasterio.errors import RasterioIOError
from rasterio.transform import from_origin
from rasterio.windows import Window

from segassign.runtime import CancelledError, RunContext
from segassign.segment.reading import RasterReadError, RasterWindowReader


def _striped(path):
    values = np.arange(2 * 40 * 48, dtype=np.uint16).reshape(2, 40, 48)
    with rasterio.open(
        path, "w", driver="GTiff", count=2, width=48, height=40, dtype="uint16",
        nodata=0, crs="EPSG:3857", transform=from_origin(0, 40, 1, 1), tiled=False, blockysize=1,
    ) as dataset:
        dataset.write(values)


def _failing_handle(source):
    return SimpleNamespace(
        name=source.name, count=source.count, width=source.width, height=source.height,
        block_shapes=source.block_shapes,
        read=Mock(side_effect=RasterioIOError("injected temporary read failure")),
    )


def test_reopen_recovers_pixels_and_mask_without_reusing_failed_handle(tmp_path, monkeypatch):
    path = tmp_path / "striped.tif"
    _striped(path)
    window = Window(4, 0, 12, 20)
    next_window = Window(0, 0, 16, 24)
    events = []
    with rasterio.open(path) as source:
        expected = source.read(window=window, masked=True)
        expected_next = source.read(window=next_window, masked=True)
        failing = _failing_handle(source)
        open_dataset = rasterio.open
        reopen = Mock(side_effect=open_dataset)
        monkeypatch.setattr("segassign.segment.reading.rasterio.open", reopen)
        with RasterWindowReader(
            failing, role="夏季影像", context=RunContext(tmp_path / "work", event_sink=events.append),
        ) as reader:
            recovered = reader.read(window, tile_ids=("r000_c000",))
            following = reader.read(next_window, tile_ids=("r000_c001",))
            handle = reader.dataset

    np.testing.assert_array_equal(recovered.data, expected.data)
    np.testing.assert_array_equal(recovered.mask, expected.mask)
    np.testing.assert_array_equal(following.data, expected_next.data)
    np.testing.assert_array_equal(following.mask, expected_next.mask)
    assert failing.read.call_count == 1
    reopen.assert_called_once_with(str(path), sharing=False, NUM_THREADS="1")
    assert handle.closed
    assert len(events) == 2
    assert "读取成功" in events[-1].message


def test_truncated_tiff_fails_with_source_window_and_original_cause(tmp_path):
    path = tmp_path / "truncated.tif"
    _striped(path)
    # Remove four complete interleaved strips while preserving the TIFF header.
    with path.open("r+b") as stream:
        stream.truncate(path.stat().st_size - 4 * 48 * 2 * 2)
    unchanged = path.read_bytes()
    window = Window(0, 38, 48, 2)
    with rasterio.open(path) as dataset, RasterWindowReader(dataset, role="冬季影像") as reader:
        with pytest.raises(RasterReadError) as caught:
            reader.read(window, tile_ids=("r009_c014",))

    diagnostic = caught.value.diagnostic
    assert diagnostic["source"] == str(path)
    assert diagnostic["role"] == "冬季影像"
    assert diagnostic["tile_ids"] == ["r009_c014"]
    assert diagnostic["window"] == {"col_off": 0, "row_off": 38, "width": 48, "height": 2}
    assert diagnostic["attempts"] == 3
    assert len(diagnostic["errors"]) == 3
    assert all("TIFFReadEncodedStrip" in error for error in diagnostic["errors"])
    assert isinstance(caught.value.__cause__, RasterioIOError)
    assert path.read_bytes() == unchanged


def test_cancel_during_retry_does_not_open_another_handle(tmp_path, monkeypatch):
    path = tmp_path / "striped.tif"
    _striped(path)
    context = RunContext(tmp_path / "work")
    context.event_sink = lambda _event: context.cancellation.cancel()
    with rasterio.open(path) as dataset:
        reopen = Mock(side_effect=AssertionError("cancelled read must not reopen"))
        monkeypatch.setattr("segassign.segment.reading.rasterio.open", reopen)
        with RasterWindowReader(_failing_handle(dataset), role="夏季影像", context=context) as reader:
            with pytest.raises(CancelledError):
                reader.read(Window(0, 0, 12, 12), tile_ids=("r000_c000",))
    reopen.assert_not_called()


def test_invalid_read_arguments_are_not_retried(tmp_path, monkeypatch):
    path = tmp_path / "striped.tif"
    _striped(path)
    with rasterio.open(path) as dataset:
        failing = _failing_handle(dataset)
        failing.read.side_effect = ValueError("invalid window")
        reopen = Mock(side_effect=AssertionError("programming errors must not retry"))
        monkeypatch.setattr("segassign.segment.reading.rasterio.open", reopen)
        with RasterWindowReader(failing, role="夏季影像") as reader:
            with pytest.raises(ValueError, match="invalid window"):
                reader.read(Window(0, 0, 12, 12), tile_ids=("r000_c000",))
    reopen.assert_not_called()
