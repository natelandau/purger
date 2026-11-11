"""Tests for the playwright plugin."""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from purger.config import Config
from purger.plugins import playwright
from purger.sizing import measure_plugin


@pytest.mark.parametrize(
    ("env", "platform", "expected"),
    [
        ({}, "darwin", "~/Library/Caches/ms-playwright"),
        ({}, "linux", "~/.cache/ms-playwright"),
        ({"XDG_CACHE_HOME": "/xdg"}, "linux", "/xdg/ms-playwright"),
        ({"PLAYWRIGHT_BROWSERS_PATH": "/browsers"}, "darwin", "/browsers"),
        ({"PLAYWRIGHT_BROWSERS_PATH": "0"}, "darwin", "~/Library/Caches/ms-playwright"),
    ],
)
def test_playwright_registry_dir(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], platform: str, expected: str
) -> None:
    """Verify the browser cache follows Playwright's platform default and its overrides."""
    # Given
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    # When / Then
    assert playwright._registry_dir(platform) == expected


def test_playwright_old_builds_keep_the_newest_of_each_browser(tmp_path: Path) -> None:
    """Verify only builds older than each browser's newest are picked, and other entries stay."""
    # Given
    names = [
        "chromium-1228",
        "chromium-1254",
        "chromium-999",
        "chromium_headless_shell-1228",
        "chromium_headless_shell-1254",
        "ffmpeg-1011",
        "webkit-2370",
        "webkit-2388",
        ".links",
        "notes",
    ]
    builds = [tmp_path / name for name in names]

    # When
    old = playwright._old_builds(builds)

    # Then
    assert [path.name for path in old] == [
        "chromium-1228",
        "chromium-999",
        "chromium_headless_shell-1228",
        "webkit-2370",
    ]


def test_playwright_without_old_builds_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify an empty browser cache skips the plugin instead of failing."""
    # Given a browser cache holding only the newest build
    (tmp_path / "chromium-1254").mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))

    # When / Then
    assert playwright.PLUGIN.skip_unless is not None
    assert not playwright.PLUGIN.skip_unless()


def test_playwright_expands_another_users_home_in_the_cache_path(
    monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture, tmp_path: Path
) -> None:
    """Verify a `~user` browser path resolves to that user's home instead of staying relative."""
    # Given a browser path in another user's home
    for name in ("chromium-1228", "chromium-1254"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "~bob/browsers")
    expand = mocker.patch.object(Path, "expanduser", return_value=tmp_path)

    # When
    builds = playwright.list_old_browser_builds()

    # Then
    expand.assert_called_once_with()
    assert builds == [tmp_path / "chromium-1228"]


def test_playwright_finds_old_builds_when_measured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify the old builds come from the cache as it is when measured, not at import."""
    # Given a browser cache that gains an old build after import
    for name in ("chromium-1228", "chromium-1254"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "bin").write_bytes(b"x" * 10_000)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert playwright.PLUGIN.skip_unless is not None

    # When
    report = measure_plugin(
        playwright.PLUGIN,
        include_dangerous=False,
        settings=Config().settings_for(playwright.PLUGIN),
    )

    # Then
    assert playwright.PLUGIN.skip_unless()
    assert report.actions[0].resolved_paths == [tmp_path / "chromium-1228"]


@pytest.mark.parametrize("folder_name", ["cache [1]", "cache *", "cache ?"])
def test_playwright_cache_folder_with_glob_characters_matches_literally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, folder_name: str
) -> None:
    """Verify a cache folder whose name holds glob characters is read as that exact folder."""
    # Given a browser cache in a folder named with glob characters, next to a decoy it could match
    cache = tmp_path / folder_name
    for name in ("chromium-1228", "chromium-1254"):
        (cache / name).mkdir(parents=True)
    (tmp_path / "cache 1" / "chromium-1100").mkdir(parents=True)
    (tmp_path / "cache 1" / "chromium-1300").mkdir(parents=True)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))

    # When
    old = playwright.list_old_browser_builds()

    # Then only the named folder's old build is found
    assert old == [cache / "chromium-1228"]
