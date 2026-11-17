# Stockin

A private, read-only app for one investor in Indian markets. It answers one question first: should I add to, trim, or exit this holding?

@.claude/pstack-models.md

## How we work

- Start rigorous work with `/pstack:poteto-mode`. It picks the playbook.
- Verification comes first. A change is done when it has run against the real surface, not when tests pass.
- Plan with code. Settle open questions with a prototype, not a longer document.
- One PR is one change with its own evidence. Small and ordered.
- **Auto pipeline (owner's standing go, 2026-11-17).** Every PR goes through pstack's shipping flow without waiting for the owner: an agent that did not write the code verifies it (gates, a live run through the `verify` skill, and a diff audit; thermos for code PRs) and posts `PASS`, `PASS+NOTES` or `FAIL` on the PR. A PR with a passing verdict and green CI on the same head is squash-merged bottom-up with a head-SHA guard. A `FAIL` gets at most two fix-forwards, then stops and is reported.
- **Properties are a gate (owner, 2026-11-17).** Money, facts, CAS import, the ledger and reconcile are covered by property-based tests in `tests/test_properties.py` (Hypothesis): round-trips, order and repeat invariance, ingest/retract inverse, and "statements cut from one true history never mismatch". A change to any of them adds or updates a property, and CI runs them with every `pytest`. The verifier also runs `HYPOTHESIS_PROFILE=deep` for such a change, Every new or changed test, property or not, is mutation-checked by the verifier: it must fail when the fix is reverted. A property failure on an unrelated PR is a real bug to chase, not a flake.
- **Agent goes first on UAT (owner, 2026-11-17).** Before asking the owner to do any test step, an agent runs that exact step as an end user: the installed app, realistic synthetic data (password-protected PDFs where the owner would use PDFs), a real browser. It reports each step's honest pass or fail with evidence. The owner's run is the final confirmation, never the first.
- **Evidence or it did not happen (owner, 2026-11-17).** Every test and verification step saves a before and an after screenshot (for anything with a screen), a video of any multi-step browser flow, and the raw command output with its exit code. The files go to `/mnt/project-files/stockin/evidence/pr-<N>/`, and the verdict comment lists each file with its sha256. A `PASS` without that evidence is not a pass and does not merge.
- **Pipeline changes are logged.** Any change to how we build or check (a new rule, gate or evidence type) adds a row to `docs/PIPELINE.md`: date, rule, why, and later what it caught.
- **Lessons are kept.** Every owner pushback, every verifier FAIL and every sprint retro adds an entry to `docs/LEARNINGS.md`: what happened, the lesson, the rule, and what it caught since.
- **Never claim a check that has not run.** A PR body or reply describes only checks that have already run, with their evidence; anything not yet run is listed as unverified.
- **Merged is not released.** Main moves on a passing verdict, but a version tag (`v0.N`) is created only after the owner has used that sprint end to end and said it works. Each sprint ends with a short retro: what slipped, and the check that now catches it.

## Hard rules

- **This repository is public.** Never commit real financial data: no CAS PDFs, tradebooks, account numbers, PAN, holdings, or screenshots of real data. Fixtures are synthetic. Real data lives only under `data/` on the server, which git ignores.
- **Read-only.** No code path places, modifies, or cancels an order. Broker tokens get the narrowest read scope offered.
- **Secrets** never go in the repo, the database, logs, or an LLM prompt. Use `sops` + `age` on the server.
- **Money** is integer paise. Never a float.
- **Instruments** are keyed by ISIN.
- **Imports** are idempotent: the same input file yields the same database.
