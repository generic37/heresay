# Heresay — a talker engine

Heresay is a small telnet-talker-style chat engine in the NUTS lineage — a single
Python `asyncio` process. **Presence over persistence:** conversation is never
written to disk. No logs of what was said, no history, no scrollback, no replay
on reconnect. (The name is a homophone of *hearsay*, with the heresy shading
intended: a house that keeps nothing is a small act of dissent.)

Accounts, live conversation, and strict ephemerality over three doors (TLS,
localhost plaintext, and a browser WebSocket). Plus profiles and encrypted mail
(the one thing kept), a staff/owner moderation tier, atmospherics, a fading
room memory, and everything an operator needs to author a world in plain text.

### Engine vs. instance

**Heresay is the engine; a running instance is a HOUSE that you name and
shape.** What ships here is a **skeleton** house — one sealed room called
`void`, a template greeting, a template map, and the help pages — because the
rooms, the map, the rank words and the banner of a real instance are *its*
content, not the engine's. Anything named "staff", "owner", or a room below is
yours to replace, and replacing it is the ordinary way to use this:
**everything an operator authors lives in one directory, `house/`**: the world is
`house/world/`, the words are `house/theme.toml` / `house/greeting.txt` /
`house/help/` / `house/agreement.txt`. The instance's own NAME is the one thing
that is not in there: it is `[world] instance_name` in `config.toml`, with the
rest of what an install decides, so a fresh house is **one file to edit**. Point
Heresay at a different `house/` and it is a different house. See **`docs/CONFIGURING.md`**.

## Requirements

- Python 3.12+ (developed and tested on 3.13)
- Three runtime dependencies: `argon2-cffi` (password hashing), `websockets`
  (the browser door), and `cryptography` (mail encryption at rest). `pytest`
  (+`pytest-asyncio`) for the test suite.

## Quickstart — from clone to your own house

```bash
git clone <repo> heresay && cd heresay
make venv                 # .venv + the three runtime deps (pinned + hash-verified)
make venv-dev             # ...plus pytest and the audit tools, for working on it
make cert                 # a self-signed dev TLS certificate into certs/
make run-dev              # start on throwaway devstate/ (never your real state)
# connect from another terminal:  ncat 127.0.0.1 2324   (or telnet)
# That is a working house. The skeleton ships one room, `void`, and you arrive
# in it. Then read docs/CONFIGURING.md.

# OPTIONAL, whenever you want a world of your own — add a room and point the
# front door at it (or replace `void` in place; nothing depends on the name):
mkdir -p house/world/rooms
printf 'name: somewhere\n\nA plain room, and a beginning.\n' > house/world/rooms/somewhere.txt
# then in config.dev.toml, under [world]:  entry_room = "somewhere"
make run-dev              # restart — arrivals now land in "somewhere"
```

On start you'll see three listeners:

```
plaintext listener on 127.0.0.1:2324
TLS listener on 0.0.0.0:2323
WebSocket listener on 127.0.0.1:8765
```

If no certificate is present, the TLS listener is skipped with a warning and the
plaintext + WebSocket dev listeners still run. (`make run` uses your real local
state; `make run-dev` uses a throwaway `devstate/` — use `run-dev` while
experimenting.)

## How to connect

**Dev / local (plaintext, localhost only):**

```bash
telnet 127.0.0.1 2324
# or
ncat 127.0.0.1 2324
```

**TLS (the real door):**

```bash
openssl s_client -connect localhost:2323 -quiet
# or, with ncat:
ncat --ssl localhost 2323
```

The dev cert is self-signed, so `s_client` prints a `self-signed certificate`
verify note — that's expected for local use. For a real deployment, use a
proper certificate (e.g. Let's Encrypt) and point `config.toml` at it.

### First login

```
Name (or 'quit'): corvid
No one uses 'corvid'. Create it? (yes/no) yes
Choose a password: ······
Confirm password: ······
```

You arrive in the room named by `[world] entry_room` — in the shipped
skeleton that is `void`, which says so. That is a complete house already; when
you want your own, write rooms into `house/world/rooms/` and point `entry_room`
at whichever should be the front door. Type `.help` for the command list.

## Features at a glance

- **Ephemeral by construction.** Room speech, emotes, thinks, shouts and tells
  are never written to disk — not to a log, not to a database, not to a crash
  dump. There is no scrollback and no replay on reconnect.
- **Three doors, one house.** A TLS socket for the world, a localhost plaintext
  door for the operator's console, and a browser WebSocket client that ships in
  this repo (no framework, no build step, no external requests).
- **A world you author in plain text.** One file per room under `house/world/`,
  read directly by the engine — no generator, no intermediate artifact. Exits
  are directed, so a one-way passage is a line you did or did not write.
- **Encrypted mail — the one thing kept.** Addressed and consented: mail and
  `.revtell` buffers are Fernet-encrypted at rest, capped, and aged out. Nobody
  reads anybody else's, at any rank.
- **Moderation that leaves a record.** A configurable rank ladder, gag / jail /
  boot / ban with a target-rank rule, and staff actions recorded as facts in an
  operational event log that never holds a word anyone said.
- **Configuration that applies while it runs.** Permissions, most `config.toml`
  values and all authored content reload from disk without a restart; the things
  that cannot are refused loudly rather than half-applied.
- **A house that says its own words.** Ranks, notices, the instance name, the
  greeting and the help pages are yours; the engine keeps only the grammar.

## Documentation

| doc | what is in it |
|---|---|
| [`docs/COMMANDS.md`](docs/COMMANDS.md) | every command a resident can type, and what it does |
| [`docs/CONFIGURING.md`](docs/CONFIGURING.md) | the operator's manual — authoring a world, every `config.toml` dial, house settings, the web client, the state layout |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | operations: triage, the alerting layer, the event log, maintenance and backups |
| [`deploy/install/README.md`](deploy/install/README.md) | the fresh-server install checklist — stands a public house up end to end |
| [`docs/UPGRADING.md`](docs/UPGRADING.md) | what an operator has to **do** when crossing a version boundary — newest first, read back to the version you are on |
| [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) | working on the engine: the test suite, the code layout, the conventions |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | contribution terms and the engineering culture |
| [`SECURITY.md`](SECURITY.md) | how to report a vulnerability privately |

To change how your house looks and behaves, read
[`docs/CONFIGURING.md`](docs/CONFIGURING.md). To put it on a server, follow
[`deploy/install/README.md`](deploy/install/README.md).

## Deployment

`deploy/` holds a complete set of production artifacts as a starting point for
your own — **none of it is installed by this repo.** For a fresh
Ubuntu VPS, follow the numbered checklist at
[`deploy/install/README.md`](deploy/install/README.md); it stands a public house
up end to end, including TLS, the reverse proxy, backups, fail2ban and phone
alerting.

## Contributing

Contributions are welcome; the terms matter here more than usual, because the
copyright holder keeps the option of separate commercial licensing. Read
[`CONTRIBUTING.md`](CONTRIBUTING.md) first — it covers the licensing of
contributions and the engineering culture the codebase is held to.

## License

**AGPL-3.0-or-later** (see [`LICENSE`](LICENSE)). Copyright (C) 2026 BigPoetry.

In plain English: if you run a modified Heresay as a public house, the AGPL
requires you to offer your users the modified source — the network-use clause is
the whole point of the "A". The copyright holder additionally reserves the right
to offer Heresay under separate commercial terms (dual licensing); contributions
are accepted on terms that keep that possible — see
[`CONTRIBUTING.md`](CONTRIBUTING.md).

**The bundled font is separately licensed.** `web/IBMPlexMono-Regular.woff2` is
IBM Plex Mono, under the **SIL Open Font License 1.1** — see
[`web/OFL.txt`](web/OFL.txt), which travels with the font as that license
requires. It is not covered by the AGPL, and redistributing the web client means
redistributing that file with it.

## Security

Report a vulnerability **privately**, not in a public issue:

- **Email the maintainer: `heresay@bigpoetry.net`** — the engine's address,
  whichever house you found it on.
- **A GitHub security advisory** on this repository, once it is published.

⚠ For something wrong with one *house* rather than the engine, `.mail <owner>`
reaches that operator directly. See [`SECURITY.md`](SECURITY.md).

Please give us a chance to fix it before disclosing. There is little stored data
to leak, but the mail key, the account store, and the owner-account reservation
are the crown jewels — findings there especially welcome.
