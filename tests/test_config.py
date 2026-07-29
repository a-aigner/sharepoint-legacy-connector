"""Settings precedence, derived views, and — above all — password redaction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spconnect.config import REDACTED, Settings, load_settings

ENV_TEXT = """
SP_BASE_URL=http://sharepoint.intern.example.de/
SP_AUTH_MODE=basic
SP_USERNAME=CONTOSO\\svc_extract
SP_PASSWORD=hunter2-from-dotenv
SP_INCLUDE_LISTS=Servicefälle, Kunden ,
SP_SKIP_EXTENSIONS=.exe,dll, .MSI
SP_PAGE_SIZE=500
SP_LOG_LEVEL=debug
"""


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(ENV_TEXT, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# redaction — an acceptance criterion
# --------------------------------------------------------------------------- #


def test_password_is_absent_from_repr(env_file: Path) -> None:
    settings = load_settings(env_file=env_file)
    assert settings.password.get_secret_value() == "hunter2-from-dotenv"
    assert "hunter2-from-dotenv" not in repr(settings)
    assert "hunter2-from-dotenv" not in str(settings)
    assert REDACTED in repr(settings)


def test_password_is_absent_from_the_manifest_config_snapshot(env_file: Path) -> None:
    snapshot = load_settings(env_file=env_file).redacted_dict()
    assert snapshot["password"] == REDACTED
    assert "hunter2-from-dotenv" not in json.dumps(snapshot, default=str)


def test_password_is_absent_from_the_model_dump_used_anywhere_public(env_file: Path) -> None:
    settings = load_settings(env_file=env_file)
    assert "hunter2-from-dotenv" not in json.dumps(settings.model_dump(mode="json"), default=str)


def test_username_is_not_redacted(env_file: Path) -> None:
    # Only the password is secret; the account name is diagnostic information.
    assert "svc_extract" in repr(load_settings(env_file=env_file))


# --------------------------------------------------------------------------- #
# precedence
# --------------------------------------------------------------------------- #


def test_dotenv_is_read(env_file: Path) -> None:
    settings = load_settings(env_file=env_file)
    assert settings.auth_mode == "basic"
    assert settings.page_size == 500


def test_environment_variable_beats_dotenv(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SP_PAGE_SIZE", "50")
    assert load_settings(env_file=env_file).page_size == 50


def test_cli_override_beats_everything(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SP_PAGE_SIZE", "50")
    assert load_settings(env_file=env_file, overrides={"page_size": 7}).page_size == 7


def test_none_overrides_are_ignored(env_file: Path) -> None:
    # A CLI flag that was not supplied must not clobber the env var.
    assert load_settings(env_file=env_file, overrides={"page_size": None}).page_size == 500


def test_defaults_apply_when_nothing_is_set() -> None:
    settings = Settings(_env_file=None)
    assert settings.page_size == 200
    assert settings.auth_mode == "ntlm"
    assert settings.requests_per_second == 3.0
    assert settings.concurrency == 2


# --------------------------------------------------------------------------- #
# derived views
# --------------------------------------------------------------------------- #


def test_base_url_loses_its_trailing_slash(env_file: Path) -> None:
    assert load_settings(env_file=env_file).base_url == "http://sharepoint.intern.example.de"


def test_comma_separated_settings_are_split_and_trimmed(env_file: Path) -> None:
    settings = load_settings(env_file=env_file)
    assert settings.include_lists_list == ["Servicefälle", "Kunden"]
    assert settings.exclude_lists_list == []


def test_skip_extensions_are_normalised(env_file: Path) -> None:
    assert load_settings(env_file=env_file).skip_extensions_list == [".exe", ".dll", ".msi"]


def test_max_file_bytes() -> None:
    assert Settings(_env_file=None, max_file_mb=1).max_file_bytes == 1024 * 1024


def test_log_level_is_upper_cased(env_file: Path) -> None:
    assert load_settings(env_file=env_file).log_level == "DEBUG"


def test_invalid_auth_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, auth_mode="kerberos")


def test_invalid_concurrency_is_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, concurrency=0)
