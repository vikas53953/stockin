# Alerts

One alert when a holding's verdict turns into add, trim or exit, sent to your own Telegram bot. The alert is the verdict's own sentence, so it carries the same numbers as `stockin verdicts`. Alerts are kept in `alerts.db` (0600, beside `ledger.db`) with the call last seen for each holding, so the same numbers never alert twice.

## Sub-features

- `stockin alerts check [--json] [--send]`: work out the verdicts now, keep an alert for each holding whose call newly became add, trim or exit, print the new ones, and with `--send` send every pending alert.
- `stockin alerts list [--json]`: every alert kept, oldest first, `[pending]` or `[sent]`. Read-only: never creates `alerts.db`.
- `stockin alerts connect`: paste the bot token (hidden prompt), message the bot once, press Enter. Saves `telegram.json` (0600) in the data folder and sends "Stockin is connected".
- `stockin alerts send`: send pending alerts, oldest first; exit 1 and keep the rest pending if Telegram fails.
- `stockin alerts test`: one test message.
- `stockin doctor` has a `telegram` check: `warn` when not connected, `ok` with the bot number and chat (never the token), `fail` when the config is damaged.

## How to get to it (user POV)

Create a bot with @BotFather in Telegram, run `stockin alerts connect`, then `stockin alerts check --send` after each price update.

## Driving it with the stockin CLI

Never use a real bot token. Run the fake Telegram and point Stockin at it:

```bash
python3 .claude/skills/verify/fake_telegram.py 8799 "$RUN/evidence/telegram-sent.jsonl" &
export STOCKIN_TELEGRAM_API=http://127.0.0.1:8799
TOKEN="123456789:AAH$(printf 'x%.0s' {1..32})"
stockin import cas tests/fixtures/cas/mf-2026-01.json
stockin import cas tests/fixtures/cas/demat-2026-02.json
stockin prices import tests/fixtures/eod/bhavcopy-sample.csv
stockin alerts check --json          # alerts: [] (every verdict is review: no thesis)
stockin thesis set INE009A01021 --why "cash machine" --target 30
stockin alerts check --json          # one TRIM alert, "because" equals the verdict's
stockin alerts send                  # exit 2: not connected
printf '%s\n\n' "$TOKEN" | stockin alerts connect   # chat 555, token not printed
stockin alerts check --send          # sent 1 alert; telegram-sent.jsonl has the TRIM
stockin alerts check --send          # sent 0 alerts
stockin alerts list                  # one line, [sent]
```

Start the fake with `--fail-after 0` to see `alerts send` exit 1 with the alert still pending.

## Gotchas

- Hold re-arms a call (trim, hold, trim alerts twice). Review does not: it keeps the call before it, so a missing price for a run does not resend a TRIM.
- An alert that has not gone out yet is withdrawn when the holding's call changes again or the holding is sold, so only what is still true is sent. Sent alerts stay in the history.
- The first check alerts every holding that is already add, trim or exit, once.
- Delivery is at least once: a crash after Telegram accepts a message but before it is marked sent sends it again on the next run.
- `STOCKIN_TELEGRAM_API` is refused unless it is `http://127.0.0.1` or `http://localhost`, so the token cannot be sent to another host.
- A damaged `alerts.db` exits 2 with "move alerts.db aside"; a busy or read-only one exits 2 with "try again". Never a traceback.
