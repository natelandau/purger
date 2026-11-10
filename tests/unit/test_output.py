"""Tests for table and JSON output."""

import json
import sys
from pathlib import Path

import pytest
from nclutils.strings import human_size
from rich.console import Console
from rich.table import Table

from purger.output import (
    SCHEMA_VERSION,
    clean_document,
    error_document,
    grand_total,
    info_document,
    is_partial,
    render_results,
    render_table,
)
from purger.plugin import Tier
from purger.reports import ActionReport, PluginReport, SkippedPlugin, SkipReason, Status
from tests.conftest import make_action, make_plugin, table_row


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


def _text(table: Table) -> str:
    console = Console(record=True, width=120)
    console.print(table)
    return console.export_text()


def _sized(name: str, footprint: int | None, *sizes: int | None) -> PluginReport:
    """Build a plugin report whose actions have the given sizes."""
    actions = [
        ActionReport(action=make_action(name=f"act{i}"), size_bytes=size, resolved_paths=[])
        for i, size in enumerate(sizes)
    ]
    return PluginReport(
        plugin=make_plugin(name=name, description=f"{name} tool"),
        footprint_bytes=footprint,
        actions=actions,
    )


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


@pytest.mark.parametrize(
    ("footprint", "sizes", "expected"),
    [
        (100, (40, 60), False),
        (100, (None, 100), False),
        (100, (None, 60), True),
        (100, (None,), True),
        (None, (None,), True),
        (None, (5,), False),
        (100, (), False),
        (0, (None,), False),
    ],
    ids=[
        "all-known",
        "known-covers-footprint",
        "unknown-part-remains",
        "only-unknown",
        "unknown-footprint-and-action",
        "unknown-footprint-known-action",
        "no-included-actions",
        "empty-footprint",
    ],
)
def test_is_partial_rules(
    footprint: int | None, sizes: tuple[int | None, ...], expected: bool
) -> None:
    """Verify a tool is partial unless its included actions account for the whole footprint."""
    # Given
    report = _sized("t", footprint, *sizes)

    # When / Then
    assert is_partial(report) is expected


def test_is_partial_empty_footprint_with_unknown_outside_action() -> None:
    """Verify a zero footprint stays partial when an outside-footprint action has no size."""
    # Given
    report = PluginReport(
        plugin=make_plugin(name="t", description="t tool"),
        footprint_bytes=0,
        actions=[
            ActionReport(
                action=make_action(name="outside", outside_footprint=True),
                size_bytes=None,
                resolved_paths=[],
            )
        ],
    )

    # When / Then
    assert is_partial(report) is True


def test_render_table_one_row_per_tool_sorted_by_name() -> None:
    """Verify the table shows one row per tool, sorted by name, with no action names or tiers."""
    # Given an exact tool, an unknown-size tool, and a partial tool, out of name order
    reports = [
        _sized("gamma", 4096, None),
        _sized("beta", None, None),
        _sized("alpha", 2048, 2048),
    ]

    # When
    table = render_table(reports)
    text = _text(table)

    # Then
    for header in ("Cache", "Size", "Partial", "Description"):
        assert header in text
    assert text.index("alpha") < text.index("beta") < text.index("gamma")
    assert "act0" not in text
    assert "safe" not in text
    assert table_row(table, "gamma") == ["gamma", human_size(4096), "\u2713", "gamma tool"]
    assert table_row(table, "alpha") == ["alpha", human_size(2048), "", "alpha tool"]
    assert table_row(table, "beta")[1:3] == ["?", "\u2713"]
    assert table_row(table, "Total") == ["Total", human_size(6144), "\u2713", ""]


def test_render_table_total_without_partial_rows() -> None:
    """Verify the Total row has no check when every tool is exact."""
    # Given
    table = render_table([_sized("alpha", 10, 10)])

    # When / Then
    assert table_row(table, "Total")[2] == ""


def test_render_table_escapes_markup() -> None:
    """Verify descriptions are shown literally rather than parsed as rich markup."""
    # Given
    report = _sized("alpha", 1, 1)
    report.plugin = make_plugin(name="alpha", description="Cache [bold]x[/bold]")

    # When / Then
    assert "Cache [bold]x[/bold]" in _text(render_table([report]))


def _ran(name: str, before: int | None, after: int | None, freed: int | None) -> PluginReport:
    """Build a report for a tool that ran, with its before, after, and freed sizes."""
    return PluginReport(
        plugin=make_plugin(name=name, description=f"{name} tool"),
        footprint_bytes=before,
        actions=[],
        freed_bytes=freed,
        after_bytes=after,
    )


def test_render_results_before_after_freed_sorted_by_freed() -> None:
    """Verify the results table shows before, after, and freed per tool, most freed first."""
    # Given
    a = _ran("aaa", 100, 40, 60)
    b = _ran("bbb", 500, 480, 20)
    c = _ran("ccc", None, None, None)

    # When
    table = render_results([b, c, a])
    text = _text(table)

    # Then
    for header in ("Cache", "Before", "After", "Freed"):
        assert header in text
    assert "Description" not in text
    assert "aaa tool" not in text
    assert text.index("aaa") < text.index("bbb") < text.index("ccc")
    assert table_row(table, "aaa") == ["aaa", human_size(100), human_size(40), human_size(60)]
    assert table_row(table, "ccc") == ["ccc", "?", "?", "?"]
    assert table_row(table, "Total") == [
        "Total",
        human_size(600),
        human_size(520),
        human_size(80),
    ]


def test_render_results_interrupted_shows_unknown() -> None:
    """Verify an interrupted re-measure shows unknown after and freed sizes."""
    # Given
    table = render_results([_ran("aaa", 100, None, None)])

    # When / Then
    assert table_row(table, "aaa") == ["aaa", human_size(100), "?", "?"]
    assert table_row(table, "Total") == ["Total", "?", "?", "?"]


def test_render_results_totals_only_rows_with_both_sizes() -> None:
    """Verify totals skip a tool whose after size is unknown, so the Total row stays consistent."""
    # Given one tool that re-measured and one that did not
    table = render_results([_ran("aaa", 10_000, None, None), _ran("bbb", 1_000, 0, 1_000)])

    # When / Then
    assert table_row(table, "Total") == [
        "Total",
        human_size(1_000),
        human_size(0),
        human_size(1_000),
    ]
