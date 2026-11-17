# Ledger design: facts in, everything else derived

This is the `/architect` output for Stockin's core domain. The type sketch is in [`sketch/`](sketch/): every body raises `NotImplementedError`, it imports cleanly, and pyright reports 0 errors. The scaffold PR implements against it.

## Problem

Stockin has to answer "add, trim or exit?" from sources that disagree in kind:

- An MF CAS lists transactions. A demat eCAS lists only balances.
- Monthly statements overlap.
- The owner types in EPF, FDs, gold and theses.
- Four surfaces (CLI, dashboard, MCP, Telegram) must quote the same numbers.

The hard rules in [CLAUDE.md](../../CLAUDE.md) narrow it further: money is integer paise, keys are ISINs, import is idempotent, and nothing can place an order.

The demat case is the hard one. A transaction ledger has nothing to fold. The usual fix is synthetic "opening balance" rows, but that puts invented data in the master record, and the rows go stale the day real trades arrive.

## Usage (caller's view)

Each README command makes one call on one of two handles. `Desk` reads; `Clerk` writes.

```python
# stockin import cas 2026-10-cams.pdf          (twice: facts_added == 0)
Clerk.open(cfg).import_cas(pdf, password)       # -> ImportReport, one line per holding

# stockin reconcile                             (exit 1 on any mismatch)
report = Desk.open(cfg).reconcile()             # report.mismatches -> ISIN + both numbers

# stockin thesis add INFY --invalidate-below 1350 --max-weight 8 ...
clerk.record(Thesis(isin=clerk.book().resolve("INFY"), invalidate_below=Price.rupees("1350"),
                    max_weight=percent("8"), ..., recorded_on=today))

# stockin review  /  MCP position_review  /  dashboard  /  Telegram job
Desk.open(cfg, live=upstox).review(today)       # rows[i].verdict, rows[i].findings[0].reason
due_alerts(review, already_sent)                # Telegram only

# stockin networth
Desk.open(cfg).networth(today)                  # total, slices with drift, lines, gaps
```

Every result is a frozen pydantic model, so `--json` is `model_dump_json()` of the same object the table is drawn from. Tests build `Book.of(facts)` with no database.

## Shape

**Data.** The database stores only facts that someone outside the program asserted. They form one tagged union:

- From statements: `Txn`, `Balance`, `Coverage`, `Complete`.
- From the owner: `StatedBalance`, `FixedDeposit`, `GoldHolding`, `Thesis`.

A fact's id is the hash of its content. Nothing derived is stored.

**Load-bearing decisions:**

1. **Content-addressed facts make import a set union.** Overlapping monthly CAS files converge instead of double counting. The ledger depends only on the set of files, not their order, and `digest()` checks this. That is stronger than the plan's file-hash guard (per make-operations-idempotent).
2. **`Balance` plus `Coverage` replace any "has transactions" flag.** One fold, `Book.timeline`, walks the transactions between balance checkpoints:
   - A gap inside a covered window is a `mismatch`.
   - A gap outside one is an `Inferred` movement with unknown cost, computed on read.
   - MF opening balances, balance-only demat, and a future tradebook all follow this one rule. Importing missing trades makes the inference disappear, with no cleanup (derive, don't sync).
3. **`Complete` closes sold-out demat positions.** An eCAS lists every holding of the account. An ISIN missing from it is therefore zero, and the importer never reads earlier state.
4. **One fold, many views.** Holdings, lots, P&L and reconcile all read the same `Timeline`, so reconcile cannot disagree with review.
5. **The whole ledger lives in memory.** One investor has about 10⁴ facts, so pure functions of `(Book, PriceBook, today)` run in milliseconds. There is no SQL in the domain and no cache.
6. **Three integer scales, one rounding site.** Money is in paise, quantities in micro-units, and prices in micro-rupees, because NAVs carry 4 decimals. `Money.of(qty, price)` is the only multiplication, and strict ints reject floats.
7. **Unknown is `None`, never zero.** An unknown cost or price propagates. A missing thesis or quote gives `review-due`, never a silent `hold`.
8. **One ordered rule table.** Rule order is verdict precedence: exit-check, review-due, trim, add-eligible, hold. Every finding is kept, and the first one is the reason shown.
9. **Read-only by handle and by file.**
   - `Desk` has no write verb. Its connection sets `query_only`.
   - Only the CLI gets a `Clerk`.
   - Prices live in `prices.db` and alert state in `alerts.db`, each with one writer (per separate-before-serializing-shared-state).
10. **`Desk` is the only door.** The ledger, prices, live quotes and today's date are assembled in exactly one place, so no surface prices a book differently.

**Interface depth.** The public surface is two handles with about eight methods between them. Behind it sit:

- FIFO lots with exact cost split.
- Splits and bonuses.
- Gap inference and coverage.
- Overlap dedupe.
- Symbol resolution.
- Price overlay.
- Rule precedence.

`store` and `sources` can be imported only by `app`, enforced by import-linter in CI.

**Deliberately not done:** tax computation, order types, intraday history, ISIN changes on mergers, and stored derived state.

## Synthesis decision

Three runners (opus, fable, sonnet) each produced a candidate. A fable cross-judge scored them blind against a six-point rubric: A (opus) 22, **B (fable) 24**, C (sonnet) 18. My own reading agreed, so **B is the base**.

B is the only shape where MF opening balances, balance-only demat and a future tradebook all go through one rule with no invented rows. It also has the strongest idempotency contract.

All three converged on the same core: append-only content-addressed facts, a pure fold, and an ordered rule table. That agreement is strong evidence for the shape.

Grafted from **A (opus)**:

- The `Complete` statement fact, which closes sold-out demat ISINs.
- Account ids keyed without the statement provider, with the account number never stored.
- `review-due` for a holding with no thesis.
- A 1-point headroom rule for `add-eligible`.
- Private internals enforced by import-linter.

Grafted from **C (sonnet)**:

- The single read/write facade (`Desk`/`Clerk`).
- Missing data never reading as `hold`.
- `NetWorth.gaps`, which names what is unpriced.

Bugs the judge found in B, now fixed:

- `networth` was called with an empty price set. Pricing now happens only in `Desk.prices`.
- The Timeline invariant was worded falsely.
- Any CAS `REVERSAL` rejected the whole statement. A reversal now cancels its pair within the statement.
- Two identical FDs collapsed into one. `label` is now required.
- `mode=ro` fails on a WAL database. Replaced with `PRAGMA query_only`.

Rejected:

- A's EOD prices stored as ledger facts. That is about 15k rows a day in the table `book()` loads, with two writers on one file.
- A's OPENING txn per statement, which double counts when statements overlap.
- C's per-account `coverage` flag, which can't absorb a later tradebook.
- C's table-shaped `Ledger` getters, which make a shallow module.

## Tradeoffs accepted

- We accept re-folding the whole ledger on every request in exchange for having no derived state. If a page ever passes its budget, cache `Book` per `digest()`.
- We accept that demat cost and P&L show as unknown until a tradebook importer exists, in exchange for never storing an invented number.
- We accept `Inferred` movements dated at the statement that asserts them, which shortens apparent holding periods, in exchange for not guessing dates.
- We accept JSON fact bodies in one table, which ad-hoc SQL can't easily read, in exchange for one write path and one schema (the pydantic union).
- We accept three SQLite files in exchange for one writer per file.
- We accept that an unknown CAS transaction type rejects the statement, in exchange for never importing half a truth.

## Alternatives considered

- **A transaction ledger with synthetic opening and adjustment rows.** The importer must read prior state, so import becomes order-dependent, and invented rows sit beside real ones. It lost on idempotency and leakage.
- **Snapshot-first: store each statement's holdings.** Reconcile becomes trivial, but lots, P&L and XIRR have no home, and two models grow side by side.
- **Mutable `holdings` and `lots` tables updated on import.** Reads are fast, but every writer must maintain derived state (split ownership), and the logic lands in SQL, where pyright can't check it.
- **Event sourcing with projections.** It is the same idea plus machinery that a ledger of 10⁴ facts doesn't need.

## Implementation reconciliation

- **Max weight is measured against market holdings** (stocks, MFs and ETFs only). The owner decided this on 2026-11-17, closing the first open question.
- **`Book` is a plain class, not a pydantic `Value`.** It is never serialized, and it caches one `Timeline` per (account, ISIN). The shared contract (`Book.of`, `timeline`, `holdings`, `positions`, `reconcile`) is unchanged. Accepted in the fold PR.
- **An oversell is judged by coverage, like a balance gap.** Inside a covered window it is a `mismatch` check (`kind="oversell"`), and only the units held are sold, with their share of the stated proceeds. Outside one, the shortfall is an `Inferred` acquisition dated at the sell. Accepted in the fold PR after the independent bug review.
- **Same-day txns are ordered by fact id after kind and `nth`**, so FIFO never depends on input order. Found by the PR 4 verifier.
- **`manual_assets(today)` takes the date from the caller**, like every other clock read.

## Open questions and risks

- **Headroom:** is 1 percentage point the right headroom for `add-eligible`? Without some headroom, the README's rule makes `hold` unreachable.
- **FD interest:** do FDs compound quarterly? And should EPF and PPF accrue between the balances you enter? The sketch says no.
- **Gold price:** should it come from IBJA, MCX EOD, or your own entry?
- **Risk: casparser on real layouts.** P2's live check imports two overlapping real months in both orders and compares `digest()`.
- **Risk: mergers and demergers.** These change the ISIN and show up today as a mismatch or as an inferred exit plus entry.

## Next implementation step

Implement `money.py` and `facts.py` with `fact_id`. Then test `Book.timeline` against synthetic fixtures (partial sell, split, bonus, balance-only demat, overlapping statements, sold-out via `Complete`) before any SQLite or casparser code exists.
