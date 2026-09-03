# Development

Working on the engine itself: the test suite, the code layout, and the two
conventions that outrank everything else. For the operator's side see
[`CONFIGURING.md`](CONFIGURING.md); for contribution terms and the engineering
culture see [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

## Tests

### Running it

```bash
make test                       # the whole suite — this is the one that counts
.venv/bin/pytest -q             # the same thing without the Makefile
.venv/bin/pytest -q tests/test_mail.py          # one file
.venv/bin/pytest -q -k "narrow and help"        # by name
.venv/bin/pytest -q -x tests/test_ranks.py      # stop at the first failure
```

**It takes about 28 minutes and runs 2,600+ tests.** Most of that is real
sockets, real websockets and injected clocks doing genuinely slow things; there
is no parallel mode and adding one is not free (several tests bind ports).

⚠ **The output is terminal-only.** `pytest -q` prints progress dots and the
failures at the end and writes no report file — if you want to keep the run, pipe
it: `make test 2>&1 | tee /tmp/suite.txt`. **The exit code is the answer**: 0 all
passed, 1 something failed, and `$?` is what a script should read rather than the
last line of text.

⛔ **A SUBSET PROVES THE SUBSET. The whole run is the evidence.** Run one file
while you are working — that is what it is for — but a release, a refactor or
anything that touches display strings is not verified until the full run is
green, and the count and the time go in the report. ⚠ A per-file sweep is only as
current as its last execution; see `CLAUDE.md` for the two occasions that lesson
was learned the hard way.

⚠ **`make audit` is separate and needs the network** (pip-audit over the pinned
requirements), which is why it is not part of `make test` — the suite runs
offline. `make release-check` is both.

### What it covers

Command parsing and the permission gate; the rate limiters (burst, refill,
escalation); room adjacency, movement and the world loader's fatal cases; mute
and `.ignore` filtering; the multi-line editor; profiles and descriptions.

**Storage and the covenant:** mail (send, receive after restart, `.dmail`
removing it from disk, the box cap, an encryption-at-rest canary, the purge, the
rate bucket), `.revtell`, the report box and its capture file, and the structural
sweeps — ⛔ **no code path reads another account's mail at any level**, no code
path writes message content to disk or to a log, and nothing in the engine reads
a capture file back.

**Moderation:** locks (announce, blocked `.go`, auto-unlock, staff bypass),
invites (delivery, join-through-lock, expiry, replacement, mute suppression),
ranks and levels (promote/demote persistence, the owner from config, the
target-rank rule on every command that names a person), gag, jail (held by the
FLAG, `.free` returning to the entry room), boot and bans over the real socket
including restart persistence, and `.mailpurge` emptying a box without reading
it.

**The doors:** an end-to-end test driving two concurrent socket sessions; the
WebSocket door's parity with a socket session (login, speak, `.who`, `.go`,
`.tell` — both doors see identical text); the `\x01echo off/on` password control
frames (never shown as room text); the telnet parser's cost and bounds; and the
frame rules in both directions.

**The words:** the wording sweep (no internal token reaches a screen), the
level-word sweep (no article in front of an operator's noun), the help screen at
both widths, and the doc-classification guard that fails on an unclassified
`docs/*.md`.

### The browser harness (`scripts/tabtest.js`)

Most of the client's behaviour *is* covered automatically. `scripts/tabtest.js`
runs the real `web/index.html` script in Node against a DOM stub — 123 checks
across seven modes (the tab mark, the favicon swap, the width report, the input
raise, the title cache, the client-only commands). `make test` runs it via
`tests/test_web_harness.py`; `make webtest` runs it alone in about a second.

**Node is optional.** It is not a dependency of the engine, so on a machine
without it (or with anything older than v18) those tests **skip** and the suite
still passes.

### Manual-test checklist (browser — no headless dependency)

What the harness cannot see — real fonts, real layout, real rotation, real
sound — is below; verify these by hand:

- [ ] **Loads offline:** open the page with devtools Network tab open. The only
      requests are the page itself and `IBMPlexMono-Regular.woff2` (+ the WS
      connection). Nothing leaves to any other host.
- [ ] **Looks like a terminal:** full-viewport mono text, the compose box at the
      bottom (one row at rest), no chrome. A screenshot could be mistaken for a
      terminal.
- [ ] **Converse both ways:** create an account; in a `telnet 127.0.0.1 2324`
      session in the same room, each sees the other speak.
- [ ] **Password masked:** during signup/login the password field shows dots and
      the password never appears on screen or in devtools frames as plain text
      (only `\x01echo off/on` control frames, which are not displayed). Shift+Enter
      does not insert a newline while masked.
- [ ] **Compose grows to 6 rows then caps:** press Shift+Enter repeatedly (or type
      a wrapping paragraph); the box grows one row at a time up to 6, the room text
      shrinking above it. At 6 rows further typing is refused with a beep and no
      7th row appears; a paste larger than fits is trimmed to 6 rows with one beep;
      backspace/editing within the cap still works.
- [ ] **Enter vs Shift+Enter:** Enter sends; Shift+Enter adds a newline.
- [ ] **Paragraph → one say:** compose a 3-line paragraph, send it; a `telnet
      127.0.0.1 2324` roommate receives it as **one** `says:` line, wrapped at 78
      by the server (not three messages). A line starting with `.` is still a
      command (e.g. `.who` with a trailing Shift+Enter blank still runs `.who`).
- [ ] **History only when empty:** Up/Down recall previously sent (collapsed)
      lines only when the box is empty; with a draft present, the arrows move the
      caret within it instead.
- [ ] **Themes:** `.theme green`, `.theme amber`, `.theme default` switch the
      palette and never reach the server; the choice survives a reload.
- [ ] **The tab:** on connect the tab reads **Commons** (the instance name)
      with the house favicon. **Reload** — it reads Commons *immediately*,
      before it reconnects (the name is cached per browser). Background it and
      have someone speak — BOTH the title and the favicon blink, at a steady one
      second in each state; come back and both restore.
- [ ] **Snap on send:** scroll the output up to read history, then type anything
      and press Enter — the view jumps to the newest line, so your command and
      its answer are both in sight. (On a phone this is the difference between
      "I sent that" and "I can see what it said".) It snaps for a server
      command, a client-only one like `.theme`, and plain speech alike.
- [ ] **Input offset:** `.raise_prompt 4` — the input jumps four lines up;
      `.raise_prompt -1` lowers it to three; `.raise_prompt -10` stops at the
      floor. `.raise_prompt` alone reports it. Reload: it sticks.
      With an auto-hidden taskbar, confirm the raised input clears the bar when
      it slides up. Nothing reaches the server.
- [ ] **Remember me:** create an account in a browser and answer **y** to "Stay
      logged in on this device?". Close the tab entirely, reopen the page — you
      are straight in, no password. Now `.quit` and reload: it asks for your
      password again — quitting is a full account logout on **every** device.
      Log back in and you are remembered once more (the setting survives a quit;
      only `.remember off` stops it). Answer **n** (or just press Enter) at signup
      instead and no token is ever issued — reloading always asks. Changing your
      password with `.passwd` signs every device out too. A `telnet 127.0.0.1
      2324` session is never offered any of this and never issued a token.
- [ ] **The 2-minute grace:** with two clients in the same room, kill one
      (close the tab). Under ~120 s, reopen: you land back in the **same room**
      with no "left"/"arrived" lines for the other person. Over ~120 s: the other
      person sees the departure at the moment the window lapses, and the returning
      client lands in the **entry room** — still logged in, just not where it was.
- [ ] **Narrow mode (portrait phone):** on a phone in **portrait**, the greeting
      is the narrow art and `.map`, `.help`, `.who` and `.set` all fit the screen
      with no horizontal scrolling and no `***` rule running off the edge.
      **Rotate to landscape** mid-session and run `.who` again — it comes back in
      the wide layout, live, with no reconnect. Rotate back and it narrows again.
      On a desktop, narrowing the browser window past roughly half its width does
      the same thing. A `telnet 127.0.0.1 2324` session is **always** 78 columns,
      whatever the terminal size — the socket door cannot report a width and
      never goes narrow.
- [ ] **500-line cap:** paste/generate >500 lines of output; the oldest lines
      drop from the DOM (scrollback stays bounded).
- [ ] **Disconnect clears:** stop the daemon (or `.quit`); the screen clears to
      a single `Connection closed. Press Enter to reconnect.` line. Press Enter
      to reconnect deliberately — no auto-reconnect loop.
- [ ] **No engagement machinery:** no notifications; no sound plays except from
      your own `.reboot` (below).
- [ ] **Background-tab mark:** with the tab focused, a room line changes nothing
      about the tab. Switch to another window (or another tab) and have someone
      say something: the title blinks `● Commons` — the house's own name — and the
      favicon becomes a dot.
      Come back: both clear at once, the title exactly as it was. Then
      `.ignore tab_alert yes` and repeat — nothing marks, for you alone; `.ignore
      tab_alert no` brings it back. Set `TAB_ALERT.enabled = false` in
      `web/index.html` and it does nothing at all, for anyone.

### Manual-test checklist (`.reboot` sound — browser)

WebAudio synthesis and the animated progress bar are browser-side; verify by eye
and ear on the web door:

- [ ] **Fake confirmation:** type `.reboot`. It asks `Reboot server? (y/n)`.
      Answer **`n`** — the boot runs anyway (also try `y`, Enter on empty, and
      gibberish; all proceed). No message ever acknowledges your answer.
- [ ] **Cues audible:** you hear, in order, a power-switch *thunk*, a fan spin-up
      settling into a faint hum, hard-drive grind/seek chatter, a floppy clunk,
      and a POST beep — timed to the boot lines.
- [ ] **Progress bar animates:** near the end, `REINITIALIZING WORLD STATE
      [####----------]` fills in place over its duration, then freezes as a line.
- [ ] **Only your command triggers it:** nothing plays on any other input, on
      login, or from another person talking. A second browser tab in the same
      room hears/sees **nothing** when you `.reboot` — only the dry emote line.
- [ ] **First-cue gesture:** the very first `.reboot` in a fresh tab still plays
      (the AudioContext is created lazily on your keypress).
- [ ] **Tunable constants:** open `web/index.html`, change `SOUND.masterGain`
      (e.g. to `0.4`) or `PROGRESS.fillChar`, refresh, `.reboot` again — the
      change shows. Each cue's frequencies/durations are likewise named constants.
- [ ] **`.reboot` ends clean:** the screen shows the login banner and the room
      (`.look`) when the sequence finishes; input you typed during the boot is
      discarded, not run afterward.

### Manual-test checklist (masked password field — browser)

A password prompt swaps the compose `<textarea>` for a masked field. That field
is **not** `type="password"` — it is a plain text input that never holds the
secret: every insertion is cancelled before it lands, the characters accumulate
in a JS buffer, and the field is repainted as one dot per character.

Why: Chromium's password manager matches by **registrable domain**, so a
credential saved for `example.net` is offered on `commons.example.net`
the moment a password field takes focus. That suggestion popup is browser
chrome — no page CSS or attribute can suppress it. The only way not to be
offered a credential is not to be a credential field.

- [ ] **No credential popup, with a real credential saved.** The load-bearing
      check: with a password saved for the parent domain (`example.net` — keep
      it saved, that is the point), reach any password prompt and focus it.
      **No suggestion popup, no key icon, no save offer**, on any of: creating
      an account, logging in, `.passwd`, and the recovery-code flow. Note the
      Chromium version you tried.
- [ ] **Dots appear per keystroke.** Type: one `•` per character, immediately.
      No character is ever visible, not even for a frame.
- [ ] **Backspace** removes one dot per press. (Ctrl+Backspace / word-delete
      also removes just one — deleting a secret wholesale when you can't see it
      would be worse.)
- [ ] **Paste works.** Paste a password in: dots appear for it, the text never
      does. A pasted newline is dropped rather than submitting.
- [ ] **Enter authenticates.** The password you typed actually logs you in —
      the buffer is what goes on the wire, not the dots.
- [ ] **The buffer is cleared afterwards.** After Enter, after the prompt ends,
      and after a disconnect, the field is empty and nothing is retained.
      Press ↑ in the compose box: masked entries never appear in history.
- [ ] **Idle: the field does not exist.** In normal compose, nothing sits below
      or beside the input. Drag-select across the whole bottom of the page and
      triple-click there: no second box, no stray empty selection, and Tab never
      lands on it. (It is `display:none` by rule, not merely blank.)
- [ ] **Active: it still looks like a terminal.** At `Password:` /
      `Choose a password:` / `Old password:`, the field is the same dark
      background, same monospace font and size, same width and position as the
      compose input, with no border, outline, or focus ring — the dots in the
      terminal's own text colour. A screenshot taken mid-prompt should be
      indistinguishable from any other frame. No white strip in any state.
- [ ] **Focus follows the prompt.** When the prompt appears, focus moves to the
      masked field without a click; clicking anywhere else on the page returns
      focus to it (the same behaviour the compose box has); when the prompt
      ends, focus returns to the compose box and typing resumes normally.
- [ ] **Theme-independent:** repeat with `.theme green` and `.theme amber` — the
      field's background and dots follow the theme, never the browser's default.

*Known limitation (IME):* a composition cannot be cancelled mid-flight, so while
composing with an IME the in-progress text is briefly visible before it commits;
it is buffered and masked on commit. ASCII entry never reaches that path.

## Layout

```
heresay/          package: server, session, transport, world, commands, mail,
                 bans, ratelimit, accounts, config
web/             index.html (self-contained terminal) + IBM Plex Mono woff2
                 + OFL.txt (the font's licence, ships with it)
house/           ALL authored content (operator-editable, read directly)
  world/         the authored world
    map.txt      the hand-drawn map, printed verbatim by .map
    rooms/<id>.txt one file per room: header lines, a blank line, the description
    echoes.txt   atmospheric lines the entry room murmurs (one per line; optional)
  help/          one file per .help <topic>
  greeting.txt   login banner, printed verbatim (built-in fallback)
  agreement.txt  shown at account creation
  theme.toml     rank/instance display words (built-in fallback)
  reboot.txt     .reboot boot-theater script (optional)
config.toml      configuration
accounts.json    account store (gitignored; created on first signup)
mailboxes.json.d/  encrypted mail, one file per mailbox
                 mail.key  Fernet key 0600  |  bans.json
                 (all gitignored runtime state)
tests/           pytest suite
deploy/          install/ (copy-once cards) + bin/ (live scripts) +
                 deploy.sh + Caddyfile.dev (nothing here is auto-installed)
scripts/         gencert.sh
```

## Dev notes

- **The name is Heresay, top to bottom.** The engine, the Python package, and the
  module paths are all `heresay/`. `.version` says *"…, Heresay version X"*, where
  "Heresay" is the software category, not the package name.
- **The covenant is the spec.** The README's opening states it, and a substantive
  change is judged against it. Tests count occurrences, not presence; state files
  are sacred; what's kept is never widened.
