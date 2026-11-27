from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
TEXT = (ROOT / ".github/workflows/eval.yml").read_text()
WF = yaml.safe_load(TEXT)
ON = WF.get("on", WF.get(True))
JOB = WF["jobs"]["eval"]
STEPS = JOB["steps"]


def test_triggers():
    assert set(ON) == {"workflow_dispatch", "pull_request"}


def test_dispatch_inputs():
    inputs = ON["workflow_dispatch"]["inputs"]
    assert not inputs["tasks"].get("required", False)
    assert str(inputs["repeat"]["default"]) == "1"
    assert str(inputs["max_usd"]["default"]) == "30"


def test_pull_request_trigger():
    pr = ON["pull_request"]
    assert pr["types"] == ["labeled", "synchronize"]
    assert "src/lights_out/agents/**" in pr["paths"]
    assert "src/lights_out/core/config.py" in pr["paths"]


def test_job_gated_on_label_and_same_repo():
    cond = JOB["if"]
    assert "'run-eval'" in cond
    assert "github.event.pull_request.head.repo.full_name == github.repository" in cond


def test_permissions():
    assert WF["permissions"] == {"contents": "read", "pull-requests": "write"}
    assert all("permissions" not in job for job in WF["jobs"].values())


def test_secrets_only_in_env():
    for step in STEPS:
        assert "secrets." not in str(step.get("run", ""))
        assert "secrets." not in str(step.get("with", ""))
        if "secrets." in str(step):
            assert "secrets." in str(step["env"])
    assert "secrets.CLAUDE_CODE_OAUTH_TOKEN" in TEXT
    assert "secrets.ANTHROPIC_API_KEY" in TEXT
    for step in STEPS:
        run = step.get("run", "")
        assert "CLAUDE_CODE_OAUTH_TOKEN=" not in run
        assert "ANTHROPIC_API_KEY=" not in run


def test_steps():
    runs = "\n".join(s.get("run", "") for s in STEPS)
    assert "docker build" in runs
    assert "lights-out" in runs and "eval" in runs
    assert "eval-report" in runs
    assert "eval/baseline.json" in runs
    assert "GITHUB_STEP_SUMMARY" in runs
    assert any(s.get("uses", "").startswith("actions/upload-artifact") for s in STEPS)
    assert "<!-- lights-out-eval -->" in TEXT


def test_placeholder_results():
    assert "No published run yet" in (ROOT / "eval/RESULTS.md").read_text()
