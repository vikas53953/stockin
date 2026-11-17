# Learnings

What this project has taught us about building it, newest at the bottom.
Each entry is a moment something went wrong or the owner pushed back, the
lesson, the rule it produced, and what that rule has caught since. Add entries
at every sprint retro and whenever the owner corrects the process. The rules
themselves are in `CLAUDE.md`; their history is in [PIPELINE.md](PIPELINE.md).

## Sprint 1, drop 1 (2026-11-17)

### 1. Passing tests did not mean a working import
- **What happened:** pytest was green, but the first run of the installed `stockin import cas` printed casparser enums as `TransactionType.PURCHASE`, because the library returns enum members and plain strings in different paths.
- **Lesson:** the real surface finds what unit tests built from our own assumptions cannot.
- **Rule:** a change is done when it has run on the installed binary through the `verify` skill (PIPELINE #1).
- **Caught since:** the busy-port crash in `stockin web`, phone overflow on the reconcile table.

### 2. The author is a poor judge of their own change
- **What happened:** two PRs that looked finished failed independent verification: a phantom holding after a flagged zero (#4), and a PDF error class the check missed (#9).
- **Lesson:** an agent that did not write the code must verify it, and its verdict, not the author's, gates the merge.
- **Rule:** independent verifier, head-SHA guard, at most two fix-forwards (PIPELINE #2).
- **Caught since:** every fix in #6, #7, #9 and #10 came from verifier notes.

### 3. A claim was written before the check ran
- **What happened:** the #10 PR body described a browser check that had not been run yet. It passed when run, but the order was wrong.
- **Lesson:** a check reported before it runs is a fake green, even if it later passes.
- **Rule:** never claim a check that has not run; evidence is attached, not described (PIPELINE #4, #7).

### 4. "Did you test this yourself?"
- **Pushback:** the owner was handed a checklist to test real CAS PDFs. Only casparser JSON had been tested; no PDF had ever gone through the app.
- **Lesson:** never ask the owner to be the first person to try something. The owner's run is confirmation, not discovery.
- **Rule:** an agent runs the owner's exact steps first, as an end user, on realistic password-protected synthetic PDFs, in a browser (PIPELINE #6).
- **Caught since:** TDS on an IDCW payout rejected the whole statement (#12); cost shows unknown for funds held before the statement period; the checklist itself gave the wrong CDSL password advice.

### 5. Test the instructions, not just the app
- **What happened:** running the owner's checklist ourselves showed two wrong instructions: "CDSL password is sometimes PAN + DOB" (CDSL's FAQ says PAN), and "cover at least 6 months" (cost needs the statement to start before the first investment).
- **Lesson:** a UAT checklist is part of the product and needs the same testing.
- **Rule:** covered by agent-first UAT; checklist fixes ship with the run that found them.

### 6. "Don't let anyone commit fake green"
- **Pushback:** the owner asked for before and after screenshots, and then video, so that no agent can report a pass without proof.
- **Lesson:** evidence must be something a reader can check: files with hashes, not a sentence.
- **Rule:** before/after screenshots, video for multi-step browser flows, raw output with exit codes, sha256 in the verdict; no evidence means no merge (PIPELINE #7, #8).

### 7. A new test can pass for the wrong reason
- **What happened:** the first set of property tests passed with the #4 fix reverted, so they did not guard against the bug that motivated them.
- **Lesson:** a test only counts once you have seen it fail on the bug it is meant to catch.
- **Rule:** property tests are a gate, and every new test is mutation-checked by reverting the fix (PIPELINE #5).
- **Caught since:** the gap itself; a demat property was added and now catches the revert.

### 8. Small drops, real QA between them
- **Pushback:** no large codebase without real QA; split features into small batches the owner tests end to end.
- **Lesson:** merged is not released. Main can move fast; a version tag waits for the owner.
- **Rule:** drop 1 / drop 2 split, `v0.N` tag only after the owner's run, retro at sprint end (PIPELINE #3).

### 9. The repository is public
- **Lesson:** every fixture, screenshot and log could be read by anyone. Synthetic data only, invented names, a fake PAN; the owner never sends a PDF or password.
- **Rule:** hard rules in `CLAUDE.md`; evidence screenshots show synthetic data only.
