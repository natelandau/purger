"""Tests for report helpers."""

import pytest

from purger.reports import sum_known


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], None),
        ([None, None], None),
        ([1, None, 2], 3),
        ([0], 0),
    ],
)
def test_sum_known_ignores_unknown_sizes(values: list[int | None], expected: int | None) -> None:
    """Verify sum_known adds known sizes and returns None when none is known."""
    # Given a mix of known and unknown sizes
    # When summing them
    result = sum_known(values)

    # Then only the known sizes count
    assert result == expected
