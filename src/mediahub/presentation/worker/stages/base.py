"""What a stage is, and what a stage may assume.

The executor knows three things about a stage: its name, that some object can
run it, and that running it either finishes or raises. Everything else - what
"download" means, which port it calls, what it writes - belongs to the handler
and is invisible from here. That is what lets the runtime be tested exhaustively
with handlers that do nothing at all.

A handler must:

* **be idempotent.** A job reclaimed mid-stage runs that stage again. A handler
  that cannot survive being run twice will corrupt work on the first crash.
* **honour the token.** Long work checks
  :attr:`~mediahub.application.common.cancellation.CancellationToken.cancelled`
  often enough to stop within about a second. Cancellation is cooperative
  everywhere in MediaHub; there is no safe way to kill work mid-write.
* **write only inside the lease** it is handed, and keep nothing after it.
* **raise typed errors.** Anything that reaches the executor is classified, and
  an unrecognised exception is treated as transient - which is safe, but says
  less than it could.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from mediahub.application.download.errors import StageHandlerMissingError
from mediahub.application.download.queue import JobStage, StageProgress

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Iterable, Sequence

    from mediahub.application.common.cancellation import CancellationToken
    from mediahub.application.download.queue import Checkpoint
    from mediahub.application.workspace.ports import WorkspaceScope
    from mediahub.domain.download.value_objects import JobId

DEFAULT_STAGE_PLAN: Final[tuple[JobStage, ...]] = (
    JobStage.PROBE,
    JobStage.DOWNLOAD,
    JobStage.VERIFY,
    JobStage.DELIVER,
    JobStage.CLEANUP,
)
"""The acquisition sequence, in the order ``docs/architecture/07-download-pipeline.md``
describes it. A deployment may run a shorter plan - a delivery-only worker, say -
but never a re-ordered one: verify cannot precede download."""


type ProgressSink = Callable[[StageProgress], None]
"""Where a handler reports what it sees.

Synchronous, because engine work often runs in a worker thread. It must be cheap
and must not raise: it describes the transfer, it does not perform it.
"""


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """What a handler has to say when it finishes.

    Attributes:
        resume_token: Opaque state the next attempt should be given if this job
            is reclaimed later. Only the handler that wrote it knows what it
            means; the runtime stores and returns it, and never reads it.
    """

    resume_token: str | None = None

    @classmethod
    def done(cls) -> StageOutcome:
        """Return the outcome of a stage with nothing to hand on."""
        return cls()


@dataclass(frozen=True, slots=True)
class StageContext:
    """Everything a handler is given, and the only things it may rely on.

    Attributes:
        job_id: Which job is running. Handlers load what they need through use
            cases rather than being handed a payload, so the runtime never
            carries business data (``docs/architecture/09-queue-architecture.md``
            §9.3).
        stage: Which stage this is.
        attempt: Which attempt of the job this is, for logging and for handlers
            that behave differently on a retry.
        checkpoint: What earlier stages recorded, including any resume token.
        workspace: The lease this attempt may write inside. It is deleted when
            the attempt ends, whatever the outcome.
        cancellation: The token to check while doing anything slow.
        report: Where to send progress.
    """

    job_id: JobId
    stage: JobStage
    attempt: int
    checkpoint: Checkpoint
    workspace: WorkspaceScope
    cancellation: CancellationToken
    report: ProgressSink

    def observe(
        self,
        *,
        transferred_bytes: int = 0,
        total_bytes: int | None = None,
        speed_bps: float | None = None,
        eta_seconds: float | None = None,
    ) -> None:
        """Report progress for this stage.

        A convenience over :attr:`report` so that a handler never has to name
        its own stage, and therefore cannot report someone else's.
        """
        self.report(
            StageProgress(
                stage=self.stage,
                transferred_bytes=transferred_bytes,
                total_bytes=total_bytes,
                speed_bps=speed_bps,
                eta_seconds=eta_seconds,
            )
        )


class StageHandler(Protocol):
    """Executes exactly one stage of a job."""

    @property
    def stage(self) -> JobStage:
        """Return the stage this handler implements."""
        ...

    async def execute(self, context: StageContext) -> StageOutcome:
        """Do the work of the stage.

        Raises:
            DownloadError: For anything that went wrong, classified so the
                caller's retry decision can be read from a field.
            DownloadCancelledError: If the token was set while working.
        """
        ...


class StageRegistry:
    """Which handler runs which stage.

    A plain mapping rather than discovery: the set of handlers a process runs is
    a deployment decision, and a worker that silently gained a stage because a
    module was imported would be impossible to reason about.
    """

    __slots__ = ("_handlers",)

    def __init__(self, handlers: Iterable[StageHandler] = ()) -> None:
        """Register each handler under the stage it declares.

        Later registrations replace earlier ones, so a deployment can override a
        built-in handler without removing it.
        """
        self._handlers: dict[JobStage, StageHandler] = {
            handler.stage: handler for handler in handlers
        }

    @property
    def stages(self) -> frozenset[JobStage]:
        """Return every stage this registry can run."""
        return frozenset(self._handlers)

    def for_stage(self, stage: JobStage) -> StageHandler:
        """Return the handler for ``stage``.

        Raises:
            StageHandlerMissingError: If nothing is registered for it. Permanent
                by classification: the next attempt would find the same gap.
        """
        handler = self._handlers.get(stage)
        if handler is None:
            raise StageHandlerMissingError(stage.value)
        return handler

    def missing_for(self, plan: Sequence[JobStage]) -> tuple[JobStage, ...]:
        """Return the stages of ``plan`` that have no handler.

        Used at startup so a misconfigured worker refuses to run rather than
        claiming a job it can only fail (``docs/architecture/10-worker-architecture.md``
        §10.5).
        """
        return tuple(stage for stage in plan if stage not in self._handlers)
