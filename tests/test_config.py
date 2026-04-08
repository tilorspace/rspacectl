"""Tests for configuration loading."""

import os
from pathlib import Path

import pytest

from rspacectl.config import ConfigError, load_config, save_config


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

    def test_raises_config_error_when_missing(self, monkeypatch):
        monkeypatch.delenv("RSPACE_URL", raising=False)
        monkeypatch.delenv("RSPACE_API_KEY", raising=False)
        # Prevent reading actual ~/.rspacectl
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("rspacectl.config.CONFIG_FILE", Path("/nonexistent/path/.rspacectl"))
            with pytest.raises(ConfigError, match="Missing configuration"):
                load_config()

    def test_raises_on_missing_url_only(self, monkeypatch):
        monkeypatch.delenv("RSPACE_URL", raising=False)
        monkeypatch.setenv("RSPACE_API_KEY", "some-key")
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("rspacectl.config.CONFIG_FILE", Path("/nonexistent/path/.rspacectl"))
            with pytest.raises(ConfigError, match="RSPACE_URL"):
                load_config()


class TestSaveConfig:
    def test_saves_and_loads(self, tmp_path, monkeypatch):
        config_file = tmp_path / ".rspacectl"
        monkeypatch.setattr("rspacectl.config.CONFIG_FILE", config_file)
        monkeypatch.delenv("RSPACE_URL", raising=False)
        monkeypatch.delenv("RSPACE_API_KEY", raising=False)

        save_config("https://saved.example.com", "saved-key")

        assert config_file.exists()
        url, key = load_config()
        assert url == "https://saved.example.com"
        assert key == "saved-key"

    def test_permissions_restricted(self, tmp_path, monkeypatch):
        config_file = tmp_path / ".rspacectl"
        monkeypatch.setattr("rspacectl.config.CONFIG_FILE", config_file)
        save_config("https://example.com", "key")
        mode = oct(config_file.stat().st_mode)
        assert mode.endswith("600")
