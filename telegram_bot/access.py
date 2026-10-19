"""Fail-closed Telegram owner authorization helpers."""
import os
from env_config import load_project_env


class AccessConfigurationError(ValueError):
    """Raised when TELEGRAM_OWNER_IDS is missing, malformed, or invalid."""
    pass


def parse_owner_ids(raw):
    """Parse comma-separated numeric Telegram user IDs; malformed lists fail closed.

    Supports:
      - Single numeric user ID: '123456789'
      - Multiple comma-separated IDs: '123456789,987654321'
      - Arbitrary whitespace around IDs and commas
      - Direct numeric values or collections

    Fails closed (raises AccessConfigurationError) on:
      - Empty or missing configuration
      - Non-numeric or negative values
      - Zero or non-integer values
    """
    if raw is None:
        raise AccessConfigurationError("Set TELEGRAM_OWNER_IDS to one or more Telegram numeric user IDs.")
    if isinstance(raw, bool):
        raise AccessConfigurationError("TELEGRAM_OWNER_IDS must contain only comma-separated numeric user IDs.")
    if isinstance(raw, int):
        if raw <= 0:
            raise AccessConfigurationError("TELEGRAM_OWNER_IDS must contain only comma-separated numeric user IDs.")
        return frozenset({raw})
    if isinstance(raw, (list, tuple, set, frozenset)):
        values = [str(x).strip() for x in raw if str(x).strip()]
    else:
        values = [part.strip() for part in str(raw).split(",") if part.strip()]

    if not values:
        raise AccessConfigurationError("Set TELEGRAM_OWNER_IDS to one or more Telegram numeric user IDs.")
    for value in values:
        if not value.isascii() or not value.isdecimal() or int(value) <= 0:
            raise AccessConfigurationError("TELEGRAM_OWNER_IDS must contain only comma-separated numeric user IDs.")
    return frozenset(int(value) for value in values)


def get_owner_ids(env_var="TELEGRAM_OWNER_IDS"):
    """Load environment variables and return validated owner IDs frozenset."""
    load_project_env()
    raw = os.getenv(env_var)
    if raw is None or not str(raw).strip():
        raise AccessConfigurationError(f"Set {env_var} to one or more Telegram numeric user IDs.")
    return parse_owner_ids(raw)


def is_authorized(update, owner_ids):
    """Only accept owner-originated commands and callbacks in private chats."""
    if not isinstance(update, dict) or not owner_ids:
        return False
    try:
        target = update.get("message") or update.get("edited_message")
        if target and isinstance(target, dict):
            sender = target.get("from") or {}
            sender_id = int(sender.get("id"))
            chat = target.get("chat") or {}
            return chat.get("type") == "private" and sender_id in owner_ids

        cb = update.get("callback_query")
        if cb and isinstance(cb, dict):
            sender = cb.get("from") or {}
            sender_id = int(sender.get("id"))
            chat = cb.get("message", {}).get("chat", {}) if isinstance(cb.get("message"), dict) else {}
            return chat.get("type") == "private" and sender_id in owner_ids

        return False
    except (AttributeError, TypeError, ValueError):
        return False
