# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Rate limiting — entirely in-memory, per-session and per-IP counters only.

Nothing here touches disk, and none of it ever records line content. The room
never sees the machinery; only the offending sender gets feedback.

All clocks are injected as an explicit ``now`` (monotonic seconds) so behaviour
is deterministic under test.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field


class TokenBucket:
    """Classic token bucket. ``capacity`` tokens, refilling ``rate`` per second."""

    def __init__(self, capacity: float, rate: float, now: float = 0.0):
        self.capacity = float(capacity)
        self.rate = float(rate)
        self.tokens = float(capacity)
        self.last = now

    def _refill(self, now: float) -> None:
        if now > self.last:
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.last = now

    def take(self, now: float, amount: float = 1.0) -> bool:
        """Consume ``amount`` tokens if available. Returns True on success."""
        self._refill(now)
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False


# Outcomes of a speech-governor check.
OK = "ok"
THROTTLED = "throttled"      # denied; tell the sender to slow down
MUTED = "muted"             # inside the server-side mute window; silently dropped
DISCONNECT = "disconnect"   # persistent flooding; drop the connection


@dataclass
class SpeechGovernor:
    """Shared throttle for say / emote / think / tell.

    Switching verbs does not evade it — every conversational line consumes one
    token from the same bucket. Sustained overflow escalates: throttle notice →
    short server-side mute → disconnect.
    """

    burst: int = 5
    refill_per_sec: float = 1.0
    mute_strikes: int = 5
    disconnect_strikes: int = 15
    mute_seconds: float = 10.0

    _bucket: TokenBucket = field(init=False)
    strikes: int = 0
    muted_until: float = 0.0

    def __post_init__(self):
        self._bucket = TokenBucket(self.burst, self.refill_per_sec)

    def check(self, now: float) -> str:
        # Already inside a server-side mute window.
        if now < self.muted_until:
            self.strikes += 1
            if self.strikes >= self.disconnect_strikes:
                return DISCONNECT
            return MUTED

        if self._bucket.take(now):
            # Sustained good behaviour heals: each accepted line sheds one strike,
            # so a long, lively-but-legitimate session can't slowly accumulate its
            # way to a flood-disconnect. A real flood rarely earns an OK (the
            # bucket stays empty), so this doesn't blunt the escalation.
            if self.strikes > 0:
                self.strikes -= 1
            return OK

        # Denied by the bucket.
        self.strikes += 1
        if self.strikes >= self.disconnect_strikes:
            return DISCONNECT
        if self.strikes >= self.mute_strikes:
            self.muted_until = now + self.mute_seconds
            return MUTED
        return THROTTLED


class ShoutLimiter:
    """A stingy separate bucket: one shout per ``interval`` seconds."""

    def __init__(self, interval: float = 30.0):
        self.interval = interval
        self.last_shout: float | None = None

    def allow(self, now: float) -> bool:
        if self.last_shout is None or (now - self.last_shout) >= self.interval:
            self.last_shout = now
            return True
        return False

    def retry_after(self, now: float) -> float:
        if self.last_shout is None:
            return 0.0
        return max(0.0, self.interval - (now - self.last_shout))


class ConnectionChurn:
    """Per-IP concurrency cap plus a brief refusal window on rapid reconnects."""

    def __init__(self, max_concurrent: int = 3, window: float = 10.0, max_in_window: int = 5):
        self.max_concurrent = max_concurrent
        self.window = window
        self.max_in_window = max_in_window
        self._active: dict[str, int] = {}
        self._recent: dict[str, deque[float]] = {}

    def _prune_all(self, now: float) -> None:
        """Drain every IP's expired window entries and forget any key that is both
        empty and inactive. Called on each connection attempt, so a stream of
        distinct (or forged) one-shot IPs can't grow the table without bound: the
        next connection from anyone sweeps the stale entries the previous ones
        left behind. (Per-IP pruning alone missed a one-shot IP that never
        reconnected — nothing re-touched its key.)"""
        for ip in list(self._recent):
            dq = self._recent[ip]
            while dq and (now - dq[0]) > self.window:
                dq.popleft()
            if not dq and self._active.get(ip, 0) <= 0:
                self._recent.pop(ip, None)

    def try_open(self, ip: str, now: float) -> tuple[bool, str]:
        """Attempt to register a new connection from ``ip``.

        Returns (allowed, reason). On success the caller must later call
        ``close(ip)`` exactly once.
        """
        self._prune_all(now)
        if self._active.get(ip, 0) >= self.max_concurrent:
            return False, "too many connections from your address"
        dq = self._recent.setdefault(ip, deque())
        if len(dq) >= self.max_in_window:
            return False, "reconnecting too fast — wait a moment"
        dq.append(now)
        self._active[ip] = self._active.get(ip, 0) + 1
        return True, ""

    def close(self, ip: str) -> None:
        n = self._active.get(ip, 0)
        if n <= 1:
            self._active.pop(ip, None)
        else:
            self._active[ip] = n - 1
        # If nothing is active and the recent-window has drained, forget the IP.
        dq = self._recent.get(ip)
        if self._active.get(ip, 0) <= 0 and not dq:
            self._recent.pop(ip, None)


class AuthBackoff:
    """Exponential backoff per IP on failed logins.

    Entries carry a last-seen time and are pruned once idle past ``ttl``.

    ⛔ **BOUNDED BY SIZE AS WELL AS BY TIME** (v1.52.1, external review). Every
    other per-IP table here prunes on a condition that a flood keeps satisfying —
    a fully-refilled bucket, an empty window — so none of them can grow. This one
    was bounded only by ``ttl``, which is a *time* bound: under a distributed
    login-guessing run over many distinct or forged source addresses, the table
    could hold an hour's worth of attacker IPs, and each new failure paid an
    O(n) sweep over all of them as it grew. `GlobalAuthCeiling` slows such a run
    but does not stop the failures being recorded.

    ⚠ An `OrderedDict` rather than the `min()` scan the obvious fix reaches for:
    eviction is O(1) that way, so the thing that fires under load is not itself
    a linear pass. The oldest-touched entry goes, which is the right one — it is
    the address that has waited longest without failing again, so it is the one
    whose backoff is least worth remembering.

    ⚠ Losing an entry costs an attacker's backoff, never a legitimate user's
    lockout: forgetting an IP RESETS its delay to zero, so the failure mode of
    the cap is leniency toward whoever is evicted. That is the safe direction —
    the alternative would be inheriting a stranger's penalty.
    """

    def __init__(self, base: float = 1.0, cap: float = 30.0, ttl: float = 3600.0,
                 max_entries: int = 8192):
        self.base = base
        self.cap = cap
        self.ttl = ttl
        self.max_entries = max_entries
        # ip -> (count, last_time), in least-recently-touched order.
        self._fails: "OrderedDict[str, tuple[int, float]]" = OrderedDict()

    def delay(self, ip: str) -> float:
        entry = self._fails.get(ip)
        if not entry:
            return 0.0
        n, _ = entry
        if n == 0:
            return 0.0
        return min(self.cap, self.base * (2 ** (n - 1)))

    def record_failure(self, ip: str, now: float = 0.0) -> None:
        n = self._fails.get(ip, (0, 0.0))[0] + 1
        self._fails[ip] = (n, now)
        self._fails.move_to_end(ip)        # most recently touched goes last
        self._prune(now)
        # The size bound, after the time one: a flood of distinct addresses can
        # arrive faster than `ttl` retires them, and this is what makes the
        # table's ceiling a fact about the table rather than about the clock.
        while len(self._fails) > self.max_entries:
            self._fails.popitem(last=False)   # evict the oldest-touched, O(1)

    def record_success(self, ip: str) -> None:
        self._fails.pop(ip, None)

    def _prune(self, now: float) -> None:
        stale = [ip for ip, (_, t) in self._fails.items() if now - t > self.ttl]
        for ip in stale:
            self._fails.pop(ip, None)


class GlobalAuthCeiling:
    """A server-WIDE failed-auth rate limit, counting every failed login and every
    failed recovery attempt across ALL sources.

    Per-IP backoff can't see a botnet spread over many IPs; this outer layer can.
    When failures over a rolling ``window`` exceed ``threshold``, it enters a
    ``cooldown`` during which the server slows new auth attempts and pauses new-
    account creation. It NEVER touches already-authenticated sessions. It is a
    deliberate self-DoS tradeoff (an attacker can make the door grumpy for
    everyone), so it is switchable off and generously tuned.

    All times are the injected monotonic ``now`` (seconds).
    """

    def __init__(self, window: float = 60.0, threshold: int = 50,
                 cooldown: float = 120.0, enabled: bool = True):
        self.window = window
        self.threshold = threshold
        self.cooldown = cooldown
        self.enabled = enabled
        self._fails: deque[float] = deque()
        self._cooldown_until = 0.0

    def _prune(self, now: float) -> None:
        while self._fails and (now - self._fails[0]) > self.window:
            self._fails.popleft()

    def note_failure(self, now: float) -> bool:
        """Record one failed auth. Returns True the moment this crosses the
        threshold into a fresh cooldown (so the caller logs it once)."""
        if not self.enabled:
            return False
        self._fails.append(now)
        self._prune(now)
        if now >= self._cooldown_until and len(self._fails) >= self.threshold:
            self._cooldown_until = now + self.cooldown
            return True
        return False

    def in_cooldown(self, now: float) -> bool:
        return self.enabled and now < self._cooldown_until

    def note_cleared(self, now: float) -> bool:
        """Returns True exactly once, the first time a cooldown lapses (so the
        caller logs the all-clear). Safe to call on every auth attempt."""
        if self._cooldown_until and now >= self._cooldown_until:
            self._cooldown_until = 0.0
            return True
        return False


class PerIPRateLimiter:
    """Per-IP token buckets (e.g. account-registration flood control).

    A bucket is created lazily per IP; buckets that refill fully (i.e. the IP has
    gone idle) are dropped, so the table only holds addresses acting right now —
    it can't be grown without bound by rotating source IPs.
    """

    def __init__(self, burst: float, refill_per_sec: float):
        self.burst = float(burst)
        self.refill = float(refill_per_sec)
        self._buckets: dict[str, TokenBucket] = {}

    def allow(self, ip: str, now: float, amount: float = 1.0) -> bool:
        b = self._buckets.get(ip)
        if b is None:
            b = TokenBucket(self.burst, self.refill, now)
            self._buckets[ip] = b
        ok = b.take(now, amount)
        self._prune(now)
        return ok

    def _prune(self, now: float) -> None:
        for ip in list(self._buckets):
            b = self._buckets[ip]
            b._refill(now)
            if b.tokens >= b.capacity:      # fully refilled => idle => forget it
                del self._buckets[ip]
