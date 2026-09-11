# Implementation Plan

This plan is ordered by dependency first and risk second. The goal is to get the
production-readiness tracker from scaffold to a working v1 without starting with
the highest-noise LLM features.

Use `claude-sonnet-5` for all implementation items. Use effort as listed per item.

## Model And Effort Matrix

| Order | Status | Work Item | Model | Effort | Risk | Depends On |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Done | Unblock local execution | `claude-sonnet-5` | `low` | Low | None |
| 2 | Done | Tighten schema validation | `claude-sonnet-5` | `low` | Low | 1 |
| 3 | Done | Implement profiler v0.1 | `claude-sonnet-5` | `medium` | Medium | 1, 2 |
| 4 | Done | Implement deterministic detector v1 | `claude-sonnet-5` | `medium` | Medium | 1, 2, 3 |
| 5 | Next | Implement dynamic detector v1 | `claude-sonnet-5` | `medium` | Medium | 1, 2, 3 |
| 6 | Pending | Implement ledger, scoring, and surfacing | `claude-sonnet-5` | `high` | Medium-High | 1-5 |
| 7 | Pending | Implement judge and verify | `claude-sonnet-5` | `xhigh` | High | 1-6 |
| 8 | Pending | Expand coverage and regression tests | `claude-sonnet-5` | `medium` | Medium | 1-7 |

## Current Status

Completed:

- Item 1: local `pytest` is unblocked from the repository root via pytest
  `pythonpath = ["src"]`, and the package metadata declares the CLI entrypoint.
- Item 2: ruleset and finding schema validation now rejects unknown detection
  classes, unknown priorities, and out-of-range confidence values.
- Item 3: profiler v0.1 now detects language, CI presence, container context,
  entrypoints, archetype, and explanatory evidence.
- Item 4: deterministic detector v1 now emits concrete findings for the v1
  deterministic checks, including lockfiles, floating versions, hardcoded
  secrets, CI, runtime pins, print-style logging, test-suite presence, and
  narrow timeout checks.

Next:

- Item 5: dynamic detector v1.

Repository self-profile note:

- The profiler currently reports this repository as a Python CLI project with
  CI config and no Dockerfile.
- CI has been added because this project is itself a scanner and should run its
  tests on every change.
- Adding Docker is not necessary yet for local development, but it becomes
  necessary before the planned Docker-based GitHub Action distribution path.

## 1. Unblock Local Execution

Model: `claude-sonnet-5`
Effort: `low`

Fix the package/test setup so the repo works from a fresh checkout.

Current issue:

- `pytest` fails with `ModuleNotFoundError: prod_tracker`.
- `python -m prod_tracker.cli rules` fails for the same reason when the package
  has not been installed or `src/` has not been added to the import path.

Expected outcome:

- `pytest` runs successfully from the repository root.
- The CLI can be exercised locally through the intended `uv run prod-tracker`
  flow.
- Add a smoke test for the `rules` command or the equivalent CLI entrypoint.

Likely files:

- `pyproject.toml`
- `tests/`

## 2. Tighten Schema Validation

Model: `claude-sonnet-5`
Effort: `low`

Make invalid rulesets and malformed findings fail early.

Expected outcome:

- `Check.detection` is constrained to known detection classes.
- `Check.priority` is constrained to expected values such as `v1` and `v2`.
- `Finding.confidence` is constrained to `0.0 <= confidence <= 1.0`.
- Tests cover valid and invalid ruleset/finding cases.

Why this comes early:

- Every later stage depends on the shape of `Check`, `Ruleset`,
  `RepoProfile`, and `Finding`.
- Bad data should fail at the boundary instead of leaking into detectors or the
  ledger.

Likely files:

- `src/prod_tracker/models.py`
- `tests/test_ruleset.py`
- New schema-focused tests under `tests/`

## 3. Implement Profiler v0.1

Model: `claude-sonnet-5`
Effort: `medium`

Replace the placeholder repo profiler with deterministic classification.

Expected outcome:

- Detect language from manifests and common lockfiles.
- Detect CI config presence.
- Detect Docker/container context.
- Detect likely entrypoints.
- Classify archetype as one of `server`, `worker`, `cron`, `cli`, or `lib`.
- Keep classification conservative when evidence is weak.

Why this matters:

- Archetype gates determine which checks run.
- Absence checks such as missing `SIGTERM` handling are only useful when the
  tool knows it is analyzing a long-running process.

Likely files:

- `src/prod_tracker/profiler.py`
- `src/prod_tracker/models.py`
- New profiler tests under `tests/`

## 4. Implement Deterministic Detector v1

Model: `claude-sonnet-5`
Effort: `medium`

Implement the lowest-noise `priority: v1` deterministic checks first.

Start with:

- `dependencies.missing-lockfile`
- `dependencies.floating-versions`
- `config.secrets-in-code`
- `build_release_run.no-ci`
- `dev_prod_parity.missing-runtime-pin`
- `logs.unstructured-logging`
- `admin_processes.no-test-suite`
- `resilience.missing-io-timeouts`, only where deterministic matching is precise

Expected outcome:

- Deterministic checks emit `Finding` objects with confidence `1.0`.
- Each finding cites a file and evidence.
- Tests use fixture repositories for positive and negative cases.

Risk controls:

- Prefer file-presence and exact config checks before broad regex scans.
- Keep noisy checks narrow until there are enough fixtures to prove precision.

Likely files:

- `src/prod_tracker/detectors/deterministic.py`
- `src/prod_tracker/models.py`
- New detector fixture tests under `tests/`

## 5. Implement Dynamic Detector v1

Model: `claude-sonnet-5`
Effort: `medium`

Run build and test commands and convert failures into ground-truth findings.

Expected outcome:

- Detect or accept a build command.
- Detect or accept a test command.
- Run commands with timeouts.
- Capture command, exit code, stdout/stderr summary, and failure evidence.
- Emit confidence `1.0` findings for failed build or failed tests.

Risk controls:

- Do not run arbitrary commands from untrusted config without a controlled allow
  list or explicit user/CI configuration.
- Add timeouts to avoid hung scans.
- Keep command discovery conservative.

Likely files:

- `src/prod_tracker/detectors/dynamic.py`
- `src/prod_tracker/profiler.py`
- `src/prod_tracker/cli.py`
- New dynamic detector tests under `tests/`

## 6. Implement Ledger, Scoring, And Surfacing

Model: `claude-sonnet-5`
Effort: `high`

Make findings persistent and route them according to severity and confidence.

Expected outcome:

- Initialize the SQLite ledger.
- Compute stable fingerprints from `check_id`, anchor, and normalized evidence
  or snippet.
- Upsert findings without creating duplicates on every run.
- Track `open`, `resolved`, `acknowledged`, `wontfix`, and `accepted` states.
- Stamp each finding with `check_version`.
- Implement routing from `checks.yaml`:
  `severity in [S1, S2] AND confidence >= 0.8` goes to auto-comment output;
  everything else goes to backlog output.
- Return enough data in `RunReport` for CLI and future GitHub integration.

Why this is higher effort:

- The core product is the tracker, not a one-shot scanner.
- Stable identity is the hard part. Line-number-based identity should not be
  used.

Likely files:

- `src/prod_tracker/ledger.py`
- `src/prod_tracker/pipeline.py`
- `src/prod_tracker/models.py`
- `src/prod_tracker/cli.py`
- Ledger tests under `tests/`

## 7. Implement Judge And Verify

Model: `claude-sonnet-5`
Effort: `xhigh`

Add the LLM-backed analysis only after the deterministic pipeline and ledger are
working.

Start with the narrow high-value v1 semantic checks:

- `stateless.in-memory-shared-state`
- `disposability.no-sigterm-handler`
- `resilience.data-race-shared-mutable-state`
- Semantic fallback for `resilience.missing-io-timeouts` when deterministic
  evidence is insufficient

Expected outcome:

- Judge reads repo profile, diff/context, and the selected check prompt.
- Judge emits structured `Finding` objects.
- Findings without concrete code evidence are dropped.
- Verify pass tries to refute each judge finding.
- Verify defaults to refuted when uncertain.
- Surviving judge findings carry confidence from the model output.

Risk controls:

- Keep the enabled judge set narrow.
- Require evidence locations.
- Run verify on every judge finding.
- Put low-confidence results into the backlog, not auto comments.

Likely files:

- `src/prod_tracker/detectors/judge.py`
- `src/prod_tracker/detectors/verify.py`
- `src/prod_tracker/models.py`
- `src/prod_tracker/pipeline.py`
- Judge/verify tests with mocked model responses under `tests/`

## 8. Expand Coverage And Regression Tests

Model: `claude-sonnet-5`
Effort: `medium`

Only expand after the end-to-end v1 path works.

Expected outcome:

- Add fixture repositories for common Python, JavaScript, Go, and Rust cases.
- Add regression tests for false positives discovered during real use.
- Add pipeline integration tests covering stage grouping, archetype gating,
  detector output, scoring, ledger upsert, and CLI rendering.
- Expand beyond the `priority: v1` starter checks only when precision is proven.

Risk controls:

- Treat every noisy check as disabled or backlog-only until fixtures prove it.
- Prefer precision over recall.

Likely files:

- `checks.yaml`
- `tests/`
- `src/prod_tracker/detectors/`
- `src/prod_tracker/pipeline.py`

## Practical Execution Order

1. Packaging and test harness.
2. Schema validation.
3. Profiler.
4. Deterministic checks.
5. Dynamic checks.
6. Ledger plus scoring and surfacing.
7. Judge plus verify.
8. Broader coverage and regression tests.

Do not start with judge/verify. That is the highest false-positive risk and
needs the profiler, schema, evidence handling, routing, and ledger to be stable
first.
