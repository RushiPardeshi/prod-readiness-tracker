from pathlib import Path
from unittest.mock import MagicMock

import pytest

from prod_tracker.detectors import judge
from prod_tracker.llm import LLMConfig, LLMProvider
from prod_tracker.models import Archetype, Check, Detection, Priority, RepoProfile, Severity


def test_evaluated_check_ids_filters_to_supported_v1_checks():
    checks = [
        Check(
            id="stateless.in-memory-shared-state",
            title="In-memory state",
            severity=Severity.S2,
            detection=Detection.judge,
            priority=Priority.v1,
        ),
        Check(
            id="disposability.no-sigterm-handler",
            title="No sigterm handler",
            severity=Severity.S2,
            detection=Detection.abs,
            priority=Priority.v1,
        ),
        Check(
            id="unknown.custom-check",
            title="Unknown check",
            severity=Severity.S3,
            detection=Detection.judge,
            priority=Priority.v1,
        ),
        Check(
            id="stateless.in-memory-shared-state-v2",
            title="In-memory state v2",
            severity=Severity.S2,
            detection=Detection.judge,
            priority=Priority.v2,
        ),
    ]
    evaluated = judge.evaluated_check_ids(checks)
    assert evaluated == {"stateless.in-memory-shared-state", "disposability.no-sigterm-handler"}


def test_judge_drops_finding_if_file_does_not_exist(tmp_path):
    check = Check(
        id="stateless.in-memory-shared-state",
        title="In-memory state",
        severity=Severity.S2,
        detection=Detection.judge,
        priority=Priority.v1,
        judge_prompt="Find in-memory state.",
    )
    profile = RepoProfile(path=str(tmp_path), archetype="server", language="python")

    # Real file exists in repo
    real_file = tmp_path / "server.py"
    real_file.write_text("CACHE = {}\n", encoding="utf-8")

    # Mock client hallucinates nonexistent file 'missing.py'
    mock_client = MagicMock()
    mock_client.generate_structured.return_value = judge.JudgeOutput(
        findings=[
            judge.CandidateFinding(
                check_id="stateless.in-memory-shared-state",
                file="missing.py",
                anchor="CACHE",
                evidence="CACHE = {}",
                confidence=0.9,
            )
        ]
    )

    llm_config = LLMConfig(provider=LLMProvider.anthropic, enabled=True)
    findings = judge.run(tmp_path, [check], profile, llm_config, client=mock_client)

    assert findings == []


def test_judge_drops_finding_if_evidence_snippet_not_in_file(tmp_path):
    check = Check(
        id="stateless.in-memory-shared-state",
        title="In-memory state",
        severity=Severity.S2,
        detection=Detection.judge,
        priority=Priority.v1,
        judge_prompt="Find in-memory state.",
    )
    profile = RepoProfile(path=str(tmp_path), archetype="server", language="python")

    real_file = tmp_path / "server.py"
    real_file.write_text("CACHE = {}\n", encoding="utf-8")

    # Mock client hallucinates a snippet not present in server.py
    mock_client = MagicMock()
    mock_client.generate_structured.return_value = judge.JudgeOutput(
        findings=[
            judge.CandidateFinding(
                check_id="stateless.in-memory-shared-state",
                file="server.py",
                anchor="SESSION_STORE",
                evidence="SESSION_STORE = redis.connect() # not in file",
                confidence=0.9,
            )
        ]
    )

    llm_config = LLMConfig(provider=LLMProvider.anthropic, enabled=True)
    findings = judge.run(tmp_path, [check], profile, llm_config, client=mock_client)

    assert findings == []


def test_judge_accepts_finding_with_concrete_code_evidence(tmp_path):
    check = Check(
        id="stateless.in-memory-shared-state",
        title="In-memory state",
        severity=Severity.S2,
        detection=Detection.judge,
        priority=Priority.v1,
        judge_prompt="Find in-memory state.",
        rationale="Local caches break when scaled.",
    )
    profile = RepoProfile(path=str(tmp_path), archetype="server", language="python")

    real_file = tmp_path / "server.py"
    real_file.write_text("LOCAL_CACHE = {}\n", encoding="utf-8")

    mock_client = MagicMock()
    mock_client.generate_structured.return_value = judge.JudgeOutput(
        findings=[
            judge.CandidateFinding(
                check_id="stateless.in-memory-shared-state",
                file="server.py",
                anchor="LOCAL_CACHE",
                evidence="LOCAL_CACHE = {}",
                confidence=0.85,
                rationale="In-memory dict used as shared cache across requests.",
            )
        ]
    )

    llm_config = LLMConfig(provider=LLMProvider.anthropic, enabled=True)
    findings = judge.run(tmp_path, [check], profile, llm_config, client=mock_client)

    assert len(findings) == 1
    assert findings[0].check_id == "stateless.in-memory-shared-state"
    assert findings[0].file == "server.py"
    assert findings[0].anchor == "LOCAL_CACHE"
    assert findings[0].evidence == "LOCAL_CACHE = {}"
    assert findings[0].confidence == 0.85
    # Severity is preserved from check definition, not altered by model
    assert findings[0].severity == Severity.S2


def test_judge_absence_check_verifies_entrypoint_file(tmp_path):
    check = Check(
        id="disposability.no-sigterm-handler",
        title="No sigterm handler",
        severity=Severity.S2,
        detection=Detection.abs,
        priority=Priority.v1,
        judge_prompt="Find missing sigterm handler.",
        applies_to=[Archetype.server],
    )
    profile = RepoProfile(path=str(tmp_path), archetype="server", language="python", entrypoints=["app.py"])

    app_file = tmp_path / "app.py"
    app_file.write_text("from http.server import HTTPServer\nserver = HTTPServer()\nserver.serve_forever()\n")

    mock_client = MagicMock()
    mock_client.generate_structured.return_value = judge.JudgeOutput(
        findings=[
            judge.CandidateFinding(
                check_id="disposability.no-sigterm-handler",
                file="app.py",
                anchor="server.serve_forever",
                evidence="searched app.py, found no signal handler",
                confidence=0.9,
            )
        ]
    )

    llm_config = LLMConfig(provider=LLMProvider.anthropic, enabled=True)
    findings = judge.run(tmp_path, [check], profile, llm_config, client=mock_client)

    assert len(findings) == 1
    assert findings[0].check_id == "disposability.no-sigterm-handler"
    assert findings[0].file == "app.py"
