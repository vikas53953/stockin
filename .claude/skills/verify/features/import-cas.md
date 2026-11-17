# Import a CAS and list holdings

`stockin import cas FILE` reads a CAMS/KFintech or NSDL/CDSL statement (a PDF, or the JSON `casparser -o json` writes) and adds its facts to the ledger. `stockin holdings` lists units per account and ISIN. Import is a set union: the same file twice, or overlapping months in any order, give the same ledger.

## Sub-features

- `import-mf`: a detailed CAMS statement adds opening and closing balances, coverage and transactions; stamp duty is part of cost, STT comes off proceeds.
- `import-repeat`: importing a file again prints `already imported; nothing changed` and `0 new facts`.
- `import-overlap`: Jan and Jan-Feb statements together count each transaction once.
- `import-demat`: an NSDL statement lists balances; cost is `unknown`; a stock missing from a later statement drops out.
- `import-reject`: a statement that does not add up is rejected whole with exit 2, and no ledger is created.
- `holdings`: lists what is held, `--json` gives the same rows.

## How to get to it (user POV)

- README step 2: download the CAS, run `stockin import cas <file>`, type the PDF password when asked.
- Run `stockin holdings` any time after.

## Driving it with the stockin CLI

Preconditions: the baseline in [README.md](./README.md). Fixtures: `F=tests/fixtures/cas` (synthetic; regenerate with `uv run python tests/cas_fixtures.py`).

- **MF import.** `stockin import cas $F/mf-2026-01.json`. Exit 0, `5 new facts`, one HDFC line with `150.5` and `cost 15,000.75`.
- **Overlap.** `stockin import cas $F/mf-2026-01-to-02.json`. Exit 0, `3 new facts, 3 already in your ledger`, the line now shows `130.5` and `cost 13,010.60`.
- **Repeat.** `stockin import cas $F/mf-2026-01.json --json`. `seen_before` is true and `facts_added` is 0.
- **Demat.** Import `$F/demat-2026-01.json` then `$F/demat-2026-02.json`. After the second, only INFOSYS (`12`, `cost unknown`) is listed for the broker account; TCS is gone.
- **Holdings.** `stockin holdings --json` has exactly two ISINs: `INE009A01021` and `INF179K01BE2`.
- **Order independence.** In a second fresh `STOCKIN_DATA`, import the four fixtures in reverse order. `sqlite3 $RUN/data/ledger.db .dump` differs only in row order; `stockin holdings --json` output is identical.
- **Reject.** Edit a copy of `mf-2026-01.json` so `close` is `151`, import it into an empty data folder. Exit 2, `nothing was imported`, and no `ledger.db` exists.

## Gotchas

- A real PDF needs its password; set `STOCKIN_CAS_PASSWORD` for scripted runs. Never use a real PDF in verification.
- MF folios in an NSDL statement are skipped with a note, because the CAMS/KFintech CAS is the source for them.
- Account numbers are never stored; accounts print as `Institution ...last4`.
