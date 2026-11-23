from __future__ import annotations

import asyncio
from pathlib import Path

from lights_out.agents import prompts
from lights_out.agents.agents import AgentResult, ClaudeAgents
from lights_out.agents.risk import find_risk_flags, format_risk_flags
from lights_out.core.config import Config
from lights_out.core.models import Ticket

STAT = " tests/x.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n\n"


def _diff(path: str, body: str, header: str = "") -> str:
    return (
        STAT
        + f"diff --git a/{path} b/{path}\n{header}index 111..222 100644\n--- a/{path}\n+++ b/{path}\n@@ -1,3 +1,3 @@\n"
        + body
    )


def test_test_file_deleted():
    d = STAT + (
        "diff --git a/tests/test_x.py b/tests/test_x.py\ndeleted file mode 100644\nindex 111..000\n"
        "--- a/tests/test_x.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def test_a():\n-    pass\n"
    )
    flags = find_risk_flags(d)
    assert len(flags) == 1 and "tests/test_x.py" in flags[0]


def test_deleted_non_test_file_is_not_flagged():
    d = STAT + (
        "diff --git a/src/a.py b/src/a.py\ndeleted file mode 100644\nindex 111..000\n"
        "--- a/src/a.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n"
    )
    assert find_risk_flags(d) == []


def test_test_definitions_removed():
    flags = find_risk_flags(_diff("tests/x.py", "-def test_a():\n-def test_b():\n-    it('x', f)\n+def test_c():\n"))
    assert len(flags) == 1 and "tests/x.py" in flags[0] and "3 removed, 1 added" in flags[0]


def test_equal_test_definitions_are_not_flagged():
    assert find_risk_flags(_diff("tests/x.py", "-def test_a():\n+def test_b():\n")) == []


def test_skip_marker_added():
    flags = find_risk_flags(_diff("tests/x.py", "+@pytest.mark.skip(reason='no')\n"))
    assert len(flags) == 1 and "tests/x.py" in flags[0] and "pytest.mark.skip" in flags[0]


def test_ci_workflow_changed():
    flags = find_risk_flags(_diff(".github/workflows/ci.yml", "-run: make check\n+run: true\n"))
    assert len(flags) == 1 and ".github/workflows/ci.yml" in flags[0]


def test_makefile_changed_but_nested_one_is_not():
    assert len(find_risk_flags(_diff("Makefile", "+x:\n"))) == 1
    assert find_risk_flags(_diff("src/lights_out/templates/bootstrap/Makefile", "+x:\n")) == []


def test_lint_suppression_added():
    flags = find_risk_flags(_diff("src/a.py", "+import os  # noqa\n"))
    assert len(flags) == 1 and "src/a.py" in flags[0] and "# noqa" in flags[0]


def test_ruff_ignore_added_in_pyproject():
    flags = find_risk_flags(_diff("pyproject.toml", '+ignore = ["E501"]\n'))
    assert len(flags) == 1 and "pyproject.toml" in flags[0]
    flags = find_risk_flags(_diff("pyproject.toml", '+    "E501",\n'))
    assert len(flags) == 1


def test_coverage_threshold_lowered():
    flags = find_risk_flags(_diff("pyproject.toml", "-fail_under = 90\n+fail_under = 80\n"))
    assert len(flags) == 1 and "pyproject.toml" in flags[0] and "90 → 80" in flags[0]


def test_coverage_threshold_removed_or_raised():
    assert len(find_risk_flags(_diff("pyproject.toml", "-fail_under = 90\n"))) == 1
    assert find_risk_flags(_diff("pyproject.toml", "-fail_under = 80\n+fail_under = 90\n")) == []


def test_removed_line_starting_with_dashes_is_not_a_header():
    assert find_risk_flags(_diff("tests/x.py", "--- a/fake\n+++ b/fake\n")) == []


def test_clean_diff_has_no_flags():
    d = _diff("src/a.py", "-x = 1\n+x = 2\n") + _diff("tests/test_a.py", "+def test_new():\n")
    assert find_risk_flags(d) == []


def test_renamed_test_file_is_not_deleted():
    d = STAT + (
        "diff --git a/tests/test_a.py b/tests/test_b.py\nsimilarity index 100%\n"
        "rename from tests/test_a.py\nrename to tests/test_b.py\n"
    )
    assert find_risk_flags(d) == []


def test_format_risk_flags():
    assert format_risk_flags([]) == "none"
    assert format_risk_flags(["a", "b"]) == "- a\n- b"


def test_review_system_says_flags_are_blocking():
    assert "Risk flags" in prompts.REVIEW_SYSTEM and "blocking" in prompts.REVIEW_SYSTEM


def _review_prompt(tmp_path, monkeypatch, flags):
    cfg = Config(trello_api_key="k", trello_token="t", trello_board_id="b", github_token="", data_dir=tmp_path)
    agents = ClaudeAgents(cfg, tmp_path / "t", 5.0)
    seen: list[str] = []

    async def fake_run(role, prompt, options):
        seen.append(prompt)
        return AgentResult("", "s", 0.0, structured={"approved": True, "issues": [], "summary": "s"})

    monkeypatch.setattr(agents, "_options", lambda **kw: None)
    monkeypatch.setattr(agents, "_run", fake_run)
    ticket = Ticket(id="1", short_id=1, title="t", description="d", url="u")
    asyncio.run(agents.review(ticket, "DIFF", Path(tmp_path), risk_flags=flags))
    return seen[0]


def test_review_prompt_lists_flags(tmp_path, monkeypatch):
    prompt = _review_prompt(tmp_path, monkeypatch, ["Test file deleted: tests/test_x.py"])
    assert "## Risk flags\n- Test file deleted: tests/test_x.py" in prompt


def test_review_prompt_says_none_without_flags(tmp_path, monkeypatch):
    assert "## Risk flags\nnone" in _review_prompt(tmp_path, monkeypatch, None)
