# Doctor

`stockin doctor` checks the data folder, the ledger and each configured source, then prints what is missing and how to fix it. It never changes anything on disk.

## Sub-features

- `doctor-fresh`: on a new install, every check is `ok` or `warn` and the exit code is 0.
- `doctor-missing-dir`: a data folder that doesn't exist is reported as `FAIL` with a `mkdir` hint, and the exit code is 1.
- `doctor-json`: `--json` prints the same report as a JSON object with `ok`, `data_dir` and `checks`.
- `doctor-read-only`: no run creates, writes or deletes a file.

## How to get to it (user POV)

- Run `stockin doctor` right after installing (README step 1).
- Run `stockin doctor --json` from a script or from Claude.

## Driving it with the stockin CLI

Preconditions: the baseline in [README.md](./README.md), with `$RUN/data` empty.

- **Fresh install.** Run `stockin doctor`. Exit code 0, with `ok` lines for `python` and `data_dir` and `warn` lines for `ledger` and `quotes`.
- **JSON.** Run `stockin doctor --json`. Exit code 0, and the output parses with `"ok": true` and `"data_dir"` equal to `$RUN/data`.
- **Read-only.** Run `ls -A "$RUN/data"`. It is still empty.
- **Missing folder.** Run `STOCKIN_DATA="$RUN/absent" stockin doctor`. Exit code 1, a `FAIL  data_dir` line whose hint starts with `mkdir -p`, and `$RUN/absent` still does not exist.

## Gotchas

- With `STOCKIN_DATA` unset, doctor checks `./data` relative to the current directory. On the server, that is the real ledger.
- `quotes` stays `warn` until the prices PR adds a quote source. That is expected, not a failure.
