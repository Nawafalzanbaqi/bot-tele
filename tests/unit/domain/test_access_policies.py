"""The allow-list and the permission table both deny by default."""

from __future__ import annotations

import pytest

from mediahub.domain.access.enums import Action, Role
from mediahub.domain.access.policies import AllowListPolicy, AuthorizationPolicy
from mediahub.domain.access.value_objects import ExternalIdentity, InvalidIdentityError

pytestmark = pytest.mark.unit


class TestExternalIdentity:
    def test_normalises_the_scheme(self) -> None:
        identity = ExternalIdentity(scheme="  TELEGRAM ", external_id=" 42 ")

        assert identity.scheme == "telegram"
        assert identity.external_id == "42"
        assert str(identity) == "telegram:42"

    @pytest.mark.parametrize(
        ("scheme", "external_id"),
        [("", "42"), ("telegram", ""), ("  ", "42"), ("telegram", "   ")],
    )
    def test_rejects_incomplete_identities(self, scheme: str, external_id: str) -> None:
        with pytest.raises(InvalidIdentityError):
            ExternalIdentity(scheme=scheme, external_id=external_id)

    def test_rejects_implausible_lengths(self) -> None:
        with pytest.raises(InvalidIdentityError):
            ExternalIdentity(scheme="telegram", external_id="x" * 500)

    def test_schemes_keep_identifiers_apart(self) -> None:
        telegram = ExternalIdentity(scheme="telegram", external_id="42")
        api = ExternalIdentity(scheme="api", external_id="42")

        assert telegram != api


class TestAllowList:
    def test_unknown_identities_get_nothing(self) -> None:
        policy = AllowListPolicy.from_ids(scheme="telegram", owner_ids=("1",))

        stranger = ExternalIdentity(scheme="telegram", external_id="999")
        assert policy.role_for(stranger) is None

    def test_known_identities_get_their_role(self) -> None:
        policy = AllowListPolicy.from_ids(
            scheme="telegram", owner_ids=("1",), member_ids=("2",), readonly_ids=("3",)
        )

        def role(external_id: str) -> Role | None:
            return policy.role_for(ExternalIdentity(scheme="telegram", external_id=external_id))

        assert role("1") is Role.OWNER
        assert role("2") is Role.MEMBER
        assert role("3") is Role.READONLY

    def test_the_more_permissive_listing_wins(self) -> None:
        policy = AllowListPolicy.from_ids(scheme="telegram", owner_ids=("7",), readonly_ids=("7",))

        assert policy.role_for(ExternalIdentity(scheme="telegram", external_id="7")) is Role.OWNER

    def test_ids_are_trimmed(self) -> None:
        policy = AllowListPolicy.from_ids(scheme="telegram", owner_ids=(" 42 ",))

        assert policy.role_for(ExternalIdentity(scheme="telegram", external_id="42")) is Role.OWNER

    def test_a_different_scheme_does_not_match(self) -> None:
        policy = AllowListPolicy.from_ids(scheme="telegram", owner_ids=("42",))

        assert policy.role_for(ExternalIdentity(scheme="api", external_id="42")) is None

    def test_an_empty_list_allows_nobody(self) -> None:
        policy = AllowListPolicy()

        assert policy.is_empty
        assert policy.role_for(ExternalIdentity(scheme="telegram", external_id="1")) is None


class TestAuthorization:
    @pytest.mark.parametrize("action", list(Action))
    def test_owner_may_do_everything(self, action: Action) -> None:
        assert AuthorizationPolicy().permits(Role.OWNER, action)

    @pytest.mark.parametrize(
        ("role", "action", "expected"),
        [
            (Role.MEMBER, Action.SUBMIT_SOURCE, True),
            (Role.MEMBER, Action.VIEW_HISTORY, True),
            (Role.READONLY, Action.VIEW_HISTORY, True),
            (Role.READONLY, Action.VIEW_SETTINGS, True),
            (Role.READONLY, Action.SUBMIT_SOURCE, False),
            (Role.READONLY, Action.CANCEL_ACQUISITION, False),
        ],
    )
    def test_the_table_is_the_rule(self, role: Role, action: Action, expected: bool) -> None:
        assert AuthorizationPolicy().permits(role, action) is expected
