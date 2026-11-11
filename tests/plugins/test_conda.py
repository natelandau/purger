"""Tests for the conda plugin."""

from purger.plugins import conda


def test_conda_counts_tarballs_and_unused_packages() -> None:
    """Verify the conda size adds tarballs and unused packages, ignoring unsized parts."""
    # Given
    output = (
        '{"success": true, "tarballs": {"total_size": 1000, "pkg_sizes": {}}, '
        '"packages": {"total_size": 2500}, "index_cache": {"files": ["/x"]}, "logfiles": []}'
    )

    # When / Then
    assert conda._reclaimable_bytes(output) == 3500


def test_conda_size_without_package_sections_is_zero() -> None:
    """Verify a report that holds only unsized parts measures as zero."""
    # Given / When / Then
    assert conda._reclaimable_bytes('{"success": true, "logfiles": []}') == 0
