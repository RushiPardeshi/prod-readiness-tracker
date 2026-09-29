from pathlib import Path

import pytest
from pydantic import ValidationError

from prod_tracker.models import Check, Detection, Finding, Priority, Severity
from prod_tracker.ruleset import load_ruleset


def write_ruleset(tmp_path: Path, check_overrides: dict[str, object]) -> Path:
    check = {
        "id": "dependencies.missing-lockfile",
        "title": "Missing lockfile",
        "severity": "S2",
        "detection": "det",
        "priority": "v1",
    }
    check.update(check_overrides)
    path = tmp_path / "checks.yaml"
    path.write_text(
        "\n".join(
            [
                "version: 1",
                "dimensions:",
                "  - id: dependencies",
                "    title: Dependencies",
                "    checks:",
                f"      - id: {check['id']}",
                f"        title: {check['title']}",
                f"        severity: {check['severity']}",
                f"        detection: {check['detection']}",
                f"        priority: {check['priority']}",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_check_constrains_detection_and_priority():
    check = Check(
        id="dependencies.missing-lockfile",
        title="Missing lockfile",
        severity="S2",
        detection="det",
        priority="v1",
    )

    assert check.detection is Detection.det
    assert check.priority is Priority.v1
    assert check.stage() == "deterministic"


def test_ruleset_rejects_unknown_detection(tmp_path):
    path = write_ruleset(tmp_path, {"detection": "regex"})

    with pytest.raises(ValidationError):
        load_ruleset(path)


def test_ruleset_rejects_unknown_priority(tmp_path):
    path = write_ruleset(tmp_path, {"priority": "experimental"})

    with pytest.raises(ValidationError):
        load_ruleset(path)


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_finding_accepts_confidence_inside_bounds(confidence):
    finding = Finding(
        check_id="dependencies.missing-lockfile",
        severity=Severity.S2,
        confidence=confidence,
        file="package.json",
        evidence="package.json exists but no package-lock.json was found",
    )

    assert finding.confidence == confidence


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_finding_rejects_confidence_outside_bounds(confidence):
    with pytest.raises(ValidationError):
        Finding(
            check_id="dependencies.missing-lockfile",
            severity=Severity.S2,
            confidence=confidence,
            file="package.json",
            evidence="package.json exists but no package-lock.json was found",
        )
