"""BLE transport for EMFMon battles.

Replaces the old ESP-NOW discovery, which couldn't find peers in the field (its
channel follows each badge's WiFi association). BLE advertising/scanning uses
fixed advertising channels every device listens on, so discovery works
regardless of WiFi. aioble is frozen into the tildagon build.

DISCOVERY (this file, stage 1): while searching, each badge ADVERTISES
`EMFMon:<name>` and SCANS for other `EMFMon:` advertisers. peer_list() returns
the nearby badges sorted closest-first (strongest RSSI). Stale peers age out.

The GATT invite/accept/stats handshake (stage 2) connects to a chosen peer and
feeds the existing shared-seed resolver - added on top of this layer.

WATCHDOG-SAFE DESIGN (this is a fix for a 5s task-WDT reboot that hit after
1-6 min of camping on the search screen - see history below). Three rules:

  1. Advertise NON-CONNECTABLE and set-and-forget. In a dense RF field (an EMF
     camp), a connectable advert invites phones/badges to actually CONNECT to
     us; each connection burns one of only CONFIG_BT_CTRL_BLE_MAX_ACT=6 BLE
     activities and churns the controller hard. Stage-2 handshake is still a
     stub, so we don't want inbound connections yet anyway. Non-connectable =
     zero connection churn, zero MAX_ACT pressure. We re-arm the advert on a
     slow timer (self-healing) rather than restarting it every couple seconds.

  2. Scan PASSIVE. Active scan makes US transmit scan-requests and pulls in
     scan-responses too - roughly double the SCAN_RESULT IRQ flood, each IRQ
     waking the MicroPython task. The `EMFMon:` name + F00D UUID both fit in the
     primary adv payload (24/31 bytes), so passive scanning still sees names.

  3. DUTY-CYCLE the scan with a GENEROUS idle gap. A short scan burst then an
     idle asyncio.sleep guarantees the FreeRTOS idle task (which the task WDT
     watches) always gets to run, even alongside the app's 60fps draw loop. A
     single continuous scan in a busy field starves idle -> WDT reboot.
"""

import asyncio

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
except Exception:  # CPython tests / no BLE
    EMFMON_SVC = None
    EMFMON_SVCS = ()

# --- watchdog-safe timing (see module docstring) -------------------------
_ADV_INTERVAL_US = 300000     # advertise every 300 ms (non-connectable, cheap)
_ADV_REARM_MS = 60000         # re-arm the advert once a minute (self-healing,
#                               near-zero churn) instead of every couple secs
_SCAN_BURST_MS = 3000         # listen for 3 s...
_SCAN_IDLE_MS = 4000          # ...then idle 4 s so the WDT idle task is fed
_SCAN_INTERVAL_US = 30000     # within a burst, scan window == interval (listen
_SCAN_WINDOW_US = 30000       #   continuously for the 3 s burst)
# Peers must survive across a full scan+idle cycle (7 s) plus grace, or they'd
# flicker out during every idle gap. Two cycles of headroom.
_PEER_STALE_MS = 15000
_MAX_NAME = 8


class BleLink:
    def __init__(self, my_name):
        self.my_name = (my_name or "???")[:_MAX_NAME]
        self.peers = {}          # id(bytes) -> {name, rssi, addr, device, seen}
        self._discovering = False
        self._adv_task = None
        self._scan_task = None

    # --- discovery ---------------------------------------------------------
    def start_discovery(self):
        if self._discovering:
            return
        self._discovering = True
        self.peers = {}
        self._adv_task = asyncio.create_task(self._advertise_loop())
        self._scan_task = asyncio.create_task(self._scan_loop())

    def stop(self):
        self._discovering = False
        for t in (self._adv_task, self._scan_task):
            if t is not None:
                t.cancel()   # interrupts the await immediately; aioble tears
                #              down gap_advertise/gap_scan on CancelledError
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
        self.peers[addr] = {
            "name": disp_name,
            "rssi": rssi,
            "addr": addr,
            "device": device,
            "seen": _ticks_ms(),
        }

    async def _advertise_loop(self):
        # NON-CONNECTABLE, set-and-forget. Re-arm slowly (self-healing) rather
        # than churning the controller. connectable=False means no inbound
        # connections -> no MAX_ACT pressure (see module docstring rule 1).
        name = NAME_PREFIX + self.my_name
        services = [EMFMON_SVC] if EMFMON_SVC is not None else None
        while self._discovering:
            try:
                # With connectable=False and no connection ever arriving, this
                # simply parks on the timeout while the controller advertises
                # autonomously - almost no Python-side work for a whole minute.
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
                # PASSIVE scan (active=False) in a short burst - see rules 2 & 3.
                async with aioble.scan(
                    _SCAN_BURST_MS, interval_us=_SCAN_INTERVAL_US,
                    window_us=_SCAN_WINDOW_US, active=False,
                ) as scanner:
                    async for r in scanner:
                        try:
                            disp = _match_name(r.name() or "", r.services())
                            if disp is not None:
                                self._record(disp, r.rssi, r.device)
                        except Exception as e:
                            print("BLE peer note:", e)
                self._prune()
                # GENEROUS idle gap: guarantees the WDT idle task runs even
                # under the 60fps draw loop. This is the core of the fix.
                await asyncio.sleep_ms(_SCAN_IDLE_MS)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print("BLE scan loop:", e)
                await asyncio.sleep_ms(500)


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
