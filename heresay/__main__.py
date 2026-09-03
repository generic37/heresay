# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Entry point: ``python -m heresay``.

Starts the daemon: the plaintext dev listener, the WebSocket listener, and (if a
cert exists) the TLS listener. World and accounts load from disk; everything else
is in memory.

``--write-permissions`` is the one other thing this can do, and it exists
because the daemon must NOT: it creates or tops up ``permissions.toml``, which
lives in ``/etc`` beside ``config.toml`` and is read-only to the running
process. Run as root at deploy time (``deploy.sh`` does), or by hand whenever.

The v1.46.0 flags — ``--announce``, ``--restart-countdown``, ``--shutdown`` —
are a different shape again: they do not start anything, they TALK to a daemon
that is already running, over its local control socket. Same trust posture as
``--write-permissions``: something the operator does by being on the box. See
control.py for why that is a socket file and not a port.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

from . import __version__, control as control_mod, permissions
from .commands import BY_NAME, command_seed
from .config import ConfigError, load_config
from .permissions import PermissionsError
from .server import TalkerServer
from .state import CommandTableError, StateStore
from .theme import ThemeError, load_theme
from .world import WorldError

# ⛔ The refuse-to-boot family, in one place. Each of these is a deliberate
# "this configuration has no safe reading" — an unset owner_name, a `[ranks]`
# block with inverted anchors, an entry_room naming no room, a permissions value
# that is not a rank, a malformed command table. They are the engine telling the
# OPERATOR something, and an operator meets them at exactly one moment: the
# first time they start a house they have just configured.
#
# ⚠ So they print as a SENTENCE, not a traceback. A traceback is right for a
# bug — something nobody predicted — and wrong for a rule the engine states on
# purpose: it buries the one line that says what to fix under twenty that say
# where Python was. Exit 2, so a unit or a script can tell "would not start"
# from "started and stopped".
BOOT_REFUSALS = (ConfigError, ThemeError, WorldError, PermissionsError,
                 CommandTableError)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


async def _amain(config_path: str) -> int:
    cfg = load_config(config_path)
    # The path is passed IN so the live re-read has something to re-read;
    # the server never guesses it (see TalkerServer.__init__).
    server = TalkerServer(cfg, config_path=config_path)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # e.g. on platforms without signal support
            pass

    # start_server (called inside start()) is already accepting connections in
    # the background, so we simply wait for a shutdown signal.
    await server.start()
    logging.getLogger("heresay").info("heresay up; waiting for connections")

    # ⚠ TWO ways out, and they end the same way. A SIGTERM (systemctl stop or
    # restart) and a `--shutdown` countdown reaching zero both land here, so the
    # close path — flush the accounts, cancel the timers, shut the doors — is
    # the one path whichever way the house is asked to go.
    await asyncio.wait(
        [asyncio.create_task(stop.wait()),
         asyncio.create_task(server.stop_requested.wait())],
        return_when=asyncio.FIRST_COMPLETED)
    logging.getLogger("heresay").info("shutting down")
    await server.aclose()
    # ⛔ ZERO, and it matters: `Restart=on-failure` in the unit leaves a clean
    # exit alone, which is what makes `--shutdown` stay down.
    return 0


def write_permissions(config_path: str) -> int:
    """Create or top up `permissions.toml`. The ROOT-run half of the split.

    ⛔ The daemon may not do this. `permissions.toml` sits in `/etc/heresay/`
    beside `config.toml`, and the hardened unit mounts `/etc` read-only — as it
    should, since a daemon that can rewrite its own configuration is a daemon
    whose configuration is not protected. v1.37.0 had the server write the file
    at boot and crash-looped on `Errno 30` the first time it met a real
    deployment. This is the same work, done by the person who has the right to
    do it, at the moment they are deploying anyway.

    Idempotent. Absent -> written complete, merging any legacy `commands` block
    still in `state.json` so an upgrading house keeps its assignments. Present
    -> role-word values canonicalized to rank NAMES (v1.43.0, once) and only the
    missing commands appended, in a marked block, with every existing name-value
    and comment left exactly as it was.

    ⛔ **It exits non-zero on a file it cannot make sense of**, and `deploy.sh`
    aborts on that. This is the deliberate place for a bad permissions file to
    surface: here, with the old code still serving, rather than at the restart
    as a house that refuses to open.
    """
    cfg = load_config(config_path)
    path = cfg.permissions_path()
    # ⚠ The LADDER is what turns the seed's role anchors into this house's rank
    # names — the whole of the names-only rule on the writing side. Loaded the
    # same way the server loads it, so the file written here says exactly what
    # the daemon will read back.
    try:
        ladder = load_theme(cfg.theme_path())
    except ThemeError as exc:
        print(f"error: {cfg.theme_path()}: {exc}", file=sys.stderr)
        return 1
    # The legacy block, if this house has not migrated yet. Read through the
    # ordinary store so a corrupt table is refused here exactly as it is at boot.
    legacy = {}
    try:
        legacy = StateStore(cfg.resolve(cfg.state_file)).legacy_commands
    except Exception as exc:                     # a broken state.json: say so
        print(f"warning: could not read {cfg.state_file} ({exc}); "
              f"writing the built-in defaults", file=sys.stderr)
    try:
        action, names, converted = permissions.write_file(
            path, command_seed(),
            categories={n: c.category for n, c in BY_NAME.items()},
            version=__version__, legacy=legacy, ladder=ladder)
    except permissions.PermissionsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: cannot write {path}: {exc}", file=sys.stderr)
        print("       (run this as root — it writes into /etc)", file=sys.stderr)
        return 1

    if converted:
        # Said first and said plainly: this is a one-time rewrite of somebody
        # else's file, and it should never happen silently.
        print(f"{path}: converted {len(converted)} value(s) from role words to "
              f"rank names: " + ", ".join(converted))
    if action == "created":
        print(f"wrote {path} — {len(names)} command(s)"
              + (f", {len(legacy)} kept from state.json" if legacy else ""))
    elif action == "appended":
        print(f"{path}: appended {len(names)} new command(s) at their defaults: "
              + ", ".join(names))
    elif action == "canonicalized":
        print(f"{path} is complete — no commands added")
    else:
        print(f"{path} is already complete — unchanged")
    return 0


def control(config_path: str, request: dict) -> int:
    """Send one request to the running daemon's control socket; print the reply.

    ⚠ **This is a CLIENT.** It starts nothing, loads no world and holds no
    state — it opens a socket file, writes a line, reads a line and exits. If
    the socket is not there the daemon is not running, and saying so plainly is
    more use than a traceback.

    ⛔ It is local by construction: there is no host to give it. Reaching this
    channel means having a filesystem path and the permission to open it.
    """
    cfg = load_config(config_path)
    path = cfg.control_socket_path()

    async def _go() -> int:
        try:
            reader, writer = await asyncio.open_unix_connection(str(path))
        except (FileNotFoundError, ConnectionRefusedError):
            print(f"error: no daemon listening on {path}", file=sys.stderr)
            print("       (is heresay running? is this the right -c config?)",
                  file=sys.stderr)
            return 1
        except PermissionError:
            print(f"error: {path} is not yours to open — run this as the "
                  f"service user or root", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"error: {path}: {exc}", file=sys.stderr)
            return 1
        try:
            writer.write((json.dumps(request) + "\n").encode("utf-8"))
            await writer.drain()
            # ⚠ No timeout: a `--restart-countdown` deliberately blocks for the
            # whole countdown, which is the point of it. `--shutdown` returns at
            # once because the ENGINE owns that clock, not this process.
            raw = await reader.readline()
        finally:
            writer.close()
        try:
            reply = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            print("error: unreadable reply from the daemon", file=sys.stderr)
            return 1
        if not reply.get("ok"):
            print(f"error: {reply.get('error', 'refused')}", file=sys.stderr)
            return 1
        if "sent" in reply:
            print(f"announced to {reply['sent']} session(s)")
        elif reply.get("cancelled") is True:
            print("shutdown cancelled")
        elif reply.get("cancelled") is False:
            print("nothing was scheduled")
        else:
            # ⚠ `marks` + `first`, not the schedule (v1.47.0, audit F2). The
            # engine stopped echoing the whole list — at 100,000 minutes it was
            # megabytes on one line, past this process's own read limit, so the
            # CLI died on its own SUCCESS reply while the shutdown was armed.
            n = int(reply.get("marks") or 0)
            first = reply.get("first") or ""
            if n:
                print(f"{n} warning(s), the first {first} out")
            else:
                print("nothing to announce")
        return 0

    return asyncio.run(_go())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="heresay", description="Presence-only chat daemon.")
    default_cfg = str(Path(__file__).resolve().parent.parent / "config.toml")
    parser.add_argument("-c", "--config", default=default_cfg, help="path to config.toml")
    parser.add_argument(
        "--write-permissions", action="store_true",
        help="create or top up permissions.toml, then exit (run as root; the "
             "daemon never writes it)")
    # ---- the local control channel (v1.46.0) ---------------------------
    parser.add_argument(
        "--announce", metavar="TEXT",
        help="say one line to every connected session, then exit")
    parser.add_argument(
        "--restart-countdown", action="store_true",
        help="warn the house at the configured checkpoints and BLOCK until the "
             "last one, then exit (deploy.sh runs the restart afterwards)")
    parser.add_argument(
        "--shutdown", metavar="MINUTES",
        help="take the house down in MINUTES, warning every whole minute and "
             "again at 10 seconds; returns at once (the engine keeps the "
             "clock). 'cancel' calls off a pending one")
    args = parser.parse_args(argv)

    _setup_logging()
    if args.write_permissions:
        return write_permissions(args.config)
    if args.announce is not None:
        return control(args.config, {"cmd": "announce", "text": args.announce})
    if args.restart_countdown:
        return control(args.config, {"cmd": "restart"})
    if args.shutdown is not None:
        return control(args.config, {"cmd": "shutdown", "minutes": args.shutdown})
    try:
        return asyncio.run(_amain(args.config))
    except KeyboardInterrupt:
        return 0
    except BOOT_REFUSALS as exc:
        # Facts only, and the fact is the engine's own message: it names config
        # keys, room ids and file paths, never a word anybody said.
        logging.getLogger("heresay").error("refusing to start — %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
