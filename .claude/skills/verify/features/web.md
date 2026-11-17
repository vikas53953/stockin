# Local web app

`stockin web` serves the same holdings and reconcile views in a browser at `http://127.0.0.1:8765`, with an upload form for a CAS. It listens on 127.0.0.1 only and refuses other Host headers and cross-site posts. The only write is a statement import, which goes through the same path as `stockin import cas`.

## Sub-features

- `web-empty`: with no ledger, `/` says "No statements yet" and shows the upload form.
- `web-import`: uploading each synthetic fixture shows the import report (new facts, already present) and that account's holdings.
- `web-holdings`: `/` lists holdings by account with a reconcile chip.
- `web-reconcile`: `/reconcile` lists every check, mismatches first.
- `web-reject`: a bad file shows "Not imported" with the uploaded file's name, and nothing is written.
- `web-local-only`: a request with `Host: evil.example:8765` or a POST with a foreign `Origin` gets 403.

## How to get to it (user POV)

- Run `stockin web`, open the printed address, pick the CAS file, type its password, press Import.

## Driving it with the stockin CLI

Preconditions: the baseline in [README.md](./README.md).

- Start `stockin web --port 8765` in the background, and wait for the "running at" line.
- Drive it with Playwright (Chromium is preinstalled; `require('playwright')`): open `/`, `setInputFiles('#statement', …)` for each of `tests/fixtures/cas/{mf-2026-01,mf-2026-01-to-02,demat-2026-01,demat-2026-02}.json`, and click Import. Expect `5 new facts`, then `3 new facts, 3 already`, then `3 new`, then `2 new`.
- On `/`, the ISINs shown (`.isin`) are exactly `INE009A01021, INF179K01BE2`, with 13,010.60 and "unknown". The chip reads `reconcile OK`.
- On `/reconcile`: `OK: no mismatches`, with 3 match and 4 inferred.
- At a 390px width, `document.documentElement.scrollWidth <= innerWidth`.
- `curl -s -o /dev/null -w '%{http_code}' -H 'Host: evil.example:8765' http://127.0.0.1:8765/` prints 403.
- Save screenshots to `$RUN/evidence/`. Stop the server when you are done.

## Gotchas

- The server keeps running until it is stopped. Kill it with `pkill -f "stockin web"` at cleanup, and only kill the one this run started.
- A real PDF needs its password. Verification uses JSON fixtures only.
