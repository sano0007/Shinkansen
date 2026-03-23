"""Configuration module for anime-pahe-dl."""

import json
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path.home() / ".shinkansen"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "default_quality": "best",
    "default_output": "downloads",
    "auto_retry": True,
    "retry_count": 3,
    "create_folder": True,
    "parallel_downloads": 3,
    "download_backend": "requests",  # "requests" | "aria2c"
    "aria2c_path": "aria2c",  # path to aria2c binary (or full path)
    "aria2c_connections": 16,  # segments per file (--split / --max-connection-per-server)
    "prepare_workers": 3,  # parallel Playwright instances for episode preparation
    "max_downloads": 5,  # max concurrent file downloads
}


def get_config_dir() -> Path:
    """Ensure config directory exists."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def load_config() -> dict:
    """Load config from file."""
    if not CONFIG_FILE.exists():
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE) as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    except (json.JSONDecodeError, IOError):
        return DEFAULT_CONFIG.copy()


def save_config(config: dict):
    """Save config to file."""
    get_config_dir()
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_config(key: str, default: Optional[str] = None) -> any:
    """Get a config value."""
    config = load_config()
    return config.get(key, default)


def set_config(key: str, value: any):
    """Set a config value."""
    config = load_config()
    config[key] = value
    save_config(config)


def reset_config():
    """Reset config to defaults."""
    save_config(DEFAULT_CONFIG)
