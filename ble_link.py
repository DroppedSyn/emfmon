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
    PERIPHERAL. (start_invite)
  - The target, still scanning in discovery, sees the invite addressed to it
    (pending_invite()) and can Accept -> it stops discovery, connects to the
    challenger as CENTRAL, and the two swap stats+nonce blobs. (start_accept)

The exchange moves two opaque blobs (packed/sanitised by battle.py) plus an
ACK, over one short-lived connection; battle.py derives the shared seed and runs
the deterministic resolver. See start_invite/start_accept + Handshake.

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
except Exception:  # CPython tests / no BLE
    EMFMON_SVC = None
    EMFMON_SVCS = ()
    _SVC_UUID = _UUID_CENTRAL = _UUID_PERIPH = None

# --- watchdog-safe discovery timing (see module docstring) ---------------
_ADV_INTERVAL_US = 300000     # advertise every 300 ms (non-connectable, cheap)
_ADV_REARM_MS = 60000         # re-arm the advert once a minute (self-healing)
_SCAN_BURST_MS = 3000         # listen for 3 s...
_SCAN_IDLE_MS = 4000          # ...then idle 4 s so the WDT idle task is fed
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


class Handshake:
    """Status handle for one in-flight battle handshake, polled per-frame by
    battle.py. `peer_blob` is the opaque stats bytes the peer sent (battle.py
    packs/unpacks/sanitises); `peer_addr` is their 6-byte BLE address."""

    def __init__(self, role):
        self.role = role          # "invite" (peripheral) | "accept" (central)
        self.status = "pending"   # pending -> connected -> exchanged | failed
        self.peer_blob = None
        self.peer_addr = None
        self.error = None

    def _fail(self, msg):
        if self.status != "exchanged":
            self.error = msg
            self.status = "failed"

    @property
    def done(self):
        return self.status in ("exchanged", "failed")


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
        self._hs = None
        self._hs_task = None

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
        return sorted(self.peers.values(), key=lambda p: -(p.get("rssi") or -999))

    def _prune(self):
        now = _ticks_ms()
        for k in list(self.peers.keys()):
            if _ticks_diff(now, self.peers[k]["seen"]) > _PEER_STALE_MS:
                del self.peers[k]

    def _record(self, disp_name, rssi, device):
        addr = bytes(device.addr)
        if addr not in self.peers and len(self.peers) >= _MAX_PEERS:
            # Table full: evict the weakest-signal peer, but only if this new
            # one is at least as strong - so a flood of weak spoofed adverts
            # can't push out the real, close badges. Bounds the dict at _MAX_PEERS.
            weakest = min(self.peers,
                          key=lambda k: (self.peers[k].get("rssi") or -999))
            if (rssi or -999) <= (self.peers[weakest].get("rssi") or -999):
                return
            del self.peers[weakest]
        self.peers[addr] = {
            "name": disp_name,
            "rssi": rssi,
            "addr": addr,
            "device": device,
            "seen": _ticks_ms(),
        }

    async def _advertise_loop(self):
        # NON-CONNECTABLE, set-and-forget; re-arm slowly (self-healing). No
        # inbound connections -> no MAX_ACT pressure (module docstring rule 1).
        name = NAME_PREFIX + self.my_name
        services = [EMFMON_SVC] if EMFMON_SVC is not None else None
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
                print("BLE adv loop:", e)
                await asyncio.sleep_ms(500)

    async def _scan_loop(self):
        while self._discovering:
            try:
                # PASSIVE scan in short bursts (rules 2 & 3).
                async with aioble.scan(
                    _SCAN_BURST_MS, interval_us=_SCAN_INTERVAL_US,
                    window_us=_SCAN_WINDOW_US, active=False,
                ) as scanner:
                    async for r in scanner:
                        try:
                            disp = _match_name(r.name() or "", r.services())
                            if disp is not None:
                                self._record(disp, r.rssi, r.device)
                            inv = self._match_invite(r)
                            if inv is not None:
                                self._invite = inv
                        except Exception as e:
                            print("BLE peer note:", e)
                self._prune()
                await asyncio.sleep_ms(_SCAN_IDLE_MS)   # generous idle: feeds WDT
            except asyncio.CancelledError:
                break
            except Exception as e:
                print("BLE scan loop:", e)
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
    def start_invite(self, peer, my_blob):
        """Challenger/peripheral: stop discovery, advertise a connectable invite
        targeted at `peer`, host the GATT service, and swap blobs on connect."""
        self.stop()
        self.cancel_handshake()
        hs = Handshake("invite")
        self._hs = hs
        self._hs_task = asyncio.create_task(self._invite_task(peer, my_blob, hs))
        return hs

    def start_accept(self, peer, my_blob):
        """Acceptor/central: stop discovery, connect to `peer`, swap blobs."""
        self.stop()
        self.cancel_handshake()
        hs = Handshake("accept")
        self._hs = hs
        self._hs_task = asyncio.create_task(self._accept_task(peer, my_blob, hs))
        return hs

    def cancel_handshake(self):
        if self._hs_task is not None:
            self._hs_task.cancel()
        self._hs_task = None
        self._hs = None

    async def _invite_task(self, peer, my_blob, hs):
        conn = None
        try:
            # Let the just-cancelled discovery advert/scan finish tearing down
            # (their gap_advertise(None)/gap_scan(None)) BEFORE we go
            # connectable, so their late teardown can't clobber our invite.
            await asyncio.sleep_ms(50)
            self._ensure_gatt()
            target = peer["addr"]
            nb = self.my_name.encode()[:8]
            nb = nb + b"\x00" * (8 - len(nb))
            mfg = (_MFG_ID, _INVITE_MAGIC + target + nb)
            # Re-arm the connectable advert until the RIGHT peer connects or the
            # invite window expires: a single stray/malicious connect no longer
            # kills the invite. Only ever ONE connectable advert at a time (no
            # scan running), so the watchdog rules hold.
            t0 = _ticks_ms()
            while True:
                remaining = _INVITE_MS - _ticks_diff(_ticks_ms(), t0)
                if remaining <= 0:
                    hs._fail("No answer")
                    return
                try:
                    conn = await aioble.advertise(
                        _INV_INTERVAL_US, manufacturer=mfg,
                        connectable=True, timeout_ms=remaining,
                    )
                except asyncio.TimeoutError:
                    hs._fail("No answer")
                    return
                if conn is None:
                    hs._fail("No answer")
                    return
                if bytes(conn.device.addr) == target:
                    break
                # Not our target - drop it and keep advertising the remainder.
                try:
                    await conn.disconnect()
                except Exception:
                    pass
                conn = None
            hs.peer_addr = bytes(conn.device.addr)
            hs.status = "connected"
            # Wait for the central's stats write, under an OVERALL deadline so a
            # peer that streams non-STATS writes can't pin the connection open.
            peer_blob = None
            ex0 = _ticks_ms()
            while peer_blob is None:
                if _ticks_diff(_ticks_ms(), ex0) > _EXCH_TOTAL_MS:
                    hs._fail("No stats")
                    return
                await self._c_central.written(timeout_ms=_EXCH_MS)
                v = self._c_central.read()
                if v and v[:1] == _TAG_STATS:
                    peer_blob = bytes(v[1:])
            # Send ours, then require a valid ACK before committing.
            self._c_periph.write(my_blob)
            self._c_periph.notify(conn, my_blob)
            await self._c_central.written(timeout_ms=_EXCH_MS)
            ack = self._c_central.read()
            if not (ack and ack[:1] == _TAG_ACK):
                hs._fail("No ack")
                return
            hs.peer_blob = peer_blob
            hs.status = "exchanged"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            hs._fail(_short_err(e))
        finally:
            if conn is not None:
                try:
                    await conn.disconnect()
                except Exception:
                    pass

    async def _accept_task(self, peer, my_blob, hs):
        conn = None
        try:
            # Let the just-cancelled discovery scan/advert tear down before we
            # bring up the connection (avoids a leftover scan racing connect()).
            await asyncio.sleep_ms(50)
            conn = await peer["device"].connect(timeout_ms=_CONNECT_MS)
            hs.peer_addr = peer["addr"]
            hs.status = "connected"
            svc = await conn.service(_SVC_UUID)
            if svc is None:
                hs._fail("No battle svc")
                return
            c_cen = await svc.characteristic(_UUID_CENTRAL)
            c_per = await svc.characteristic(_UUID_PERIPH)
            if c_cen is None or c_per is None:
                hs._fail("No battle svc")
                return
            await c_per.subscribe(notify=True)
            await c_cen.write(_TAG_STATS + my_blob, response=True)
            peer_blob = await c_per.notified(timeout_ms=_EXCH_MS)
            await c_cen.write(_TAG_ACK, response=True)
            hs.peer_blob = bytes(peer_blob)
            hs.status = "exchanged"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            hs._fail(_short_err(e))
        finally:
            if conn is not None:
                try:
                    await conn.disconnect()
                except Exception:
                    pass


def _short_err(e):
    s = str(e) or e.__class__.__name__
    return s[:24]


def _match_name(name, services):
    """Is this advert an EMFMon badge? Return its display name, else None.
    Matches an `EMFMon:` name prefix OR our service UUID (services is an
    iterable of bluetooth.UUID from the scan result)."""
    if isinstance(name, str) and name.startswith(NAME_PREFIX):
        return name[len(NAME_PREFIX):][:_MAX_NAME] or "???"
    if EMFMON_SVCS:
        try:
            for u in services:
                if u in EMFMON_SVCS:   # 16-bit OR full 128-bit base form
                    if isinstance(name, str) and name:
                        return name[:_MAX_NAME]
                    return "guest"
        except Exception:
            pass
    return None
