"""Tests for the pacman plugin."""

import pytest

from purger.plugins import pacman


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "==> finished dry run: 3 candidates (disk space saved: 120.5 MiB)\n",
            int(120.5 * 1024**2),
        ),
        ("==> finished dry run: 1 candidates (disk space saved: 812 KiB)\n", 812 * 1024),
        ("==> no candidate packages found for pruning\n", 0),
    ],
)
def test_pacman_reads_dry_run_savings(output: str, expected: int) -> None:
    """Verify the paccache dry-run summary converts to bytes, and no candidates means zero."""
    # Given / When / Then
    assert pacman._saved_bytes(output) == expected
