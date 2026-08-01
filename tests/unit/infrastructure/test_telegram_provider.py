"""The Telegram delivery provider translates in both directions."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mediahub.application.delivery.errors import (
    ArtifactTooLargeError,
    DeliveryAuthenticationError,
    DeliveryProviderError,
    DeliveryQuotaExceededError,
    DeliveryRateLimitedError,
    ProviderUnavailableError,
    ReferenceNotUsableError,
    TargetUnreachableError,
)
from mediahub.application.delivery.ports import (
    DeliveryKind,
    DeliveryProgress,
    DeliveryRequest,
    DeliveryStage,
    DeliveryTarget,
    RemoteArtifactRef,
    ResendRequest,
    TargetAddress,
)
from mediahub.application.workspace.ports import ArtifactRole
from mediahub.domain.common.fingerprint import HashAlgorithm
from mediahub.infrastructure.delivery.telegram.client import PythonTelegramBotClient
from mediahub.infrastructure.delivery.telegram.errors import classify, extract_retry_after
from mediahub.infrastructure.delivery.telegram.provider import (
    BOT_API_MAX_BYTES,
    LOCAL_API_MAX_BYTES,
    MAX_CAPTION_LENGTH,
    TelegramDeliveryProvider,
)
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from tests.support.telegram_fakes import FakeUploader

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mediahub.application.workspace.ports import WorkspaceScope

pytestmark = pytest.mark.unit

CLIP_BYTES = 2048


@pytest.fixture
def scope(tmp_path: Path) -> Iterator[WorkspaceScope]:
    workspace = FilesystemWorkspace(tmp_path / "ws")
    with workspace.lease(label="delivery") as leased:
        leased.path_for("clip.mp4").write_bytes(b"x" * CLIP_BYTES)
        leased.path_for("clip.jpg").write_bytes(b"x" * 64)
        yield leased


def a_target(chat: str = "4242") -> DeliveryTarget:
    return DeliveryTarget(
        provider="telegram",
        address=TargetAddress(provider="telegram", opaque={"chat": chat}),
    )


def a_request(scope: WorkspaceScope, **overrides: object) -> DeliveryRequest:
    defaults: dict[str, object] = {
        "target": a_target(),
        # Resolved only when not overridden: a lease that holds a different
        # file has no ``clip.mp4`` to take a reference to.
        "artifact": overrides.pop("artifact", None) or scope.artifact("clip.mp4"),
        "kind": DeliveryKind.VIDEO,
        "caption": "A Test Video",
        "duration_seconds": 125,
        "width": 1920,
        "height": 1080,
    }
    defaults.update(overrides)
    return DeliveryRequest(**defaults)  # type: ignore[arg-type]


class TestCapabilities:
    def test_reports_the_public_ceiling_by_default(self) -> None:
        capabilities = TelegramDeliveryProvider(FakeUploader(), bot_principal="1").capabilities()

        assert capabilities.maximum_file_size == BOT_API_MAX_BYTES
        assert capabilities.can_serve_back, "Telegram keeps the bytes; that is the point"
        assert capabilities.supports_resend
        assert capabilities.supports_thumbnails
        assert capabilities.supports_metadata
        assert capabilities.supports_history

    def test_declares_honestly_what_it_cannot_do(self) -> None:
        capabilities = TelegramDeliveryProvider(FakeUploader(), bot_principal="1").capabilities()

        # The client library buffers the whole file before uploading, and a bot
        # cannot reliably delete arbitrary messages. Claiming either would make
        # a future feature quietly wrong.
        assert not capabilities.supports_streaming
        assert not capabilities.supports_delete
        assert not capabilities.supports_large_files

    def test_a_local_api_server_raises_the_ceiling(self) -> None:
        capabilities = TelegramDeliveryProvider(
            FakeUploader(), bot_principal="1", local_api_server=True
        ).capabilities()

        assert capabilities.maximum_file_size == LOCAL_API_MAX_BYTES
        assert capabilities.supports_large_files

    def test_supports_only_its_own_targets(self) -> None:
        provider = TelegramDeliveryProvider(FakeUploader(), bot_principal="1")

        assert provider.supports(a_target())
        assert not provider.supports(
            DeliveryTarget(provider="s3", address=TargetAddress(provider="s3"))
        )
        assert not provider.supports(
            DeliveryTarget(provider="telegram", address=TargetAddress(provider="telegram"))
        )


class TestDelivery:
    async def test_uploads_and_returns_a_full_receipt(self, scope: WorkspaceScope) -> None:
        uploader = FakeUploader()
        provider = TelegramDeliveryProvider(uploader, bot_principal="777")

        receipt = await provider.deliver(a_request(scope), scope)

        assert receipt.provider == "telegram"
        assert receipt.size_bytes == CLIP_BYTES
        assert receipt.provider_asset_id == "FILE-ABC"
        assert receipt.reference.remote_unique_id == "UNIQ-ABC"
        assert (
            receipt.reference.principal == "777"
        ), "a reference is only usable by the bot that made it"
        assert receipt.provider_message_id == "500"
        assert receipt.can_serve_back
        assert receipt.delivery_time.total_seconds() >= 0
        assert receipt.reused_reference is False

    async def test_the_receipt_carries_a_real_checksum(self, scope: WorkspaceScope) -> None:
        provider = TelegramDeliveryProvider(FakeUploader(), bot_principal="1")

        receipt = await provider.deliver(a_request(scope), scope)

        assert receipt.checksum is not None
        assert receipt.checksum.algorithm is HashAlgorithm.SHA256
        assert receipt.checksum.digest == hashlib.sha256(b"x" * CLIP_BYTES).hexdigest()

    async def test_reports_progress_while_uploading(self, scope: WorkspaceScope) -> None:
        seen: list[DeliveryProgress] = []
        provider = TelegramDeliveryProvider(FakeUploader(read_size=256), bot_principal="1")

        await provider.deliver(a_request(scope), scope, on_progress=seen.append)

        stages = [update.stage for update in seen]
        assert stages[0] is DeliveryStage.PREPARING
        assert DeliveryStage.UPLOADING in stages
        assert stages[-1] is DeliveryStage.COMPLETED
        uploading = [u for u in seen if u.stage is DeliveryStage.UPLOADING]
        assert [u.sent_bytes for u in uploading] == sorted(u.sent_bytes for u in uploading)
        assert uploading[-1].percentage == 100.0

    async def test_passes_presentation_hints_through(self, scope: WorkspaceScope) -> None:
        uploader = FakeUploader()
        provider = TelegramDeliveryProvider(uploader, bot_principal="1")

        await provider.deliver(
            a_request(scope, thumbnail=scope.artifact("clip.jpg", role=ArtifactRole.THUMBNAIL)),
            scope,
        )

        upload = uploader.uploads[0]
        assert upload["kind"] == "video"
        assert upload["duration_seconds"] == 125
        assert upload["width"] == 1920
        assert upload["thumbnail"] is not None

    async def test_a_long_caption_is_truncated(self, scope: WorkspaceScope) -> None:
        uploader = FakeUploader()
        provider = TelegramDeliveryProvider(uploader, bot_principal="1")

        await provider.deliver(a_request(scope, caption="x" * 5000), scope)

        caption = uploader.uploads[0]["caption"]
        assert caption is not None
        assert len(caption) <= MAX_CAPTION_LENGTH

    async def test_something_over_the_ceiling_is_refused_before_uploading(
        self, scope: WorkspaceScope
    ) -> None:
        uploader = FakeUploader()
        provider = TelegramDeliveryProvider(uploader, bot_principal="1")
        oversized = replace(scope.artifact("clip.mp4"), size_bytes=BOT_API_MAX_BYTES + 1)

        with pytest.raises(ArtifactTooLargeError) as excinfo:
            await provider.deliver(a_request(scope, artifact=oversized), scope)

        assert excinfo.value.limit_bytes == BOT_API_MAX_BYTES
        assert uploader.uploads == []

    async def test_a_large_file_is_read_in_chunks(self, tmp_path: Path) -> None:
        # Proof that a big artifact does not become one enormous read: the
        # measuring wrapper reports many observations, which is also what makes
        # progress possible at all.
        workspace = FilesystemWorkspace(tmp_path / "big")
        seen: list[DeliveryProgress] = []
        with workspace.lease(label="big") as scope:
            scope.path_for("big.mp4").write_bytes(b"y" * (4 * 1024 * 1024))
            provider = TelegramDeliveryProvider(
                FakeUploader(read_size=1024 * 1024), bot_principal="1"
            )

            receipt = await provider.deliver(
                a_request(scope, artifact=scope.artifact("big.mp4")),
                scope,
                on_progress=seen.append,
            )

        assert receipt.size_bytes == 4 * 1024 * 1024
        uploading = [u for u in seen if u.stage is DeliveryStage.UPLOADING]
        assert len(uploading) >= 4

    async def test_a_target_without_a_conversation_is_refused(self, scope: WorkspaceScope) -> None:
        provider = TelegramDeliveryProvider(FakeUploader(), bot_principal="1")
        request = a_request(
            scope,
            target=DeliveryTarget(provider="telegram", address=TargetAddress(provider="telegram")),
        )

        with pytest.raises(DeliveryProviderError):
            await provider.deliver(request, scope)

    async def test_error_messages_never_carry_a_path(self, scope: WorkspaceScope) -> None:
        uploader = FakeUploader(error=RuntimeError(f"failed writing {scope.directory()}"))
        provider = TelegramDeliveryProvider(uploader, bot_principal="1")

        with pytest.raises(DeliveryProviderError) as excinfo:
            await provider.deliver(a_request(scope), scope)

        assert str(scope.directory()) not in excinfo.value.message


class TestResend:
    def _reference(self, principal: str = "777") -> RemoteArtifactRef:
        return RemoteArtifactRef(
            provider="telegram",
            principal=principal,
            remote_id="FILE-ABC",
            remote_unique_id="UNIQ-ABC",
        )

    async def test_reuses_the_reference_and_moves_no_bytes(self) -> None:
        uploader = FakeUploader()
        provider = TelegramDeliveryProvider(uploader, bot_principal="777")

        receipt = await provider.resend(
            ResendRequest(target=a_target(), reference=self._reference(), kind=DeliveryKind.VIDEO)
        )

        assert uploader.uploads == [], "a re-send must not upload"
        assert uploader.resends[0]["reference"] == "FILE-ABC"
        assert receipt.reused_reference is True
        assert receipt.size_bytes == 0
        assert receipt.checksum is None
        assert receipt.provider_asset_id == "FILE-ABC"

    async def test_a_reference_from_another_bot_is_refused(self) -> None:
        uploader = FakeUploader()
        provider = TelegramDeliveryProvider(uploader, bot_principal="777")

        with pytest.raises(ReferenceNotUsableError) as excinfo:
            await provider.resend(
                ResendRequest(target=a_target(), reference=self._reference("999"))
            )

        assert not excinfo.value.is_retryable
        assert uploader.resends == []

    async def test_transport_failures_are_classified(self) -> None:
        provider = TelegramDeliveryProvider(
            FakeUploader(error=RuntimeError("Bad Gateway")), bot_principal="777"
        )

        with pytest.raises(ProviderUnavailableError):
            await provider.resend(ResendRequest(target=a_target(), reference=self._reference()))


class TestClientConstruction:
    """No network: constructing the client only builds an object."""

    def test_it_talks_to_telegram_by_default(self) -> None:
        client = PythonTelegramBotClient("123:ABC")

        assert client._bot.base_url.startswith("https://api.telegram.org/bot")

    def test_a_local_api_server_is_honoured(self) -> None:
        # This is the wiring behind ``supports_large_files``: get the base URL
        # wrong and the bot silently keeps talking to Telegram with a 50 MB
        # ceiling while the capabilities claim 2 GB.
        client = PythonTelegramBotClient("123:ABC", api_base_url="http://local-api:8081/")

        assert client._bot.base_url == "http://local-api:8081/bot123:ABC"


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("Unauthorized", DeliveryAuthenticationError),
            ("Invalid token", DeliveryAuthenticationError),
            ("Too Many Requests: retry after 12", DeliveryRateLimitedError),
            ("Flood control exceeded", DeliveryRateLimitedError),
            ("Storage quota exceeded", DeliveryQuotaExceededError),
            ("Forbidden: bot was blocked by the user", TargetUnreachableError),
            ("Bad Request: chat not found", TargetUnreachableError),
            ("Bad Gateway", ProviderUnavailableError),
            ("Timed out", ProviderUnavailableError),
            ("something nobody has seen", DeliveryProviderError),
        ],
    )
    def test_messages_map_to_the_right_class(self, message: str, expected: type[Exception]) -> None:
        assert isinstance(classify(RuntimeError(message)), expected)

    @pytest.mark.parametrize(
        ("message", "retryable"),
        [
            ("Unauthorized", False),
            ("Too Many Requests", True),
            ("Storage quota exceeded", False),
            ("chat not found", False),
            ("Bad Gateway", True),
            ("Request Entity Too Large", False),
            ("???", True),
        ],
    )
    def test_each_class_declares_the_right_retryability(
        self, message: str, retryable: bool
    ) -> None:
        assert classify(RuntimeError(message)).is_retryable is retryable

    def test_request_entity_too_large_is_a_policy_refusal(self) -> None:
        assert isinstance(classify(RuntimeError("Request Entity Too Large")), ArtifactTooLargeError)

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("HTTP 413 returned by the server", ArtifactTooLargeError),
            ("Client error '429 Too Many Requests'", DeliveryRateLimitedError),
            ("upstream said 502", ProviderUnavailableError),
        ],
    )
    def test_a_standalone_status_code_is_enough(
        self, message: str, expected: type[Exception]
    ) -> None:
        assert isinstance(classify(RuntimeError(message)), expected)

    @pytest.mark.parametrize(
        "message",
        [
            # A lease identifier, a temporary path and a byte count all carry
            # digits. Reading "413" out of one of them would turn a transport
            # blip into a permanent size refusal the user cannot act on.
            "failed writing lease e0e413caa2e434b26923df3a18259859e",
            "failed writing /tmp/ws/a413b/clip.mp4",
            "connection dropped after 413429 bytes",
            "chunk 1.413 of the upload",
        ],
    )
    def test_digits_inside_other_text_are_not_status_codes(self, message: str) -> None:
        error = classify(RuntimeError(message))

        assert type(error) is DeliveryProviderError
        assert error.is_retryable, "an unrecognised failure gets one honest attempt later"

    def test_retry_after_is_obeyed(self) -> None:
        error = classify(RuntimeError("Too Many Requests: retry after 12"))

        assert error.retry_after_seconds == 12.0

    def test_already_typed_errors_pass_through(self) -> None:
        original = ArtifactTooLargeError(1, 2)

        assert classify(original) is original

    def test_wrapped_causes_are_inspected(self) -> None:
        outer = RuntimeError("upload failed")
        outer.__cause__ = RuntimeError("chat not found")

        assert isinstance(classify(outer), TargetUnreachableError)

    def test_the_detail_is_included_but_never_a_path(self) -> None:
        error = classify(RuntimeError("Bad Gateway"), detail="video")

        assert "video" in error.message
        assert "/" not in error.message

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("retry after 5", 5.0), ("Retry-After: 30", 30.0), ("nothing", None)],
    )
    def test_retry_hints_are_extracted(self, text: str, expected: float | None) -> None:
        assert extract_retry_after(text) == expected
