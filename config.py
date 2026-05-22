"""Centralized configuration for Open Deep Research.

Loads settings from odr-config.json with fallback to defaults.
Auto-creates odr-config.json from odr-config.example.json on first run.
"""

import json
import copy
import shutil
from pathlib import Path

CONFIG_DIR = Path(__file__).parent
CONFIG_PATH = CONFIG_DIR / "odr-config.json"
EXAMPLE_PATH = CONFIG_DIR / "odr-config.example.json"

DEFAULTS = {
    "model": {
        "providers": [
            {"provider": "openai", "api_key": "", "base_url": ""},
        ],
        "default_model_id": "o3-deep-research",
        "max_completion_tokens": 32768,
        "reasoning_effort": "medium",
    },
    "models": [
        {
            "id": "o3-deep-research",
            "name": "OpenAI o3 Deep Research",
            "description": "OpenAI Deep Research model for thorough research tasks",
            "reasoning_efforts": ["medium"],
            "default_reasoning_effort": "medium",
        },
        {
            "id": "o4-mini-deep-research",
            "name": "OpenAI o4-mini Deep Research",
            "description": "Faster, lower-cost OpenAI Deep Research model",
            "reasoning_efforts": ["medium"],
            "default_reasoning_effort": "medium",
        },
    ],
}


def _deep_merge(base, override):
    """Deep merge override into base. Returns a new dict.
    Lists are replaced entirely (not merged element-wise)."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config():
    """Load config from odr-config.json, deep-merged with defaults.
    Auto-creates odr-config.json from example if it doesn't exist."""
    if not CONFIG_PATH.exists():
        if EXAMPLE_PATH.exists():
            shutil.copy2(EXAMPLE_PATH, CONFIG_PATH)
        else:
            save_config(DEFAULTS)

    try:
        with open(CONFIG_PATH, "r") as f:
            user_config = json.load(f)
    except (json.JSONDecodeError, OSError):
        user_config = {}

    return _deep_merge(DEFAULTS, user_config)


def save_config(data):
    """Write config data to odr-config.json."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def get_config(key_path, config=None):
    """Access config value via dot-notation path.
    e.g. get_config("model.default_model_id")
    """
    if config is None:
        config = load_config()
    keys = key_path.split(".")
    value = config
    for key in keys:
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return None
    return value
