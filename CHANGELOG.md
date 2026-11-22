# Changelog

All notable changes to lights-out are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- Dashboard: Interrupted (violet) and Declined (gray) no longer share a colour; Running moved to teal so it
  stays distinct from Interrupted. Red remains reserved for Failed. All pairs checked for colour-blind and
  normal-vision separation in light and dark mode.

### Added
- Interactive dashboard on port 8080, replacing the per-day report pages: KPI tiles with comparison to the
  previous period (runs, merge rate, spend, cost per merged card, median time to merge, first-pass rate),
  runs and spend per day, outcomes, where unmerged runs stopped, time and cost per stage, cost vs. duration,
  per-repository table, and a sortable, searchable runs table with per-run stage timelines. Filters for
  time range, repository, outcome and text; table view for every chart; light and dark themes; refreshes
  every 15 s and follows a card live. Self-contained: no external scripts.
- Stage timeline in the run log (`events` table, added automatically to existing databases), including
  each `fix` round after a failed `make check`.
- Repository routing by Trello label: `REPO_LABELS=label=owner/repo,...` lets one board drive several
  repositories. Cards with no repository label, or with two, go to Needs Info with a comment listing the
  valid labels (or to the optional `DEFAULT_REPO`). `setup-board` creates the labels, `doctor` checks
  push access to every repository, `bootstrap` takes the target label, and the report shows each card's
  repository.

### Changed
- Grouped modules into `core/`, `integrations/`, `agents/`, `orchestration/` and `reporting/`.
  `Dockerfile.test` moved to `docker/`.
- **Breaking:** the old top-level import paths (e.g. `lights_out.pipeline`,
  `from lights_out.agents import ClaudeAgents`) have been removed; import from the new subpackages
  instead (e.g. `lights_out.orchestration.pipeline`, `lights_out.agents.agents`).
- **Breaking:** `GITHUB_REPO` is replaced by `REPO_LABELS` (and the optional `DEFAULT_REPO`); lights-out
  refuses to start with the old variable and explains the change.
- Renamed the package from `ticket_agent` to `lights_out` and the command to `lights-out`.
- `make check` and CI now lint (ruff) and test lights-out itself; CI also builds the Docker image.

### Removed
- The sample app (`src/app`, `tests/test_smoke.py`) that an early run generated in this repository.

### Added
- MIT license, this changelog, and the roadmap (`ROADMAP.md`).

## [0.1.0] - 2026-11-22

### Added
- Trello adapter: picks cards from a queue column within ~15 s and moves them through
  Triaged → In Progress → Verifying → Reviewing → PR Opened → Done (or Needs Info / Failed).
- Agent pipeline on the Claude Agent SDK: triage and planning (Opus), implementation (Sonnet),
  `make check` with fix loop, independent review (Opus) with revision loop.
- GitHub delivery: branch push, pull request, CI wait, squash auto-merge when CI is green.
- Safety: orchestrator holds the tokens; the agent and checks run as an unprivileged user;
  pushes only to `agent/*` branches; per-ticket and per-day budgets; crash recovery.
- `bootstrap` command that gives an empty target repository CI, a Makefile and agent conventions.
- Daily HTML activity report served on port 8080.
