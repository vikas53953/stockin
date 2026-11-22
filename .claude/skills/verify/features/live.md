# Live prices and watch

Today's last traded price for each share and ETF you hold, from public quotes (no login), laid over the end-of-day prices; and `stockin watch`, which re-prices during market hours and keeps (and sends) an alert when a verdict turns into add, trim or exit. Mutual funds stay end-of-day. Live prices are never stored in `prices.db`.

## Sub-features

- `stockin prices live [--json]`: one pass. Each share/ETF is `LIVE` (traded today, at most 20 minutes ago while NSE is open, or today's last trade after the close) or `CLOSE` with the reason it kept the end-of-day price.
- `stockin watch [--every 300] [--once] [--send]`: while NSE is open (Mon–Fri 09:15–15:30 IST), every `--every` seconds: live pass, verdicts, `alerts.db`, and with `--send` Telegram. Closed: sleeps until 09:15 (at most an hour at a time). Ctrl+C stops it.
- NSE's symbol list (`EQUITY_L.csv` and the ETF list) is downloaded at most once a day and kept as `nse-symbols.json` in the data folder (public data).

## How to get to it (user POV)

`stockin prices live` to look; `stockin watch --send` left running in a PowerShell window (or at logon via Task Scheduler) for alerts through the day.

## Driving it with the stockin CLI

The public sources need the network. With no network, every holding must fall back:

```bash
stockin import cas tests/fixtures/cas/demat-2026-02.json
stockin prices import tests/fixtures/eod/bhavcopy-sample.csv
stockin prices live --json     # live: [] and end_of_day names INFY with a reason (offline), exit 0
stockin watch --once           # "0 live, 1 at close, N new alerts"
```

With network, during market hours, `live` has INFY with a time within 20 minutes. Compare its price with the NSE website by eye.

## Gotchas

- Quotes are a few minutes behind and unofficial; anything odd (wrong symbol, not INR, no time, ≤ 0, beyond ₹1 crore) is refused, never guessed.
- Prices are rounded half-even to the paise from the JSON text, never through a float.
- On an NSE holiday the newest trade is from an earlier day, so nothing is live.
- A live price beats only the same day's close; it is never written to `prices.db`.
