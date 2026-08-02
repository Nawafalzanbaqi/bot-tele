# Running MediaHub on a Raspberry Pi

For a Pi that is **already running other things**. Every choice below assumes
the device is shared and that breaking someone else's container is a worse
outcome than this project not starting.

Target used for this document: Raspberry Pi 5, 4 GB, Debian 13 (trixie),
arm64, NVMe, Docker 29 with Compose v5.

---

## 1. What runs, and what deliberately does not

| Service | Role | Memory limit |
| ------- | ---- | ------------ |
| `telegram` | The bot. Long-polls, probes, downloads, delivers. | 1200 MB |
| `api` | HTTP surface, for the web interface that comes later. | 400 MB |
| `botapi` | Optional self-hosted Bot API server (`--profile localapi`). | 600 MB |

**No database service.** SQLite is the system of record
([ADR-0006](../adr/0006-sqlite-system-of-record.md)): one file, no second
process, no administration, and a transactional enqueue for free. On a host
already running two PostgreSQL instances and a Redis for other projects, a
third store to hold a few thousand rows is pure cost.

**No worker.** The Telegram gateway acquires directly - it does not enqueue -
so the worker is only needed once the HTTP API is queueing work. Leave
`MEDIAHUB_WORKER__ENABLED=false` until then.

---

## 2. Ports

The API is published on **8090, bound to loopback**. It is not a default and
it is not on `0.0.0.0`, for two reasons: the common ports were already taken
on the target host, and the API has no authentication yet, so exposing it to
the LAN would be handing every device on the network an unauthenticated
download service.

Check before starting, and pick another if 8090 is taken too:

```bash
ss -tlnp | awk '{print $4}' | grep -oE '[0-9]+$' | sort -un
```

Reach it from a laptop over SSH rather than by publishing it:

```bash
ssh -L 8090:localhost:8090 <user>@<pi>
# then open http://localhost:8090/docs
```

---

## 3. First run

```bash
cd ~/bot-tele
cp .env.example .env          # if there is no .env yet
chmod 600 .env                # it holds the bot token
```

Fill in exactly two values:

| Variable | Where it comes from |
| -------- | ------------------- |
| `MEDIAHUB_TELEGRAM__BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `MEDIAHUB_TELEGRAM__OWNER_IDS` | [@userinfobot](https://t.me/userinfobot) → your numeric id, as `["123456789"]` |

Generate a real secret key rather than keeping the placeholder - production
refuses to boot with it:

```bash
sed -i "s|^MEDIAHUB_SECURITY__SECRET_KEY=.*|MEDIAHUB_SECURITY__SECRET_KEY=$(openssl rand -hex 32)|" .env
```

Then:

```bash
docker compose -f docker-compose.pi.yml build
docker compose -f docker-compose.pi.yml up -d
docker compose -f docker-compose.pi.yml logs -f telegram
```

**The allow-list denies by default.** An instance with all three id lists empty
allows nobody - which is the correct posture for a bot whose username is
guessable, and which is why an empty list is a startup failure rather than a
silent "everyone".

---

## 4. The 50 MB ceiling

The public Bot API accepts uploads up to **50 MB**. That is a Telegram limit,
not a MediaHub one, and it is why the quality menu drops renditions known to be
larger: offering a choice that will certainly be refused wastes the user's time
and a download slot.

A self-hosted Bot API server raises it to about **2 GB**. It needs an
`api_id` and `api_hash` from <https://my.telegram.org> → *API development
tools*. Those identify **an application on your Telegram account**, not the bot,
and are not the bot token.

```bash
# in .env
TELEGRAM_API_ID=<id>
TELEGRAM_API_HASH=<hash>
MEDIAHUB_TELEGRAM__API_BASE_URL=http://botapi:8081
```

```bash
docker compose -f docker-compose.pi.yml --profile localapi up -d
```

The provider reads this and reports `supports_large_files` accordingly, so the
ceiling that the quality menu applies moves with it - nothing else changes.

---

## 5. Platforms that need a logged-in session

Some platforms show nothing to an anonymous visitor. The failure is reported
honestly but reads like a bug in the bot:

| Platform | What it says | What it means |
| -------- | ------------ | ------------- |
| X / Twitter | `No video could be found in this tweet` | The post exists; the media is not served to a logged-out session |
| TikTok | `Your IP address is blocked from accessing this post` | The address, not the post - TikTok blocks broadly |
| Instagram | varies | Private accounts, and increasingly public ones |

The fix for all three is the same: give the engine a cookie jar exported from a
browser that is logged in.

```bash
mkdir -p ~/bot-tele/secrets
# Export cookies for the site with any Netscape-format cookie exporter,
# then put the file here:
mv cookies.txt ~/bot-tele/secrets/cookies.txt
chmod 600 ~/bot-tele/secrets/cookies.txt
```

```bash
# in .env
MEDIAHUB_DOWNLOAD__COOKIES_FILE=/secrets/cookies.txt
```

```bash
docker compose -f docker-compose.pi.yml up -d --force-recreate telegram
```

Easier: **send the file to the bot as an attachment.** Only an owner may, the
jar is validated before it replaces anything, and the upload is deleted from
the conversation afterwards. `/cookies` reports what is installed and when the
first cookie lapses; `/cookies clear` removes it.

### It keeps itself fresh

The jar is mounted **writable**, which looks like the less safe choice and is
the right one. Sites rotate their cookies on nearly every request, and the
engine writes the rotated set back after each download. That renewal is what
keeps a session alive with nobody touching it - measured on the target device,
one download turned a two-line seed jar into eleven lines of current cookies.

Mount it read-only and the renewal is silently discarded: the stored jar
slowly goes stale and a platform starts refusing again weeks later, for no
visible reason and with nothing in the logs to connect the two.

So the maintenance story is: **export once, and it maintains itself** as long
as the bot is used regularly enough that the session never lapses between uses.

**Understand what this file is before creating it.** It is a set of live
session credentials: whoever holds it is logged in as the account that exported
it, without a password and without a second factor. It is 0600 and read by one
non-root process, but the real control is *which account you export from* - use
one whose loss would be an inconvenience rather than a catastrophe.

**Do not export YouTube cookies.** Google invalidates a session used from an
unexpected address, and the usual result is being signed out everywhere.
YouTube needs no cookies here anyway.

A separate consequence: **extractors expire too.** yt-dlp is installed from the
pre-release channel precisely because platforms change without notice, and a
month-old release fails on TikTok with a fix already published upstream. When a
site that used to work stops, rebuild before investigating anything else.

---

## 6. Storage

Both the database and the scratch space live on one named volume, because the
target has one NVMe. On a machine with two devices, put the workspace on the
other one: it carries a write-heavy, entirely throwaway load, and keeping it
away from the only irreplaceable file is worth an extra mount.

Nothing downloaded is kept. The local copy exists only inside a workspace lease
and is deleted once the destination has confirmed
([ADR-0007](../adr/0007-ephemeral-local-media.md)); what survives is a few
hundred bytes of history per item. `MAX_TOTAL_BYTES` bounds what several
concurrent jobs can occupy, and `MIN_FREE_BYTES` is never allocatable - it is
the headroom that leaves SQLite able to commit the record of whatever went
wrong.

### Backups

The database is one file, but **never `cp` a live WAL database** - the result
is a file that opens and is missing its most recent transactions. Use SQLite's
own backup:

```bash
docker compose -f docker-compose.pi.yml exec telegram \
  python -c "import sqlite3; s=sqlite3.connect('/data/mediahub.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close(); s.close()"
```

The workspace needs no backup. That is the point of it.

---

## 7. Sharing the device

Three settings exist because other projects are running here:

- **`MEDIAHUB_WORKSPACE__PURGE_ON_START`** wipes the workspace root at startup,
  on the grounds that anything present belongs to a process that no longer
  exists. That is correct for the gateway, which is the only writer, and it is
  set to `false` for the API for exactly the same reason - two processes
  purging one root means one of them deletes the live leases of the other.
- **Memory limits on every service.** An unbounded upload on a 4 GB board is
  not this project failing, it is the OOM killer choosing a victim among
  someone else's containers. The gateway's limit must exceed the largest
  artifact the destination accepts, because the Telegram client library buffers
  a file in memory before sending it.
- **Log rotation on every service.** Without it a chatty container fills the
  disk it is running on, and the other projects find out as "no space left on
  device".

---

## 8. Checks

```bash
# Is it healthy?
docker compose -f docker-compose.pi.yml ps
curl -s localhost:8090/health/ready | python3 -m json.tool

# What has it done?
docker compose -f docker-compose.pi.yml logs --tail 50 telegram

# Is the store intact?
docker compose -f docker-compose.pi.yml exec telegram \
  python -c "import sqlite3; print(sqlite3.connect('/data/mediahub.db').execute('PRAGMA integrity_check').fetchone())"

# Is anything left in the workspace? (steady state: nothing)
docker compose -f docker-compose.pi.yml exec telegram ls -la /data/workspace
```

`/health/ready` answers `503` when the store cannot be reached or the workspace
root has gone away - the two states in which this instance accepts requests and
completes none of them. A **full** workspace is deliberately not one of them:
requests still queue correctly and the backpressure holds them until space
returns, so taking the instance out of service for a condition one deletion
fixes would be wrong.

---

## 9. Updating

yt-dlp is pinned and updated by rebuilding, never by self-updating at runtime -
a downloader that rewrites itself on a device nobody watches is a supply chain
with no review step. Extractors do break when platforms change, so expect to
rebuild periodically:

```bash
cd ~/bot-tele && git pull
docker compose -f docker-compose.pi.yml build --pull
docker compose -f docker-compose.pi.yml up -d
```

`stop_grace_period` is 60s, longer than any drain the gateway performs, so a
download in flight finishes and is delivered rather than being abandoned
half-uploaded.
