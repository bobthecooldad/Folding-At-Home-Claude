"""Validators shared by the config flow, entities and services."""

from __future__ import annotations

import re

import voluptuous as vol

PASSKEY_RE = re.compile(r"^[0-9a-fA-F]{32}$")
# Same rule as the F@h stats site: no whitespace/tabs, printable, <= 100 chars
USER_RE = re.compile(r"^[^\s]{1,100}$")


def is_valid_passkey(value: str) -> bool:
    """Empty, or 32 hex characters (enforced by the client's PasskeyConstraint)."""
    return value == "" or bool(PASSKEY_RE.match(value))


def valid_passkey(value: str) -> str:
    """Voluptuous validator."""
    value = value.strip()
    if not is_valid_passkey(value):
        raise vol.Invalid("Passkey must be 32 hexadecimal characters (0-9, a-f)")
    return value


def is_valid_user(value: str) -> bool:
    """Username check."""
    return bool(USER_RE.match(value))


def valid_user(value: str) -> str:
    """Voluptuous validator."""
    value = value.strip()
    if not is_valid_user(value):
        raise vol.Invalid("User name must be 1-100 characters with no spaces")
    return value
