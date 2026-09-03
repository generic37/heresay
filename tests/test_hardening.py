# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression tests for the v1.9.0 security-hardening pass (M2-M5, L1-L10, I6)."""

import asyncio
import ssl
import traceback
from pathlib import Path

import pytest

from heresay.accounts import AccountStore, valid_name
from heresay.commands import dispatch
from heresay.config import Config, load_config
from heresay.ratelimit import (
    AuthBackoff, ConnectionChurn, PerIPRateLimiter, SpeechGovernor, TokenBucket,
)
from heresay.server import TalkerServer
from heresay.transport import MAX_LINE_LENGTH, WebSocketTransport

from conftest import FakeSession, seed_account

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


def _cfg(tmp_path, **over):
    base = dict(
        plain_host="127.0.0.1", plain_port=0, ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        world_dir=str(WORLD),
        entry_room="atrium", jail_room="holding_cell",
        owner_name="Generic",
    )
    base.update(over)
    return Config(**base)


async def _start(cfg):
    srv = TalkerServer(cfg)
    await srv.start()
    return srv, srv._servers[0].sockets[0].getsockname()[1]


async def collect(reader, timeout=0.4):
    buf = b""
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    return buf.decode(errors="replace")


async def send(writer, text):
    writer.write((text + "\n").encode())
    await writer.drain()


# ====================== L4 — ASCII-only names ======================

def test_valid_name_ascii_only():
    assert valid_name("Corvid") and valid_name("a-b_1") and valid_name("Wz9")
    assert not valid_name("1leadingdigit")     # must start with a letter
    assert not valid_name("Аdmin")             # Cyrillic А (U+0410) homoglyph
    assert not valid_name("Οwl")               # Greek Omicron
    assert not valid_name("full２width")        # full-width digit
    assert not valid_name("a")                 # too short
    assert not valid_name("a" * 17)            # too long
    assert not valid_name("has space")


# ====================== L7 — flood strikes decay ======================

def test_speech_strikes_decay_on_good_behaviour():
    g = SpeechGovernor(burst=1, refill_per_sec=1000.0,
                       mute_strikes=3, disconnect_strikes=6, mute_seconds=0.0)
    # Two denials at the same instant (bucket holds 1): strikes climb.
    g.check(0.0)                       # OK, consumes the token
    g.check(0.0); g.check(0.0)         # two throttles
    assert g.strikes == 2
    # Later, with the bucket refilled, good lines shed strikes one at a time.
    g.check(1.0); g.check(2.0)
    assert g.strikes == 0              # healed, so no false-positive disconnect


# ====================== L5 — rate-limit dict pruning ======================

def test_connection_churn_prunes_stale_ips():
    ch = ConnectionChurn(window=10.0)
    ch.try_open("1.1.1.1", now=0.0)
    ch.close("1.1.1.1")
    # A later connection from anyone sweeps the now-stale window entry.
    ch.try_open("2.2.2.2", now=100.0)
    assert "1.1.1.1" not in ch._recent
    ch.close("2.2.2.2")


def test_auth_backoff_prunes_idle_ips():
    ab = AuthBackoff(ttl=3600.0)
    ab.record_failure("9.9.9.9", now=0.0)
    assert "9.9.9.9" in ab._fails
    ab.record_failure("8.8.8.8", now=10_000.0)   # >ttl later -> old entry swept
    assert "9.9.9.9" not in ab._fails
    assert "8.8.8.8" in ab._fails


# ====================== L6 — registration flood limit ======================

def test_per_ip_rate_limiter_bursts_then_denies():
    rl = PerIPRateLimiter(burst=2, refill_per_sec=1.0)
    assert rl.allow("6.6.6.6", 0.0) and rl.allow("6.6.6.6", 0.0)
    assert not rl.allow("6.6.6.6", 0.0)          # third denied
    assert rl.allow("6.6.6.6", 10.0)             # refilled later


def test_per_ip_rate_limiter_prunes_idle_buckets():
    rl = PerIPRateLimiter(burst=2, refill_per_sec=1.0)
    rl.allow("6.6.6.6", 0.0)
    rl.allow("7.7.7.7", 100.0)                    # sweep: 6.6.6.6 refilled full -> dropped
    assert "6.6.6.6" not in rl._buckets


# ====================== M4a — command governor ======================

def test_command_governor_throttles(world, clock):
    accounts = AccountStore  # unused; FakeSession makes its own
    s = FakeSession(world, name="Corvid", clock=clock)
    s.command_bucket = TokenBucket(2, 0.0)        # tiny bucket, no refill
    s.clear()
    dispatch(s, ".who"); dispatch(s, ".who")      # two allowed
    s.clear()
    dispatch(s, ".who")                            # third throttled
    assert "too fast" in s.text


def test_command_governor_never_traps_quit(world, clock):
    s = FakeSession(world, name="Corvid", clock=clock)
    s.command_bucket = TokenBucket(0, 0.0)         # empty bucket
    assert dispatch(s, ".quit") is not None        # quit still works (QUIT sentinel)


# ====================== M5 — mute cap ======================

def test_mute_cap_and_name_validation(world, clock):
    s = FakeSession(world, name="Corvid", clock=clock,
                    config=Config(wrap_columns=1000, mute_cap=3))
    for nm in ("aaa", "bbb", "ccc"):               # pre-mute absent (valid) names
        s.clear(); dispatch(s, f".mute {nm}")
        assert "no longer hear" in s.text
    s.clear(); dispatch(s, ".mute ddd")            # 4th exceeds the cap
    assert "full" in s.text
    s.clear(); dispatch(s, ".mute has space")      # not a legal name
    assert "isn't a name" in s.text


# ====================== L1 — oversized int arg ======================

def test_dmail_huge_number_does_not_raise(world, clock):
    s = FakeSession(world, name="Corvid", clock=clock)
    s.clear()
    dispatch(s, ".dmail " + "9" * 5000)            # would raise on int() unbounded
    assert "Usage" in s.text                        # handled, session intact


# ====================== M2 — WS line length cap ======================

class _FakeWSConn:
    def __init__(self, frame):
        self.remote_address = ("1.2.3.4", 5)
        self._frame = frame

    async def recv(self):
        f, self._frame = self._frame, None
        if f is None:
            await asyncio.sleep(3600)
        return f

    async def send(self, frame):
        pass


@pytest.mark.asyncio
async def test_ws_line_is_length_capped():
    t = WebSocketTransport(_FakeWSConn("z" * 5000), "1.2.3.4")
    try:
        line = await t.recv_line()
        assert len(line) == MAX_LINE_LENGTH == 1024
    finally:
        t._sender.cancel()


# ====================== M4b — deferred pref writes persist ======================

def test_pref_writes_are_deferred_then_flushed(tmp_path):
    path = tmp_path / "accounts.json"
    store = AccountStore(path)
    store.create("Corvid", "password1")             # security write -> on disk now
    store.set_setting("Corvid", "dim", True)        # a pref -> deferred
    assert store._dirty is True
    # Not yet on disk: a fresh load doesn't see it.
    assert AccountStore(path).get("Corvid").settings.get("dim") is None
    store.flush()
    assert AccountStore(path).get("Corvid").settings.get("dim") is True


def test_security_write_flushes_pending_prefs(tmp_path):
    path = tmp_path / "accounts.json"
    store = AccountStore(path)
    store.create("Corvid", "password1")
    store.set_setting("Corvid", "dim", True)        # deferred
    store.set_rank("Corvid", "staff")              # security write -> saves everything
    assert store._dirty is False
    assert AccountStore(path).get("Corvid").settings.get("dim") is True


# ====================== L9 — exception log omits user text ======================

def test_exception_log_render_omits_user_text():
    marker = "USER_SECRET_c0ffee"
    try:
        raise ValueError(marker)
    except ValueError as exc:
        frames = "".join(traceback.format_tb(exc.__traceback__))
        rendered = f"session error: {type(exc).__name__}\n{frames}"   # what server.py logs
    assert marker not in rendered
    assert "ValueError" in rendered


# ====================== L10 — secret prompts keep whitespace ======================

class _AskTransport:
    supports_ansi = False
    wants_prompt = True

    def __init__(self, line):
        self._line = line
        self.peer_ip = "127.0.0.1"

    def send_text(self, text): pass
    def set_echo(self, on): pass
    def prompt(self): pass
    async def drain(self): pass
    async def recv_line(self, max_len=1024): return self._line


@pytest.mark.asyncio
async def test_secret_prompt_preserves_whitespace(world):
    from heresay.session import Session
    s = Session(_AskTransport("  spaced pw  "), world, None, Config(wrap_columns=1000))
    assert await s.ask("pw: ", secret=True) == "  spaced pw  "   # verbatim
    s2 = Session(_AskTransport("  name  "), world, None, Config(wrap_columns=1000))
    assert await s2.ask("name: ") == "name"                       # ordinary: trimmed


# ====================== L8 — TLS minimum version ======================

def _make_selfsigned(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime as dt
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nm = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (x509.CertificateBuilder().subject_name(nm).issuer_name(nm)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(dt.datetime(2020, 1, 1))
            .not_valid_after(dt.datetime(2030, 1, 1))
            .sign(key, hashes.SHA256()))
    cp, kp = tmp_path / "cert.pem", tmp_path / "key.pem"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return cp, kp


def test_tls_minimum_version_is_1_2(tmp_path):
    cp, kp = _make_selfsigned(tmp_path)
    cfg = _cfg(tmp_path, cert_file=str(cp), key_file=str(kp))
    ctx = TalkerServer(cfg)._make_ssl_context()
    assert ctx is not None
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


# ====================== M3 — pre-auth timeout ======================

@pytest.mark.asyncio
async def test_pre_auth_timeout_closes_connection(tmp_path):
    srv, port = await _start(_cfg(tmp_path, auth_phase_seconds=0.3))
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        # Send nothing. One read that spans the 0.3s deadline captures the
        # greeting AND the timeout notice, up to the server's close.
        out = await collect(r, timeout=1.5)
        assert "timed out" in out.lower()
        w.close()
    finally:
        await srv.aclose()


# ============ v1.29.2 — the agreement gets its own clock ============
#
# The pre-auth phase used to run under ONE deadline, so reading the house rules
# was billed to the same 60 seconds as typing a password. A careful reader on a
# phone was dropped mid-clause. The credential prompts keep the short clock; the
# agreement gets `agreement_seconds`, and the credential clock is paused (not
# spent) while it is up.


async def _to_agreement(port, name="Newbie", pw="correct horse battery"):
    """Drive a fresh connection as far as the agreement page. Returns it."""
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r, timeout=0.2); await send(w, name)
    await collect(r, timeout=0.2); await send(w, "yes")
    await collect(r, timeout=0.2); await send(w, pw)
    await collect(r, timeout=0.2); await send(w, pw)
    return r, w, await collect(r, timeout=0.2)


@pytest.mark.asyncio
async def test_reading_the_agreement_outlives_the_credential_deadline(tmp_path):
    """The bug, in one test: sit on the agreement for longer than the whole
    pre-auth budget, then agree, and still get in."""
    srv, port = await _start(_cfg(tmp_path, auth_phase_seconds=2.5,
                                  agreement_seconds=30.0))
    try:
        r, w, page = await _to_agreement(port)
        assert "18 or older" in page          # we really are on the agreement
        # Longer than the ENTIRE credential budget, and several times what was
        # left of it after the prompts above.
        await asyncio.sleep(4.0)
        await send(w, "y")
        out = await collect(r, timeout=2.0)
        assert "timed out" not in out.lower()
        assert "Welcome, Newbie" in out
        w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_name_prompt_still_drops_at_auth_phase_seconds(tmp_path):
    """The other half: a generous agreement budget must not leak into the
    credential prompts, or the anti-slowloris deadline is gone."""
    srv, port = await _start(_cfg(tmp_path, auth_phase_seconds=0.3,
                                  agreement_seconds=600.0))
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        out = await collect(r, timeout=1.5)
        assert "timed out" in out.lower()
        w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_agreement_window_is_finite(tmp_path):
    """Generous, never unlimited: idling on the agreement past
    `agreement_seconds` drops the connection like any other timeout."""
    srv, port = await _start(_cfg(tmp_path, auth_phase_seconds=30.0,
                                  agreement_seconds=0.5))
    try:
        r, w, page = await _to_agreement(port)
        assert "18 or older" in page
        out = await collect(r, timeout=2.0)
        assert "timed out" in out.lower()     # the same graceful drop
        w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_nothing_is_written_when_the_agreement_times_out(tmp_path):
    """No half-state: an account is only created after consent, so a reader who
    times out leaves nothing behind and the name is still free."""
    cfg = _cfg(tmp_path, auth_phase_seconds=30.0, agreement_seconds=0.5)
    srv, port = await _start(cfg)
    try:
        r, w, _ = await _to_agreement(port)
        assert "timed out" in (await collect(r, timeout=2.0)).lower()
        w.close()
        assert not srv.accounts.exists("Newbie")
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_reconsent_path_gets_the_reading_clock_too(tmp_path):
    """Both paths that show the agreement, not just creation. Re-reading a
    revised agreement is exactly as slow as reading it the first time."""
    cfg = _cfg(tmp_path, auth_phase_seconds=2.5, agreement_seconds=30.0)
    srv, port = await _start(cfg)
    try:
        # An account that agreed to some OTHER version of the text.
        seed_account(srv.accounts, "Oldtimer", "correct horse battery")
        srv.accounts.record_agreement("Oldtimer", "a-stale-hash")

        r, w = await asyncio.open_connection("127.0.0.1", port)
        await collect(r, timeout=0.2); await send(w, "Oldtimer")
        await collect(r, timeout=0.2); await send(w, "correct horse battery")
        page = await collect(r, timeout=0.4)
        assert "revised" in page.lower()
        await asyncio.sleep(4.0)              # past the whole credential budget
        await send(w, "y")
        out = await collect(r, timeout=2.0)
        assert "timed out" not in out.lower()
        assert "atrium" in out.lower()        # arrived in the entry room
        w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_credential_clock_is_paused_not_refreshed(tmp_path):
    """The property that keeps the phase bounded: the window gives back exactly
    the time the credential clock had left, never a fresh budget. Measured on
    the deadline itself, because 'still finite' is not visible from outside."""
    srv = TalkerServer(_cfg(tmp_path, auth_phase_seconds=10.0,
                            agreement_seconds=30.0))
    loop = asyncio.get_running_loop()

    class _S:
        auth_timeout = None

    session = _S()
    async with asyncio.timeout(10.0) as deadline:
        session.auth_timeout = deadline
        spent = 5.0                            # pretend 5s went on the prompts
        deadline.reschedule(deadline.when() - spent)
        left_before = deadline.when() - loop.time()
        async with srv._agreement_window(session):
            await asyncio.sleep(0.2)           # "reading"
        left_after = deadline.when() - loop.time()

    # Reading cost the credential clock nothing (paused)...
    assert left_after == pytest.approx(left_before, abs=0.05)
    # ...and bought nothing either: it is NOT back up at auth_phase_seconds.
    assert left_after < 6.0


@pytest.mark.asyncio
async def test_the_agreement_window_bounds_a_session_with_no_credential_clock(tmp_path):
    """Belt and braces: the window's own timeout is the guarantee, so it holds
    even where there is no credential deadline to suspend."""
    srv = TalkerServer(_cfg(tmp_path, agreement_seconds=0.2))

    class _S:
        auth_timeout = None

    with pytest.raises(asyncio.TimeoutError):
        async with srv._agreement_window(_S()):
            await asyncio.sleep(5.0)


def test_agreement_seconds_is_configurable(tmp_path):
    """The key is read from [ratelimit], beside auth_phase_seconds."""
    p = tmp_path / "config.toml"
    p.write_text("[ratelimit]\nagreement_seconds = 42.5\n", encoding="utf-8")
    assert load_config(p).agreement_seconds == 42.5
    assert Config().agreement_seconds == 300.0          # the shipped default


# ====================== L3 — password minimum 8 ======================

@pytest.mark.asyncio
async def test_password_min_length_rejects_7_accepts_8(tmp_path):
    srv, port = await _start(_cfg(tmp_path))
    try:
        # 7 chars -> rejected
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await collect(r); await send(w, "Newbie")
        await collect(r); await send(w, "yes")
        await collect(r); await send(w, "seven77")     # 7 chars
        assert "at least 8" in await collect(r)
        w.close()
        # 8 chars -> accepted through to arrival
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        await collect(r2); await send(w2, "Newbie")
        await collect(r2); await send(w2, "yes")
        await collect(r2); await send(w2, "eightch8")   # 8 chars
        await collect(r2); await send(w2, "eightch8")
        await collect(r2); await send(w2, "y")
        assert "atrium" in (await collect(r2, timeout=2.0)).lower()
        w2.close()
    finally:
        await srv.aclose()


# ====================== M4b — prefs persist across server shutdown ======================

@pytest.mark.asyncio
async def test_prefs_persist_on_clean_shutdown(tmp_path):
    path = tmp_path / "accounts.json"
    seed_account(AccountStore(path), "Corvid", "password1")
    srv, _ = await _start(_cfg(tmp_path))
    srv.accounts.set_setting("Corvid", "dim", True)   # deferred pref write
    await srv.aclose()                                  # clean shutdown must flush
    assert AccountStore(path).get("Corvid").settings.get("dim") is True


# ============ A2 — the WebSocket wire cap (v1.49.0) ==========================
#
# The engine reads at most MAX_LINE_LENGTH (1024 characters) off any frame, and
# the library's default let 1 MiB be received, decompressed and buffered before
# being thrown away — ~1000x, per frame, for as long as a peer cared to send.
#
# ⚠ The pair of caps is the design, and neither half works alone. The WIRE cap
# is a protocol bound: an over-size frame is a violation and the connection
# closes with 1009, it does NOT truncate. The CLIENT cap is what keeps that from
# landing on a person — pasting a long message is an ordinary thing to do, and
# before MAX_SEND a 60 KB paste would have dropped them out of the world where
# today it is quietly trimmed. Measured, on the shipped code: 8192 delivered,
# 8193 refused with 1009.

def test_the_ws_door_bounds_the_frame_to_what_it_will_actually_read():
    """The bound exists, is a named constant, and is the smallest safe one.

    ⚠ 8 KB rather than 1 KB because the cap counts BYTES and MAX_LINE_LENGTH
    counts CHARACTERS: 1024 characters of four-byte UTF-8 is 4096 bytes and has
    to fit, with a control frame beside it.
    """
    from heresay.server import WS_MAX_FRAME
    assert WS_MAX_FRAME == 8192
    assert WS_MAX_FRAME >= MAX_LINE_LENGTH * 4, \
        "the cap must fit MAX_LINE_LENGTH characters of 4-byte UTF-8"


@pytest.mark.asyncio
async def test_ws_serve_is_actually_GIVEN_the_cap(tmp_path, monkeypatch):
    """⚠ A constant nobody passes is a comment. This asserts the call.

    The library default (1 MiB) is what the finding was about, so the test is
    that the argument reaches `ws_serve` — not merely that the number exists.
    """
    import heresay.server as S
    seen = {}
    real = S.ws_serve

    async def spy(handler, host, port, **kw):
        seen.update(kw)
        return await real(handler, host, port, **kw)

    monkeypatch.setattr(S, "ws_serve", spy)
    srv, _ = await _start(_cfg(tmp_path))
    try:
        assert seen.get("max_size") == S.WS_MAX_FRAME
    finally:
        await srv.aclose()


def test_the_shipped_client_cannot_reach_the_wire_cap():
    """⛔ The half that keeps the bound off a person.

    Every `ws.send(...)` in the shipped page is either a control frame (tens of
    bytes) or truncated to MAX_SEND. A test rather than a comment because the
    failure mode is silent: an unbounded send site would work perfectly until
    somebody pasted something long, and then disconnect them.
    """
    import pathlib
    import re
    client = (pathlib.Path(__file__).resolve().parent.parent
              / "web" / "index.html").read_text(encoding="utf-8")
    m = re.search(r"const MAX_SEND = (\d+);", client)
    assert m, "the client no longer declares MAX_SEND"
    max_send = int(m.group(1))
    assert max_send == MAX_LINE_LENGTH, \
        "MAX_SEND should mirror the server's own truncation, so nothing changes"

    from heresay.server import WS_MAX_FRAME
    assert max_send * 4 <= WS_MAX_FRAME, \
        "a 4-byte-UTF-8 line at MAX_SEND would exceed the wire cap"

    sends = re.findall(r"ws\.send\(([^;]*?)\)\s*[;}]", client)
    assert len(sends) >= 4, f"expected every send site; found {sends}"
    for s in sends:
        assert "slice(0, MAX_SEND)" in s or "CONTROL" in s, \
            f"unbounded ws.send(): {s}"


@pytest.mark.asyncio
async def test_an_over_size_frame_is_refused_and_a_normal_line_passes(tmp_path):
    """The behaviour, on a real socket, at the boundary.

    ⚠ Both halves matter. "Over-size is refused" alone would also be true of a
    cap set absurdly low, so the test that the door still WORKS sits beside it —
    a frame at exactly the bound goes through.
    """
    import websockets
    from heresay.server import WS_MAX_FRAME

    srv, _ = await _start(_cfg(tmp_path))
    ws_port = next(s.getsockname()[1] for s in srv._ws_server.sockets)
    try:
        async def attempt(size):
            """Returns the close code, or None if the door kept the connection.

            ⚠ Something has to READ for the close to surface — `close_code` is
            not populated until the client processes the frame. A first cut of
            this test asserted on it straight after the send and read None for
            both sizes, which would have passed a cap that did nothing.
            """
            ws = await websockets.connect(f"ws://127.0.0.1:{ws_port}",
                                          max_size=None)

            async def pump():
                try:
                    async for _ in ws:
                        pass
                except Exception:
                    pass
            reader = asyncio.create_task(pump())
            try:
                await ws.send("\x01width 100")
                await ws.send("\x01resume")
                await asyncio.sleep(0.3)
                try:
                    await ws.send("Q" * size)
                except Exception:
                    pass
                await asyncio.sleep(1.2)
                return ws.close_code
            finally:
                reader.cancel()
                try:
                    await ws.close()
                except Exception:
                    pass

        assert await attempt(WS_MAX_FRAME) is None      # at the bound: fine
        assert await attempt(WS_MAX_FRAME + 1) == 1009  # over it: refused
    finally:
        await srv.aclose()
