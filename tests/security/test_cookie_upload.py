"""Uploading a cookie jar is a privileged operation, and behaves like one.

A jar is a live session: whoever installs one decides which accounts this
device fetches as. The tests here are therefore about *refusals* - who may do
it, what is accepted, and what happens to the credential afterwards - rather
than about the happy path, which is one line.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mediahub.application.credentials.errors import (
    CookieStoreUnavailableError,
    InvalidCookieJarError,
)
from mediahub.domain.access.enums import Action, Role
from mediahub.domain.access.policies import AuthorizationPolicy
from mediahub.infrastructure.credentials.cookie_jar import parse
from mediahub.infrastructure.credentials.filesystem_store import FilesystemCookieStore

pytestmark = pytest.mark.security

FUTURE = int((datetime.now(UTC) + timedelta(days=30)).timestamp())


def jar(*rows: str) -> bytes:
    """Build a Netscape jar with a realistic header."""
    header = "# Netscape HTTP Cookie File\n# This is a generated file.\n"
    return (header + "".join(f"{row}\n" for row in rows)).encode("utf-8")


def row(domain: str = "x.com", name: str = "auth_token", expiry: int = FUTURE) -> str:
    """One cookie line: domain, subdomains, path, secure, expiry, name, value."""
    return f"{domain}\tTRUE\t/\tTRUE\t{expiry}\t{name}\tsecret-value-here"


# -- Who may do it -----------------------------------------------------------


@pytest.mark.parametrize("role", [Role.MEMBER, Role.READONLY])
def test_only_an_owner_may_manage_credentials(role: Role) -> None:
    """A member may fetch things; a member may not decide *as whom*."""
    assert not AuthorizationPolicy().permits(role, Action.MANAGE_CREDENTIALS)


def test_an_owner_may_manage_credentials() -> None:
    assert AuthorizationPolicy().permits(Role.OWNER, Action.MANAGE_CREDENTIALS)


# -- What is accepted --------------------------------------------------------


async def test_a_valid_jar_is_stored(tmp_path: Path) -> None:
    store = FilesystemCookieStore(tmp_path / "cookies.txt")

    summary = await store.install(jar(row(), row(name="ct0")))

    assert summary.cookie_count == 2
    assert summary.domains == ("x.com",)
    assert (tmp_path / "cookies.txt").exists()


@pytest.mark.parametrize(
    ("content", "why"),
    [
        (b"", "empty"),
        (b"just some text a person typed", "prose"),
        (jar(), "header only, no cookies"),
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "a screenshot"),
        (b"\x00\x01\x02\x03binary sqlite garbage", "the browser's own database"),
    ],
    ids=["empty", "prose", "no-cookies", "png", "binary"],
)
async def test_something_that_is_not_a_jar_is_refused(
    tmp_path: Path, content: bytes, why: str
) -> None:
    """Storing an unusable jar is worse than refusing one.

    The engine would keep failing with the platform's own message - "no video
    could be found in this post" - which points at the source rather than at
    the export that went wrong.
    """
    del why
    store = FilesystemCookieStore(tmp_path / "cookies.txt")

    with pytest.raises(InvalidCookieJarError):
        await store.install(content)


async def test_a_refused_jar_does_not_replace_a_good_one(tmp_path: Path) -> None:
    """The most damaging version of getting this wrong."""
    path = tmp_path / "cookies.txt"
    store = FilesystemCookieStore(path)
    await store.install(jar(row()))
    good = path.read_bytes()

    with pytest.raises(InvalidCookieJarError):
        await store.install(b"not a jar")

    assert path.read_bytes() == good


async def test_an_absurdly_large_upload_is_refused(tmp_path: Path) -> None:
    """A jar is kilobytes. Anything else is a memory attack on a small device."""
    store = FilesystemCookieStore(tmp_path / "cookies.txt")

    with pytest.raises(InvalidCookieJarError):
        await store.install(b"x" * (4 * 1024 * 1024))


# -- How it is stored --------------------------------------------------------


@pytest.mark.skipif(not hasattr(__import__("os"), "getuid"), reason="POSIX permissions only")
async def test_the_jar_is_not_readable_by_anyone_else(tmp_path: Path) -> None:
    path = tmp_path / "cookies.txt"

    await FilesystemCookieStore(path).install(jar(row()))

    assert path.stat().st_mode & 0o077 == 0, "the jar must not be group or world readable"


async def test_replacing_a_jar_leaves_no_temporary_behind(tmp_path: Path) -> None:
    """A stray copy of a credential is the same leak, one directory over."""
    store = FilesystemCookieStore(tmp_path / "cookies.txt")

    await store.install(jar(row()))
    await store.install(jar(row(name="ct0")))

    assert sorted(p.name for p in tmp_path.iterdir()) == ["cookies.txt"]


async def test_an_unconfigured_store_says_so_rather_than_crashing(tmp_path: Path) -> None:
    del tmp_path
    with pytest.raises(CookieStoreUnavailableError):
        await FilesystemCookieStore(None).install(jar(row()))


async def test_discarding_removes_the_file(tmp_path: Path) -> None:
    path = tmp_path / "cookies.txt"
    store = FilesystemCookieStore(path)
    await store.install(jar(row()))

    assert await store.discard() is True
    assert not path.exists()
    assert await store.discard() is False


async def test_describe_reports_without_reading_secrets_out(tmp_path: Path) -> None:
    store = FilesystemCookieStore(tmp_path / "cookies.txt")
    await store.install(jar(row(domain=".x.com"), row(domain="tiktok.com", name="sid")))

    summary = await store.describe()

    assert summary is not None
    assert summary.domains == ("tiktok.com", "x.com"), "a leading dot is not a different site"
    assert summary.cookie_count == 2


# -- Parsing -----------------------------------------------------------------


def test_http_only_cookies_are_counted() -> None:
    """The `#HttpOnly_` prefix looks like a comment and is not one.

    Skipping it would silently drop the session cookies that matter most, and
    the jar would look valid while being useless.
    """
    parsed = parse("#HttpOnly_x.com\tTRUE\t/\tTRUE\t0\tauth_token\tvalue")

    assert parsed.cookie_count == 1
    assert parsed.domains == ("x.com",)


def test_comments_and_blank_lines_are_ignored() -> None:
    parsed = parse("# a comment\n\n   \n" + row() + "\n")

    assert parsed.cookie_count == 1


def test_a_session_cookie_reports_no_expiry() -> None:
    parsed = parse(row(expiry=0))

    assert parsed.cookie_count == 1
    assert parsed.earliest_expiry is None


def test_the_earliest_expiry_is_the_one_reported() -> None:
    """It is when the jar starts failing, not when it finishes failing."""
    soon = int((datetime.now(UTC) + timedelta(days=2)).timestamp())
    parsed = parse(row(expiry=FUTURE) + "\n" + row(name="ct0", expiry=soon))

    assert parsed.earliest_expiry is not None
    assert (parsed.earliest_expiry - datetime.now(UTC)).days <= 2


def test_a_nonsense_expiry_does_not_reject_the_jar() -> None:
    """Exporters write year-9999 sentinels; that is not a reason to refuse."""
    parsed = parse("x.com\tTRUE\t/\tTRUE\t99999999999999\tauth_token\tvalue")

    assert parsed.cookie_count == 1
