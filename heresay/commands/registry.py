# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The command registry — ONE declaration per command (v1.28.0, extended v1.29.0).

Every command is declared exactly once, here: canonical name, category, the
form(s) help shows, its description, its aliases, and the rank ANCHOR it seeds
to. The dispatch table, the canonical-name lookup, the permission seed AND the
`.help` screen are all DERIVED from this tuple.

That is what closes the alias hole by construction — the gate keys on the
canonical command, so an alias cannot carry a different rank from the command it
belongs to — and, since v1.29.0, what makes the help screen incapable of drifting
from the permissions: `.help` is generated from this plus the live command table,
so reassigning a command's rank moves its help entry to that rank's section with
no edit anywhere.

**Data only, no handlers.** The help handler lives in `commands/__init__` itself,
so importing handlers here would be an import cycle; `__init__` binds `cmd_<name>`
from its own namespace and a test asserts every entry binds. That also leaves
handlers free to move between modules without touching this file.

**Anchors, not rank names.** A command seeds to `floor` / `staff` / `arch` — the
ROLES — resolved live against the ladder. Renaming a rank's word therefore breaks
nothing, and a staff command tracks the staff line even if the ladder is
reconfigured. Only a command pinned to a SPECIFIC custom rung stores a rank name,
and only that can be orphaned by a rename.

**The seed is not the authority.** `state.json` holds the live, authoritative
table; this tuple is what a fresh or upgrading house is seeded FROM, and what the
covenant tests read — because "nothing above the floor reads another's mail" is a
statement about the code, not something an operator can switch off by editing a
file.

**Descriptions carry placeholders, not baked words.** `{staff}` /
`{staff_plural}` / `{owner}` / `{resident}` are filled by the RENDERER from the
live theme, so the five
role-naming descriptions stay theme-aware from one source. An f-string here would
bake in whatever theme happened to be loaded at import.

**`usage` is a tuple** because one command can legitimately show several forms —
`.mail`, `.mail <name>`, `.mail <name> <text>` are three lines for one command,
all at one rank. `description` is then a matching tuple.

**`kind`** keeps three help-listed things out of the permission table:

    command  the 55 real ones      in _COMMANDS, in the table, gated
    speech   `say`                 bare speech bypasses the gate entirely
    client   `theme`, `raise_prompt`   browser-side; no server handler at all

Without it, generating help from this file would silently drop `<text>`, `.theme`
and `.raise_prompt` — three rows a resident has always had.
"""

from __future__ import annotations

from dataclasses import dataclass

# The rank ANCHORS a command may seed to. Resolved against the live ladder, so
# they survive a rank being renamed. Reserved as rank names (see theme.py): a
# rung called "staff" would make a table value ambiguous between the role and
# the rung.
RESIDENT, STAFF, OWNER = "resident", "staff", "owner"
ANCHORS = (RESIDENT, STAFF, OWNER)

# What a registry entry IS. See the module note.
COMMAND, SPEECH, CLIENT = "command", "speech", "client"

# Categories are CODE-DEFINED, never operator-editable: the help screen is built
# from them, so a house inventing its own would produce a screen the engine
# cannot lay out. The first ten are the house's own taxonomy (they were the
# short index's groups); the last three are the staff commands, which had no
# category at all before v1.28.0.
CATEGORIES = ("talking", "private", "world", "room", "self", "mail", "house",
              "account", "system", "misc", "moderation", "accounts",
              "administration")

# The word each category wears on screen. Code-side, like the categories: the
# ids are machine names, and "the world" reads better than "world" in a list a
# resident is scanning.
CATEGORY_LABELS = {
    "talking": "talking", "private": "private", "world": "the world",
    "room": "room life", "self": "yourself", "mail": "mail",
    "house": "the house", "account": "account", "system": "administrative",
    "misc": "misc", "moderation": "moderation", "accounts": "accounts",
    "administration": "administration",
}


@dataclass(frozen=True)
class Command:
    name: str                       # the canonical verb — the table's key
    category: str
    usage: tuple = ()               # the form(s) help shows; default (f".{name}",)
    description: tuple | str = ""   # {staff}/{staff_plural}/{owner}/{resident}
    aliases: tuple = ()
    anchor: str = RESIDENT             # the SEED value; state.json is the authority
    kind: str = COMMAND

    @property
    def forms(self) -> tuple:
        """[(form, description)] — one command may show several forms."""
        u = self.usage or (f".{self.name}",)
        d = self.description if isinstance(self.description, tuple) \
            else (self.description,) * len(u)
        return tuple(zip(u, d))

    @property
    def gated(self) -> bool:
        """Does this belong in the permission table? Only real commands do."""
        return self.kind == COMMAND

    @property
    def dispatchable(self) -> bool:
        """Does a typed verb reach a handler? `say` has a handler but no verb;
        the client verbs have neither."""
        return self.kind == COMMAND


C = Command

COMMANDS: tuple = (
    C("say", "talking",
      usage=('<text>',),
      description='say something to the room',
      kind="speech",
      ),
    C("emote", "talking",
      usage=('.emote <text>',),
      description='emote:  Corvid stretches.',
      aliases=('e', 'em', ';'),
      ),
    C("think", "talking",
      usage=('.think <text>',),
      description='Corvid thinks . . oO( ... )',
      ),
    C("shout", "talking",
      usage=('.shout <text>',),
      description='heard in every room (used sparingly)',
      ),

    C("tell", "private",
      usage=('.tell <name> <text>',),
      description='private message, anywhere',
      aliases=('t', '>'),
      ),
    C("remote", "private",
      usage=('.remote <name> <text>',),
      description='private emote, anywhere',
      aliases=('r', '<'),
      ),

    C("who", "world",
      usage=('.who',),
      description="who's on, where, idle, and away",
      aliases=('w',),
      ),
    C("look", "world",
      usage=('.look',),
      description='re-show the room and who is in it',
      aliases=('l',),
      ),
    C("go", "world",
      usage=('.go <room>',),
      description='move to a connected room (prefixes ok)',
      aliases=('g',),
      ),
    C("map", "world",
      usage=('.map',),
      description='show the world map',
      aliases=('m',),
      ),
    C("topic", "world",
      usage=('.topic <text>',),
      description="set this room's topic (it goes when all do)",
      ),

    C("lock", "room",
      usage=('.lock',),
      description='shut the door (in a lockable room)',
      ),
    C("unlock", "room", usage=('.unlock',), description='open it again'),
    C("invite", "room",
      usage=('.invite <name>',),
      description='invite someone to join you',
      ),
    C("join", "room",
      # Bare is the ordinary form since v1.52.4 — you hold one invitation, so
      # there is nothing to disambiguate — and the name is optional rather than
      # gone: naming who you meant is exactly what makes a stale invitation
      # refusable instead of silently honoured.
      usage=('.join [name]',),
      description='accept an invitation',
      ),
    C("rev", "room",
      usage=('.rev',),
      description="the entry room's last lines (.help rev)",
      ),

    C("afk", "self",
      usage=('.afk [message]',),
      description='mark yourself away (input brings you back)',
      ),
    C("desc", "self",
      usage=('.desc', '.desc <text>'),
      description=('show yours (and offer to clear it)',
                   'set your one-line description'),
      ),
    C("set", "self",
      usage=('.set',),
      description='show/change preferences (.help set)',
      ),
    C("profile", "self",
      usage=('.profile',),
      description='write/replace your profile (editor)',
      ),
    C("examine", "self",
      usage=('.examine', '.examine <name>'),
      description=('examine yourself, as others see you',
                   "read someone's description + profile"),
      aliases=('ex', 'exa'),
      ),
    C("revtell", "self",
      usage=('.revtell',),
      description='your last tells and remotes (.help revtell)',
      ),

    C("mail", "mail",
      usage=('.mail', '.mail <name>', '.mail <name> <text>'),
      description=('read your mailbox', 'compose a multi-line letter (editor)', 'send a one-line message (online or offline)'),
      ),
    C("dmail", "mail",
      usage=('.dmail <n>',),
      description='delete message n from your mailbox',
      ),
    C("staffmail", "mail",
      usage=('.staffmail',),
      description='read the shared {staff} letterbox',
      anchor="staff",
      ),
    C("dstaffmail", "mail",
      usage=('.dstaffmail <n>',),
      description='clear {staff} message n for all',
      anchor="staff",
      ),

    C("call_staff", "house",
      usage=('.call_staff <text>',),
      description='call the {staff_plural} — real trouble only',
      ),
    C("mail_staff", "house",
      usage=('.mail_staff <text>',),
      description='leave a note in the {staff} letterbox',
      ),
    # Reports (v1.50.0). Two ordinary permission-settable commands over one
    # shared mailbox — the access model is the command table, nothing else.
    # `.report` seeds to the RESIDENT floor because the people most likely to
    # need it are residents; `.read_report` to the OWNER, because a report can be
    # ABOUT a staff account and should not land in front of the class it names.
    # ⚠ Both are the operator's to move.
    C("report", "house",
      usage=('.report <name>', '.report <name> <text>'),
      description=('report someone (multi-line editor)',
                   'report someone in one line'),
      ),
    C("read_report", "house",
      usage=('.read_report',),
      description='read the report box',
      anchor="owner",
      ),

    C("passwd", "account",
      usage=('.passwd',),
      description='change your password',
      ),
    C("recovery", "account",
      usage=('.recovery',),
      description='codes left; .recovery new regenerates',
      ),
    C("remember", "account",
      usage=('.remember on|off',),
      description='web: stay logged in (.help remember)',
      ),
    C("deleteme", "account",
      usage=('.deleteme',),
      description='delete account, profile, mail, review — done',
      ),

    C("reboot", "system", usage=('.reboot',), description='initialize server'),
    C("version", "system", usage=('.version',), description='server version'),
    # AGPL §13: a network user must be OFFERED the corresponding source, and a
    # licence file in a checkout is not an offer to somebody at a text prompt.
    C("source", "system",
      usage=('.source',),
      description='where this house\'s source lives',
      ),
    # v1.36.1. Floor rank, and not a decision worth agonising over: what you
    # agreed to is not a privilege to read back. Assignable like anything else,
    # though a house that gated it would be a strange house.
    C("agreement", "system",
      usage=('.agreement',),
      description='re-read the house agreement',
      ),

    C("mute", "misc",
      usage=('.mute <name>',),
      description='stop hearing someone (this session)',
      ),
    C("unmute", "misc",
      usage=('.unmute <name>',),
      description='hear them again',
      ),
    C("ignore", "misc",
      usage=('.ignore',),
      description='hide kinds of line (.help ignore)',
      ),
    C("help", "misc",
      usage=('.help',),
      description='this list',
      aliases=('h',),
      ),
    C("quit", "misc",
      usage=('.quit',),
      description='leave',
      aliases=('q', 'x'),
      ),

    C("gag", "moderation",
      usage=('.gag <name>',),
      description='talker-wide silence (visible to the room)',
      anchor="staff",
      ),
    C("ungag", "moderation",
      usage=('.ungag <name>',),
      description='give the voice back',
      anchor="staff",
      ),
    C("jail", "moderation",
      usage=('.jail <name>',),
      description='confine to the jail room (persists)',
      anchor="staff",
      ),
    C("free", "moderation",
      usage=('.free <name>',),
      description='release to the start room',
      anchor="staff",
      ),
    C("boot", "moderation",
      usage=('.boot <name> [reason]',),
      description='disconnect a session',
      anchor="staff",
      ),
    C("ban", "moderation",
      usage=('.ban <name> [hours]',),
      description='refuse an account entry; persists',
      anchor="staff",
      ),
    # Staff tier, alongside `.ban`, since v1.33.0. It was arch-only when it
    # could expose an address; nothing prints one now (the "IPs are never shown"
    # rule), so the reason for the higher bar went with it. Assignable, as ever.
    C("ban_ip", "moderation",
      usage=('.ban_ip <name> [hours]',),
      description="ban the address they're on (confirms)",
      anchor="staff",
      ),
    C("unban_ip", "moderation",
      usage=('.unban_ip <name>',),
      description="lift the IP ban on their address",
      anchor="staff",
      ),
    C("bans", "moderation",
      usage=('.bans',),
      description='list the active bans, by name',
      anchor="staff",
      ),
    C("unban", "moderation",
      usage=('.unban <name|ip>',),
      description='lift a ban',
      anchor="staff",
      ),

    C("accounts", "accounts",
      usage=('.accounts',),
      description='list every account (name, rank, created)',
      anchor="owner",
      ),
    C("promote", "accounts",
      usage=('.promote <name>',),
      description='one rank up (persisted)',
      anchor="owner",
      ),
    C("demote", "accounts",
      usage=('.demote <name>',),
      description='one rank down (persisted)',
      anchor="owner",
      ),
    C("mailpurge", "accounts",
      usage=('.mailpurge <name>',),
      description='delete a whole mailbox unread (spam)',
      anchor="owner",
      ),
    C("deleteuser", "accounts",
      usage=('.deleteuser <name>',),
      description='erase an account for good (two gates)',
      anchor="owner",
      ),
    C("resetpassword", "accounts",
      usage=('.resetpassword <name>',),
      description="set someone's password (two gates)",
      anchor="owner",
      ),

    C("newusers", "administration",
      usage=('.newusers [on|off]',),
      description='open/close account creation (persists)',
      anchor="owner",
      ),
    C("logins", "administration",
      usage=('.logins [on|off]',),
      description='open/close new logons; {owner} always in',
      anchor="owner",
      ),
    C("logoutall", "administration",
      usage=('.logoutall',),
      description='disconnect everyone but you (confirms first)',
      anchor="owner",
      ),
    C("ownerset", "administration",
      usage=('.ownerset',),
      description='house-wide strings (.help ownerset)',
      anchor="owner",
      ),
    C("staff_actions", "administration",
      usage=('.staff_actions [n]',),
      description='recent {staff} actions, newest first',
      anchor="owner",
      ),
    # The two counting views (v1.35.0). `.staff_calls` seeds to STAFF because it
    # is staff-ops — the people it names are the people calling them — while
    # `.security` is the owner's glance and seeds to arch. Both assignable, like
    # everything else in the table.
    C("staff_calls", "administration",
      usage=('.staff_calls',),
      description='who has called or mailed the {staff_plural}',
      anchor="staff",
      ),
    C("security", "administration",
      usage=('.security',),
      description='auth and account-security counts',
      anchor="owner",
      ),
    # v1.36.0 — the builder's view. Staff tier because the people who author the
    # house are the people who need to see what it made of their files.
    C("load_report", "administration",
      usage=('.load_report',),
      description='the last load: counts and warnings',
      anchor="staff",
      ),

    C("theme", "misc",
      usage=('.theme <name>',),
      description='web: default / green / amber',
      kind="client",
      ),
    C("raise_prompt", "misc",
      usage=('.raise_prompt <n>',),
      description='web: lift the input off the bottom',
      kind="client",
      ),
)


# ---------------------------------------------------------------------------
# Derived, once, at import. Nothing below is hand-maintained.
# ---------------------------------------------------------------------------

BY_NAME: dict = {c.name: c for c in COMMANDS}

# verb (canonical OR alias) -> canonical name. The gate keys on THIS, so an alias
# can never carry a different rank from the command it is an alias of. Only
# dispatchable commands are in it: `say` has no typed verb and the client verbs
# have no server handler.
CANONICAL: dict = {v: c.name for c in COMMANDS if c.dispatchable
                   for v in (c.name, *c.aliases)}


def command_seed() -> dict:
    """The built-in permission table: canonical name -> anchor.

    What a fresh or upgrading house is seeded with. Only GATED entries: bare
    speech and the browser-side verbs are listed in help but have no rank to
    assign — pretending otherwise would put a row in `state.json` that changing
    could not possibly do anything.
    """
    return {c.name: c.anchor for c in COMMANDS if c.gated}


def privileged_commands() -> frozenset:
    """Every command reachable only ABOVE the floor, by the CODE's seeding.

    The covenant tests read this. Deliberately the seed and not `state.json`:
    "nothing above the floor reads another's mail or .revtell" is a guarantee
    about what the handlers do, and an operator re-assigning a rank must not be
    able to turn the check off.
    """
    return frozenset(c.name for c in COMMANDS if c.gated and c.anchor != RESIDENT)
