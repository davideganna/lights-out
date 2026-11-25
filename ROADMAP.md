# lights-out roadmap

lights-out is an orchestrator that lets any team develop their own repository from
Trello (and later Jira) tickets: a card moved to the queue column is triaged, implemented,
tested, reviewed, opened as a pull request and merged when CI is green.

Plenty of tools turn an issue into a pull request. What lights-out should be known for:

- **The tracker drives the work.** Product people stay in Trello or Jira.
- **Self-hosted, bring your own keys.** Code and credentials stay on the user's infrastructure.
- **Auto-merge you can trust.** Most tools stop at "PR opened"; lights-out merges, so safety
  and measured reliability come before new features.

The phases are in priority order. Each item is a candidate ticket.

## Phase 1: Groundwork

- [x] Own repository, git history, CI on every push (`make check` = ruff + pytest, Docker image build)
- [x] MIT license, changelog, semantic versioning
- [x] Line endings normalized (`.gitattributes`) so Windows and Linux checkouts agree
- [x] Choose the target repository per card with a Trello label (`REPO_LABELS`); lights-out works on
      itself only when a card carries its label
- [x] Graceful shutdown: on SIGTERM, finish or roll back the current card and put it back in the queue
      (today a stop mid-card relies on crash recovery at the next start)
- [x] `doctor` also checks Claude authentication

## Phase 2: Safety (auto-merge is the risky part)

- [x] Document and verify branch protection on the target's `main`: required CI check, no bypass
- [x] Allow-list of Trello members whose cards are processed (anyone who can edit the board can
      currently give orders to an agent with merge rights)
- [x] Merge policy: auto-merge small or low-risk changes; require human approval when the diff touches
      CI, dependencies, auth, migrations, or is large. Configurable per repository (global settings for
      now; per-repository with `lights-out.yaml`)
- [ ] Network egress allow-list for the container (GitHub, Anthropic, package registries)
- [x] Reviewer hardening: flag changes that weaken tests or CI as blocking

## Phase 3: Reliability

- [x] Feed CI failure logs back to the coder and retry, like the local `make check` loop
- [x] Rebase onto a moved base branch and re-check before merging; resolve simple conflicts
- [ ] React to PR review comments: a human comment on the PR triggers a revision
- [ ] Health: heartbeat plus alerts (Slack or email) when a card fails or the service stops (alerts done)
- [x] Interactive dashboard: outcomes, merge rate, spend, time and cost per stage, per-repository metrics,
      run timelines
- [x] Link full agent transcripts from the dashboard

## Phase 4: Measure it (evaluation suite)

- [ ] 20–30 realistic tickets on a sample repository, each with hidden acceptance tests
- [ ] Metrics: success rate, cost and time per ticket, false "needs info", false "proceed"
- [ ] Run the suite in CI for prompt, model and effort changes; publish results in the README

## Phase 5: Packaging for other teams

- [ ] GitHub App instead of a personal token: separate bot identity, scoped permissions, installable
- [ ] One config file (`lights-out.yaml`): board, column mapping, label → repository mapping, policies,
      budgets, check command
- [ ] Pre-built image on GHCR and tagged releases
- [ ] `lights-out init`: interactive setup that checks credentials, creates board columns and
      bootstraps the target repository
- [ ] Hosted mode: Trello/Jira webhooks instead of polling, plus a deployment recipe for a small VPS
- [ ] Documentation site: quick start, configuration reference, security model, writing good cards

## Phase 6: Breadth

- [ ] Jira adapter (the tracker is already isolated in one module)
- [ ] Parallel cards, each in its own container
- [ ] More toolchains in the agent image, or a per-repository image
