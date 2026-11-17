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
- **What happened:** three PRs that looked finished failed independent verification before the PDF work: #3 (the docs promised `--json` on every command, `version` had none), #4 twice (the fold depended on the order facts arrived in, then a phantom holding after a flagged zero), and #9 (a PDF error class the check missed).
- **Lesson:** an agent that did not write the code must verify it, and its verdict, not the author's, gates the merge.
- **Rule:** independent verifier, head-SHA guard, at most two fix-forwards (PIPELINE #2).
- **Caught since:** every fix in #6, #7, #9 and #10 came from verifier notes.

### 3. A claim was written before the check ran
- **What happened:** the #10 PR body described a browser check that had not been run yet. It passed when run, but the order was wrong.
- **Lesson:** a check reported before it runs is a fake green, even if it later passes.
- **Rule:** never claim a check that has not run; evidence is attached, not described (PIPELINE #4, #7).

### 4. The owner asked whether we had tested it ourselves
- **Pushback:** the owner was handed a checklist to test real CAS PDFs. No statement-layout PDF had ever been imported: the imports had used casparser JSON, and the only PDFs through the app were a blank one, junk bytes and an encrypted dummy, all checking rejections.
- **Lesson:** never ask the owner to be the first person to try something. The owner's run is confirmation, not discovery.
- **Rule:** an agent runs the owner's exact steps first, as an end user, on realistic password-protected synthetic PDFs, in a browser (PIPELINE #6).
- **Caught since:** TDS on an IDCW payout rejected the whole statement (#12); cost shows unknown for funds held before the statement period; the checklist itself gave the wrong CDSL password advice.

### 5. Test the instructions, not just the app
- **What happened:** running the owner's checklist ourselves showed two wrong instructions: "CDSL password is sometimes PAN + DOB" (CDSL's FAQ says PAN), and "cover at least 6 months" (cost needs the statement to start before the first investment).
- **Lesson:** a UAT checklist is part of the product and needs the same testing.
- **Rule:** covered by agent-first UAT; checklist fixes ship with the run that found them.

### 6. Make fake green impossible
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

### 10. A fix can be wrong in a case nobody looked at
- **What happened:** #12 fixed TDS on an IDCW payout, but also took TDS off a reinvested IDCW, whose amount is already net. The verifier's own property test found it, shrunk to a 1-paisa example. Before the fix such a CAS was rejected; after it, it would have imported a wrong amount silently, and re-importing the same file cannot correct a stored fact.
- **Lesson:** the riskiest bug turns a loud rejection into a quiet wrong number.
- **Rule:** fix-forward with a test asserting Income equals the net Buy; the verifier mutation-checks it.

### 11. Commit before a mutation check
- **What happened:** during a mutation check, `git checkout -- file` restored the file to the last commit and wiped the uncommitted fix along with the mutation. It was noticed from `git diff --stat` and re-applied.
- **Lesson:** mutations are reverted with git, so the real change must already be in git.
- **Rule:** commit (a WIP commit is fine) before injecting mutations.

### 12. Properties can look strong and still miss most bugs
- **What happened:** the #11 verifier injected 10 bugs of its own; the new properties caught 4. One property could not fail (share + rest equals the total by construction), about half the retract examples checked nothing, exact .5 ties were almost never drawn, and nothing checked money in the ledger.
- **Lesson:** the author's mutation check uses bugs the author already thought of; the verifier's finds the ones they did not.
- **Rule:** every new or changed test is mutation-checked by the verifier with its own mutations (PIPELINE #10).
- **Caught since:** the #14 verifier confirmed five of the six gaps closed, including a ledger read (`Ledger._latest`) that had no test at all; the sixth (Indian grouping) is caught by a unit test. Its own new mutations then found per-lot proceeds unchecked (#15).

### 13. History needs checking too
- **What happened:** the first version of this file and PIPELINE.md said two PRs had failed verification; the record shows #3 and #4's first FAIL as well. One claim ("no PDF had ever gone through the app") overstated what was true.
- **Lesson:** a log written from memory drifts from the record, and a flattering log teaches the wrong lesson.
- **Rule:** docs about our own process are verified against PR comments and git history (PIPELINE #11).
- **Caught since:** #19 FAIL. A new lesson said none of the ten apps studied shows SIP discipline or long-term lot dates; the teardowns it cited show Zerodha Console, Value Research and Kuvera covering parts of both, and the catalogue had only said "none of four, inferred". The same claim had gone to the owner and was corrected.

### 14. Evidence files can change after you hash them
- **What happened:** the #13 verifier found that the project share adds a metadata chunk to PNG screenshots a few seconds after they are copied. Hashes taken right after the copy failed `sha256sum -c` twenty seconds later, though the pixels were identical.
- **Lesson:** a hash proves nothing if the file can legitimately change under it; the evidence store has to be settled before it is fingerprinted.
- **Rule:** fingerprint evidence with `evidence_hash.py`, which hashes every byte of a PNG except the share's own `caBX` chunk, and only where the share puts it (PIPELINE #13).
- **Caught since:** three fixes failed verification on #16. "Wait until two hashes 30 seconds apart agree" passed, then the share rewrote every PNG at about two minutes (other times it took eleven, twenty-seven and forty-two; there is no known upper bound): timing rules for a process you do not control are guesses. Then hashing only the image data let the verifier add APNG frames that make an "after" screenshot show the "before" page, with the fingerprint unchanged. Then skipping `caBX` anywhere let a `caBX` placed between IDATs hide the bottom of a screenshot. Exclude exactly what you must, and nothing more. The fourth attempt skips it only as the second chunk and passed (PASS+NOTES).

### 15. Agent-first UAT ran on the wrong operating system
- **What happened:** the owner ran the checklist on Windows. Its data-folder step, `mkdir -p ~/stockin-data && $env:STOCKIN_DATA=...` (the Unix line with a Windows note to swap `export` by hand), fails to parse in PowerShell: 5.1 has no `&&`, and `$env:X=...` after `&&` is not valid in 7 either. Its install line also used a `git+` URL, which needs Git installed. Our own UAT run had used Linux and bash only, so it passed. (The checklist lives in the project files, not in git, so its old text is quoted from the owner's error message.)
- **Lesson:** running the owner's steps first only counts if it runs them where the owner runs them: same OS, same shell, same version.
- **Rule:** the checklist gives one command per line per OS, with no hand edits. CI runs its PowerShell lines on `windows-latest` in both PowerShell 5.1 and 7 (PIPELINE #14); outside CI, lint them with PSScriptAnalyzer `PSUseCompatibleSyntax`/`PSUseCompatibleCommands`. On the old line it reported four findings: a parse error, `&&` (not in 5.1), `mkdir` (not in 5.1) and `-p` (not on 7.0); on the new lines, none. Install now uses the zip URL, so Git is not needed.

### 16. Synthetic statements are not the real risk
- **What happened:** the owner has no CAS of his own yet, so he is testing drop 1 on our synthetic PDFs. They copy the CAMS, NSDL and CDSL layouts as the parser expects them, not as measured from a real statement.
- **Lesson:** a pass on files we made shows the app agrees with our idea of the layout, not with the issuers'. Whether a real CAS imports correctly is still open.
- **Rule:** every report about drop 1 says it used synthetic PDFs. The real-statement risk stays open (last row of the drop 1 retro) until a real CAS has been imported end to end. (Whether that should gate `v0.1` is the owner's call and has not been asked yet.)

## Sprint 1, drop 1 retro (2026-11-17)

The owner ran the whole drop 1 checklist on Windows with the synthetic sample PDFs, and every step worked as expected (accepted on synthetic data; no tag until drop 2 is accepted too).

| What slipped | The check that now catches it |
|---|---|
| UAT was asked of the owner with PDFs only tested as JSON (lesson 4) | Agent goes first on UAT, with password-protected PDFs and a real browser (PIPELINE #6) |
| The Windows data-folder step failed to parse in PowerShell (lesson 15) | Windows install job on PowerShell 5.1 and 7 (PIPELINE #14) |
| TDS double-deducted on a reinvested IDCW (lesson 10) | An independent verifier reads the code and writes its own property tests (PIPELINE #2, #5) |
| Evidence hashes went stale on the project share (lesson 14) | `evidence_hash.py` with `--check` (PIPELINE #13) |
| Process docs drifted from the record (lesson 13) | Docs audited against PR comments and git history (PIPELINE #11) |
| No real CAS has been through the app (lesson 16) | Not caught yet: open risk |

### 17. The owner wants numbers turned into learnings
- **What happened:** after using drop 1, the owner said plain tables are not enough. Data should be shown visually, and Stockin's moat is translating numbers into patterns the user understands and learns from. A study of ten apps (`/mnt/project-files/stockin/re/`) found the pattern most clearly in INDmoney (a chart, one sentence and a "Learn how" link) and Tickertape (a band name and one next step). Some apps already cover parts of behaviour and tax timing: Zerodha Console lists each lot's days held and long- or short-term status, Value Research shows unrealised short- and long-term gains, and Kuvera's Discipline lens tracks SIP behaviour, though how it looks is unknown. A CAS alone has the data for both.
- **Lesson:** the product is the explanation, not the table. An explanation that disagrees with its own chart is worse than none (one app's own mock-up says 32% in the sentence and 35% in the legend).
- **Rule:** insights are computed by code from the same numbers as their chart, and a test asserts the sentence and the chart agree. No model writes a number. Design directions are shown to the owner as options before the UI is built.

