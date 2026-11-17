---
name: verify
description: Drive the Stockin CLI the way its owner does and capture proof. Use before calling any Stockin change done, to check that a command works against the real installed binary, not just pytest.
---

# Verify Stockin

Stockin's surfaces are the `stockin` CLI and the local web app (`stockin web`, see [features/web.md](features/web.md)). Telegram alerts and the MCP server arrive in later sprints, and each will get its own section here. Every command takes `--json`, which prints the same object the human output is drawn from. Prefer `--json` for assertions and keep the human output as evidence.

**Never point a run at real data.** This repository is public. Verification uses a disposable `STOCKIN_DATA` folder and synthetic fixtures only. Never read, copy or print anything from the server's real `data/` folder.

## Launch

The CLI is short-lived, so launching means installing the current checkout as a tool and making a fresh data folder:

```bash
RUN=$(mktemp -d /tmp/stockin-verify-XXXX)
mkdir -p "$RUN/data" "$RUN/evidence"
uv tool install --force .          # from the repo root; puts `stockin` on PATH
export STOCKIN_DATA="$RUN/data"
```

It is ready when `stockin version` prints the version from `src/stockin/__init__.py`.

## Doctor

Run `stockin doctor --json` first, and again whenever something looks wrong.

- Require `"ok": true`, and require `data_dir` to equal `$RUN/data`. If it points anywhere else, stop: you are about to drive someone's real ledger.
- Exit code `0` means usable. `1` means a `fail` check, which names the fix in `hint`.

## Drive

Run each command through the installed binary, never `python -m` or a test helper, and capture stdout, stderr and the exit code:

```bash
cmd() { name=$1; shift; { "$@"; echo "exit=$?"; } > "$RUN/evidence/$name.txt" 2>&1; }
cmd doctor-json stockin doctor --json
```

The per-feature recipes are in [features/README.md](features/README.md).

## Evidence

- For each command, save the command, its stdout, stderr and exit code to `$RUN/evidence/<feature>-<step>.txt`.
- For a command that writes, also save a second read-only view of the result, such as `stockin doctor --json` or, once it lands, `stockin reconcile --json`.
- Put the evidence directory's listing and the key lines in the PR's Verification section.

## Cleanup

```bash
rm -rf "$RUN/data"                 # keep $RUN/evidence: it is the proof
```

Don't uninstall the tool, since another run may be using it. Only ever remove the `$RUN` that this run created.
