# Secrets loaded from: C:\Users\Nayyar\secrets\oracle.env
# This file is OUTSIDE the VS Code workspace — Claude Code cannot read it
# Never move secrets inside the project folder

"""
config/loader.py — Unified config loader for ORACLE.

Load order:
  1. config/config.json  — all non-secret settings (URLs, limits, flags, etc.)
  2. C:\\Users\\Nayyar\\secrets\\oracle.env  — all secret API keys

The two are merged into a single config dict. Secrets overwrite the
empty-string placeholders in config.json. The caller gets one complete dict.

Required keys (RuntimeError if missing or empty):
  GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Optional keys (warning logged, but startup continues — paper trading works without them):
  TWITTER_BEARER_TOKEN, TWITTER_API_KEY, TWITTER_API_SECRET,
  TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET,
  WHALE_ALERT_API_KEY,
  POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE,
  POLYMARKET_PRIVATE_KEY, POLYMARKET_WALLET_ADDRESS
"""

import json
import logging
import os
from pathlib import Path

from dotenv import dotenv_values

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
_CONFIG_JSON = Path(__file__).parent / "config.json"
_ENV_FILE = Path(r"C:\Users\Nayyar\secrets\oracle.env")

# ── Key definitions ───────────────────────────────────────────────────────────
_REQUIRED_KEYS: list[str] = [
    "GEMINI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]

_OPTIONAL_KEYS: list[str] = [
    "TWITTER_BEARER_TOKEN",
    "TWITTER_API_KEY",
    "TWITTER_API_SECRET",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_TOKEN_SECRET",
    "WHALE_ALERT_API_KEY",
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_WALLET_ADDRESS",
]

_ALL_SECRET_KEYS = _REQUIRED_KEYS + _OPTIONAL_KEYS


# ── Main entry point ──────────────────────────────────────────────────────────

def load_config() -> dict:
    """
    Load config.json + oracle.env, merge, validate, return complete config dict.
    Raises RuntimeError if any required key is missing or empty.
    """
    config = _load_json()
    secrets = _load_env()
    _inject_secrets(config, secrets)
    _validate_and_report(secrets)
    return config


# ── JSON base config ──────────────────────────────────────────────────────────

def _load_json() -> dict:
    if not _CONFIG_JSON.exists():
        raise FileNotFoundError(f"config.json not found at {_CONFIG_JSON}")
    with open(_CONFIG_JSON, encoding="utf-8") as f:
        return json.load(f)


# ── .env loading ──────────────────────────────────────────────────────────────

def _load_env() -> dict[str, str]:
    """
    Load secrets from the external .env file.
    Returns a dict of key→value for all non-empty entries.
    Missing file is a warning, not a crash (dev mode may not need all keys).
    """
    if not _ENV_FILE.exists():
        logger.warning(
            "[SECRETS] oracle.env not found at %s — all secrets will be empty. "
            "Create the file and fill in your keys.",
            _ENV_FILE,
        )
        return {}

    raw = dotenv_values(_ENV_FILE)
    # Keep only non-empty values
    return {k: v for k, v in raw.items() if v and v.strip()}


# ── Injection: env values → nested config dict ────────────────────────────────

def _inject_secrets(config: dict, secrets: dict[str, str]):
    """Map flat env keys into their correct nested config locations."""

    def _set(config: dict, *path: str, value: str):
        node = config
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value

    _mapping = {
        "GEMINI_API_KEY":                ("gemini", "api_key"),
        "TELEGRAM_BOT_TOKEN":            ("telegram", "bot_token"),
        "TELEGRAM_CHAT_ID":              ("telegram", "chat_id"),
        "TWITTER_BEARER_TOKEN":          ("twitter", "bearer_token"),
        "TWITTER_API_KEY":               ("twitter", "api_key"),
        "TWITTER_API_SECRET":            ("twitter", "api_secret"),
        "TWITTER_ACCESS_TOKEN":          ("twitter", "access_token"),
        "TWITTER_ACCESS_TOKEN_SECRET":   ("twitter", "access_token_secret"),
        "WHALE_ALERT_API_KEY":           ("whale_alert", "api_key"),
        "POLYMARKET_API_KEY":            ("polymarket", "api_key"),
        "POLYMARKET_API_SECRET":         ("polymarket", "api_secret"),
        "POLYMARKET_API_PASSPHRASE":     ("polymarket", "api_passphrase"),
        "POLYMARKET_PRIVATE_KEY":        ("polymarket", "private_key"),
        "POLYMARKET_WALLET_ADDRESS":     ("polymarket", "wallet_address"),
    }

    for env_key, path in _mapping.items():
        if env_key in secrets:
            _set(config, *path, value=secrets[env_key])


# ── Validation + startup report ───────────────────────────────────────────────

def _validate_and_report(secrets: dict[str, str]):
    """
    Print per-key status. Raise RuntimeError if any required key is absent.
    """
    missing_required: list[str] = []

    for key in _ALL_SECRET_KEYS:
        loaded = key in secrets
        required = key in _REQUIRED_KEYS

        if loaded:
            logger.info("[SECRETS] %-40s loaded", key)
        elif required:
            logger.error("[SECRETS] %-40s MISSING — required key not set", key)
            missing_required.append(key)
        else:
            logger.warning("[SECRETS] %-40s not set (paper trading, OK)", key)

    if missing_required:
        raise RuntimeError(
            f"Required secrets not found in {_ENV_FILE}: {', '.join(missing_required)}. "
            f"Add them to the .env file and restart."
        )
