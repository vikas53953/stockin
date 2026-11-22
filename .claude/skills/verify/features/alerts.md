# Alerts

One alert when a holding's verdict turns into add, trim or exit. The alert is the verdict's own sentence, so it carries the same numbers as `stockin verdicts`. Alerts are kept in `alerts.db` (0600, beside `ledger.db`) with the call last seen for each holding, so the same numbers never alert twice. Sending to Telegram arrives in a later PR; until then every alert stays `pending`.

## Sub-features

- `stockin alerts check [--json]`: work out the verdicts now, keep an alert for each holding whose call newly became add, trim or exit, and print the new ones.
- `stockin alerts list [--json]`: every alert kept, oldest first, `[pending]` or `[sent]`.

## How to get to it (user POV)

After importing a CAS, fetching prices and writing a thesis with a target, run `stockin alerts check`. Run it again after each price update.

## Driving it with the stockin CLI

```bash
stockin import cas tests/fixtures/cas/mf-2026-01.json
stockin import cas tests/fixtures/cas/demat-2026-02.json
stockin prices import tests/fixtures/eod/bhavcopy-sample.csv
stockin alerts check --json          # alerts: [] (every verdict is review: no thesis)
stockin thesis set INE009A01021 --why "cash machine" --target 30
stockin alerts check --json          # one TRIM alert, "because" equals the verdict's
stockin alerts check --json          # alerts: [] (same numbers)
stockin thesis broken INE009A01021
stockin alerts check --json          # one EXIT alert
stockin alerts list                  # two lines, both [pending]
```

Assert on `--json`: each alert's `because` equals the `because` of the same ISIN in `stockin verdicts --json`.

## Gotchas

- Hold and review never alert. A hold in between re-arms a call: trim, hold, trim alerts twice.
- The first check alerts every holding that is already add, trim or exit, once.
- A holding you sell is forgotten, so buying it back starts fresh.
- A damaged `alerts.db` exits 2 with "move alerts.db aside"; never a traceback.
