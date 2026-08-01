# Failure Matrix

Every failure this deployment is expected to meet, what the system actually does
about it, and the test that proves it still does.

This is not a list of things that *could* go wrong. It is the list of things
that **do** go wrong on a Raspberry Pi plugged into a domestic socket, and each
row is backed by a test in `tests/failure/` or `tests/chaos/` that reproduces
the condition rather than describing it. A row without a test is a belief; a row
with one is a property.

Read the columns as: *what happened* → *what the system decides* → *what an
operator sees* → *where it is proven*.

---

## 1. Power and process loss

| Failure | Classification | Behaviour | Operator sees | Proven by |
| --- | --- | --- | --- | --- |
| Power cut during **probe** | crash | Nothing was checkpointed. The lease lapses, the job requeues, probing starts over. One attempt spent. | Job back in queue, `attempts` +1 | `test_power_loss.py::TestPowerLossBeforeAnythingIsCheckpointed` |
| Power cut during **download** | crash | Completed stages survive in the checkpoint. The lease directory is deleted with its partial file; the next attempt downloads again. | Job back in queue | `test_power_loss.py::TestPowerLossDuringEveryStage[download]` |
| Power cut during **verify** | crash | Verify re-runs. The artifact is gone with its lease, so verify rebuilds it by downloading. | Job back in queue | `…[verify]` |
| Power cut during **deliver** | crash | If no receipt was checkpointed, delivery is retried. If one was, it is never repeated. | Exactly one upload | `…[deliver]`, `test_nothing_is_delivered_twice` |
| Power cut during **cleanup** | crash | The receipt is already durable, so the resumed job deletes the local copy and completes without re-uploading. | Job succeeds, one upload | `TestPowerLossBetweenDeliveryAndCleanup` |
| `SIGKILL` / OOM kill / container stop timeout | crash | Indistinguishable from a power cut. Lease held, nothing settled, recovery by lapse. | Job `running` for up to one lease period | `test_process_failures.py::TestSigkill` |
| `SIGTERM` (deploy, restart) | graceful | Claiming stops, the current stage finishes, the job is checkpointed and **released**. The attempt is refunded. | Job back in queue, `attempts` unchanged | `TestGracefulShutdown::test_a_drained_job_is_handed_back_without_costing_an_attempt` |
| `SIGTERM` twice | graceful → immediate | The drain grace is abandoned. Remaining leases recover by lapsing. | Faster exit; jobs stalled one lease period | `test_a_second_signal_stops_waiting_for_the_drain` |
| Repeated crash on the same job | policy | Each crash costs one attempt. At `max_attempts` the job is failed and stops being claimed. | Job `failed`, stops consuming capacity | `TestRepeatedPowerLoss::test_a_job_that_dies_every_time_is_eventually_abandoned` |
| A claim loop dies without being asked to | defect | The supervisor notices, logs it with the slot number, drains and exits so the orchestrator restarts the process. | `ERROR` + process exit | `test_process_failures.py::TestSupervision` |

**The rule underneath all of these:** nothing recovers because shutdown was
polite. A graceful stop is an *optimisation* over what the lease already
guarantees, and every row above is exercised through the same recovery path.

---

## 2. Storage

| Failure | Classification | Behaviour | Operator sees | Proven by |
| --- | --- | --- | --- | --- |
| Disk full at lease admission | transient | Reservation refused before any work starts. No directory is created. | `insufficient_disk_space` | `test_storage_failures.py::TestDiskFull` |
| Disk full mid-write (`ENOSPC`) | transient | `ENOSPC`/`EDQUOT`/`EFBIG` are named at the point they happen, not left as an unknown error retried every 30 s forever. Nothing is published. | `insufficient_disk_space`, long backoff | `test_a_write_that_fills_the_device_is_named_not_guessed` |
| Disk full with a queue behind it | transient + backpressure | The first job is claimed, fails and requeues on the disk backoff. **Further claims are held off** until headroom returns, so 50 queued jobs do not spend 50 attempts learning the same fact. | One `ERROR`, then quiet; queue intact | `TestDiskFullDuringAJob::test_a_full_disk_does_not_spend_the_queue` |
| Space returns | — | The refusal clears on the next cycle and claiming resumes. | `INFO` "headroom recovered" | `test_claiming_resumes_once_space_returns` |
| Lease ceiling exceeded (a source that lied about its size) | policy | The write is refused at the ceiling, not after filling the card. Not treated as disk pressure — waiting would not help. | `workspace_quota_exceeded` | `test_the_lease_ceiling_stops_a_source_that_lied_about_its_size` |
| Emergency reserve | — | Never allocatable, so the store can still commit the transaction that records what went wrong. | — | `test_the_emergency_reserve_is_never_allocatable` |
| Filesystem remounted read-only | permanent (until fixed) | Leases cannot be opened; readiness reports **degraded** and the instance stops being sent traffic. | `503` on `/health/ready` | `test_health_degradation.py`, `TestReadOnlyFilesystem` |
| A lease that cannot be deleted | leak, reported | `close()` verifies removal. If the directory survives it returns **0 reclaimed** and logs an `ERROR` naming the path and the stranded bytes. It is then listed by `workspace.orphans()`. | `ERROR` + `usage().is_leaking` | `test_a_workspace_that_cannot_be_emptied_says_so` |
| Symlink planted inside a lease | attack / corruption | `verify_consistency()` refuses. Nothing is read or written through it. | `workspace_inconsistent` | `TestWorkspaceCorruption` |
| Lease directory vanished mid-job | corruption | Same check, same refusal. | `workspace_inconsistent` | `test_a_lease_whose_directory_vanished_fails_loudly` |
| Truncated / empty `lease.json` | corruption | Reads as **unclaimed** rather than as a guess. Manifests are now `fsync`ed before the rename that publishes them, so this state is far harder to reach in the first place. | Directory swept at next restart | `test_a_truncated_manifest_reads_as_unclaimed_rather_than_as_a_guess` |
| Manifest from a future schema version | corruption | Not interpreted. Guessing here deletes somebody's download. | Directory left alone | `test_a_manifest_from_a_future_version_is_not_interpreted` |
| One corrupt lease among many | corruption | The sweep reports everything under the root, readable or not, and keeps going. A sweep that one bad file can stop is a sweep that stops running. | — | `test_a_corrupt_lease_does_not_stop_the_sweep_finding_the_others` |
| Interrupted manifest write (staging debris) | leak | `manifest.clear_staging()` removes it; the real manifest is untouched. | — | `test_staging_debris_from_an_interrupted_write_can_be_cleared` |

---

## 3. Network, source and destination

| Failure | Classification | Behaviour | Operator sees | Proven by |
| --- | --- | --- | --- | --- |
| Network unreachable during probe | transient | Probe is retried inside the engine (cheap, idempotent), then the job requeues. | Job queued | `test_runtime_failures.py::TestNetworkDisconnect` |
| Connection reset mid-download | transient | The attempt fails and requeues. **The transfer starts again from zero** — see the limitation below. | Job queued | `test_a_retry_after_a_disconnect_starts_the_transfer_again` |
| Source refuses permanently (private, removed) | permanent | Failed immediately. Retrying a dead link three times wastes three downloads to learn what the first refusal said. | Job `failed` | `TestDownloadEngineFailure` |
| Transfer exceeds its wall-clock budget | transient | `DownloadTimeoutError`, requeued. | Job queued | `test_a_transfer_that_exceeds_its_budget_is_retried` |
| Engine produces an empty file | permanent | Refused at **verify** as well as inside the engine adapter. A zero-byte file is never delivered to a person. | Job `failed`, zero uploads | `test_an_engine_that_produces_nothing_does_not_look_like_success` |
| Engine raises something unrecognised | transient | Classified as transient — one honest attempt later rather than written off — and the worker survives. | Job queued | `test_an_unrecognised_engine_error_is_transient_not_fatal` |
| Engine thread will not unwind | **leak, reported** | The drain budget (30 s) expires, the thread is counted as abandoned and an `ERROR` says the process has permanently lost pool capacity. | `ERROR`; `engine_thread_stats().abandoned > 0` | `test_resource_accounting.py::TestEngineThreadLedger` |
| Telegram upload times out | transient | Requeued. Completed stages are not repeated. | Job queued | `TestTelegramTimeout` |
| Telegram rate limit (`429`) | transient | The destination's own `retry_after` is obeyed exactly rather than guessed at. | Job queued | `test_a_rate_limit_is_obeyed_rather_than_guessed_at` |
| Chat deleted / bot blocked | permanent | Not retried. | Job `failed` | `test_a_conversation_that_no_longer_exists_is_not_retried` |
| Artifact larger than the destination accepts | policy | Refused before the upload starts, naming the real ceiling. | Job `failed`, `artifact_too_large` | `test_a_file_the_destination_cannot_accept_stops_immediately` |
| Destination fails repeatedly | transient + circuit | The registry's circuit breaker deprioritises it in favour of an alternative, and never blocks an attempt when there is no alternative. | Provider deprioritised for the cooldown | `tests/unit/infrastructure/test_delivery_registry.py` |

### Stated limitation: no cross-attempt download resume

A workspace lease belongs to **one attempt**. A partial file left by a dropped
connection is deleted along with the lease that held it, so the next attempt
starts from zero. Resumption is real *within* one fetch — the engine's own
fragment retries — and within one lease when a stage is re-entered.

This is a deliberate trade (it is what makes "the workspace never leaks" a
simple, checkable statement), but it has an operational consequence: on a slow
connection, `download.download_timeout_seconds` must comfortably exceed the time
to fetch `download.max_item_bytes`, or a large item can never finish. Both halves
are pinned by tests: `test_a_retry_after_a_disconnect_starts_the_transfer_again`
and `test_a_stage_re_run_inside_one_attempt_does_resume`.

---

## 4. Store, clock and memory

| Failure | Classification | Behaviour | Operator sees | Proven by |
| --- | --- | --- | --- | --- |
| Store busy / locked during a stage | transient | Costs the attempt, never the worker. | Job queued | `TestDatabaseBusy` |
| Store busy during a **heartbeat** | transient | The heartbeat logs and retries on the next tick. It must never die: a heartbeat that stops renewing while the job runs is how one job becomes two. | `WARNING`, lease intact | `test_a_heartbeat_that_cannot_reach_the_store_keeps_ticking` |
| Lease reclaimed while the job runs | — | The heartbeat surrenders immediately and the worker stops touching the job. Whoever owns it now is running it. | `WARNING` "lease lost" | `test_a_reclaimed_lease_does_stop_the_heartbeat` |
| **Clock steps backwards** (NTP corrects a Pi with no RTC) | handled | Negative elapsed time is read as "renew now" rather than "not yet". Without this the lease expires under a perfectly healthy job and the work runs twice — and nothing in the log looks like a clock problem. | `WARNING` naming the drift | `TestClockJumps::test_a_backward_step_does_not_stall_lease_renewal` |
| Clock steps backwards (progress) | handled | The throttle re-anchors instead of going silent for the duration of the jump. | Progress keeps flowing | `test_a_backward_step_re_anchors_progress_instead_of_muting_it` |
| Clock steps forwards | handled | At worst one extra lease renewal and one extra progress write. A stalled transfer still produces no flood, because a write also has to have something new to say. | — | `test_a_forward_step_does_not_produce_a_flood` |
| Transfer stalls | handled | Progress writes stop entirely rather than repeating the same number every interval for hours. The lease is still renewed, so liveness is unaffected. | Progress stops advancing | `TestStalledTransfers` |
| Low memory during upload | policy | The client library buffers the whole file; a ceiling turns what would be an OOM kill into a classified, non-retryable refusal. Chunked readers are never capped. | Job `failed`, `artifact_too_large` | `TestLowMemory` |
| Hashing a large artifact | bounded | Streamed at 1 MiB granularity: peak allocation is the chunk, not the file. | — | `test_hashing_a_large_file_is_bounded_by_the_chunk_not_the_file` |

### A note on "SQLite busy"

This build's persistence backends are PostgreSQL and the in-process store; the
SQLite adapter of the phase-02 design is **not built** (`ARCHITECTURE.md` §8).
The equivalent condition — the store refusing a write that would succeed on a
retry — is therefore exercised against the backend that actually ships. A test
written against an adapter nobody runs would prove nothing about this
deployment, and the `TestDatabaseBusy` rows above are the honest substitute.

---

## 5. Chaos: the invariants that hold regardless of ordering

`tests/chaos/test_random_disruption.py` runs many jobs through many disruptions
in seeded-random order (seeds `1, 7, 13, 29, 101` — fixed, so a red build is a
bug and not a coincidence) and asserts five properties:

1. **Every job reaches a terminal state.** Nothing is left running for ever.
2. **No job is delivered twice.** Uploads equal successes, exactly.
3. **No completed stage is repeated.**
4. **The workspace is empty when the dust settles.**
5. **The loop is still claiming.** A fresh job goes straight through afterwards.

Those five are the product. If a change breaks one of them, it does not matter
what else it improved.
