# Operational Runbook

What to do when something is wrong, written for the person who did not build it
and is reading this at an inconvenient hour.

Each entry follows the same shape: **what you see → what it means → what to do →
what not to do.** The "what not to do" lines are there because most of the ways
to make this system worse involve deleting something that was about to be
recovered.

The single most useful thing to know before reading further: **almost nothing
here requires urgent action.** Jobs are held in a queue, bytes are reclaimed by
context managers, and crashes are recovered by leases lapsing. The failures that
genuinely need a human are the ones about *storage* and *threads*, and they are
first below for that reason.

---

## Quick reference

| Symptom | Section |
| --- | --- |
| Nothing is downloading | [§1](#1-the-worker-is-not-claiming-work) |
| Disk is filling / full | [§2](#2-the-disk-is-full-or-filling) |
| `/health/ready` returns 503 | [§3](#3-readiness-is-degraded) |
| A job is stuck | [§4](#4-a-job-is-stuck) |
| The same file arrived twice | [§5](#5-something-was-delivered-twice) |
| Everything fails after months of working | [§6](#6-descriptor-or-thread-exhaustion) |
| Deploys lose in-flight work | [§7](#7-deploys-lose-work) |
| Telegram is silent | [§8](#8-the-telegram-gateway-is-silent) |
| Odd behaviour after a reboot | [§9](#9-clock-jumps) |

---

## 1. The worker is not claiming work

**What you see.** Jobs sit in `queued`. The process is running and answers its
liveness probe.

**Diagnose in this order.**

1. `grep "No workspace headroom" <log>` → **the disk is full.** Go to §2. This
   is by far the most common cause, and it is deliberate: the worker refuses to
   claim rather than spending every job's retry budget on the same full device.
2. `grep "Worker ready" <log>` → if absent, the worker never started. The
   refusal names the reason: no queue configured, no workspace, no destination,
   or no handler for a stage in the plan.
3. `grep "A claim loop ended without being asked to" <log>` → a slot died. The
   supervisor stops the process so the orchestrator restarts it; if it is *not*
   restarting, fix the restart policy.
4. Check `worker.enabled` and `download.enabled`. Both default to **false**, and
   an instance with them off simply does not claim.
5. Check the identity. Two workers with the same `host:role:index` release each
   other's leases at startup and can livelock each other. They must differ in
   `worker.index`.

**Do not** delete the workspace root to "unstick" it. If the cause is §2 you
have not fixed anything, and if a job is mid-flight you have just destroyed it.

---

## 2. The disk is full, or filling

**What you see.** `ERROR ... No workspace headroom above the emergency reserve;
refusing to claim` — **once**, not once per job.

**What it means.** Free space minus outstanding reservations minus the emergency
reserve has reached zero. The first job to meet this was claimed, failed as
transient `insufficient_disk_space`, and was requeued on a long backoff. Every
job after it has been left queued rather than failed.

**What to do.**

1. Find the space: `du -sh /data/workspace/*` and `df -h /data`.
2. If the workspace itself is large, look for **orphans** — leases this process
   owns on disk but is no longer holding:
   ```
   grep "could not be deleted and is now an orphan" <log>
   ```
   Each line names the directory and the stranded bytes.
3. Free space anywhere on the device. Claiming resumes within one idle poll
   (≤ 5 s) and logs `Workspace headroom recovered`.
4. If the workspace is full of orphans, **restart the worker**: the startup sweep
   reclaims them. This is the supported way to clear orphans.

**Do not** raise `min_free_bytes` to make the message go away. That reserve is
what keeps the store able to commit the transaction recording whatever went
wrong; spending it converts a clean refusal into a corrupt database.

**Do not** delete lease directories by hand while a worker is running. Use the
restart sweep, which knows which directories belong to a live process.

---

## 3. Readiness is degraded

**What you see.** `GET /health/ready` → `503`, body `{"status": "degraded", …}`.

**What it means.** One of exactly two dependencies failed:

* **the store did not answer** — `database: false` in the body; or
* **the workspace root is missing or not writable** — `database: true`, and an
  `ERROR` line says `The workspace root is missing or not writable`.

The second is the interesting one. It means an unmounted volume, or a filesystem
the kernel remounted read-only after an I/O error. An instance in that state
accepts every request and completes none of them.

**What to do.**

1. `mount | grep /data` — is the volume there?
2. `touch /data/workspace/.probe` — read-only filesystems fail here.
3. `dmesg | tail` — an SD card that has started refusing writes says so.
4. If the card is failing, replace it. Queued jobs are in the store, not on the
   card, and survive.

**Note.** A workspace that is merely **full** does *not* make readiness degraded.
That is deliberate: requests still queue correctly, and pulling a working API out
of service for a condition one `rm` fixes would be the wrong trade.

---

## 4. A job is stuck

**What you see.** A job in `running` that is not making progress.

**What it means, in order of likelihood.**

* **It is downloading something large and slow.** Progress writes stop entirely
  when a transfer stalls — that is the throttle refusing to write the same number
  every five seconds, not a fault. The lease is still being renewed.
* **It is in verify.** Hashing is one uninterruptible pass; on a Pi reading 2 GiB
  from an SD card that is ~70 s.
* **The worker died.** The job will requeue when the lease lapses — at most two
  lease periods (240 s on defaults).

**What to do.**

1. Wait `2 × lease_seconds`. Most "stuck" jobs resolve here.
2. Check the job's `updated_at` and the last progress observation.
3. If it never moves, the transfer will hit `download_timeout_seconds` (3600 s
   default) and fail as transient.
4. To stop it now, cancel it through the normal path. A **running** job stops at
   its next checkpoint; a **queued** job simply stops being offered.

**Do not** restart the worker to clear one job unless you want the other slots
drained too. Restarting is safe — it costs the job one attempt — it is just
heavier than waiting.

---

## 5. Something was delivered twice

**What you see.** A user reports receiving the same file twice.

**What it means.** This should not happen. The receipt is written in the *same*
checkpoint as the stage that earned it, so no reader can see one without the
other, and a delivery that has succeeded is never repeated.

The realistic cause is **two workers sharing one identity**, which lets each
release the other's lease and run the same job.

**What to do.**

1. `grep "Worker ready" <log>` on every host and compare the `worker` fields.
   Two entries with the same `host:role:index` is the bug.
2. Give them distinct `worker.index` values.
3. Check `lease_seconds` against `heartbeat_seconds`. If renewal cannot keep up,
   healthy jobs are reclaimed mid-flight. The validator enforces
   `heartbeat × 2 ≤ lease`, but a *saturated* worker can still miss ticks — look
   for `Lease was reclaimed while settling` in the log.
4. Check for backward clock steps (§9), which used to cause exactly this before
   they were handled.

---

## 6. Descriptor or thread exhaustion

**What you see.** After weeks or months of working: everything starts failing at
once, often with `OSError: [Errno 24] Too many open files`, and the logging that
would explain it fails too.

**What it means.** A resource that leaks slowly has run out. There are two:

* **File descriptors.** Bounded and tested (`tests/benchmarks`), so a leak here
  would be a new bug.
* **Engine threads.** A yt-dlp thread that will not unwind cannot be killed. It
  holds a pool slot for the life of the process, and enough of them means no
  download can ever start again — silently, with the process still looking
  healthy.

**What to do.**

1. `grep "did not unwind within its drain budget" <log>`. Every line is one
   permanently lost pool slot.
2. **Restart the worker.** This is the only recovery; nothing can force a Python
   thread to stop.
3. Look at what wedged it — the log line names the URL's host and the reason
   (`timeout` or `cancelled`). A source that reliably wedges the extractor is
   worth blocking.
4. If this recurs, schedule a periodic restart. It costs one lease period.

The abandoned count is also reported at shutdown (`Engine threads were abandoned
during this process's life`), so a clean restart still tells you it happened.

---

## 7. Deploys lose work

**What you see.** Every deploy leaves jobs stalled for a couple of minutes, and
`attempts` climbs on jobs nobody retried.

**What it means.** The graceful drain is not running. The process is being killed
while it is still politely winding down, so instead of *releasing* jobs
(attempt refunded) they are recovered by lease expiry (attempt spent).

**What to do.**

1. Compare the orchestrator's kill timeout with `worker.drain_grace_seconds`.
   The kill timeout must be **larger**. Compose defaults to 10 s; the shipped
   `docker-compose.yml` sets `stop_grace_period: 45s` against a 30 s drain.
2. Confirm the polite path runs: after a `SIGTERM` you should see
   `Stop requested; draining`, then `Claim loop stopped`, then `Worker stopped`.
   If the process dies before `Worker stopped`, the timeout is too short.
3. If you see `Drain grace expired; the remaining leases will be recovered when
   they lapse`, a stage would not wind down in time — usually verify on a large
   artifact. Either raise the grace (and the kill timeout with it) or accept
   lease recovery.

**Do not** raise `drain_grace_seconds` without raising `stop_grace_period` too.
That makes the problem strictly worse.

---

## 8. The Telegram gateway is silent

**What you see.** The bot does not answer.

**Diagnose.**

1. `grep "Polling failed; backing off" <log>` → Telegram is unreachable. The loop
   backs off 5 s and keeps trying; the API and any running downloads are
   unaffected. This is weather, not an incident.
2. **Check the allow-list.** Deny by default. A user not in `owner_ids`,
   `member_ids` or `readonly_ids` is ignored — which is correct, and looks
   identical to being broken.
3. `grep "Telegram gateway started"` → if absent, `telegram.enabled` is false, or
   the token is missing (both are refused at boot with a reason).
4. Delivery failures are separate from gateway failures. `429` rate limits are
   obeyed using Telegram's own `retry_after`; a deleted chat is permanent and
   fails the job rather than retrying.

---

## 9. Clock jumps

**What you see.** Odd timings just after a reboot: a burst of lease renewals, a
`WARNING` saying `The clock stepped backwards`.

**What it means.** A Raspberry Pi has no battery-backed clock. It boots in the
past and jumps to the present the moment NTP answers, then is nudged for the rest
of its life.

**This is handled.** A negative elapsed measurement is read as "renew now"
rather than "not yet", both for lease renewal and for progress throttling.
Without that, a backward step would stop lease renewal until the clock climbed
back past where it started — the lease would expire under a perfectly healthy
job, and the work would run twice with nothing in the log looking like a clock
problem.

**What to do.** Nothing, unless the warnings are frequent, which means NTP is
fighting something. Install a hardware RTC if the device reboots often.

---

## 10. Useful commands

```bash
# Health
curl -sf localhost:8000/health/live     # process is up
curl -s  localhost:8000/health/ready    # store + workspace are usable
curl -s  localhost:8000/health          # version and environment

# What is on the disk
du -sh /data/workspace/*
df -h /data

# The lines that matter
grep -E "No workspace headroom|is now an orphan|did not unwind|ended without being asked" <log>
grep -E "Worker ready|Acquisition pipeline wired|Worker stopped" <log>

# Graceful stop, then insist
docker compose stop worker              # SIGTERM, honours the drain
docker compose kill -s SIGTERM worker   # a second one skips the drain

# Verify the safety properties yourself
pytest tests/failure -m failure
pytest tests/chaos   -m chaos
pytest tests/benchmarks -m benchmark --durations=25
```

---

## 11. When in doubt

Restarting the worker is **safe**. It costs at most one attempt per in-flight
job, the startup sweep reclaims this identity's leases immediately, and the
workspace root is swept clean. Every recovery path in this system is exercised by
the failure suite on every commit.

Deleting things by hand is **not** safe. The workspace looks like scratch space —
it is — but a directory a live worker is writing into is somebody's download, and
the recovery policy is deliberately conservative about exactly that. Let the
sweep do it.

## See also

* [Production Checklist](production-checklist.md) — what to verify before this is
  ever needed.
* [Failure Matrix](failure-matrix.md) — every failure, its classification, and
  the test that proves the behaviour.
* [Recovery Matrix](recovery-matrix.md) — who recovers what, how long it takes,
  and what it costs.
* [Performance Report](performance-report.md) — measured costs and the ceilings
  that guard them.
