from pathlib import Path

from prod_tracker.profiler import build_profile


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_profiles_python_cli_from_pyproject_script(tmp_path):
    write(
        tmp_path / "pyproject.toml",
        """
[project]
name = "tool"
dependencies = ["typer>=0.12"]

[project.scripts]
tool = "tool.cli:app"
""".strip(),
    )

    profile = build_profile(tmp_path)

    assert profile.language == "python"
    assert profile.archetype == "cli"
    assert profile.entrypoints == ["pyproject.toml:project.scripts.tool"]


def test_profiles_javascript_server_from_package_json(tmp_path):
    write(
        tmp_path / "package.json",
        """
{
  "scripts": {"start": "node server.js"},
  "dependencies": {"express": "^5.0.0"}
}
""".strip(),
    )
    write(tmp_path / "server.js", "import express from 'express';")
    write(tmp_path / "Dockerfile", "FROM node:22")
    write(tmp_path / ".github" / "workflows" / "ci.yml", "name: ci")

    profile = build_profile(tmp_path)

    assert profile.language == "javascript"
    assert profile.archetype == "server"
    assert profile.has_dockerfile is True
    assert profile.has_ci is True
    assert "server.js" in profile.entrypoints
    assert any("server signal" in item for item in profile.evidence)


def test_profiles_worker_from_python_dependency(tmp_path):
    write(
        tmp_path / "pyproject.toml",
        """
[project]
name = "worker"
dependencies = ["celery>=5"]
""".strip(),
    )

    profile = build_profile(tmp_path)

    assert profile.language == "python"
    assert profile.archetype == "worker"


def test_profiles_cron_from_scheduled_github_workflow(tmp_path):
    write(tmp_path / "package.json", '{"name": "daily-job"}')
    write(
        tmp_path / ".github" / "workflows" / "daily.yml",
        """
on:
  schedule:
    - cron: "0 0 * * *"
""".strip(),
    )

    profile = build_profile(tmp_path)

    assert profile.language == "javascript"
    assert profile.archetype == "cron"
    assert profile.has_ci is True


def test_profiles_library_from_source_layout(tmp_path):
    write(
        tmp_path / "pyproject.toml",
        """
[project]
name = "library"
dependencies = []
""".strip(),
    )
    write(tmp_path / "src" / "library" / "__init__.py")

    profile = build_profile(tmp_path)

    assert profile.language == "python"
    assert profile.archetype == "lib"


def test_leaves_archetype_unknown_without_strong_signal(tmp_path):
    write(tmp_path / "README.md", "# notes")

    profile = build_profile(tmp_path)

    assert profile.language == "unknown"
    assert profile.archetype == "unknown"
    assert profile.entrypoints == []


def test_language_tie_is_unknown(tmp_path):
    write(tmp_path / "pyproject.toml", "[project]\nname = 'mixed'")
    write(tmp_path / "package.json", '{"name": "mixed"}')

    profile = build_profile(tmp_path)

    assert profile.language == "unknown"
    assert any("language tie" in item for item in profile.evidence)


def test_marker_constants_do_not_count_as_worker_imports(tmp_path):
    write(
        tmp_path / "pyproject.toml",
        """
[project]
name = "tool"
dependencies = ["typer>=0.12"]

[project.scripts]
tool = "tool.cli:app"
""".strip(),
    )
    write(tmp_path / "src" / "tool" / "markers.py", 'MARKERS = ("celery", "rq", "dramatiq")')

    profile = build_profile(tmp_path)

    assert profile.archetype == "cli"
