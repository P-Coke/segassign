from __future__ import annotations

import numpy as np

from segassign.zonal.state import merge_counts, read_count_state, state_path, write_count_state


def test_raw_count_state_roundtrips_and_merges_classes(tmp_path):
    vector = tmp_path / "segments.shp"
    previous_values = np.asarray([3], dtype=np.int64)
    previous_counts = np.asarray([[40], [0]], dtype=np.int64)
    write_count_state(vector, previous_values, previous_counts)

    loaded = read_count_state(vector, feature_count=2)
    values, counts = merge_counts(
        loaded,
        np.asarray([7], dtype=np.int64),
        np.asarray([[60], [80]], dtype=np.int64),
    )

    assert state_path(vector).is_file()
    np.testing.assert_array_equal(values, [3, 7])
    np.testing.assert_array_equal(counts, [[40, 60], [0, 80]])


def test_count_state_rejects_changed_feature_count(tmp_path):
    vector = tmp_path / "segments.shp"
    write_count_state(vector, np.asarray([3]), np.asarray([[10]], dtype=np.int64))

    try:
        read_count_state(vector, feature_count=2)
    except ValueError as error:
        assert "feature count" in str(error)
    else:
        raise AssertionError("stale assignment state should be rejected")
