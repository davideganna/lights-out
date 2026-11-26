# Conventions for agents working in this repository

- `make check` runs every linter and test. It must pass before a change is done,
  and CI runs exactly this command.
- Every feature ships with automated tests that cover the new behaviour.
  Tests are deterministic and need no network access.
- Keep changes focused on the ticket. No drive-by refactors.
- When you introduce a language or tool, wire it into `make check`.
