"""Tests for the config loader."""

import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from purger.config import (
    Config,
    ConfigError,
    config_path,
    load_config,
)
from purger.plugin import Plugin, Setting, SettingValue
from tests.conftest import make_plugin

if TYPE_CHECKING:
    from collections.abc import Mapping, MutableMapping


def _reject_big(value: SettingValue) -> None:
    """Reject depths above ten."""
    if isinstance(value, int) and value > 10:
        msg = "too big"
        raise ValueError(msg)


def _fake(name: str = "fake") -> Plugin:
    """Build a plugin with one setting of each kind."""
    return make_plugin(
        name=name,
        settings=(
            Setting(name="paths", value_type=list[str], default=("~/p",), description="Paths."),
            Setting(
                name="max_depth",
                value_type=int,
                default=4,
                description="Depth.",
                validate=_reject_big,
            ),
            Setting(name="flag", value_type=bool, default=False, description="Flag."),
            Setting(name="label", value_type=str, default="x", description="Label."),
        ),
    )


def _write(tmp_path: Path, text: str) -> Path:
    """Write a config file and return its path."""
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def fake() -> Plugin:
    """Provide the fake plugin."""
    return _fake()


def test_missing_default_file_gives_empty_config(tmp_path: Path, fake: Plugin) -> None:
    """Verify a missing default config file is an empty config."""
    # Given
    env = {"XDG_CONFIG_HOME": str(tmp_path)}

    # When
    config = load_config({"fake": fake}, env=env)

    # Then
    assert config.skip == ()
    assert config.warnings == ()


def test_missing_explicit_file_raises(tmp_path: Path, fake: Plugin) -> None:
    """Verify a PURGER_CONFIG path that does not exist is an error."""
    # Given
    env = {"PURGER_CONFIG": str(tmp_path / "nope.toml")}

    # When / Then
    with pytest.raises(ConfigError, match="does not exist"):
        load_config({"fake": fake}, env=env)


def test_unknown_home_reads_only_the_environment(
    fake: Plugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a run with no home folder and no config variables reads the environment only."""
    # Given

    def no_home() -> Path:
        msg = "no home"
        raise RuntimeError(msg)

    monkeypatch.setattr("purger.config.invoking_home", no_home)

    # When
    config = load_config({"fake": fake}, env={"PURGER_SKIP": "fake"})

    # Then
    assert config.skip == ("fake",)
    assert config.warnings == ()


def test_xdg_config_home_is_used() -> None:
    """Verify XDG_CONFIG_HOME decides the default path."""
    # Given / When / Then
    assert config_path({"XDG_CONFIG_HOME": "/x"}) == (Path("/x/purger/config.toml"), False)


def test_file_values_are_read(tmp_path: Path, fake: Plugin) -> None:
    """Verify skip and plugin settings come from the file."""
    # Given
    path = _write(tmp_path, 'skip = ["fake"]\n[plugins.fake]\npaths = ["~/r"]\n')

    # When
    config = load_config({"fake": fake}, env={"PURGER_CONFIG": str(path)})

    # Then
    assert config.skip == ("fake",)
    assert config.values["fake"]["paths"] == ("~/r",)
    assert config.warnings == ()


def test_env_beats_file(tmp_path: Path, fake: Plugin) -> None:
    """Verify an environment variable replaces the file value."""
    # Given
    path = _write(tmp_path, "[plugins.fake]\nmax_depth = 3\n")
    env = {"PURGER_CONFIG": str(path), "PURGER_FAKE__MAX_DEPTH": "9"}

    # When
    config = load_config({"fake": fake}, env=env)

    # Then
    assert config.values["fake"]["max_depth"] == 9


def test_env_skip_replaces_file_skip(tmp_path: Path, fake: Plugin) -> None:
    """Verify PURGER_SKIP replaces the file's skip list."""
    # Given
    path = _write(tmp_path, 'skip = ["other"]\n')
    env = {"PURGER_CONFIG": str(path), "PURGER_SKIP": "fake"}

    # When
    config = load_config({"fake": fake}, env=env)

    # Then
    assert config.skip == ("fake",)


def test_env_list_trims_and_drops_empty_items(tmp_path: Path, fake: Plugin) -> None:
    """Verify list env values are split on commas, trimmed, and stripped of empties."""
    # Given
    path = _write(tmp_path, "")

    # When
    spaced = load_config(
        {"fake": fake}, env={"PURGER_CONFIG": str(path), "PURGER_FAKE__PATHS": " ~/a , ,~/b "}
    )
    empty = load_config({"fake": fake}, env={"PURGER_CONFIG": str(path), "PURGER_FAKE__PATHS": ""})

    # Then
    assert spaced.values["fake"]["paths"] == ("~/a", "~/b")
    assert empty.values["fake"]["paths"] == ()


def test_hyphenated_plugin_env_name(tmp_path: Path) -> None:
    """Verify a hyphen in the plugin name becomes an underscore in the variable."""
    # Given
    plugin = _fake("pre-commit")
    path = _write(tmp_path, "")
    env = {"PURGER_CONFIG": str(path), "PURGER_PRE_COMMIT__FLAG": "yes"}

    # When
    config = load_config({"pre-commit": plugin}, env=env)

    # Then
    assert config.values["pre-commit"]["flag"] is True
    assert config.warnings == ()


@pytest.mark.parametrize(
    ("toml", "env", "key"),
    [
        ('[plugins.fake]\nmax_depth = "x"\n', {}, "max_depth"),
        ("[plugins.fake]\nmax_depth = true\n", {}, "max_depth"),
        ("", {"PURGER_FAKE__MAX_DEPTH": "x"}, "max_depth"),
        ("", {"PURGER_FAKE__FLAG": "maybe"}, "flag"),
        ('skip = "fake"\n', {}, "skip"),
        ("[plugins.fake]\npaths = [1]\n", {}, "paths"),
    ],
)
def test_wrong_type_raises(
    tmp_path: Path, fake: Plugin, toml: str, env: dict[str, str], key: str
) -> None:
    """Verify a value of the wrong type raises an error that names the key."""
    # Given
    path = _write(tmp_path, toml)

    # When / Then
    with pytest.raises(ConfigError, match=key):
        load_config({"fake": fake}, env={"PURGER_CONFIG": str(path), **env})


def test_setting_validate_rejects_value(tmp_path: Path, fake: Plugin) -> None:
    """Verify a failing setting check becomes a ConfigError."""
    # Given
    path = _write(tmp_path, "[plugins.fake]\nmax_depth = 99\n")

    # When / Then
    with pytest.raises(ConfigError, match="too big"):
        load_config({"fake": fake}, env={"PURGER_CONFIG": str(path)})


@pytest.mark.parametrize(
    ("toml", "expected_message"),
    [
        (
            '[plugins.fake]\npaths = "~/p"\n',
            "is not valid for 'paths', expected a list of strings",
        ),
        (
            '[plugins.fake]\nmax_depth = "deep"\n',
            "is not valid for 'max_depth', expected an integer",
        ),
    ],
)
def test_wrong_type_error_names_the_expected_type(
    tmp_path: Path, fake: Plugin, toml: str, expected_message: str
) -> None:
    """Verify the error for a wrong-typed value says what type was expected."""
    # Given a config file with a value of the wrong type
    path = _write(tmp_path, toml)

    # When / Then
    with pytest.raises(ConfigError, match=expected_message):
        load_config({"fake": fake}, env={"PURGER_CONFIG": str(path)})


def test_invalid_toml_raises(tmp_path: Path, fake: Plugin) -> None:
    """Verify invalid TOML raises a ConfigError."""
    # Given
    path = _write(tmp_path, "skip = [")

    # When / Then
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config({"fake": fake}, env={"PURGER_CONFIG": str(path)})


@pytest.mark.parametrize(
    ("toml", "env", "needle"),
    [
        ("bogus = 1\n", {}, "bogus"),
        ("[plugins.nope]\na = 1\n", {}, "nope"),
        ("[plugins.fake]\nbogus = 1\n", {}, "bogus"),
        ("", {"PURGER_BOGUS": "1"}, "PURGER_BOGUS"),
        ('skip = ["ghost"]\n', {}, "ghost"),
    ],
)
def test_unknown_keys_warn(
    tmp_path: Path, fake: Plugin, toml: str, env: dict[str, str], needle: str
) -> None:
    """Verify an unknown key, table, variable, or skip name warns without failing."""
    # Given
    path = _write(tmp_path, toml)

    # When
    config = load_config({"fake": fake}, env={"PURGER_CONFIG": str(path), **env})

    # Then
    assert len(config.warnings) == 1
    assert needle in config.warnings[0]


def test_invalid_utf8_raises(tmp_path: Path, fake: Plugin) -> None:
    """Verify a config file that is not UTF-8 raises a ConfigError."""
    # Given
    path = tmp_path / "config.toml"
    path.write_bytes(b"skip = [\xff]")

    # When / Then
    with pytest.raises(ConfigError, match="cannot read"):
        load_config({"fake": fake}, env={"PURGER_CONFIG": str(path)})


def test_unreadable_path_raises(tmp_path: Path, fake: Plugin) -> None:
    """Verify a config path that is a directory raises a ConfigError."""
    # Given / When / Then
    with pytest.raises(ConfigError, match="cannot read"):
        load_config({"fake": fake}, env={"PURGER_CONFIG": str(tmp_path)})


@pytest.mark.parametrize(
    ("toml", "needle"),
    [("plugins = 1\n", "'plugins'"), ("[plugins]\nfake = 1\n", "plugins.fake")],
)
def test_plugin_tables_must_be_tables(tmp_path: Path, fake: Plugin, toml: str, needle: str) -> None:
    """Verify `plugins` and each `plugins.<name>` must be tables."""
    # Given
    path = _write(tmp_path, toml)

    # When / Then
    with pytest.raises(ConfigError, match=needle):
        load_config({"fake": fake}, env={"PURGER_CONFIG": str(path)})


def test_validate_rejects_env_value(tmp_path: Path, fake: Plugin) -> None:
    """Verify a failing setting check on an environment value is a ConfigError."""
    # Given
    path = _write(tmp_path, "")
    env = {"PURGER_CONFIG": str(path), "PURGER_FAKE__MAX_DEPTH": "99"}

    # When / Then
    with pytest.raises(ConfigError, match="PURGER_FAKE__MAX_DEPTH"):
        load_config({"fake": fake}, env=env)


def test_env_skip_unknown_name_warns(tmp_path: Path, fake: Plugin) -> None:
    """Verify an unknown name in PURGER_SKIP warns."""
    # Given
    path = _write(tmp_path, "")

    # When
    config = load_config({"fake": fake}, env={"PURGER_CONFIG": str(path), "PURGER_SKIP": "ghost"})

    # Then
    assert len(config.warnings) == 1
    assert "ghost" in config.warnings[0]


def test_config_values_are_read_only(tmp_path: Path, fake: Plugin) -> None:
    """Verify the loaded values cannot be mutated."""
    # Given
    path = _write(tmp_path, "[plugins.fake]\nmax_depth = 3\n")
    config = load_config({"fake": fake}, env={"PURGER_CONFIG": str(path)})

    # When / Then
    outer = cast("MutableMapping[str, Mapping[str, SettingValue]]", config.values)
    with pytest.raises(TypeError):
        outer["other"] = {}
    mutable = cast("MutableMapping[str, SettingValue]", config.values["fake"])
    with pytest.raises(TypeError):
        mutable["max_depth"] = 5


def test_settings_fall_back_to_defaults(fake: Plugin) -> None:
    """Verify a default Config resolves every setting to its declared default."""
    # Given / When
    settings = Config().settings_for(fake)

    # Then
    assert settings.value("max_depth") == 4
    assert settings.get_string_list("paths") == ("~/p",)


def test_settings_reject_undeclared_name(fake: Plugin) -> None:
    """Verify asking for an undeclared setting raises KeyError."""
    # Given
    settings = Config().settings_for(fake)

    # When / Then
    with pytest.raises(KeyError, match="declares no setting 'nope'"):
        settings.value("nope")
    with pytest.raises(KeyError):
        settings.get_integer("nope")


def test_configured_value_wins_over_default(fake: Plugin) -> None:
    """Verify a configured value replaces the default while other settings keep theirs."""
    # Given
    config = Config(values={"fake": {"max_depth": 7}})

    # When
    settings = config.settings_for(fake)

    # Then
    assert settings.get_integer("max_depth") == 7
    assert settings.get_string_list("paths") == ("~/p",)


def test_settings_ignore_other_plugins_values(fake: Plugin) -> None:
    """Verify one plugin's settings never pick up values configured for another plugin."""
    # Given
    config = Config(values={"other": {"max_depth": 7}})

    # When / Then
    assert config.settings_for(fake).get_integer("max_depth") == 4


def test_typed_accessors_reject_wrong_kind(fake: Plugin) -> None:
    """Verify the typed accessors raise TypeError for a setting of another kind."""
    # Given
    settings = Config().settings_for(fake)

    # When / Then
    with pytest.raises(TypeError, match="is not an integer"):
        settings.get_integer("label")
    with pytest.raises(TypeError, match="is not a list of strings"):
        settings.get_string_list("label")


def test_resolved_settings_are_read_only(fake: Plugin) -> None:
    """Verify a plugin cannot change its resolved settings."""
    # Given
    settings = Config().settings_for(fake)

    # When / Then
    mutable = cast("MutableMapping[str, SettingValue]", settings.values)
    with pytest.raises(TypeError):
        mutable["max_depth"] = 5


def test_tests_never_see_purger_env_vars() -> None:
    """Verify the test isolation drops every `PURGER_*` variable from the shell."""
    # Given / When / Then
    assert [name for name in os.environ if name.startswith("PURGER_")] == []
