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
- During the day a live trade beats yesterday's close (it is newer). On the same day NSE's official close, once `stockin prices fetch` has it, beats the day's last live trade. Live prices are never written to `prices.db`.
- A live price is used only next to a close it can be checked against: a close from the last 7 days, and the live price within 20% of it (NSE's widest daily band; exactly 20% is used). Further away is a split or bonus day, or a bad quote: the close stands and the reason names both prices. With an older close, or none, the reason says to run `stockin prices fetch`. This is tighter than the 2x rule for price files, because a 1:1 split that opens up is just inside 2x.
- What the band cannot see: a bonus of 1:4 or smaller moves the price by 20% or less, like an ordinary day. And a share in futures and options that really moves more than 20% in a day keeps its close until the evening's `prices fetch`.
- Once today's bhavcopy is in, `prices live` shows `CLOSE … today's official close of ₹… is in; verdicts use it` for each share it prices, and asks for no quote.
- One quote is asked for once, with a 10-second timeout; a cut-off or garbled answer falls back to the close.
- A symbol list with only one of NSE's two files (either one failed or could not be read) is used for that pass but not kept, so the next pass downloads again. A holding missing from such a list is reported as "the list was incomplete", not as "not listed".
- `watch` never stops on an error in a pass: it prints `error: this pass failed (...)` and tries again next pass. Only Ctrl+C ends it, also in the middle of a pass.
- After 3 failed checks in a row, `watch` prints `error: watch has failed 3 checks in a row` once, and with `--send` sends one Telegram message, `Stockin watch cannot run: <reason>. …`. Nothing more is said for that run of failures until a check has worked. If Telegram does not take the message, the next failed check tries again. `--once` only exits with its error.
- `watch` does not download end-of-day files. Schedule `stockin prices fetch` once each evening (after about 19:00 IST) next to it.
