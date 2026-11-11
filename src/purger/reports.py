"""Report types describing what purger measured and did."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from purger.plugin import Action, Plugin, PluginSettings, SizeBreakdownRow


class Status(StrEnum):
    """Outcome of one action."""

    DONE = "done"
    FAILED = "failed"
    DECLINED = "declined"
    SKIPPED = "skipped"
    NOTHING_TO_CLEAN = "nothing_to_clean"
    PLANNED = "planned"


class SkipReason(StrEnum):
    """Why a plugin was left out of a run."""

    PLATFORM = "platform"
    MISSING_BINARY = "missing_binary"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"


@dataclass
class ActionReport:
    """Measured size and outcome of one action."""

    action: Action
    size_bytes: int | None
    resolved_paths: list[Path]
    status: Status | None = None
    error: str | None = None
    details: list[SizeBreakdownRow] = field(default_factory=list)


@dataclass(kw_only=True)
class PluginReport:
    """Measured total size of one plugin and the reports of its actions.

    `settings` holds the plugin's settings for this run, so re-measuring after
    a cleanup sizes the plugin the same way.
    """

    plugin: Plugin
    total_size_bytes: int | None
    actions: list[ActionReport]
    settings: PluginSettings
    freed_bytes: int | None = None
    after_bytes: int | None = None


@dataclass(frozen=True)
class SkippedPlugin:
    """A plugin that was not run, with the reason."""

    name: str
    reason: SkipReason


def sum_known(values: Iterable[int | None]) -> int | None:
    """Sum the known sizes so a report can show a total despite unmeasured parts.

    Args:
        values: Sizes in bytes, with None for unknown.

    Returns:
        int | None: The sum of known sizes, or None when none is known.
    """
    known = [value for value in values if value is not None]
    return sum(known) if known else None
