"""Fail-closed Telegram owner authorization helpers."""


class AccessConfigurationError(ValueError):
    pass


def parse_owner_ids(raw):
    """Parse comma-separated numeric Telegram user IDs; malformed lists fail closed."""
    values = [part.strip() for part in (raw or "").split(",") if part.strip()]
    if not values:
        raise AccessConfigurationError("Set TELEGRAM_OWNER_IDS to one or more Telegram numeric user IDs.")
    if any(not value.isdigit() for value in values):
        raise AccessConfigurationError("TELEGRAM_OWNER_IDS must contain only comma-separated numeric user IDs.")
    return frozenset(int(value) for value in values)


def is_authorized(update, owner_ids):
    """Only accept owner-originated commands in private chats."""
    try:
        message = update.get("message") or update.get("edited_message") or {}
        sender_id = int(message.get("from", {}).get("id"))
        chat = message.get("chat", {})
        return chat.get("type") == "private" and sender_id in owner_ids
    except (AttributeError, TypeError, ValueError):
        return False
