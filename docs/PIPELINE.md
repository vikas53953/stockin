# Pipeline improvements

How Stockin is built and checked, one rule at a time. Every rule says when it
was added, why, and what it has caught since. Add a row whenever the pipeline
changes, in the same PR as the change. A rule that has never caught anything
after a sprint is a candidate to drop at the retro.

The rules themselves live in `CLAUDE.md` and `.claude/skills/verify/`; this
file is their history. The stories behind them are in [LEARNINGS.md](LEARNINGS.md).

## The flow today

1. Write the change on a branch with its tests. Money, facts, import, the
   ledger or reconcile also get a property in `tests/test_properties.py`.
2. Gates: ruff, ruff format, pyright (strict on `src`), pytest (unit + properties), and
   the installed-CLI smoke step in CI.
3. Author runs it on the real surface (installed binary, browser) and
   records before/after evidence.
4. An agent that did not write the code verifies it: gates, its own live run
   with before/after screenshots and a video for browser flows, a diff audit
   (plus thermos reviewers for code PRs), and a mutation check that the new
   tests fail without the fix. It posts `PASS`, `PASS+NOTES` or
   `FAIL` with the head SHA, patch-id and the sha256 of every evidence file.
5. Passing verdict + green CI on the same head: squash-merge with a head-SHA
   guard. `FAIL`: at most two fix-forwards, then stop and report.
6. Before the owner tests anything, an agent runs the owner's exact steps
   first, as an end user, and reports pass/fail with evidence.
7. The owner's run is the final confirmation. Only then a `v0.N` tag and a
   retro.

## Log

| # | Date | Rule | Why it was added | What it caught |
|---|---|---|---|---|
| 1 | 2026-11-17 | Adopt pstack: plan with code, one PR is one change, a `verify` skill that drives the installed binary (#1) | Tests passing is not the same as the app working | First live run of `import cas` (#5) showed casparser enums printing as `TransactionType.PURCHASE`; pytest had passed |
| 2 | 2026-11-17 | Independent verifier posts PASS / PASS+NOTES / FAIL; merge only on a passing verdict and green CI on the same head SHA; at most two fix-forwards | The author cannot be the only judge of their own change | #3 FAIL: the docs said every command takes `--json`, but `version` did not. #4 FAIL twice: the fold depended on fact order (a lump sum and a SIP on the same day), then a phantom holding after a flagged zero. #9 FAIL: CASParseError not recognised as "not a CAS". #12 FAIL: TDS after a reinvested IDCW taken off twice. Notes on #5–#8 led to fixes for tracebacks on bad input, a non-atomic read, reversal pairing, phone layout and a busy port |
| 3 | 2026-11-17 | Merged is not released: `v0.N` is tagged only after the owner uses the sprint and says it works, then a retro | Main moving is not the same as the owner being able to use it | Drop 1 is merged but not tagged until the owner's own CAS run |
| 4 | 2026-11-17 | Never claim a check that has not run; say what is unverified | A PR body claimed a browser check before it ran (#10) | The check was run straight after and passed; the rule stops the claim coming first |
| 5 | 2026-11-17 | Property-based tests (Hypothesis) are a gate for money, facts, import, ledger and reconcile; the verifier runs the deep profile and a mutation check (#11) | Hand-picked fixtures only test the cases we thought of | Mutation check showed the first property set did not catch a revert of the #4 fix; a demat property was added and now does, shrinking to two statements |
| 6 | 2026-11-17 | Agent goes first on UAT: before the owner tests, an agent runs the same checklist as an end user on realistic, password-protected synthetic PDFs | The owner was about to test PDF import that had only been tested with JSON | TDS on an IDCW payout rejected the whole CAS (#12). Cost shows unknown for funds held before the statement starts (checklist now says to download from first investment). Checklist said CDSL uses PAN + DOB; CDSL's FAQ says PAN |
| 7 | 2026-11-17 | Evidence or it did not happen: before and after screenshots plus raw output with exit codes, saved under `evidence/pr-<N>/` and hashed (sha256) in the verdict | A green tick with nothing behind it can be fake | Every verdict from #11 on. Hashes must be taken after copying, because the share alters PNGs (found by the #13 verifier) |
| 8 | 2026-11-17 | Multi-step browser flows are also recorded as a video (Playwright `recordVideo`), hashed like the screenshots | A video shows the order of steps and what happened between screenshots | The drop 1 checklist run is recorded; the #13 verifier proved the recipe on a fresh install |
| 9 | 2026-11-17 | Lessons are kept in `docs/LEARNINGS.md`: every pushback, verifier FAIL and retro adds an entry | Lessons were living only in chat and would be lost between threads | Seeded with 9 lessons from drop 1 |
| 10 | 2026-11-17 | Every new or changed test is mutation-checked by the verifier with its own mutations, not only properties | The #11 verifier's own mutations showed the first properties missed 6 of 10 bugs | All 6 now caught (#14) |
| 11 | 2026-11-17 | Docs about our own process are verified against PR comments and git history, like code | The #13 verifier found these docs undercounted past FAILs and overstated two claims | #13 FAIL, fixed forward |
