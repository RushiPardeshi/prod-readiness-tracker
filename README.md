# Production-Readiness Tracker (`prod-tracker`)

> **Provider-agnostic production-readiness tracker for code (12-Factor + Resilience), tracked as persistent technical debt.**

`prod-tracker` analyzes repositories and pull-request diffs against the **12-factor methodology** plus a 13th **resilience & runtime-correctness** dimension. Violations are recorded as persistent technical debt with severity and confidence scores—not ephemeral, noisy flags.

---

## Key Concepts

### 1. A Tracker, Not a Flagger
Traditional linters and scanners re-flag every issue on every commit, creating alert fatigue. `prod-tracker` persists findings into a SQLite **ledger** keyed by content and structural fingerprints (`check_id + path + stable-anchor + normalized snippet`). Issues survive line shifts, track lifecycle states (`open`, `acknowledged`, `resolved`, `wontfix`, `accepted`), and measure real debt trends over time.

### 2. Code is Ground Truth
Production readiness is a property of the git artifact, never an LLM's conversation. `prod-tracker` does not trust self-reported compliance ("*I added graceful shutdown*")—every finding must cite verifiable code locations or dynamic execution proof.

### 3. The 13 Dimensions
The taxonomy is defined in [`checks.yaml`](checks.yaml):
- **Factors 1–12 (12-Factor App)**: Codebase, Dependencies, Config, Backing Services, Build/Release/Run, Stateless Processes, Port Binding, Concurrency, Disposability, Dev/Prod Parity, Logs, and Admin Processes.
- **Dimension 13 (`resilience`)**: Runtime correctness concerns not covered by 12-Factor: missing I/O timeouts, unhandled data races, lack of idempotency, retries/backpressure, and transaction atomicity.

---

## Architecture & Pipeline

```
 (0) Repo Profiler     Detect language, entrypoints, CI, Docker, archetype (server/worker/cron/cli/lib)
        │
 (1) Deterministic     Zero-false-positive checks (lockfiles, secrets, runtime pins, logging)
        │
 (2) Dynamic Runner    Execute build & test commands in CI/local subprocess (ground-truth build/test status)
        │
 (3) LLM Judge         Semantic analysis for archetype-gated checks (requires concrete code evidence citation)
        │
 (4) Adversarial Verify Secondary refutation pass (defaults to refuted when uncertain)
        │
 (5) Scorer & Gate     Severity × Confidence routing + archetype applicability filtering
        │
 (6) Ledger Upsert     SQLite fingerprint matching (marks new, still-open, reopened, resolved)
        │
 (7) Surface           Route S1/S2 high-confidence items to auto-comment; rest to backlog
```

### Detection Classes
- **`det` (Deterministic)**: Static file and AST checks. Runs locally without an LLM. Confidence = `1.0`.
- **`dyn` (Dynamic)**: Executes build and test suites directly. Ground-truth proof. Confidence = `1.0`.
- **`judge` (LLM Judge)**: Semantic analysis for nuanced patterns (in-memory shared state, missing timeouts).
- **`abs` (Absence)**: Archetype-gated absence detection (e.g. missing SIGTERM handler on long-running servers).

---

## Installation & Setup

### Prerequisites
- Python `>= 3.12`
- [`uv`](https://github.com/astral-sh/uv) (recommended package manager)

### Install Dependencies
```bash
# Clone the repository
git clone https://github.com/RushiPardeshi/prod-readiness-tracker.git
cd prod-readiness-tracker

# Sync dependencies using uv
uv sync

# Optional: install LLM provider extras if using Judge/Verify stages
uv sync --extra llm
```

---

## Quickstart

### 1. Scan a Repository
Run a scan on the current repository or a target path:
```bash
# Standard scan (persists to .prod-tracker/ledger.db)
uv run prod-tracker scan .

# Scan without LLM stages (fast, deterministic + dynamic only)
uv run prod-tracker scan . --no-llm

# Ephemeral scan without writing to ledger
uv run prod-tracker scan . --no-ledger
```

### 2. Profile a Repository
Inspect what the profiler detects about a codebase (language, detected archetype, CI, entrypoints):
```bash
uv run prod-tracker profile .
```

### 3. List Rules and Checks
Inspect the loaded checks from `checks.yaml` grouped by dimension and priority:
```bash
uv run prod-tracker rules
```

---

## CLI Options

### `scan`
```
Usage: prod-tracker scan [OPTIONS] [path]

Arguments:
  path                        Repo or subtree to scan [default: .]

Options:
  --checks <path>             Path to checks.yaml [default: checks.yaml]
  --llm-provider <provider>   LLM provider (anthropic | openai) [default: anthropic]
  --llm-model <model>         Override provider model (e.g. claude-sonnet-5, gpt-5)
  --no-llm                    Skip LLM-backed judge and verify stages
  --build-command <cmd>       Override auto-detected build command
  --test-command <cmd>        Override auto-detected test command
  --ledger <path>             Path to SQLite ledger [default: .prod-tracker/ledger.db]
  --no-ledger                 Disable ledger persistence for this run
  --repo <name>               Repository name identifier for ledger records
  --sha <commit>              Git commit SHA to stamp on findings
  --help                      Show help message
```

### Environment Variables
- `PROD_TRACKER_LLM_PROVIDER`: Default LLM provider (`anthropic` or `openai`).
- `PROD_TRACKER_LLM_MODEL`: Model name override.
- `PROD_TRACKER_BUILD_COMMAND`: Custom build command.
- `PROD_TRACKER_TEST_COMMAND`: Custom test command.
- `ANTHROPIC_API_KEY`: API key when using Anthropic provider.
- `OPENAI_API_KEY`: API key when using OpenAI provider.

---

## Scoring & Surfacing

Findings are scored across three independent axes:
- **Severity**:
  - `S1` (Critical): Outages, data loss, security violations, corruption under prod conditions.
  - `S2` (High): Breaks under scale, deploy, failover, or partial network failure.
  - `S3` (Medium): Operational friction; harder to maintain, monitor, or debug.
  - `S4` (Low): Hygiene and best-practice drift.
- **Confidence**: `0.0` to `1.0` (Deterministic and dynamic checks are always `1.0`).
- **Archetype Gate**: Only evaluates checks applicable to the target service type (`server`, `worker`, `cron`, `cli`, `lib`).

### Routing Policy
Defined in `checks.yaml`:
- **Auto-comment (`S1` / `S2` and confidence $\ge 0.8$)**: High-severity, high-confidence issues surfaced directly to pull requests.
- **Backlog**: Lower severity or advisory findings logged to the ledger for tracking without interrupting developer workflows.

---

## Development & Testing

Run the test suite using `pytest`:
```bash
uv run pytest
```

The test suite covers:
- Schema validation for rulesets and findings
- Deterministic and dynamic detectors
- Profiler archetype classification across multiple languages (Python, TypeScript/JS, Go, Rust)
- LLM Judge & Adversarial Verify stages
- SQLite ledger upsert, deduplication, and lifecycle transitions
- False-positive regression tests

---

## License

Apache-2.0
