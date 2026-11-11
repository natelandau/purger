"""Plugin for the apt package cache."""

from purger.plugin import (
    Action,
    OperatingSystem,
    Plugin,
    RiskLevel,
    RunCommand,
    SizeOfPaths,
    SizeUnknown,
)

APT_PACKAGE_FILES = "/var/cache/apt/archives/*.deb"

PLUGIN = Plugin(
    name="apt",
    description="apt downloaded package files",
    operating_systems=(OperatingSystem.LINUX,),
    requires_on_path="apt-get",
    total_size=SizeOfPaths(APT_PACKAGE_FILES),
    actions=(
        Action(
            name="autoclean",
            risk_level=RiskLevel.SAFE,
            description="Remove downloaded packages that can no longer be downloaded again.",
            cleanup=RunCommand("apt-get", "autoclean", with_sudo=True),
            size=SizeUnknown(reason="apt-get cannot preview which packages autoclean removes"),
        ),
        Action(
            name="clean",
            risk_level=RiskLevel.SAFE,
            description="Remove all downloaded package files. Reinstalls download them again.",
            cleanup=RunCommand("apt-get", "clean", with_sudo=True),
            size=SizeOfPaths(APT_PACKAGE_FILES),
        ),
    ),
)
