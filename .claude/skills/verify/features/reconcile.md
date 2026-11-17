# Reconcile

`stockin reconcile` compares every balance a statement stated with what the transactions explain. It prints `OK` and exits 0 when nothing contradicts; any mismatch prints the ISIN, account, date and both numbers, and exits 1.

## Sub-features

- `reconcile-ok`: after the four synthetic fixtures, `OK  7 checks: 3 match, 4 inferred, 0 mismatch`.
- `reconcile-mismatch`: a later statement whose opening contradicts an earlier close gives `MISMATCH balance` and exit 1.
- `reconcile-json`: `--json` lists every check with `status` match, inferred or mismatch.
- `reconcile-no-ledger`: before any import, exit 2 with the hint `stockin import cas FILE`.

## How to get to it (user POV)

- README step 4, right after importing statements.

## Driving it with the stockin CLI

Preconditions: the four fixtures from [import-cas.md](./import-cas.md) imported.

- **OK.** `stockin reconcile`. Exit 0, first line starts with `OK`.
- **JSON.** `stockin reconcile --json`. 7 checks, none with `"status": "mismatch"`.
- **Mismatch.** In a fresh data folder, import a one-scheme statement closing at 10, then one for the next month with `open` and `close` 7 and no transactions (see `test_cli_reconcile_exits_1_on_mismatch`). Exit 1, a `MISMATCH balance` line.
- **No ledger.** With an empty `STOCKIN_DATA`, exit 2 and the import hint.

## Gotchas

- `inferred` is not a failure: it means a statement gave a balance with no trades in that window (demat statements always do).
