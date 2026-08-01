"""Closed value sets used by the access context."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """What a principal is allowed to be.

    Attributes:
        OWNER: The person who runs the device. May do everything.
        MEMBER: A household member. May acquire and read their own history.
        READONLY: May look, not touch.
    """

    OWNER = "owner"
    MEMBER = "member"
    READONLY = "readonly"


class Action(StrEnum):
    """Something a principal may attempt.

    Named after intentions rather than endpoints, so the same action covers a
    Telegram message, an HTTP request and a CLI invocation.
    """

    SUBMIT_SOURCE = "submit_source"
    CANCEL_ACQUISITION = "cancel_acquisition"
    VIEW_HISTORY = "view_history"
    VIEW_SETTINGS = "view_settings"
