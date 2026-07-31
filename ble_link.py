"""BLE transport for EMFMon battles.

Replaces the old ESP-NOW discovery, which couldn't find peers in the field (its
channel follows each badge's WiFi association). BLE advertising/scanning uses
fixed advertising channels every device listens on, so discovery works
regardless of WiFi. aioble is frozen into the tildagon build.

DISCOVERY (stage 1): while searching, each badge ADVERTISES `EMFMon:<name>`
(non-connectable) and SCANS for other `EMFMon:` advertisers. peer_list()
returns the nearby badges sorted closest-first (strongest RSSI). Stale peers
age out.

HANDSHAKE (stage 2): connecting a chosen peer. Discovery stays NON-connectable
(that keeps the watchdog fix intact - see below). Only after the user picks an
opponent do we briefly go connectable, targeted at that one peer:

  - CHALLENGER stops discovery, starts a CONNECTABLE advert whose manufacturer
    data tags it as an EMFMon invite for the target's address + carries the
    challenger's name, and hosts the battle GATT service. It becomes the
    PERIPHERAL. (start_evo_invite)
  - The target, still scanning in discovery, sees the invite addressed to it
    (pending_invite()) and can Accept -> it stops discovery, connects to the
    challenger as CENTRAL. (start_evo_accept)

From there it is BATTLE_EVO! only: version frame, mon exchange, a timed
selection phase and commit-reveal, over one short-lived connection (plan 5).
battle.py derives the shared seed and both sides simulate independently.

THE CLASSIC HANDSHAKE IS GONE. `start_invite`, `start_accept`, their two tasks
and the `Handshake` handle were removed on 2026-08-01: 7.6 KB, 18% of this
module, with ZERO callers in the app or the harness. Rev 22 took Classic off the
badge entirely ("no fallback, no dual pipeline, no frozen legacy path" - plan 2)
and plan 5.1 is explicit that this build must never attempt the legacy exchange,
so the transport for it had been dead since then and was still being read from
flash and compiled on every session. CLASSIC lives in the v1.0.22 worktree
(plan 15.3, 18.5), which is a real shipped peer rather than this leftover.

WATCHDOG-SAFE DESIGN (fix for a 5s task-WDT reboot that hit 1-6 min into camping
on the search screen). Three discovery rules:
  1. Advertise NON-CONNECTABLE + set-and-forget (re-armed slowly). A connectable
     camp advert invites real inbound connections in a dense field, each burning
     one of only CONFIG_BT_CTRL_BLE_MAX_ACT=6 BLE activities + churning the
     controller. The stage-2 connect window is short + targeted, so it doesn't
     reintroduce that churn.
  2. Scan PASSIVE (active=False). `EMFMon:` name + F00D UUID + the invite
     manufacturer data all fit the 24/31-byte PRIMARY adv payload, so a passive
     scan sees them without scan-responses.
  3. DUTY-CYCLE the scan (3s burst / 4s idle) so the FreeRTOS idle task the WDT
     watches always runs, even alongside the app's 60fps draw loop.
"""

import asyncio
import struct

import aioble

try:  # MicroPython on the badge
    from time import ticks_diff as _ticks_diff
    from time import ticks_ms as _ticks_ms
except ImportError:  # CPython (unit tests)
    import time as _t

    def _ticks_ms():
        return int(_t.monotonic() * 1000)

    def _ticks_diff(a, b):
        return a - b

# EMFMon badges are identified TWO ways: a name that starts with NAME_PREFIX
# (for the on-screen name), AND a dedicated 16-bit service UUID (the robust
# "same app" marker, and the one a phone BLE tool can advertise for testing).
NAME_PREFIX = "EMFMon:"
try:
    import bluetooth
    EMFMON_SVC = bluetooth.UUID(0xF00D)   # what we advertise (compact 16-bit)
    # Accept BOTH the 16-bit form and its full 128-bit base form, since a phone
    # BLE tool may advertise F00D as either.
    EMFMON_SVCS = (
        EMFMON_SVC,
        bluetooth.UUID("0000F00D-0000-1000-8000-00805F9B34FB"),
    )
    # Battle handshake GATT service (peripheral hosts it while inviting).
    _SVC_UUID = bluetooth.UUID(0xF00E)
    _UUID_CENTRAL = bluetooth.UUID(0xF00F)   # central WRITES its blob/ACK here
    _UUID_PERIPH = bluetooth.UUID(0xF010)    # peripheral's blob (read + notify)
    # BATTLE_EVO! session characteristic. Present ONLY on EVO-capable builds:
    # the central discovers characteristics already, so its absence is how a
    # too-old peer is detected (plan 5.1). One characteristic carries both
    # directions - the central writes to it, the peripheral notifies on it.
    _UUID_EVO = bluetooth.UUID(0xF011)
    # Advertised capability marker (plan 5.1). A second 16-bit service UUID in
    # the SAME list the scan already parses, so old badges can be greyed out in
    # the peer list instead of being invited and only then found wanting.
    EVO_MARKER_SVC = bluetooth.UUID(0xF012)
    EVO_MARKER_SVCS = (
        EVO_MARKER_SVC,
        bluetooth.UUID("0000F012-0000-1000-8000-00805F9B34FB"),
    )
except Exception:  # CPython tests / no BLE
    EMFMON_SVC = None
    EMFMON_SVCS = ()
    _SVC_UUID = _UUID_CENTRAL = _UUID_PERIPH = _UUID_EVO = None
    EVO_MARKER_SVC = None
    EVO_MARKER_SVCS = ()

# --- BATTLE_EVO! kill switch (plan 5.1) ------------------------------------
# Lives HERE, not in battle.py, because battle.py imports this module and the
# reverse would be a circular import. Setting it False registers no 0xF011
# characteristic, advertises no 0xF012 marker, and offers no networked battle
# at all - leaving this build indistinguishable from v1.0.22 on air. Practice
# still works; it needs no radio.
EVO_ENABLED = True

# --- watchdog-safe discovery timing (see module docstring) ---------------
_ADV_INTERVAL_US = 300000     # advertise every 300 ms (non-connectable, cheap)
_ADV_REARM_MS = 60000         # re-arm the advert once a minute (self-healing)
_SCAN_BURST_MS = 3000         # listen for 3 s...
_SCAN_IDLE_MS = 4000          # ...then idle 4 s so the WDT idle task is fed
# How quiet a REPEATING fault goes after its first report (see BleLink._note).
# 30 s is long enough that a stuck loop retrying twice a second costs one write
# instead of thousands, and short enough that a fault you are actively watching
# still reappears while you are looking at it.
_ERR_QUIET_MS = 30000

# Plan 5.10 part 2. Milliseconds to spend probing for a counter-invite before
# falling back to advertising our own. **0 DISABLES IT**, which is the shipped
# default and deliberate:
#
#   this path CANNOT BE TESTED WITHOUT TWO BADGES, and there is exactly one.
#
# It changes the invite path - the one path that demonstrably works on air -
# and the failure it would introduce (a probe that eats the window, or a
# connect that half-succeeds) lands on every challenge where we hold the lower
# address, i.e. half of them. Untested radio code does not default on in front
# of a field full of people.
#
# TO ENABLE: set to ~700 and run the two-badge test in plan 5.10 - both press C
# at once, twice, with the roles swapped so each badge gets a turn at being the
# lower address. If both fights start, it works.
_SIMUL_PROBE_MS = 0
_SCAN_INTERVAL_US = 30000
_SCAN_WINDOW_US = 30000
_PEER_STALE_MS = 15000
_MAX_NAME = 8
# Cap the peer table: advertising addresses are attacker-controlled and
# `EMFMon:`/F00D is trivially forgeable, so a flood of spoofed adverts could
# otherwise grow this unbounded (OOM) and make the 400ms-throttled peer_list()
# sort a huge dict (CPU stall -> the watchdog class we fixed). Keep the closest.
_MAX_PEERS = 16

# --- handshake tuning ----------------------------------------------------
_MFG_ID = 0xFFFF              # manufacturer id for our invite beacon
_INVITE_MAGIC = b"EB"         # EMFMon Battle - tags an invite manuf record
_INVITE_STALE_MS = 4000       # a seen invite older than this is ignored
_INVITE_MS = 15000            # how long the challenger stays connectable
_CONNECT_MS = 10000           # central connect timeout
_EXCH_MS = 5000               # per GATT step timeout
_EXCH_TOTAL_MS = 8000         # overall budget for the stats exchange (hostile
#                               peers can't pin the connection open forever)
_INV_INTERVAL_US = 100000     # invite advert interval (100 ms - responsive)
_TAG_STATS = b"S"             # central->periph: stats blob follows
_TAG_ACK = b"A"               # central->periph: got your blob, commit

# --- BATTLE_EVO! session timing (plan 5.9) -------------------------------
# Every await gets a timeout. No exceptions - a peer that never answers must
# never be able to pin the connection open.
_EVO_VER_MS = 3000            # version check
_EVO_MON_MS = 5000            # mon exchange
_EVO_ROUND_MS = 3000          # one selection round
_EVO_REVEAL_MS = 3000         # lock-in reveal
_EVO_POLL_MS = 1000           # selection cadence: 1 Hz, driven HERE by a timer
#                               in this task - never by counting draw frames
# The selection phase's ABSOLUTE ceiling, independent of the 20 s countdown the
# UI shows. It has to be its own budget: _EXCH_TOTAL_MS governs the pre-
# selection exchange and must keep doing exactly that, or every EVO battle
# would die at 8 seconds.
_EVO_SELECT_TOTAL_MS = 30000




class EvoSession:
    """Status handle for one BATTLE_EVO! session, polled per-frame by battle.py.

    The UI never awaits: it
    reads these fields from update()/draw() and pushes its own state back in
    with set_status()/lock_in(). Blocking the draw loop on an await is what the
    watchdog rules forbid (plan 6.1), and this is the longest-lived connection
    in the app.

    Everything from the peer is stored RAW. This module never decodes a frame
    beyond its first byte: layout, sanitising, hashing and seeding all live in
    battle.py, so the server can call those same functions rather than growing
    a second opinion about the wire format (plan 7.2).
    """

    # phases, in order
    PENDING = "pending"
    VERSION = "version"
    MON = "mon"
    SELECTING = "selecting"
    REVEALED = "revealed"     # both queues known: disconnect, then simulate
    FAILED = "failed"

    # why it failed - picks the message the player sees
    F_OLD_PEER = "old_peer"   # no 0xF011: a v1.0.23-or-older badge (plan 5.1)
    F_VERSION = "version"     # proto/rules mismatch (plan 5.2)
    F_LINK = "link"           # dropped, timed out, malformed (plan 5.7)

    def __init__(self, role):
        self.role = role          # "invite" (peripheral) | "accept" (central)
        self.status = self.PENDING
        self.error = None
        self.fail_kind = None
        self.peer_addr = None
        self.peer_ver = None      # raw version frame
        self.peer_mon = None      # raw mon frame
        self.peer_status = None   # raw last selection-status frame (advisory)
        self.peer_commit = None   # raw commit frame
        self.peer_reveal = None   # raw reveal frame
        # pushed in by the UI
        self._my_status = None
        self._my_commit = None
        self._my_reveal = None

    def set_status(self, frame):
        """Our selection-phase status, rebuilt by the UI once a SECOND (not per
        frame) and sent on the next round."""
        self._my_status = frame

    def lock_in(self, commit_frame, reveal_frame):
        """Commit to a queue. Irreversible - there are no take-backs after the
        commitment, which is the whole point of it (plan 5.5)."""
        self._my_commit = commit_frame
        self._my_reveal = reveal_frame

    @property
    def locked(self):
        return self._my_commit is not None

    def _fail(self, msg, kind=None):
        if self.status != self.REVEALED:
            self.error = msg
            self.fail_kind = kind or self.F_LINK
            self.status = self.FAILED

    @property
    def done(self):
        return self.status in (self.REVEALED, self.FAILED)


class BleLink:
    def __init__(self, my_name):
        self.my_name = (my_name or "???")[:_MAX_NAME]
        self.peers = {}          # addr(bytes) -> {name, rssi, addr, device, seen}
        self._discovering = False
        self._adv_task = None
        self._scan_task = None
        # handshake / gatt
        self._my_addr = None
        self._addr_tried = False  # so a failed MAC lookup never retries per-packet
        self._invite = None       # a seen invite addressed to me (or None)
        self._gatt_ready = False
        self._c_central = None
        self._c_periph = None
        self._c_evo = None
        self._hs = None
        self._hs_task = None
        self._conn = None      # live connection, so a cancel can still close it
        # Repeating-fault throttle. A print BLOCKS on the serial write, so a
        # fault on a per-packet or per-loop path is not just noise - it is a
        # frame budget being spent, over and over, on a badge with a 5 s
        # watchdog. See _note().
        self._noted = {}

    def i_am_central_if_both_challenge(self, target_addr):
        """PLAN 5.10 PART 2 - who connects when BOTH sides challenge at once.

        The rule: **the LOWER address becomes the CENTRAL (connects); the higher
        stays the PERIPHERAL (keeps advertising).**

        A pure function of two values both sides already hold - ours from
        `my_addr()`, theirs from the peer entry being challenged - so both
        compute the same answer with NOTHING SENT. There is no round trip to
        lose and no state in which the two can disagree, which is the whole
        reason the rule is an address compare rather than a negotiation.
        Addresses are unique, so the tie-break has no tie to break.

        Returns None when our own address is unknown: `_my_addr_bytes()` can
        fail, and an unknown address must mean "behave exactly as before"
        rather than "guess". Absence means default, never inherit (review plan
        1.7).
        """
        mine = self._my_addr_bytes()
        if not mine or not target_addr:
            return None
        return bytes(mine) < bytes(target_addr)

    def _note(self, key, msg):
        """Report a repeating fault at most once per _ERR_QUIET_MS.

        Every print here is on a path that can fire again immediately: the
        scan and advertise loops retry after 500 ms, so a persistent fault
        used to mean two blocking serial writes a SECOND, indefinitely. The
        first occurrence is what tells you something is wrong; the four
        thousandth tells you nothing and costs a frame each time.

        Keyed, so two different faults do not silence each other.
        """
        now = _ticks_ms()
        last = self._noted.get(key)
        if last is not None and _ticks_diff(now, last) < _ERR_QUIET_MS:
            return
        self._noted[key] = now
        print(msg)

    # --- discovery ---------------------------------------------------------
    def start_discovery(self):
        if self._discovering:
            return
        self._discovering = True
        self.peers = {}
        self._invite = None
        # Resolve our address ONCE here, off the scan-result hot path (retry
        # once per search session). Then _match_invite's per-packet calls hit
        # the cache instead of a blocking BLE controller call each result.
        self._addr_tried = False
        self._my_addr_bytes()
        self._adv_task = asyncio.create_task(self._advertise_loop())
        self._scan_task = asyncio.create_task(self._scan_loop())

    def stop(self):
        """Stop discovery (advertise + scan). Leaves any handshake alone."""
        self._discovering = False
        for t in (self._adv_task, self._scan_task):
            if t is not None:
                t.cancel()   # interrupts the await; aioble tears down gap ops
        self._adv_task = None
        self._scan_task = None

    def peer_list(self):
        """Nearby badges, closest (strongest signal) first."""
        self._prune()
        return sorted(self.peers.values(), key=lambda p: -_rssi_key(p.get("rssi")))

    def _prune(self):
        now = _ticks_ms()
        for k in list(self.peers.keys()):
            if _ticks_diff(now, self.peers[k]["seen"]) > _PEER_STALE_MS:
                del self.peers[k]

    def _record(self, disp_name, rssi, device, evo=False):
        addr = bytes(device.addr)
        if addr not in self.peers and len(self.peers) >= _MAX_PEERS:
            # Table full: evict the weakest-signal peer, but only if this new
            # one is at least as strong - so a flood of weak spoofed adverts
            # can't push out the real, close badges. Bounds the dict at _MAX_PEERS.
            weakest = min(self.peers,
                          key=lambda k: _rssi_key(self.peers[k].get("rssi")))
            if _rssi_key(rssi) <= _rssi_key(self.peers[weakest].get("rssi")):
                return
            del self.peers[weakest]
        self.peers[addr] = {
            "name": disp_name,
            "rssi": rssi,
            "addr": addr,
            "device": device,
            "evo": evo,          # advertised 0xF012: can be battled (plan 5.1)
            "seen": _ticks_ms(),
        }

    async def _advertise_loop(self):
        # NON-CONNECTABLE, set-and-forget; re-arm slowly (self-healing). No
        # inbound connections -> no MAX_ACT pressure (module docstring rule 1).
        name = NAME_PREFIX + self.my_name
        services = _adv_services()   # built ONCE, not per re-arm
        while self._discovering:
            try:
                await aioble.advertise(
                    _ADV_INTERVAL_US, name=name, services=services,
                    connectable=False, timeout_ms=_ADV_REARM_MS,
                )
            except asyncio.TimeoutError:
                pass   # expected once per _ADV_REARM_MS; loop re-arms
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._note("advloop", "BLE adv loop: %s" % e)
                await asyncio.sleep_ms(500)

    async def _scan_loop(self):
        while self._discovering:
            try:
                # PASSIVE scan in short bursts (rules 2 & 3).
                async with aioble.scan(
                    _SCAN_BURST_MS, interval_us=_SCAN_INTERVAL_US,
                    window_us=_SCAN_WINDOW_US, active=False,
                ) as scanner:
                    bad, last = 0, None
                    async for r in scanner:
                        try:
                            disp, evo = _match_name(r.name() or "", r.services())
                            if disp is not None:
                                self._record(disp, r.rssi, r.device, evo)
                            inv = self._match_invite(r)
                            if inv is not None:
                                self._invite = inv
                        except Exception as e:
                            # NEVER print per result. This ran ~9 times a
                            # second in a measured soak (13,675 results in
                            # 25 min), so one malformed advert in range meant
                            # a blocking serial write every ~100 ms - and any
                            # passer-by could cause it just by broadcasting
                            # one (plan 5.8: untrusted input must not be able
                            # to make us do work). Counted, and summarised
                            # once per burst instead, which still surfaces the
                            # fault and tells you how big it is.
                            bad += 1
                            last = e
                    if bad:
                        self._note("scanres", "BLE: %d bad advert(s) this burst"
                                              " (last: %s)" % (bad, last))
                self._prune()
                await asyncio.sleep_ms(_SCAN_IDLE_MS)   # generous idle: feeds WDT
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._note("scanloop", "BLE scan loop: %s" % e)
                await asyncio.sleep_ms(500)

    # --- own address & gatt ------------------------------------------------
    def my_addr(self):
        return self._my_addr_bytes()

    def _my_addr_bytes(self):
        # Cache the ATTEMPT, not just a success: a persistent failure must NOT
        # re-run the blocking BLE call on every scan result (that would starve
        # the idle task -> watchdog). Retried once per search session via
        # start_discovery resetting _addr_tried.
        if self._addr_tried:
            return self._my_addr
        self._addr_tried = True
        try:
            import bluetooth
            b = bluetooth.BLE()
            b.active(True)
            # active(True) RESETS the GATT DB to defaults on this firmware (see
            # TEST/badge_peer.py), so any battle service registered by an
            # earlier invite is GONE as of this call. _gatt_ready is sticky, so
            # without clearing it _ensure_gatt() would no-op and the next invite
            # would advertise with an empty GATT table - the peer connects, finds
            # no 0xF00E, and both sides sit there until the exchange times out.
            # Hit on the SECOND search of a session (the first registers after
            # this call), and it stays broken until reboot.
            self._gatt_ready = False
            v = b.config("mac")
            # config('mac') returns (addr_type, addr) on the badge.
            a = v[1] if isinstance(v, tuple) else v
            self._my_addr = bytes(a)
        except Exception as e:
            print("BLE my_addr:", e)
            self._my_addr = None
        return self._my_addr

    def _ensure_gatt(self):
        """Register the battle GATT service ONCE (resets the GATT DB, so guard
        it). The peripheral hosts this while inviting; the central discovers
        it after connecting."""
        if self._gatt_ready or _SVC_UUID is None:
            return
        svc = aioble.Service(_SVC_UUID)
        self._c_central = aioble.Characteristic(svc, _UUID_CENTRAL, write=True)
        self._c_periph = aioble.Characteristic(
            svc, _UUID_PERIPH, read=True, notify=True
        )
        # One characteristic carries the whole EVO session in both directions:
        # the central writes to it, the peripheral notifies on it. Registered
        # only when EVO is enabled, because its ABSENCE is exactly how a peer
        # detects a build that cannot battle (plan 5.1).
        self._c_evo = None
        if EVO_ENABLED and _UUID_EVO is not None:
            self._c_evo = aioble.Characteristic(
                svc, _UUID_EVO, write=True, read=True, notify=True
            )
        aioble.register_services(svc)
        self._gatt_ready = True

    # --- invite detection (acceptor side, runs during discovery) -----------
    def _match_invite(self, r):
        """Is this scan result an EMFMon invite addressed to ME? Return an
        invite dict (device/addr/name) or None."""
        my = self._my_addr_bytes()
        if my is None:
            return None
        try:
            for mid, data in r.manufacturer():
                if mid == _MFG_ID and len(data) >= 8 and data[:2] == _INVITE_MAGIC:
                    if bytes(data[2:8]) == my:
                        name = ""
                        if len(data) >= 16:
                            try:
                                name = bytes(data[8:16]).rstrip(b"\x00").decode()
                            except Exception:
                                name = ""
                        return {
                            "device": r.device,
                            "addr": bytes(r.device.addr),
                            "name": name or "???",
                            "seen": _ticks_ms(),
                        }
        except Exception:
            pass
        return None

    def pending_invite(self):
        """The peer currently inviting me (fresh), or None."""
        inv = self._invite
        if inv is None:
            return None
        if _ticks_diff(_ticks_ms(), inv["seen"]) > _INVITE_STALE_MS:
            self._invite = None
            return None
        return inv

    def clear_invite(self):
        self._invite = None

    # --- handshake ---------------------------------------------------------


    def cancel_handshake(self):
        if self._hs_task is not None:
            self._hs_task.cancel()
        self._hs_task = None
        self._hs = None
        # The task we just cancelled OWNS the connection, and its teardown runs
        # inside its own cancellation - the `await conn.disconnect()` there can
        # be cut short, and CancelledError is a BaseException so the `except
        # Exception` around it does not catch it either. Either way the link
        # leaks with the radio still allocated, and discovery then restarts on
        # top of it. Reap it from a task that is NOT being cancelled, so the
        # disconnect actually gets to finish.
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                asyncio.create_task(_reap(conn))
            except Exception as e:
                self._note("reap", "BLE reap: %s" % e)

    async def _advertise_invite(self, target):
        """Go connectable, targeted at `target`, until it connects or the invite
        window expires. Returns the connection, or None on timeout.

        Re-arms until the RIGHT peer connects, so a single stray or malicious
        connect no longer kills the invite. Only ever ONE connectable advert at
        a time (no scan is running), so the watchdog rules hold.
        """
        nb = self.my_name.encode()[:8]
        nb = nb + b"\x00" * (8 - len(nb))
        mfg = (_MFG_ID, _INVITE_MAGIC + target + nb)
        t0 = _ticks_ms()
        while True:
            remaining = _INVITE_MS - _ticks_diff(_ticks_ms(), t0)
            if remaining <= 0:
                return None
            try:
                conn = await aioble.advertise(
                    _INV_INTERVAL_US, manufacturer=mfg,
                    connectable=True, timeout_ms=remaining,
                )
            except asyncio.TimeoutError:
                return None
            if conn is None:
                return None
            if bytes(conn.device.addr) == target:
                return conn
            # Not our target - drop it and keep advertising the remainder.
            try:
                await conn.disconnect()
            except Exception:
                pass

    def start_evo_invite(self, peer, ver_frame, mon_frame, status_frame, tags):
        """Challenger/peripheral: advertise a targeted invite, then run the
        session over 0xF011."""
        self.stop()
        self.cancel_handshake()
        sess = EvoSession("invite")
        sess.set_status(status_frame)
        self._hs = sess
        self._hs_task = asyncio.create_task(
            self._evo_invite_task(peer, ver_frame, mon_frame, tags, sess))
        return sess

    def start_evo_accept(self, peer, ver_frame, mon_frame, status_frame, tags):
        """Acceptor/central: connect to the challenger, then run the session."""
        self.stop()
        self.cancel_handshake()
        sess = EvoSession("accept")
        sess.set_status(status_frame)
        self._hs = sess
        self._hs_task = asyncio.create_task(
            self._evo_accept_task(peer, ver_frame, mon_frame, tags, sess))
        return sess

    async def _evo_invite_task(self, peer, ver_frame, mon_frame, tags, sess):
        conn = None
        try:
            await asyncio.sleep_ms(50)   # let discovery finish tearing down
            self._ensure_gatt()
            if self._c_evo is None:
                sess._fail("EVO off", sess.F_LINK)
                return
            # PLAN 5.10 PART 2 - genuinely simultaneous, neither has seen the
            # other. See `i_am_central_if_both_challenge`: the LOWER address
            # connects, the higher advertises. If we are the lower one, PROBE
            # first - a short connect that succeeds only if they are already
            # advertising an invite, i.e. only if they pressed too.
            #
            # WHY A PROBE RATHER THAN INTERLEAVED SCANNING, which is what 5.10
            # sketches. Scanning while a connectable advert is up is exactly
            # what section 7 rule 1 forbids and what rebooted a badge before,
            # and duty-cycling the advert to make room for scan bursts puts
            # GAPS in it - so a normal, non-simultaneous invite could be missed
            # at the moment the peer tries to connect. A probe is strictly
            # SEQUENTIAL: one connect, then advertise. No rule bent, no gap.
            #
            # Cost when nobody else is challenging: the lower-addressed badge
            # spends _SIMUL_PROBE_MS on a doomed connect before its invite goes
            # out. That is the whole price, it lands on half of all challenges,
            # and it is why the window is short.
            if _SIMUL_PROBE_MS and self.i_am_central_if_both_challenge(
                    peer["addr"]):
                try:
                    conn = await peer["device"].connect(
                        timeout_ms=_SIMUL_PROBE_MS)
                except Exception:
                    conn = None      # not advertising: the normal case
                if conn is not None:
                    # They WERE advertising - both of us pressed. We hold the
                    # lower address, so we are the central.
                    self._conn = conn
                    sess.peer_addr = peer["addr"]
                    await self._evo_as_central(conn, ver_frame, mon_frame,
                                               tags, sess)
                    return
            conn = self._conn = await self._advertise_invite(peer["addr"])
            if conn is None:
                sess._fail("No answer")
                return
            sess.peer_addr = bytes(conn.device.addr)
            c = self._c_evo

            async def send(frame):
                # NOTIFY ONLY. Calling write() here would overwrite the stored
                # value that read() has to return for the central's NEXT write,
                # and we would end up parsing our own outbound frame.
                c.notify(conn, frame)

            async def recv(ms):
                await c.written(timeout_ms=ms)
                return bytes(c.read() or b"")

            await self._evo_run(sess, ver_frame, mon_frame, tags,
                                send, recv, False)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            sess._fail(_short_err(e))
        finally:
            self._conn = None
            if conn is not None:
                try:
                    await conn.disconnect()
                except Exception:
                    pass

    async def _evo_as_central(self, conn, ver_frame, mon_frame, tags, sess):
        """Discover the EVO characteristic on `conn` and run the session as
        CENTRAL. Split out because plan 5.10 part 2 gives the CHALLENGER a way
        to end up central too - so this had to stop being welded into the
        accept path (review plan 1.1: the second caller is what proves the
        primitive was missing)."""
        svc = await conn.service(_SVC_UUID)
        if svc is None:
            sess._fail("No battle svc")
            return
        c = await svc.characteristic(_UUID_EVO)
        if c is None:
            # No 0xF011: a v1.0.23-or-older badge. It cannot be battled - this
            # build has no Classic combat to fall back to and must never
            # attempt the legacy exchange (plan 5.1, constraint 12).
            sess._fail("Older EMFMon", sess.F_OLD_PEER)
            return
        await c.subscribe(notify=True)

        async def send(frame):
            await c.write(frame, response=True)

        async def recv(ms):
            return bytes(await c.notified(timeout_ms=ms))

        await self._evo_run(sess, ver_frame, mon_frame, tags, send, recv, True)

    async def _evo_accept_task(self, peer, ver_frame, mon_frame, tags, sess):
        conn = None
        try:
            await asyncio.sleep_ms(50)
            conn = self._conn = await peer["device"].connect(timeout_ms=_CONNECT_MS)
            sess.peer_addr = peer["addr"]
            await self._evo_as_central(conn, ver_frame, mon_frame, tags, sess)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            sess._fail(_short_err(e))
        finally:
            self._conn = None
            if conn is not None:
                try:
                    await conn.disconnect()
                except Exception:
                    pass

    async def _evo_run(self, sess, ver_frame, mon_frame, tags, send, recv,
                       central):
        tag_lock, tag_commit, tag_reveal = tags

        async def swap(out_fn, ms):
            """One round. The CENTRAL always speaks first, so both sides agree
            on who is waiting for whom without negotiating it.

            `out_fn` is called at SEND TIME, not before the wait. That matters
            on the peripheral, which receives before it sends: a player who
            locks in DURING that wait would otherwise send the stale status
            frame captured at the top of the round, while the break condition
            below saw the fresh commit. The peer never learns we committed, we
            advance to reveal anyway, and the next frame each side sees is one
            the other is not expecting - both then reject with "Bad data".
            """
            if central:
                await send(out_fn())
                return await recv(ms)
            got = await recv(ms)
            await send(out_fn())
            return got

        # 1. version check (plan 5.2)
        sess.status = sess.VERSION
        peer_ver = await swap(lambda: ver_frame, _EVO_VER_MS)
        sess.peer_ver = peer_ver
        if peer_ver != ver_frame:
            # Byte equality covers proto AND rules at once. There is nothing to
            # negotiate down to: keeping every historical balance table on the
            # badge is exactly the unbounded growth plan 6.3 forbids. A hard
            # stop, which is why the message matters - a badge that silently
            # refuses to fight reads as a broken radio.
            sess._fail("Different version", sess.F_VERSION)
            return

        # 2. mon exchange (plan 5.3)
        sess.status = sess.MON
        peer_mon = await swap(lambda: mon_frame, _EVO_MON_MS)
        if not peer_mon:
            sess._fail("No mon")
            return
        sess.peer_mon = peer_mon

        # 3. selection (plan 5.4). The COMMITMENT gates the advance, not the
        # status frame's locked byte: a peer that says "locked" without sending
        # a commit has committed to nothing.
        sess.status = sess.SELECTING
        t0 = _ticks_ms()
        sent_commit = [False]   # has OUR commitment actually gone out?
        while True:
            if _ticks_diff(_ticks_ms(), t0) > _EVO_SELECT_TOTAL_MS:
                # The absolute ceiling, independent of the countdown the UI
                # shows. A peer that never locks cannot pin the link open.
                sess._fail("Timed out")
                return
            if sess._my_commit is None and sess._my_status is None:
                sess._fail("EVO off", sess.F_LINK)
                return
            # Resolved at send time - see swap(). Committing mid-round must be
            # visible in the frame we actually put on the wire.
            #
            # `sent_commit` records whether the frame we ACTUALLY put on the
            # wire was the commitment, and gates the advance below. Resolving
            # at send time is not enough on its own: the central sends at the
            # TOP of a round and then waits, so a player who locks in during
            # that wait holds a commit the peer has never seen. Without this
            # the loop would break on it and go straight to reveal, and the
            # peer - still in selection, one commit short - can only call a no
            # contest. That is the bug this pair of lines fixes, and it was
            # timing-dependent enough to pass a whole gate before showing up.
            def _out():
                f = sess._my_commit or sess._my_status
                if f is sess._my_commit:
                    sent_commit[0] = True
                return f

            got = await swap(_out, _EVO_ROUND_MS)
            head = got[:1]
            if head == tag_commit:
                sess.peer_commit = got
            elif head == tag_lock:
                sess.peer_status = got
            elif head == tag_reveal:
                # They are a phase AHEAD: a reveal is only sent once a peer
                # holds both commits, so ours reached them and theirs never
                # reached us. Distinct from corruption - the frame is perfectly
                # well formed, it is the sequence that broke - and it is the
                # one case a plain "Bad data" would send us looking at parsers
                # instead of at ordering.
                #
                # Still a no contest: without their commitment their reveal
                # cannot be verified, and accepting an unverifiable reveal is
                # exactly the cheat commit-reveal exists to stop (plan 5.5).
                print("EVO: peer revealed while we were still selecting - "
                      "our commit reached them, theirs did not reach us "
                      "(tick %d of the round)" %
                      _ticks_diff(_ticks_ms(), t0))
                sess._fail("Out of step")
                return
            else:
                # Say WHAT arrived. "Bad data" with no detail is unactionable,
                # and this is the frame boundary most likely to catch a
                # transport that pads, truncates or delivers an empty read.
                print("EVO: bad selection frame len=%d head=%r" %
                      (len(got), head))
                sess._fail("Bad data")
                return
            if (sent_commit[0] and sess._my_commit is not None
                    and sess.peer_commit is not None):
                break
            if central:
                # Pace at 1 Hz from HERE, in the BLE task - never by counting
                # draw frames (plan 6.1). The peripheral is paced by the
                # central's cadence and must not sleep as well, or the two
                # drift apart.
                await asyncio.sleep_ms(_EVO_POLL_MS)

        # 4. reveal (plan 5.5). Verifying it against the commitment is
        # battle.py's job - it is the side that holds the hash.
        peer_reveal = await swap(lambda: sess._my_reveal, _EVO_REVEAL_MS)
        if peer_reveal[:1] != tag_reveal:
            print("EVO: bad reveal frame len=%d head=%r" %
                  (len(peer_reveal), peer_reveal[:1]))
            sess._fail("Bad data")
            return
        sess.peer_reveal = peer_reveal
        sess.status = sess.REVEALED
        # 5. the caller's `finally` disconnects. The radio is released BEFORE
        # anything is simulated: no connection survives into the fight
        # (plan 1, constraint 1).

# --- module helpers ---------------------------------------------------------
# RESTORED at rev 73. These were destroyed as collateral by the rev 69 removal
# of the Classic handshake: that edit walked each dead METHOD forward to the
# next 4-space `def`, which stepped straight over the MODULE-LEVEL functions
# sitting between them and took those too. `_match_name` and `_adv_services`
# are the scan and advertise paths, so the badge could neither see nor be
# seen. See blelab/names_test.py, which now refuses to let this recur.

def _short_err(e):
    s = str(e) or e.__class__.__name__
    return s[:24]


def _rssi_key(rssi):
    # sort/eviction key: unknown RSSI (None) sorts weakest. Real BLE RSSI is
    # negative dBm, so an actual 0 never occurs here.
    return -999 if rssi is None else rssi


def _match_name(name, services):
    """Is this advert an EMFMon badge? Returns (display_name, evo_capable),
    with a display name of None if it is not one of ours.

    `evo_capable` comes from the 0xF012 marker in the same UUID list (plan 5.1),
    so the peer list can grey out badges that cannot be battled rather than
    letting a player invite one and only find out after connecting.

    The list is walked ONCE: `services` is a generator over the advert payload
    and this runs per scan result, which is the allocation hot path the
    watchdog rules care about.
    """
    disp = None
    if isinstance(name, str) and name.startswith(NAME_PREFIX):
        disp = name[len(NAME_PREFIX):][:_MAX_NAME] or "???"
    evo = False
    matched_svc = False
    try:
        for u in services:
            if EMFMON_SVCS and u in EMFMON_SVCS:   # 16-bit OR 128-bit base form
                matched_svc = True
            elif EVO_MARKER_SVCS and u in EVO_MARKER_SVCS:
                evo = True
    except Exception:
        pass
    if disp is None and matched_svc:
        if isinstance(name, str) and name:
            disp = name[:_MAX_NAME]
        else:
            disp = "guest"
    return disp, evo


def _adv_services():
    """The 16-bit UUID list for the DISCOVERY advert.

    F00D always; F012 as well on an EVO build (plan 5.1). Watchdog rule 2
    requires the whole advert to fit the PRIMARY payload so a passive scan sees
    it without scan responses - flags 3 + name 17 + one UUID 4 = 24 of 31 today,
    and appending a second 16-bit UUID to the SAME list costs 2 more: 26 of 31,
    5 spare. Confirm the encoded length on hardware once (plan 15.1): an
    overflow presents as "peers stopped appearing", never as an error.
    """
    if EMFMON_SVC is None:
        return None
    if EVO_ENABLED and EVO_MARKER_SVC is not None:
        return [EMFMON_SVC, EVO_MARKER_SVC]
    return [EMFMON_SVC]


async def _reap(conn):
    """Close a connection whose owning task was cancelled (see
    cancel_handshake). Runs in its own task so nothing can interrupt it."""
    try:
        await conn.disconnect()
    except Exception:
        pass
