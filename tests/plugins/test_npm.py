"""Tests for the npm plugin."""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from purger.output import is_partial
from purger.plugins import npm
from purger.plugins.npm import _explain_npx_failure
from purger.sizing import disk_usage, measure_plugin
from tests.conftest import make_completed


def test_npm_footprint_counts_only_cleanable_parts(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify the npm footprint leaves out logs, so its two cleanups cover all of it."""
    # Given an npm cache with the package cache, the npx cache, and logs
    for part, size in (("_cacache", 10_000), ("_npx", 10_000), ("_logs", 50_000)):
        (tmp_path / part).mkdir()
        (tmp_path / part / "f").write_bytes(b"x" * size)
    mocker.patch(
        "purger.sizing.run_command",
        autospec=True,
        return_value=make_completed(stdout=f"{tmp_path}\n"),
    )

    # When
    report = measure_plugin(npm.PLUGIN, include_dangerous=True)

    # Then
    assert report.footprint_bytes == disk_usage([tmp_path / "_cacache", tmp_path / "_npx"])
    assert is_partial(report) is False


@pytest.mark.parametrize(
    ("stderr", "has_hint"),
    [
        ("npm error code ENOTEMPTY\nnpm error syscall rmdir", True),
        ("npm error code EACCES", False),
    ],
)
def test_explain_npx_failure_hints_only_on_enotempty(stderr: str, *, has_hint: bool) -> None:
    """Verify the npx hint names a concurrent npx run only for a not-empty directory error."""
    # Given / When
    hint = _explain_npx_failure(stderr)

    # Then
    assert (hint is not None) is has_hint
    if hint is not None:
        assert "npx" in hint


_NODE = "/opt/homebrew/bin/node"


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        ([_NODE, "/opt/homebrew/bin/npm", "install"], True),
        (["npm install left-pad", "", "", ""], True),
        ([_NODE, "/opt/homebrew/lib/node_modules/npm/bin/npm-cli.js", "ci"], True),
        ([_NODE, "/opt/homebrew/bin/npx", "ccstatusline@latest"], False),
        (["npm exec @upstash/context7-mcp", "", "", "", ""], False),
        ([_NODE, "/opt/homebrew/bin/npm", "--yes", "x", "cowsay"], False),
        (["/Applications/My App/node", "/opt/homebrew/bin/npm", "ci"], True),
        (["npm", "ci"], True),
        ([_NODE, "/opt/homebrew/bin/npm", "run", "dev"], False),
        (["npm run dev", "", ""], False),
        ([_NODE, "server.js"], False),
    ],
)
def test_npm_busy_process_matches_npm_runs(cmdline: list[str], expected: bool) -> None:
    """Verify npm counts as busy however it was launched, and nothing else does."""
    # Given
    assert npm.PLUGIN.busy_process is not None

    # When / Then
    assert npm.PLUGIN.busy_process(cmdline) is expected
