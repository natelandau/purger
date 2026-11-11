"""Tests for building detail rows."""

from collections.abc import Sequence
from pathlib import Path
from typing import cast

from pytest_mock import MockerFixture

from purger.config import Config, PluginSettings
from purger.details import action_details, default_details, merge, tilde
from purger.plugin import Detail, PathSizes
from tests.conftest import make_action, make_plugin


def _settings() -> PluginSettings:
    return Config().settings_for(make_plugin())


def test_default_details_label_by_name_with_parent_folder(tmp_path: Path) -> None:
    """Verify each path becomes a row named after itself, with its folder as the item."""
    # Given a path inside a long, shared folder
    item = tmp_path / "iOS DeviceSupport" / "iPhone18,1 27.0.1 (24A446)"

    # When
    rows = default_details({item: 10})

    # Then
    assert rows == [
        Detail(
            label="iPhone18,1 27.0.1 (24A446)",
            size_bytes=10,
            items=("iOS DeviceSupport",),
            paths=(item,),
            key=str(item),
        )
    ]


def test_default_details_keep_same_names_in_different_folders_apart(tmp_path: Path) -> None:
    """Verify two items with one name in different folders stay two rows after merging."""
    # Given
    sizes = {tmp_path / "x" / "cache": 1, tmp_path / "y" / "cache": 2}

    # When
    rows = merge(default_details(sizes))

    # Then
    assert [(row.label, row.items) for row in rows] == [("cache", ("y",)), ("cache", ("x",))]


def test_action_details_uses_hook(tmp_path: Path) -> None:
    """Verify an action's own hook describes its rows."""

    # Given an action with a hook
    def hook(sizes: PathSizes, settings: PluginSettings) -> Sequence[Detail]:
        return [Detail(label="mine", size_bytes=sum(sizes.values()))]

    action = make_action(details=hook)

    # When
    rows = action_details(action=action, sizes={tmp_path / "a": 5}, settings=_settings())

    # Then
    assert rows == [Detail(label="mine", size_bytes=5)]


def test_action_details_falls_back_when_hook_raises(tmp_path: Path) -> None:
    """Verify a failing hook falls back to one row per path."""

    # Given a hook that raises
    def hook(sizes: PathSizes, settings: PluginSettings) -> Sequence[Detail]:
        msg = "boom"
        raise RuntimeError(msg)

    action = make_action(details=hook)
    sizes = {tmp_path / "a": 5}

    # When
    rows = action_details(action=action, sizes=sizes, settings=_settings())

    # Then
    assert rows == default_details(sizes)


def test_action_details_sorts_largest_first(tmp_path: Path) -> None:
    """Verify rows come back largest first, unknown sizes last."""

    # Given a hook returning rows out of order
    def hook(sizes: PathSizes, settings: PluginSettings) -> Sequence[Detail]:
        return [
            Detail(label="unknown", size_bytes=None),
            Detail(label="small", size_bytes=1),
            Detail(label="big", size_bytes=9),
        ]

    # When
    rows = action_details(action=make_action(details=hook), sizes={}, settings=_settings())

    # Then
    assert [row.label for row in rows] == ["big", "small", "unknown"]


def test_merge_combines_same_key_and_sorts_largest_first(tmp_path: Path) -> None:
    """Verify rows for the same project from two actions become one row."""
    # Given rows from two actions, two of them for one project
    rows = [
        Detail(label="app", size_bytes=10, items=("a",), paths=(tmp_path / "a",), key="k"),
        Detail(label="other", size_bytes=15),
        Detail(label="app", size_bytes=20, items=("b", "a"), paths=(tmp_path / "b",), key="k"),
    ]

    # When
    merged = merge(rows)

    # Then
    assert merged == [
        Detail(
            label="app",
            size_bytes=30,
            items=("a", "b"),
            paths=(tmp_path / "a", tmp_path / "b"),
            key="k",
        ),
        Detail(label="other", size_bytes=15),
    ]


def test_merge_unknown_size_sorts_last() -> None:
    """Verify rows with an unknown size go after measured rows."""
    # Given / When
    merged = merge([Detail(label="a", size_bytes=None), Detail(label="b", size_bytes=1)])

    # Then
    assert [row.label for row in merged] == ["b", "a"]


def test_action_details_falls_back_when_hook_returns_junk(tmp_path: Path) -> None:
    """Verify a hook that returns something other than rows cannot crash the run."""

    # Given a hook returning strings
    def hook(sizes: PathSizes, settings: PluginSettings) -> Sequence[Detail]:
        return cast("Sequence[Detail]", ["not a row", "another"])

    sizes = {tmp_path / "a": 5}

    # When
    rows = action_details(action=make_action(details=hook), sizes=sizes, settings=_settings())

    # Then
    assert rows == default_details(sizes)


def test_tilde_handles_symlinked_home(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify a path under the resolved home still shows as ~ when home is a symlink."""
    # Given a home folder reached through a symlink
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    mocker.patch("purger.details.invoking_home", return_value=link)

    # When / Then
    assert tilde(real / "proj") == "~/proj"
