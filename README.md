# lights-out

**Develop your repository from Trello tickets.** lights-out is a self-hosted orchestrator you point at
your own GitHub repository and Trello board (Jira is on the [roadmap](ROADMAP.md)). Write cards in
*To Do*; **the moment you move one to *Queued***, lights-out picks it up (it checks the column every
15 seconds) and Claude agents carry it through to a merged pull request. Cards are handled one at a
time, top first. For each card:

1. **Triage** (Opus, read-only): is this clear, PR-sized work? Any kind of ticket is fine:
   features, bug fixes, refactors, setup, tests, docs. If it's unclear or too big, the agent
   comments its questions on the card and moves it to **Needs Info**.
2. **Implement** (Sonnet): writes the code and tests in a fresh clone on branch `agent/<id>-<slug>`.
3. **Check**: the orchestrator runs `make check` itself. If it fails, the output goes back to the
   coder (up to `MAX_FIX_ATTEMPTS` times).
4. **Review** (Opus, read-only, separate context): approves, or sends issues back to the coder
   (up to `MAX_REVIEW_ROUNDS` times).
5. **Deliver**: pushes the branch, opens a PR, waits for GitHub Actions, **squash-merges when CI
   is green**, comments the PR link on the card and moves it to **Done**. If CI fails, the logs go back
   to the coder, which fixes and pushes again, up to `MAX_CI_FIX_ATTEMPTS` times (default 2, `0` disables).

Whatever happens, the card gets a comment. A live **dashboard** at <http://localhost:8080/> shows what
the agent is doing and how well it's doing it: runs per day by outcome, merge rate, spend and cost per
merged card, time and cost per stage, where unmerged runs stopped, cost vs. duration, per-repository
numbers, and every run with its stage timeline. Filter by time range, repository, outcome or text; every
chart has a table view. It refreshes every 15 seconds and needs no internet access.

> **Security:** the dashboard links the agents' full transcripts (code and prompts) and has no login.
> `docker-compose.yml` publishes it on `127.0.0.1` only. Don't expose it publicly; to view it remotely use an
> SSH tunnel (`ssh -L 8080:localhost:8080 host`) or similar.

Cards move through **Triaged → In Progress → Verifying → Reviewing → PR Opened → Done**;
errors (checks or review that never pass, CI failures) go to **Failed** with a comment.

```
Trello Queued ─► triage ─► implement ─► make check ⟲ fix ─► review ⟲ revise ─► PR ─► CI ─► merge ─► Done
                  │                         │                │                     │
                  └── unclear / too big    └─ still failing └─ still rejected     └─ CI red: PR left open
                                     ▼             ▼                 ▼                     ▼
                                                 Trello Needs Info (with a comment)
```

Why polling instead of a Trello webhook: webhooks must call a public HTTPS URL, and the agent runs
on your machine. Polling one column every 15 s is cheap and exposes nothing. If you host it publicly
later, a webhook can replace the poll.

## Safety model

- **Secrets stay with the orchestrator.** It runs as root in the container and is the only process
  holding the GitHub and Trello tokens; it removes them from its environment at startup. Claude
  Code, and every command it runs, plus `make check`, run as the unprivileged `agent` user
  (`docker/claude-as-agent`), which can't read root's environment or files.
- **It only pushes to `agent/*` branches** and changes `main` only through a squash-merged PR.
- **It merges only after CI passes.** If the repository has no CI, it leaves the PR open unless
  `MERGE_WITHOUT_CI=true`.
- **It expects the base branch to be protected.** See [Branch protection](#branch-protection); `doctor` checks it.
- **Risky or large changes are left for a human.** Even with green CI, the PR is not merged (the card stays
  in PR Opened, with a comment listing why) if it touches a path matching `HUMAN_REVIEW_PATHS` (CI,
  dependencies, auth, migrations; comma-separated globs, see `.env.example` for the default list) or changes
  more than `MAX_AUTO_MERGE_LINES` lines (added + deleted, default `400`). Set `HUMAN_REVIEW_PATHS=` (empty)
  to disable path holds and `MAX_AUTO_MERGE_LINES=0` to disable the size limit.
- **Only allowed members can queue work.** With `ALLOWED_MEMBERS` set (Trello usernames, comma-separated),
  a card is processed only if the member who moved it into **Queued** is on the list; otherwise it goes to
  Needs Info without any agent call. Comments by other members are left out of what the agents read
  (lights-out's own comments are kept). If it is empty, any board member can queue work; `run` and
  `doctor` warn about it.
- **Budgets:** `MAX_TICKET_USD` per card (enforced across all agent calls) and `MAX_DAY_USD`
  per day. A card re-queued after failing is retried at most 3 times a day.
- **Crash recovery:** cards left mid-flight by a restart go back to Queued with a comment.
  A normal stop (SIGTERM, Ctrl-C, `docker compose stop`) stops taking new cards and puts the
  current card back right away if it is in an agent stage. Blocking stages (checks, and delivery: push,
  PR, CI wait, merge) run to the end instead; if that takes longer than the 30 s `stop_grace_period`
  (e.g. waiting for CI), Docker kills the process and the crash recovery above applies.

### Merging held PRs

A PR held for human approval (or left open after a CI problem) is followed up by lights-out. GitHub doesn't let
an author approve their own PR and lights-out opens PRs with the owner's token, so approval is a comment:

- Comment exactly `/lights-out merge` (surrounding whitespace is ignored) on the PR. It counts only if it was
  posted after the PR's latest commit by a user with write, maintain or admin access to the repository.
  Comments starting with lights-out's own marker never count.
- An approving review from another user with write access (not the PR author) on the latest commit counts too.
- CI on the latest commit must be green; otherwise lights-out replies on the PR with the reason and does not
  merge. Post the command again once CI is green: each command is acted on only once.
- On success the PR is squash-merged, the branch deleted and the card moved to Done. This overrides the
  merge policy hold, since a human approved.
- PRs merged by hand are synced (card to Done, run `merged`); PRs closed without merging move the card to Failed.

The watcher runs only when no card is in progress, at most every `PR_POLL_SECONDS` (default 120).

## Monitoring

The dashboard server answers `GET /healthz` with JSON:

```json
{"status": "ok", "last_beat": "2026-01-01T12:00:00+00:00", "age_s": 12, "current_card": "20", "stage": "ci"}
```

It returns HTTP 200 while the last sign of life is at most `HEALTH_STALE_S` seconds old (default 900) and
HTTP 503 with `"status": "stale"` otherwise. `current_card` and `stage` are `null` when idle.

What counts as a beat: each poll of the Queued list, each stage change, each poll while waiting for CI, and
every 60 seconds while a card is being processed (so long agent calls stay alive).

- `docker ps` shows `healthy`/`unhealthy` (the image has a `HEALTHCHECK` that calls `/healthz`), and
  `docker inspect --format '{{.State.Health.Status}}' <container>` prints it. Compose's
  `restart: unless-stopped` does not restart an unhealthy container; it only restarts one that exited.
- An external uptime monitor must run on the same host (`http://127.0.0.1:8080/healthz`) or reach it through
  a reverse proxy, because the dashboard port is published on localhost only.
- Limitation: blocking stages (`make check`, up to `CHECK_TIMEOUT_S`, default 1200) do not beat. If your
  checks run long, set `HEALTH_STALE_S` above `CHECK_TIMEOUT_S`.
- Only the `run` command serves `/healthz`, so one-off `docker compose run` containers show as unhealthy.

## Branch protection

lights-out merges automatically once CI is green. If the base branch (`BASE_BRANCH`, default `main`) of a
target repository is not protected, nothing stops a direct push or a merge without CI. Set this up on every
target repository (Settings → Branches → add a branch protection rule, or Settings → Rules → Rulesets):

1. Require a pull request before merging.
2. Require status checks to pass before merging, and select the CI jobs. For lights-out itself these are
   `check` and `docker` (from `.github/workflows/ci.yml`).
3. Block force pushes.
4. Don't allow bypassing: tick "Do not allow bypassing the above settings" (or "Include administrators");
   for a ruleset, leave the bypass list empty.

`lights-out doctor` reads both classic protection and active rulesets for `BASE_BRANCH` of every repository
in `REPO_LABELS` and `DEFAULT_REPO`:

- Problems (exit 1): no protection, no required status checks, or force pushes allowed.
- Warnings (exit 0): admins not included in classic protection, rulesets (their bypass actors can't be read
  with a normal token, so check them by hand), and a 403 because the token lacks *Administration: read*.

## Setup

1. Credentials: copy `.env.example` to `.env` and fill it in.
   - `ANTHROPIC_API_KEY`, or `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`.
   - Trello: get the API key at <https://trello.com/power-ups/admin> (create a Power-Up, then
     "API key"), then generate a token from the link on that page.
   - `REPO_LABELS`: which repositories lights-out may work on, keyed by Trello label, e.g.
     `REPO_LABELS=web=acme/website,api=acme/api`. See [Choosing the repository](#choosing-the-repository).
   - `ALLOWED_MEMBERS`: Trello usernames (without `@`) allowed to queue cards, e.g. `ALLOWED_MEMBERS=alice,bob`.
     Strongly recommended; see the Safety model.
   - Optional: `HUMAN_REVIEW_PATHS` and `MAX_AUTO_MERGE_LINES` decide which changes need a human to merge;
     see the Safety model.
   - Optional: `PLANNER_MODEL`, `CODER_MODEL`, `REVIEWER_MODEL` pick the model per role, and `PLANNER_EFFORT`
     (default `high`), `CODER_EFFORT` (default empty) and `REVIEWER_EFFORT` (default `high`) pick the effort
     (`low`, `medium`, `high`, `xhigh`, `max`). Empty means the model's default effort. Any other value is a
     config error. Each run records the models and efforts used as `settings` in the run log and `data.json`.
   - Optional: `HEALTH_STALE_S` (default 900): seconds without a sign of life before `/healthz` reports
     stale; see [Monitoring](#monitoring).
   - GitHub: a fine-grained token limited to those repositories with *Contents*, *Pull requests* and
     *Workflows* set to read/write and *Checks* set to read. Optionally add *Actions* set to read so CI
     failure logs can be sent to the coder (without it only check summaries are available), and
     *Administration* set to read so `doctor` can verify branch protection (without it, `doctor` only warns).
   - `TRELLO_BOARD_ID`: the board whose *Queued* column drives the work.
2. Build and check: `docker compose build && docker compose run --rm lights-out doctor`
   (`doctor` also checks the Claude credentials with a tiny real request, well under one cent)
   If you set `ALERT_WEBHOOK_URL`, send a test message with `docker compose run --rm lights-out test-alert`.
3. Create any missing board columns and repository labels: `docker compose run --rm lights-out setup-board`
4. If a repository is empty, give it a first commit (CI workflow running `make check`, a Makefile
   and a `CLAUDE.md` with conventions): `docker compose run --rm lights-out bootstrap <label>`
5. Start it: `docker compose up -d`. From then on, moving a card to **Queued** starts the agent.
   (`docker compose run --rm lights-out run-once` processes what's queued now and exits.)

## Alerts

Set `ALERT_WEBHOOK_URL` to be told when something needs you. lights-out sends `POST <url>` with the JSON
body `{"text": "lights-out: ..."}` (Slack's incoming-webhook format). Examples:

- Slack: `https://hooks.slack.com/services/...`
- Discord: `https://discord.com/api/webhooks/<id>/<token>/slack` (note the `/slack` suffix)
- Mattermost: `https://<host>/hooks/<id>`

Every message starts with `lights-out:` and names the card (number, title, URL) and the PR when there is one.
`ALERT_EVENTS` picks a comma-separated subset of the events (default: all; unknown names are a config error):

- `failed`: a card failed, with the stage and the first line of the error
- `pr_open`: a PR was opened but not merged (CI failed, CI timeout, no CI, needs human approval)
- `needs_info`: a card was sent to Needs Info
- `budget`: the daily budget (`MAX_DAY_USD`) is used up (once per day)
- `lifecycle`: the service started or is stopping (`run` daemon only)
- `error`: an unexpected exception in the scheduler loop

`lights-out test-alert` sends one test message (ignoring `ALERT_EVENTS`) and says whether it worked;
`doctor` only reports whether alerts are on. The URL is a secret: it is never logged or printed, and the
coding agent never sees it. A failed alert is logged as a warning and never fails a card.

## Choosing the repository

The board decides which repository each card is about, through labels. `REPO_LABELS` maps a label
to a repository:

```
REPO_LABELS=web=acme/website,api=acme/api
```

A card labelled **web** is implemented in `acme/website`. Other labels (bug, urgent, …) are ignored.
A card with no repository label, or with two, goes to **Needs Info** with a comment listing the valid
labels, unless `DEFAULT_REPO` is set, in which case unlabelled cards go there. Only repositories
listed in `REPO_LABELS` or `DEFAULT_REPO` can ever be touched, and the GitHub token must cover all of
them (`doctor` checks this).

## Writing good cards

- One piece of work per card, small enough for one PR, with exactly one repository label.
- Describe the expected behaviour; put acceptance criteria in a checklist.
- If the agent asks questions, answer them in a comment and move the card back to Queued. It reads
  the whole comment thread.

## Layout

| Module (in `src/lights_out/`) | Role |
|---|---|
| `cli.py` | command-line entry point |
| `core/config.py` | env-based configuration, secret scrubbing |
| `core/models.py` | ticket and comment data models |
| `integrations/trello.py` | board adapter (cards, comments, moves) |
| `integrations/github.py` | PRs, CI polling, merge |
| `orchestration/workspace.py` | clone/branch/commit/push, runs checks as the agent user, bootstraps empty repos |
| `agents/agents.py` / `agents/prompts.py` | triage, coder and reviewer agents (Claude Agent SDK) |
| `core/routing.py` | card label → repository |
| `orchestration/pipeline.py` | one card end to end |
| `orchestration/scheduler.py` | queue watcher (polls Queued), budgets, recovery, report server |
| `eval/` (repo root) and `eval/` (in `src/lights_out/`) | sample repository, evaluation tasks and the `lights-out eval` runner |
| `reporting/store.py` | SQLite run log and per-run stage timeline |
| `reporting/report.py` / `templates/dashboard.html` | dashboard data (`data.json`) and the self-contained dashboard page |

## Evaluation

`lights-out eval` runs the real pipeline (real Claude agents, real `make check`, real review) on the sample tickets in
`eval/tasks/` against a small sample repository (`eval/sample-repo/`), without Trello or GitHub. Afterwards it
checks the merged result with acceptance tests the agents never saw. **It costs real money**: every task makes real
Claude calls, and `--max-usd` (default 30) stops the run before a task starts once that much has been spent.

```
docker compose run --rm -v "$PWD/eval-results:/app/eval-results" lights-out eval [--tasks id1,id2] [--repeat N] [--out DIR] [--max-usd X]
```

Results go to `DIR/results.json` (default `eval-results/<UTC timestamp>/`), with a line per task on stdout. The eval
database, workspaces and agent transcripts also live in `DIR`, never in `/data`. The exit code is 0 whenever the
evaluation completes, whatever the pass rate. A task is a directory with `task.toml`, hidden `acceptance/` tests and an
optional `setup.patch` (see the existing ones).

## Development

`make check` runs ruff and the test suite (it creates a local `.venv`). Without a local Python:
`docker build -f docker/Dockerfile.test -t lights-out-test . && docker run --rm lights-out-test`.
CI runs `make check` and builds the Docker image on every push and pull request.

See [ROADMAP.md](ROADMAP.md) for what's next and [CHANGELOG.md](CHANGELOG.md) for what changed.

## License

[MIT](LICENSE)
