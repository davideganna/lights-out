import importlib

import pytest

from lights_out import _MOVED


@pytest.mark.parametrize(("old", "new"), sorted(_MOVED.items()))
def test_old_import_paths_alias_new_modules(old, new):
    assert importlib.import_module(f"lights_out.{old}") is importlib.import_module(new)


def test_old_from_import_is_same_object():
    from lights_out.pipeline import Pipeline

    from lights_out.orchestration.pipeline import Pipeline as New

    assert Pipeline is New


def test_from_package_import_old_module():
    from lights_out import prompts
    from lights_out.agents import prompts as new

    assert prompts is new


def test_agents_package_reexports_public_names():
    from lights_out.agents import AgentResult, BudgetExceeded, ClaudeAgents
    from lights_out.agents.agents import ClaudeAgents as Real

    assert ClaudeAgents is Real
    assert AgentResult and BudgetExceeded


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
