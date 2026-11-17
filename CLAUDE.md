# Stockin

A private, read-only app for one investor in Indian markets. It answers one question first: should I add to, trim, or exit this holding?

@.claude/pstack-models.md

## How we work

- Start rigorous work with `/pstack:poteto-mode`. It picks the playbook.
- Verification comes first. A change is done when it has run against the real surface, not when tests pass.
- Plan with code. Settle open questions with a prototype, not a longer document.
- One PR is one change with its own evidence. Small, ordered, reviewed by the owner before merge.

## Hard rules

- **This repository is public.** Never commit real financial data: no CAS PDFs, tradebooks, account numbers, PAN, holdings, or screenshots of real data. Fixtures are synthetic. Real data lives only under `data/` on the server, which git ignores.
- **Read-only.** No code path places, modifies, or cancels an order. Broker tokens get the narrowest read scope offered.
- **Secrets** never go in the repo, the database, logs, or an LLM prompt. Use `sops` + `age` on the server.
- **Money** is integer paise. Never a float.
- **Instruments** are keyed by ISIN.
- **Imports** are idempotent: the same input file yields the same database.
