from __future__ import annotations

import io
import json

import pytest

from lights_out.cli import main
from lights_out.eval.report import (
    ReportError,
    compare,
    compute_metrics,
    format_markdown,
    format_summary,
    load_results,
    resolve_results,
    write_report,
)

SETTINGS = {
    "planner_model": "claude-opus-5-5",
    "planner_effort": "high",
    "coder_model": "claude-sonnet-5-5",
    "coder_effort": "",
    "reviewer_model": "claude-opus-5-5",
    "reviewer_effort": "high",
    "lights_out_commit": "abc1234def",
}


def t(id, expected, outcome, passed, cost=1.0, dur=10.0, cat="feature", fix=0, review=0, acc=None):
    return {
        "id": id,
        "category": cat,
        "expected": expected,
        "repeat": 1,
        "outcome": outcome,
        "acceptance_ok": acc,
        "cost_usd": cost,
        "duration_s": dur,
        "fix_rounds": fix,
        "review_rounds": review,
        "passed": passed,
    }


def mixed():
    return [
        t("a", "proceed", "merged", True, cost=1, dur=10, fix=1, review=1, acc=True),
        t("b", "proceed", "needs_info", False, cost=2, dur=20, cat="bug"),
        t("c", "proceed", "merged", False, cost=3, dur=30, fix=2, review=2, acc=False),
        t("d", "needs_info", "needs_info", True, cost=4, dur=40, cat="bug"),
        t("e", "needs_info", "merged", False, cost=5, dur=50),
    ]


def write(path, tasks, settings=SETTINGS):
    data = {"tasks": tasks}
    if settings is not None:
        data["settings"] = settings
    path.write_text(json.dumps(data))
    return path


def test_every_metric_on_mixed_set():
    m = compute_metrics(mixed())
    assert m.success_rate == pytest.approx(2 / 5)
    assert m.per_category == {"feature": (1, 3, pytest.approx(1 / 3)), "bug": (1, 2, 0.5)}
    assert m.implementation_success == pytest.approx(1 / 3)
    assert m.false_needs_info_rate == pytest.approx(1 / 3)
    assert m.false_proceed_rate == 0.5
    assert m.cost_total == 15 and m.cost_mean == 3 and m.cost_median == 3
    assert m.cost_mean_passed_proceed == 1
    assert m.duration_mean == 30 and m.duration_median == 30
    assert m.fix_rounds_mean == 1 and m.review_rounds_mean == 1
    assert [x.id for x in m.per_task] == ["a", "b", "c", "d", "e"]


def test_no_proceed_tasks():
    m = compute_metrics([t("d", "needs_info", "needs_info", True)])
    assert m.implementation_success is None and m.false_needs_info_rate is None
    assert m.fix_rounds_mean is None and m.cost_mean_passed_proceed is None
    assert m.false_proceed_rate == 0


def test_no_needs_info_tasks():
    m = compute_metrics([t("a", "proceed", "merged", True, acc=True)])
    assert m.false_proceed_rate is None and m.success_rate == 1


def test_no_passed_tasks():
    m = compute_metrics([t("a", "proceed", "failed", False)])
    assert m.success_rate == 0 and m.cost_mean_passed_proceed is None


def test_empty_tasks(tmp_path):
    m = compute_metrics([])
    assert m.success_rate is None and m.cost_mean is None and m.duration_median is None and m.cost_total == 0
    p = write(tmp_path / "r.json", [])
    out = io.StringIO()
    md = tmp_path / "r.md"
    assert write_report([p], markdown_path=md, out=out) == 0
    assert "n/a" in md.read_text() and "n/a" in out.getvalue()


def test_repeats():
    tasks = [
        t("a", "proceed", "merged", True, acc=True),
        t("a", "proceed", "merged", True, acc=True),
        t("a", "proceed", "failed", False),
    ]
    m = compute_metrics(tasks)
    assert m.success_rate == pytest.approx(2 / 3)
    assert (m.per_task[0].passed, m.per_task[0].attempts) == (2, 3)
    assert m.per_task[0].outcomes == "merged, merged, failed"
    md = format_markdown(load_results([]), m)
    assert "| a | proceed | merged, merged, failed | 2/3 |" in md


def test_files_are_concatenated_and_folders_resolve(tmp_path):
    d = tmp_path / "run1"
    d.mkdir()
    write(d / "results.json", [t("a", "proceed", "merged", True)])
    f = write(tmp_path / "two.json", [t("b", "proceed", "failed", False)], settings={"coder_model": "x"})
    assert resolve_results(d) == d / "results.json"
    res = load_results([d, f])
    assert [x["id"] for x in res.tasks] == ["a", "b"]
    assert len(res.settings) == 2
    with pytest.raises(ReportError):
        resolve_results(tmp_path / "nope")


def with_rates(success_passed, total, fp_bad=0, fp_total=0):
    tasks = [t(f"p{i}", "proceed", "merged", i < success_passed) for i in range(total)]
    tasks += [t(f"n{i}", "needs_info", "merged" if i < fp_bad else "needs_info", i >= fp_bad) for i in range(fp_total)]
    return tasks


def test_regression_thresholds():
    base = compute_metrics(with_rates(10, 10))
    assert compare(compute_metrics(with_rates(9, 10)), base) == []  # exactly 10 points
    cur = compute_metrics(with_rates(89, 100))
    base = compute_metrics(with_rates(100, 100))
    assert [r.metric for r in compare(cur, base)] == ["success_rate"]
    assert compare(compute_metrics([]), base) == []
    base = compute_metrics([t("n", "needs_info", "needs_info", True)] * 10)
    ok_tasks = [t("n", "needs_info", "merged", False)] + [t("n", "needs_info", "needs_info", True)] * 9
    ok = compute_metrics(ok_tasks)
    assert compare(ok, base) == []  # +10 points exactly
    bad = compute_metrics([t("n", "needs_info", "merged", False)] * 2 + [t("n", "needs_info", "needs_info", True)] * 8)
    assert "false_proceed_rate" in [r.metric for r in compare(bad, base)]


def test_cli_exit_codes(tmp_path, capsys):
    base = write(tmp_path / "base.json", with_rates(10, 10))
    worse = write(tmp_path / "worse.json", with_rates(5, 10))
    same = write(tmp_path / "same.json", with_rates(10, 10))
    assert main(["eval-report", str(worse), "--baseline", str(base)]) == 1
    assert "REGRESSION:" in capsys.readouterr().out
    assert main(["eval-report", str(same), "--baseline", str(base)]) == 0
    assert main(["eval-report", str(tmp_path / "missing.json")]) == 2
    assert main(["eval-report", str(same), "--baseline", str(tmp_path / "missing.json")]) == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert main(["eval-report", str(bad)]) == 2
    assert "Report error" in capsys.readouterr().err


def test_markdown_report(tmp_path):
    base = write(tmp_path / "base.json", with_rates(10, 10))
    cur = write(tmp_path / "cur.json", mixed())
    md = tmp_path / "out.md"
    assert main(["eval-report", str(cur), "--baseline", str(base), "--markdown", str(md)]) == 1
    text = md.read_text()
    assert "# Eval report" in text
    assert "Settings: planner claude-opus-5-5 (high), coder claude-sonnet-5-5, reviewer claude-opus-5-5 (high)" in text
    assert "commit abc1234" in text
    assert "| Metric | Value | Baseline | Δ |" in text
    assert "| Category | Passed | Total | Rate |" in text
    assert "| Task | Expected | Outcome | Pass | Cost | Time |" in text
    assert "| e | needs_info | merged | 0/1 | $5.00 | 50s |" in text
    assert "| Success rate | 40.0% | 100.0% | -60.0 pts ⚠ regression |" in text


def test_markdown_without_baseline_and_unknown_settings(tmp_path):
    cur = write(tmp_path / "cur.json", mixed(), settings=None)
    md = tmp_path / "out.md"
    assert main(["eval-report", str(cur), "--markdown", str(md)]) == 0
    text = md.read_text()
    assert "Settings: unknown" in text
    assert "| Metric | Value |" in text and "regression" not in text


def test_format_summary_lines():
    m = compute_metrics(mixed())
    assert "Success rate: 40.0%" in format_summary(m)
    assert "(baseline 40.0%, +0.0 pts)" in format_summary(m, m)


def test_eval_report_help_needs_no_environment(monkeypatch, capsys):
    for name in ("TRELLO_API_KEY", "TRELLO_TOKEN", "TRELLO_BOARD_ID", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["eval-report", "--help"])
    assert exc.value.code == 0
    assert "--baseline" in capsys.readouterr().out


def test_cmd_eval_writes_report(tmp_path, monkeypatch, capsys):
    from lights_out.eval import runner

    (tmp_path / "eval" / "sample-repo").mkdir(parents=True)
    (tmp_path / "eval" / "tasks").mkdir()
    monkeypatch.chdir(tmp_path)
    out_dir = tmp_path / "res"

    class FakeTask:
        id = "a"

    monkeypatch.setattr("lights_out.eval.tasks.load_tasks", lambda d, ids: [FakeTask()])

    async def fake_run_eval(tasks, cfg, sample_repo, **kw):
        write(cfg.data_dir / "results.json", mixed())

    monkeypatch.setattr(runner, "run_eval", fake_run_eval)
    assert main(["eval", "--out", str(out_dir)]) == 0
    assert (out_dir / "report.md").is_file()
    assert "Success rate: 40.0%" in capsys.readouterr().out
