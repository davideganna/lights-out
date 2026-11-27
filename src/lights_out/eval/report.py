"""Turn eval `results.json` files into metrics, compare them with a baseline and render text or Markdown."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, median
from typing import TextIO

THRESHOLD = 0.10  # a regression is a change of strictly more than 10 points


class ReportError(Exception):
    """A results or baseline file is missing, unreadable or not valid JSON."""


@dataclass
class TaskSummary:
    id: str
    expected: str
    outcomes: str
    passed: int
    attempts: int
    cost: float
    duration_mean: float


@dataclass
class Metrics:
    attempts: int = 0
    success_rate: float | None = None
    per_category: dict[str, tuple[int, int, float | None]] = field(default_factory=dict)
    implementation_success: float | None = None
    false_needs_info_rate: float | None = None
    false_proceed_rate: float | None = None
    cost_total: float = 0.0
    cost_mean: float | None = None
    cost_median: float | None = None
    cost_mean_passed_proceed: float | None = None
    duration_mean: float | None = None
    duration_median: float | None = None
    fix_rounds_mean: float | None = None
    review_rounds_mean: float | None = None
    per_task: list[TaskSummary] = field(default_factory=list)


@dataclass
class Regression:
    metric: str
    label: str
    baseline: float
    current: float

    def __str__(self) -> str:
        delta = _delta_pts(self.current - self.baseline)
        return f"{self.label} {_pct(self.baseline)} -> {_pct(self.current)} ({delta})"


@dataclass
class Results:
    tasks: list[dict]
    settings: list[dict]


def resolve_results(path: str | Path) -> Path:
    p = Path(path)
    if p.is_dir():
        p = p / "results.json"
    if not p.is_file():
        raise ReportError(f"results file not found: {p}")
    return p


def load_results(paths: Sequence[str | Path]) -> Results:
    tasks: list[dict] = []
    settings: list[dict] = []
    for path in paths:
        p = resolve_results(path)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            file_tasks = list(data.get("tasks", []))
            s = data.get("settings") or {}
        except (OSError, ValueError, AttributeError, TypeError) as e:
            raise ReportError(f"cannot read {p}: {e}") from e
        tasks.extend(file_tasks)
        if s not in settings:
            settings.append(s)
    return Results(tasks, settings)


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def _mean(xs: Sequence[float]) -> float | None:
    return float(mean(xs)) if xs else None


def _median(xs: Sequence[float]) -> float | None:
    return float(median(xs)) if xs else None


def compute_metrics(tasks: Sequence[dict]) -> Metrics:
    m = Metrics(attempts=len(tasks))
    proceed = [t for t in tasks if t.get("expected") == "proceed"]
    needs_info = [t for t in tasks if t.get("expected") == "needs_info"]
    m.success_rate = _rate(sum(1 for t in tasks if t.get("passed")), len(tasks))

    cats: dict[str, list[int]] = {}
    for t in tasks:
        c = cats.setdefault(t.get("category") or "unknown", [0, 0])
        c[0] += 1 if t.get("passed") else 0
        c[1] += 1
    m.per_category = {k: (p, n, _rate(p, n)) for k, (p, n) in cats.items()}

    m.implementation_success = _rate(
        sum(1 for t in proceed if t.get("outcome") == "merged" and t.get("acceptance_ok") is True), len(proceed)
    )
    m.false_needs_info_rate = _rate(sum(1 for t in proceed if t.get("outcome") == "needs_info"), len(proceed))
    m.false_proceed_rate = _rate(sum(1 for t in needs_info if t.get("outcome") != "needs_info"), len(needs_info))

    costs = [float(t.get("cost_usd") or 0) for t in tasks]
    m.cost_total = sum(costs)
    m.cost_mean, m.cost_median = _mean(costs), _median(costs)
    m.cost_mean_passed_proceed = _mean([float(t.get("cost_usd") or 0) for t in proceed if t.get("passed")])
    durations = [float(t.get("duration_s") or 0) for t in tasks]
    m.duration_mean, m.duration_median = _mean(durations), _median(durations)
    m.fix_rounds_mean = _mean([float(t.get("fix_rounds") or 0) for t in proceed])
    m.review_rounds_mean = _mean([float(t.get("review_rounds") or 0) for t in proceed])

    groups: dict[str, list[dict]] = {}
    for t in tasks:
        groups.setdefault(str(t.get("id", "?")), []).append(t)
    for tid, ts in groups.items():
        m.per_task.append(
            TaskSummary(
                id=tid,
                expected=str(ts[0].get("expected", "")),
                outcomes=", ".join(str(t.get("outcome", "")) for t in ts),
                passed=sum(1 for t in ts if t.get("passed")),
                attempts=len(ts),
                cost=sum(float(t.get("cost_usd") or 0) for t in ts),
                duration_mean=float(mean(float(t.get("duration_s") or 0) for t in ts)),
            )
        )
    return m


_REGRESSION_CHECKS = (
    ("success_rate", "Success rate", -1),  # dropping is bad
    ("false_proceed_rate", "False proceed rate", 1),  # rising is bad
)


def compare(current: Metrics, baseline: Metrics) -> list[Regression]:
    found: list[Regression] = []
    for attr, label, sign in _REGRESSION_CHECKS:
        cur, base = getattr(current, attr), getattr(baseline, attr)
        if cur is None or base is None:
            continue
        # round to avoid float noise making exactly 10 points look like more
        if round(sign * (cur - base), 9) > THRESHOLD:
            found.append(Regression(attr, label, base, cur))
    return found


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _delta_pts(d: float) -> str:
    return f"{d * 100:+.1f} pts"


def _usd(x: float | None) -> str:
    return "n/a" if x is None else f"${x:.2f}"


def _secs(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0f}s"


def _num(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


# (attribute, label, formatter, kind) where kind is "rate", "usd", "secs" or "num"
_ROWS = (
    ("success_rate", "Success rate", _pct, "rate"),
    ("implementation_success", "Implementation success", _pct, "rate"),
    ("false_needs_info_rate", "False needs_info rate", _pct, "rate"),
    ("false_proceed_rate", "False proceed rate", _pct, "rate"),
    ("cost_total", "Cost total", _usd, "usd"),
    ("cost_mean", "Cost mean per task", _usd, "usd"),
    ("cost_median", "Cost median per task", _usd, "usd"),
    ("cost_mean_passed_proceed", "Cost mean per passed proceed task", _usd, "usd"),
    ("duration_mean", "Time mean per task", _secs, "secs"),
    ("duration_median", "Time median per task", _secs, "secs"),
    ("fix_rounds_mean", "Fix rounds mean per proceed task", _num, "num"),
    ("review_rounds_mean", "Review rounds mean per proceed task", _num, "num"),
)


def _delta(kind: str, cur: float | None, base: float | None) -> str:
    if cur is None or base is None:
        return "n/a"
    d = cur - base
    if kind == "rate":
        return _delta_pts(d)
    if kind == "usd":
        return f"{'-' if d < 0 else '+'}${abs(d):.2f}"
    if kind == "secs":
        return f"{d:+.0f}s"
    return f"{d:+.2f}"


def format_summary(metrics: Metrics, baseline: Metrics | None = None, regressions: Sequence[Regression] = ()) -> str:
    lines = [f"Eval summary ({metrics.attempts} attempts)"]
    for attr, label, fmt, kind in _ROWS:
        cur = getattr(metrics, attr)
        line = f"  {label}: {fmt(cur)}"
        if baseline is not None:
            line += f" (baseline {fmt(getattr(baseline, attr))}, {_delta(kind, cur, getattr(baseline, attr))})"
        lines.append(line)
    lines.extend(f"REGRESSION: {r}" for r in regressions)
    return "\n".join(lines)


def _settings_text(s: dict) -> str:
    if not s:
        return "unknown"

    def part(role: str) -> str:
        model, effort = s.get(f"{role}_model"), s.get(f"{role}_effort")
        return f"{role} {model or 'unknown'}" + (f" ({effort})" if effort else "")

    commit = s.get("lights_out_commit")
    return f"{part('planner')}, {part('coder')}, {part('reviewer')}, commit {str(commit)[:7] if commit else 'unknown'}"


def format_markdown(
    results: Results,
    metrics: Metrics,
    baseline_metrics: Metrics | None = None,
    regressions: Sequence[Regression] = (),
) -> str:
    regressed = {r.metric for r in regressions}
    settings = "; ".join(_settings_text(s) for s in results.settings) or "unknown"
    out = ["# Eval report", "", f"Settings: {settings}", ""]
    if baseline_metrics is None:
        out += ["| Metric | Value |", "| --- | --- |"]
    else:
        out += ["| Metric | Value | Baseline | Δ |", "| --- | --- | --- | --- |"]
    for attr, label, fmt, kind in _ROWS:
        cur = getattr(metrics, attr)
        if baseline_metrics is None:
            out.append(f"| {label} | {fmt(cur)} |")
        else:
            base = getattr(baseline_metrics, attr)
            mark = " ⚠ regression" if attr in regressed else ""
            out.append(f"| {label} | {fmt(cur)} | {fmt(base)} | {_delta(kind, cur, base)}{mark} |")
    out += ["", "| Category | Passed | Total | Rate |", "| --- | --- | --- | --- |"]
    for cat, (p, n, rate) in metrics.per_category.items():
        out.append(f"| {cat} | {p} | {n} | {_pct(rate)} |")
    out += ["", "| Task | Expected | Outcome | Pass | Cost | Time |", "| --- | --- | --- | --- | --- | --- |"]
    for t in metrics.per_task:
        out.append(
            f"| {t.id} | {t.expected} | {t.outcomes} | {t.passed}/{t.attempts} | {_usd(t.cost)} "
            f"| {_secs(t.duration_mean)} |"
        )
    return "\n".join(out) + "\n"


def write_report(
    results_paths: Sequence[str | Path],
    baseline_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    results = load_results(results_paths)
    metrics = compute_metrics(results.tasks)
    base_metrics: Metrics | None = None
    regressions: list[Regression] = []
    if baseline_path:
        base_metrics = compute_metrics(load_results([baseline_path]).tasks)
        regressions = compare(metrics, base_metrics)
    print(format_summary(metrics, base_metrics, regressions), file=out)
    if markdown_path:
        Path(markdown_path).write_text(format_markdown(results, metrics, base_metrics, regressions), encoding="utf-8")
    return 1 if regressions else 0
