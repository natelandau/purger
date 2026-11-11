"""Plugin for MacPorts build files, distfiles, and inactive ports."""

from pathlib import Path

from nclutils.sh import which

from purger.plugin import (
    Action,
    OperatingSystem,
    Plugin,
    RiskLevel,
    RunCommand,
    SizeOfPaths,
    SizeUnknown,
)

# sudo's secure_path omits the MacPorts bin folder, so the command uses an absolute
# path. The prefix follows the `port` on PATH, since MacPorts can install elsewhere.
PORT_EXECUTABLE = which("port") or Path("/opt/local/bin/port")
MACPORTS_PREFIX = PORT_EXECUTABLE.parent.parent

PLUGIN = Plugin(
    name="macports",
    description="MacPorts build files, distfiles, and inactive ports",
    operating_systems=(OperatingSystem.MACOS,),
    requires_on_path="port",
    total_size=SizeOfPaths(
        f"{MACPORTS_PREFIX}/var/macports/build",
        f"{MACPORTS_PREFIX}/var/macports/distfiles",
        f"{MACPORTS_PREFIX}/var/macports/logs",
    ),
    actions=(
        Action(
            name="clean",
            risk_level=RiskLevel.SAFE,
            description="Remove build directories, source archives, and logs of installed ports.",
            cleanup=RunCommand(
                str(PORT_EXECUTABLE), "-N", "clean", "--all", "installed", with_sudo=True
            ),
            size=SizeUnknown(reason="port cannot preview what clean removes"),
        ),
        Action(
            name="reclaim",
            risk_level=RiskLevel.CAUTION,
            description=(
                "Uninstall inactive ports and unrequested ports nothing depends on, "
                "and remove unused distfiles. Removed ports must be installed again to return."
            ),
            cleanup=RunCommand(str(PORT_EXECUTABLE), "-N", "reclaim", with_sudo=True),
            size=SizeUnknown(reason="port cannot preview what reclaim removes"),
        ),
    ),
)
