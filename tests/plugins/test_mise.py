"""Tests for the mise plugin."""

from pathlib import Path

from purger.plugins import mise


def test_mise_prunable_install_paths() -> None:
    """Verify every prunable version's install path is read from the JSON."""
    # Given
    output = (
        '{"node": [{"version": "20.0.0", "install_path": "/m/node/20.0.0"},'
        ' {"version": "21.0.0", "install_path": "/m/node/21.0.0"}],'
        ' "go": [{"version": "1.0"}]}'
    )

    # When / Then
    assert mise._prunable_install_paths(output) == [
        Path("/m/node/20.0.0"),
        Path("/m/node/21.0.0"),
    ]


def test_mise_prunable_install_paths_empty() -> None:
    """Verify nothing prunable measures nothing."""
    # Given / When / Then
    assert mise._prunable_install_paths("{}") == []


def test_mise_prune_counts_outside_footprint() -> None:
    """Verify pruned tool versions add to the footprint, since the mise cache excludes them."""
    # Given / When
    flags = {a.name: a.outside_footprint for a in mise.PLUGIN.actions}

    # Then
    assert flags == {"cache-prune": False, "prune": True, "cache-clear": False}
