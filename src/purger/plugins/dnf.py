"""Plugin for the dnf downloaded package cache."""

from purger.plugin import Action, OperatingSystem, Plugin, RiskLevel, RunCommand, SizeOfPaths

# dnf4 caches under /var/cache/dnf, dnf5 under /var/cache/libdnf5.
DNF_PACKAGE_FILES = ("/var/cache/dnf/*/packages/*.rpm", "/var/cache/libdnf5/*/packages/*.rpm")

PLUGIN = Plugin(
    name="dnf",
    description="dnf downloaded package files",
    operating_systems=(OperatingSystem.LINUX,),
    requires_on_path="dnf",
    total_size=SizeOfPaths(*DNF_PACKAGE_FILES),
    actions=(
        Action(
            name="packages",
            risk_level=RiskLevel.SAFE,
            description="Remove downloaded package files. Reinstalls download them again.",
            cleanup=RunCommand("dnf", "clean", "packages", with_sudo=True),
            size=SizeOfPaths(*DNF_PACKAGE_FILES),
        ),
    ),
)
