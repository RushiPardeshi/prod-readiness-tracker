# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

---

## What this project is

A **provider-agnostic production-readiness tracker** for code. It analyzes a
repository and its pull-request diffs against the **12-factor methodology** plus
a 13th **resilience & runtime-correctness** dimension, and records every
violation as **tracked technical debt with a severity** — not a one-shot flag.

It is a *tracker*, not a *flagger*: findings persist in a ledger, keyed by a
content-based fingerprint, so the same issue survives edits and you get a debt
**trend per service** instead of the same warnings re-firing on every commit.

### Core mental model — two things, kept separate

- **`checks.yaml` = the ruleset.** The definitions: *what a violation looks
  like*. Stateless. Changes only when the taxonomy changes.
- **The ledger = the tracked instances.** Every time a check matches real code,
  that match becomes a persisted **debt item** with its own lifecycle. This is
  the product.

One check (e.g. `resilience.missing-io-timeouts`) can produce many ledger rows
across many repos over time.

### Two design commitments (do not violate)

1. **Code is ground truth, never the LLM conversation.** Production-readiness is
   a property of the artifact in git, not the chat that produced it. We analyze
   diffs/repos. We never trust an AI's self-report ("I added graceful
   shutdown") — every finding must cite a code location, or it doesn't exist.
   This is also what makes the tool provider-agnostic: it doesn't care whether
   Claude, Copilot, Codex, or a human wrote the code.
2. **Provider-agnostic and platform-agnostic.** No dependency on any specific
   AI tool's telemetry or hooks. It runs on any repo in any CI.

---

## The taxonomy (13 dimensions)

The full, machine-readable taxonomy lives in **`checks.yaml`** at the repo root —
that file is both the spec and the engine's config. Do not duplicate check
definitions anywhere else; add or edit checks there.

- **Factors 1–12**: the 12-factor methodology (codebase, dependencies, config,
  backing services, build/release/run, stateless, port binding, concurrency,
  disposability, dev/prod parity, logs, admin processes). This is the
  **operability / deployability** axis — a recognized industry standard.
- **Dimension 13 — `resilience`**: the runtime-correctness concerns 12-factor is
  silent on (I/O timeouts, data races, idempotency, retries/backpressure,
  transaction atomicity). This is where the highest-severity S1 findings live.

> ⚠️ **Factor 8 ("concurrency") is process-model scale-out, NOT thread-safety.**
> Data races and unsynchronized shared state belong to `resilience`, not
> Factor 8. Keep these two senses of "concurrency" separate — conflating them
> under-detects the S1 race conditions.

### Scoring axes (three, independent — see `checks.yaml`)

- **Severity** — `S1` Critical / `S2` High / `S3` Medium / `S4` Low.
- **Confidence** — 0–1. `det`/`dyn` checks are 1.0; `judge`/`abs` come from the
  model. This is the noise-control knob, separate from severity.
- **Archetype gate** — `server` / `worker` / `cron` / `cli` / `lib`. A check only
  fires if the service *is* the relevant kind of thing. This is what makes
  absence-detection (e.g. "missing SIGTERM handler") tractable instead of noisy.

### Detection classes

- `det` — deterministic (semgrep / tree-sitter / file-presence). No LLM.
- `abs` — absence check: archetype-gated search of expected sites. Hybrid.
- `judge` — LLM semantic analysis of the diff + context. Must cite evidence and
  pass an adversarial verify.
- `dyn` — dynamic: actually run build/tests in CI.

---

## Architecture — the pipeline

Runs as a CLI, per PR (or on demand). Stages:

```
 (0) Repo profiler    Detect archetype, language, frameworks, entrypoints,
        │             Dockerfile/CI/manifest presence → repo_profile
        │             (Also the primary detector: most 12-factor checks are
        │              file-presence / config-shape checks resolved here.)
        │
 (1) Deterministic    semgrep + tree-sitter + artifact scan → findings[]
        │             (the `det` checks; cheap, instant, ~zero false positives)
        │
 (2) Dynamic runner   Execute build + test commands in CI → findings[]
        │             (the `dyn` checks; ground-truth of ground-truth)
        │
 (3) LLM judge        `judge`/`abs` checks only, gated by archetype. Feed
        │             {diff + context + repo_profile + check.judge_prompt}.
        │             Structured output (Pydantic); evidence citation MANDATORY.
        │
 (4) Adversarial      For each judge finding, a second pass whose job is to
        │  verify       REFUTE it (check.verify). Survivors only. Primary
        │               anti-hallucination / false-positive defense.
        │
 (5) Scorer + gate    Map to taxonomy; severity × confidence; archetype gate;
        │             routing (see checks.yaml `routing`).
        │
 (6) Ledger upsert    Dedup vs prior runs by fingerprint. Compute
        │             new / still-open / resolved. Record check version.
        │
 (7) Surface          PR comment for high-sev × high-confidence only (avoid
                       fatigue). Everything else → backlog/dashboard. Trend.
```

**Where `checks.yaml` is consumed:** `applies_to` → stage 0 gating · `signals`
→ stage 1 · `command_hint` → stage 2 · `judge_prompt` → stage 3 · `verify` →
stage 4 · `severity`/`routing` → stage 5 · `remediation` → stage 7. The engine
has **zero check logic hardcoded** — adding a check to `checks.yaml` makes it
live.

---

## The ledger

The stateful half of the system. A debt item:

```
id
repo, path, anchor            -- symbol/function, NOT a raw line number
check_id, category            -- references checks.yaml
severity, confidence
status                        -- open | acknowledged | resolved | wontfix | accepted
evidence                      -- code location + snippet + rationale
archetype_context
fingerprint                   -- hash(check_id + enclosing_symbol + normalized_snippet)
check_version                 -- which checks.yaml version found it
first_seen_sha, first_seen_at
last_seen_sha,  last_seen_at
assignee
```

**Stable identity is the hard problem.** Never key debt on `file:line` — line
numbers shift and the same issue re-reports as "new" every commit (the failure
that gets trackers uninstalled). Key on the **content + structural fingerprint**
above. **Stamp `check_version` on every finding** so tightening a rule doesn't
look like a code change.

---

## Tech stack

- **Language / runtime:** Python 3.12+, environments via **`uv`**.
- **CLI:** **Typer**.
- **Config + validation:** **Pydantic v2** — models `checks.yaml` *and* validates
  LLM-judge output (structured outputs). One schema library for both.
- **Deterministic analysis:** **semgrep** (subprocess; polyglot, analyzes any
  target language) + **tree-sitter** (AST checks semgrep can't express) +
  plain file-presence/regex for repo-artifact checks.
- **Dynamic analysis:** subprocess execution of detected build/test commands.
- **LLM (judge + verify only):** provider-agnostic interface in
  `prod_tracker.llm`. Default provider is **Anthropic**, default model
  **`claude-sonnet-5`** (starting point while precision is being proven; may
  move up to `claude-opus-5` later). Keep **OpenAI** available as a switchable
  provider, default model **`gpt-5`**.
  - Switch with `--llm-provider openai|anthropic`, `--llm-model ...`, or
    `PROD_TRACKER_LLM_PROVIDER` / `PROD_TRACKER_LLM_MODEL`.
  - Control reasoning depth via the provider adapter (`high`/`xhigh` for the
    judge, `low` for the cheap verify pass); do not leak provider-specific
    settings outside the adapter.
  - **Structured outputs** must validate against the Pydantic `Finding` model
    at the SDK/provider layer where supported, with no hand-parsing.
  - **Prompt caching** on the stable prefix (repo profile + check definitions)
    so many findings in one run share a cached prefix. Keep that prefix
    byte-stable; put the volatile diff last.
  - The LLM is used **only** for `judge`/`abs` and the verify pass. Never for
    `det`/`dyn`, and never as the source of truth for whether a practice exists.
- **Ledger:** **SQLite** (local/CI) with a schema that ports to **Postgres**
  (hosted).
- **GitHub integration:** post PR comments via the REST API using the CI's
  `GITHUB_TOKEN`.
- **Distribution:** **Docker-based GitHub Action** (target repos need no Python
  env); `uv` for local runs. Graduate to a hosted GitHub App + dashboard only
  after check precision is proven.

### AI usage rules (this repo builds an AI feature — follow these)

- Default provider/model is **Anthropic `claude-sonnet-5`**. OpenAI **`gpt-5`**
  remains supported behind the same provider interface for easy switching.
- Do not scatter provider-specific knobs through judge/verify. Keep SDK-specific
  options inside the provider adapter.
- Every judge finding **must** carry an evidence location; the verify pass
  defaults to "refuted" when uncertain.

---

## Repository layout

```
pyproject.toml         # deps + `prod-tracker` entry point
.python-version        # pinned runtime (dev/prod parity — factor 10)
checks.yaml            # the taxonomy: spec + engine config (source of truth)
CLAUDE.md              # this file
src/prod_tracker/
  cli.py               # Typer entrypoint — `scan`, `rules`
  models.py            # Pydantic: Check/Dimension/Ruleset, RepoProfile, Finding
  ruleset.py           # load_ruleset(checks.yaml)
  profiler.py          # stage 0 — archetype + repo_profile
  pipeline.py          # orchestrates the stages → RunReport
  ledger.py            # stage 6 — SQLite debt store (stub)
  detectors/
    deterministic.py   # stage 1 — semgrep/tree-sitter/artifact  (stub)
    dynamic.py         # stage 2 — build/test runner              (stub)
    judge.py           # stage 3 — LLM judge, provider adapter     (stub)
    verify.py          # stage 4 — adversarial refutation         (stub)
tests/
  test_ruleset.py      # loads checks.yaml, asserts taxonomy shape
action/                # Dockerfile + action.yml for the GitHub Action (planned)
```

Stages 5 (score/gate) and 7 (surface) are TODO inside `pipeline.py` for now.

---

## Conventions & principles

- **False-positive discipline is existential.** A tracker that cries wolf gets
  turned off. Prefer precision over recall; low-confidence findings go to the
  backlog, never to an auto PR comment.
- **v1 scope is narrow on purpose.** Ship the `priority: v1` checks in
  `checks.yaml` first (mostly `det` + the two headline `judge` checks:
  statelessness, disposability, plus timeouts and shared-state races from
  `resilience`). Prove precision, then expand. Do not enable the full taxonomy
  on day one.
- **Deprioritize Factor 1 (`codebase`) for v1** — "structure makes sense" is
  subjective and the most likely to generate noise. Keep it advisory /
  low-confidence.
- **Match code style to its surroundings.** New code should read like the file
  it's in.

---

## Commit workflow

- Work on **feature branches** (`feat/…`, `fix/…`, `chore/…`), one reviewable
  PR per chunk of work — not directly on `main`. (This also lets the tracker
  eventually dogfood its own PRs.)
- **After each commit-worthy chunk of progress in a chat session, proactively
  suggest a commit message** — Conventional-Commits style (`feat:`, `fix:`,
  `docs:`, `chore:`, `refactor:`, `test:`). Leave *when* to commit to the human;
  do not auto-commit unless explicitly asked.

---

## Commands

```bash
uv sync                          # install deps into .venv (first run)
uv run prod-tracker scan .       # profile the repo, plan the pipeline, report
uv run prod-tracker rules        # list loaded checks by dimension + stage
uv run pytest                    # run tests
```

> This working copy lives under an iCloud-synced `Documents` folder, and a
> `.venv` there gets silently corrupted by cloud sync (thousands of churning
> symlinks/binaries don't survive eviction/partial sync). Locally the
> virtualenv is named **`.venv.nosync`** instead — already gitignored, and
> Python's own `venv` module self-excludes it too. If you recreate it, keep
> the `.nosync` naming (`python3 -m venv .venv.nosync && .venv.nosync/bin/pip
> install -e ".[dev]"`, or `uv venv .venv.nosync` once `uv` is installed).
> The deterministic detector's own directory-exclusion list (see
> `_is_ignored_dir` in `detectors/deterministic.py`) already treats any
> `*.nosync` directory as vendored/ignorable, same as `.venv`/`node_modules`.
