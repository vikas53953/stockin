# Stockin verification map

This is the maintained list of what a user can do with Stockin and how to prove each thing works. Read [the verify skill](../SKILL.md) first for launch, doctor and cleanup.

## Baseline preconditions

- `stockin` is installed from this checkout with `uv tool install --force .`
- `STOCKIN_DATA` points at a fresh, empty folder created by this run.
- All fixtures are synthetic. Never use a real CAS, tradebook or statement.

## Feature entry contract

Each feature file has an H1 and one paragraph, followed by four H2s: `Sub-features`, `How to get to it (user POV)`, `Driving it with the stockin CLI`, and `Gotchas`.

## Features

| Feature | Status |
|---|---|
| [Doctor](./doctor.md) | Working |
| [Import a CAS and list holdings](./import-cas.md) | Working |
| [Net worth: assets, target mix, `stockin networth`](./networth.md) | Working (CLI); web page in Sprint 2 PR 2 |
| [Reconcile](./reconcile.md) | Working |
| [Local web app](./web.md) (`stockin web`) | Working |
| Thesis and review (`stockin thesis add`, `stockin review`) | Not built yet |
| [Alerts](./alerts.md) (`stockin alerts check/list/connect/send/test`) | Working |

Each "not built" row gets its own file in the PR that builds it.
