# Recovery Matrix

For each thing that can be left behind by a failure: **who notices, how long it
takes, and what it costs.**

The Failure Matrix answers "what happens when X breaks". This answers the
question you ask at 3 a.m., which is different: *something already broke — when
will it be over, and do I need to do anything?*

The short version is that there are only three recovery mechanisms in the whole
system, and everything below is one of them:

* **The lease lapses.** The only crash-detection primitive. No liveness ping, no
  registry, no lock service.
* **The startup sweep runs.** Once per process start, for leases and for
  workspace directories.
* **The context manager unwinds.** Which covers every in-process failure,
  including `BaseException`.

---

## 1. Recovery times

| Left behind | Noticed by | Worst case | Typical | Costs |
| --- | --- | --- | --- | --- |
| A job leased by a dead worker | Lease expiry sweep | `2 × lease_seconds` (240 s default) | one lease period | 1 attempt |
| …when the same worker restarts | Startup recovery of own leases | one process start | seconds | 1 attempt |
| A job drained by `SIGTERM` | Immediate release | `drain_grace_seconds` (30 s) | one stage boundary | **nothing** — the attempt is refunded |
| A job whose stage will not wind down | Drain grace expiry, then lease expiry | `drain_grace + 2 × lease_seconds` | — | 1 attempt |
| A workspace lease from a crashed process | Startup sweep | one process start | seconds | its bytes until then |
| A workspace lease this process failed to delete | `workspace.orphans()`, then the next startup sweep | until restart | — | its bytes; logged as `ERROR` |
| A workspace directory with no readable manifest | Startup sweep, after `lease_expiry_seconds` | 3600 s default | — | its bytes |
| A workspace lease belonging to *another* live identity | Startup sweep, after `lease_expiry_seconds` | 3600 s default | — | its bytes |
| An abandoned engine thread | Counted at once; only a **restart** reclaims it | never, without a restart | — | one thread-pool slot, permanently |
| A stalled destination | Circuit breaker cooldown | `delivery.cooldown_seconds` (60 s) | — | nothing, if an alternative exists |
| A full disk | Backpressure clears on the next cycle | one idle poll (≤5 s) after space returns | — | 1 attempt, once |

**The number to remember:** after a hard crash, work resumes within **two lease
periods**, or within **one process start** if the same worker comes back. Both
are pinned by tests (`test_recovery_takes_at_most_two_lease_periods`,
`test_a_restarted_worker_takes_its_own_work_back_immediately`).

---

## 2. Who recovers what

### The lease (jobs)

A worker that dies stops extending its lease. When the lease expires the job
becomes claimable again — that is the entire mechanism.

| Sweep | When it runs | What it takes back | Setting |
| --- | --- | --- | --- |
| Own-identity recovery | Worker startup | Every lease held by *this* `host:role:index`, expired or not | `worker.recover_own_leases_on_start` |
| Expired-lease reclaim | Worker startup | Every lease from *any* worker that has lapsed | `worker.reclaim_expired_leases_on_start` |
| Heartbeat renewal | Every `heartbeat_seconds` | Keeps a live job's lease from lapsing | `worker.heartbeat_seconds` |

Worker identity is **stable, never random** (`host:role:index`) precisely so a
restarted process can take back what its previous incarnation was holding. Two
workers on one machine must differ in `index`, or they share an identity and
release each other's leases at startup.

There is no periodic reaper. A single-worker deployment recovers at startup,
which is enough; running these sweeps *on a timer* belongs to a scheduler
process that is not built (`ARCHITECTURE.md` §8).

### The workspace (bytes)

| Situation | Decision | Rule |
| --- | --- | --- |
| Same identity, **same** process id | **Leave.** This is our own live lease. | `RecoveryPolicy` rule 1 |
| Same identity, different process id | **Adopt** if it still holds work and adoption is enabled, else **delete**. | rule 2 |
| A different identity | **Delete only** once quiet for `lease_expiry_seconds`; otherwise leave. | rule 3 |
| No readable manifest | **Delete** once older than `lease_expiry_seconds` — a manifest is written immediately after the directory, so anything still unclaimed an hour later is debris. | `decide_unclaimed` |
| `purge_on_start = true` | **Delete everything.** Correct for a single-process deployment, and exactly wrong for a shared root. | `workspace.purge_on_start` |

Rule 3 is not negotiable: deleting a directory another worker is writing into
destroys a healthy job, and the cost of waiting is a few megabytes for one
sweep. It is the conservative half of the storage strategy, and the reason the
default deployment is one process per workspace root.

### The context manager (in-process)

The workspace lease wraps the whole attempt and unwinds on **`BaseException`**,
not just `Exception`. That is why a `SIGKILL`-shaped failure inside the process
still leaves no bytes behind — and it is the single most load-bearing `finally`
in the codebase (`test_no_workspace_survives_the_kill`).

---

## 3. What recovery does *not* do

Worth stating plainly, because each of these looks like a bug the first time
somebody meets it.

* **It does not resume a download across attempts.** The lease is per attempt;
  the partial file goes with it. See the Failure Matrix §3 limitation.
* **It does not reclaim an abandoned engine thread.** Nothing can force a Python
  thread to stop. The count is reported so you can restart before the pool is
  exhausted; the restart is the recovery.
* **It does not un-send a delivery.** Once a receipt exists the transfer is never
  repeated, and a bot cannot reliably delete its own messages — which is why the
  Telegram provider honestly declares `supports_delete=False`.
* **It does not refund an attempt spent on a crash.** A job that reliably kills
  its worker must eventually stop, or it takes the system down with it. Only a
  *graceful* drain refunds.
* **It does not run on a timer.** Every sweep above is startup-only.

---

## 4. Recovery decision tree

```
Something is wrong.
│
├─ Is the process running?
│  ├─ No  → the orchestrator restarts it.
│  │        Startup recovers this worker's own leases immediately
│  │        and sweeps the workspace root. Nothing to do.
│  │
│  └─ Yes → is it claiming work?
│     ├─ No, and the log says "no workspace headroom"
│     │        → the disk is full. Free space; claiming resumes
│     │          within one idle poll. Jobs were NOT failed.
│     │
│     ├─ No, and readiness says degraded
│     │        → the workspace root is missing or read-only.
│     │          Check the mount. See the Runbook, §"Disk".
│     │
│     ├─ No, and nothing in the log
│     │        → check `engine_thread_stats().abandoned`.
│     │          Non-zero means pool capacity is gone: restart.
│     │
│     └─ Yes → jobs are moving. If one job is stuck,
│              it is inside a stage; it will hit its timeout
│              (`download_timeout_seconds`) or its lease will lapse.
│
└─ Are bytes accumulating in the workspace?
   ├─ `usage().is_leaking` is true → a delete failed. The ERROR log
   │   names the directory. A restart sweeps it.
   └─ Otherwise → leases in flight. Normal.
```

---

## 5. Sizing the settings that decide recovery time

| Setting | Default | What it buys | What it costs |
| --- | --- | --- | --- |
| `worker.lease_seconds` | 120 s | Lower = faster crash recovery | Too low and a healthy job is reclaimed mid-stage; must be ≥ `2 × heartbeat_seconds` (enforced) |
| `worker.heartbeat_seconds` | 30 s | Lower = faster cancellation, more lease writes | SD-card wear |
| `worker.drain_grace_seconds` | 30 s | Longer = more work survives a deploy | **Must be shorter than the orchestrator's kill timeout**, or the graceful path never runs |
| `workspace.lease_expiry_seconds` | 3600 s | Lower = orphans reclaimed sooner | Too low and a sweep deletes another live worker's directory |
| `download.download_timeout_seconds` | 3600 s | Bounds a wedged transfer | Must exceed the time to fetch `max_item_bytes` on your connection, since there is no cross-attempt resume |
| `job retry `max_attempts`` | 3 | More = survives more transient failures | Each attempt is a full download |

The one that is silently dangerous is `drain_grace_seconds`. If it exceeds the
orchestrator's stop timeout, the process is killed while it is still politely
winding down, the graceful path never actually runs, and every deploy loses
in-flight work — with nothing in the logs that looks like a misconfiguration.
Docker Compose defaults to 10 s; this deployment sets `stop_grace_period`
explicitly for that reason.
