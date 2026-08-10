"""
Shared Telegram helpers.

The single most common source of "the bot silently does nothing" in this
project was chat-ID resolution. Telegram exposes the same supergroup under
several numeric forms:

    5533411583          <- raw internal channel id
    -5533411583         <- naive negation (INVALID for supergroups)
    -1005533411583      <- the real "marked" id Telethon expects

A plain `get_entity(-5533411583)` raises ValueError/PeerIdInvalid, so every
post and every invite failed. `resolve_target()` tries all sensible forms
(and @usernames / t.me links) until one resolves, so a slightly wrong value
in .env no longer silently disables a whole module.
"""
import logging
from typing import Any, List, Optional

logger = logging.getLogger("OmniBot.TelegramUtils")


def candidate_ids(raw: str) -> List[Any]:
    """
    Builds the list of forms worth trying for a configured chat target,
    most-likely first. Non-numeric values (usernames, invite links) are
    returned as-is.
    """
    raw = (raw or "").strip()
    if not raw:
        return []

    # Username / link forms — hand straight to Telethon.
    if not raw.lstrip("-").isdigit():
        cleaned = raw
        for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
            if cleaned.lower().startswith(prefix):
                cleaned = cleaned[len(prefix):]
                break
        cleaned = cleaned.strip("/")
        if cleaned.startswith("+") or cleaned.startswith("joinchat"):
            # Private invite link — only usable via ImportChatInvite, not get_entity
            return [raw]
        if not cleaned.startswith("@"):
            cleaned = "@" + cleaned
        # Try the normalized @name, then whatever was configured
        return [cleaned] if cleaned == raw else [cleaned, raw]

    n = int(raw)
    digits = str(abs(n))
    forms: List[Any] = []

    def add(v):
        if v not in forms:
            forms.append(v)

    if n < 0:
        if digits.startswith("100"):
            # Already a properly marked channel id, e.g. -1005533411583
            add(n)
            add(-int(digits[3:]))          # in case it's really a basic group
        else:
            # Most likely a supergroup id that is missing the 100 prefix.
            add(int(f"-100{digits}"))      # the usual correct form
            add(n)                          # basic-group / already-correct form
    else:
        add(int(f"-100{digits}"))
        add(-n)
        add(n)

    return forms


async def resolve_target(client, raw: str, label: str = "target") -> Optional[Any]:
    """
    Resolves a configured chat target to a real Telethon entity, trying each
    candidate form. Returns the entity, or None if nothing resolved.
    """
    if not client or not raw:
        return None

    errors = []
    for candidate in candidate_ids(raw):
        try:
            entity = await client.get_entity(candidate)
            logger.info(
                f"Resolved {label} '{raw}' -> "
                f"{getattr(entity, 'title', getattr(entity, 'username', 'entity'))} "
                f"(using {candidate!r})"
            )
            return entity
        except Exception as e:
            errors.append(f"{candidate!r}: {type(e).__name__}")
            continue

    logger.error(f"Could not resolve {label} '{raw}'. Tried -> {'; '.join(errors)}")
    return None


async def resolve_via_dialogs(client, raw: str, label: str = "target") -> Optional[Any]:
    """
    Last-resort resolution: scan the account's dialog list for a chat whose id
    matches any candidate form. This works even when get_entity() fails because
    the entity isn't in the session cache yet (common on a fresh StringSession
    in production, which is exactly what happens on Render).
    """
    if not client or not raw:
        return None

    wanted = set()
    for c in candidate_ids(raw):
        if isinstance(c, int):
            wanted.add(c)
            wanted.add(abs(c))
            digits = str(abs(c))
            if digits.startswith("100"):
                wanted.add(int(digits[3:]))

    if not wanted:
        return None

    try:
        async for dialog in client.iter_dialogs():
            did = dialog.id
            if did in wanted or abs(did) in wanted:
                logger.info(f"Resolved {label} '{raw}' via dialog scan -> {dialog.name} (id {did})")
                return dialog.entity
    except Exception as e:
        logger.warning(f"Dialog scan for {label} failed: {type(e).__name__}: {e}")

    return None


async def resolve_chat(client, raw: str, label: str = "target") -> Optional[Any]:
    """Full resolution strategy: direct forms first, then a dialog scan."""
    entity = await resolve_target(client, raw, label)
    if entity is None:
        entity = await resolve_via_dialogs(client, raw, label)
    return entity
