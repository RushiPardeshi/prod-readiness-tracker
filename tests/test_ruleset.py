from pathlib import Path

from prod_tracker.llm import LLMProvider, config_from_env
from prod_tracker.ruleset import load_ruleset


def test_checks_yaml_parses_and_has_expected_shape():
    rs = load_ruleset(Path("checks.yaml"))
    assert rs.version == 1

    checks = rs.all_checks()
    assert len(checks) > 20, "expected the full taxonomy to load"

    # the narrow v1 starter set exists
    assert any(c.priority == "v1" for c in checks)

    # the 13th dimension is present and distinct from the 12 factors
    assert any(d.id == "resilience" for d in rs.dimensions)

    # every check maps to a known pipeline stage
    assert all(c.stage() in {"deterministic", "dynamic", "judge"} for c in checks)


def test_llm_config_defaults_to_anthropic(monkeypatch):
    monkeypatch.delenv("PROD_TRACKER_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("PROD_TRACKER_LLM_MODEL", raising=False)

    config = config_from_env()

    assert config.provider is LLMProvider.anthropic
    assert config.resolved_model == "claude-sonnet-5"


def test_llm_config_can_select_openai(monkeypatch):
    monkeypatch.delenv("PROD_TRACKER_LLM_MODEL", raising=False)

    config = config_from_env(provider="openai")

    assert config.provider is LLMProvider.openai
    assert config.resolved_model == "gpt-5"
