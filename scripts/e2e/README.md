# End-to-end delivery check

`e2e_driver.py` runs the bot's real acquisition pipeline for a list of links and
delivers every file to the owner chat through the local Bot API: probe, AUTO
quality, workspace lease, yt-dlp/gallery-dl fetch, ffprobe completeness check,
upload by path, journal entry, lease release. It builds the same composition
root as `mediahub.presentation.telegram.__main__` minus the poll loop, so the
live gateway keeps its getUpdates session. It is the proof that a deploy
works: nothing counts as done until a real file reaches the chat
(rule since 2026-10-01).

Run on the Pi, sequentially, under the voltage gate (see CLAUDE.md §8.10):

```
cd ~/bot-tele
docker compose -f docker-compose.pi.yml --profile localapi --profile vpn run --rm --no-deps -T \
  -e E2E_CHAT=<owner chat id> -e MEDIAHUB_WORKSPACE__PURGE_ON_START=false \
  -v $PWD/scripts/e2e/e2e_driver.py:/e2e/e2e_driver.py:ro \
  -v $PWD/scripts/e2e/links-public-samples.json:/e2e/links.json:ro \
  telegram python /e2e/e2e_driver.py /e2e/links.json /data/e2e-results.jsonl [--probe-only]
```

`--probe-only` validates links without downloading. Results are one JSON
object per link in the data volume; delete the file afterwards. Public sample
links rot; replace them before blaming the pipeline.
