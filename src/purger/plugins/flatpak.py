"""Plugin for unused Flatpak runtimes and extensions."""

from purger.plugin import (
    Action,
    OperatingSystem,
    Plugin,
    RiskLevel,
    RunCommand,
    SizeUnknown,
)

UNINSTALL_UNUSED_COMMAND = ("flatpak", "uninstall", "--unused", "-y", "--noninteractive")

PLUGIN = Plugin(
    name="flatpak",
    description="Flatpak runtimes and extensions no app uses",
    operating_systems=(OperatingSystem.LINUX,),
    requires_on_path="flatpak",
    total_size=SizeUnknown(reason="flatpak cannot preview what --unused removes"),
    actions=(
        Action(
            name="unused-user",
            risk_level=RiskLevel.SAFE,
            description="Remove runtimes and extensions no app in your user installation uses.",
            cleanup=RunCommand(*UNINSTALL_UNUSED_COMMAND, "--user"),
            size=SizeUnknown(reason="flatpak cannot preview what --unused removes"),
        ),
        Action(
            name="unused-system",
            risk_level=RiskLevel.SAFE,
            description="Remove runtimes and extensions no app in the system installation uses.",
            cleanup=RunCommand(*UNINSTALL_UNUSED_COMMAND, "--system", with_sudo=True),
            size=SizeUnknown(reason="flatpak cannot preview what --unused removes"),
        ),
    ),
)
