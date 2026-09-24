"""Typer entrypoint for the production-readiness tracker."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from . import pipeline
from .ledger import DEFAULT_LEDGER_PATH
from .llm import LLMProvider, config_from_env
from .models import RepoProfile
from .profiler import build_profile
from .ruleset import load_ruleset

app = typer.Typer(
    add_completion=False,
    help="Production-readiness tracker — 12-factor + resilience, tracked as debt.",
)

DEFAULT_CHECKS = Path("checks.yaml")


def _print_profile(profile: RepoProfile) -> None:
    typer.echo(f"target     : {profile.path}")
    typer.echo(f"language   : {profile.language}")
    typer.echo(f"archetype  : {profile.archetype}")
    typer.echo(f"docker     : {profile.has_dockerfile}")
    typer.echo(f"ci         : {profile.has_ci}")
    typer.echo("entrypoints: " + (", ".join(profile.entrypoints) if profile.entrypoints else "(none)"))
    if profile.evidence:
        typer.echo("evidence   :")
        for item in profile.evidence:
            typer.echo(f"  - {item}")


@app.command()
def profile(
    path: Path = typer.Argument(Path("."), help="Repo or subtree to profile."),
) -> None:
    """Print deterministic repository profile information."""
    _print_profile(build_profile(path))


@app.command()
def scan(
    path: Path = typer.Argument(Path("."), help="Repo or subtree to scan."),
    checks: Path = typer.Option(DEFAULT_CHECKS, "--checks", help="Path to checks.yaml."),
    llm_provider: LLMProvider | None = typer.Option(
        None,
        "--llm-provider",
        help="LLM provider for judge/verify checks. Defaults to env PROD_TRACKER_LLM_PROVIDER or anthropic.",
    ),
    llm_model: str | None = typer.Option(
        None,
        "--llm-model",
        help="Override the provider default model. Env fallback: PROD_TRACKER_LLM_MODEL.",
    ),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip LLM-backed judge/verify stages."),
    build_command: str | None = typer.Option(
        None,
        "--build-command",
        help="Override the auto-detected build command. Env fallback: PROD_TRACKER_BUILD_COMMAND.",
    ),
    test_command: str | None = typer.Option(
        None,
        "--test-command",
        help="Override the auto-detected test command. Env fallback: PROD_TRACKER_TEST_COMMAND.",
    ),
    ledger: Path = typer.Option(
        Path(DEFAULT_LEDGER_PATH),
        "--ledger",
        help="Path to the SQLite ledger database.",
    ),
    no_ledger: bool = typer.Option(False, "--no-ledger", help="Skip ledger persistence for this run."),
    repo: str | None = typer.Option(
        None, "--repo", help="Repo name to key ledger rows on. Defaults to the git remote or directory name."
    ),
    sha: str | None = typer.Option(None, "--sha", help="Commit SHA to stamp on findings. Defaults to `git rev-parse HEAD`."),
) -> None:
    """Profile the target, plan the pipeline, and report findings."""
    ruleset = load_ruleset(checks)
    profile = build_profile(path)
    llm_config = config_from_env(
        provider=llm_provider.value if llm_provider else None,
        model=llm_model,
        enabled=not no_llm,
    )
    report = pipeline.run(
        path,
        ruleset,
        profile,
        llm_config,
        build_command=build_command or os.getenv("PROD_TRACKER_BUILD_COMMAND"),
        test_command=test_command or os.getenv("PROD_TRACKER_TEST_COMMAND"),
        ledger_path=None if no_ledger else ledger,
        repo=repo,
        sha=sha,
    )

    _print_profile(report.profile)
    typer.echo(f"llm        : {llm_config.provider.value}/{llm_config.resolved_model}")
    typer.echo(
        f"checks     : {report.applicable} applicable / {report.total_checks} total "
        f"({report.skipped} gated out)"
    )
    typer.echo("plan       : " + ", ".join(f"{k}={v}" for k, v in report.plan.items()))
    typer.echo(f"findings   : {len(report.findings)}")
    for finding in report.findings:
        location = finding.file if finding.anchor is None else f"{finding.file} ({finding.anchor})"
        typer.echo(f"- {finding.severity.value} {finding.check_id}: {location}")
        typer.echo(f"  evidence: {finding.evidence}")
    if not report.findings:
        typer.echo("\n(judge/verify are stubs — det/dyn findings only. See CLAUDE.md pipeline stages.)")

    if no_ledger:
        return

    typer.echo(
        f"\nledger     : {report.ledger.new} new, {report.ledger.still_open} still open, "
        f"{report.ledger.reopened} reopened, {report.ledger.resolved} resolved"
    )
    typer.echo(f"auto-comment ({len(report.auto_comment)}):")
    for item in report.auto_comment:
        location = item.path if item.anchor is None else f"{item.path} ({item.anchor})"
        typer.echo(f"- {item.severity.value} {item.check_id}: {location}")
    typer.echo(f"backlog ({len(report.backlog)}):")
    for item in report.backlog:
        location = item.path if item.anchor is None else f"{item.path} ({item.anchor})"
        typer.echo(f"- {item.severity.value} {item.check_id}: {location}")


@app.command()
def rules(
    checks: Path = typer.Option(DEFAULT_CHECKS, "--checks", help="Path to checks.yaml."),
) -> None:
    """List the loaded checks grouped by dimension."""
    ruleset = load_ruleset(checks)
    for dim in ruleset.dimensions:
        typer.echo(f"\n[{dim.id}] {dim.title}")
        for c in dim.checks:
            v1 = " *v1" if c.priority == "v1" else ""
            typer.echo(f"  {c.severity.value}  {c.stage():13} {c.id}{v1}")


if __name__ == "__main__":
    app()
