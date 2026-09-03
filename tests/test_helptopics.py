# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

""".help <topic>: operator-editable pages loaded from help/ at startup."""

from pathlib import Path

import pytest

from heresay import commands
from heresay.commands import (NO_TOPIC, dispatch, help_text, help_topic_key,
                             unknown_command, wrap_page)
from heresay.config import Config
from heresay.server import _load_help_topics
from heresay.theme import DEFAULT_THEME as T

from conftest import FakeSession

REPO = Path(__file__).resolve().parent.parent
SHIPPED = _load_help_topics(REPO / "tests" / "fixtures" / "house" / "help")


@pytest.fixture
def topical(world, clock):
    return FakeSession(world, name="Corvid", clock=clock, help_topics=SHIPPED)


# ---- the shipped pages ---------------------------------------------------

def test_repo_ships_the_authored_pages(world, clock):
    """One page per command ANYONE can type — resident, staff and owner —
    plus the concept pages. A new file is a new topic, so this list IS the help/
    directory. The staff pages arrived with the two-tier split in v1.20.0."""
    assert sorted(SHIPPED) == [
        "accounts", "afk", "agreement", "ban", "ban_ip", "bans", "boot",
        "call_staff",
        "deleteme", "deleteuser", "demote", "desc",
        "dmail", "dstaffmail", "emote", "examine", "free", "gag",
        "go", "ignore", "invite", "jail", "join", "load_report", "lock", "logins",
        "logoutall", "look", "mail", "mail_staff", "mailpurge", "map",
        "mute", "newusers", "ownerset", "passwd", "profile", "promote", "quit",
        "raise_prompt", "read_report", "reboot", "recovery", "remember", "remote",
        "report", "resetpassword", "rev", "revtell", "security", "set", "shout",
        "source", "staff_actions", "staff_calls", "staffmail", "tell", "theme",
        "think", "topic", "unban",
        "unban_ip", "ungag", "unlock", "unmute", "version", "who",
    ]


def test_every_staff_command_has_a_page():
    """The rule the split makes necessary: both grids and `.help <topic>` are
    only complete if every gated verb has a page of its own."""
    from heresay.commands import privileged_commands
    missing = sorted(privileged_commands() - set(SHIPPED))
    assert missing == [], f"staff commands with no help page: {missing}"


def test_the_set_page_lists_every_topic_and_the_whole_grammar():
    """Still one line per topic — but since v1.25.0 the page also has to teach a
    grammar (`<value>` / `default` / bare `default`) that applies to all of them,
    and say why `name` is the exception to it."""
    page = SHIPPED["set"]
    assert page.startswith("set — ")
    for topic in ("dim", "time", "helpstyle", "enter_msg", "exit_msg",
                  "afk_msg", "login_msg", "logout_msg"):
        assert any(ln.startswith(topic) for ln in page.split("\n")), topic
    # The four forms of the grammar, each named.
    for form in (".set <topic> <value>", ".set <topic> default", ".set default"):
        assert form in page, form
    # name is documented as outside the reset, and so is .remember.
    assert "name is the exception" in page
    assert ".remember" in page
    # The deleted prose is deleted, not relocated.
    assert "60 characters" not in page
    assert "Settings persist on your account." not in page


def test_the_ignore_page_teaches_the_same_grammar():
    page = SHIPPED["ignore"]
    for form in (".ignore <cat> yes|no", ".ignore <cat> default", ".ignore default"):
        assert form in page, form
    # And is explicit that, unlike .set, nothing here follows the house.
    assert "nothing here follows a house setting" in page


def test_the_rev_family_is_three_pages_now():
    """rev.txt split: the room's memory, your own private memory, and the note
    on the door were three subjects sharing one page."""
    # The exact header wording is the maintainer's to keep current (v1.5.0 made .rev a
    # fading window, so any "last 20 lines" phrasing is his pen's, not a test's);
    # pin only that it is the .rev page and covers what the room keeps.
    assert SHIPPED["rev"].startswith(".rev — ")
    assert "thinks, shouts, tells and notices are not kept" in SHIPPED["rev"]
    assert SHIPPED["revtell"].startswith(".revtell — your own last 20 private lines")
    assert "no rank can read another's" in SHIPPED["revtell"]
    assert SHIPPED["topic"].startswith(".topic — a public banner")
    assert "when the last\nperson leaves, it vanishes with them" in SHIPPED["topic"]


def test_the_topic_page_describes_the_new_position():
    """The page must not still say 'under the room's name' — it moved."""
    assert "below the exits" in SHIPPED["topic"]
    assert "under the room's name" not in SHIPPED["topic"]


# ---- house style: every shipped page obeys the caps -----------------------
#
# The LENGTH cap is gone (v1.25.1). It was 12 lines with a growing exemption
# list — reboot, ownerset, remember, set, ignore — which is the shape of a
# rule that had stopped being one. How long a page should be is the owner's
# call; the engine's job is to render the file it is given.
#
# What makes "any length" safe is the trailing-blank trim, and that has its own
# test below: a long or sloppy file renders with no wall of empty lines under
# the border.
#
# The WIDTH cap stays. It is not a matter of taste: a line over 78 columns is
# re-wrapped by the renderer and the operator's own layout is lost, so the cap
# is really "author at the width the page will be shown at".


@pytest.mark.parametrize("name", sorted(SHIPPED))
def test_every_page_obeys_the_house_style(name):
    page = SHIPPED[name]
    lines = page.split("\n")
    assert " — " in lines[0], "header is 'command — short phrase'"
    assert lines[1] == "", "a blank line under the header"
    assert all(len(ln) <= 78 for ln in lines), "78 columns, always"


def test_a_long_sloppy_page_still_renders_cleanly(world, clock):
    """What makes "any length" safe, and therefore what replaces the cap.

    A page may now be as long as its author likes, so the thing worth pinning is
    that a file with trailing blank lines, whitespace-only lines and trailing
    spaces does not produce a wall of emptiness under the border — while blank
    lines INSIDE the page survive, because those are the author's paragraphing.
    """
    page = ("who — a deliberately sloppy page\n"
            "\n"
            "first line   \n"          # trailing spaces
            "\n"                       # an INTERIOR blank: must survive
            "second line\n"
            "\n\n\n"                   # trailing blanks: must go
            "   \n"                    # whitespace-only line: must go
            "\t\n")                    # ditto
    s = FakeSession(world, name="Corvid", clock=clock, help_topics={"who": page})
    dispatch(s, ".help who")
    lines = s.out[-1].split("\n")

    assert set(lines[-1]) == {"*"}, "the block ends on the bottom rule"
    assert lines[-2] == "", "exactly one framing blank above it, no more"
    assert lines[-3] == "second line", "and the last real line sits right there"
    assert "" in lines[3:-2], "an interior blank line survived"
    assert not any(ln != ln.rstrip() for ln in lines), "no trailing whitespace"


def test_the_msgs_page_is_gone():
    assert not (REPO / "tests" / "fixtures" / "house" / "help" / "msgs.txt").exists()
    assert "msgs" not in SHIPPED


def test_the_reboot_page_ships_and_reads_as_authored():
    page = SHIPPED["reboot"]
    assert page.startswith(".reboot — initialize server")
    assert "80486DX2 (66 MHz, the good one)" in page
    assert "QUANTUM LP240A 540MB" in page
    assert r"C:\HOUSE\TALKER.EXE" in page        # the backslashes survive verbatim
    assert "in the way one documents a fire axe" in page
    assert page.rstrip().endswith("The owner thanks you for not rebooting the server.")


def test_help_files_are_not_excluded_from_the_deploy_rsync():
    """help/ is authored repo-root content, like world/ — it must ship."""
    script = (REPO / "deploy" / "deploy.sh").read_text()
    excludes = [ln for ln in script.split("\n") if "--exclude" in ln]
    assert excludes                                   # sanity: we found the block
    assert not any("help" in ln for ln in excludes)


# ---- lookup --------------------------------------------------------------

def test_known_topic_renders_the_file(topical):
    dispatch(topical, ".help set")
    assert "fade system lines" in topical.text
    assert "capitalization" in topical.text   # the new .set name row


@pytest.mark.parametrize("topic,marker", [
    ("rev", "thinks, shouts, tells and notices are not kept"),
    ("revtell", "no rank can read another's"),
    ("topic", "it belongs to the gathering, not the room"),
    ("set", "prefix lines with HH:MM"),
    ("reboot", "in the way one documents a fire axe"),
])
def test_each_shipped_page_renders_through_its_own_topic(topical, topic, marker):
    dispatch(topical, f".help {topic}")
    assert marker.lower() in topical.text.lower(), topic
    assert topical.text.lstrip("\n").startswith(f"*** .help {topic} *")


def test_help_msgs_is_gone_for_good(topical):
    """'.msgs' was folded into .set and is not a command at all any more, so
    asking for its page is asking about a verb that does not exist."""
    dispatch(topical, ".help msgs")
    assert topical.out[-1] == unknown_command("msgs")


def test_a_name_that_is_not_a_command_gets_the_unknown_command_line(topical):
    for probe, verb in ((".help nosuchthing", "nosuchthing"),
                        (".help umnute", "umnute"),        # the typo, asked about
                        (".help tel", "tel")):             # not an alias either
        topical.clear()
        dispatch(topical, probe)
        assert topical.out[-1] == unknown_command(verb), probe


def test_the_two_failures_are_said_differently(topical):
    """A typo and a missing page are different facts, so they get different
    sentences. Saying "no help" for both taught that a typo was a doc gap."""
    dispatch(topical, ".umnute Wren")             # typed directly: unchanged
    typed = topical.out[-1]
    topical.clear()
    dispatch(topical, ".help umnute")             # asked about: the same line
    assert topical.out[-1] == typed == unknown_command("umnute")
    topical.clear()
    dispatch(topical, ".help unmute")             # a real verb: its own page
    assert "undoes a .mute" in topical.text
    topical.clear()
    dispatch(topical, ".help t")                  # a real alias with no page
    assert topical.out[-1] == NO_TOPIC


def test_the_lookup_never_reveals_a_verb_above_your_rank(world, clock):
    """A staff verb does not exist, as far as a resident is concerned — and
    `.help gag` has to answer exactly as `.gag` does, or the lookup becomes the
    hole the level-gating was built to close."""
    res = FakeSession(world, name="Corvid", clock=clock, help_topics=SHIPPED)
    for probe, verb in ((".gag Wren", "gag"), (".help gag", "gag"),
                        (".help ownerset", "ownerset"),
                        (".help mailpurge", "mailpurge")):
        res.clear()
        dispatch(res, probe)
        assert res.out[-1] == unknown_command(verb), probe


@pytest.mark.parametrize("level", ["wizard", "arch"])
def test_staff_get_the_ordinary_answer_for_their_own_verbs(world, clock, level):
    """Above the rank, the same two answers as anyone: the page if one exists,
    else 'no help'. There are no staff pages yet; the machinery will render
    them the moment the maintainer writes one."""
    staff = FakeSession(world, name="Warden", clock=clock, level=level,
                        help_topics=SHIPPED)
    dispatch(staff, ".help gag")
    if "gag" in SHIPPED:
        assert staff.text.lstrip("\n").startswith("*** .help gag *")
    else:
        assert staff.out[-1] == NO_TOPIC
    # And a page that DOES exist renders for them exactly as it does for anyone.
    staff.clear()
    dispatch(staff, ".help version")
    assert "what is running" in staff.text


def test_a_keeper_page_renders_the_moment_it_exists(world, clock):
    """The rule stated as a test rather than as a promise: with a page present,
    staff get it and a resident still gets the unknown-command line."""
    topics = dict(SHIPPED, gag=".gag — silence someone\n\nthe page")
    staff = FakeSession(world, name="Warden", clock=clock, level="wizard",
                        help_topics=topics)
    dispatch(staff, ".help gag")
    assert "silence someone" in staff.text
    res = FakeSession(world, name="Corvid", clock=clock, help_topics=topics)
    dispatch(res, ".help gag")
    assert res.out[-1] == unknown_command("gag")


def test_topic_names_are_sanitized_to_lowercase_alphanumerics_and_underscore():
    """The underscore joined the allowlist in v1.22.0 so `raise_prompt` survives
    normalization. It cannot name a directory, a parent or a separator, so the
    path-safety property below is untouched."""
    assert help_topic_key("SET") == "set"
    assert help_topic_key("../etc") == "etc"
    assert help_topic_key("../../etc/passwd") == "etcpasswd"
    assert help_topic_key("  ") == ""
    assert help_topic_key("m-s.g_s") == "msg_s"
    assert help_topic_key("raise_prompt") == "raise_prompt"
    assert help_topic_key("RAISE_Prompt") == "raise_prompt"


def test_path_shaped_topics_are_safe(topical):
    # Lookup is a dict of pages loaded at startup, never a filesystem read, so a
    # path-shaped topic can only ever MISS. Since v1.22.0 the miss quotes back
    # what was actually typed (being told ".raiseprompt" when you typed
    # ".raise_prompt" sends you hunting a typo you did not make) — which is safe
    # because it goes only to the typist, every output line is control-stripped
    # at the session sink, and it is length-capped so a paste is not mirrored.
    for probe in ("../etc", "../../etc/passwd", "/etc/shadow", "../help/set.txt"):
        topical.clear()
        dispatch(topical, f".help {probe}")
        assert topical.out[-1] == unknown_command(probe), probe
        assert "No additional help available." not in topical.text   # never read


def test_a_pasted_topic_is_not_mirrored_back_at_length(topical):
    topical.clear()
    dispatch(topical, ".help " + "x" * 400)
    assert len(topical.out[-1]) < 80
    # Punctuation is simply dropped, so a decorated real topic still resolves —
    # harmlessly, to the page of that name and nothing else.
    topical.clear()
    dispatch(topical, ".help ....//set")
    assert "fade system lines" in topical.text


def test_case_and_padding_are_forgiven(topical):
    for probe in (".help SET", ".help   Set  ", ".help s-e-t"):
        topical.clear()
        dispatch(topical, probe)
        assert "fade system lines" in topical.text, probe


def test_missing_help_dir_is_never_fatal(tmp_path, world, clock):
    assert _load_help_topics(tmp_path / "nope") == {}
    bare = FakeSession(world, name="Corvid", clock=clock, help_topics={})
    dispatch(bare, ".help topic")
    assert bare.out[-1] == NO_TOPIC


def test_a_new_file_is_a_new_topic_with_no_code_change(tmp_path):
    (tmp_path / "tea.txt").write_text("All about tea.\n", encoding="utf-8")
    topics = _load_help_topics(tmp_path)
    assert topics == {"tea": "All about tea."}


# ---- the footer on bare .help --------------------------------------------

def test_bare_help_carries_the_pointer_and_not_a_topic_list(topical):
    """There is a page per command now, so naming them all under a list of the
    same commands was printing the same thing twice."""
    dispatch(topical, ".help")
    text = topical.text
    assert "More: .help <command>" in text
    assert "Topics:" not in text
    assert text.rstrip().endswith("*" * 78)          # and stays inside the frame


def test_the_pointer_does_not_depend_on_the_pages_being_there(world, clock):
    """A deployment with no help/ still shows the pointer — the footer describes
    the command, not the directory (and .help x then says so plainly)."""
    bare = FakeSession(world, name="Corvid", clock=clock, help_topics={})
    dispatch(bare, ".help")
    assert "More: .help <command>" in bare.text
    bare.clear()
    dispatch(bare, ".help topic")
    assert bare.out[-1] == NO_TOPIC


def test_every_registry_command_can_be_looked_up():
    """The footer's promise, checked against the REGISTRY now that the screen is
    generated from it: every command it can show has a page. Two exceptions, both
    as before: bare speech is not a verb you can type `.help` for, and `.help`
    documents itself by being the thing you just typed."""
    from heresay.commands.registry import COMMANDS
    for c in COMMANDS:
        if c.name in ("say", "help"):
            continue
        assert c.name in SHIPPED, c.name


def test_bare_help_is_the_help_text_verbatim(topical):
    dispatch(topical, ".help")
    assert topical.out[-1] == help_text(topical.theme)  # long form by default
    assert topical.text.lstrip("\n").startswith("*** Talker commands *")


def test_help_screen_still_fits_78_columns(topical):
    for style in ("long", "short"):
        for ln in help_text(T, style).split("\n"):
            assert len(ln) <= 78, ln


# ---- page rendering ------------------------------------------------------

def test_pages_wrap_at_78_and_keep_their_indentation():
    long_line = "  " + "word " * 40
    wrapped = wrap_page(long_line + "\n\nplain\n")
    lines = wrapped.split("\n")
    assert all(len(ln) <= 78 for ln in lines)
    assert all(ln.startswith("  ") for ln in lines if ln.strip() and ln != "plain")
    assert "" in lines                                # blank lines survive


def test_shipped_pages_render_inside_78_columns():
    for name, page in SHIPPED.items():
        for ln in wrap_page(page).split("\n"):
            assert len(ln) <= 78, (name, ln)


def test_page_is_one_unit(topical):
    dispatch(topical, ".help msgs")
    assert len(topical.out) == 1                      # blank-line separation applies


def test_help_reboot_renders_the_page(topical):
    dispatch(topical, ".help reboot")
    out = topical.text
    assert "in the way one documents a fire axe" in out
    assert r"C:\HOUSE\TALKER.EXE" in out          # rendered, not mangled
    assert "The owner thanks you for not rebooting the server." in out


def test_help_set_renders_every_setting_and_no_prose(topical):
    """The page that replaced the merged one: seven rows, nothing else."""
    dispatch(topical, ".help set")
    out = topical.text
    for name in ("dim", "time", "enter_msg", "exit_msg", "afk_msg", "name"):
        assert name in out, name
    assert "when you walk INTO a room" not in out      # the old prose is gone
    assert "nothing about where you went" not in out


# ---- where the pages are looked for (the production bug) -----------------
#
# help_dir defaulted to "help" resolved against the CONFIG's directory. A real
# deployment keeps code and prose in /opt/heresay and the config in
# /etc/heresay, so the daemon looked for /etc/heresay/help, found nothing, and
# served no topics at all — silently, because a missing help dir is not fatal.
# These pin the shape that broke it.

def _prod_shaped(tmp_path, **over):
    """Config in one directory, authored content in another — as in production."""
    etc, opt = tmp_path / "etc", tmp_path / "opt"
    (opt / "help").mkdir(parents=True)
    etc.mkdir()
    (opt / "help" / "tea.txt").write_text("All about tea.\n", encoding="utf-8")
    (opt / "help" / "cake.txt").write_text("All about cake.\n", encoding="utf-8")
    (opt / "agreement.txt").write_text("the rules\n", encoding="utf-8")
    cfg = Config(base_dir=etc, house_dir=str(opt), **over)
    return cfg, opt


def test_help_dir_defaults_under_house_dir_not_beside_the_config(tmp_path):
    cfg, opt = _prod_shaped(tmp_path)
    assert cfg.help_path() == opt / "help"
    assert _load_help_topics(cfg.help_path()) == {
        "cake": "All about cake.", "tea": "All about tea."}


def test_an_explicit_help_dir_still_wins(tmp_path):
    cfg, opt = _prod_shaped(tmp_path)
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    (elsewhere / "only.txt").write_text("just this\n", encoding="utf-8")
    cfg.help_dir = str(elsewhere)          # absolute: wins outright
    assert cfg.help_path() == elsewhere
    assert sorted(_load_help_topics(cfg.help_path())) == ["only"]


def test_the_old_resolution_is_what_found_nothing(tmp_path):
    """The regression itself, stated: resolving against the config's directory
    is what produced an empty topic set in production."""
    cfg, opt = _prod_shaped(tmp_path)
    assert _load_help_topics(cfg.resolve("help")) == {}         # the old way
    assert _load_help_topics(cfg.help_path()) != {}             # the new way


@pytest.mark.asyncio
async def test_startup_logs_the_count_and_the_path(tmp_path, caplog):
    """A quiet operational line — a count and a path, never a page's content —
    so 'no topics' can never again be invisible."""
    import logging
    from heresay.server import TalkerServer
    world = REPO / "tests" / "fixtures" / "house" / "world"
    etc, opt = tmp_path / "etc", tmp_path / "opt"
    (opt / "help").mkdir(parents=True)
    etc.mkdir()
    (opt / "help" / "tea.txt").write_text("All about tea.\n", encoding="utf-8")
    (opt / "agreement.txt").write_text("the rules\n", encoding="utf-8")
    cfg = Config(
        plain_host="127.0.0.1", plain_port=0, ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        house_dir=str(opt),
        world_dir=str(world),
        entry_room="atrium", jail_room="holding_cell",
        owner_name="Generic",
        base_dir=etc,
    )
    with caplog.at_level(logging.INFO, logger="heresay"):
        srv = TalkerServer(cfg)
    line = next(r.getMessage() for r in caplog.records
                if r.getMessage().startswith("help topics:"))
    assert line == f"help topics: 1 loaded from {opt / 'help'}"
    assert "All about tea" not in caplog.text        # a count and a path, no content
    assert srv.help_topics                            # and it really did load them
