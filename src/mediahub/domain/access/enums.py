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
    MANAGE_CREDENTIALS = "manage_credentials"
    """Install or inspect the credentials the engine presents to sources.

    Owner only, and it stays that way by construction: the owner's permission
    set is *every* action, while the other roles are enumerated, so a new
    action is owner-only until somebody deliberately widens it. That is the
    right default here - a cookie jar is a live session, and whoever can
    replace it can make the device fetch as somebody else.
    """
