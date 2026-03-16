"""Tests for anime_pahe_dl.config module."""

import json

from anime_pahe_dl.config import (
    DEFAULT_CONFIG,
    get_config,
    get_config_dir,
    load_config,
    reset_config,
    save_config,
    set_config,
)


class TestGetConfigDir:
    def test_creates_directory(self, tmp_config_dir):
        # tmp_config_dir patches CONFIG_DIR to tmp_path which already exists,
        # but get_config_dir should still work
        result = get_config_dir()
        assert result.exists()
        assert result.is_dir()


class TestLoadConfig:
    def test_no_file_returns_defaults(self, tmp_config_dir):
        config = load_config()
        assert config == DEFAULT_CONFIG

    def test_reads_existing_file(self, tmp_config_dir):
        data = {"default_quality": "720", "retry_count": 5}
        (tmp_config_dir / "config.json").write_text(json.dumps(data))
        config = load_config()
        assert config["default_quality"] == "720"
        assert config["retry_count"] == 5

    def test_merges_with_defaults(self, tmp_config_dir):
        # Only write one key — the rest should come from defaults
        data = {"retry_count": 10}
        (tmp_config_dir / "config.json").write_text(json.dumps(data))
        config = load_config()
        assert config["retry_count"] == 10
        assert config["default_quality"] == DEFAULT_CONFIG["default_quality"]
        assert config["create_folder"] == DEFAULT_CONFIG["create_folder"]

    def test_corrupt_json_returns_defaults(self, tmp_config_dir):
        (tmp_config_dir / "config.json").write_text("{invalid json!!")
        config = load_config()
        assert config == DEFAULT_CONFIG

    def test_empty_file_returns_defaults(self, tmp_config_dir):
        (tmp_config_dir / "config.json").write_text("")
        config = load_config()
        assert config == DEFAULT_CONFIG


class TestSaveConfig:
    def test_creates_file(self, tmp_config_dir):
        save_config({"key": "val"})
        path = tmp_config_dir / "config.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["key"] == "val"

    def test_overwrites_existing(self, tmp_config_dir):
        save_config({"a": 1})
        save_config({"b": 2})
        data = json.loads((tmp_config_dir / "config.json").read_text())
        assert "b" in data
        # save_config writes exactly what's passed
        assert data == {"b": 2}


class TestGetConfig:
    def test_returns_value(self, tmp_config_dir):
        save_config({**DEFAULT_CONFIG, "default_quality": "1080"})
        assert get_config("default_quality") == "1080"

    def test_returns_default_for_missing_key(self, tmp_config_dir):
        assert get_config("nonexistent_key", "fallback") == "fallback"

    def test_returns_none_for_missing_key_no_default(self, tmp_config_dir):
        assert get_config("nonexistent_key") is None


class TestSetConfig:
    def test_persists_value(self, tmp_config_dir):
        set_config("retry_count", 5)
        assert get_config("retry_count") == 5

    def test_preserves_other_keys(self, tmp_config_dir):
        # First set up defaults
        save_config(DEFAULT_CONFIG.copy())
        set_config("retry_count", 99)
        config = load_config()
        assert config["retry_count"] == 99
        assert config["default_quality"] == DEFAULT_CONFIG["default_quality"]
        assert config["create_folder"] == DEFAULT_CONFIG["create_folder"]


class TestResetConfig:
    def test_restores_defaults(self, tmp_config_dir):
        set_config("retry_count", 999)
        reset_config()
        config = load_config()
        assert config == DEFAULT_CONFIG


class TestDefaultConfig:
    def test_has_expected_keys(self):
        expected_keys = {
            "default_quality",
            "default_output",
            "auto_retry",
            "retry_count",
            "create_folder",
            "parallel_downloads",
        }
        assert set(DEFAULT_CONFIG.keys()) == expected_keys

    def test_default_values_types(self):
        assert isinstance(DEFAULT_CONFIG["default_quality"], str)
        assert isinstance(DEFAULT_CONFIG["default_output"], str)
        assert isinstance(DEFAULT_CONFIG["auto_retry"], bool)
        assert isinstance(DEFAULT_CONFIG["retry_count"], int)
        assert isinstance(DEFAULT_CONFIG["create_folder"], bool)
        assert isinstance(DEFAULT_CONFIG["parallel_downloads"], int)
