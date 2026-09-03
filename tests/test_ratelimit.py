# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

from heresay import ratelimit
from heresay.ratelimit import (
    AuthBackoff,
    ConnectionChurn,
    ShoutLimiter,
    SpeechGovernor,
    TokenBucket,
)


def test_token_bucket_burst_then_empty():
    b = TokenBucket(capacity=5, rate=1.0, now=0.0)
    assert all(b.take(0.0) for _ in range(5))  # burst of 5 at t=0
    assert not b.take(0.0)                      # 6th denied


def test_token_bucket_refills_one_per_second():
    b = TokenBucket(capacity=5, rate=1.0, now=0.0)
    for _ in range(5):
        b.take(0.0)
    assert not b.take(0.5)   # not enough time yet
    assert b.take(1.0)       # one token back after 1s
    assert not b.take(1.0)   # and only one


def test_speech_governor_escalates_burst_mute_disconnect():
    g = SpeechGovernor(burst=5, refill_per_sec=1.0, mute_strikes=5,
                       disconnect_strikes=15, mute_seconds=10.0)
    # First 5 lines at t=0 pass (burst).
    assert [g.check(0.0) for _ in range(5)] == [ratelimit.OK] * 5
    # Next lines are throttled until strikes reach the mute threshold.
    outcomes = [g.check(0.0) for _ in range(5)]
    assert ratelimit.THROTTLED in outcomes
    assert outcomes[-1] == ratelimit.MUTED  # 5th strike opens the mute window
    # While muted, further attempts stay muted...
    assert g.check(0.0) == ratelimit.MUTED
    # ...and sustained flooding eventually forces a disconnect.
    result = None
    for _ in range(20):
        result = g.check(0.0)
        if result == ratelimit.DISCONNECT:
            break
    assert result == ratelimit.DISCONNECT


def test_speech_governor_lets_a_normal_talker_through():
    g = SpeechGovernor(burst=5, refill_per_sec=1.0, mute_strikes=5,
                       disconnect_strikes=15, mute_seconds=10.0)
    # One line every ~2 seconds for a while — never throttled.
    t = 0.0
    for _ in range(20):
        assert g.check(t) == ratelimit.OK
        t += 2.0


def test_shout_limiter():
    s = ShoutLimiter(interval=30.0)
    assert s.allow(0.0)
    assert not s.allow(5.0)
    assert not s.allow(29.9)
    assert s.allow(30.0)


def test_connection_churn_concurrency_cap():
    c = ConnectionChurn(max_concurrent=3, window=10.0, max_in_window=5)
    ip = "1.2.3.4"
    assert all(c.try_open(ip, 0.0)[0] for _ in range(3))
    ok, reason = c.try_open(ip, 0.0)
    assert not ok and "connections" in reason
    c.close(ip)
    assert c.try_open(ip, 0.0)[0]  # a slot freed up


def test_connection_churn_rapid_reconnect_window():
    c = ConnectionChurn(max_concurrent=10, window=10.0, max_in_window=5)
    ip = "5.6.7.8"
    for i in range(5):
        assert c.try_open(ip, 0.0)[0]
        c.close(ip)
    ok, reason = c.try_open(ip, 1.0)
    assert not ok and "fast" in reason
    # After the window passes, reconnects are allowed again.
    assert c.try_open(ip, 11.1)[0]


def test_auth_backoff_exponential():
    a = AuthBackoff(base=1.0, cap=30.0)
    ip = "9.9.9.9"
    assert a.delay(ip) == 0.0
    a.record_failure(ip)
    assert a.delay(ip) == 1.0
    a.record_failure(ip)
    assert a.delay(ip) == 2.0
    a.record_failure(ip)
    assert a.delay(ip) == 4.0
    for _ in range(10):
        a.record_failure(ip)
    assert a.delay(ip) == 30.0  # capped
    a.record_success(ip)
    assert a.delay(ip) == 0.0   # reset on success


# ---------------------------------------------------------------------------
# ⛔ AuthBackoff is bounded by SIZE as well as by time (v1.52.1, ext. review)
# ---------------------------------------------------------------------------

def test_auth_backoff_is_bounded_by_SIZE_not_only_by_ttl():
    """⛔ The one per-IP table that was bounded only by a clock.

    Every other table here prunes on a condition a flood keeps satisfying — a
    refilled bucket, an empty window — so none can grow. This one relied on
    `ttl`, and under a distributed run over many distinct or forged addresses it
    could hold an hour's worth of them, each new failure paying an O(n) sweep as
    it grew.

    ⚠ All the failures share ONE `now`, so `_prune` retires nothing: this is the
    size bound alone doing the work, which is the point.
    """
    a = AuthBackoff(max_entries=64, ttl=3600.0)
    for i in range(1000):
        a.record_failure(f"10.0.{i // 256}.{i % 256}", now=1000.0)
    assert len(a._fails) <= 64


def test_the_cap_evicts_the_OLDEST_TOUCHED_and_keeps_the_newest():
    """⚠ Which entry goes matters. The oldest-touched is the address that has
    waited longest without failing again — the one whose backoff is least worth
    remembering — and evicting it is O(1) on an OrderedDict rather than the
    linear `min()` scan the obvious fix reaches for.

    ⚠ And note the direction of the failure: forgetting an IP RESETS its delay,
    so the cost of eviction is leniency toward whoever is evicted, never a
    stranger's penalty inherited by somebody else.
    """
    a = AuthBackoff(max_entries=3)
    for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
        a.record_failure(ip, now=1.0)
    a.record_failure("4.4.4.4", now=1.0)          # over the cap
    assert "1.1.1.1" not in a._fails               # the oldest went
    assert a.delay("1.1.1.1") == 0.0               # ...and its penalty with it
    for ip in ("2.2.2.2", "3.3.3.3", "4.4.4.4"):
        assert ip in a._fails


def test_touching_an_ip_again_moves_it_out_of_eviction_range():
    """An address that keeps failing must not be evicted ahead of one that has
    gone quiet — otherwise the cap would forget exactly the attacker it is
    meant to be slowing."""
    a = AuthBackoff(max_entries=3)
    for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
        a.record_failure(ip, now=1.0)
    a.record_failure("1.1.1.1", now=2.0)          # touched again -> now newest
    a.record_failure("4.4.4.4", now=2.0)          # evicts the oldest: 2.2.2.2
    assert "1.1.1.1" in a._fails
    assert "2.2.2.2" not in a._fails


def test_the_size_cap_does_not_disturb_the_ttl_prune_or_the_delays():
    """Existing behaviour, re-asserted beside the new bound."""
    a = AuthBackoff(base=1.0, cap=30.0, ttl=3600.0, max_entries=8192)
    a.record_failure("9.9.9.9", now=0.0)
    assert a.delay("9.9.9.9") == 1.0
    a.record_failure("8.8.8.8", now=10_000.0)     # past ttl -> the old one goes
    assert "9.9.9.9" not in a._fails
    assert "8.8.8.8" in a._fails
    assert AuthBackoff().max_entries == 8192      # the shipped default
