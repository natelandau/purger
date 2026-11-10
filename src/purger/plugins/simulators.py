"""Plugin for iOS and other Apple simulators that the installed Xcode cannot run."""

import json
from pathlib import Path

from nclutils.sh import run_command

from purger.plugin import Action, Plugin, Tier
from purger.runner import command_succeeds
from purger.sizing import disk_usage

_LIST_CMD = ["xcrun", "simctl", "list", "devices", "--json"]


def _is_available() -> bool:
    """Report whether Xcode finished its first-launch setup.

    Before that setup, any simctl call makes macOS ask for an administrator
    password to install simulator components. `/usr/bin/xcodebuild` is a shim on
    every Mac that opens an install dialog when no developer tools exist, so
    `xcode-select -p`, which never prompts, runs first.

    Returns:
        bool: True when developer tools are selected and
            `xcodebuild -checkFirstLaunchStatus` exits zero.
    """
    return command_succeeds(["xcode-select", "-p"], timeout=10) and command_succeeds(
        ["xcodebuild", "-checkFirstLaunchStatus"], timeout=10
    )


def _unavailable_devices(listing: str) -> list[Path]:
    """Return the folders of the devices that simctl marks unavailable.

    Args:
        listing: Output of `xcrun simctl list devices --json`.

    Returns:
        list[Path]: Each unavailable device's folder, the parent of its data folder.
    """
    runtimes: dict[str, list[dict[str, object]]] = json.loads(listing)["devices"]
    return [
        Path(str(device["dataPath"])).parent
        for devices in runtimes.values()
        for device in devices
        if device.get("isAvailable") is False and device.get("dataPath")
    ]


def _unavailable_size() -> int:
    """Measure the devices that `simctl delete unavailable` removes.

    Returns:
        int: Bytes on disk.
    """
    listing = run_command(_LIST_CMD, timeout=30).stdout
    return disk_usage(_unavailable_devices(listing))


PLUGIN = Plugin(
    name="simulators",
    description="Simulator devices the installed Xcode cannot run",
    platforms=frozenset({"darwin"}),
    requires="xcodebuild",
    is_available=_is_available,
    actions=(
        Action(
            name="unavailable",
            tier=Tier.SAFE,
            help=(
                "Remove simulator devices whose runtime the installed Xcode no longer "
                "supports, along with their data. They cannot boot anyway."
            ),
            command=("xcrun", "simctl", "delete", "unavailable"),
            size=_unavailable_size,
        ),
    ),
)
