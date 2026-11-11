"""Load the user config file and environment overrides, and resolve plugin settings."""

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import cast

from purger.plugin import (
    Plugin,
    PluginSettings,
    Setting,
    SettingValue,
)
from purger.safety import invoking_home

FALSY_VALUES = frozenset({"", "0", "false", "f", "no", "n", "off"})
_BOOL_WORDS: Mapping[str, bool] = {
    **dict.fromkeys(("1", "true", "t", "yes", "y", "on"), True),
    **dict.fromkeys(FALSY_VALUES, False),
}

_ENV_PREFIX = "PURGER_"
_ENV_CONFIG = "PURGER_CONFIG"
_ENV_SKIP = "PURGER_SKIP"


class ConfigError(Exception):
    """Raised when the config file or an environment variable holds an unusable value."""


@dataclass(frozen=True, kw_only=True)
class Config:
    """The resolved user configuration.

    `values` holds only the keys the user set, so a plugin falls back to its
    declared default for everything else. `warnings` collects problems that
    should not stop a run, such as a misspelled key.
    """

    skip: tuple[str, ...] = ()
    values: Mapping[str, Mapping[str, SettingValue]] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def settings_for(self, plugin: Plugin) -> PluginSettings:
        """Resolve one plugin's settings, falling back to each declared default.

        A default `Config()` yields only the declared defaults, which lets tests
        and the help catalog read settings without loading a file.

        Args:
            plugin: The plugin whose settings to resolve.

        Returns:
            PluginSettings: Every declared setting with its value for this run.
        """
        configured = self.values.get(plugin.name, {})
        return PluginSettings(
            plugin=plugin.name,
            values=MappingProxyType(
                {item.name: configured.get(item.name, item.default) for item in plugin.settings}
            ),
        )


def config_path(env: Mapping[str, str]) -> tuple[Path, bool]:
    """Resolve where the config file lives.

    Args:
        env: The environment to read `PURGER_CONFIG` and `XDG_CONFIG_HOME` from.

    Returns:
        tuple[Path, bool]: The path, and whether `PURGER_CONFIG` set it explicitly.

    Raises:
        RuntimeError: If neither variable is set and no home directory can be determined.
    """
    explicit = env.get(_ENV_CONFIG)
    if explicit:
        return Path(explicit).expanduser(), True
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else invoking_home() / ".config"
    return base / "purger" / "config.toml", False


def _split_list(raw: str) -> tuple[str, ...]:
    """Split a comma-separated value, trimming items and dropping empty ones."""
    return tuple(item for part in raw.split(",") if (item := part.strip()))


def _from_env(*, setting_def: Setting, raw: str) -> object:
    """Convert an environment string toward a setting's type, unchanged when it does not fit."""
    if setting_def.value_type is int:
        try:
            return int(raw)
        except ValueError:
            return raw
    if setting_def.value_type is bool:
        return _BOOL_WORDS.get(raw.strip().lower(), raw)
    # Equality, because `list[str]` builds a new alias on each use.
    if setting_def.value_type == list[str]:
        return _split_list(raw)
    return raw


def _invalid(setting_def: Setting, *, raw: object, source: str) -> ConfigError:
    """Build the error for a value that does not have the setting's type."""
    msg = (
        f"{source}: {raw!r} is not valid for {setting_def.name!r}, "
        f"expected {setting_def.expected_type_description}"
    )
    return ConfigError(msg)


def _validated(setting_def: Setting, *, raw: object, value: object, source: str) -> SettingValue:
    """Check a converted value against the setting's type and its `validate` function.

    Args:
        setting_def: The setting the value is for.
        raw: The value as the user wrote it, quoted in a type error.
        value: The value converted toward the setting's type.
        source: Where the value came from, for error messages.

    Raises:
        ConfigError: If the value has the wrong type or fails the setting's `validate`.
    """
    if not setting_def.accepts(value):
        raise _invalid(setting_def, raw=raw, source=source)
    checked = cast("SettingValue", value)
    if setting_def.validate is not None:
        try:
            setting_def.validate(checked)
        except ValueError as error:
            msg = f"{source}: {checked!r} is not valid for {setting_def.name!r}: {error}"
            raise ConfigError(msg) from error
    return checked


def _read_file(path: Path, *, explicit: bool) -> dict[str, object]:
    """Read and parse the TOML file, treating a missing default file as empty."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if explicit:
            msg = f"config file {str(path)!r} set by {_ENV_CONFIG} does not exist"
            raise ConfigError(msg) from None
        return {}
    except (OSError, UnicodeDecodeError) as error:
        msg = f"cannot read config file {str(path)!r}: {error}"
        raise ConfigError(msg) from error
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        msg = f"config file {str(path)!r} is not valid TOML: {error}"
        raise ConfigError(msg) from error


def _env_name(plugin_name: str, key: str) -> str:
    """Build the environment variable that overrides one plugin setting."""
    return f"{_ENV_PREFIX}{plugin_name.upper().replace('-', '_')}__{key.upper()}"


def _read_file_skip(raw: object, *, source: str) -> tuple[str, ...]:
    """Validate the file's top-level `skip` list."""
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        msg = f"{source}: {raw!r} is not valid for 'skip', expected a list of strings"
        raise ConfigError(msg)
    return tuple(raw)


def _read_file_plugins(
    *, table: object, plugins: Mapping[str, Plugin], source: str
) -> tuple[dict[str, dict[str, SettingValue]], list[str]]:
    """Read the file's `[plugins.<name>]` tables.

    Returns:
        tuple[dict[str, dict[str, SettingValue]], list[str]]: The values by plugin and
            setting name, and warnings for unknown plugins and settings.
    """
    if not isinstance(table, dict):
        msg = f"{source}: {table!r} is not valid for 'plugins', expected a table"
        raise ConfigError(msg)
    values: dict[str, dict[str, SettingValue]] = {}
    warnings: list[str] = []
    for plugin_name, plugin_table in table.items():
        plugin = plugins.get(plugin_name)
        if plugin is None:
            warnings.append(f"{source}: unknown plugin table [plugins.{plugin_name}]")
            continue
        if not isinstance(plugin_table, dict):
            msg = f"{source}: {plugin_table!r} is not valid for 'plugins.{plugin_name}', expected a table"
            raise ConfigError(msg)
        declared = {item.name: item for item in plugin.settings}
        for key, raw in plugin_table.items():
            setting_def = declared.get(key)
            if setting_def is None:
                warnings.append(f"{source}: unknown setting {key!r} in [plugins.{plugin_name}]")
                continue
            values.setdefault(plugin_name, {})[key] = _validated(
                setting_def,
                raw=raw,
                # TOML arrays load as lists, and list settings hold tuples.
                value=tuple(raw) if isinstance(raw, list) else raw,
                source=f"{source} [plugins.{plugin_name}]",
            )
    return values, warnings


@dataclass(frozen=True, kw_only=True)
class _EnvOverrides:
    """What the `PURGER_*` environment variables set."""

    values: dict[str, dict[str, SettingValue]]
    skip: tuple[str, ...] | None
    warnings: list[str]


def _read_env(*, plugins: Mapping[str, Plugin], env: Mapping[str, str]) -> _EnvOverrides:
    """Read the `PURGER_*` overrides, including the skip list when it is set."""
    known: dict[str, tuple[str, Setting]] = {
        _env_name(plugin.name, item.name): (plugin.name, item)
        for plugin in plugins.values()
        for item in plugin.settings
    }
    values: dict[str, dict[str, SettingValue]] = {}
    warnings: list[str] = []
    skip: tuple[str, ...] | None = None
    for name, raw in env.items():
        if not name.startswith(_ENV_PREFIX) or name == _ENV_CONFIG:
            continue
        if name == _ENV_SKIP:
            skip = _split_list(raw)
        elif name in known:
            plugin_name, setting_def = known[name]
            values.setdefault(plugin_name, {})[setting_def.name] = _validated(
                setting_def,
                raw=raw,
                value=_from_env(setting_def=setting_def, raw=raw),
                source=f"environment variable {name}",
            )
        else:
            warnings.append(f"unknown environment variable {name}")
    return _EnvOverrides(values=values, skip=skip, warnings=warnings)


def load_config(plugins: Mapping[str, Plugin], *, env: Mapping[str, str] | None = None) -> Config:
    """Read the config file and environment into one validated Config.

    Environment variables replace file values, so a one-off run can override
    the file. Problems that do not stop a run, such as a misspelled key, are
    collected as warnings so the caller can print them.

    Args:
        plugins: Registered plugins by name, which declare the valid settings.
        env: The environment to read. Defaults to `os.environ`.

    Raises:
        ConfigError: If the file is unreadable or invalid TOML, or a file or
            environment value has the wrong type or fails a setting's `validate`.
    """
    environment = os.environ if env is None else env
    try:
        path, explicit = config_path(environment)
    except RuntimeError:
        # Without a home folder there is no default file, so only the environment applies.
        data: dict[str, object] = {}
        source = "config file"
    else:
        data = _read_file(path, explicit=explicit)
        source = f"config file {str(path)!r}"

    warnings: list[str] = []
    values: dict[str, dict[str, SettingValue]] = {}
    skip: tuple[str, ...] = ()
    for key, raw in data.items():
        if key == "skip":
            skip = _read_file_skip(raw, source=source)
        elif key == "plugins":
            values, plugin_warnings = _read_file_plugins(table=raw, plugins=plugins, source=source)
            warnings.extend(plugin_warnings)
        else:
            warnings.append(f"{source}: unknown key {key!r}")

    overrides = _read_env(plugins=plugins, env=environment)
    warnings.extend(overrides.warnings)
    for plugin_name, found in overrides.values.items():
        values.setdefault(plugin_name, {}).update(found)
    if overrides.skip is not None:
        skip = overrides.skip
    warnings.extend(
        f"skip names an unknown plugin {name!r}" for name in skip if name not in plugins
    )

    frozen = MappingProxyType({name: MappingProxyType(found) for name, found in values.items()})
    return Config(skip=skip, values=frozen, warnings=tuple(warnings))
