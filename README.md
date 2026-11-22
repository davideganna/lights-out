# ticket-agent

An autonomous software engineer driven by your Trello board. Write cards in *To Do*; **the moment you
move one to *Queued***, the agent picks it up (it checks the column every 15 seconds) and works through
queued cards one at a time, top first. For each card:

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

Whatever happens, the card gets a comment, and each day gets an HTML report at
<http://localhost:8080/>.

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

## Setup

1. Credentials: copy `.env.example` to `.env` and fill it in.
   - `ANTHROPIC_API_KEY`, or `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`.
   - Trello: get the API key at <https://trello.com/power-ups/admin> (create a Power-Up, then
     "API key"), then generate a token from the link on that page.
   - GitHub: a fine-grained token limited to the target repo with *Contents*, *Pull requests* and
     *Workflows* set to read/write and *Checks* set to read.
2. Build and check: `docker compose build && docker compose run --rm ticket-agent doctor`
3. Create any missing board columns: `docker compose run --rm ticket-agent setup-board`
4. If the repository is empty, give it a first commit (CI workflow running `make check`, a Makefile
   and a `CLAUDE.md` with conventions): `docker compose run --rm ticket-agent bootstrap`
5. Start it: `docker compose up -d`. From then on, moving a card to **Queued** starts the agent.
   (`docker compose run --rm ticket-agent run-once` processes what's queued now and exits.)

## Writing good cards

- One piece of work per card, small enough for one PR.
- Describe the expected behaviour; put acceptance criteria in a checklist.
- If the agent asks questions, answer them in a comment and move the card back to Queued. It reads
  the whole comment thread.

## Layout

| Module | Role |
|---|---|
| `config.py` | env-based configuration, secret scrubbing |
| `trello.py` | board adapter (cards, comments, moves) |
| `github.py` | PRs, CI polling, merge |
| `workspace.py` | clone/branch/commit/push, runs checks as the agent user, bootstraps empty repos |
| `agents.py` / `prompts.py` | triage, coder and reviewer agents (Claude Agent SDK) |
| `pipeline.py` | one card end to end |
| `scheduler.py` | queue watcher (polls Queued), budgets, recovery, report server |
| `store.py` / `report.py` | SQLite run log and HTML reports |

Tests: `docker build -f Dockerfile.test -t ticket-agent-test . && docker run --rm ticket-agent-test`
