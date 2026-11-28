# Changelog

All notable changes to lights-out are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Dashboard redesign: dark-first theme (light still available), sticky top bar with live status, a
  "Running now" strip for cards in progress, KPI tiles with icons and delta badges, Chart/Table switches,
  status badges with SVG icons, an expandable runs table and a layout that fits phones. Still a single
  self-contained page with no network assets.

### Added
- Per-repository settings: a `repos:` entry in `lights-out.yaml` can be a mapping with `repo:` plus overrides of
  `base_branch`, `check_command`, `check_timeout_s`, `ci_timeout_s`, `human_review_paths`,
  `max_auto_merge_lines`, `max_ticket_usd` and `merge_without_ci`. Other keys are a config error. The pipeline,
  revisions, workspaces, the PR watcher, `doctor` and `bootstrap` use the card's repository settings.
  `REPO_LABELS` in the environment means plain names without overrides (`lights-out config` warns).
- Evaluation in CI: `.github/workflows/eval.yml` runs `lights-out eval` in Docker on demand (`workflow_dispatch` with
  `tasks`, `repeat`, `max_usd`) and on same-repository pull requests labelled `run-eval` that change
  `src/lights_out/agents/` or `src/lights_out/core/config.py` (never on forks or push). It uses the
  `CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`) repository secret, uploads the results as an artifact, appends
  the report to the job summary, posts or updates one PR comment, and fails on a regression against
  `eval/baseline.json`. `eval/RESULTS.md` is a placeholder for published results, committed by a human.
- Config file: an optional `lights-out.yaml` (path in `LIGHTS_OUT_CONFIG`) holds every non-secret setting, with
  `lists:` for the Trello column names and `repos:` for the label → repository mapping. An environment variable
  wins over the file, and the file over the built-in default, so `.env`-only setups work unchanged. Secrets in
  the file, unknown keys and wrong types are config errors. `lights-out config` prints the effective settings
  with their source (`env`, `file`, `default`); secrets show only as `set` / `not set`. See
  `lights-out.example.yaml`.
- Evaluation: 14 more tasks that should be implemented (6 features, 4 planted bugs, 2 refactors, 1 docs task and 1
  task whose answer is in the card comments), each with hidden acceptance tests, plus an optional `solution.patch`
  per task (a reference solution the runner never shows to agents). Tests check that every acceptance suite fails
  on the sample repository and passes with its reference solution, and that planted bugs slip past the sample's own
  `make check`.
- Revisions on request: a comment, line comment or review (comments or changes requested) on an open lights-out PR
  from a user with write access, posted after the PR's latest commit, starts a revision run. The coder gets the
  ticket, the current diff and the feedback in a fresh session; checks, review and delivery run as for a new
  card (a held PR stays held), and lights-out replies on the PR with what changed. `MAX_PR_REVISIONS` (default 3)
  caps revisions per PR; revisions count towards `MAX_DAY_USD` and the 3 attempts per card per day.
- Ten more eval tasks: 8 that should be sent back for more information (3 ambiguous, 2 too large, 2 not
  actionable, 1 self-contradictory) and 2 near-miss tasks that look vague but should proceed
  (`sort-list-by-date`, `list-total-line`). Tasks may carry an optional `solution.patch` reference solution, which
  the tests check (acceptance fails before, passes after) and which is never given to agents.
- Evaluation: `lights-out eval [--tasks ...] [--repeat N] [--out DIR] [--max-usd X]` runs the real pipeline (real
  agents, real checks and review, no Trello or GitHub) on tasks in `eval/tasks/` against the sample repository
  `eval/sample-repo/` (`ledger`), then runs hidden acceptance tests on the merged branch. It writes `results.json`
  (outcome, check and acceptance results, cost, duration, fix and review rounds) and stops at `--max-usd`
  (default 30). First tasks: `add-category-filter`, `fix-month-boundary`, `export-somewhere`.
- Alerts: set `ALERT_WEBHOOK_URL` (Slack, Discord `/slack`, Mattermost) to get a `{"text": ...}` POST when a card
  fails, a PR is left open, a card goes to Needs Info, the daily budget is used up, the service starts or
  stops, or the scheduler loop hits an unexpected error. `ALERT_EVENTS` selects events, `lights-out test-alert`
  sends a test message and `doctor` reports whether alerts are on. The URL is never logged.
- Effort per agent role: `PLANNER_EFFORT` (default `high`), `CODER_EFFORT` (default empty) and `REVIEWER_EFFORT`
  (default `high`) accept `low`, `medium`, `high`, `xhigh`, `max` or empty (model default); anything else is a
  config error. Each run stores the models and efforts in a new `settings` column (JSON), added automatically
  to existing databases and included in `data.json`.
- Health heartbeat: `GET /healthz` on the dashboard port returns JSON (`status`, `last_beat`, `age_s`,
  `current_card`, `stage`) with 200, or 503 when the last sign of life is older than `HEALTH_STALE_S`
  (default 900). The scheduler loop, stage changes, CI polls and running agent calls beat. The Docker image has
  a `HEALTHCHECK` that calls it.
- Follow-up on held PRs: between cards, a watcher (every `PR_POLL_SECONDS`, default 120) checks each card whose
  latest run is `pr_open`. PRs merged by hand move the card to Done (run `merged`), PRs closed without merging
  move it to Failed (with a `pr_open` alert). The card moves only if still in PR Opened, the `agent/*` branch is
  deleted after a human merge, a check runs at startup, and `PR_POLL_SECONDS=0` disables the watcher.
  A `/lights-out merge` PR comment (or an approving review) from a user with write, maintain
  or admin access, posted after the latest commit, merges the PR when CI is green; each command is acted on once.
- The dashboard's run detail links each run's agent transcripts (opened in a new tab). The server serves them at
  `/runs/<run_id>/<file>` (strict name check, 404 otherwise) and `data.json` has a `transcripts` list per run.

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

### Security
- `docker-compose.yml` publishes the dashboard port on `127.0.0.1` only; the README warns that it has no login.

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
