from pathlib import Path
from unittest.mock import MagicMock

import pytest

from prod_tracker.detectors import verify
from prod_tracker.llm import LLMConfig, LLMProvider
from prod_tracker.models import Check, Detection, Finding, Priority, RepoProfile, Severity


def test_verify_drops_refuted_finding(tmp_path):
    check = Check(
        id="stateless.in-memory-shared-state",
        title="In-memory state",
        severity=Severity.S2,
        detection=Detection.judge,
        priority=Priority.v1,
        verify="Try to refute: is the state backed by Redis or harmless per-request cache?",
    )
    profile = RepoProfile(path=str(tmp_path), archetype="server")

    finding = Finding(
        check_id=check.id,
        severity=check.severity,
        confidence=0.9,
        file="cache.py",
        anchor="CACHE",
        evidence="CACHE = {}",
    )

    mock_client = MagicMock()
    # Verifier successfully refutes the finding
    mock_client.generate_structured.return_value = verify.VerificationResult(
        refuted=True,
        reason="CACHE is actually a request-scoped cache cleared after each request handler finishes.",
    )

    llm_config = LLMConfig(provider=LLMProvider.anthropic, enabled=True)
    survivors = verify.run(
        tmp_path,
        [finding],
        profile,
        llm_config,
        checks=[check],
        client=mock_client,
    )

    assert survivors == []


def test_verify_retains_surviving_finding_and_updates_confidence(tmp_path):
    check = Check(
        id="resilience.data-race-shared-mutable-state",
        title="Data race on shared mutable state",
        severity=Severity.S1,
        detection=Detection.judge,
        priority=Priority.v1,
        verify="Try to refute: is access serialized?",
    )
    profile = RepoProfile(path=str(tmp_path), archetype="server")

    finding = Finding(
        check_id=check.id,
        severity=check.severity,
        confidence=0.75,
        file="counter.py",
        anchor="increment",
        evidence="counter += 1",
    )

    mock_client = MagicMock()
    # Verifier fails to refute: finding is confirmed as real
    mock_client.generate_structured.return_value = verify.VerificationResult(
        refuted=False,
        reason="Concurrent requests increment global variable without a lock or atomic operation.",
        adjusted_confidence=0.92,
    )

    llm_config = LLMConfig(provider=LLMProvider.anthropic, enabled=True)
    survivors = verify.run(
        tmp_path,
        [finding],
        profile,
        llm_config,
        checks=[check],
        client=mock_client,
    )

    assert len(survivors) == 1
    assert survivors[0].check_id == "resilience.data-race-shared-mutable-state"
    assert survivors[0].confidence == 0.92


def test_verify_defaults_to_refuted_on_exception_or_uncertainty(tmp_path):
    check = Check(
        id="stateless.in-memory-shared-state",
        title="In-memory state",
        severity=Severity.S2,
        detection=Detection.judge,
        priority=Priority.v1,
    )
    profile = RepoProfile(path=str(tmp_path), archetype="server")

    finding = Finding(
        check_id=check.id,
        severity=check.severity,
        confidence=0.8,
        file="app.py",
        anchor="STATE",
        evidence="STATE = {}",
    )

    mock_client = MagicMock()
    # Model throws or connection drops
    mock_client.generate_structured.side_effect = RuntimeError("Connection timeout")

    llm_config = LLMConfig(provider=LLMProvider.anthropic, enabled=True)
    survivors = verify.run(
        tmp_path,
        [finding],
        profile,
        llm_config,
        checks=[check],
        client=mock_client,
    )

    # Uncertainty / error defaults to refuted (dropped)
    assert survivors == []
