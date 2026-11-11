"""Plugin for iOS and other Apple simulators that the installed Xcode cannot run."""

import json
from pathlib import Path

from nclutils.sh import run_command

from purger.details import tilde
from purger.plugin import (
    Action,
    OperatingSystem,
    PathSizes,
    Plugin,
    PluginSettings,
    RiskLevel,
    RunCommand,
    SizeBreakdownRow,
    SizeOfPaths,
    once_per_run,
)
from purger.runner import command_succeeds

LIST_DEVICES_COMMAND = ("xcrun", "simctl", "list", "devices", "--json")


def xcode_first_launch_is_complete() -> bool:
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


def parse_unavailable_devices(listing: str) -> dict[Path, str]:
    """Return the folders of the devices that simctl marks unavailable, with their labels.

    Args:
        listing: Output of `xcrun simctl list devices --json`.

    Returns:
        dict[Path, str]: Each unavailable device's folder, the parent of its data
            folder, mapped to `<name> (<runtime>)`.
    """
    runtimes: dict[str, list[dict[str, object]]] = json.loads(listing)["devices"]
    return {
        Path(
            str(device["dataPath"])
        ).parent: f"{device.get('name', 'Unknown')} ({runtime_name(runtime)})"
        for runtime, devices in runtimes.items()
        for device in devices
        if device.get("isAvailable") is False and device.get("dataPath")
    }


def runtime_name(key: str) -> str:
    """Turn a runtime key such as `com.apple.CoreSimulator.SimRuntime.iOS-17-5` into `iOS 17.5`."""
    _, _, short = key.rpartition("SimRuntime.")
    system, _, version = short.partition("-")
    return f"{system} {version.replace('-', '.')}".strip()


@once_per_run
def unavailable_device_labels(settings: PluginSettings) -> dict[Path, str]:  # noqa: ARG001
    """Ask simctl for the unavailable devices once per run, since the size and its breakdown both need them.

    Args:
        settings: The plugin's settings, which this plugin does not use.

    Returns:
        dict[Path, str]: Each unavailable device's folder mapped to its label.
    """
    return parse_unavailable_devices(run_command(list(LIST_DEVICES_COMMAND), timeout=30).stdout)


def find_unavailable_device_folders(settings: PluginSettings) -> list[Path]:
    """List the folders `simctl delete unavailable` removes, so purger can size and show them.

    Args:
        settings: The plugin's settings for this run.

    Returns:
        list[Path]: Each unavailable device's folder.
    """
    return list(unavailable_device_labels(settings))


def one_row_per_device(sizes: PathSizes, settings: PluginSettings) -> list[SizeBreakdownRow]:
    """Label each unavailable device by name and runtime.

    Args:
        sizes: Each device folder's measured size.
        settings: The plugin's settings for this run.

    Returns:
        list[SizeBreakdownRow]: One row per device.
    """
    labels = unavailable_device_labels(settings) if sizes else {}
    # Keyed by folder, so two devices with the same name and runtime stay apart.
    return [
        SizeBreakdownRow(
            label=labels.get(path) or tilde(path),
            size_bytes=size,
            paths=(path,),
            merge_key=str(path),
        )
        for path, size in sizes.items()
    ]


PLUGIN = Plugin(
    name="simulators",
    description="Simulator devices the installed Xcode cannot run",
    operating_systems=(OperatingSystem.MACOS,),
    requires_on_path="xcodebuild",
    skip_unless=xcode_first_launch_is_complete,
    actions=(
        Action(
            name="unavailable",
            risk_level=RiskLevel.SAFE,
            description=(
                "Remove simulator devices whose runtime the installed Xcode no longer "
                "supports, along with their data. They cannot boot anyway."
            ),
            cleanup=RunCommand("xcrun", "simctl", "delete", "unavailable"),
            size=SizeOfPaths(found_by=find_unavailable_device_folders),
            size_breakdown=one_row_per_device,
        ),
    ),
)
