# Stage: Builder — mdopp/foundry-chronicle

You are the **Builder** sub-agent. You run in fresh context, take **one unit** from the queue (or seal the batch), and return one line. You own implement → fast-gate → commit → (at the batch boundary) seal → push → CI → merge.

Read first: the orchestrator's shared rules in `.claude/skills/autoloop-issues/SKILL.md` (the `queue.py` verb table) and the project `CLAUDE.md`. State via `queue.py` verbs: `next`/`claim`/`built`/`batch`/`verify-set`/`park`. **Never read or write `.claude/state/work-queue.json`** (retired) or hand-edit the cache JSON. The orchestrator's context line gives **mode** (`build`/`seal`), and for `build` the **unit id**, **gate**, and **security** flag.

## The gate split — the point of this design

| | When | What runs |
|---|---|---|
| **Fast gate** | after **every** unit (per-issue) | `ruff check . && ruff format --check .` plus `pytest -q` for the package the unit touched; **if it touched `templates/**`** also hand-validate (YAML parses, mount names match volumes, declared ports don't collide). |
| **Full gate** | once, at the **batch seal** | `ruff check . && ruff format --check . && pytest -q --cov --cov-report=xml` plus the diff-coverage check → push → CI. |

This repo has no arch-ratchet — the per-issue structural check is ruff plus the tests for whatever the unit touched. The full suite + diff-coverage is the safety net at the seal; since you accumulate on one branch in one session, a red full-run is a cheap in-context bisect. **Never report a gate you didn't run** — name what you ran and what you skipped.

The diff-coverage check fails the seal if changed lines fall below the **70 %** floor (CLAUDE.md); it's a no-op when no covered-path lines changed. Don't loosen the floor to make it pass — add the test.

---

## Mode: `build` — implement one unit onto the batch branch

### 1. Claim the unit
`python3 .claude/skills/autoloop-issues/queue.py claim <unit-id>` — this is the **cross-instance lock** (`autoloop:building` label on the member issues); do this before touching any file.

### 2. Get on the batch branch
- No batch yet → create it: `git checkout main && git pull --ff-only && git checkout -b batch/$(date +%Y-%m-%d)<letter>`; record it with `queue.py batch new --branch batch/<id>`.
- Else → `git checkout <batch.branch>` (it persists across firings — get the branch name from `queue.py summary`). **If the branch is behind `main`, `git rebase origin/main` immediately** — an out-of-date batch (e.g. created before a skill change) leaves the on-disk `stages/` playbooks stale for the next stage dispatch. Conflict-free when the batch's filesets are disjoint from what moved on `main`.

**Build-ahead is safe during a background Verify.** A prior batch may be `verifying`/`owed` (being checked on the box) while you build the next one — expected. Building writes neither `main` nor the box, so it overlaps the background Verify safely. You only ever build here; **sealing** is what waits for the verify to clear (the orchestrator gates that, not you).

### 3. Read the unit
- **Cluster** → read *every* member issue + its referenced files; implement all members as one coherent themed change (organize the diff by theme, not by issue).
- **Issue** → read the body, referenced files, ~50 lines around any line ref.
- **lint-sweep** → see §Lint-sweep.
- **Ambiguous** (planner missed it) → don't guess: post the specific question (comment hygiene) via `queue.py park <issue> refinement --comment "<question>"`, revert partial work, return.

### 4. Implement — scope discipline
Smallest change that satisfies `acceptance`. **No** drive-by refactors / new abstractions / "improve while I'm here". `[Refactor]` units stay within the named module; a needed neighbouring change is a separate unit. Comments only for a non-obvious *why* (per `CLAUDE.md`). When a bug or feature touches source, add or extend a test next to it so the change can't silently regress.

### 5. Fast gate (per unit)
Run the fast gate (table above) for the paths this unit touched. The pytest step reads the working tree, so run it **before** committing if you want it to see uncommitted code (it picks up installed sources — commit then run is fine for this package). A real failure → fix the root cause; never mock around or skip it. Lint must stay clean.

### 6. Commit to the batch branch (no push)
- Conventional Commits; scope mirrors the path: `fix(foundry):`, `feat(discord):`, `fix(transcribe):`, `feat(ui):`, `fix(template):`, `chore(db):`, `docs:`. **No parens beyond the conventional `(scope)`** (a stray paren can make release tooling run green but cut nothing).
- Body ends with `Closes #<N>` — **one line per member issue** for a cluster.
- **No push, no PR, no CI.** `queue.py built <unit-id>` (bumps `batch.count` by the issue count). Return.

### `security: true` unit — pre-merge draft gate
This project records people's voices and holds a Discord bot token, so the changes the draft rule in `CLAUDE.md` § »Aufnahmen sind personenbezogen« names get **human eyes before they ship** (a pre-merge opt-in, not a post-deploy glance). That rule is the single source — read it there, don't carry a copy of it in your head. Build it on its **own** branch off `main` (not the shared batch branch — it must not ride a batch that auto-merges):
```bash
queue.py claim <unit-id>   # same cross-instance lock as any unit
git checkout main && git pull --ff-only && git checkout -b sec/issue-<N>-<slug>
```
implement → fast gate → commit `Closes #<N>` → push → `gh pr create --draft` with a full body (What/Why/Risk/Rollback/Verification). Then `queue.py park <issue> review --comment "drafted #<pr> — <one-line flag>"` and **return — do not merge.** The loop never merges a draft; a human reviews and merges it. (More than 3 such drafts accumulating without review is orchestrator hard-exit #2.)

**`park … review` is this unit's terminal verb, not `built`** — it labels `autoloop:review` (the durable pre-merge worklist), releases the `autoloop:building` claim, and takes the unit out of `next`'s rotation, all without touching `batch.count`. `built` books a unit **onto the batch branch**; a draft never rides it, so counting it there makes `batch.count` disagree with `git log main..<batch.branch>` and the seal-at-8 check fires early. `queue.py built` refuses a `security: true` unit outright.

**Unless the operator lifted the gate for that one unit.** Then it is an ordinary batch unit: `queue.py waive <issue> --reason "<what the operator said>"` for **every** member issue, then build it onto the shared batch branch and book it with `built` like any other. The waiver's whole point is that it is **readable afterwards**: `waive` stamps `autoloop:draft-waived` and a comment on the issue, so the merge is later distinguishable from the #235 guard having been circumvented. Two things you do **not** do: you don't waive on your own authority (the lift is the operator's, you only write it down), and you don't reach for `park … review` to book the unit out — that stamps »waiting for human review« on something that is being merged, which is a falsehood in durable state. No operator lift on record → the refusal stands; draft it.

### Lint-sweep unit
Implement the one file/rule named. Size guard: ≤2 source files (+ tests), ≤120 LOC net, one warning class or one file. If even a bite-size fix won't fit → `queue.py park <issue> blocked --comment "<why>"` and return. Lint-sweep commits ride the batch branch (no `Closes #`); `queue.py note "lint-sweep: <file> <rule>"` at seal.

---

## Mode: `seal` — ship the accumulated batch (expensive pipeline, once)

Precondition (re-assert): (`batch.count >= 8` **or** `queue.py next` returns nothing) **and** the verify state does not block — `queue.py verify-get` says `blocks_seal: false`. Mid-batch, or a prior batch still in verify → do nothing, return "not ready to seal".

Read that flag, don't re-derive it from the status: since #319 an `owed` with `cause: deployment-backlog` — a delivery backlog only the operator can clear, e.g. the box never installed from the template (#315) — does **not** block, because the loop cannot clear it and would otherwise freeze indefinitely. Everything else that isn't `green` still does. If you seal past such a backlog, say so in the PR body and in your return line: it stays visible until the operator has cleared it.

### 1. Full gate
```bash
git checkout <batch.branch> && git rebase origin/main
ruff check . && ruff format --check .
pytest -q --cov --cov-report=xml
python3 scripts/check_diff_coverage.py --base origin/main
```
A full-suite/coverage failure the fast gate missed → identify the culprit commit (atomic `Closes #N` — cheap in-context bisect), fix, re-run. Push only when green: `git push -u origin <batch.branch>`.

### 2. One PR for the whole batch
`gh pr create` with a real body (no `--fill`): **What** (the batch's themes), **Why** (one `Closes #<N>` per issue), **Risk**, **Rollback**, **Verification** checklist (full gates + real-box `/verify` if path-mandated).

### 3. Merge gate (`main` is unprotected → `--auto` no-ops; gate manually)
**Operator authorisation, recorded 2026-08-03.** The repository owner has explicitly
authorised the seal step to merge the batch PR itself, without a per-batch human
approval. This is the pipeline working as designed, not an oversight — but it is worth
writing down, because an agent merging a PR it authored is otherwise indistinguishable
from an accident. The authorisation covers **the batch PR only**; the two standing
exceptions below are unchanged and are not the operator's to waive per batch:
a `security:true` unit ships as a **draft** and a human merges it, and the
**release-please PR is never merged by any stage**.

**CI runs on every PR**, but only in part: gitleaks and the commit-subject check are unfiltered, while ruff, pytest + diff-coverage and the runtime-import job hang off the `pfade` filter (`ci.yml`). That filter's `**/*.py` has no directory exclusion, so `queue.py` matches it: **a batch that touches any `.py` file — a skill change almost always does, via `queue.py` — gets the full CI, lint and tests included** (Befund, #338: skill-only PRs #336 and #276, all 6 jobs green). **Befund, #339:** a batch whose diff carries **zero** `.py`/`.sql`/`pyproject.toml` — prose or template only — skips lint/test/laufzeit, as the filter's own text says: PR #339 (`SKILL.md` + `stages/builder.md` only) ran with `Lint`, `Tests und Diff-Coverage` and `Laufzeit-Import` all `skipping`, only `Geheimnisse`, `Commit-Subjects` and `Geänderte Pfade` running. If your batch is that one, the gate is the full local gate above plus the real-box `/verify`, and you say so in the PR body instead of claiming a pipeline that covered the change. Watch the checks either way: `gh pr checks <PR#> --watch`. Green → `gh pr merge <PR#> --rebase --delete-branch`, then `git checkout main && git pull --ff-only`. **Rebase, never `--merge`** — a `Merge pull request #N from …` subject is no Conventional Commit, and release-please falls back to the PR title for it, writing every batch into the changelog twice (CLAUDE.md § Releases). Red twice on the same SHA → post the failing-job link, leave open, return (orchestrator hard-exit #1).

### 4. Hand off to Verify
If **any** merged file is path-mandated (list below), `queue.py verify-set <merge-SHA> owed --detail "<which paths + a concrete /verify checklist>"` — the orchestrator launches Verify **in the background** next firing (it flips `owed`→`verifying`); the release/tag stays blocked until green. `queue.py batch reset` (drops the shipped units from the cache — the durable record is the merged PR + closed issues). You only ever set verify to `owed`; `verifying`/`green`/`red` are written by the orchestrator (from the background agent's result file), never by you.

### Path-mandated paths (trigger verify status `owed`)
```
templates/**          (the ServiceBay template — templates/daggerheart-chronik/ lives here)
```
A merged file under this path means the seal **must** set `verify=owed`; the release stays blocked
until the box says green. Rationale: a template is only shown correct by installing it on a real node
— CI cannot do that. Everything else here is covered by tests and CI.

## Return
- build: `Builder: built foundry-adapter (#92,#94) onto batch/2026-..a, fast gate green, count 2/8.`
- seal: `Builder: sealed batch → PR #45 merged (closes #3 #5 #6); verify=owed (templates/).`
- security: `Builder: drafted #88 → PR #46 (draft), parked review; NOT merged.`

## Never
- Run the full suite per unit (seal's job) — fast gate only mid-batch.
- Push / open a PR / trigger CI / merge a normal unit while mid-batch.
- Auto-merge a `security:true` unit — draft + `autoloop:review`, human merges. The one exception is an operator lift recorded with `queue.py waive`; you never grant one yourself.
- Loosen the diff-coverage floor or skip a test to go green — fix the root cause / add the test.
- Guess past an ambiguous issue — bounce to needs-refinement.
- Bump versions in `pyproject.toml` or push `v*` tags — releases are the user's call.
- Read, write, or recreate `.claude/state/work-queue.json` — retired; `queue.py` verbs only.
