"""Tests for configuration loading."""

import os

import pytest

from rspacectl.config import ConfigError, load_config, save_config


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Isolate tests from the developer's real config.

    - Redirects the profile directory to tmp_path, so ~/.rspacectl is never
      read or (worse) overwritten by save_config.
    - Disables the OS keychain lookup.
    - Snapshots os.environ, because load_dotenv writes RSPACE_* straight into
      it, bypassing monkeypatch.
    """
    monkeypatch.setattr("rspacectl.config._CONFIG_DIR", tmp_path)
    monkeypatch.setattr("rspacectl.config._load_from_keychain", lambda profile: (None, None))
    saved_env = dict(os.environ)
    for key in ("RSPACE_URL", "RSPACE_API_KEY"):
        os.environ.pop(key, None)
    yield tmp_path
    os.environ.clear()
    os.environ.update(saved_env)


class TestLoadConfig:
    def test_loads_from_env_vars(self, monkeypatch):
        monkeypatch.setenv("RSPACE_URL", "https://example.com")
        monkeypatch.setenv("RSPACE_API_KEY", "test-key-123")
        url, key = load_config()
        assert url == "https://example.com"
        assert key == "test-key-123"

    def test_strips_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("RSPACE_URL", "https://example.com/")
        monkeypatch.setenv("RSPACE_API_KEY", "test-key")
        url, key = load_config()
        assert url == "https://example.com"

    def test_overrides_take_priority(self, monkeypatch):
        monkeypatch.setenv("RSPACE_URL", "https://env.example.com")
        monkeypatch.setenv("RSPACE_API_KEY", "env-key")
        url, key = load_config(url_override="https://cli.example.com", api_key_override="cli-key")
        assert url == "https://cli.example.com"
        assert key == "cli-key"

    def test_raises_config_error_when_missing(self):
        with pytest.raises(ConfigError, match="Missing configuration"):
            load_config()

    def test_raises_on_missing_url_only(self, monkeypatch):
        monkeypatch.setenv("RSPACE_API_KEY", "some-key")
        with pytest.raises(ConfigError, match="RSPACE_URL"):
            load_config()


class TestSaveConfig:
    def test_saves_and_loads(self, isolated_config):
        config_file = isolated_config / ".rspacectl"

        save_config("https://saved.example.com", "saved-key")

        assert config_file.exists()
        url, key = load_config()
        assert url == "https://saved.example.com"
        assert key == "saved-key"

    def test_permissions_restricted(self, isolated_config):
        config_file = isolated_config / ".rspacectl"
        save_config("https://example.com", "key")
        mode = oct(config_file.stat().st_mode)
        assert mode.endswith("600")
