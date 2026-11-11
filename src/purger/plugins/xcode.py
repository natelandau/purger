"""Plugin for Xcode build products and device support files."""

from purger.plugin import Action, DeletePaths, OperatingSystem, Plugin, RiskLevel

PLUGIN = Plugin(
    name="xcode",
    description="Xcode derived data, device support, and archives",
    operating_systems=(OperatingSystem.MACOS,),
    actions=(
        Action(
            name="derived-data",
            risk_level=RiskLevel.SAFE,
            description="Remove build products and indexes. Xcode rebuilds them on next build.",
            cleanup=DeletePaths("~/Library/Developer/Xcode/DerivedData/*"),
        ),
        Action(
            name="device-support",
            risk_level=RiskLevel.CAUTION,
            description=(
                "Remove iOS debug symbols copied from devices. "
                "Reconnecting each device re-copies them, which takes minutes."
            ),
            cleanup=DeletePaths("~/Library/Developer/Xcode/iOS DeviceSupport/*"),
        ),
        Action(
            name="archives",
            risk_level=RiskLevel.DANGEROUS,
            description=(
                "Remove app archives, which hold the dSYMs needed to symbolicate shipped apps. "
                "Deleted archives cannot be rebuilt."
            ),
            cleanup=DeletePaths("~/Library/Developer/Xcode/Archives/*"),
        ),
    ),
)
