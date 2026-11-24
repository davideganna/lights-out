# Changelog

All notable changes to lights-out are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Before merging, the base branch is checked for new commits. If it moved, the branch is rebased onto it; the
  coder resolves simple conflicts (no conflict markers may remain, otherwise the card fails and the rebase is
  aborted), then `make check`, one review (only when conflicts were resolved), the merge policy and CI run
  again. At most 2 rebases per card. A 405/409 answer from GitHub's merge call is treated as a moved base
  once (rebase and retry).
- When CI fails on the PR, the failure logs (GitHub Actions job logs, or the check summary when the token
  lacks *Actions: read*) are sent back to the coder, which fixes and pushes again; the fix must pass
  `make check` and one review. Controlled by `MAX_CI_FIX_ATTEMPTS` (default 2, `0` keeps the old behaviour).
- `doctor` verifies branch protection (classic rules and rulesets) on `BASE_BRANCH` for every target
  repository: missing protection, no required checks or allowed force pushes are problems; admins not included,
  rulesets' bypass actors and an unreadable token (403) are warnings. The README has a new Branch protection section.
- Merge policy: a PR whose diff matches `HUMAN_REVIEW_PATHS` (CI, dependencies, auth, migrations by default)
  or exceeds `MAX_AUTO_MERGE_LINES` changed lines (default 400; `0` disables) is no longer merged
  automatically. The PR body gets a "Needs human approval" section, the PR and card get a comment with the
  reasons, and the run ends as `pr_open`. An empty `HUMAN_REVIEW_PATHS` disables path holds.

### Fixed
- Dashboard: Interrupted (violet) and Declined (gray) no longer share a colour; Running moved to teal so it
  stays distinct from Interrupted. Red remains reserved for Failed. All pairs checked for colour-blind and
  normal-vision separation in light and dark mode.

### Added
- Deterministic risk flags: the diff is scanned for deleted tests, removed test definitions, added skip/xfail
  markers, CI or Makefile changes, added lint suppressions and lowered coverage thresholds. The reviewer gets
  them in a "Risk flags" section and must treat each as blocking unless the ticket asks for that change; the
  flags of the approved diff are listed under "Risk flags" in the PR body.
- `ALLOWED_MEMBERS` setting: only cards queued (moved or created into the ready list) by listed Trello
  usernames are processed; others go to Needs Info with no agent call. Comments from other members are
  hidden from the agents. `run` warns and `doctor` reports when the list is empty.
- `doctor` checks Claude authentication: it fails if neither `CLAUDE_CODE_OAUTH_TOKEN` nor `ANTHROPIC_API_KEY`
  is set, otherwise makes a one-turn request with `PLANNER_MODEL`.
- Graceful shutdown: on SIGTERM or SIGINT (`docker compose stop`, Ctrl-C) lights-out stops picking up cards,
  puts the card in progress back in Queued with a comment and records the run as interrupted. Stages that
  block (workspace setup, checks, and delivery: push, PR, CI wait, merge) can't be interrupted: the card
  finishes them and no further card is picked up. If that outlasts `stop_grace_period` (raised to 30 s; a CI
  wait can take up to `CI_TIMEOUT_S`), Docker sends SIGKILL and crash recovery handles the card at the next start.
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
