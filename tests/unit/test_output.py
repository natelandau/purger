"""Tests for table and JSON output."""

import json
import sys
from pathlib import Path

import pytest
from nclutils.strings import human_size
from rich.console import Console

from purger.output import (
    SCHEMA_VERSION,
    clean_document,
    error_document,
    grand_total,
    info_document,
    render_table,
)
from purger.plugin import Tier
from purger.reports import ActionReport, PluginReport, SkippedPlugin, SkipReason, Status
from tests.conftest import make_action, make_plugin


def _reports() -> list[PluginReport]:
    known = ActionReport(
        action=make_action(name="prune", tier=Tier.SAFE, help="Prune [cache]", command=("x", "p")),
        size_bytes=2048,
        resolved_paths=[Path("/home/u/.cache/x")],
        status=Status.DONE,
    )
    unknown = ActionReport(
        action=make_action(name="wipe", tier=Tier.DANGEROUS, command=None, paths=("~/w",)),
        size_bytes=None,
        resolved_paths=[],
        status=Status.FAILED,
        error="boom",
    )
    return [
        PluginReport(
            plugin=make_plugin(name="alpha", description="Alpha tool"),
            footprint_bytes=2048,
            actions=[known, unknown],
            freed_bytes=2048,
        ),
        PluginReport(
            plugin=make_plugin(name="beta", description="Beta tool"),
            footprint_bytes=None,
            actions=[],
        ),
    ]


def _render(*, show_status: bool = False) -> str:
    console = Console(record=True, width=120)
    console.print(render_table(_reports(), show_status=show_status))
    return console.export_text()


def test_grand_total_sums_known_and_none_when_all_unknown() -> None:
    """Verify grand_total sums known footprints and is None when none are known."""
    # Given
    reports = _reports()

    # When / Then
    assert grand_total(reports) == 2048
    assert grand_total([reports[1]]) is None
    assert grand_total([]) is None


def test_info_document_shape() -> None:
    """Verify info_document exposes the stable public shape and is JSON serializable."""
    # Given
    skipped = [SkippedPlugin(name="gamma", reason=SkipReason.PLATFORM)]

    # When
    doc = info_document(_reports(), skipped)

    # Then
    assert list(doc) == ["schema_version", "platform", "total_bytes", "plugins", "skipped"]
    assert doc["schema_version"] == SCHEMA_VERSION == 1
    assert doc["platform"] == sys.platform
    assert doc["total_bytes"] == 2048
    assert doc["skipped"] == [{"name": "gamma", "reason": "platform"}]
    plugin = doc["plugins"][0]
    assert set(plugin) == {"name", "description", "footprint_bytes", "actions"}
    action = plugin["actions"][0]
    assert set(action) == {
        "name",
        "tier",
        "help",
        "size_bytes",
        "command",
        "paths",
        "needs_sudo",
    }
    assert action["tier"] == "safe"
    assert action["command"] == ["x", "p"]
    assert action["paths"] == ["/home/u/.cache/x"]
    assert plugin["actions"][1]["command"] is None
    assert plugin["actions"][1]["size_bytes"] is None
    json.dumps(doc)


def test_clean_document_adds_results() -> None:
    """Verify clean_document adds status, error, and freed totals to the info shape."""
    # Given
    reports = _reports()

    # When
    doc = clean_document(reports, [])

    # Then
    assert doc["total_freed_bytes"] == 2048
    assert doc["plugins"][0]["freed_bytes"] == 2048
    assert doc["plugins"][1]["freed_bytes"] is None
    first, second = doc["plugins"][0]["actions"]
    assert (first["status"], first["error"]) == ("done", None)
    assert (second["status"], second["error"]) == ("failed", "boom")
    json.dumps(doc)


def test_clean_document_unknown_freed_and_status_is_null() -> None:
    """Verify total_freed_bytes and status are null when nothing is known."""
    # Given
    reports = _reports()
    reports[0].freed_bytes = None
    reports[0].actions[0].status = None

    # When
    doc = clean_document(reports, [])

    # Then
    assert doc["total_freed_bytes"] is None
    assert doc["plugins"][0]["actions"][0]["status"] is None


def test_error_document() -> None:
    """Verify error_document wraps the code and message with the schema version."""
    # Given / When building a document from an error code and message
    doc = error_document("unknown_plugin", "no such plugin")

    # Then
    assert doc == {
        "schema_version": 1,
        "error": {"code": "unknown_plugin", "message": "no such plugin"},
    }


def test_render_table_rows() -> None:
    """Verify the table lists plugins, actions, tiers, sizes, and the total footer."""
    # Given / When rendering a plugin with known and unknown actions, and an empty plugin
    text = _render()

    # Then
    for expected in ("alpha", "Alpha tool", "beta", "prune", "wipe", "safe", "dangerous"):
        assert expected in text
    assert human_size(2048) in text
    assert "Prune [cache]" in text
    assert "Found in caches" in text
    assert "-" in text
    assert "Status" not in text


def test_render_table_status_column() -> None:
    """Verify show_status adds a Status column with the action outcomes."""
    # Given / When rendering a plugin with known and unknown actions, and an empty plugin
    text = _render(show_status=True)

    # Then
    assert "Status" in text
    assert "done" in text
    assert "failed" in text


def test_grand_total_sums_several_known_footprints() -> None:
    """Verify grand_total adds every known footprint and ignores unknown ones."""
    # Given three plugins, two with known footprints
    reports = [
        PluginReport(plugin=make_plugin(name=name), footprint_bytes=size, actions=[])
        for name, size in (("a", 100), ("b", None), ("c", 23))
    ]

    # When / Then
    assert grand_total(reports) == 123


def test_clean_document_sums_several_freed() -> None:
    """Verify total_freed_bytes adds every known freed size and ignores unknown ones."""
    # Given
    reports = [
        PluginReport(
            plugin=make_plugin(name=name), footprint_bytes=1, actions=[], freed_bytes=freed
        )
        for name, freed in (("a", 5), ("b", None), ("c", 7))
    ]

    # When
    doc = clean_document(reports, [])

    # Then
    assert doc["total_freed_bytes"] == 12


def test_render_table_unknown_size_shows_dash() -> None:
    """Verify an action of unknown size shows `-` in its Size cell."""
    # Given
    console = Console(record=True, width=120)

    # When
    console.print(render_table(_reports()))

    # Then the wipe row's size column is a dash
    wipe_row = next(line for line in console.export_text().splitlines() if "wipe" in line)
    cells = [cell.strip() for cell in wipe_row.split("\u2502")]
    assert cells[3] == "-"


@pytest.mark.parametrize(
    ("tier", "color"),
    [(Tier.SAFE, "green"), (Tier.CAUTION, "yellow"), (Tier.DANGEROUS, "red")],
)
def test_render_table_colors_tiers(tier: Tier, color: str) -> None:
    """Verify each tier is drawn in its own color."""
    # Given
    action = ActionReport(action=make_action(name="x", tier=tier), size_bytes=1, resolved_paths=[])
    report = PluginReport(
        plugin=make_plugin(actions=(action.action,)), footprint_bytes=1, actions=[action]
    )
    console = Console(record=True, width=120, force_terminal=True, color_system="standard")

    # When
    console.print(render_table([report]))
    text = console.export_text(styles=True)

    # Then the tier label carries the tier's ANSI color
    ansi = {"green": "32", "yellow": "33", "red": "31"}[color]
    assert f"\x1b[{ansi}m{tier.value}\x1b[0m" in text
