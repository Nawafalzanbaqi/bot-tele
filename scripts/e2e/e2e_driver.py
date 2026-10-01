"""mediahub end-to-end driver: the bot's real acquisition pipeline, one link at a time.

Runs inside the mediahub image with the telegram service's environment and volumes
(``docker compose run``). It builds the same composition root as
``mediahub.presentation.telegram.__main__`` - container, Bot API client, Telegram
delivery provider, router, ProbeSource and AcquireMedia - and leaves out only the
poll loop, so the live gateway keeps its getUpdates session. For every link it runs
probe -> AUTO quality -> acquire (workspace lease, yt-dlp/gallery-dl fetch, ffprobe
completeness check, upload by path through the local Bot API, journal entry, lease
release) to the owner chat, exactly as a message in the chat would.

Usage: python e2e_driver.py <links.json> <results.jsonl> [--probe-only]
links.json: [{"platform": "...", "kind": "...", "url": "..."}, ...]
Results: one JSON object per link. Never prints the token.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path

from loguru import logger

from mediahub.application.common.errors import ApplicationError
from mediahub.application.delivery.ports import DeliveryTarget, TargetAddress
from mediahub.application.download.dto import AcquireMediaCommand, ProbeSourceQuery
from mediahub.application.download.quality import AUTO_KEY
from mediahub.domain.common.errors import DomainError
from mediahub.infrastructure.delivery.telegram.client import PythonTelegramBotClient
from mediahub.infrastructure.delivery.telegram.provider import TelegramDeliveryProvider
from mediahub.infrastructure.di.container import build_container
from mediahub.presentation.telegram import formatters
from mediahub.presentation.telegram.__main__ import HANDOFF_UMASK
from mediahub.shared.config.settings import get_settings
from mediahub.shared.logging.setup import configure_logging

CHAT = int(os.environ["E2E_CHAT"])
PRINCIPAL = f"telegram:{CHAT}"
WORKSPACE = Path(os.environ.get("MEDIAHUB_WORKSPACE__ROOT", "/data/workspace"))
SETTLE_SECONDS = float(os.environ.get("E2E_SETTLE_SECONDS", "8"))


def _workspace_entries() -> set[str]:
    try:
        return {p.name for p in WORKSPACE.iterdir()}
    except FileNotFoundError:
        return set()


async def run(links: list[dict[str, str]], out: Path, probe_only: bool) -> None:
    os.umask(HANDOFF_UMASK)
    settings = get_settings()
    configure_logging(settings)

    verified: dict[str, object] = {}

    def capture(message: object) -> None:
        record = message.record  # type: ignore[attr-defined]
        if record["message"] == "Delivered file verified complete":
            verified.clear()
            verified.update(record["extra"])

    logger.add(capture, level="INFO")

    container = build_container(settings)
    await container.prepare()
    telegram = settings.telegram
    client = PythonTelegramBotClient(
        telegram.bot_token.get_secret_value(), api_base_url=telegram.api_base_url
    )
    await client.start()
    provider = TelegramDeliveryProvider(
        client, bot_principal=telegram.bot_id, local_api_server=telegram.uses_local_api_server
    )
    router = container.delivery_router(provider)
    probe_source = container.probe_source_use_case()
    acquire_media = container.acquire_media_use_case(router)

    try:
        with out.open("a", encoding="utf-8") as sink:
            for link in links:
                row: dict[str, object] = {
                    "platform": link["platform"],
                    "kind": link["kind"],
                    "url": link["url"],
                    "started_at": time.strftime("%H:%M:%S"),
                }
                before = _workspace_entries()
                verified.clear()
                t0 = time.monotonic()
                try:
                    summary = await probe_source.execute(ProbeSourceQuery(url=link["url"]))
                    row["probe_seconds"] = round(time.monotonic() - t0, 1)
                    row["title"] = summary.title[:80]
                    row["provider"] = summary.provider
                    row["qualities"] = [q.label for q in summary.qualities][:8]
                    row["is_playlist"] = summary.is_playlist
                    row["is_live"] = summary.is_live
                    if probe_only:
                        row["result"] = "probed"
                    else:
                        result = await acquire_media.execute(
                            AcquireMediaCommand(
                                url=summary.url,
                                quality_key=AUTO_KEY,
                                target=DeliveryTarget(
                                    provider="telegram",
                                    address=TargetAddress(provider="telegram", opaque={"chat": CHAT}),
                                    label="هذه المحادثة",
                                ),
                                requested_by=PRINCIPAL,
                                caption=summary.title,
                            )
                        )
                        stages = result.stages
                        row.update(
                            result="delivered",
                            quality=result.quality_label,
                            bytes=result.bytes_delivered,
                            items=result.items_delivered,
                            message_id=result.message_id,
                            egress=result.egress,
                            via_proxy=result.via_proxy,
                            capped_from=result.capped_from,
                            sent_as_document=result.sent_as_document,
                            local_copy_released=result.local_copy_released,
                            stage_probe=None if stages is None else round(stages.probe_seconds, 1),
                            stage_download=None if stages is None else round(stages.download_seconds, 1),
                            stage_deliver=None if stages is None else round(stages.deliver_seconds, 1),
                            video_codecs=verified.get("video"),
                            audio_codecs=verified.get("audio"),
                            size=verified.get("size"),
                            duration=verified.get("seconds"),
                        )
                        with_label = f"🧪 {link['platform']} · {link['kind']}\n"
                        await client.send_message(
                            chat_id=CHAT, text=with_label + formatters.render_delivered(result)
                        )
                except (DomainError, ApplicationError) as exc:
                    row["result"] = "failed"
                    row["code"] = exc.code
                    row["error"] = str(exc)[:240]
                    if not probe_only:
                        await client.send_message(
                            chat_id=CHAT,
                            text=f"🧪 {link['platform']} · {link['kind']}\n"
                            + formatters.render_error(exc.code),
                        )
                except Exception as exc:  # noqa: BLE001 - a test driver reports everything
                    row["result"] = "crashed"
                    row["error"] = f"{type(exc).__name__}: {exc}"[:300]
                    row["trace"] = traceback.format_exc()[-1200:]
                row["total_seconds"] = round(time.monotonic() - t0, 1)
                after = _workspace_entries()
                row["workspace_new_entries"] = sorted(after - before)
                row["workspace_deleted"] = not (after - before)
                sink.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                sink.flush()
                print("E2E", json.dumps({k: row.get(k) for k in ("platform", "kind", "result", "code", "quality", "total_seconds")}), file=sys.stderr, flush=True)
                if not probe_only:
                    await asyncio.sleep(SETTLE_SECONDS)
    finally:
        await client.close()
        await container.shutdown()


def main() -> None:
    links = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    probe_only = "--probe-only" in sys.argv[3:]
    asyncio.run(run(links, out, probe_only))


if __name__ == "__main__":
    main()
