# Stockin

A private, read-only app for one investor in Indian markets. It answers one question first: should I add to, trim, or exit this holding?

@.claude/pstack-models.md

## How we work

- Start rigorous work with `/pstack:poteto-mode`. It picks the playbook.
- Verification comes first. A change is done when it has run against the real surface, not when tests pass.
- Plan with code. Settle open questions with a prototype, not a longer document.
- One PR is one change with its own evidence. Small and ordered.
- **Auto pipeline (owner's standing go, 2026-11-17).** Every PR goes through pstack's shipping flow without waiting for the owner: an agent that did not write the code verifies it (gates, a live run through the `verify` skill, and a diff audit; thermos for code PRs) and posts `PASS`, `PASS+NOTES` or `FAIL` on the PR. A PR with a passing verdict and green CI on the same head is squash-merged bottom-up with a head-SHA guard. A `FAIL` gets at most two fix-forwards, then stops and is reported.

## Hard rules

- **This repository is public.** Never commit real financial data: no CAS PDFs, tradebooks, account numbers, PAN, holdings, or screenshots of real data. Fixtures are synthetic. Real data lives only under `data/` on the server, which git ignores.
- **Read-only.** No code path places, modifies, or cancels an order. Broker tokens get the narrowest read scope offered.
- **Secrets** never go in the repo, the database, logs, or an LLM prompt. Use `sops` + `age` on the server.
- **Money** is integer paise. Never a float.
- **Instruments** are keyed by ISIN.
- **Imports** are idempotent: the same input file yields the same database.
