"""Plugin for iOS and other Apple simulators that the installed Xcode cannot run."""

import glob
import json
from functools import cache
from pathlib import Path

from nclutils.sh import run_command

from purger.config import PluginSettings
from purger.details import tilde
from purger.plugin import Action, Detail, PathSizes, Plugin, Tier
from purger.runner import command_succeeds

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


def _unavailable_devices(listing: str) -> dict[Path, str]:
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
        ).parent: f"{device.get('name', 'Unknown')} ({_runtime_name(runtime)})"
        for runtime, devices in runtimes.items()
        for device in devices
        if device.get("isAvailable") is False and device.get("dataPath")
    }


def _runtime_name(key: str) -> str:
    """Turn a runtime key such as `com.apple.CoreSimulator.SimRuntime.iOS-17-5` into `iOS 17.5`."""
    _, _, short = key.rpartition("SimRuntime.")
    system, _, version = short.partition("-")
    return f"{system} {version.replace('-', '.')}".strip()


@cache
def _unavailable_labels() -> dict[Path, str]:
    """Ask simctl for the unavailable devices once per run, since paths and details both need them.

    Returns:
        dict[Path, str]: Each unavailable device's folder mapped to its label.
    """
    return _unavailable_devices(run_command(_LIST_CMD, timeout=30).stdout)


def _unavailable_device_paths() -> tuple[str, ...]:
    """List the folders `simctl delete unavailable` removes, so purger can size and show them.

    Returns:
        tuple[str, ...]: Each unavailable device's folder, escaped so glob characters match literally.
    """
    return tuple(glob.escape(str(path)) for path in _unavailable_labels())


def _details(sizes: PathSizes, settings: PluginSettings) -> list[Detail]:  # noqa: ARG001
    """Label each unavailable device by name and runtime.

    Args:
        sizes: Each device folder's measured size.
        settings: The plugin's settings for this run, unused.

    Returns:
        list[Detail]: One row per device.
    """
    labels = _unavailable_labels() if sizes else {}
    # Keyed by folder, so two devices with the same name and runtime stay apart.
    return [
        Detail(label=labels.get(path) or tilde(path), size_bytes=size, paths=(path,), key=str(path))
        for path, size in sizes.items()
    ]


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
            paths=_unavailable_device_paths,
            details=_details,
        ),
    ),
)
