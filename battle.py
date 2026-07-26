"""EMFMon battle addon.

OPTIONAL. The pet is fully functional without this module - app.py imports it
lazily inside a try/except, so any failure here can never affect the core pet.

Badge-to-badge battles use BLE (see ble_link.py) - it discovers peers on fixed
advertising channels every device scans, so it works regardless of WiFi (unlike
the old ESP-NOW transport, whose channel followed each badge's WiFi association
and couldn't find peers in the field). A solo Practice mode works with one badge.

Outcome is decided by a small strength nudge on a forgiving coin-flip:
    P(lower-id player wins) = clamp(0.5 + 0.04*(str_lo - str_hi), 0.25, 0.75)
For a networked battle both badges share one seed and run the SAME deterministic
resolver + animation, so they agree on the winner with no server.

CRITICAL: the ButtonDownEvent handler is registered under the EMFMon app on the
eventbus, and the bus KILLS the owning app if a handler raises. So _handle_input
must never propagate an exception.

DISCOVERY (this stage): 'PVP' advertises EMFMon:<name> and scans,
listing nearby searching badges closest-first. The GATT invite/accept/stats
handshake is the next stage - challenging a peer currently shows a note.
"""

import gc
import json
import math
import struct

from app_components import clear_background
from app_components.tokens import set_color
from events.input import BUTTON_TYPES, ButtonDownEvent
from system.eventbus import eventbus

from .app import (
    SHAPES,
    _DIR,
    _fill_polygon,
    _fill_star,
    _life_stage,
    _random_colour,
    _random_name,
)

try:
    import random
except Exception:  # pragma: no cover - always present on-badge
    random = None

from .arcmenu import (
    ArcMenu,
    arc_text_layout,
    draw_arc_text,
    draw_hints,
    pulse_k,
    ticks_diff,
    ticks_ms,
)

try:
    from .ble_link import BleLink
    _HAVE_BLE = True
except Exception:  # BLE unavailable - Practice / Records still work
    _HAVE_BLE = False
    BleLink = None

BATTLES_PATH = _DIR + "/battles.json"

# --- outcome tuning --------------------------------------------------------
STRENGTH_NUDGE = 0.04    # per point of strength difference
P_WIN_MIN = 0.25         # even the weakest mon wins at least this often
P_WIN_MAX = 0.75         # ...and the strongest never more than this (forgiving)
BATTLE_MIN_HEALTH = 100.0  # must be fully healed to battle
WIN_HEALTH = 75.0        # winner is knocked back to this
LOSE_HEALTH = 25.0       # loser is knocked back to this
MAX_LOG = 20             # battle history entries kept

# --- discovery -------------------------------------------------------------
NO_PEERS_HINT_MS = 5000  # show a hint after searching this long with nobody found

# --- animation timing (ms) -------------------------------------------------
_INTRO_MS = 900
_VOLLEY_MS = 3000
_FINISH_MS = 900
_ANIM_MS = _INTRO_MS + _VOLLEY_MS + _FINISH_MS
_SHOT_MS = 500
_SHOTS = _VOLLEY_MS // _SHOT_MS      # shots exchanged in a volley (6)
_LOSER_HITS = (_SHOTS + 1) // 2      # winner fires on even shots -> hits the loser
_WINNER_HITS = _SHOTS // 2           # loser fires on odd shots -> hits the winner
_WINNER_END_BAR = 45.0               # winner's health bar left at the end
_HIT_FLASH_MS = 200                  # a bar flashes white this long after a hit

# --- battle screen ---------------------------------------------------------
# Health bars are ARCS hugging the bezel: ours in the bottom-left corner
# (nearest the holder), theirs in the top-right, diagonally opposite - the same
# diagonal the mons and the projectile already use. Angles run from +x with y
# down, so they increase clockwise: 90 deg = bottom, 180 = left, 270 = top.
_BAR_R = 104.0                    # arc radius
_BAR_T = 9                        # stroke thickness
_MY_ARC = (100.0 * math.pi / 180, 172.0 * math.pi / 180)    # bottom-left
_OPP_ARC = (280.0 * math.pi / 180, 352.0 * math.pi / 180)   # top-right
_BAR_H = 9
_BAR_TRACK = (0.16, 0.16, 0.18)
_BAR_GHOST = (0.55, 0.12, 0.12)   # damage just taken, draining away behind
_BAR_HI = (0.25, 0.80, 0.35)      # health colour bands - the bar says how bad
_BAR_MID = (0.95, 0.70, 0.15)     # it is without anyone reading a number
_BAR_LO = (0.90, 0.25, 0.20)
_GHOST_MS = 320.0                 # ms for the ghost to catch up to a hit
# Each combatant is a column: mon, then name, then bar - all on one x, so a
# glance ties the three together. Mine bottom-left, theirs top-right.
_MY_XY = (-46, 34)
_OPP_XY = (46, -34)
_MY_NAME_Y = 62
_MY_BAR_Y = 78
_OPP_NAME_Y = -62
_OPP_BAR_Y = -78


def _bar_colour(v):
    if v > 55.0:
        return _BAR_HI
    if v > 25.0:
        return _BAR_MID
    return _BAR_LO


def _xorshift(seed):
    """Deterministic 32-bit PRNG - identical on both badges for a given seed."""
    state = [seed & 0xFFFFFFFF or 0x1A2B3C4D]

    def nxt():
        x = state[0]
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        x &= 0xFFFFFFFF
        state[0] = x
        return x

    return nxt


def resolve(my_id, my_str, opp_id, opp_str, seed):
    """Return True if *I* win. Symmetric: run on both badges with the ids
    swapped and the same seed, each gets the consistent result."""
    # tuple-of-ints comparison (MicroPython-safe; avoids relying on bytes '<')
    i_am_lo = tuple(my_id) < tuple(opp_id)
    str_lo = my_str if i_am_lo else opp_str
    str_hi = opp_str if i_am_lo else my_str
    nxt = _xorshift(seed)
    roll = nxt() / 4294967296.0
    p_lo = 0.5 + STRENGTH_NUDGE * (str_lo - str_hi)
    p_lo = min(P_WIN_MAX, max(P_WIN_MIN, p_lo))
    lo_wins = roll < p_lo
    return lo_wins == i_am_lo


# --- untrusted-input sanitisers (used now for names, and by the handshake) --
def _clean_name(x):
    if isinstance(x, str) and x:
        return x[:8]
    return "???"


def _clean_strength(x):
    try:
        return min(10, max(1, int(x)))
    except (TypeError, ValueError):
        return 5


def _clean_shape(x):
    return x if x in SHAPES else "circle"


def _draw_mon(ctx, x, y, s, shape, colour, fainted=False):
    try:
        r, g, b = colour
    except Exception:
        r, g, b = 0.6, 0.6, 0.6
    ctx.rgb(r, g, b)
    shape = _clean_shape(shape)
    if shape == "square":
        ctx.rectangle(x - s, y - s, 2 * s, 2 * s).fill()
    elif shape == "triangle":
        ctx.begin_path()
        ctx.move_to(x, y - s)
        ctx.line_to(x + s, y + s)
        ctx.line_to(x - s, y + s)
        ctx.close_path()
        ctx.fill()
    elif shape == "diamond":
        _fill_polygon(ctx, x, y, s, 4, -math.pi / 2)
    elif shape == "pentagon":
        _fill_polygon(ctx, x, y, s, 5, -math.pi / 2)
    elif shape == "hexagon":
        _fill_polygon(ctx, x, y, s, 6, 0.0)
    elif shape == "octagon":
        _fill_polygon(ctx, x, y, s, 8, math.pi / 8)
    elif shape == "star":
        _fill_star(ctx, x, y, s)
    else:
        ctx.arc(x, y, s, 0, 2 * math.pi, False).fill()
    ex = s * 0.34
    ey = y - s * 0.15
    if fainted:
        ctx.rgb(0, 0, 0)
        ctx.line_width = max(1.0, s * 0.09)
        er = s * 0.16
        for sx in (-ex, ex):
            cx = x + sx
            ctx.begin_path()
            ctx.move_to(cx - er, ey - er)
            ctx.line_to(cx + er, ey + er)
            ctx.move_to(cx + er, ey - er)
            ctx.line_to(cx - er, ey + er)
            ctx.stroke()
    else:
        for sx in (-ex, ex):
            ctx.rgb(1, 1, 1).arc(x + sx, ey, s * 0.2, 0, 2 * math.pi, False).fill()
            ctx.rgb(0, 0, 0).arc(x + sx, ey, s * 0.09, 0, 2 * math.pi, False).fill()


def blank_records():
    """A fresh, empty record - what a newly hatched mon starts with."""
    return {"w": 0, "l": 0, "log": [], "pw": 0, "pl": 0}


def archive_records():
    """Take the current mon's battle record and clear the file for the next one.

    Records used to be global: battles.json was never reset, so a new mon
    inherited its predecessor's W/L and opponent log. Now app.py calls this when
    a mon dies or is replaced, stores the returned snapshot on the history entry,
    and the successor starts from zero.
    """
    rec = _load_records()
    try:
        with open(BATTLES_PATH, "w") as f:
            f.write(json.dumps(blank_records()))
    except Exception as e:
        print("Battle: reset records failed:", e)
    return rec


def _load_records():
    try:
        with open(BATTLES_PATH) as f:
            data = json.loads(f.read())
        if not isinstance(data, dict):
            raise ValueError("bad records")
        log = data.get("log")
        if isinstance(log, list):
            log = [e for e in log if isinstance(e, dict)]  # drop malformed rows
        else:
            log = []
        return {
            "w": max(0, int(data.get("w", 0))),      # ranked wins
            "l": max(0, int(data.get("l", 0))),      # ranked losses
            "log": log,                               # ranked opponent history
            "pw": max(0, int(data.get("pw", 0))),    # practice wins
            "pl": max(0, int(data.get("pl", 0))),    # practice losses
        }
    except Exception:
        return blank_records()


class Battle:
    """Owns the battle view: menu, records, BLE discovery, and animation.

    States: menu | info | records | rec_ranked | rec_practice | searching |
            invited | handshaking | anim | result.
    """

    def __init__(self, app, records=None, state="menu", title=None,
                 opened_by=None):
        self.app = app
        self.done = False
        # `records` set = read-only view of a PAST mon's archived record, opened
        # from Menu -> History. It never loads or writes battles.json, and
        # CANCEL closes the view outright instead of walking back up our menus.
        self.archived = records is not None
        self.records = records if self.archived else _load_records()
        self.rec_title = title     # past mon's name, shown instead of "Ranked"
        self.menu_items = ("Practice", "PVP", "Records", "Back")
        self.menu_idx = 0
        self.rec_items = ("Ranked", "Practice", "Back")
        self.rec_idx = 0
        self.rec_scroll = 0        # scroll offset in the ranked opponent log
        # List screens render through the app's single shared ArcMenu. _arc_state
        # is the state its contents were last loaded for, so set_items() runs
        # once per transition and NEVER per frame (a per-frame list rebuild is
        # what fragmented the heap in _update_searching - see peer_list()).
        self._arc_state = None
        self._own_arc = None       # only used if the app has no arcmenu
        self.message = ""
        self.state = state
        # per-battle
        self.opp = None            # {"name","shape","colour","strength","id"}
        self.i_won = False
        self.is_practice = False   # practice is free: no HP change, no W/L
        self.my_str = 5            # OWN strength, snapshotted per battle
        # The press that opened us is still in flight: we subscribe our own
        # ButtonDownEvent handler below, and the eventbus delivers that same
        # press to it afterwards. Left alone it lands on the fresh menu and
        # instantly picks row 0 (Practice).
        #
        # This CANNOT be solved with a timer: the press arrives after the first
        # update(), and that update carries a large delta (building this object
        # + its BLE link), so a lockout counted in deltas is already spent. We
        # ignore the event OBJECT itself - exact, no window to tune.
        self._opened_by = opened_by
        # Screen-change lockout, measured in WALL CLOCK, not accumulated delta.
        # It used to be decremented by update()'s delta and was observed on-badge
        # draining a full 250ms in a single frame, which let a double-tap blow
        # straight through whatever screen had just appeared (Records->Practice
        # flashing back out, gate-reason screens flashing past).
        self._lock_t0 = ticks_ms()
        self._lock_ms = 250
        self.anim_t = 0.0
        self.my_bar = 100.0
        self.opp_bar = 100.0
        # Ghost bars lag behind the real ones, so a hit leaves a red sliver that
        # drains away - you can see how much that shot cost you.
        self._ghost_my = 100.0
        self._ghost_opp = 100.0
        self._combat_rgb = None    # (myR,myG,myB, oppR,oppG,oppB), built once
        self._hits_done = 0        # shots whose damage we've already applied
        self._flash_my = 0.0       # ms of hit-flash left on each bar
        self._flash_opp = 0.0
        # a stable local id (for Practice's resolve ordering; networked battles
        # will use the real BLE address). Random per session is fine.
        self._own_id = bytes(random.getrandbits(8) for _ in range(6))
        # BLE discovery
        # an archived record is a static screen - it never discovers or fights,
        # so it has no business holding a BLE link
        self.ble = BleLink(self._my_name()) if (_HAVE_BLE and not self.archived) else None
        self._peers = []           # cached closest-first list while searching
        # Pre-rendered visible rows for the search screen. Built only when the
        # peer list or the selection actually changes - the draw path used to
        # slice the list and concatenate a label per row EVERY frame, which is
        # the allocation pattern that fragmented the heap here before.
        self._peer_rows = []
        self._peer_rows_dirty = True
        # same treatment for the ranked-records screen
        self._rec_rows = []
        self._rec_head = ("0W", "0L")
        self._rec_foot = None
        self._rec_rows_dirty = True
        self._title_layout = None  # curved title, measured on first draw
        self.peer_idx = 0
        self._search_t = 0.0
        self._peer_refresh = 0.0   # throttle peer_list() (it allocates - fragments)
        # GATT handshake (stage 2)
        self._hs = None            # in-flight ble_link.Handshake handle
        self._invite_peer = None   # peer we're challenging / who's challenging us
        self._my_nonce = 0         # our nonce this handshake (seed = ours ^ theirs)
        self._decline_addr = None  # briefly ignore re-invites from a declined peer
        self._decline_ms = 0.0
        # Register our OWN button handler (the proven app_components.Menu pattern)
        # rather than relying on EMFMon delegating - held ref so we can remove it.
        self._input_handler = self._handle_input
        eventbus.on_async(ButtonDownEvent, self._input_handler, self.app)

    # --- input lockout -----------------------------------------------------
    def _arm_input_lock(self, ms=250):
        """Ignore buttons for `ms` of REAL time. Wall clock, not accumulated
        delta: a single slow frame used to spend the whole budget at once."""
        self._lock_t0 = ticks_ms()
        self._lock_ms = ms

    def _input_locked(self):
        return ticks_diff(ticks_ms(), self._lock_t0) < self._lock_ms

    # --- input handler (own eventbus registration) -------------------------
    async def _handle_input(self, event):
        # MUST NOT raise: the eventbus stops the owning app if a handler throws.
        try:
            self.on_button(event)
        except Exception as e:
            print("Battle: input error:", e)

    # --- lifecycle ---------------------------------------------------------
    def close(self):
        if self.ble is not None:
            try:
                self.ble.stop()
                self.ble.cancel_handshake()
            except Exception as e:
                print("Battle: ble stop:", e)
        try:
            eventbus.remove(ButtonDownEvent, self._input_handler, self.app)
        except Exception as e:
            print("Battle: input unsubscribe failed:", e)

    def _my_name(self):
        return _clean_name(self.app.pet.get("name", "???"))

    def _gate_reason(self):
        pet = self.app.pet
        if not pet.get("alive"):
            return "Your pet has\ndied."
        if _life_stage(pet.get("age", 0)) not in ("adult", "elder"):
            return "Must be an ADULT\nto battle (age 6h+)."
        if pet.get("health", 0) < BATTLE_MIN_HEALTH:
            return "Must be FULLY\nHEALED to battle."
        return None

    # --- battle setup ------------------------------------------------------
    def _start_practice(self):
        reason = self._gate_reason()
        if reason is not None:
            self.message = reason
            self.state = "info"
            return
        opp_id = bytes(random.getrandbits(8) for _ in range(6))
        self.opp = {
            "name": _random_name(),
            "shape": random.choice(SHAPES),
            "colour": _random_colour(),
            "strength": random.randint(2, 9),
            "id": opp_id,
        }
        self.my_str = _clean_strength(self.app.pet.get("strength", 5))
        seed = random.getrandbits(32)
        self.is_practice = True  # free: no HP change, no record
        self.i_won = resolve(
            self._own_id, self.my_str, opp_id, self.opp["strength"], seed
        )
        self._begin_anim()

    def _begin_anim(self):
        self.anim_t = 0.0
        self.my_bar = 100.0
        self.opp_bar = 100.0
        self._ghost_my = 100.0
        self._ghost_opp = 100.0
        self._combat_rgb = None    # new opponent, new colours
        self._hits_done = 0
        self._flash_my = 0.0
        self._flash_opp = 0.0
        self.state = "anim"

    # --- handshake wire format --------------------------------------------
    # 17 bytes: name[8] shape[1] rgb[3] str[1] nonce[4]. With the 1-byte tag the
    # write is 18B - this MUST stay <=20 (default ATT MTU 23 - 3) or writes
    # silently truncate. Growing this format requires an MTU exchange first.
    _STATS_FMT = "<8sBBBBBI"

    def _pack_stats(self, nonce):
        pet = self.app.pet
        name = _clean_name(pet.get("name", "???")).encode()[:8]
        name = name + b"\x00" * (8 - len(name))
        shape = _clean_shape(pet.get("shape", "circle"))
        shape_idx = SHAPES.index(shape) if shape in SHAPES else SHAPES.index("circle")
        try:
            r, g, b = (min(255, max(0, int(c * 255)))
                       for c in pet.get("colour", [0.6, 0.6, 0.6])[:3])
        except Exception:
            r, g, b = 153, 153, 153
        strv = _clean_strength(pet.get("strength", 5))
        return struct.pack(self._STATS_FMT, name, shape_idx, r, g, b,
                           strv, nonce & 0xFFFFFFFF)

    def _unpack_stats(self, blob):
        """Decode + SANITISE an opponent's stats blob (untrusted wire data).
        Returns a dict, or None if malformed."""
        try:
            name_b, shape_idx, r, g, b, strv, nonce = struct.unpack(
                self._STATS_FMT, bytes(blob)[:struct.calcsize(self._STATS_FMT)]
            )
        except Exception:
            return None
        try:
            name = name_b.rstrip(b"\x00").decode()
        except Exception:
            name = ""
        shape = SHAPES[shape_idx] if shape_idx < len(SHAPES) else "circle"
        return {
            "name": _clean_name(name),
            "shape": _clean_shape(shape),
            "colour": [r / 255.0, g / 255.0, b / 255.0],
            "strength": _clean_strength(strv),
            "nonce": nonce & 0xFFFFFFFF,
        }

    def _apply_result(self):
        rec = self.records
        if self.is_practice:
            # free: no HP change, tracked as a simple separate tally (no names)
            if self.i_won:
                rec["pw"] = rec.get("pw", 0) + 1
            else:
                rec["pl"] = rec.get("pl", 0) + 1
            self._save_records()
            return
        pet = self.app.pet
        if not pet.get("alive", True):
            return   # pet died mid-handshake - don't apply a ranked HP/W-L result
        pet["health"] = WIN_HEALTH if self.i_won else LOSE_HEALTH
        if self.i_won:
            rec["w"] += 1
        else:
            rec["l"] += 1
        rec["log"].insert(0, {
            "o": self.opp.get("name", "???") if self.opp else "???",
            "r": "W" if self.i_won else "L",
            # OUR pet's age in hours at the moment of the fight, so a trainer can
            # see how young their mon was when it took a scalp. Not a wall-clock
            # date: the badge RTC is never NTP-synced outside an OTA check, so
            # time.localtime() reads 2000-01-01 and a real date would be a lie.
            "a": int(pet.get("age", 0)),
        })
        rec["log"] = rec["log"][:MAX_LOG]
        self._save_records()
        try:
            self.app._save_state()
        except Exception as e:
            print("Battle: save pet failed:", e)

    def _save_records(self):
        if self.archived:
            return  # a past mon's record is history: never write it back
        try:
            with open(BATTLES_PATH, "w") as f:
                f.write(json.dumps(self.records))
        except Exception as e:
            print("Battle: save records failed:", e)

    # --- discovery ---------------------------------------------------------
    def _enter_searching(self):
        self.peer_idx = 0
        self._search_t = 0.0
        self._peer_refresh = 0.0
        self._peers = []
        self._peer_rows = []
        self._peer_rows_dirty = True
        self._decline_addr = None    # fresh session: don't carry a stale suppress
        self._decline_ms = 0.0
        if self.ble is not None:
            self.ble.start_discovery()
        self.state = "searching"

    def _leave_searching(self, new_state):
        if self.ble is not None:
            self.ble.stop()
        self.state = new_state

    # --- update ------------------------------------------------------------
    def update(self, delta):
        # (the input lockout is wall-clock now - nothing to tick down here)
        st = self.state
        if st == "searching":
            self._update_searching(delta)
        elif st == "invited":
            self._update_invited(delta)
        elif st == "handshaking":
            self._update_handshaking(delta)
        elif st == "anim":
            self._update_anim(delta)

    def _update_searching(self, delta):
        self._search_t += delta
        # refresh the peer list only a few times a second (not every frame) -
        # peer_list() allocates a fresh sorted list and pruning the dict, and
        # doing that 60x/s fragmented the heap into an OOM reboot after minutes
        self._peer_refresh += delta
        if self.ble is not None and self._peer_refresh >= 400:
            self._peer_refresh = 0.0
            # Remember which badge is highlighted so the selection tracks the
            # SAME peer across RSSI reshuffles/evictions (index isn't identity -
            # otherwise a reshuffle could make CONFIRM challenge the wrong badge).
            sel_addr = None
            if 0 <= self.peer_idx < len(self._peers):
                sel_addr = self._peers[self.peer_idx].get("addr")
            self._peers = self.ble.peer_list()   # closest (strongest) first
            if sel_addr is not None:
                for i, p in enumerate(self._peers):
                    if p.get("addr") == sel_addr:
                        self.peer_idx = i
                        break
            self._peer_rows_dirty = True
            gc.collect()                          # keep the heap tidy
        n = len(self._peers)
        if self.peer_idx >= n:
            self.peer_idx = max(0, n - 1)
        # Someone challenging us? (a fresh invite beacon addressed to this badge)
        if self._decline_ms > 0.0:
            self._decline_ms = max(0.0, self._decline_ms - delta)
        if self.ble is not None:
            inv = self.ble.pending_invite()
            if inv is not None and not (
                self._decline_ms > 0.0 and inv["addr"] == self._decline_addr
            ):
                self._invite_peer = inv
                self.state = "invited"

    def _update_anim(self, delta):
        self.anim_t += delta
        self._flash_my = max(0.0, self._flash_my - delta)
        self._flash_opp = max(0.0, self._flash_opp - delta)
        # ease the ghosts down toward the real bars (no allocation, no state
        # beyond these two floats)
        e = delta / _GHOST_MS
        if e > 1.0:
            e = 1.0
        if self._ghost_my > self.my_bar:
            self._ghost_my += (self.my_bar - self._ghost_my) * e
        else:
            self._ghost_my = self.my_bar
        if self._ghost_opp > self.opp_bar:
            self._ghost_opp += (self.opp_bar - self._ghost_opp) * e
        else:
            self._ghost_opp = self.opp_bar
        # Health drops in a STEP each time a projectile lands (not a smooth
        # drain). A shot fired at t=k*_SHOT_MS lands at (k+1)*_SHOT_MS; even
        # shots hit the loser, odd shots hit the winner.
        volley_t = self.anim_t - _INTRO_MS
        landed = 0 if volley_t <= 0 else min(_SHOTS, int(volley_t / _SHOT_MS))
        while self._hits_done < landed:
            shot = self._hits_done
            hit_loser = (shot % 2 == 0)
            hit_mine = hit_loser != self.i_won  # is the bar that got hit mine?
            if hit_mine:
                self._flash_my = _HIT_FLASH_MS
            else:
                self._flash_opp = _HIT_FLASH_MS
            self._hits_done += 1
        loser_hits = (landed + 1) // 2
        winner_hits = landed // 2
        loser_bar = max(0.0, 100.0 - loser_hits * (100.0 / _LOSER_HITS))
        winner_bar = max(
            0.0, 100.0 - winner_hits * ((100.0 - _WINNER_END_BAR) / _WINNER_HITS)
        )
        if self.i_won:
            self.my_bar, self.opp_bar = winner_bar, loser_bar
        else:
            self.my_bar, self.opp_bar = loser_bar, winner_bar
        if self.anim_t >= _ANIM_MS:
            self._apply_result()
            self.state = "result"

    # --- input -------------------------------------------------------------
    def on_button(self, event):
        # NB: unlike the pet view we do NOT ignore the joystick centre here - in
        # a plain menu it's a perfectly good CONFIRM (JOYFIRE carries CONFIRM).
        if self._opened_by is not None:
            # only ever true for the very first press we are handed; if the
            # opening press was never re-delivered, this simply clears itself
            was_opener = event is self._opened_by
            self._opened_by = None
            if was_opener:
                return
        if self._input_locked():
            return  # brief lockout after arriving at a screen (anti double-tap)
        st = self.state
        if st == "menu":
            self._menu_button(event)
        elif st == "searching":
            self._searching_button(event)
        elif st == "invited":
            self._invited_button(event)
        elif st == "handshaking":
            if BUTTON_TYPES["CANCEL"] in event.button:
                self._abort_handshake("Cancelled")
        elif st == "info":
            if BUTTON_TYPES["CANCEL"] in event.button:
                self.state = "menu"
        elif st == "records":
            self._records_button(event)
        elif st == "rec_ranked":
            self._rec_ranked_button(event)
        elif st == "rec_practice":
            if BUTTON_TYPES["CANCEL"] in event.button:
                self.state = "records"
        elif st == "anim":
            # Practice battles can be skipped; a networked one (later) must play
            # out so the peer's result stays in sync.
            if self.is_practice and BUTTON_TYPES["CANCEL"] in event.button:
                self._finish_anim()
        elif st == "result":
            if BUTTON_TYPES["CANCEL"] in event.button:
                self.opp = None
                self.state = "menu"
        # Whenever a button just moved us to a NEW screen, briefly ignore input
        # so the same physical press (or a bounce/double-tap) can't blow through
        # the screen we just landed on. This is what makes the gate-reason info
        # screen ("must be fully healed", etc) linger instead of flashing past.
        if self.state != st:
            self._arm_input_lock()

    def _finish_anim(self):
        # snap bars/flash to their final state and apply the result exactly once
        if self.i_won:
            self.my_bar, self.opp_bar = _WINNER_END_BAR, 0.0
            self._ghost_my, self._ghost_opp = _WINNER_END_BAR, 0.0
        else:
            self.my_bar, self.opp_bar = 0.0, _WINNER_END_BAR
            self._ghost_my, self._ghost_opp = 0.0, _WINNER_END_BAR
        self._flash_my = 0.0
        self._flash_opp = 0.0
        self._apply_result()
        self.state = "result"

    # --- shared curved menu ------------------------------------------------
    _ARC_HINT_C = "C pick"

    def _arc(self):
        """The app's one ArcMenu. Falls back to our own if the host app doesn't
        provide one, so battle.py stays a self-contained optional addon."""
        m = getattr(self.app, "arcmenu", None)
        if m is None:
            if self._own_arc is None:
                self._own_arc = ArcMenu()
            m = self._own_arc
        return m

    @staticmethod
    def _visible(items):
        """Drop a trailing 'Back' row - F backs out, as in every other menu."""
        return list(items[:-1]) if items and items[-1] == "Back" else list(items)

    def _sync_arc(self):
        """Load the current list-state into the shared menu. Guarded by
        _arc_state so it runs once per transition, never per frame."""
        st = self.state
        if st == self._arc_state:
            return
        if st == "menu":
            src, idx = self.menu_items, self.menu_idx
        elif st == "records":
            src, idx = self.rec_items, self.rec_idx
        else:
            self._arc_state = st
            return
        m = self._arc()
        m.set_items(self._visible(src))
        m.idx = min(max(0, idx), max(0, len(m.items) - 1))
        m.side = "left"
        m.hint_c = self._ARC_HINT_C
        m.hint_f = "F back"
        self._arc_state = st

    def _menu_button(self, event):
        self._sync_arc()
        m = self._arc()
        act = m.button(event)
        self.menu_idx = m.idx
        if act == "back":
            self.done = True
        elif act == "select":
            self._menu_select(m.items[m.idx])

    def _menu_select(self, item):
        if item == "Practice":
            self._start_practice()
        elif item == "PVP":
            if self.ble is None:
                self.message = "Bluetooth not\navailable here."
                self.state = "info"
                return
            reason = self._gate_reason()
            if reason is not None:
                self.message = reason
                self.state = "info"
                return
            self._enter_searching()
        elif item == "Records":
            self.rec_idx = 0
            self.state = "records"
        # NB: no "Back" branch - _visible() strips that row, F backs out instead.

    def _searching_button(self, event):
        peers = self._peers
        if BUTTON_TYPES["CANCEL"] in event.button:
            self._leave_searching("menu")
        elif BUTTON_TYPES["UP"] in event.button:
            if peers:
                self.peer_idx = (self.peer_idx - 1) % len(peers)
                self._peer_rows_dirty = True
        elif BUTTON_TYPES["DOWN"] in event.button:
            if peers:
                self.peer_idx = (self.peer_idx + 1) % len(peers)
                self._peer_rows_dirty = True
        elif BUTTON_TYPES["CONFIRM"] in event.button:
            if peers:
                peer = peers[self.peer_idx % len(peers)]
                self._start_invite(peer)

    # --- GATT handshake (stage 2) -----------------------------------------
    def _start_invite(self, peer):
        """Challenger: go connectable + invite this peer, then swap stats."""
        if not self._handshake_ready():
            return
        self._invite_peer = peer
        self._my_nonce = random.getrandbits(32)
        self.my_str = _clean_strength(self.app.pet.get("strength", 5))
        blob = self._pack_stats(self._my_nonce)
        # start_invite() stops discovery internally, then advertises the invite.
        self._hs = self.ble.start_invite(peer, blob)
        self.message = "Challenging\n" + peer.get("name", "???") + "..."
        self.state = "handshaking"

    def _update_invited(self, delta):
        # Auto-dismiss the CHALLENGE! popup if the challenger stops advertising
        # (walks away / gives up). Discovery is still running here, so a stale
        # invite means they're gone - drop back to the peer list.
        if self.ble is not None and self.ble.pending_invite() is None:
            self._invite_peer = None
            self.state = "searching"

    def _invited_button(self, event):
        if BUTTON_TYPES["CANCEL"] in event.button:
            # Decline: briefly ignore this peer, resume searching.
            peer = self._invite_peer
            if peer is not None:
                self._decline_addr = peer.get("addr")
                # Suppress longer than the challenger's 15s invite window so a
                # declined invite can't immediately re-pop while it's still live.
                self._decline_ms = 16000.0
            self._invite_peer = None
            if self.ble is not None:
                self.ble.clear_invite()
            self.state = "searching"
        elif BUTTON_TYPES["CONFIRM"] in event.button:
            self._accept_invite()

    def _accept_invite(self):
        """Acceptor: connect to the challenger and swap stats."""
        peer = self._invite_peer
        if peer is None:
            self.state = "searching"
            return
        if self.ble is not None:
            self.ble.clear_invite()
        if not self._handshake_ready():
            return
        self._my_nonce = random.getrandbits(32)
        self.my_str = _clean_strength(self.app.pet.get("strength", 5))
        blob = self._pack_stats(self._my_nonce)
        self._hs = self.ble.start_accept(peer, blob)
        self.message = "Connecting to\n" + peer.get("name", "???") + "..."
        self.state = "handshaking"

    def _handshake_ready(self):
        """Gate + BLE availability check shared by invite/accept. On failure it
        parks on an info screen (stopping discovery) and returns False."""
        if self.ble is None:
            self.message = "Bluetooth not\navailable here."
            self._leave_searching("info")
            return False
        reason = self._gate_reason()
        if reason is not None:
            self.message = reason
            self._leave_searching("info")
            return False
        return True

    def _update_handshaking(self, delta):
        hs = self._hs
        if hs is None:
            self._abort_handshake("Link error")
        elif hs.status == "exchanged":
            self._on_exchange_done(hs)
        elif hs.status == "failed":
            self._abort_handshake(hs.error or "Lost them!")

    def _on_exchange_done(self, hs):
        stats = self._unpack_stats(hs.peer_blob)
        if stats is None:
            self._abort_handshake("Bad data")
            return
        my_n = self._my_nonce & 0xFFFFFFFF
        peer_n = stats["nonce"] & 0xFFFFFFFF
        # Equal nonces would make BOTH badges order themselves the same way
        # (neither is "lo") -> they'd disagree on the winner, and seed=0 hits the
        # xorshift constant fallback. Astronomically rare for honest badges
        # (1/2^32) but a malicious peer could force it: reject and abort.
        if my_n == peer_n:
            self._abort_handshake("Bad data")
            return
        seed = (my_n ^ peer_n) & 0xFFFFFFFF
        # Order the two players by their EXCHANGED NONCES (both badges hold the
        # exact same pair) rather than by BLE address. This makes the winner
        # identical on both badges BY CONSTRUCTION - no dependency on the
        # controller presenting the same address over-air as config('mac')
        # returns. (Equal nonces = 1/2^32, negligible.)
        my_id = struct.pack("<I", my_n)
        peer_id = struct.pack("<I", peer_n)
        self.opp = {
            "name": stats["name"],
            "shape": stats["shape"],
            "colour": stats["colour"],
            "strength": stats["strength"],
            "id": hs.peer_addr,     # informational only (resolve uses nonces)
        }
        self.is_practice = False   # networked = ranked (HP + W/L cost)
        self.i_won = resolve(my_id, self.my_str, peer_id, stats["strength"], seed)
        self._hs = None
        self._invite_peer = None
        self._begin_anim()

    def _abort_handshake(self, msg):
        if self.ble is not None:
            try:
                self.ble.cancel_handshake()
            except Exception as e:
                print("Battle: hs cancel:", e)
        self._hs = None
        self._invite_peer = None
        self.message = msg + "\nStay close, retry."
        self.state = "info"

    _REC_ROWS = 6  # ranked-log rows visible at once

    def _records_button(self, event):
        self._sync_arc()
        m = self._arc()
        act = m.button(event)
        self.rec_idx = m.idx
        if act == "back":
            self.state = "menu"
        elif act == "select":
            sel = m.items[m.idx]
            if sel == "Ranked":
                self.rec_scroll = 0
                self._rec_rows_dirty = True
                self.state = "rec_ranked"
            elif sel == "Practice":
                self.state = "rec_practice"

    def _rec_ranked_button(self, event):
        log = self.records.get("log", [])
        max_scroll = max(0, len(log) - self._REC_ROWS)
        if BUTTON_TYPES["CANCEL"] in event.button:
            if self.archived:
                # opened straight into this screen from the History menu, so
                # there is no records menu behind it - close the view instead
                self.done = True
            else:
                self.state = "records"
        elif BUTTON_TYPES["UP"] in event.button:
            self.rec_scroll = max(0, self.rec_scroll - 1)
            self._rec_rows_dirty = True
        elif BUTTON_TYPES["DOWN"] in event.button:
            self.rec_scroll = min(max_scroll, self.rec_scroll + 1)
            self._rec_rows_dirty = True

    # --- drawing -----------------------------------------------------------
    def draw(self, ctx):
        clear_background(ctx)
        st = self.state
        if st == "menu":
            self._draw_menu(ctx)
        elif st == "info":
            self._draw_info(ctx)
        elif st == "records":
            self._draw_records(ctx)
        elif st == "rec_ranked":
            self._draw_rec_ranked(ctx)
        elif st == "rec_practice":
            self._draw_rec_practice(ctx)
        elif st == "searching":
            self._draw_searching(ctx)
        elif st == "invited":
            self._draw_invited(ctx)
        elif st == "handshaking":
            self._draw_handshaking(ctx)
        elif st in ("anim", "result"):
            self._draw_battle(ctx)

    _TITLE_RGB = (0.95, 0.18, 0.18)   # red, pulsing on the shared heartbeat
    _RING_RGB = (0.85, 0.12, 0.12)
    _RING_W = 3                       # stroke width, px
    _RING_R = 120 - _RING_W / 2 - 1   # centre it just inside the bezel
    # Title curls around the TOP-RIGHT arc: the menu rows hug the left, so the
    # right side is free. Radius is set in from the ring's inner edge so the
    # glyphs sit inside it with a clear gap rather than touching it.
    _TITLE_TEXT = "BATTLE MODE"
    _TITLE_SIZE = 20
    _TITLE_R = _RING_R - _RING_W / 2 - 18
    _TITLE_MID = 52 * math.pi / 180   # clockwise from 12 o'clock

    def _draw_ring(self, ctx):
        """Red rim around the screen edge - frames battle mode. Drawn AFTER the
        menu so the scrim doesn't dim it."""
        ctx.line_width = self._RING_W
        ctx.rgb(*self._RING_RGB)
        ctx.begin_path()
        ctx.arc(0, 0, self._RING_R, 0, 2 * math.pi, False)
        ctx.stroke()

    def _draw_menu(self, ctx):
        self._sync_arc()
        m = self._arc()
        m.draw(ctx, hint=False)      # first: it lays down the scrim
        self._draw_ring(ctx)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        if self._title_layout is None:
            # measured once - the title never changes
            self._title_layout = arc_text_layout(
                ctx, self._TITLE_TEXT, self._TITLE_R,
                self._TITLE_MID, self._TITLE_SIZE)
        ctx.font_size = self._TITLE_SIZE
        k = pulse_k()                # same cadence as the selected menu row
        r, g, b = self._TITLE_RGB
        ctx.rgb(r * k, g * k, b * k)
        draw_arc_text(ctx, self._title_layout)
        m.draw_hint(ctx)             # last: the call-outs sit over the rim

    def _draw_info(self, ctx):
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 16
        lines = self.message.split("\n")
        for i, line in enumerate(lines):
            ctx.move_to(0, -20 + i * 22).text(line)
        draw_hints(ctx, f="F back")

    def _draw_records(self, ctx):
        self._sync_arc()
        m = self._arc()
        m.draw(ctx, hint=False)      # first: it lays down the scrim
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 19
        ctx.move_to(0, -96).text("Records")
        m.draw_hint(ctx)             # last: the call-outs stay on top

    def _build_rec_rows(self):
        """Pre-render the ranked screen's strings. Records are immutable while
        it's open, so this runs on entry and on scroll - not 60x/s."""
        log = self.records.get("log", [])
        start = min(self.rec_scroll, max(0, len(log) - self._REC_ROWS))
        rows = []
        for e in log[start:start + self._REC_ROWS]:
            won = e.get("r") == "W"
            age = e.get("a")
            rows.append((
                ("W vs " if won else "L vs ") + str(e.get("o", "???")),
                ("%dh" % age) if isinstance(age, int) else None,
                won,
            ))
        self._rec_rows = rows
        self._rec_head = ("%dW" % self.records.get("w", 0),
                          "%dL" % self.records.get("l", 0))
        self._rec_foot = None
        if len(log) > self._REC_ROWS:
            self._rec_foot = "A/D scroll  %d-%d/%d" % (
                start + 1, min(len(log), start + self._REC_ROWS), len(log))
        self._rec_rows_dirty = False

    def _draw_rec_ranked(self, ctx):
        if self._rec_rows_dirty:
            self._build_rec_rows()
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 17
        ctx.move_to(0, -96).text(self.rec_title or "Ranked")
        ctx.font_size = 22
        ctx.rgb(0.2, 0.8, 0.35).move_to(-30, -70).text(self._rec_head[0])
        ctx.rgb(0.9, 0.25, 0.25).move_to(30, -70).text(self._rec_head[1])
        ctx.font_size = 13
        if not self._rec_rows:
            set_color(ctx, "label")
            ctx.move_to(0, -6).text("No ranked fights yet")
        else:
            for row, (main, age_s, won) in enumerate(self._rec_rows):
                y = -44 + row * 18
                ctx.font_size = 13   # reset: the age suffix below shrinks it
                ctx.rgb(*((0.2, 0.8, 0.35) if won else (0.9, 0.25, 0.25)))
                ctx.move_to(0, y).text(main)
                # our mon's age at the time, tucked in small + grey after the
                # result. Rows are CENTER-aligned, so offset by half of each
                # string's width to butt it up against the right edge of main.
                if age_s is not None:
                    mw = ctx.text_width(main)
                    ctx.font_size = 10
                    ctx.rgb(0.55, 0.55, 0.55)
                    ctx.move_to(mw / 2 + 4 + ctx.text_width(age_s) / 2,
                                y).text(age_s)
            if self._rec_foot is not None:
                ctx.font_size = 11
                ctx.rgb(0.6, 0.6, 0.6).move_to(0, 78).text(self._rec_foot)
        draw_hints(ctx, f="F back")

    def _draw_rec_practice(self, ctx):
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 17
        ctx.move_to(0, -70).text("Practice")
        # just a big win / loss counter in the middle - no names
        ctx.font_size = 40
        ctx.rgb(0.2, 0.8, 0.35).move_to(-42, 4).text("%d" % self.records.get("pw", 0))
        set_color(ctx, "label")
        ctx.font_size = 26
        ctx.move_to(0, 4).text("-")
        ctx.rgb(0.9, 0.25, 0.25).move_to(42, 4).text("%d" % self.records.get("pl", 0))
        ctx.font_size = 14
        set_color(ctx, "label")
        ctx.move_to(0, 44).text("wins - losses")
        draw_hints(ctx, f="F back")

    def _build_peer_rows(self):
        """Pre-render the visible 5-row window: label, signal level, selected.
        Called on a peer refresh (<=2.5/s) or a selection move - never per frame.
        """
        peers = self._peers
        start = 0
        if len(peers) > 5:
            # keep the highlighted peer inside the window
            start = min(max(0, self.peer_idx - 2), len(peers) - 5)
        rows = []
        for row in range(start, min(start + 5, len(peers))):
            peer = peers[row]
            sel = row == self.peer_idx
            rows.append((
                ("> " if sel else "   ") + peer["name"],
                _signal_level(peer.get("rssi")),
                sel,
            ))
        self._peer_rows = rows
        self._peer_rows_dirty = False

    def _draw_searching(self, ctx):
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 17
        ctx.move_to(0, -96).text("PVP")
        ctx.font_size = 11
        ctx.rgb(0.35, 0.55, 0.95).move_to(0, -78).text("via Bluetooth")
        peers = self._peers
        if not peers:
            ctx.font_size = 14
            set_color(ctx, "label")
            ctx.move_to(0, -30).text("Searching...")
            if self._search_t >= NO_PEERS_HINT_MS:
                ctx.font_size = 12
                ctx.rgb(0.9, 0.7, 0.1)
                for i, line in enumerate(
                    ("No badges nearby.", "They must also be", "in PVP too.")
                ):
                    ctx.move_to(0, 6 + i * 18).text(line)
            draw_hints(ctx, f="F back")
        else:
            if self._peer_rows_dirty:
                self._build_peer_rows()
            for row, (label, level, sel) in enumerate(self._peer_rows):
                y = -48 + row * 22
                ctx.text_align = ctx.LEFT
                ctx.font_size = 14
                if sel:
                    ctx.rgb(0.9, 0.7, 0.1)
                else:
                    set_color(ctx, "label")
                ctx.move_to(-56, y).text(label)
                _draw_signal_bars(ctx, 42, y, level)
            ctx.text_align = ctx.CENTER
            draw_hints(ctx, c="C fight", f="F back")

    def _draw_invited(self, ctx):
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        peer = self._invite_peer or {}
        ctx.font_size = 20
        ctx.rgb(0.9, 0.7, 0.1).move_to(0, -50).text("CHALLENGE!")
        ctx.font_size = 16
        set_color(ctx, "label")
        ctx.move_to(0, -14).text(str(peer.get("name", "???")))
        ctx.move_to(0, 10).text("wants to battle")
        ctx.font_size = 12
        ctx.rgb(0.6, 0.6, 0.6).move_to(0, 96).text("C: accept   F: decline")

    def _draw_handshaking(self, ctx):
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 16
        for i, line in enumerate(self.message.split("\n")):
            ctx.move_to(0, -14 + i * 22).text(line)
        ctx.font_size = 12
        ctx.rgb(0.6, 0.6, 0.6).move_to(0, 96).text("F: cancel")

    def _combat_colours(self):
        """(myR,myG,myB, oppR,oppG,oppB), built once per battle. Opponent colour
        arrives over the air, so it is coerced defensively - and caching keeps
        that off the per-frame path."""
        if self._combat_rgb is None:
            self._combat_rgb = (
                _rgb3(self.app.pet.get("colour"))
                + _rgb3((self.opp or {}).get("colour")))
        return self._combat_rgb

    def _draw_battle(self, ctx):
        pet = self.app.pet
        opp = self.opp or {}
        mx, my = _MY_XY
        ox, oy = _OPP_XY
        mr, mg, mb, orr, og, ob = self._combat_colours()
        intro = _clamp01(self.anim_t / _INTRO_MS)
        my_dead = self.state == "result" and not self.i_won
        opp_dead = self.state == "result" and self.i_won
        mxx = mx - (1.0 - intro) * 60
        oxx = ox + (1.0 - intro) * 60
        _draw_mon(ctx, mxx, my, 20, pet.get("shape", "circle"),
                  pet.get("colour", [0.6, 0.6, 0.6]), fainted=my_dead)
        _draw_mon(ctx, oxx, oy, 20, opp.get("shape", "circle"),
                  opp.get("colour", [0.6, 0.6, 0.6]), fainted=opp_dead)
        if self.state == "anim" and _INTRO_MS < self.anim_t < _INTRO_MS + _VOLLEY_MS:
            self._draw_projectile(ctx, mx, my, ox, oy)
        self._draw_bar(ctx, _MY_ARC, self.my_bar, self._ghost_my,
                       self._flash_my, mr, mg, mb)
        self._draw_bar(ctx, _OPP_ARC, self.opp_bar, self._ghost_opp,
                       self._flash_opp, orr, og, ob)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 14
        # names in their own mon's colour - identity, not just a label
        ctx.rgb(mr, mg, mb).move_to(mx, _MY_NAME_Y).text(
            pet.get("name", "you"))
        ctx.rgb(orr, og, ob).move_to(ox, _OPP_NAME_Y).text(
            opp.get("name", "???"))
        if self.state == "result":
            self._draw_result_banner(ctx)

    def _draw_projectile(self, ctx, mx, my, ox, oy):
        t = self.anim_t - _INTRO_MS
        shot = int(t / _SHOT_MS)
        frac = (t % _SHOT_MS) / _SHOT_MS
        winner_fires = (shot % 2 == 0)
        if self.i_won == winner_fires:
            sx, sy, tx, ty, col = mx, my, ox, oy, (0.3, 0.6, 1.0)
        else:
            sx, sy, tx, ty, col = ox, oy, mx, my, (1.0, 0.5, 0.3)
        px = sx + (tx - sx) * frac
        py = sy + (ty - sy) * frac
        ctx.rgb(*col).arc(px, py, 5, 0, 2 * math.pi, False).fill()

    def _draw_bar(self, ctx, arc, val, ghost, flash, r, g, b):
        """A curved health bar: frame, track, ghost, fill - back to front.

        Each layer is one thick arc stroke rather than a rectangle, so the bar
        follows the bezel. It always drains toward its own corner, away from the
        middle of the screen.

        The frame takes the mon's OWN colour, tying bar to creature; the fill is
        graded by remaining health, so it reads at a glance without printing a
        number - which would mean building a string every frame.
        """
        a0, a1 = arc
        span = a1 - a0
        ctx.line_width = _BAR_T + 3          # frame, peeking out behind
        ctx.rgb(r * 0.85, g * 0.85, b * 0.85)
        ctx.begin_path()
        ctx.arc(0, 0, _BAR_R, a0, a1, False)
        ctx.stroke()
        ctx.line_width = _BAR_T
        ctx.rgb(*_BAR_TRACK)
        ctx.begin_path()
        ctx.arc(0, 0, _BAR_R, a0, a1, False)
        ctx.stroke()
        if ghost > val:
            ctx.rgb(*_BAR_GHOST)
            ctx.begin_path()
            ctx.arc(0, 0, _BAR_R, a0, a0 + span * _clamp01(ghost / 100.0), False)
            ctx.stroke()
        if val > 0.0:
            fr, fg, fb = _bar_colour(val)
            if flash > 0.0:                  # brighten toward white on a hit
                f = flash / _HIT_FLASH_MS
                if f > 1.0:
                    f = 1.0
                fr += (1.0 - fr) * f
                fg += (1.0 - fg) * f
                fb += (1.0 - fb) * f
            ctx.rgb(fr, fg, fb)
            ctx.begin_path()
            ctx.arc(0, 0, _BAR_R, a0, a0 + span * _clamp01(val / 100.0), False)
            ctx.stroke()

    def _draw_result_banner(self, ctx):
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 26
        if self.i_won:
            ctx.rgb(0.2, 0.85, 0.35).move_to(0, 0).text("YOU WIN!")
        else:
            ctx.rgb(0.9, 0.3, 0.3).move_to(0, 0).text("YOU LOSE")
        ctx.font_size = 13
        set_color(ctx, "label")
        if self.is_practice:
            ctx.move_to(0, 24).text("practice - no cost")
        else:
            hp = WIN_HEALTH if self.i_won else LOSE_HEALTH
            ctx.move_to(0, 24).text("HP -> %d" % int(hp))
        draw_hints(ctx, f="F back")


# --- module helpers --------------------------------------------------------
def _clamp01(v):
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def _rgb3(colour):
    """Coerce a stored/received colour to three floats. The opponent's arrives
    over the air, so it can be anything at all."""
    try:
        r, g, b = colour
        return float(r), float(g), float(b)
    except Exception:
        return 0.6, 0.6, 0.6



def _signal_level(rssi):
    # coarse 1-3 signal level from RSSI (dBm); always >=1 if it's in the list
    if rssi is None:
        return 1
    if rssi >= -55:
        return 3
    if rssi >= -72:
        return 2
    return 1


def _draw_signal_bars(ctx, x, y, level):
    # three rising bars, filled up to `level` (like a phone signal icon)
    for i in range(3):
        h = 3 + i * 3
        if i < level:
            ctx.rgb(0.2, 0.8, 0.35)
        else:
            ctx.rgb(0.32, 0.32, 0.32)
        ctx.rectangle(x + i * 5, y + 4 - h, 3, h).fill()
