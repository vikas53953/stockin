# Net worth

`stockin asset add` records what the CAS does not cover (bank, FD, EPF, PPF, NPS, gold, property, other, loans) as dated snapshots in `assets.db` (0600, beside `ledger.db`). `stockin target set` stores a target mix. `stockin networth` adds priced market holdings to the latest snapshot of each entry, subtracts loans, splits what you own by class with exact basis points, and prints drift from the target and computed notes. Verdicts stay measured against market holdings only.

## Sub-features

- `asset-add`: one snapshot per kind, label and day; the same day again replaces it; labels ignore case and extra spaces.
- `asset-fd`: an FD is valued by formula (quarterly compounding from 6 months, simple interest under 6), capped at maturity; a matured FD gets a note.
- `asset-gold`: grams x `--rate-per-gram`.
- `asset-close`: a `Closed` snapshot; history stays. A close and a value can share a day, and the later write wins (a value added after that day's close counts; both rows stay, the close marked `reopened`); `--on` before the entry's latest date is refused with the date to use. An FD's entry is dated the day it opened unless `--as-of` is given, so it closes at maturity.
- `asset-list`: current entries and their value today.
- `target-set` / `target-clear`: CLASS=PERCENT pairs adding to exactly 100.
- `networth`: total = owned - owed; slices add to 10,000 bps; drift = weight - target; notes (drift, on target, stale > 90 days, matured FD, loans, market share, unpriced).
- `doctor-assets`: `warn` with no file or a zero-byte file (opens `mode=ro`, never creates it), `ok` with a count, `fail` on damage.
- `assets-damage`: any non-empty file must pass `PRAGMA integrity_check` and hold exactly the `entries` and `targets` tables; otherwise every command exits 2 and nothing is written (one zeroed byte at offset 104 is the classic case).

## How to get to it (user POV)

- README step 3 (add what the CAS does not cover) and step 5 (see your net worth).

## Driving it with the stockin CLI

Preconditions: a fresh `STOCKIN_DATA`. A CAS import is optional; with the fixtures from [import-cas.md](./import-cas.md) and `stockin prices import tests/fixtures/eod/bhavcopy-sample.csv`, market holdings join the total.

- `stockin asset add bank --label "SBI savings" --value 250000 --as-of 2026-11-01` prints `saved Bank 'SBI savings' ... ₹2,50,000.00 today`, exit 0.
- `stockin asset add fd --label "HDFC FD" --principal 100000 --rate 7.1 --start 2025-11-22 --months 12` (a 12-month FD opened a year before today) is worth `₹1,07,291.28` at maturity.
- `stockin asset add gold --grams 20 --rate-per-gram 7650` is `₹1,53,000.00`.
- `stockin asset add loan --label "Car loan" --value 100000`.
- `stockin target set equity=60 debt=20 gold=10 cash=10`, then `stockin networth --json`: `total.paise == owned.paise - owed.paise`, `sum(slices[].bps) == 10000`, and each `drift == bps - target`.
- Refusals exit 2 with a sentence and no traceback: an unknown kind, a flag for another kind (`--grams` on a bank), more than 2 decimals, a future `--as-of`, targets not adding to 100.
- Damage: write junk into `assets.db`; `stockin networth` exits 2 with "only copy of what you typed", and `stockin doctor --json` shows the `assets` check as `fail`.

## Gotchas

- `assets.db` is the only copy of what the owner typed. Never move it aside as a fix; copy it first.
- `--rate` is the FD's interest rate; gold uses `--rate-per-gram`. A flag means one thing.
- `--as-of` defaults to today in India (`india_today`); a run near midnight UTC can land on the next Indian day.
