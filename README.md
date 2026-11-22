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
   is green**, comments the PR link on the card and moves it to **Done**.

Whatever happens, the card gets a comment. A live **dashboard** at <http://localhost:8080/> shows what
the agent is doing and how well it's doing it: runs per day by outcome, merge rate, spend and cost per
merged card, time and cost per stage, where unmerged runs stopped, cost vs. duration, per-repository
numbers, and every run with its stage timeline. Filter by time range, repository, outcome or text; every
chart has a table view. It refreshes every 15 seconds and needs no internet access.

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
- **Budgets:** `MAX_TICKET_USD` per card (enforced across all agent calls) and `MAX_DAY_USD`
  per day. A card re-queued after failing is retried at most 3 times a day.
- **Crash recovery:** cards left mid-flight by a restart go back to Queued with a comment.
  A normal stop (SIGTERM, Ctrl-C, `docker compose stop`) stops taking new cards and puts the
  current card back right away if it is in an agent stage. Blocking stages (checks, and delivery: push,
  PR, CI wait, merge) run to the end instead; if that takes longer than the 30 s `stop_grace_period`
  (e.g. waiting for CI), Docker kills the process and the crash recovery above applies.

## Setup

1. Credentials: copy `.env.example` to `.env` and fill it in.
   - `ANTHROPIC_API_KEY`, or `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`.
   - Trello: get the API key at <https://trello.com/power-ups/admin> (create a Power-Up, then
     "API key"), then generate a token from the link on that page.
   - `REPO_LABELS`: which repositories lights-out may work on, keyed by Trello label, e.g.
     `REPO_LABELS=web=acme/website,api=acme/api`. See [Choosing the repository](#choosing-the-repository).
   - GitHub: a fine-grained token limited to those repositories with *Contents*, *Pull requests* and
     *Workflows* set to read/write and *Checks* set to read.
   - `TRELLO_BOARD_ID`: the board whose *Queued* column drives the work.
2. Build and check: `docker compose build && docker compose run --rm lights-out doctor`
3. Create any missing board columns and repository labels: `docker compose run --rm lights-out setup-board`
4. If a repository is empty, give it a first commit (CI workflow running `make check`, a Makefile
   and a `CLAUDE.md` with conventions): `docker compose run --rm lights-out bootstrap <label>`
5. Start it: `docker compose up -d`. From then on, moving a card to **Queued** starts the agent.
   (`docker compose run --rm lights-out run-once` processes what's queued now and exits.)

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
| `reporting/store.py` | SQLite run log and per-run stage timeline |
| `reporting/report.py` / `templates/dashboard.html` | dashboard data (`data.json`) and the self-contained dashboard page |

## Development

`make check` runs ruff and the test suite (it creates a local `.venv`). Without a local Python:
`docker build -f docker/Dockerfile.test -t lights-out-test . && docker run --rm lights-out-test`.
CI runs `make check` and builds the Docker image on every push and pull request.

See [ROADMAP.md](ROADMAP.md) for what's next and [CHANGELOG.md](CHANGELOG.md) for what changed.

## License

[MIT](LICENSE)
