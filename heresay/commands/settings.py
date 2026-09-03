# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Command handlers — see the package __init__ for dispatch and shared helpers."""
from __future__ import annotations
import logging
import time
from ..accounts import valid_name
from ..state import house_phrase
from ..state import raise_bounds
from ..transport import (CTRL_FAVICON, CTRL_INWORLD, CTRL_RAISE_BOUNDS,
                         CTRL_TAB_ALERT, CTRL_TITLE)
from ._shared import (HELP_STYLES, NARROW_COLUMNS, SWITCH_KEYS, clean_message, framed,
                      narrow_columns, narrow_stacked, wrap_at, wrap_page)


def _house_phrase(session, key: str) -> str:
    """The house default for a `.set` message pref.

    Since 1.16.0 all five come from ONE place — the owner's `.ownerset`
    value (state) — instead of two (defaults.txt for the room wording, state for
    login/logout). That is the whole point of the rename: one scheme, one home,
    live for every key.
    """
    return house_phrase(getattr(session, "state", None), key)


PASSWD = "passwd"        # handled by the async session loop (echo-off prompts)
RECOVERY_NEW = "recovery_new"   # ditto: needs an echo-off password prompt
DELETEME = "deleteme"           # ditto: warn + echo-off password + disconnect
MSG_RESET_WORD = "default"

# key -> (when it fires, how it reads). The house default for every one of them
# is the live `.ownerset` value (see _house_phrase). The shape is how the
# line actually appears, so showing one previews the real thing: the room lines
# follow the name directly, afk_msg reads as a sentence, and the two world-wide
# presence lines are bracketed by the engine.
MSG_COMMANDS = {
    "enter_msg": ("walking into a room", "{name} {text}"),
    "exit_msg": ("walking out of a room", "{name} {text}"),
    "afk_msg": ("a bare .afk", "{name} is AFK: {text}"),
    "login_msg": ("logging in", "[{name} {text}]"),
    "logout_msg": ("leaving", "[{name} {text}]"),
}


def _cmd_message(session, args: str, key: str) -> None:
    """Shared body of the five message prefs, reached as `.set <key> [text]`:
    show / set / reset. The storage keys were renamed in 1.16.0 (inmsg ->
    enter_msg, …); a resident who set one years ago keeps it, because
    accounts.migrate_settings renames it in place on load."""
    res = session.resident
    when, shape = MSG_COMMANDS[key]
    house = _house_phrase(session, key)
    text = clean_message(args)

    def rendered(body: str) -> str:
        return shape.format(name=res.name, text=body)

    if not text:                                   # bare: show what you have
        own = (res.messages or {}).get(key)
        if own:
            session.send(f"Your {key} ({when}):  {rendered(own)}")
        else:
            session.send(f"Your {key} ({when}) is the house default:  "
                         f"{rendered(house)}\n"
                         f"Set your own with .set {key} <text>.")
        return

    if text.lower() == MSG_RESET_WORD:             # back to the house wording
        res.messages.pop(key, None)
        session.accounts.clear_setting(res.name, key)
        session.send(f"Your {key} is the house default again:  {rendered(house)}")
        return

    res.messages[key] = text
    session.accounts.set_setting(res.name, key, text)
    session.send(f"Your {key} ({when}):  {rendered(text)}")


def cmd_passwd(session, args: str) -> str:
    # The real work (three echo-off prompts) happens in the async session loop.
    return PASSWD


def cmd_deleteme(session, args: str):
    """Self-service account deletion. Everyone may leave for good — except the
    arch, whose account is named in config and must not vanish from under it. The
    warning, echo-off password prompt, and disconnect happen in the session loop."""
    res = session.resident
    if res.name.casefold() == session.config.owner_name.casefold():
        session.send(f"The house cannot delete its {session.theme.owner_rank}. "
                     "Demote or reconfigure first.")
        return None
    return DELETEME


def cmd_recovery(session, args: str):
    """`.recovery` reports how many codes remain; `.recovery new` regenerates the
    set (the regeneration needs an echo-off password prompt, so it defers to the
    session loop via RECOVERY_NEW)."""
    if args.strip().lower() == "new":
        return RECOVERY_NEW
    n = session.accounts.recovery_count(session.resident.name)
    plural = "" if n == 1 else "s"
    session.send(
        f"You have {n} unused recovery code{plural}. They can't be shown again — "
        f"use '.recovery new' to replace the set (this voids the old codes)."
    )


# ---------------------------------------------------------------------------
# .set — small resident preferences, stored on the account
# ---------------------------------------------------------------------------

SET_USAGE = ("Usage: .set <topic> <value>   |   .set <topic> default   |   "
             ".set default (all of them)\n"
             "Topics: dim on|off, time on|off, helpstyle long|short, "
             "enter_msg/exit_msg/afk_msg/login_msg/logout_msg <text>"
             "   |   .set name <NewCapitalization>")
SET_TOGGLES = ("dim", "time")
# Not a toggle (its values are words, not on/off), but the same shape: one
# stored key, one session attribute, house default when unset.
SET_HELPSTYLE = "helpstyle"


def _set_overview(session) -> None:
    """Everything `.set` knows, with its current value — one screen, one unit.

    The message prefs show the resident's own wording, or the house default
    marked as such, so "what am I set to" never needs five more commands."""
    res = session.resident
    width = session.width
    switches = [(k, _switch_display(session, k)
                 + ("   (house default)" if session.follows_house(k) else ""))
                for k in SWITCH_KEYS]
    messages = []
    for key in MSG_COMMANDS:
        own = (res.messages or {}).get(key)
        house = _house_phrase(session, key)
        mark = "" if own else "   (house default)"
        messages.append((key, f"{own or house}{mark}"))

    if width <= NARROW_COLUMNS:
        # The message values are whole phrases — "slips out toward the shop." —
        # so a two-column form at 40 leaves a ribbon and shreds every one of
        # them. Stacked, with a blank between: the shape the .help page uses,
        # so the listing and its documentation read alike.
        lines = narrow_columns(switches, width, gutter=2)
        lines.append("")
        lines.extend(narrow_stacked(messages, width))
        title = "Settings — .set <name> <value>"
    else:
        w = max(len(k) for k in MSG_COMMANDS)
        lines = [f"  {k:<{w}} {v}" for k, v in switches]
        lines.append("")
        lines.extend(f"  {k:<{w}} {v}" for k, v in messages)
        title = "Settings — .set <name> <value>, see .help set"
    session.send_raw(framed(title, wrap_page("\n".join(lines), width), width=width))


def _set_name(session, rest: str) -> None:
    """`.set name <NewCapitalization>` — restyle your OWN name's capitalization
    (display only). Case-only: the new value must casefold-equal your current
    name; the letters may not change. Identity/login/lookup/mail stay casefold, so
    only the displayed case changes — you still log in and get tells under any case.
    """
    res = session.resident
    new = rest.strip()
    if not new:
        session.send(f"You are shown as {res.name}. "
                     "Use '.set name <NewCapitalization>' to restyle it.")
        return
    if new.casefold() != res.name.casefold():
        session.send("You can only change the capitalization of your name, "
                     "not the name itself.")
        return
    if not valid_name(new):     # same letters, so it will pass — checked for safety
        session.send("Names are 2–16 chars, start with a letter, "
                     "letters/digits/-/_ only.")
        return
    if new == res.name:
        session.send(f"Your name already shows as {new}.")
        return
    # Display-only: persist the new capitalization, then update the live resident.
    # The casefold key is unchanged, so the world dict, mail, mute, and login all
    # keep working under any case.
    session.accounts.set_display_name(res.name, new)
    res.name = new
    session.send(f"Your name now shows as {new}.")


# ---------------------------------------------------------------------------
# .ignore — the per-resident category noise filter
#
# `.mute` filters a PERSON; `.ignore` filters a KIND OF LINE. They are separate
# on purpose: "I don't want to hear Bob" and "I don't want a running commentary
# of who walked through which door" are different wishes, and collapsing them
# would make one impossible to express without the other.
#
# Self-only, persisted on the account, and default OFF for every category — a
# new resident hears the whole house, and quiet is something you choose.
# ---------------------------------------------------------------------------

IGNORE_SETTING = "ignore"        # the account settings key holding the dict

# ⚠ FIVE of the category names below — login_msg, logout_msg, enter_msg,
# exit_msg, afk_msg — also exist as keys in state.HOUSE_DEFAULTS. They are
# UNRELATED, and the collision is a trap worth naming: a house key is the PHRASE
# ("enters the world"); a category here is A KIND OF LINE TO HIDE. They live in
# different namespaces on the account —
#
#     acct.settings["login_msg"]           -> the resident's own phrase
#     acct.settings["ignore"]["login_msg"] -> whether they hide those lines
#
# — so no `.ownerset` value has any bearing on this filter, and no category
# here has a house-configurable default. Every one of them defaults to the
# built-in "no" (show it). Do not "unify" the two lists.

# Order is the order `.ignore` lists them: the presence pairs, then the two
# room-life kinds, then the two ambient ones.
IGNORE_CATEGORIES = {
    "login_msg": "people logging in (including jailed arrivals)",
    "logout_msg": "people logging out",
    "enter_msg": "people walking into your room",
    "exit_msg": "people walking out of your room",
    "afk_msg": "people going AFK in your room",
    "shout": "shouts from elsewhere",
    "atmospherics": "the room's own murmurs",
    # The odd one out, and deliberately in this list: the others hide a KIND OF
    # LINE at delivery; this one turns off the web client's background-tab mark.
    # It belongs here because to a resident it is the same question — "do I want
    # to be shown this?" — and the covenant's answer is that the person decides.
    # It is web-only in effect: a socket session stores it as a harmless no-op.
    "tab_alert": "the browser tab mark when you're away (web only)",
}

# `.ignore` categories that are not a delivery filter but a setting the WEB
# CLIENT has to be told about. Changing one pushes a control frame.
IGNORE_PUSHES_TO_CLIENT = ("tab_alert",)

IGNORE_USAGE = ("Usage: .ignore   |   .ignore <category> yes|no|default"
                "   |   .ignore default (all of them)   (.help ignore)")


def _ignore_overview(session) -> None:
    """Every category with its current setting — one screen, one unit."""
    ig = session.resident.ignores or {}
    width = session.width
    tail = "yes = hidden from you.  This is yours alone; .mute silences a person."
    if width <= NARROW_COLUMNS:
        # The setting rides ON the label rather than in a column of its own:
        # "login_msg  yes" is the fact, and giving yes/no a third column at 40
        # would cost four characters that the description badly needs.
        rows = [(f"{key}  {'yes' if ig.get(key) else 'no'}", what)
                for key, what in IGNORE_CATEGORIES.items()]
        lines = narrow_stacked(rows, width)
        lines.append("")
        lines.extend(wrap_at(tail, width))
        title = "Ignore — .ignore <cat> yes|no"
    else:
        w = max(len(k) for k in IGNORE_CATEGORIES)
        lines = [f"  {key:<{w}}  {('yes' if ig.get(key) else 'no'):<3}  {what}"
                 for key, what in IGNORE_CATEGORIES.items()]
        lines.append("")
        lines.append(f"  {tail}")
        title = "Ignore — .ignore <category> yes|no, see .help ignore"
    session.send_raw(framed(title, wrap_page("\n".join(lines), width), width=width))


def _ignore_persist(session) -> None:
    """Write the filter back, and tell the browser if it needs to know."""
    res = session.resident
    # Stored as one dict under one settings key, so the account file gains a
    # single field however many categories exist. An empty dict is still written
    # (rather than cleared) so "I turned them all back on" persists as such.
    session.accounts.set_setting(res.name, IGNORE_SETTING, dict(res.ignores))


def _ignore_all_default(session) -> None:
    """`.ignore default` — every category back to showing.

    "Default" is unambiguous here in a way it is not for `.set`: NO category has
    a house-configurable default (there is no `.ownerset` key for any of
    them), so default always means the built-in "no" — show it. See the note on
    IGNORE_CATEGORIES about the names that merely LOOK house-backed.
    """
    session.resident.ignores.clear()
    _ignore_persist(session)
    push_client_settings(session)      # tab_alert is among them; a no-op on socket
    session.send("Every category is shown again. (.help ignore)")


def cmd_ignore(session, args: str) -> None:
    """`.ignore` lists; `.ignore <category> yes|no|default` sets one;
    `.ignore default` resets them all.

    An unknown category and a value that is not yes/no/default are both refused
    with the usage line — the filter has a fixed vocabulary, and silently
    accepting a typo'd category would leave someone believing they had turned
    something off.
    """
    args = args.strip()
    if not args:
        _ignore_overview(session)
        return
    parts = args.split(None, 1)
    key = parts[0].lower()
    value = (parts[1].strip().lower() if len(parts) > 1 else "")

    if key == MSG_RESET_WORD and not value:        # `.ignore default` — the lot
        _ignore_all_default(session)
        return

    if key not in IGNORE_CATEGORIES:
        session.send(f"No such category: {key}")
        session.send(IGNORE_USAGE)
        return
    if value not in ("yes", "no", MSG_RESET_WORD):
        session.send(IGNORE_USAGE)
        return

    res = session.resident
    # `default` and `no` land in the same place, and that is not a shortcut: the
    # built-in default for every category IS "no". Kept as separate words because
    # `.set <topic> default` reads the same way, and one grammar across both
    # commands is worth more than saving a branch.
    hide = value == "yes"
    if hide:
        res.ignores[key] = True
    else:
        res.ignores.pop(key, None)
    # Stored as one dict under one settings key, so the account file gains a
    # single field however many categories exist. An empty dict is still written
    # (rather than cleared) so "I turned them all back on" persists as such.
    _ignore_persist(session)
    if key in IGNORE_PUSHES_TO_CLIENT:
        # This one is not enforced at delivery — it lives in the browser, so the
        # browser has to be told. A no-op on a socket door (see send_control).
        push_client_settings(session)
    if value == MSG_RESET_WORD:
        session.send(f"{key}: shown (the default).")
    else:
        session.send(f"{key}: {'hidden' if hide else 'shown'}.")


def push_client_settings(session) -> None:
    """Tell the WEB client everything it cannot work out for itself.

    Called at login (so a fresh page starts correct) and again whenever one of
    these changes. Silently does nothing on a socket door — there is no tab to
    title there and no favicon to set, and `send_control` would refuse anyway.

    Four frames, and the reason each has to be pushed rather than built in:
      tab_alert    — the RESIDENT's own `.ignore` choice, which lives on their
                     account, not in the browser;
      raise_bounds — the HOUSE's `.raise_prompt` clamp, retunable by the owner;
      title        — the instance's name, so the tab says the house's own name rather than
                     the engine's name;
      favicon      — the house's icon, as a data: URL so the browser still makes
                     no external request;
      inworld      — that this connection is now attached to a resident in a
                     room, which is the ONLY state the tab mark may blink in.
    """
    ignores = getattr(session.resident, "ignores", None) or {}
    session.send_control(CTRL_TAB_ALERT + ("off" if ignores.get("tab_alert") else "on"))
    push_raise_bounds(session)
    theme = getattr(session, "theme", None)
    if theme is not None and theme.instance_name:
        session.send_control(CTRL_TITLE + theme.instance_name)
    # Always sent, even when empty: an empty favicon frame is how the client is
    # told "the blank icon is the normal one", which it needs in order to blink
    # back to something.
    session.send_control(CTRL_FAVICON + getattr(session, "favicon_url", ""))
    # Last, and deliberately so: by the time the client is told it is in-world,
    # it already has the title, icon and tab_alert setting it will need in order
    # to mark the tab correctly the first time.
    session.send_control(CTRL_INWORLD + "on")


def push_raise_bounds(session) -> None:
    """Push the house's `.raise_prompt` clamp to ONE web session."""
    lo, hi = raise_bounds(getattr(session, "state", None))
    session.send_control(f"{CTRL_RAISE_BOUNDS}{lo} {hi}")


def push_house_identity(session, theme, favicon_url: str) -> None:
    """The two things the BROWSER holds and cannot discover: the tab's name and
    its icon. Pushed on connect (above) and again whenever the house changes
    them (v1.39.0 — a live `theme.toml` reload)."""
    if theme is not None and theme.instance_name:
        session.send_control(CTRL_TITLE + theme.instance_name)
    session.send_control(CTRL_FAVICON + (favicon_url or ""))


def push_house_identity_all(world) -> None:
    """Push the house's name and icon to EVERY connected web session.

    House identity is house-wide, so a change has to reach everyone rather than
    only the next arrival — the same reasoning, and the same shape, as
    `push_raise_bounds_all` below. Sockets are skipped by send_control, as
    always: a terminal would print the frame.
    """
    for res in world.all_connected():
        if res.session is not None:
            push_house_identity(res.session, getattr(res.session, "theme", None),
                                getattr(res.session, "favicon_url", ""))


def push_raise_bounds_all(world) -> None:
    """Push the clamp to EVERY connected web session.

    House bounds are house-wide, so an owner changing them has to reach
    everyone — unlike `.ignore`, which is one person's own setting and reaches
    only them. Sockets are skipped by send_control, as always.
    """
    for res in world.all_connected():
        if res.session is not None:
            push_raise_bounds(res.session)


# The eight PREFERENCE topics — what `.set default` resets, and what bare `.set`
# lists. `name` is deliberately not among them (see _set_name) and neither is
# `.remember`, which is not a `.set` topic at all: both are identity/security
# rather than display, and a "tidy up my prefs" command that renamed you or
# signed your phone out would be a nasty surprise. Stated in help/set.txt.
SET_TOPICS = SWITCH_KEYS + tuple(MSG_COMMANDS)


def _switch_default(session, key: str) -> None:
    """Put one display switch back on the house — the reset half of the pair.

    Deleting the stored key is the whole mechanism: absent means "follow the
    house", so from here on this resident moves whenever the house does.
    """
    res = session.resident
    (res.switches or {}).pop(key, None)
    session.accounts.clear_setting(res.name, key)
    shown = _switch_display(session, key)
    session.send(f"{key} is the house default again: {shown}.")


def _switch_display(session, key: str) -> str:
    """The current value of a switch as a person reads it."""
    if key == SET_HELPSTYLE:
        return session.help_style
    return "on" if (session.dim_system if key == "dim" else session.show_time) else "off"


def _cmd_switch(session, key: str, rest: str) -> None:
    """One display switch: bare shows, `default` resets to the house, a legal
    value sets it. The same three-way shape as the message prefs, so the whole
    of `.set` answers to one grammar."""
    val = rest.strip().lower()
    legal = HELP_STYLES if key == SET_HELPSTYLE else ("on", "off")

    if not val:                                    # bare: show what you have
        shown = _switch_display(session, key)
        mark = "   (house default)" if session.follows_house(key) else ""
        session.send(f"{key} {shown}{mark}\n"
                     f"Set it with '.set {key} {legal[0]}|{legal[1]}', "
                     f"or '.set {key} default'.")
        return

    if val == MSG_RESET_WORD:
        _switch_default(session, key)
        return

    if val not in legal:
        session.send(SET_USAGE)
        return

    if key == SET_HELPSTYLE:
        session.help_style = val
        stored = val
    else:
        on = val == "on"
        if key == "dim":
            session.dim_system = on
        else:
            session.show_time = on
        stored = on
    session.accounts.set_setting(session.resident.name, key, stored)
    session.send(f"{key} {val}.")


def _set_all_default(session) -> None:
    """`.set default` — every preference topic back to its default at once.

    NOT `name` and NOT `.remember`: this resets how the house LOOKS to you, and
    neither of those is that. One renames you; the other would revoke your resume
    tokens and sign every device out.
    """
    res = session.resident
    for key in SWITCH_KEYS:
        (res.switches or {}).pop(key, None)
        session.accounts.clear_setting(res.name, key)
    for key in MSG_COMMANDS:
        (res.messages or {}).pop(key, None)
        session.accounts.clear_setting(res.name, key)
    session.send("All your .set preferences are back to the house defaults. "
                 "(Your name and .remember are untouched — they aren't display "
                 "preferences.)")


def cmd_set(session, args: str) -> None:
    """The whole preferences surface: three display switches and five message
    prefs, all answering to one grammar.

        .set                   list everything, with house-default markers
        .set <topic>           show one, and how to change it
        .set <topic> <value>   set it
        .set <topic> default   back to the house default
        .set default           all eight back to the house default

    Folded into one verb because they are one thing — "how this account is set
    up" — and eight near-identical top-level verbs made the help screen longer
    without making anything clearer.
    """
    args = args.strip()
    if not args:
        _set_overview(session)
        return
    parts = args.split(None, 1)
    key = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if key == MSG_RESET_WORD and not rest:         # `.set default` — the lot
        _set_all_default(session)
        return

    if key == "name":                              # restyle your OWN capitalization
        _set_name(session, rest)
        return

    if key in MSG_COMMANDS:                        # a message pref
        _cmd_message(session, rest, key)
        return

    if key in SWITCH_KEYS:                         # a display switch
        _cmd_switch(session, key, rest)
        return

    session.send(SET_USAGE)


# ---------------------------------------------------------------------------
# .remember — staying logged in on a device (web door only)
#
# The setting ships OFF and is opt-in. It is asked once at account creation
# (defaulting to no) and explained at length in help/remember.txt, because the
# behaviour is genuinely surprising: closing or backgrounding a tab keeps you
# logged in, and it has to, because a phone browser discards a backgrounded tab
# and a discarded tab looks exactly like a fresh open when you come back.
#
# That informed opt-in IS the consent. It is why `agreement.txt` did not have to
# change (and why nobody was made to re-consent): the agreement describes what
# the house keeps about everyone, and this is kept only for people who asked.
# ---------------------------------------------------------------------------

REMEMBER_SETTING = "remember"
REMEMBER_USAGE = "Usage: .remember on|off   (.help remember)"


def remembered(acct) -> bool:
    """Is remember-me on for this account? Absent means OFF — see above."""
    return bool(getattr(acct, "settings", {}).get(REMEMBER_SETTING))


def _revoke_all(session, name: str) -> int:
    """Drop every resume token for ``name``. Zero if there is no store (unit
    sessions) or the account never had one — either way, nothing to say."""
    store = getattr(session, "resume", None)
    return store.revoke_account(name) if store is not None else 0


def cmd_remember(session, args: str) -> None:
    """`.remember` shows; `.remember on|off` sets. `off` also revokes.

    Turning it off has to revoke, not merely stop issuing: a setting that says
    "no" while three devices still hold live tokens would be a lie, and it is
    the exact lie someone turning it off is trying to prevent.
    """
    res = session.resident
    value = args.strip().lower()
    acct = session.accounts.get(res.name) if session.accounts else None
    current = remembered(acct) if acct is not None else False

    if not value:
        state = "on" if current else "off"
        session.send(f"Remember me on this device: {state}.  {REMEMBER_USAGE}")
        return
    if value not in ("on", "off"):
        session.send(REMEMBER_USAGE)
        return

    if value == "on":
        session.accounts.set_setting(res.name, REMEMBER_SETTING, True)
        # Issued at the NEXT web login, not here: the token belongs to a browser,
        # and this command may well have been typed from a terminal.
        session.send("Remembering you on devices you log in from. "
                     "A browser will stay logged in for 30 days. Leaving with "
                     ".quit signs every device out. (.help remember)")
        return

    session.accounts.clear_setting(res.name, REMEMBER_SETTING)
    n = _revoke_all(session, res.name)
    session.send_control_resume("")        # this browser drops its copy too
    if n:
        session.send(f"No longer remembering you. {n} device(s) signed out.")
    else:
        session.send("No longer remembering you.")
