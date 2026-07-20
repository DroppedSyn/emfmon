"""EMFMon battle addon.

OPTIONAL. The pet is fully functional without this module - app.py imports it
lazily inside a try/except, so any failure here can never affect the core pet.

Badge-to-badge battles run over ESP-NOW (the firmware's connectionless radio;
no WiFi network required, but both badges must be on the same channel). A solo
Practice mode works with one badge.

Outcome is decided by a small strength nudge on a forgiving coin-flip:
    P(lower-mac player wins) = clamp(0.5 + 0.04*(str_lo - str_hi), 0.25, 0.75)
Both badges exchange stats + a random nonce, XOR the nonces into one shared
seed, and each runs the SAME deterministic resolver + animation - so they agree
on the winner with no server and no live frame-sync.

CRITICAL: the ESP-NOW receive handler is registered under the EMFMon app on the
eventbus, and the bus KILLS the owning app if a handler raises. So _on_msg must
never propagate an exception, and every field off the wire is untrusted and
sanitised before use.
"""

import json
import math

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

try:
    from system.espnow import BROADCAST_MAC, espnow_service
    from system.espnow.events import EspNowReceiveEvent
    _HAVE_ESPNOW = True
except Exception:  # networking unavailable - Practice still works
    _HAVE_ESPNOW = False
    BROADCAST_MAC = b"\xff\xff\xff\xff\xff\xff"

BATTLES_PATH = _DIR + "/battles.json"
MAGIC = b"EMFB1"         # protocol tag/version; ignore anything not starting with it

# --- outcome tuning --------------------------------------------------------
STRENGTH_NUDGE = 0.04    # per point of strength difference
P_WIN_MIN = 0.25         # even the weakest mon wins at least this often
P_WIN_MAX = 0.75         # ...and the strongest never more than this (forgiving)
BATTLE_MIN_HEALTH = 100.0  # must be fully healed to battle
WIN_HEALTH = 75.0        # winner is knocked back to this
LOSE_HEALTH = 25.0       # loser is knocked back to this
MAX_LOG = 20             # battle history entries kept

# --- networking timing (ms) ------------------------------------------------
HELLO_MS = 900           # broadcast "here I am" while searching
INVITE_MS = 700          # resend an invite while waiting for an answer
STATS_MS = 350           # resend stats while exchanging (best-effort radio)
PEER_STALE_MS = 4000     # forget a nearby badge not heard from in this long
MAX_PEERS = 12           # cap the discovery list (guards against HELLO flooding)
NO_PEERS_HINT_MS = 5000  # show the "same WiFi?" hint after searching this long
INVITE_TIMEOUT_MS = 9000
# keep <= INVITE_TIMEOUT so the invitee's prompt closes no later than the
# inviter gives up (else a late accept lands on a peer who already bailed)
INVITED_TIMEOUT_MS = 8000
EXCHANGE_TIMEOUT_MS = 12000  # generous: best-effort radio needs retries to converge
CONFIRM_TAIL_MS = 1500   # keep confirming stats this far into the animation

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


def resolve(my_mac, my_str, opp_mac, opp_str, seed):
    """Return True if *I* win. Symmetric: run on both badges with the MACs
    swapped and the same seed, each gets the consistent result."""
    # tuple-of-ints comparison (MicroPython-safe; avoids relying on bytes '<')
    i_am_lo = tuple(my_mac) < tuple(opp_mac)
    str_lo = my_str if i_am_lo else opp_str
    str_hi = opp_str if i_am_lo else my_str
    nxt = _xorshift(seed)
    roll = nxt() / 4294967296.0
    p_lo = 0.5 + STRENGTH_NUDGE * (str_lo - str_hi)
    p_lo = min(P_WIN_MAX, max(P_WIN_MIN, p_lo))
    lo_wins = roll < p_lo
    return lo_wins == i_am_lo


# --- untrusted-input sanitisers (everything off the wire) ------------------
def _clean_name(x):
    if isinstance(x, str) and x:
        return x[:8]
    return "???"


def _clean_colour(c):
    if (
        isinstance(c, list)
        and len(c) == 3
        and all(isinstance(v, (int, float)) for v in c)
    ):
        return [min(1.0, max(0.0, float(v))) for v in c]
    return [0.6, 0.6, 0.6]


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
        return {"w": 0, "l": 0, "log": [], "pw": 0, "pl": 0}


class Battle:
    """Owns the battle view: menu, records, discovery/handshake, and animation.

    States: menu | info | records | rec_ranked | rec_practice | searching |
            inviting | invited | exchanging | anim | result
    """

    def __init__(self, app):
        self.app = app
        self.done = False
        self.records = _load_records()
        self.menu_items = ("Practice", "Find opponent", "Records", "Back")
        self.menu_idx = 0
        self.rec_items = ("Ranked", "Practice", "Back")
        self.rec_idx = 0
        self.rec_scroll = 0        # scroll offset in the ranked opponent log
        self.message = ""
        self.state = "menu"
        # per-battle
        self.opp = None            # {"name","shape","colour","strength","mac"}
        self.i_won = False
        self.is_practice = False   # practice is free: no HP change, no W/L
        self.my_str = 5            # OWN strength, snapshotted per battle (see below)
        self._invite_return = "menu"  # state to return to if an invite is declined
        self._input_lock = 0.0     # brief button lockout after arriving at a menu
        self.anim_t = 0.0
        self.my_bar = 100.0
        self.opp_bar = 100.0
        self._hits_done = 0        # shots whose damage we've already applied
        self._flash_my = 0.0       # ms of hit-flash left on each bar
        self._flash_opp = 0.0
        # networking
        self.net = _HAVE_ESPNOW
        self._sub = None
        mac = _own_mac()
        if mac is None:
            # without our real STA MAC we can't order players consistently with a
            # peer (they see our real MAC), so disable networked play - Practice
            # only. The random id below is just a local placeholder for Practice.
            self.net = False
            mac = bytes(random.getrandbits(8) for _ in range(6))
        self._own_mac = mac
        self.peers = {}            # mac(bytes) -> {"name","seen"}
        self.peer_idx = 0
        self.engaged_mac = None    # peer we're inviting / invited-by / exchanging
        self.engaged_name = ""
        self.my_nonce = 0
        self.peer_stats = None     # sanitised {"name","shape","colour","strength","nonce"}
        self.peer_has_mine = False
        self._search_t = 0.0
        self._send_t = 0.0
        self._to_t = 0.0
        # Register our OWN button handler (the proven app_components.Menu pattern)
        # rather than relying on EMFMon delegating - held ref so we can remove it.
        # ESP-NOW is subscribed LAZILY (only when you pick Find opponent), so
        # Practice/Records never touch the radio.
        self._input_handler = self._handle_input
        eventbus.on_async(ButtonDownEvent, self._input_handler, self.app)

    def _ensure_net(self):
        # bring up the ESP-NOW listener on demand (keeps the radio asleep for
        # Practice/menu; returns True if networking is available)
        if not self.net:
            return False
        if self._sub is None:
            try:
                self._sub = espnow_service.subscribe(
                    self._on_msg, self.app, predicate=_is_battle_msg
                )
            except Exception as e:
                print("Battle: espnow subscribe failed:", e)
                self.net = False
                return False
        return True

    # --- input handler (own eventbus registration) -------------------------
    async def _handle_input(self, event):
        # MUST NOT raise: the eventbus stops the owning app if a handler throws.
        try:
            self.on_button(event)
        except Exception as e:
            print("Battle: input error:", e)

    # --- lifecycle ---------------------------------------------------------
    def close(self):
        if self.engaged_mac is not None:
            self._send(self.engaged_mac, {"t": "X"})  # let the peer bail early
        try:
            eventbus.remove(ButtonDownEvent, self._input_handler, self.app)
        except Exception as e:
            print("Battle: input unsubscribe failed:", e)
        if self._sub is not None:
            try:
                eventbus.remove(EspNowReceiveEvent, self._sub, self.app)
            except Exception as e:
                print("Battle: unsubscribe failed:", e)
            self._sub = None

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

    # --- networking send + receive ----------------------------------------
    def _send(self, mac, obj):
        if not self.net:
            return
        try:
            espnow_service.send(MAGIC + bytes(json.dumps(obj), "utf-8"), mac)
        except Exception as e:
            print("Battle: send failed:", e)

    def _send_stats(self):
        pet = self.app.pet
        self._send(self.engaged_mac, {
            "t": "S",
            "n": self._my_name(),
            "sh": pet.get("shape", "circle"),
            "c": [round(float(v), 3) for v in pet.get("colour", [0.6, 0.6, 0.6])],
            "st": self.my_str,   # snapshotted at _start_exchange (determinism)
            "nc": self.my_nonce,
            "gy": self.peer_stats is not None,  # "got yours"
        })

    def _on_msg(self, event):
        # BULLETPROOF: the eventbus stops the owning app if a handler raises.
        try:
            self._handle(event)
        except Exception as e:
            print("Battle: msg error:", e)

    def _handle(self, event):
        mac = bytes(event.mac)
        if mac == self._own_mac:
            return
        obj = json.loads(event.msg[len(MAGIC):].decode())
        if not isinstance(obj, dict):
            return
        t = obj.get("t")
        if t == "H":  # a nearby badge advertising
            # only while actively searching, and capped so a flood of spoofed
            # HELLOs can't grow the dict without bound (aged in _update_searching)
            if self.state == "searching" and (
                mac in self.peers or len(self.peers) < MAX_PEERS
            ):
                self.peers[mac] = {"name": _clean_name(obj.get("n")), "seen": 0.0}
            return
        if t == "I":  # invite
            if self.state in ("menu", "searching"):
                self._invite_return = self.state  # go back here if we decline
                self.engaged_mac = mac
                self.engaged_name = _clean_name(obj.get("n"))
                self._to_t = 0.0
                self.state = "invited"
            elif self.state == "inviting" and mac == self.engaged_mac:
                self._start_exchange()  # mutual invite - both want it
            return
        # remaining message types only from the peer we're engaged with
        if self.engaged_mac is None or mac != self.engaged_mac:
            return
        if t == "A":  # accept
            if self.state == "inviting":
                self._start_exchange()
        elif t == "N":  # decline
            if self.state == "inviting":
                self.message = self.engaged_name + "\ndeclined."
                self._end_session("info")
        elif t == "S":  # stats
            if self.state == "exchanging":
                self._recv_stats(obj)
            elif self.state == "inviting":
                # I invited them, so I've already consented; their ACCEPT may have
                # been lost but their stats prove they accepted, so proceed. We
                # NEVER auto-start from "invited" - that side must press Accept.
                self._start_exchange()
                if self.state == "exchanging":
                    self._recv_stats(obj)
        elif t == "X":  # peer left / aborted
            if self.state in ("inviting", "invited", "exchanging"):
                self.message = self.engaged_name + "\nleft."
                self._end_session("info")

    # --- battle setup ------------------------------------------------------
    def _start_practice(self):
        reason = self._gate_reason()
        if reason is not None:
            self.message = reason
            self.state = "info"
            return
        opp_mac = bytes(random.getrandbits(8) for _ in range(6))
        self.opp = {
            "name": _random_name(),
            "shape": random.choice(SHAPES),
            "colour": _random_colour(),
            "strength": random.randint(2, 9),
            "mac": opp_mac,
        }
        self.my_str = _clean_strength(self.app.pet.get("strength", 5))
        seed = random.getrandbits(32)
        self.engaged_mac = None  # practice: no live peer to confirm with
        self.is_practice = True  # free: no HP change, no record
        self.i_won = resolve(
            self._own_mac, self.my_str, opp_mac, self.opp["strength"], seed
        )
        self._begin_anim()

    def _start_exchange(self):
        if self.state == "exchanging":
            return
        reason = self._gate_reason()  # re-check my own eligibility
        if reason is not None:
            self._send(self.engaged_mac, {"t": "X"})
            self.message = reason
            self._end_session("info")
            return
        self.my_nonce = random.getrandbits(30)
        # snapshot own strength ALONGSIDE the nonce so a fitness tick mid-exchange
        # can't make the two badges resolve with different strengths (both send
        # AND resolve from this frozen value)
        self.my_str = _clean_strength(self.app.pet.get("strength", 5))
        self.peer_stats = None
        self.peer_has_mine = False
        self.is_practice = False  # a real, networked battle - counts + costs HP
        self._send_t = 0.0
        self._to_t = 0.0
        self.state = "exchanging"

    def _recv_stats(self, obj):
        if self.peer_stats is None:
            self.peer_stats = {
                "name": _clean_name(obj.get("n")),
                "shape": _clean_shape(obj.get("sh")),
                "colour": _clean_colour(obj.get("c")),
                "strength": _clean_strength(obj.get("st")),
                "nonce": _clean_nonce(obj.get("nc")),
            }
        self.peer_has_mine = bool(obj.get("gy"))
        # resolve once BOTH badges hold both nonces (I have theirs; they have mine)
        if self.peer_stats is not None and self.peer_has_mine:
            seed = (self.my_nonce ^ self.peer_stats["nonce"]) & 0xFFFFFFFF
            self.opp = {
                "name": self.peer_stats["name"],
                "shape": self.peer_stats["shape"],
                "colour": self.peer_stats["colour"],
                "strength": self.peer_stats["strength"],
                "mac": self.engaged_mac,
            }
            self.i_won = resolve(
                self._own_mac, self.my_str, self.engaged_mac,
                self.peer_stats["strength"], seed,
            )
            self._begin_anim()  # keeps engaged_mac so we tail-confirm to the peer

    def _begin_anim(self):
        self.anim_t = 0.0
        self.my_bar = 100.0
        self.opp_bar = 100.0
        self._hits_done = 0
        self._flash_my = 0.0
        self._flash_opp = 0.0
        self._send_t = 0.0
        self.state = "anim"

    def _enter_searching(self):
        # always reset discovery state so we never show a stale peer list
        self.peers = {}
        self.peer_idx = 0
        self._search_t = 0.0
        self._send_t = HELLO_MS  # broadcast immediately
        self.state = "searching"

    def _end_session(self, new_state):
        self.engaged_mac = None
        self.engaged_name = ""
        self.peer_stats = None
        self.peer_has_mine = False
        self._to_t = 0.0
        self._send_t = 0.0
        self.state = new_state

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
        pet["health"] = WIN_HEALTH if self.i_won else LOSE_HEALTH
        if self.i_won:
            rec["w"] += 1
        else:
            rec["l"] += 1
        rec["log"].insert(0, {
            "o": self.opp.get("name", "???") if self.opp else "???",
            "r": "W" if self.i_won else "L",
        })
        rec["log"] = rec["log"][:MAX_LOG]
        self._save_records()
        try:
            self.app._save_state()
        except Exception as e:
            print("Battle: save pet failed:", e)

    def _save_records(self):
        try:
            with open(BATTLES_PATH, "w") as f:
                f.write(json.dumps(self.records))
        except Exception as e:
            print("Battle: save records failed:", e)

    # --- update ------------------------------------------------------------
    def update(self, delta):
        if self._input_lock > 0.0:
            self._input_lock = max(0.0, self._input_lock - delta)
        st = self.state
        if st == "searching":
            self._update_searching(delta)
        elif st == "inviting":
            self._to_t += delta
            self._send_t += delta
            if self._send_t >= INVITE_MS:
                self._send_t = 0.0
                self._send(self.engaged_mac, {"t": "I", "n": self._my_name()})
            if self._to_t >= INVITE_TIMEOUT_MS:
                self.message = "No answer.\nSame WiFi? Move\ncloser & retry."
                self._end_session("info")
        elif st == "invited":
            self._to_t += delta
            if self._to_t >= INVITED_TIMEOUT_MS:
                self._end_session(self._invite_return)  # prompt expired
        elif st == "exchanging":
            self._to_t += delta
            self._send_t += delta
            if self._send_t >= STATS_MS:
                self._send_t = 0.0
                self._send_stats()
            if self._to_t >= EXCHANGE_TIMEOUT_MS:
                self._send(self.engaged_mac, {"t": "X"})
                self.message = "Lost them!\nStay close, same\nWiFi, rematch."
                self._end_session("info")
        elif st == "anim":
            self._update_anim(delta)

    def _update_searching(self, delta):
        self._search_t += delta
        self._send_t += delta
        for m in list(self.peers.keys()):        # age out stale badges
            self.peers[m]["seen"] += delta
            if self.peers[m]["seen"] > PEER_STALE_MS:
                del self.peers[m]
        if self._send_t >= HELLO_MS:
            self._send_t = 0.0
            self._send(BROADCAST_MAC, {"t": "H", "n": self._my_name()})
        n = len(self.peers)
        if self.peer_idx >= n:
            self.peer_idx = max(0, n - 1)

    def _update_anim(self, delta):
        # tail-confirm stats so the peer also converges (networked battles only)
        if self.engaged_mac is not None and self.anim_t < CONFIRM_TAIL_MS:
            self._send_t += delta
            if self._send_t >= STATS_MS:
                self._send_t = 0.0
                self._send_stats()
        self.anim_t += delta
        self._flash_my = max(0.0, self._flash_my - delta)
        self._flash_opp = max(0.0, self._flash_opp - delta)
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
            self.engaged_mac = None  # battle over; stop talking to the peer
            self._apply_result()
            self.state = "result"

    # --- input -------------------------------------------------------------
    def on_button(self, event):
        # NB: unlike the pet view we do NOT ignore the joystick centre here - in
        # a plain menu it's a perfectly good CONFIRM (JOYFIRE carries CONFIRM).
        if self._input_lock > 0.0:
            return  # brief lockout after arriving at a menu (anti double-tap)
        st = self.state
        if st == "menu":
            self._menu_button(event)
        elif st == "searching":
            self._searching_button(event)
        elif st == "invited":
            if BUTTON_TYPES["CONFIRM"] in event.button:
                self._send(self.engaged_mac, {"t": "A", "n": self._my_name()})
                self._start_exchange()
            elif BUTTON_TYPES["CANCEL"] in event.button:
                self._send(self.engaged_mac, {"t": "N"})
                self._end_session(self._invite_return)
        elif st == "inviting":
            if BUTTON_TYPES["CANCEL"] in event.button:
                self._send(self.engaged_mac, {"t": "X"})
                self._end_session("searching")
        elif st == "exchanging":
            if BUTTON_TYPES["CANCEL"] in event.button:
                self._send(self.engaged_mac, {"t": "X"})
                self._end_session("searching")
        elif st == "info":
            if _any_button(event):  # terminal screen - any key dismisses
                self.state = "menu"
        elif st == "records":
            self._records_button(event)
        elif st == "rec_ranked":
            self._rec_ranked_button(event)
        elif st == "rec_practice":
            if _any_button(event):
                self.state = "records"
        elif st == "anim":
            # only allow skipping a solo Practice battle - skipping a networked
            # one could desync the peer's result, so let it play out
            if self.engaged_mac is None and BUTTON_TYPES["CANCEL"] in event.button:
                self._finish_anim()
        elif st == "result":
            if _any_button(event):
                self.opp = None
                self.state = "menu"
        # a brief lockout when we land on a menu, so a late second press from
        # mashing "any key" can't immediately trigger a menu selection
        if self.state != st and self.state in ("menu", "records"):
            self._input_lock = 250.0

    def _finish_anim(self):
        # snap bars/flash to their final state and apply the result exactly once
        if self.i_won:
            self.my_bar, self.opp_bar = _WINNER_END_BAR, 0.0
        else:
            self.my_bar, self.opp_bar = 0.0, _WINNER_END_BAR
        self._flash_my = 0.0
        self._flash_opp = 0.0
        self._apply_result()
        self.state = "result"

    def _menu_button(self, event):
        if BUTTON_TYPES["CANCEL"] in event.button:
            self.done = True
        elif BUTTON_TYPES["UP"] in event.button:
            self.menu_idx = (self.menu_idx - 1) % len(self.menu_items)
        elif BUTTON_TYPES["DOWN"] in event.button:
            self.menu_idx = (self.menu_idx + 1) % len(self.menu_items)
        elif BUTTON_TYPES["CONFIRM"] in event.button:
            self._menu_select(self.menu_items[self.menu_idx])

    def _menu_select(self, item):
        if item == "Practice":
            self._start_practice()
        elif item == "Find opponent":
            if not self._ensure_net():
                self.message = "Networking off\non this badge."
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
        elif item == "Back":
            self.done = True

    def _searching_button(self, event):
        macs = self._peer_list()
        if BUTTON_TYPES["CANCEL"] in event.button:
            self.state = "menu"
        elif BUTTON_TYPES["UP"] in event.button:
            if macs:
                self.peer_idx = (self.peer_idx - 1) % len(macs)
        elif BUTTON_TYPES["DOWN"] in event.button:
            if macs:
                self.peer_idx = (self.peer_idx + 1) % len(macs)
        elif BUTTON_TYPES["CONFIRM"] in event.button:
            if macs:
                mac = macs[self.peer_idx % len(macs)]
                self.engaged_mac = mac
                self.engaged_name = self.peers[mac]["name"]
                self._to_t = 0.0
                self._send_t = INVITE_MS  # send invite immediately
                self.state = "inviting"

    def _peer_list(self):
        # stable, deterministic order (MicroPython dicts are NOT insertion-ordered)
        return sorted(self.peers.keys(), key=lambda m: tuple(m))

    _REC_ROWS = 6  # ranked-log rows visible at once

    def _records_button(self, event):
        if BUTTON_TYPES["CANCEL"] in event.button:
            self.state = "menu"
        elif BUTTON_TYPES["UP"] in event.button:
            self.rec_idx = (self.rec_idx - 1) % len(self.rec_items)
        elif BUTTON_TYPES["DOWN"] in event.button:
            self.rec_idx = (self.rec_idx + 1) % len(self.rec_items)
        elif BUTTON_TYPES["CONFIRM"] in event.button:
            sel = self.rec_items[self.rec_idx]
            if sel == "Ranked":
                self.rec_scroll = 0
                self.state = "rec_ranked"
            elif sel == "Practice":
                self.state = "rec_practice"
            else:  # Back
                self.state = "menu"

    def _rec_ranked_button(self, event):
        log = self.records.get("log", [])
        max_scroll = max(0, len(log) - self._REC_ROWS)
        if BUTTON_TYPES["CANCEL"] in event.button:
            self.state = "records"
        elif BUTTON_TYPES["UP"] in event.button:
            self.rec_scroll = max(0, self.rec_scroll - 1)
        elif BUTTON_TYPES["DOWN"] in event.button:
            self.rec_scroll = min(max_scroll, self.rec_scroll + 1)

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
        elif st == "inviting":
            self._draw_waiting(ctx, "Inviting", self.engaged_name)
        elif st == "invited":
            self._draw_invited(ctx)
        elif st == "exchanging":
            self._draw_waiting(ctx, "Battling", self.engaged_name)
        elif st in ("anim", "result"):
            self._draw_battle(ctx)

    def _draw_menu(self, ctx):
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 20
        ctx.move_to(0, -70).text("Battle!")
        ctx.font_size = 15
        for i, item in enumerate(self.menu_items):
            y = -26 + i * 24
            if i == self.menu_idx:
                ctx.rgb(0.9, 0.7, 0.1).move_to(0, y).text("> " + item + " <")
            else:
                set_color(ctx, "label")
                ctx.move_to(0, y).text(item)
        # control hint - the A/D petals move, C selects, F exits
        ctx.font_size = 11
        ctx.rgb(0.6, 0.6, 0.6).move_to(0, 96).text("A/D move  C pick  F back")

    def _draw_info(self, ctx):
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 16
        lines = self.message.split("\n")
        for i, line in enumerate(lines):
            ctx.move_to(0, -20 + i * 22).text(line)
        ctx.font_size = 12
        ctx.rgb(0.6, 0.6, 0.6).move_to(0, 90).text("any key")

    def _draw_records(self, ctx):
        # submenu: choose which record to view
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 19
        ctx.move_to(0, -70).text("Records")
        ctx.font_size = 16
        for i, item in enumerate(self.rec_items):
            y = -20 + i * 26
            if i == self.rec_idx:
                ctx.rgb(0.9, 0.7, 0.1).move_to(0, y).text("> " + item + " <")
            else:
                set_color(ctx, "label")
                ctx.move_to(0, y).text(item)
        ctx.font_size = 11
        ctx.rgb(0.6, 0.6, 0.6).move_to(0, 96).text("A/D move  C pick  F back")

    def _draw_rec_ranked(self, ctx):
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 17
        ctx.move_to(0, -96).text("Ranked")
        ctx.font_size = 22
        ctx.rgb(0.2, 0.8, 0.35).move_to(-30, -70).text("%dW" % self.records.get("w", 0))
        ctx.rgb(0.9, 0.25, 0.25).move_to(30, -70).text("%dL" % self.records.get("l", 0))
        ctx.font_size = 13
        log = self.records.get("log", [])
        if not log:
            set_color(ctx, "label")
            ctx.move_to(0, -6).text("No ranked fights yet")
        else:
            start = min(self.rec_scroll, max(0, len(log) - self._REC_ROWS))
            for row, e in enumerate(log[start:start + self._REC_ROWS]):
                won = e.get("r") == "W"
                ctx.rgb(*((0.2, 0.8, 0.35) if won else (0.9, 0.25, 0.25)))
                tag = "W vs " if won else "L vs "
                ctx.move_to(0, -44 + row * 18).text(tag + str(e.get("o", "???")))
            if len(log) > self._REC_ROWS:
                ctx.font_size = 11
                ctx.rgb(0.6, 0.6, 0.6).move_to(0, 78).text(
                    "A/D scroll  %d-%d/%d" % (start + 1,
                                              min(len(log), start + self._REC_ROWS),
                                              len(log))
                )
        ctx.font_size = 11
        ctx.rgb(0.6, 0.6, 0.6).move_to(0, 100).text("F back")

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
        ctx.font_size = 11
        ctx.rgb(0.6, 0.6, 0.6).move_to(0, 100).text("any key")

    def _draw_searching(self, ctx):
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 17
        ctx.move_to(0, -96).text("Find opponent")
        ctx.font_size = 11
        ch = _wifi_channel()
        ctx.rgb(0.55, 0.55, 0.6).move_to(0, -78).text(
            "WiFi ch %s" % (ch if ch is not None else "-")
        )
        macs = self._peer_list()
        if not macs:
            ctx.font_size = 14
            set_color(ctx, "label")
            ctx.move_to(0, -30).text("Searching...")
            if self._search_t >= NO_PEERS_HINT_MS:
                ctx.font_size = 13
                ctx.rgb(0.9, 0.7, 0.1)
                for i, line in enumerate(
                    ("No badges nearby.", "Both on the same", "WiFi? (or both off)")
                ):
                    ctx.move_to(0, 6 + i * 18).text(line)
        else:
            ctx.font_size = 14
            # scroll a 5-row window so the highlighted peer is always visible
            start = 0
            if len(macs) > 5:
                start = min(max(0, self.peer_idx - 2), len(macs) - 5)
            for row, mac in enumerate(macs[start:start + 5]):
                idx = start + row
                y = -46 + row * 22
                name = self.peers[mac]["name"]
                if idx == self.peer_idx:
                    ctx.rgb(0.9, 0.7, 0.1).move_to(0, y).text("> " + name + " <")
                else:
                    set_color(ctx, "label")
                    ctx.move_to(0, y).text(name)
            ctx.font_size = 11
            ctx.rgb(0.6, 0.6, 0.6).move_to(0, 96).text("C: invite  CANCEL: back")

    def _draw_waiting(self, ctx, verb, name):
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 18
        ctx.move_to(0, -20).text(verb)
        ctx.font_size = 20
        ctx.rgb(0.9, 0.7, 0.1).move_to(0, 8).text(str(name))
        # simple animated dots
        dots = "." * (1 + int(self._to_t / 400) % 3)
        ctx.font_size = 18
        set_color(ctx, "label")
        ctx.move_to(0, 36).text(dots)
        ctx.font_size = 11
        ctx.rgb(0.6, 0.6, 0.6).move_to(0, 96).text("CANCEL: stop")

    def _draw_invited(self, ctx):
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 20
        ctx.rgb(0.9, 0.7, 0.1).move_to(0, -30).text(str(self.engaged_name))
        set_color(ctx, "label")
        ctx.font_size = 16
        ctx.move_to(0, -4).text("wants to battle!")
        ctx.font_size = 13
        ctx.rgb(0.2, 0.8, 0.35).move_to(0, 40).text("C: accept")
        ctx.rgb(0.9, 0.3, 0.3).move_to(0, 62).text("CANCEL: decline")

    def _draw_battle(self, ctx):
        pet = self.app.pet
        opp = self.opp or {}
        mx, my = -46, 34
        ox, oy = 46, -34
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
        self._draw_bar(ctx, -70, 70, self.my_bar, (0.3, 0.6, 1.0), self._flash_my)
        self._draw_bar(ctx, 14, -78, self.opp_bar, (1.0, 0.5, 0.3), self._flash_opp)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 12
        set_color(ctx, "label")
        ctx.move_to(mx, my + 34).text(str(pet.get("name", "you")))
        ctx.move_to(ox, oy - 34).text(str(opp.get("name", "???")))
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

    def _draw_bar(self, ctx, x, y, val, col, flash=0.0):
        ctx.rgb(0.25, 0.25, 0.25).rectangle(x, y, 56, 7).fill()
        if flash > 0.0:
            f = min(1.0, flash / _HIT_FLASH_MS)  # brighten toward white on a hit
            col = (col[0] + (1.0 - col[0]) * f,
                   col[1] + (1.0 - col[1]) * f,
                   col[2] + (1.0 - col[2]) * f)
        ctx.rgb(*col).rectangle(x, y, 56 * _clamp01(val / 100.0), 7).fill()

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
        ctx.rgb(0.6, 0.6, 0.6).move_to(0, 100).text("any key")


# --- module helpers --------------------------------------------------------
def _clamp01(v):
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def _confirm_or_cancel(event):
    return (
        BUTTON_TYPES["CONFIRM"] in event.button
        or BUTTON_TYPES["CANCEL"] in event.button
    )


def _any_button(event):
    b = event.button
    return any(
        BUTTON_TYPES[k] in b
        for k in ("UP", "DOWN", "LEFT", "RIGHT", "CONFIRM", "CANCEL")
    )


def _clean_nonce(x):
    try:
        return int(x) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return 0


def _is_battle_msg(event):
    try:
        return event.msg[:len(MAGIC)] == MAGIC
    except Exception:
        return False


def _wifi_channel():
    if not _HAVE_ESPNOW:
        return None
    try:
        return espnow_service.wifi_channel
    except Exception:
        return None


def _own_mac():
    """This badge's real STA MAC (6 bytes), or None if it can't be read (the
    caller then disables networked play - a random MAC wouldn't match what the
    peer sees and would break winner agreement)."""
    try:
        import network
        mac = network.WLAN(network.STA_IF).config("mac")
        if isinstance(mac, (bytes, bytearray)) and len(mac) == 6:
            return bytes(mac)
    except Exception:
        pass
    return None
