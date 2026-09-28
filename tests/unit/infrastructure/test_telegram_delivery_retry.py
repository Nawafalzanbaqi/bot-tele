"""One retry of a delivery, and only when it cannot deliver twice.

A network blip at the very last step used to discard a download that had taken
minutes, because the lease is released on any failure. The retry exists for
that. Its limits exist because the alternative failure is worse: a file that
arrives twice because the first send actually landed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mediahub.application.delivery.errors import (
    DeliveryRateLimitedError,
    ProviderUnavailableError,
    TargetUnreachableError,
)
from mediahub.application.delivery.ports import (
    DeliveryKind,
    DeliveryRequest,
    DeliveryTarget,
    TargetAddress,
)
from mediahub.infrastructure.delivery.telegram import provider as provider_module
from mediahub.infrastructure.delivery.telegram.errors import classify, is_safe_to_retry
from mediahub.infrastructure.delivery.telegram.provider import (
    LOCAL_API_MAX_BYTES,
    TelegramDeliveryProvider,
)
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from tests.support.telegram_fakes import FakeUploader

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mediahub.application.workspace.ports import WorkspaceScope

pytestmark = pytest.mark.unit


class FlakyUploader(FakeUploader):
    """Fails with the queued errors first, then behaves like the fake."""

    def __init__(self, *failures: Exception) -> None:
        super().__init__()
        self.failures = list(failures)

    async def send_media(self, **kwargs: Any) -> Any:
        if self.failures:
            raise self.failures.pop(0)
        return await super().send_media(**kwargs)


@pytest.fixture
def scope(tmp_path: Path) -> Iterator[WorkspaceScope]:
    workspace = FilesystemWorkspace(tmp_path / "ws")
    with workspace.lease(label="delivery") as leased:
        leased.path_for("clip.mp4").write_bytes(b"x" * 4096)
        yield leased


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the pauses the provider asks for instead of taking them."""
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(
        "mediahub.infrastructure.delivery.telegram.provider.asyncio.sleep", fake_sleep
    )
    return delays


def request_for(scope: WorkspaceScope) -> DeliveryRequest:
    return DeliveryRequest(
        target=DeliveryTarget(
            provider="telegram", address=TargetAddress(provider="telegram", opaque={"chat": "1"})
        ),
        artifact=scope.artifact("clip.mp4"),
        kind=DeliveryKind.VIDEO,
        caption="clip",
    )


def local_provider(uploader: FakeUploader) -> TelegramDeliveryProvider:
    return TelegramDeliveryProvider(uploader, bot_principal="1", local_api_server=True)


class TestTheCeiling:
    def test_matches_what_a_local_server_actually_accepts(self) -> None:
        assert LOCAL_API_MAX_BYTES == 2000 * 1024 * 1024


class TestWhatIsSafe:
    @pytest.mark.parametrize(
        "message",
        [
            "NetworkError: httpx.ConnectError: [Errno 111] Connection refused",
            "Network is unreachable",
            "Temporary failure in name resolution",
        ],
    )
    def test_a_failure_before_the_request_is_safe(self, message: str) -> None:
        exc = ConnectionError(message)
        assert is_safe_to_retry(classify(exc), exc)

    def test_an_explicit_rate_limit_is_safe(self) -> None:
        exc = RuntimeError("Flood control exceeded. Retry in 3 seconds")
        error = classify(exc)
        assert isinstance(error, DeliveryRateLimitedError)
        assert is_safe_to_retry(error, exc)

    @pytest.mark.parametrize(
        "message",
        [
            "Timed out",
            "The write operation timed out",
            "Connection reset by peer",
            "Bad Gateway",
        ],
    )
    def test_an_ambiguous_failure_is_not_retried(self, message: str) -> None:
        """The destination may have taken the request; a second send could double it."""
        exc = RuntimeError(message)
        assert not is_safe_to_retry(classify(exc), exc)

    def test_a_final_refusal_is_not_retried(self) -> None:
        exc = RuntimeError("Forbidden: bot was blocked by the user")
        error = classify(exc)
        assert isinstance(error, TargetUnreachableError)
        assert not is_safe_to_retry(error, exc)


class TestTheRetry:
    async def test_a_refused_connection_is_retried_once(
        self, scope: WorkspaceScope, no_sleep: list[float]
    ) -> None:
        uploader = FlakyUploader(ConnectionError("httpx.ConnectError: Connection refused"))

        receipt = await local_provider(uploader).deliver(request_for(scope), scope)

        assert receipt.message is not None
        assert receipt.message.message_id == "500"
        assert len(uploader.uploads) == 1
        assert no_sleep == [provider_module.RETRY_DELAY_SECONDS]

    async def test_the_destinations_own_delay_is_honoured(
        self, scope: WorkspaceScope, no_sleep: list[float]
    ) -> None:
        uploader = FlakyUploader(RuntimeError("Flood control exceeded. Retry in 7 seconds"))

        await local_provider(uploader).deliver(request_for(scope), scope)

        assert no_sleep == [7.0]

    async def test_an_absurd_delay_is_capped(
        self, scope: WorkspaceScope, no_sleep: list[float]
    ) -> None:
        uploader = FlakyUploader(RuntimeError("Flood control exceeded. Retry in 900 seconds"))

        await local_provider(uploader).deliver(request_for(scope), scope)

        assert no_sleep == [provider_module.MAX_RETRY_DELAY_SECONDS]

    async def test_only_one_retry(self, scope: WorkspaceScope, no_sleep: list[float]) -> None:
        uploader = FlakyUploader(
            ConnectionError("Connection refused"), ConnectionError("Connection refused")
        )

        with pytest.raises(ProviderUnavailableError):
            await local_provider(uploader).deliver(request_for(scope), scope)

        assert uploader.uploads == []
        assert len(no_sleep) == 1

    async def test_a_timeout_is_never_retried(
        self, scope: WorkspaceScope, no_sleep: list[float]
    ) -> None:
        uploader = FlakyUploader(TimeoutError("The write operation timed out"))

        with pytest.raises(ProviderUnavailableError):
            await local_provider(uploader).deliver(request_for(scope), scope)

        assert no_sleep == []

    async def test_the_buffered_public_api_path_is_not_retried(
        self, scope: WorkspaceScope, no_sleep: list[float]
    ) -> None:
        """Without a path to re-send there is a consumed stream to rewind; not attempted."""
        uploader = FlakyUploader(ConnectionError("Connection refused"))
        provider = TelegramDeliveryProvider(uploader, bot_principal="1", local_api_server=False)

        with pytest.raises(ProviderUnavailableError):
            await provider.deliver(request_for(scope), scope)

        assert no_sleep == []
