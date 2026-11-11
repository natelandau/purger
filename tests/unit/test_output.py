"""Tests for table and JSON output."""

import json
import sys
from pathlib import Path

import pytest
from nclutils.strings import human_size
from rich.console import Console, RenderableType
from rich.table import Table

from purger.details import default_details
from purger.output import (
    SCHEMA_VERSION,
    action_summary,
    clean_document,
    error_document,
    grand_total,
    info_document,
    is_partial,
    render_plugin_details,
    render_results,
    render_table,
    render_total,
)
from purger.plugin import (
    DeletePaths,
    RiskLevel,
    RunCommand,
    SizeBreakdownRow,
)
from purger.reports import ActionReport, PluginReport, SkippedPlugin, SkipReason, Status
from tests.conftest import EMPTY_SETTINGS, make_action, make_plugin, table_row


def _reports() -> list[PluginReport]:
    known = ActionReport(
        action=make_action(
            name="prune",
            risk_level=RiskLevel.SAFE,
            description="Prune [cache]",
            cleanup=RunCommand("x", "p"),
        ),
        size_bytes=2048,
        resolved_paths=[Path("/home/u/.cache/x")],
        status=Status.DONE,
    )
    unknown = ActionReport(
        action=make_action(name="wipe", risk_level=RiskLevel.DANGEROUS, cleanup=DeletePaths("~/w")),
        size_bytes=None,
        resolved_paths=[],
        status=Status.FAILED,
        error="boom",
    )
    return [
        PluginReport(
            settings=EMPTY_SETTINGS,
            plugin=make_plugin(name="alpha", description="Alpha tool"),
            total_size_bytes=2048,
            actions=[known, unknown],
            freed_bytes=2048,
        ),
        PluginReport(
            settings=EMPTY_SETTINGS,
            plugin=make_plugin(name="beta", description="Beta tool"),
            total_size_bytes=None,
            actions=[],
        ),
    ]


def _text(table: Table) -> str:
    console = Console(record=True, width=120)
    console.print(table)
    return console.export_text()


def _sized(name: str, total_size: int | None, *sizes: int | None) -> PluginReport:
    """Build a plugin report whose actions have the given sizes."""
    actions = [
        ActionReport(action=make_action(name=f"act{i}"), size_bytes=size, resolved_paths=[])
        for i, size in enumerate(sizes)
    ]
    return PluginReport(
        settings=EMPTY_SETTINGS,
        plugin=make_plugin(name=name, description=f"{name} tool"),
        total_size_bytes=total_size,
        actions=actions,
    )


def test_grand_total_sums_known_and_none_when_all_unknown() -> None:
    """Verify grand_total sums known total sizes and is None when none are known."""
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
        "details",
    }
    assert action["details"] == []
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


def test_grand_total_sums_several_known_total_sizes() -> None:
    """Verify grand_total adds every known total size and ignores unknown ones."""
    # Given three plugins, two with known total sizes
    reports = [
        PluginReport(
            settings=EMPTY_SETTINGS,
            plugin=make_plugin(name=name),
            total_size_bytes=size,
            actions=[],
        )
        for name, size in (("a", 100), ("b", None), ("c", 23))
    ]

    # When / Then
    assert grand_total(reports) == 123


def test_clean_document_sums_several_freed() -> None:
    """Verify total_freed_bytes adds every known freed size and ignores unknown ones."""
    # Given
    reports = [
        PluginReport(
            settings=EMPTY_SETTINGS,
            plugin=make_plugin(name=name),
            total_size_bytes=1,
            actions=[],
            freed_bytes=freed,
        )
        for name, freed in (("a", 5), ("b", None), ("c", 7))
    ]

    # When
    doc = clean_document(reports, [])

    # Then
    assert doc["total_freed_bytes"] == 12


@pytest.mark.parametrize(
    ("total_size", "sizes", "expected"),
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
        "known-covers-total-size",
        "unknown-part-remains",
        "only-unknown",
        "unknown-total-size-and-action",
        "unknown-total-size-known-action",
        "no-included-actions",
        "empty-total-size",
    ],
)
def test_is_partial_rules(
    total_size: int | None, sizes: tuple[int | None, ...], expected: bool
) -> None:
    """Verify a tool is partial unless its included actions account for the whole total size."""
    # Given
    report = _sized("t", total_size, *sizes)

    # When / Then
    assert is_partial(report) is expected


def test_is_partial_empty_total_size_with_unknown_outside_action() -> None:
    """Verify a zero total size stays partial when an action that adds to the total size has no size."""
    # Given
    report = PluginReport(
        settings=EMPTY_SETTINGS,
        plugin=make_plugin(name="t", description="t tool"),
        total_size_bytes=0,
        actions=[
            ActionReport(
                action=make_action(name="outside", adds_to_plugin_total_size=True),
                size_bytes=None,
                resolved_paths=[],
            )
        ],
    )

    # When / Then
    assert is_partial(report) is True


def test_render_table_one_row_per_tool_sorted_by_name() -> None:
    """Verify the table shows one row per tool, sorted by name, with no action names or risk levels."""
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
        settings=EMPTY_SETTINGS,
        plugin=make_plugin(name=name, description=f"{name} tool"),
        total_size_bytes=before,
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


def _render(renderable: RenderableType | None, width: int = 120) -> str:
    """Render to plain text at a fixed width."""
    assert renderable is not None
    console = Console(record=True, width=width)
    console.print(renderable)
    return console.export_text()


def _path_report(tmp_path: Path, sizes: dict[str, int], **overrides: object) -> ActionReport:
    """Build a measured path action whose size breakdown rows are one per path."""
    action = make_action(cleanup=DeletePaths(str(tmp_path / "*")), **overrides)
    rows = [
        SizeBreakdownRow(label=name, size_bytes=size, paths=(tmp_path / name,))
        for name, size in sizes.items()
    ]
    return ActionReport(
        action=action,
        size_bytes=sum(sizes.values()),
        resolved_paths=[tmp_path / name for name in sizes],
        details=rows,
    )


def test_plugin_details_without_rows_still_shows_header() -> None:
    """Verify a plugin with nothing to list keeps its name, size, and description."""
    # Given
    report = PluginReport(
        settings=EMPTY_SETTINGS,
        plugin=make_plugin(name="brew", description="Homebrew downloads"),
        total_size_bytes=2048,
        actions=[ActionReport(action=make_action(), size_bytes=2048, resolved_paths=[])],
    )

    # When
    text = _render(render_plugin_details(report, verbosity=1))

    # Then
    assert "brew" in text
    assert human_size(2048) in text
    assert "Homebrew downloads" in text


def test_plugin_details_marks_partial_purge() -> None:
    """Verify the header carries the mark the table's Partial purge column showed."""
    # Given an action with an unknown size under a known total size
    report = PluginReport(
        settings=EMPTY_SETTINGS,
        plugin=make_plugin(),
        total_size_bytes=2048,
        actions=[ActionReport(action=make_action(), size_bytes=None, resolved_paths=[])],
    )

    # When
    text = _render(render_plugin_details(report, verbosity=1))

    # Then
    assert "partial purge" in text


def test_plugin_details_by_action_groups_rows_under_each_action() -> None:
    """Verify clean's sections put each action's rows under its name and risk level."""
    # Given two actions with rows, one of them empty
    full = ActionReport(
        action=make_action(name="deps", risk_level=RiskLevel.CAUTION),
        size_bytes=5,
        resolved_paths=[],
        details=[SizeBreakdownRow(label="row-of-deps", size_bytes=5)],
    )
    empty = ActionReport(
        action=make_action(name="caches"),
        size_bytes=0,
        resolved_paths=[],
        details=[SizeBreakdownRow(label="row-of-caches", size_bytes=0)],
    )
    report = PluginReport(
        settings=EMPTY_SETTINGS, plugin=make_plugin(), total_size_bytes=5, actions=[empty, full]
    )

    # When
    text = _render(render_plugin_details(report, verbosity=1, by_action=True))

    # Then
    assert "deps (caution)" in text
    assert "row-of-deps" in text
    assert "row-of-caches" not in text


def test_plugin_details_by_action_lists_actions_without_rows() -> None:
    """Verify clean's sections name a command action that has a size but no rows."""
    # Given
    command = ActionReport(
        action=make_action(name="prune", risk_level=RiskLevel.CAUTION),
        size_bytes=600,
        resolved_paths=[],
    )
    report = PluginReport(
        settings=EMPTY_SETTINGS, plugin=make_plugin(), total_size_bytes=600, actions=[command]
    )

    # When
    text = _render(render_plugin_details(report, verbosity=1, by_action=True))

    # Then
    assert "prune (caution)" in text
    assert human_size(600) in text


def test_render_total_sums_known_total_sizes() -> None:
    """Verify the closing total line matches the table's total."""
    # Given / When
    text = _render(render_total(_reports()))

    # Then
    assert "Total" in text
    assert human_size(2048) in text


def test_plugin_details_header_uses_total_size_and_not_cleaned_line(tmp_path: Path) -> None:
    """Verify rows that fall short of the total size end with a not cleaned line."""
    # Given a total size larger than its one path action
    action = _path_report(tmp_path, {"a": 400})
    report = PluginReport(
        settings=EMPTY_SETTINGS,
        plugin=make_plugin(name="ruby"),
        total_size_bytes=1000,
        actions=[action],
    )

    # When
    text = _render(render_plugin_details(report, verbosity=1))

    # Then
    assert "ruby" in text
    assert human_size(1000) in text
    assert "not cleaned" in text
    assert human_size(600) in text


def test_plugin_details_not_listed_when_command_action_has_size(tmp_path: Path) -> None:
    """Verify a gap from a command action that cleans data says not listed."""
    # Given one listed path action and one command action with a size and no rows
    listed = _path_report(tmp_path, {"a": 400})
    command = ActionReport(action=make_action(name="b"), size_bytes=600, resolved_paths=[])
    report = PluginReport(
        settings=EMPTY_SETTINGS,
        plugin=make_plugin(),
        total_size_bytes=1000,
        actions=[listed, command],
    )

    # When
    text = _render(render_plugin_details(report, verbosity=1))

    # Then
    assert "not listed" in text
    assert "not cleaned" not in text


def test_plugin_details_lists_every_row_at_v(tmp_path: Path) -> None:
    """Verify -v lists every row, however many there are."""
    # Given 17 rows
    action = _path_report(tmp_path, {f"r{i:02}": 100 - i for i in range(17)})
    report = PluginReport(
        settings=EMPTY_SETTINGS,
        plugin=make_plugin(),
        total_size_bytes=action.size_bytes,
        actions=[action],
    )

    # When
    text = _render(render_plugin_details(report, verbosity=1))

    # Then
    assert all(f"r{i:02}" in text for i in range(17))
    assert "more" not in text


def test_plugin_details_vv_shows_full_paths(tmp_path: Path) -> None:
    """Verify -vv lists each row's paths."""
    # Given
    action = _path_report(tmp_path, {"a": 1})
    report = PluginReport(
        settings=EMPTY_SETTINGS, plugin=make_plugin(), total_size_bytes=1, actions=[action]
    )

    # When
    text = _render(render_plugin_details(report, verbosity=2), width=400)

    # Then
    assert str(tmp_path / "a") in text


def test_action_summary_counts_items_and_size(tmp_path: Path) -> None:
    """Verify a path action summarizes as an item count and size."""
    # Given
    report = _path_report(tmp_path, {"a": 1024, "b": 1024})

    # When / Then
    assert action_summary(report) == f"would delete 2 items, {human_size(2048)}"


def test_action_summary_command_with_sudo() -> None:
    """Verify a command action summarizes as the command it runs."""
    # Given
    report = ActionReport(
        action=make_action(cleanup=RunCommand("tool", "prune", with_sudo=True)),
        size_bytes=None,
        resolved_paths=[],
    )

    # When / Then
    assert action_summary(report) == "would run sudo tool prune"


def test_size_breakdown_rows_stay_indented_when_labels_wrap(tmp_path: Path) -> None:
    """Verify a long label or path never wraps back to the left margin."""
    # Given a long label with a long path, rendered at 80 columns with paths
    long = "~/Library/Developer/Xcode/iOS DeviceSupport/iPhone18,1 27.0.1 (24A446) and more"
    row = SizeBreakdownRow(label=long, size_bytes=7, paths=(tmp_path / ("deep" * 30),))
    action = ActionReport(action=make_action(), size_bytes=7, resolved_paths=[], details=[row])
    report = PluginReport(
        settings=EMPTY_SETTINGS, plugin=make_plugin(), total_size_bytes=7, actions=[action]
    )

    # When
    text = _render(render_plugin_details(report, verbosity=2), width=80)

    # Then the header is the only line at the margin
    lines = [line for line in text.splitlines() if line.strip()]
    assert all(line.startswith("  ") for line in lines[1:])
    assert "(24A446)" in text


def test_default_row_fits_one_line_at_80_columns(tmp_path: Path) -> None:
    """Verify a deep default path renders as one short row, not a wrapped full path."""
    # Given a default row for an item deep in the home folder
    item = (
        tmp_path
        / "Library"
        / "Developer"
        / "Xcode"
        / "iOS DeviceSupport"
        / "iPhone18,1 27.0.1 (24A446)"
    )
    rows = default_details({item: 7_100_000_000})
    action = ActionReport(
        action=make_action(), size_bytes=7_100_000_000, resolved_paths=[item], details=rows
    )
    report = PluginReport(
        settings=EMPTY_SETTINGS,
        plugin=make_plugin(name="xcode"),
        total_size_bytes=7_100_000_000,
        actions=[action],
    )

    # When
    text = _render(render_plugin_details(report, verbosity=1), width=80)

    # Then the row is one line hanging from the header, with its name, size, and folder
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 2
    assert "\u2514\u2500 iPhone18,1 27.0.1 (24A446)" in lines[1]
    assert human_size(7_100_000_000) in lines[1]
    assert "iOS DeviceSupport" in lines[1]


_LONG_NAME = "https___github_com_getsentry_sentry_cocoa_releases_download_9_33_0_Sentry_Dynamic_xcframework_zip"


def _long_label_report() -> PluginReport:
    """Build a plugin whose one row has a label far longer than a line's label column."""
    row = SizeBreakdownRow(label=_LONG_NAME, size_bytes=5)
    action = ActionReport(action=make_action(), size_bytes=5, resolved_paths=[], details=[row])
    return PluginReport(
        settings=EMPTY_SETTINGS, plugin=make_plugin(), total_size_bytes=5, actions=[action]
    )


def test_long_label_shortened_in_middle_at_v() -> None:
    """Verify -v keeps both ends of a long label and drops its middle."""
    # Given / When
    text = _render(render_plugin_details(_long_label_report(), verbosity=1), width=80)

    # Then the row is one line holding the start, an ellipsis, and the end
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 2
    label = lines[1].split()[1]
    assert len(label) == 40
    assert label.startswith("https___github")
    assert label.endswith("xcframework_zip")
    assert "…" in label


def test_long_label_shown_in_full_at_vv() -> None:
    """Verify -vv shows the whole label."""
    # Given / When
    text = _render(render_plugin_details(_long_label_report(), verbosity=2), width=400)

    # Then the label is all there, though a label this long folds across lines
    assert _LONG_NAME in "".join(text.split())


def test_info_document_actions_carry_details(tmp_path: Path) -> None:
    """Verify each size breakdown row reaches the JSON document with its fields."""
    # Given an action with one row
    row = SizeBreakdownRow(
        label="proj", size_bytes=7, item_names=(".venv",), paths=(tmp_path / "proj",)
    )
    action = ActionReport(action=make_action(), size_bytes=7, resolved_paths=[], details=[row])
    report = PluginReport(
        settings=EMPTY_SETTINGS, plugin=make_plugin(), total_size_bytes=7, actions=[action]
    )

    # When
    doc = info_document(reports=[report], skipped=[])

    # Then
    assert doc["plugins"][0]["actions"][0]["details"] == [
        {"label": "proj", "size_bytes": 7, "items": [".venv"], "paths": [str(tmp_path / "proj")]}
    ]


def _inset_report(paths: dict[str, tuple[Path, ...]]) -> PluginReport:
    """Build a plugin whose rows each cover the given paths."""
    rows = [
        SizeBreakdownRow(label=label, size_bytes=10, paths=row_paths)
        for label, row_paths in paths.items()
    ]
    action = ActionReport(
        action=make_action(), size_bytes=10 * len(rows), resolved_paths=[], details=rows
    )
    return PluginReport(
        settings=EMPTY_SETTINGS,
        plugin=make_plugin(),
        total_size_bytes=action.size_bytes,
        actions=[action],
    )


def test_v_rows_hang_from_header_without_paths(tmp_path: Path) -> None:
    """Verify -v draws each row as a branch of the plugin header and lists no paths."""
    # Given two rows that each cover a path
    report = _inset_report({"first": (tmp_path / "a",), "second": (tmp_path / "b",)})

    # When
    text = _render(render_plugin_details(report, verbosity=1), width=200)

    # Then the first row takes a branch, the last a corner, and no path shows
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 3
    assert "\u251c\u2500 first" in lines[1]
    assert "\u2514\u2500 second" in lines[2]
    assert str(tmp_path) not in text


def test_vv_nests_absolute_paths_under_each_row(tmp_path: Path) -> None:
    """Verify -vv hangs each row's full paths from that row, below the row's branch."""
    # Given two rows, so the first row's paths sit beside a continuing branch
    report = _inset_report({"first": (tmp_path / "a", tmp_path / "b"), "second": (tmp_path / "c",)})

    # When
    text = _render(render_plugin_details(report, verbosity=2), width=400)

    # Then the first row's paths keep the header's line running beside them
    lines = [line for line in text.splitlines() if line.strip()]
    assert "\u251c\u2500 first" in lines[1]
    assert lines[2].startswith("  \u2502  \u251c\u2500 ")
    assert str(tmp_path / "a") in lines[2]
    assert lines[3].startswith("  \u2502  \u2514\u2500 ")
    assert "\u2514\u2500 second" in lines[4]
    assert lines[5].startswith("     \u2514\u2500 ")
    assert str(tmp_path / "c") in lines[5]


def test_inset_rows_keep_their_columns_aligned(tmp_path: Path) -> None:
    """Verify sizes line up across rows even with inset paths between them."""
    # Given two rows of different label lengths, each with a path
    report = _inset_report({"a": (tmp_path / "a",), "much-longer-label": (tmp_path / "b",)})

    # When
    text = _render(render_plugin_details(report, verbosity=1), width=200)

    # Then both size cells end in the same column
    size = human_size(10)
    row_lines = [line for line in text.splitlines() if size in line]
    assert len(row_lines) == 2
    assert row_lines[0].index(size) == row_lines[1].index(size)


def test_vv_long_label_never_cuts_the_size_at_80_columns() -> None:
    """Verify a full label wider than the terminal folds instead of cutting the size."""
    # Given a long label with a wide size and an item
    row = SizeBreakdownRow(label=_LONG_NAME, size_bytes=187_100_000, item_names=("artifacts",))
    action = ActionReport(
        action=make_action(), size_bytes=187_100_000, resolved_paths=[], details=[row]
    )
    report = PluginReport(
        settings=EMPTY_SETTINGS,
        plugin=make_plugin(),
        total_size_bytes=187_100_000,
        actions=[action],
    )

    # When
    text = _render(render_plugin_details(report, verbosity=2), width=80)

    # Then the size and item show whole, with no ellipsis anywhere
    assert human_size(187_100_000) in text
    assert "artifacts" in text
    assert "…" not in text
