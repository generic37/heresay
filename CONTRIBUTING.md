# Contributing to Heresay

Contributions are welcome once the project is public. It's small on purpose, and
it means to stay legible — a patch that keeps it that way is worth more than one
that adds a feature.

## Licensing of contributions

By submitting a contribution (a pull request, a patch, a suggested diff), you
agree that your contribution is licensed to the project under the same terms as
the project itself (AGPL-3.0-or-later), **and** that you grant the maintainer the
right to relicense your contribution under other terms. That second grant is what
keeps dual-licensing viable: the reference instance is run under the AGPL, but the
copyright holder may also offer Heresay commercially, and a contribution that
couldn't be relicensed would foreclose that. If you're not able to make that
grant for a given change, say so in the PR and we'll figure it out.

No CLA to sign, no ceremony — submitting the change is the agreement.

## The house engineering culture

Three things this codebase believes, so a patch reads like it belongs:

- **Tests count occurrences, not presence.** `assert "hi" in output` passes just
  as happily when the line is there twice; when correctness is "exactly once,"
  the test must *count*. Several real bugs here hid behind presence assertions.
- **State files are sacred.** `accounts.json`, `mailboxes.json.d/`, `mail.key`,
  `revtells.json.d/`, `bans.json`, `state.json` and `reports/` are real people's
  real data. Never write, migrate, or "clean up" them in a test or a tool; tests
  use tmp paths, always. Room conversation is never written anywhere at all.
  ⚠ The two mail stores are **directories** — one file per box since v1.52.5, not
  the single JSON files the names suggest.
- **The covenant is not widenable.** "What's said here stays here" — nothing said
  in a room is persisted or logged, with exactly the two named, consented
  exceptions (mail and `.revtell`). A change that quietly broadens what gets kept
  is the one kind of change that will be refused on principle, however useful.

Those three are what a substantive change is judged against; the conventions and
the layout are in [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md). Run `make test`
before you open a PR.
