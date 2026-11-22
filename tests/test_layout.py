import importlib

import pytest


@pytest.mark.parametrize(
    "name",
    [
        "config",
        "models",
        "routing",
        "trello",
        "github",
        "pipeline",
        "scheduler",
        "workspace",
        "store",
        "report",
        "prompts",
    ],
)
def test_old_top_level_modules_are_gone(name):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"lights_out.{name}")


def test_agents_package_does_not_reexport_names():
    with pytest.raises(ImportError):
        exec("from lights_out.agents import ClaudeAgents")


def test_new_paths_import():
    from lights_out.agents import prompts
    from lights_out.agents.agents import ClaudeAgents
    from lights_out.orchestration.pipeline import Pipeline

    assert Pipeline and ClaudeAgents and prompts


def test_bootstrap_templates_found():
    from lights_out.orchestration.workspace import TEMPLATES

    assert TEMPLATES.is_dir()
    for name in ("Makefile", "CLAUDE.md", "README.md", ".github/workflows/ci.yml"):
        assert (TEMPLATES / name).is_file(), name


def test_cli_help_exits_zero():
    from lights_out.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
