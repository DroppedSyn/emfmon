"""EMFMon battle addon.

OPTIONAL. The pet is fully functional without this module - app.py imports it
lazily inside a try/except, so any failure here can never affect the core pet.

Badge-to-badge battles use BLE (see ble_link.py) - it discovers peers on fixed
advertising channels every device scans, so it works regardless of WiFi (unlike
the old ESP-NOW transport, whose channel followed each badge's WiFi association
and couldn't find peers in the field). A solo Practice mode works with one badge.

BATTLE_EVO!: a fight is a pure function of (both mons, both queues, one shared
seed). Actions auto-fire from a queue built BEFORE the fight, so once the first
tick runs no human input exists - which is why both badges can disconnect the
moment queues are known, simulate independently, and still agree on the winner
with no server and no round trips (plan 1).

There is NO Classic combat here. It was removed with rev 22 of the plan and
ships as the separate v1.0.23 line instead; a peer without 0xF011 is told it is
too old rather than being offered a downgrade (plan 2, constraint 16).

CRITICAL: the ButtonDownEvent handler is registered under the EMFMon app on the
eventbus, and the bus KILLS the owning app if a handler raises. So _handle_input
must never propagate an exception.
"""

import gc
import hashlib
import math
import struct

from app_components import clear_background
from app_components.tokens import set_color
from events.input import BUTTON_TYPES, ButtonDownEvent
from system.eventbus import eventbus

from .app import (
    SHAPES,
    TRAITS,
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

from .records import (
    MAX_LOG,
    OUT_DRAW,
    OUT_LOSE,
    OUT_NONE,
    OUT_WIN,
    TRAIT_ACTION,
    _load_records,
    battlepoints_for,
    load_trainer,
    save_records,
    save_trainer,
)

from .arcmenu import (
    ArcMenu,
    arc_text_layout,
    draw_arc_text,
    draw_hints,
    draw_joystick_icon,
    FONT_ROW,
    FONT_SEL,
    pulse_k,
    TRI_LEFT,
    TRI_RIGHT,
    ticks_diff,
    ticks_ms,
)

# ble_link is imported ON DEMAND - see _ensure_ble(). It is deliberately NOT
# imported here: doing so costs ~1.7 s on the badge (0.7 s compiling
# ble_link.py, plus ~1.0 s the first time anything pulls in aioble/asyncio) and
# all of it lands in ONE frame of the draw loop, against a 5 s task watchdog.

# --- outcome tuning --------------------------------------------------------
BATTLE_MIN_HEALTH = 100.0  # must be fully healed to battle
WIN_HEALTH = 75.0        # winner is knocked back to this
LOSE_HEALTH = 25.0       # loser is knocked back to this

# --- BATTLE_EVO! -----------------------------------------------------------
# The kill switch (plan 5.1) lives in ble_link.py, NOT here. It has to: it gates
# whether the 0xF011 characteristic is registered and whether 0xF012 is
# advertised, and ble_link cannot import this module without a cycle. There was
# briefly a second copy at this spot, harmlessly shadowed by the import - which
# is exactly the sort of thing that gets flipped in a hurry during a field
# failure and appears to do nothing. One definition, in ble_link.py.
#
# Wire versions (plan 5.2). proto_ver covers FRAMING only. rules_ver covers the
# combat model, and it is the more dangerous of the two: two badges running
# different ACTIONS tables exchange perfectly valid frames and then silently
# compute DIFFERENT fights from identical inputs, disagreeing about who won with
# no error anywhere.
#
# BUMP rules_ver on ANY change to ACTIONS, TRAIT_ACTION, the elder aura, tick
# length, the HP pool, the rotation, or the damage maths.
PROTO_VER = 1
RULES_VER = 1

# --- combat model (plan 4.4) ------------------------------------------------
# Every number here was derived by simulation over ~250,000 fights.
# tools/evosim.py reproduces the 4.4.7 baseline; re-run it and DIFF after any
# change rather than eyeballing one. Fights end with a median HP gap of ~10
# against ~20-damage hits, so small edges swing win rates hard.
TICK_MS = 250            # all costs are whole tick multiples, expressed in TICKS
CAP_TICKS = 80           # 20 s, then the higher HP wins (plan 5.7)
EVO_HP = 100             # fits one byte, for the animation buffer (plan 4.10)
ELDER_BONUS_HP = 2       # the one shared elder aura (plan 4.4.6)
REPEAT_PENALTY = 14      # percent off a repeated action (plan 4.4.3)

ATK, LEECH, SLOW, CHIP, HEAL, GUARD = range(6)

# Indexed by action id - a TUPLE, not a dict. Ids are contiguous 0..5, so this
# drops the hash lookup and the dict's overhead outright (plan 6.2.1 #1).
#
# IDS ARE WIRE FORMAT (plan 5.6). Never renumber; append only.
#         label        cost kind   power spread param param2
ACTIONS = (
    ("Tackle",    8,  ATK,   17, 6, 0,  0),
    ("Gobble",    12, LEECH, 15, 6, 30, 0),   # param = leech %
    ("Prank",     12, SLOW,  13, 4, 4,  14),  # param = delay add, param2 = cap
    ("Mud Sling", 18, CHIP,  13, 4, 20, 0),   # param = chip ticks
    ("Disinfect", 7,  HEAL,  10, 4, 0,  0),
    ("Brace",     8,  GUARD, 10, 0, 14, 0),   # power = flat cut, param = ticks
)

def action_effect(act):
    """The one line of an action's detail that differs per action (plan 8.1.3).

    Lives HERE, beside the kind constants it switches on, and is injected into
    the queue screen the way default_queue_for and innate_actions_for already
    are - queues.py must not import this module, which is what keeps the combat
    model off the screen that opens it.

    Built from the tuple's own fields, never retyped: `kind` decides which of
    param/param2 means anything, and this is the only place that mapping is
    written down in words rather than executed.

    May contain a newline. A compound effect does not fit on one line at the
    size this leads the body at, and wrapping keeps the second number rather
    than dropping it - the cap and the chip duration are exactly the sort of
    detail a player cannot get any other way.

    THE LABEL VARIES BY KIND, and that is not decoration: "DMG" on Disinfect
    would be a lie, and a screen that labels a heal as damage is worse than one
    that labels nothing. Damage, healing and blocking each say what they are.
    """
    _name, _cost, kind, power, spread, param, param2 = act
    if kind == GUARD:
        return "BLOCK: %d\nfor %d ticks" % (power, param)
    if kind == HEAL:
        return "HEAL: %d-%d" % (power, power + spread)
    dmg = ("DMG: %d" % power if not spread
           else "DMG: %d-%d" % (power, power + spread))
    if kind == LEECH:
        return "%s\nsteals %d%%" % (dmg, param)
    if kind == CHIP:
        return "%s\nbleeds %d ticks" % (dmg, param)
    if kind == SLOW:
        return "%s\ndelays %d (cap %d)" % (dmg, param, param2)
    return dmg


# One sentence each, for the moves list on the queue screen (plan 8.1.3). Kept
# HERE, immediately under the numbers they describe, so a retune of the model
# and the prose about it are the same edit - a blurb living in queues.py would
# still say "cheap" after Tackle stopped being.
#
# They say what the action is FOR, not what its fields are: the screen already
# prints cost, power and spread straight out of the tuple above, and a sentence
# that only restates them is a sentence that will one day contradict them.
#
# NOT part of rules_ver. These are display text - two badges disagreeing about
# a description still compute the same fight.
# Broken to fit CENTRED IN THE BODY of the moves screen, which is the full width
# now that the selector is one line at the top rather than a list down the side.
# Keep lines to ~26 characters: the screen is round, and these sit low enough
# that the chord is noticeably shorter than the diameter.
ACTION_BLURB = (
    "Cheap and steady. It fires\noften, and often wins.",
    "Steals what it hits. Slow,\nbut it pays you back.",
    "Sets them back a turn.\nBuys ticks, not damage.",
    "Leaves them bleeding.\nDear, and it keeps working.",
    "Heals you, and comes round\nfaster than anything else.",
    "Blunts their next hits.\nCheap, and spoils big ones.",
)
# TRAIT_ACTION is imported from records.py (see the import block above), not
# defined here. It is the one table this module and the PET-DEATH path both need -
# plan 14.3 grants the trait action when a mon dies or is retired - and that path
# must not import the combat model to read it (plan 6.2.2/6.2.3, the 1126 ms
# blocking compile that rebooted a badge). It is re-exported by this import, so
# `from emfmon.battle import TRAIT_ACTION` still works.
#
# records.py also derives N_COLLECTIBLE from it, so the collectible count cannot
# drift from the table. N_ACTIONS there still restates len(ACTIONS) below.

QUEUE_MIN = 3            # plan 4.5
QUEUE_MAX = 5            # fixed size on the wire (plan 5.6)
QUEUE_EMPTY = 0xFF       # pad byte for an unused slot

# --- discovery -------------------------------------------------------------
NO_PEERS_HINT_MS = 5000  # show a hint after searching this long with nobody found

# --- animation timing (ms) -------------------------------------------------
# EVO's animation length is VARIABLE: sim_ticks * 250 ms, so 9-20 s depending on
# the fight. CLASSIC's fixed _ANIM_MS = 4800 belongs to the v1.0.23 line and is
# deliberately not reused here (plan 4.1).
# PLAYBACK speed, which is NOT the sim's tick. TICK_MS = 250 is part of the
# combat model and therefore of rules_ver - two badges disagreeing on it compute
# different fights. This is only how fast the finished fight is replayed, and
# because the radio is off before a single frame plays (plan 1), each badge
# replays independently and nothing has to agree on it. Lower = snappier.
_PLAYBACK_MS = 180        # 250 ms read as sluggish on the badge
_DICE_MS = 1400           # the dice-off, before the mons move (plan 8.3)
# The VS card, before the dice-off. Two seconds of "this is happening" - the
# fight otherwise begins with dice, which is procedure, not an opening.
_VS_MS = 2000
_VS_LEAN = -0.16          # radians. ctx has rotate but no shear, so the slant
#                           is the whole word leaning, not true italic - at this
#                           size and angle it reads the same and costs nothing.
_VS_SIZE = 52
_VS_RGB = (1.00, 0.72, 0.15)
_INTRO_MS = 900           # mons slide in, during the dice-off
_ENDING_MS = 900          # beat after the last tick, before the result banner
_HIT_FLASH_MS = 200       # a bar flashes white this long after a hit
# Tied to playback, not a bare number. At 700 ms against a 180 ms tick a label
# outlived its own tick by roughly four and was simply overwritten by the next,
# and the last action of a fight lingered into the result banner (plan 8.3.1).
# Two ticks is long enough to read and short enough to belong to its tick.
_ACTION_FLASH_MS = _PLAYBACK_MS * 2

# --- battle screen ---------------------------------------------------------
# Health bars are ARCS hugging the bezel: ours in the bottom-left corner
# (nearest the holder), theirs in the top-right, diagonally opposite - the same
# diagonal the mons and the projectile already use. Angles run from +x with y
# down, so they increase clockwise: 90 deg = bottom, 180 = left, 270 = top.
# Sits just inside the rim ring. The gap used to be 12 px, which read as two
# unrelated rings; closing it by 75% to 3 px makes the bar belong to the rim it
# hangs off. Computed from the ring rather than guessed: the frame stroke is
# _BAR_T + 3 wide, so the bar's outer edge is _BAR_R + (_BAR_T + 3) / 2.
_BAR_R = 108.0                    # arc radius
_BAR_T = 9                        # stroke thickness

# The battle screen's own rim. Blue where BATTLE MODE's is red, and thinner:
# that screen frames a menu you are reading, this one frames a fight you are
# watching, so it should sit further back. It BREAKS at top and bottom centre
# with a short line turning inward at each end, which splits the screen down
# the middle - your side left, theirs right - and gives the two halves an edge
# to belong to instead of floating on one field.
_BRING_RGB = (0.85, 0.12, 0.12)   # red, as BATTLE MODE's rim is
_BRING_W = 3          # same weight as BATTLE MODE's rim. It was 2 while it was
#                       blue and meant to sit further back; in the same red as
#                       the menu, a thinner stroke just reads as a weaker
#                       version of the same line.
_BRING_R = 120 - _BRING_W / 2.0 - 1
_BRING_GAP = 7.0 * math.pi / 180   # half-width of the break, radians
_BRING_TICK = 13.0                 # how far the end lines turn inward, px

# The right-hand move log: what just fired, newest first, sliding out and
# fading as it ages. Depth is small on purpose - it is a glance, not a
# transcript, and the fight is the thing being watched.
# Each list gets ONE quadrant and stays in it: the queue upper-left, the log
# lower-right. Running the full height of a round screen put text over the mons,
# the bars and the rim ticks at once, which is what made them unreadable - it
# was never the point size. Diagonally opposite so the two never crowd, and
# each clear of its own button disc (F at -54,-84 and C at 54,84).
_MOVE_LOG_N = 4
_MOVE_X = 55                       # lower-RIGHT quadrant
_MOVE_Y = 16                       # newest entry at the top of that quadrant
_MOVE_ROW = 17.0
_MOVE_FADE = 3                     # ticks before an entry is gone

# Your rotation, current slot lit. Upper-LEFT quadrant.
#
# The gap between the firing row and the rest is deliberately large, the way
# ArcMenu runs 15 against 31: a couple of points apart reads as a typo, and the
# whole column then reads as one grey block. The waiting rows are legible
# rather than dim - they are what you built, and you should be able to see
# what is coming - but only the firing one is hot.
_QCOL_X = -55
# Centre nudged down and the rows tightened to buy height for the firing row:
# at 23pt it is tall enough that the top slot would otherwise reach into the
# F disc at (-54, -84).
_QCOL_MID = -30.0                  # the column is centred on this
_QCOL_ROW = 15.0
_QCOL_SIZE = 11                    # waiting
_QCOL_SEL_SIZE = 23                # firing now
_QCOL_RGB = (0.62, 0.62, 0.68)
# Green, and the app's brightest. Orange was fiery but it is also the loss
# colour's neighbour, and the firing move is not a warning - it is the thing
# your mon is doing right now, which is the same "go" the C disc means.
_QCOL_HOT_RGB = (0.35, 1.00, 0.45)
_MY_ARC = (100.0 * math.pi / 180, 172.0 * math.pi / 180)    # bottom-left
_OPP_ARC = (280.0 * math.pi / 180, 352.0 * math.pi / 180)   # top-right
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
_OPP_NAME_Y = -62


# One colour per outcome, shared by the result banner and the records log so a
# draw never has to be inferred from "not a win".
_RESULT_RGB = {
    "W": (0.20, 0.80, 0.35),
    "L": (0.90, 0.25, 0.25),
    "D": (0.90, 0.70, 0.10),
}

# The selection screen's action column. Mirrors the builder's (queues.py
# _ACT_X/_ACT_SIZE) so the queue you pick here is laid out like the queue you
# built there - the ArcMenu owns the left, this owns the right.
# The result words. Fiery for a loss, green for a win - hotter than the bar
# colours, because this is the one moment the screen is allowed to shout.
_RESULT_WIN_RGB = (0.30, 1.00, 0.45)
_RESULT_LOSE_RGB = (1.00, 0.45, 0.10)
# Radiance without animation: the word drawn four times at low alpha around
# itself, then crisply on top. Four offsets, not eight - five text draws per
# word is a glow, nine is the same glow costing twice as much on a screen that
# never changes. No allocation, so it is safe to redraw every frame.
_GLOW_OFF = ((-3.0, 0.0), (3.0, 0.0), (0.0, -3.0), (0.0, 3.0))
_GLOW_A = 0.22
_RESULT_X = -52          # both words ride the left, over your own mon's side
_RESULT_Y = 46           # +- this: YOU upper-left, the verdict lower-right
_VS_RESULT_SIZE = 34     # smaller than the opening card - it shares the screen

# The records screen's right column - same x as the queue builder's, so the
# two screens line up when you move between them.
_REC_X = 46
_REC_TOP = -46.0
_REC_ROW = 17.0

# Sits further out and smaller than the builder's column. The ArcMenu's
# selected row grows INWARD from the left bezel, so on this screen the two
# reach for the same middle - the builder has no countdown or status text
# there and can afford the width, this one cannot.
_SEL_MENU_ROW = 13       # the selection screen's own menu sizes
_SEL_MENU_SEL = 27       # short of the usual 31: the action column is at x=52
#                          and the selected row grows inward from the bezel

_SEL_ACT_X = 52
_SEL_ACT_SIZE = 13
_SEL_ACT_ROW = 18


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


def _clean_action_id(x):
    """One action id off the wire. None if this build cannot field it.

    A protocol violation, NOT something to clamp (plan 5.6): clamping an
    unknown id would leave the two badges fielding different queues and
    computing different fights, which is the silent desync the whole version
    handshake exists to prevent.
    """
    try:
        v = int(x)
    except (TypeError, ValueError):
        return None
    return v if 0 <= v < len(ACTIONS) else None


# --- action queues (plan 5.6, 14.5) ----------------------------------------
# A queue is held in its WIRE form throughout: QUEUE_MAX bytes, unused slots
# padded with QUEUE_EMPTY (plan 6.2.1 #2). Indexing yields ints directly, it is
# far more compact than a 5-element list, and the pack/unpack step for the
# exchange disappears entirely - the buffer received is the buffer simulated.
_PAD = bytes((QUEUE_EMPTY,))
ALL_TACKLE = bytes((0, 0, 0, 0, QUEUE_EMPTY))


def default_queue_for(pet):
    """The queue a mon brings when the player has not built one (plan 14.5).

    Tackle-led, trait action in slot 2, Tackle to fill - so it is never
    all-Tackle, and it changes when the mon changes. A pure function of trait:
    it deliberately ignores the collected pool, because a pool-filled default
    measures WEAKER - 24.9% against a plain new-trainer default (plan 4.4.8 #5).
    The pool's value is choice, not quantity, and auto-equipping it would hand
    veterans a worse queue than beginners.

    Four callers must all resolve HERE: the practice opponent, the auto-lock at
    selection-timer expiry, an all-empty received queue, and a player who has
    never opened the queue builder.
    """
    trait = pet.get("trait") if isinstance(pet, dict) else None
    aid = TRAIT_ACTION.get(trait)
    if aid is None:
        return ALL_TACKLE   # corrupt/absent trait; app.py:462 normally repairs it
    return bytes((0, aid, 0, 0, QUEUE_EMPTY))


def pack_queue(ids):
    """A list of action ids -> the QUEUE_MAX-byte wire form, padded with
    QUEUE_EMPTY. The one place a stored queue becomes a fieldable one."""
    q = bytes(ids[:QUEUE_MAX])
    return q + _PAD * (QUEUE_MAX - len(q))


def queue_len(queue):
    """Used slots in a wire queue. The rotation wraps at this, not at
    QUEUE_MAX."""
    n = 0
    for b in queue:
        if b == QUEUE_EMPTY:
            break
        n += 1
    return n


def _clean_queue(blob):
    """Decode + validate an action queue off the wire (plan 5.6, 5.8).

    Returns the QUEUE_MAX-byte wire form, or None on a protocol violation -
    which the caller turns into a no contest (plan 5.7).

    Two deliberate readings of plan 5.6, both documented because they are not
    quite what a first read of that section suggests:

    - An ALL-EMPTY queue substitutes rather than failing, per 5.6. It cannot
      substitute 14.5's default, because that needs the SENDER's trait and the
      mon-exchange frame has no room to carry it - 5.3 is already exactly at the
      20-byte ATT write limit. All-Tackle is the only substitute both badges can
      derive from what they share, and it buys a liar nothing: it wins 49.6%
      against the five defaults (4.4.7), i.e. no better than playing honestly.
    - A SHORT queue (1-2 entries) is accepted, not rejected. QUEUE_MIN is a
      builder-UI rule; 5.6 does not list "too short" among its violations, and a
      short queue is self-punishing anyway - it repeats sooner and eats the 14%
      repeat penalty more often. Rejecting it would only manufacture no contests.
    """
    try:
        q = bytes(blob)
    except Exception:
        return None
    if len(q) != QUEUE_MAX:
        return None
    n = queue_len(q)
    if q[n:] != _PAD * (QUEUE_MAX - n):
        return None    # a hole in the middle: empties only ever pad the tail
    if n == 0:
        return ALL_TACKLE
    for i in range(n):
        if _clean_action_id(q[i]) is None:
            return None
    return q


def innate_actions_for(pet):
    """Every action this mon can field from birth (plan 4.3). Pure function of
    the trait - DERIVED, never stored, so a corrupt or absent trait degrades to
    Tackle instead of persisting junk.

    Age deliberately adds nothing. The elder aura is a passive +2 HP computed
    from age (plan 4.4.6); it is not an entry in ACTIONS, has no id, and can
    never appear in a queue.
    """
    aid = TRAIT_ACTION.get(pet.get("trait") if isinstance(pet, dict) else None)
    return (0,) if aid is None else (0, aid)


def is_elder(age):
    """The aura gate (plan 4.4.6). Kept here rather than calling app._life_stage
    so the sim depends on a number, not on a life-stage string."""
    return _clean_age(age) >= 48


# --- the simulator (plan 4.4, 4.5, 4.10) -----------------------------------
# Runs ONCE, the instant both queues are known, then the animation is pure
# playback. It must stay tick-for-tick identical to tools/evosim.py or the
# section 7.6 parity check is comparing two different games - so keep the two
# in step, and re-run the simulator's baseline after touching either.
#
# FOUR bytes per tick, not three (plan 8.3.1). 4.10 specifies three - both HP
# values and one packed event byte - which is enough for an INSTANTANEOUS
# visual and not enough for a LINGERING one: nothing in it says "chip is
# burning" or "the guard is up", because those live in _Mon.chip_until and
# guard_until and are gone the moment simulate() returns. The fourth byte is a
# status bitfield, so Mud Sling's 20 ticks of chip and Brace's 14 ticks of
# guard can be DRAWN rather than re-derived. Re-deriving them while replaying
# is the re-simulation 4.10 forbids, and the way the sim and the animation
# would drift apart.
#
# 324 bytes, preallocated once. (4.10 also says 240, which is one tick short
# either way: the loop runs 0..CAP_TICKS INCLUSIVE, 81 ticks.)
BUF_STRIDE = 4
ST_CHIP_A = 0x01
ST_GUARD_A = 0x02
ST_CHIP_B = 0x04
ST_GUARD_B = 0x08
_EVENT_BUF = bytearray(BUF_STRIDE * (CAP_TICKS + 1))

DIE = 20                 # readable on screen, and ties 5% of the time
MAX_REROLLS = 8          # bounded - plan 6.3 allows no unbounded loop anywhere


def dice_off(nxt):
    """Initiative (plan 4.5 step 0). Both mons roll a d20 off the SHARED stream,
    higher moves first, a tie re-rolls. Returns (a_first, rolls).

    Both rolls coming off the shared stream is the entire safety property.
    Initiative is worth a 34-point swing - 79-97% in a mirror - so a rule that
    read it off the salt order directly would let a modified badge take it in
    every fight by sending salt 0. seed = my_salt ^ peer_salt cannot be biased,
    because commit-reveal fixes my salt before I ever see theirs.
    """
    rolls = []
    for _ in range(MAX_REROLLS):
        ra = nxt() % DIE + 1
        rb = nxt() % DIE + 1
        rolls.append((ra, rb))
        if ra != rb:
            return ra > rb, rolls
    return (nxt() & 1) == 0, rolls


class _Mon:
    """One combatant, for the duration of one fight. Two of these per battle -
    the only objects the sim allocates."""

    __slots__ = ("q", "n", "hp", "cap", "ready", "ptr", "chip_until",
                 "guard_until", "guard_amt", "last")

    def __init__(self, queue, elder):
        self.q = queue                 # WIRE form; indexing yields ints (6.2.1)
        self.n = queue_len(queue) or 1
        self.cap = EVO_HP + (ELDER_BONUS_HP if elder else 0)
        self.hp = self.cap
        self.ready = 0
        self.ptr = 0
        self.chip_until = 0
        self.guard_until = 0
        self.guard_amt = 0
        self.last = -1


def _resolve(me, opp, aid, tick, nxt):
    """Plan 4.5 steps 3-4 for one mon's action. GUARD already applied in step 2,
    so it only records `last` here."""
    kind = ACTIONS[aid][2]
    power = ACTIONS[aid][3]
    spread = ACTIONS[aid][4]
    param = ACTIONS[aid][5]
    # ATK/LEECH/SLOW/CHIP are ids 0..3 - the four that deal damage. Comparing
    # against CHIP rather than building a membership tuple keeps this off the
    # per-tick allocation path (plan 6.2.1 #3).
    if kind <= CHIP:
        dmg = power + (nxt() % (spread + 1) if spread else 0)
        if aid == me.last:
            dmg = max(1, dmg * (100 - REPEAT_PENALTY) // 100)
        if tick < opp.guard_until:
            dmg = max(1, dmg - opp.guard_amt)   # GUARD never reduces below 1
        opp.hp -= dmg
        if kind == LEECH:
            me.hp = min(me.cap, me.hp + dmg * param // 100)
        elif kind == SLOW:
            # additive but CAPPED. An uncapped += lets one mon push another
            # unboundedly behind; a pure max() floor is a no-op whenever it is
            # shorter than the action it delays.
            opp.ready = min(opp.ready + param, tick + ACTIONS[aid][6])
        elif kind == CHIP:
            opp.chip_until = tick + param       # refresh, never stack
    elif kind == HEAL:
        heal = power + (nxt() % (spread + 1) if spread else 0)
        if aid == me.last:
            heal = max(1, heal * (100 - REPEAT_PENALTY) // 100)
        me.hp = min(me.cap, me.hp + heal)
        me.chip_until = 0                       # clears OWN chip
    me.last = aid


def simulate(qa, qb, seed, elder_a=False, elder_b=False, buf=None):
    """The whole fight, in one tight loop. Returns
    (result, ticks, winner_hp, rolls) - result 1 = A wins, -1 = B, 0 = draw.

    A and B are the CANONICAL order (lower salt is A, plan 5.5 step 4), not
    "me" and "them": both badges run this with identical arguments and get an
    identical answer, and the caller maps A/B onto its own side afterwards.

    `buf` is filled with BUF_STRIDE bytes per tick - A's HP, B's HP, an event
    code packing both mons' actions (low nibble A, high nibble B, 0 for "did
    not fire"), and a status bitfield for the effects that PERSIST. The
    animation is then a pure lookup by tick index: no list of event dicts, and
    no re-simulating per frame.
    """
    if buf is None:
        buf = _EVENT_BUF
    nxt = _xorshift(seed)
    a = _Mon(qa, elder_a)
    b = _Mon(qb, elder_b)

    a_first, rolls = dice_off(nxt)
    (b if a_first else a).ready = 1

    ticks = CAP_TICKS
    result = 0
    for tick in range(CAP_TICKS + 1):
        if tick < a.chip_until:
            a.hp -= 1
        if tick < b.chip_until:
            b.hp -= 1

        # Fire, if ready. The queue is a ROTATION - the pointer wraps, so a
        # 3-entry queue loops roughly twice in a 12 s fight.
        aa = -1
        if tick >= a.ready:
            aa = a.q[a.ptr % a.n]
            a.ptr += 1
            a.ready = tick + ACTIONS[aa][1]
        ab = -1
        if tick >= b.ready:
            ab = b.q[b.ptr % b.n]
            b.ptr += 1
            b.ready = tick + ACTIONS[ab][1]

        # Step 2 - defensive effects first, both mons. Order is irrelevant
        # here: a guard only ever touches its own mon.
        if aa >= 0 and ACTIONS[aa][2] == GUARD:
            a.guard_until = tick + ACTIONS[aa][5]
            a.guard_amt = ACTIONS[aa][3]
        if ab >= 0 and ACTIONS[ab][2] == GUARD:
            b.guard_until = tick + ACTIONS[ab][5]
            b.guard_amt = ACTIONS[ab][3]

        # Steps 3-4 - damage, then heals and lingering, dice-off winner FIRST.
        # This order is observable (a heal landing near the HP cap resolves
        # differently either way), so it is pinned, not incidental.
        if a_first:
            if aa >= 0:
                _resolve(a, b, aa, tick, nxt)
            if ab >= 0:
                _resolve(b, a, ab, tick, nxt)
        else:
            if ab >= 0:
                _resolve(b, a, ab, tick, nxt)
            if aa >= 0:
                _resolve(a, b, aa, tick, nxt)

        i = BUF_STRIDE * tick
        buf[i] = a.hp if a.hp > 0 else 0
        buf[i + 1] = b.hp if b.hp > 0 else 0
        buf[i + 2] = (aa + 1) | ((ab + 1) << 4)
        # Status AFTER this tick resolved, so the animation shows an effect
        # starting on the tick that applied it.
        st = 0
        if tick < a.chip_until:
            st |= ST_CHIP_A
        if tick < a.guard_until:
            st |= ST_GUARD_A
        if tick < b.chip_until:
            st |= ST_CHIP_B
        if tick < b.guard_until:
            st |= ST_GUARD_B
        buf[i + 3] = st

        # Step 5 - KO only AFTER everything has resolved. Checking between the
        # damage and heal steps is what would turn a simultaneous kill into a
        # race won by whichever mon the code happened to evaluate first.
        if a.hp <= 0 or b.hp <= 0:
            ticks = tick
            if a.hp <= 0 and b.hp <= 0:
                return 0, tick, 0, rolls
            return (1, tick, a.hp, rolls) if b.hp <= 0 else (-1, tick, b.hp, rolls)

    # Cap reached: the higher HP wins, an exact tie draws (plan 5.7). Measured
    # at 0.14% of fights, so this only ever governs deliberate stalls.
    if a.hp == b.hp:
        return 0, ticks, a.hp, rolls
    return ((1, ticks, a.hp, rolls) if a.hp > b.hp
            else (-1, ticks, b.hp, rolls))


# --- the 17-byte stats blob ------------------------------------------------
# name[8] shape[1] rgb[3] str[1] nonce[4]. Module-level so blelab/ and the EVO
# mon frame both build it through THIS code rather than a copy (plan 7.2).
#
# DO NOT WIDEN IT. An older badge unpacks a fixed-size struct and rejects
# anything longer, and with a 1-byte tag the write is already 18 B against a
# hard 20 B ceiling (default ATT MTU 23 - 3), beyond which writes silently
# truncate. Growing it needs an MTU exchange first.
_STATS_FMT = "<8sBBBBBI"
STATS_LEN = struct.calcsize(_STATS_FMT)        # 17


def pack_stats(pet, nonce):
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
    return struct.pack(_STATS_FMT, name, shape_idx, r, g, b,
                       strv, nonce & 0xFFFFFFFF)


def unpack_stats(blob):
    """Decode + SANITISE an opponent's stats blob (untrusted wire data).
    Returns a dict, or None if malformed."""
    try:
        name_b, shape_idx, r, g, b, strv, nonce = struct.unpack(
            _STATS_FMT, bytes(blob)[:STATS_LEN]
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


# --- BATTLE_EVO! wire framing (plan 5.2, 5.3, 5.5, 5.6) --------------------
# Tags are the first byte of every EVO frame, and they are WIRE FORMAT: never
# reuse one for a different meaning. All of this lives here rather than in
# ble_link.py because blelab/ imports these exact functions - the server must
# never hand-roll a frame, a hash or a seed (plan 7.2). Every hand-rolled copy
# is a place the server and the badge silently diverge, which is precisely the
# class of bug the harness exists to catch.
TAG_VER = b"V"       # version check, both directions        (plan 5.2)
TAG_MON = b"M"       # mon exchange                          (plan 5.3)
TAG_LOCK = b"L"      # 1 Hz selection status                 (plan 5.4)
TAG_COMMIT = b"C"    # lock-in commitment                    (plan 5.5)
TAG_REVEAL = b"R"    # queue + salt                          (plan 5.5)

# The mon frame is the 17-byte stats blob + age_u16. With the tag that is
# EXACTLY 20 bytes - the whole ATT budget at the default MTU of 23 (23 - 3).
# There is no room for another field without an MTU exchange first, which is
# why the peer's TRAIT is not on the wire, and therefore why _clean_queue
# cannot substitute 14.5's default for an all-empty queue.
_AGE_FMT = "<H"
MON_FRAME_LEN = 1 + STATS_LEN + struct.calcsize(_AGE_FMT)   # 20
VER_FRAME_LEN = 3
LOCK_FRAME_LEN = 3
COMMIT_LEN = 16
COMMIT_FRAME_LEN = 1 + COMMIT_LEN                      # 17
SALT_LEN = 4
REVEAL_FRAME_LEN = 1 + QUEUE_MAX + SALT_LEN            # 10


def _clean_age(x):
    """Age in hours off the wire. Clamped, not rejected: age only picks the
    life stage and the elder aura, and a clamp cannot desync the two badges
    because both clamp the same received value the same way."""
    try:
        return min(0xFFFF, max(0, int(x)))
    except (TypeError, ValueError):
        return 0


def pack_version():
    return TAG_VER + bytes((PROTO_VER & 0xFF, RULES_VER & 0xFF))


def unpack_version(frame):
    """(proto_ver, rules_ver) off the wire, or None if it is not a version
    frame. Length is checked exactly: a truncated or padded frame is a
    protocol violation, not something to interpret generously."""
    try:
        f = bytes(frame)
    except Exception:
        return None
    if len(f) != VER_FRAME_LEN or f[:1] != TAG_VER:
        return None
    return f[1], f[2]


def pack_mon(pet, nonce, age):
    """This mon, framed for the exchange (plan 5.3).

    Age rather than a bare elder flag, so future stage-gated content needs no
    protocol change.
    """
    return TAG_MON + pack_stats(pet, nonce) + struct.pack(_AGE_FMT,
                                                          _clean_age(age))


def unpack_mon(frame):
    """Decode + SANITISE a peer's mon (plan 5.3). None if malformed.

    Everything here is attacker-controlled, so it runs through the same
    sanitisers a locally loaded pet does. The `nonce` field is carried only
    because it is part of the frozen 17-byte blob layout - EVO NEVER USES IT.
    The shared seed comes from the commit-reveal salts (plan 5.5), which is
    what closes the ordered-nonce grinding defect (plan 3.3) by construction.

    Note there is nothing to check for plan 5.8's "peer claims an aura while
    sending an age under 48 h": the settled aura (plan 4.4.6) is a passive +2 HP
    DERIVED from this age, with no wire representation and no action id, so a
    peer cannot claim one. Both badges compute it from the same received value.
    """
    try:
        f = bytes(frame)
    except Exception:
        return None
    if len(f) != MON_FRAME_LEN or f[:1] != TAG_MON:
        return None
    mon = unpack_stats(f[1:1 + STATS_LEN])
    if mon is None:
        return None
    try:
        age = struct.unpack(_AGE_FMT, f[1 + STATS_LEN:])[0]
    except Exception:
        return None
    mon["age"] = _clean_age(age)
    return mon


def pack_lock(locked, secs_left):
    """Selection-phase status, sent once a second (plan 5.4)."""
    return TAG_LOCK + bytes((1 if locked else 0,
                             min(255, max(0, int(secs_left)))))


def unpack_lock(frame):
    """(locked, secs_left) or None.

    `secs_left` is ADVISORY ONLY - it is displayed, never used to drive our own
    deadline. Letting a peer's number set our timer hands them a stall lever
    (plan 5.4).
    """
    try:
        f = bytes(frame)
    except Exception:
        return None
    if len(f) != LOCK_FRAME_LEN or f[:1] != TAG_LOCK:
        return None
    return bool(f[1]), f[2]


def commit_hash(queue, salt, peer_addr):
    """The lock-in commitment (plan 5.5): sha256(queue | salt | peer_addr)[:16].

    Binding the PEER'S address in is what stops a commitment being replayed at
    a different opponent. hashlib.sha256 is confirmed present on the Tildagon.
    If that ever regresses on a future firmware, STOP - do not substitute a
    hand-rolled hash. The binding property is the entire point, and a weak one
    silently reintroduces both counter-picking and the plan 3.3 seed grind.
    """
    h = hashlib.sha256()
    h.update(bytes(queue))
    h.update(bytes(salt))
    h.update(bytes(peer_addr))
    return h.digest()[:COMMIT_LEN]


def pack_commit(h16):
    return TAG_COMMIT + bytes(h16)[:COMMIT_LEN]


def pack_reveal(queue, salt):
    return TAG_REVEAL + bytes(queue)[:QUEUE_MAX] + bytes(salt)[:SALT_LEN]


def unpack_reveal(frame):
    """(queue_wire_bytes, salt) or None. The queue is NOT validated here -
    the caller runs _clean_queue on it, because an invalid queue and a
    malformed frame are the same verdict but not the same message."""
    try:
        f = bytes(frame)
    except Exception:
        return None
    if len(f) != REVEAL_FRAME_LEN or f[:1] != TAG_REVEAL:
        return None
    return f[1:1 + QUEUE_MAX], f[1 + QUEUE_MAX:]


def evo_seed(my_salt, peer_salt):
    """(seed, i_am_lower) from the two revealed salts (plan 5.5 step 4).

    Both badges hold the same pair, so ordering by salt makes the player order -
    and therefore the same-tick resolution order (plan 4.5) and the stagger -
    agree BY CONSTRUCTION, with no dependency on BLE addresses.

    Equal salts are rejected by the caller for the same reason equal nonces
    were: neither side would be "lower", so both would order themselves
    identically and compute different fights. 1 in 2^32 by accident, trivially
    forced by a hostile peer.
    """
    a = int.from_bytes(bytes(my_salt), "little") & 0xFFFFFFFF
    b = int.from_bytes(bytes(peer_salt), "little") & 0xFFFFFFFF
    return (a ^ b) & 0xFFFFFFFF, a < b


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


class Battle:
    """Owns the battle view: menu, records, BLE discovery, and animation.

    States: menu | info | records | rec_ranked | searching |
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
        # evo_CONNECT (plan 8.1): everything that uses the radio, plus the
        # queue builder, under one entry. There is NO mode picker - rev 22
        # removed Classic from this badge, so there is nothing to pick.
        self.menu_items = ("Find opponent", "Practice", "Action queues",
                           "Records", "Back")
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
        self.result = None         # R_WIN | R_LOSE | R_DRAW | R_NONE
        self._result_applied = False
        # sim output + animation playback state (plan 4.10, 4.1). Defaulted
        # here so the skip path is safe even if no fight has been run.
        self._sim_ticks = 0
        self._sim_rolls = None
        self._i_am_a = True        # which side of the canonical A/B order
        self._sim_winner_hp = 0    # scoring inputs (plan 14.2). Zero and False
        self._sim_ko = False       # score a minimum, never a phantom 142.
        self._my_cap = EVO_HP      # HP caps, +2 each if elder (plan 4.4.6)
        self._opp_cap = EVO_HP
        self._anim_tick = 0
        self._anim_acc = 0.0
        self._my_action = -1
        self._opp_action = -1
        self._my_slot = -1
        # [action id, age in ticks, 1 if ours]. One list for the whole app
        # life: the fight allocates nothing, the log included (plan 6.2).
        self._move_log = [[-1, 0, 0] for _ in range(_MOVE_LOG_N)]
        self._grad_ok = None       # gradient support, decided once
        self._action_flash = 0.0
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
        self._flash_my = 0.0       # ms of hit-flash left on each bar
        self._flash_opp = 0.0
        # BLE discovery. Built lazily by _ensure_ble() when the player actually
        # picks PVP - an archived record never discovers or fights, and neither
        # does Practice, so neither should pay for the radio.
        self.ble = None
        self._ble_owned = False    # does the current screen own the radio?
        self._ble_tried = False
        self._evo_enabled = False
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
        self._invite_peer = None   # peer we're challenging / who's challenging us
        self._my_nonce = 0         # carried in the mon frame, never used by EVO
        # BATTLE_EVO! session
        self._evo = None           # in-flight ble_link.EvoSession
        self._my_mon = None        # OUR mon frame, snapshotted for the session
        self._my_queue = None      # wire-form queue we are fielding
        self._peer_queue = None    # theirs, once revealed
        self._my_salt = None
        self._evo_secs = 0         # local countdown, seconds
        self._evo_acc = 0.0        # ms accumulator driving that countdown
        self._evo_rows = None      # cached selection-screen strings
        self._evo_rows_dirty = True
        self._sel_queues = ()      # (name, queue) the player may pick from
        self._sel_idx = 0
        self._trainer_cache = None # trainer.json, loaded lazily (plan 14.4)
        self._queue_screen = None  # the Action queues screen, while it is open
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

    # Screens that legitimately own the radio. Anything else must not, and
    # _enforce_ble_ownership below makes that true every frame rather than
    # trusting each exit path to tidy up after itself.
    _BLE_STATES = ("searching", "invited", "handshaking", "evo_select")

    def _enforce_ble_ownership(self):
        """One invariant: not on a BLE screen -> radio down.

        close() already does this, but it only runs when the whole app exits.
        Moving BETWEEN screens - cancelling a locked-in session back to the
        menu, say - never called it, so the link stayed up with no screen
        owning it. Per-exit-path cleanup means enumerating every way out and
        getting all of them right; this is the same rule expressed once, and it
        covers the ways out nobody thought of.

        Cheap enough to run per frame: a string membership test, and the
        teardown itself happens once per transition, not repeatedly.
        """
        if self.ble is None:
            return
        owns = self.state in self._BLE_STATES
        if owns == self._ble_owned:
            return
        self._ble_owned = owns
        if not owns:
            try:
                self.ble.stop()
                self.ble.cancel_handshake()
            except Exception as e:
                print("Battle: ble release:", e)

    # --- lifecycle ---------------------------------------------------------
    def close(self):
        # SAVE THE QUEUES FIRST. `_close_queues` writes them on the way out
        # (plan 14.4: write rarely, not per edit), but that is the TIDY exit -
        # the player pressing F. app.py reaches here two other ways that skip
        # it entirely: an exception out of `addon.update()`, and five
        # consecutive draw errors. Both call `_close_addon()`, which nulls
        # `self.battle` and takes the QueueScreen with it, unsaved.
        #
        # So a player who built a queue and hit a draw fault lost the lot,
        # silently. Review plan 1.3 exactly: cleanup hanging off one exit path,
        # and the untidy exits skipping it. The fix belongs HERE, where closing
        # actually happens, rather than in a fourth caller that remembers.
        #
        # Safe on the tidy path too: `_close_queues` nulls `_queue_screen`, so
        # this is a no-op when it has already run.
        scr = self._queue_screen
        self._queue_screen = None
        if scr is not None:
            try:
                scr.save()
            except Exception as e:
                print("Battle: queue save on close failed:", e)
        if self.ble is not None:
            try:
                self.ble.stop()
                self.ble.cancel_handshake()   # covers Handshake and EvoSession
            except Exception as e:
                print("Battle: ble stop:", e)
        # Drop the session so nothing module-level outlives the battle
        # (plan 6.3). The result is already banked, so this cannot lose one.
        self._evo = None
        try:
            eventbus.remove(ButtonDownEvent, self._input_handler, self.app)
        except Exception as e:
            print("Battle: input unsubscribe failed:", e)

    def _ensure_ble(self):
        """Import ble_link and build the link on demand. Returns it, or None.

        This is a WATCHDOG measure, not tidiness. Importing ble_link costs about
        1.7 s on the badge - ~0.7 s compiling it, plus ~1.0 s the first time
        anything in the app pulls in aioble and asyncio - and MicroPython
        compiles at import, with no cached .mpy, so it is paid on every session.
        It all lands in a single frame of the draw loop against a 5 s task WDT,
        on top of the ~1.2 s battle.py itself costs.

        Practice needs no radio at all (plan 4.9) and Records needs less, so
        paying for the radio merely to OPEN the battle menu spent watchdog
        headroom on a screen that may never use it. Measured: it moves the
        battle-open stall from ~2.9 s to ~1.2 s, which is better than the
        v1.0.23 build managed.

        Tried once per Battle instance - a persistent failure must not re-run a
        1.7 s import every time the player highlights PVP.
        """
        if self._ble_tried:
            return self.ble
        self._ble_tried = True
        if self.archived:
            return None    # a past mon's record is a static screen
        try:
            from .ble_link import EVO_ENABLED, BleLink
        except Exception as e:      # Practice and Records still work without it
            print("Battle: BLE unavailable:", e)
            return None
        self._evo_enabled = EVO_ENABLED
        try:
            self.ble = BleLink(self._my_name())
        except Exception as e:
            print("Battle: BLE init:", e)
            self.ble = None
        return self.ble

    # --- the fight renderer, loaded on demand (plan 6.2.2) -----------------
    # Class-level, not per-instance: the module is installed onto the CLASS, so
    # the second Battle of a session must not pay for it again.
    _fv_loaded = False

    def _ensure_fightview(self):
        """Import `fightview` and attach it to Battle. Idempotent and cheap.

        WHY THIS EXISTS, and it is the same argument as `_ensure_ble` above.
        MicroPython reads and compiles a module in ONE frame, and that frame is
        measured against the 5 s task WDT. Every byte of `battle.py` is paid for
        when the player opens the battle MENU - a screen that may only ever show
        Practice or Records.

        20 KB of fight rendering has no business on that path: measured at
        ~12 ms/KB for read+compile, it was ~245 ms of a frame that had crept
        back to ~2.4 s, against the 2.9 s that once rebooted a badge.

        Returns True if the renderer is available. A False is not fatal - the
        fallback `_draw_battle` below says so on screen instead of raising,
        because an addon that throws from draw() gets torn down and the player
        just sees the battle vanish.
        """
        if Battle._fv_loaded:
            return True
        try:
            from . import fightview
            fightview.install(Battle)
            Battle._fv_loaded = True
            return True
        except Exception as e:
            # Reported once per attempt, not per frame: the caller only asks
            # again on a state change, not from the draw loop.
            print("Battle: fight view unavailable:", e)
            return False

    def _draw_battle(self, ctx):
        """Fallback, REPLACED by `fightview.install()`.

        Only ever reached if that import failed. It must not raise: app.py tears
        the addon down after a few draw errors, so a throw here loses the result
        screen as well as the fight.
        """
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.rgb(1.0, 0.4, 0.4)
        ctx.font_size = 16
        ctx.move_to(0, -10).text("fight view")
        ctx.move_to(0, 12).text("unavailable")
        draw_hints(ctx, f="F back")

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
        # EVO needs a FULLER opponent than CLASSIC did: a trait, to pick its
        # default queue and its slot-2 action, and an age, to decide whether it
        # has an elder aura (plan 4.9). Rolled from the same sources a real mon
        # uses, and put through the same sanitisers a wire-received mon goes
        # through, so practice exercises the real code path for free.
        self.opp = {
            "name": _random_name(),
            "shape": _clean_shape(random.choice(SHAPES)),
            "colour": _random_colour(),
            "strength": _clean_strength(random.randint(2, 9)),  # inert (4.7)
            "trait": random.choice(TRAITS),
            # adult..elder, so a practice opponent is always old enough to be a
            # real sparring partner and sometimes carries the aura
            "age": _clean_age(random.randint(6, 60)),
            "id": bytes(random.getrandbits(8) for _ in range(6)),
        }
        self.my_str = _clean_strength(self.app.pet.get("strength", 5))
        self.is_practice = True  # free: no HP change, no record, no points
        # Pick a queue first, on the SAME screen a networked battle uses.
        # Practice used to field _active_queue() with nothing asked and nothing
        # shown, so a player who had built a queue but never made it active -
        # which saving does not do - sparred with the default and had no way to
        # tell. Practice is where a queue is meant to be TRIED.
        self._open_queue_pick("prac_select")

    def _open_queue_pick(self, state):
        """Load the selectable queues into the shared picker and go to it."""
        self._sel_queues = self._selectable_queues()
        self._sel_idx = 0
        active = self._active_queue()
        for i, (_n, q) in enumerate(self._sel_queues):
            if q == active:
                self._sel_idx = i
                break
        self._my_queue = self._sel_queues[self._sel_idx][1]
        self._evo_rows_dirty = True
        self._arc_state = None
        self.state = state
        self._arm_input_lock()

    def _start_practice_fight(self):
        # The opponent ALWAYS fields the default queue for its trait - never a
        # built or saved one. It is a baseline to beat, not a rival (plan 4.9).
        self._run_fight(
            self._my_queue,
            default_queue_for(self.opp),
            random.getrandbits(32),
            True,                       # no salts offline, so we are player A
        )

    def _trainer(self):
        """The trainer file, loaded once per Battle and cached (plan 14.4)."""
        if self._trainer_cache is None:
            self._trainer_cache = load_trainer(len(ACTIONS))
        return self._trainer_cache

    def _active_queue(self):
        """The queue this mon brings into a fight.

        Falls back to 14.5's default whenever the chosen one is missing or the
        current mon cannot field it (plan 14.4b) - a queue saved by a greedy mon
        contains Gobble and is unusable once the next mon is tidy. The default
        is derived live from the current mon and therefore ALWAYS valid, which
        is what guarantees this can never leave a player unable to fight.

        Never send an unvalidated saved queue over the wire: an entry the sender
        cannot field is a protocol violation on the receiving side and would
        hand the opponent a no contest through no fault of theirs.
        """
        default = default_queue_for(self.app.pet)
        tr = self._trainer()
        idx = tr.get("active", 0) - 1        # row 0 is the virtual default
        qs = tr.get("queues", ())
        if not 0 <= idx < len(qs):
            return default
        have = innate_actions_for(self.app.pet)
        pool = tr.get("pool", ())
        q = qs[idx]
        if not q or any(a not in have and a not in pool for a in q):
            return default
        return pack_queue(q)

    def _close_queues(self):
        """Written on the way OUT, not per edit - plan 14.4 says write rarely."""
        scr = self._queue_screen
        self._queue_screen = None
        if scr is not None:
            try:
                scr.save()
            except Exception as e:
                print("Battle: queue save failed:", e)
        # The queue screen borrowed the shared ArcMenu, so force battle.py's
        # own menu to reload into it rather than trusting the cached state.
        self._arc_state = None
        self.state = "menu"
        self._arm_input_lock()

    def _open_queues(self):
        """Imported HERE, not at module scope. battle.py already costs ~1.7 s of
        blocking compile in one frame against a 5 s watchdog (plan 6.2.2); a
        whole screen does not go on top of that for players who never open
        it."""
        try:
            from .queues import QueueScreen
        except Exception as e:
            print("Battle: queue screen unavailable:", e)
            self.message = "Queue builder\nunavailable."
            self.state = "info"
            return
        self._queue_screen = QueueScreen(
            ACTIONS, self._trainer(), self.app.pet,
            default_queue_for, innate_actions_for, self._arc(),
            blurbs=ACTION_BLURB, effect_for=action_effect)
        self.state = "queues"
        self._arm_input_lock()

    def _run_fight(self, my_queue, opp_queue, seed, i_am_a):
        """Simulate the whole fight, BANK THE RESULT, then animate.

        `i_am_a` is which side of the canonical A/B ordering we are - lower salt
        is A (plan 5.5 step 4). Both badges call this with identical arguments
        and get an identical answer; only the mapping back onto "me" differs.
        """
        my_elder = is_elder(self.app.pet.get("age", 0))
        opp_elder = is_elder((self.opp or {}).get("age", 0))
        if i_am_a:
            qa, qb, ea, eb = my_queue, opp_queue, my_elder, opp_elder
        else:
            qa, qb, ea, eb = opp_queue, my_queue, opp_elder, my_elder
        result, ticks, winner_hp, rolls = simulate(
            qa, qb, seed, ea, eb, _EVENT_BUF)
        self._sim_ticks = ticks
        self._sim_rolls = rolls
        self._i_am_a = i_am_a
        self._my_cap = EVO_HP + (ELDER_BONUS_HP if my_elder else 0)
        self._opp_cap = EVO_HP + (ELDER_BONUS_HP if opp_elder else 0)
        # Scoring inputs (plan 14.2), captured here because this is the only
        # place they exist - the animation is a pure buffer lookup and the result
        # is banked before a frame of it plays.
        self._sim_winner_hp = winner_hp
        # KO or a win on HP at the cap. Derived from the LOSER being at zero, not
        # from `ticks < CAP_TICKS`: the loop runs 0..CAP_TICKS inclusive, so a KO
        # on the final tick reports the cap and a tick comparison would score it
        # as the 0.00% HP-win tier. Nothing reaches zero without the loop having
        # returned a KO, so zero on either side means exactly one thing.
        my_hp, their_hp = self._tick_hp(ticks)
        self._sim_ko = my_hp == 0 or their_hp == 0
        if result == 0:
            out = self.R_DRAW
        elif (result == 1) == i_am_a:
            out = self.R_WIN
        else:
            out = self.R_LOSE
        # Banked BEFORE a single frame of animation plays (plan 4.11).
        self._commit_result(out)
        self._begin_anim()

    def _tick_hp(self, tick):
        """(my HP, their HP) after `tick`, straight out of the event buffer.
        A pure lookup - never a re-simulation (plan 4.10)."""
        i = BUF_STRIDE * tick
        a_hp, b_hp = _EVENT_BUF[i], _EVENT_BUF[i + 1]
        return (a_hp, b_hp) if self._i_am_a else (b_hp, a_hp)

    def _tick_actions(self, tick):
        """(my action id, their action id) for `tick`, -1 for "did not fire"."""
        code = _EVENT_BUF[BUF_STRIDE * tick + 2]
        a, b = (code & 0x0F) - 1, (code >> 4) - 1
        return (a, b) if self._i_am_a else (b, a)

    def _tick_status(self, tick):
        """(my chip, my guard, their chip, their guard) for `tick`. Booleans
        straight out of the buffer - never re-derived (plan 4.10)."""
        st = _EVENT_BUF[BUF_STRIDE * tick + 3]
        if self._i_am_a:
            return (st & ST_CHIP_A, st & ST_GUARD_A,
                    st & ST_CHIP_B, st & ST_GUARD_B)
        return (st & ST_CHIP_B, st & ST_GUARD_B,
                st & ST_CHIP_A, st & ST_GUARD_A)

    def _begin_anim(self):
        self.anim_t = 0.0
        self._anim_tick = 0
        self._anim_acc = 0.0
        self.my_bar = 100.0
        self.opp_bar = 100.0
        self._ghost_my = 100.0
        self._ghost_opp = 100.0
        self._combat_rgb = None    # new opponent, new colours
        self._flash_my = 0.0
        self._flash_opp = 0.0
        self._my_action = -1       # what to flash on screen, and for how long
        self._opp_action = -1
        self._my_slot = -1         # how many of OUR actions have fired
        # Fixed-size move log, mutated in place - the right-hand list of what
        # just happened. Allocated once per fight, never per tick (plan 6.2).
        for e in self._move_log:
            e[0] = -1
            e[1] = 0
            e[2] = 0
        self._action_flash = 0.0
        self.state = "anim"

    # --- handshake wire format --------------------------------------------
    # A thin delegate to the module-level function, kept as a METHOD because
    # the harness drives it on a real Battle instance
    # (blelab/evo_proto_test.py's arm()).
    #
    # Two siblings, `_pack_stats` and `_unpack_stats`, were removed at rev 69:
    # their comment claimed blelab/badge.py called them and it did not - it uses
    # the module-level functions. The comment was the only thing keeping them
    # alive, which is review plan §1.8 exactly. Check the caller, do not trust
    # the note that says there is one.
    def pack_mon(self):
        """This mon, framed for the EVO mon exchange (plan 5.3)."""
        pet = self.app.pet
        return pack_mon(pet, self._my_nonce, pet.get("age", 0))

    # --- result (plan 4.11, 5.7) -------------------------------------------
    # Four outcomes. A DRAW is a real result and costs exactly what a win costs;
    # a NO CONTEST is a protocol failure and must leave the pet BYTE-IDENTICAL
    # with nothing written. Plan 5.7's opening sentence loosely calls both of
    # them "a draw"; its outcome table, which is the settled part, does not.
    # They look the same to the player and are recorded completely differently.
    # Aliases, not literals - records.py owns the four strings now that the
    # scoring formula (plan 14.2) switches on them. Same values as before, so
    # nothing compared or stored changes; what changes is that a rename can no
    # longer leave two sets of four literals silently disagreeing.
    R_WIN = OUT_WIN
    R_LOSE = OUT_LOSE
    R_DRAW = OUT_DRAW
    R_NONE = OUT_NONE

    def _commit_result(self, result):
        """Bank the outcome the MOMENT it is known, then animate (plan 4.11).

        This used to happen at the END of the animation, so leaving the app
        mid-animation meant the result was never applied: no HP change, no
        recorded loss. At CLASSIC's 4.8 s that window was marginal. An EVO fight
        animates for 12-20 s, which turns it into a comfortable, repeatable way
        to dodge every loss. The animation is now pure playback of a result that
        has already been saved, so exiting part-way through is harmless.
        """
        self.result = result
        self.i_won = result == self.R_WIN
        self._result_applied = False
        self._apply_result()

    def _apply_result(self):
        # Structurally idempotent (plan 4.11). _commit_result is the only caller
        # now, but the animation-end and skip paths both used to apply the
        # result, and this guard is what makes any stray second call harmless
        # rather than a double-apply.
        if self._result_applied:
            return
        self._result_applied = True
        if self.result == self.R_NONE:
            # No contest: a dropped link, a timeout, a bad frame. Not the
            # player's fault, and in a dense field it will happen often - so the
            # pet is left byte-identical and nothing at all is recorded.
            return
        rec = self.records
        draw = self.result == self.R_DRAW
        if self.is_practice:
            # free: no HP change, tracked as a simple separate tally (no names).
            # There is no practice-draw counter - plan 8.1.2 keeps pw/pl only.
            if draw:
                pass
            elif self.i_won:
                rec["pw"] = rec.get("pw", 0) + 1
            else:
                rec["pl"] = rec.get("pl", 0) + 1
            self._save_records()
            return
        pet = self.app.pet
        if not pet.get("alive", True):
            # Pet died underneath us. The background simulation keeps running
            # during a battle and the selection phase alone can last 60 s, so
            # banking the result EARLIER does not remove this race - applying
            # WIN_HEALTH to a pet that just died would silently resurrect it.
            #
            # This also skips the battlepoints (plan 14.2), and that is a CHOICE
            # rather than a consequence - flagged for the owner. The argument for
            # it: bp track the ranked record, and this path records no W/L, so
            # scoring a fight that is not in the record splits one rule into two.
            # The argument against: the win did happen, points are trainer-level,
            # and a mon starving during the 60 s interaction is not the trainer's
            # fault in the way a dropped link is nobody's. Moving the award above
            # this guard is a one-line change if the owner wants the other rule.
            return
        # A draw costs what a win costs: symmetric, so it is never cheaper than
        # fighting, which is what stops stall-for-a-free-reset. Softer than a
        # loss, because a genuine dead heat is not a defeat.
        pet["health"] = WIN_HEALTH if (self.i_won or draw) else LOSE_HEALTH
        if draw:
            rec["d"] = rec.get("d", 0) + 1
        elif self.i_won:
            rec["w"] += 1
        else:
            rec["l"] += 1
        rec["log"].insert(0, {
            "o": self.opp.get("name", "???") if self.opp else "???",
            "r": "D" if draw else ("W" if self.i_won else "L"),
            # OUR pet's age in hours at the moment of the fight, so a trainer can
            # see how young their mon was when it took a scalp. Not a wall-clock
            # date: the badge RTC is never NTP-synced outside an OTA check, so
            # time.localtime() reads 2000-01-01 and a real date would be a lie.
            "a": int(pet.get("age", 0)),
        })
        rec["log"] = rec["log"][:MAX_LOG]
        self._save_records()
        self._award_battlepoints()
        try:
            self.app._save_state()
        except Exception as e:
            print("Battle: save pet failed:", e)

    def _award_battlepoints(self):
        """Add this fight's battlepoints to the trainer file (plan 14.2).

        Called from the RANKED path of _apply_result only, and deliberately from
        the bottom of it rather than the top. Everything that returns early above
        must also score nothing, and each for its own reason:

        - **practice** returns before this, which is plan 14.6's mandatory
          anti-farm rule. The auto-repeat practice loop is a few button presses
          and would otherwise farm the score indefinitely.
        - **no contest** returns before this, because a protocol failure has to
          leave the badge byte-identical (plan 5.7). Points are the one thing
          here that a mon's death does NOT wipe, so scoring a dropped link would
          make link-dropping a permanent, unclearable gain.
        - **a dead pet** returns before this, and that one is a judgement call
          rather than a consequence - the reasoning is at the guard itself.

        Points survive every mon's death (plan 14.2), which is why they are in
        the trainer file and not `state.json`: `_die()` and `_hatch_new()` cannot
        reach them from a different file. Monotonic - nothing subtracts, ever.
        """
        bp = battlepoints_for(self.result, self._sim_winner_hp, self._sim_ko)
        if not bp:
            return          # a loss scores zero: don't write the file for nothing
        tr = self._trainer()
        tr["bp"] = max(0, tr.get("bp", 0)) + bp
        # The SAME dict object the queue screen holds (_trainer caches it and
        # passes it in), so this cannot diverge from what that screen saves.
        save_trainer(tr)

    def _save_records(self):
        if self.archived:
            return  # a past mon's record is history: never write it back
        save_records(self.records)

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
        self._enforce_ble_ownership()
        st = self.state
        if st == "searching":
            self._update_searching(delta)
        elif st == "invited":
            self._update_invited(delta)
        elif st == "handshaking":
            self._update_handshaking(delta)
        elif st == "evo_select":
            self._update_evo_select(delta)
        elif st == "queues":
            if self._queue_screen is not None and self._queue_screen.done:
                self._close_queues()
        elif st == "anim":
            self._update_anim(delta)

    def _update_searching(self, delta):
        self._search_t += delta
        # PRELOAD the fight renderer here, where the slack is. Owner's idea, and
        # it is the right screen for it: the player is reading a peer list, a
        # ~250 ms hitch is invisible against a list that refreshes at 400 ms
        # anyway, and every route from here to a fight needs the renderer.
        #
        # Deliberately AFTER the first frame (`_search_t`), so it lands on a
        # frame that has already drawn rather than stacking onto the ~950 ms
        # ble_link import that got us onto this screen. Two big imports in one
        # frame is precisely the watchdog trap this whole scheme avoids.
        #
        # Only ever an optimisation - `draw()` loads it for real if this has not
        # run, which is what makes Practice (never passes through here) safe.
        if not Battle._fv_loaded and self._search_t > 700:
            self._ensure_fightview()
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
        self._action_flash = max(0.0, self._action_flash - delta)
        # The dice-off plays first, and the mons slide in behind it (plan 8.3).
        if self.anim_t < _VS_MS + _DICE_MS:
            return

        # PLAYBACK. Advance one tick per _PLAYBACK_MS of wall time by
        # accumulating frame delta - `while`, not `if`, so a dropped frame
        # CATCHES UP rather than stretching the fight (plan 4.1). The sim is
        # never re-run here; every value below is a lookup into the buffer.
        self._anim_acc += delta
        while self._anim_acc >= _PLAYBACK_MS and self._anim_tick < self._sim_ticks:
            self._anim_acc -= _PLAYBACK_MS
            self._anim_tick += 1
            my_a, opp_a = self._tick_actions(self._anim_tick)
            if my_a >= 0 or opp_a >= 0:
                self._my_action = my_a
                self._opp_action = opp_a
                self._action_flash = _ACTION_FLASH_MS
                # Queue position advances on OUR actions only, so the left
                # column tracks our rotation rather than the tick count. Ids
                # repeat (Tackle, Tackle), so a counter is the only honest way
                # to know which slot fired.
                if my_a >= 0:
                    self._my_slot += 1
                if Battle._fv_loaded or self._ensure_fightview():
                    self._push_move(my_a, opp_a)
            my_hp, opp_hp = self._tick_hp(self._anim_tick)
            my_bar = 100.0 * my_hp / self._my_cap
            opp_bar = 100.0 * opp_hp / self._opp_cap
            if my_bar < self.my_bar:
                self._flash_my = _HIT_FLASH_MS
            if opp_bar < self.opp_bar:
                self._flash_opp = _HIT_FLASH_MS
            self.my_bar, self.opp_bar = my_bar, opp_bar

        if (self._anim_tick >= self._sim_ticks
                and self._anim_acc >= _ENDING_MS):
            # The result was banked at simulation time (plan 4.11); reaching the
            # end of the animation only reveals it.
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
                self._abort_evo("Cancelled")
        elif st == "evo_select":
            self._evo_select_button(event)
        elif st == "prac_select":
            self._prac_select_button(event)
        elif st == "queues":
            if self._queue_screen is not None:
                self._queue_screen.button(event)
        elif st == "info":
            # EITHER button leaves. It is a dead-end notice with one way out,
            # so making the player find the right button is pure friction.
            if (BUTTON_TYPES["CANCEL"] in event.button
                    or BUTTON_TYPES["CONFIRM"] in event.button):
                self.state = "menu"
        elif st == "records":
            self._records_button(event)
        elif st == "rec_ranked":
            self._rec_ranked_button(event)
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
        # Skip: snap the visuals to the FINAL TICK. This must STAY - a 20 s
        # unskippable replay is worse than a 5 s one - but it no longer applies
        # the result. That happens at simulation time now (plan 4.11), so this
        # is purely cosmetic, and it reads the same buffer the playback does
        # rather than inventing an end state.
        self._anim_tick = self._sim_ticks
        my_hp, opp_hp = self._tick_hp(self._sim_ticks)
        self.my_bar = 100.0 * my_hp / self._my_cap
        self.opp_bar = 100.0 * opp_hp / self._opp_cap
        self._ghost_my = self.my_bar
        self._ghost_opp = self.opp_bar
        self._flash_my = 0.0
        self._flash_opp = 0.0
        self._action_flash = 0.0
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
            # No C: the detail for the highlighted row is already on the right,
            # so there is nothing left to "pick" and a disc offering it would
            # promise a screen that no longer exists.
            self._arc().configure(self._visible(self.rec_items),
                                  idx=self.rec_idx, side="left",
                                  hint_c=None, hint_f="F back")
            self._arc_state = st
            return
        elif st in ("evo_select", "prac_select"):
            # Built ONCE on entry. The comment here used to claim it was
            # "rebuilt whenever the selectable set changes", which _sync_arc's
            # own early return makes impossible - and it does not need to be:
            # `_sel_queues` is only ever assigned BEFORE this state is entered
            # (_open_queue_pick, _enter_evo_select). If that ever stops being
            # true, this branch will silently show a stale list, so change the
            # guard rather than this comment (review plan 2.3).
            m = self._arc()
            # Smaller than every other menu: the selected row grows inward from
            # the bezel and this screen has a countdown, an action column and a
            # peer status sharing the middle with it.
            m.configure([n for n, _q in self._sel_queues] or ["Default"],
                        idx=self._sel_idx, side="left",
                        hint_c="C fight" if st == "prac_select" else "C lock in",
                        hint_f="F back" if st == "prac_select" else "F cancel",
                        font=_SEL_MENU_ROW, font_sel=_SEL_MENU_SEL)
            self._arc_state = st
            return
        else:
            self._arc_state = st
            return
        self._arc().configure(self._visible(src), idx=idx, side="left",
                              hint_c=self._ARC_HINT_C, hint_f="F back")
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
        elif item == "Action queues":
            self._open_queues()
        elif item == "Find opponent":
            # First point the radio is actually wanted, so this is where its
            # import is paid - not on opening the battle menu.
            if self._ensure_ble() is None:
                self.message = "Bluetooth not\navailable here."
                self.state = "info"
                return
            if not self._evo_enabled:
                # The kill switch (plan 5.1). There is no Classic to fall back
                # to, so this disables battling rather than downgrading it -
                # Practice is untouched, because it needs no radio.
                self.message = "Battling is off\nin this build.\nPractice works."
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
                self._start_evo(peer, True)

    # --- BATTLE_EVO! session (plan 5.2-5.6) --------------------------------
    _EVO_SELECT_SECS = 20     # the countdown the player sees (plan 5.4). Our
    #                           own clock; the peer's number is never trusted.

    def _start_evo(self, peer, challenger):
        """Open a session with `peer`. `challenger` picks the BLE role: the
        challenger advertises a targeted invite (peripheral), the acceptor
        connects (central). Both halves must work - the badge can be either."""
        if not self._handshake_ready():
            return
        if not peer.get("evo", False):
            # Advertised no 0xF012 marker, so it cannot be battled. Say so
            # rather than connecting and failing (plan 5.1). This is only the
            # early out - a badge with no marker but a real 0xF011 still works,
            # and one that lies about the marker is caught after connecting.
            self._evo_too_old(peer.get("name", "???"))
            return
        # PLAN 5.10 PART 1 - "they got in first".
        #
        # Two players agree to fight and both press C. Challenging STOPS
        # discovery and starts a targeted connectable advert, so a badge that
        # has already committed to challenging is no longer scanning and can
        # never see the other's invite: both sit out the 15 s window and both
        # report NO ANSWER. It is the most natural thing two people can do.
        #
        # This catches the common half - one person a moment ahead of the other
        # - and it is nearly free, because the invite is already sitting in the
        # scan results we collected a moment ago. If the peer we are about to
        # challenge has ALREADY invited us, take theirs instead of shouting over
        # it. Their `device` handle comes from the invite itself, which is the
        # one we must connect to.
        #
        # Only ever turns a failure into a battle: if the invite is stale
        # `pending_invite()` has already dropped it, and if the connect fails we
        # land on the same NO ANSWER we would have had anyway (plan 5.10).
        if challenger and self.ble is not None:
            inv = self.ble.pending_invite()
            if inv is not None and inv.get("addr") == peer.get("addr"):
                challenger = False
                peer = inv
                self.ble.clear_invite()
        self._invite_peer = peer
        # Carried in the mon frame because it is part of the frozen 17-byte
        # blob, and then never used: the seed comes from the commit-reveal
        # salts, which is what closes the plan 3.3 grinding defect.
        self._my_nonce = random.getrandbits(32)
        self.my_str = _clean_strength(self.app.pet.get("strength", 5))
        # Phase 1 fields the default queue. The builder arrives in Phase 3;
        # everything below is already queue-agnostic.
        self._my_queue = default_queue_for(self.app.pet)
        self._my_salt = bytes(random.getrandbits(8) for _ in range(SALT_LEN))
        self._evo_secs = self._EVO_SELECT_SECS
        self._evo_acc = 0.0
        # SNAPSHOT our mon frame for the whole session. It must not be rebuilt
        # later: the peer binds their commitment to the frame they received, and
        # `age` ticks up in the background - a session that straddles an hour
        # boundary would otherwise fail its own verification, turning an honest
        # battle into a no contest.
        self._my_mon = self.pack_mon()
        start = (self.ble.start_evo_invite if challenger
                 else self.ble.start_evo_accept)
        self._evo = start(
            peer, pack_version(), self._my_mon,
            pack_lock(False, self._EVO_SELECT_SECS),
            (TAG_LOCK, TAG_COMMIT, TAG_REVEAL),
        )
        self.message = (("Challenging\n" if challenger else "Connecting to\n")
                        + peer.get("name", "???") + "...")
        self.state = "handshaking"

    def _evo_too_old(self, name):
        """A peer this build cannot battle (plan 5.1, constraint 12). Never
        silent, and never an attempt at the legacy exchange - this build does
        not implement one."""
        self.message = "TOO OLD\n" + name + " runs an\nolder EMFMon"
        self._leave_searching("info")

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
        """Acceptor: connect to the challenger and run the session."""
        peer = self._invite_peer
        if peer is None:
            self.state = "searching"
            return
        if self.ble is not None:
            self.ble.clear_invite()
        # An invite reaches us through the manufacturer beacon, which carries no
        # capability marker, so `evo` is unknown here. Assume capable and let
        # the missing 0xF011 characteristic settle it after connecting - that
        # check is the authoritative one anyway (plan 5.1).
        peer = dict(peer)
        peer["evo"] = True
        self._start_evo(peer, False)

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
        """Connect -> version -> mon. The moment the session reaches its
        selection phase the UI moves on; everything before that is a spinner."""
        sess = self._evo
        if sess is None:
            self._abort_evo("Link error")
        elif sess.status == sess.FAILED:
            self._evo_failed(sess)
        elif sess.status == sess.SELECTING:
            self._enter_evo_select(sess)

    def _enter_evo_select(self, sess):
        mon = unpack_mon(sess.peer_mon)
        if mon is None:
            self._abort_evo("Bad data")
            return
        self.opp = {
            "name": mon["name"],
            "shape": mon["shape"],
            "colour": mon["colour"],
            "strength": mon["strength"],   # inert in EVO (plan 4.7)
            "age": mon["age"],
            "id": sess.peer_addr,          # informational only
        }
        self.is_practice = False           # networked = ranked (HP + W/L cost)
        self._evo_secs = self._EVO_SELECT_SECS
        self._evo_acc = 0.0
        self._evo_rows_dirty = True
        self._sel_queues = self._selectable_queues()
        self._sel_idx = 0
        for i, (_n, q) in enumerate(self._sel_queues):
            if q == self._my_queue:
                self._sel_idx = i
                break
        self.state = "evo_select"
        self._arm_input_lock()

    def _selectable_queues(self):
        """(name, wire queue) for everything this mon can actually field.

        Row 0 is 14.5's default, derived live from the current mon and
        therefore always valid - which is what guarantees this list is never
        empty. Saved queues only appear if THIS mon can field every entry
        (plan 14.4b): an unusable queue must never reach the wire, where it
        would be a protocol violation on the receiving side and hand the
        opponent a no contest through no fault of theirs.
        """
        out = [("Default", default_queue_for(self.app.pet))]
        tr = self._trainer()
        have = innate_actions_for(self.app.pet)
        pool = tr.get("pool", ())
        for i, q in enumerate(tr.get("queues", ())):
            if q and all(a in have or a in pool for a in q):
                out.append(("Saved %d" % (i + 1), pack_queue(q)))
        return out

    def _update_evo_select(self, delta):
        """The one connected phase that lasts (plan 5.4). The countdown is
        LOCAL: a peer's secs_left is displayed, never obeyed, or it becomes a
        stall lever."""
        sess = self._evo
        if sess is None:
            self._abort_evo("Link error")
            return
        if sess.status == sess.FAILED:
            self._evo_failed(sess)
            return
        if sess.status == sess.REVEALED:
            self._on_evo_revealed(sess)
            return
        self._evo_acc += delta
        if self._evo_acc >= 1000.0:
            # Once a SECOND, not per frame: this rebuilds a status frame and a
            # countdown string, and per-frame allocation is what fragmented the
            # heap into an OOM reboot before (plan 6.2).
            self._evo_acc -= 1000.0
            if self._evo_secs > 0:
                self._evo_secs -= 1
            self._evo_rows_dirty = True
            try:
                sess.set_status(pack_lock(sess.locked, self._evo_secs))
            except Exception as e:
                print("Battle: evo status:", e)
            if self._evo_secs <= 0 and not sess.locked:
                # Timer expired: auto-lock with what is on screen, which in
                # Phase 1 is always the default queue (plan 5.4).
                self._evo_lock_in()

    def _evo_lock_in(self):
        """Locking in IS the commitment (plan 5.5). No take-backs after this -
        that is precisely what stops counter-picking."""
        sess = self._evo
        if sess is None or sess.locked:
            return
        binding = sess.peer_mon
        if not binding:
            self._abort_evo("Link error")
            return
        h = commit_hash(self._my_queue, self._my_salt, binding)
        sess.lock_in(pack_commit(h), pack_reveal(self._my_queue, self._my_salt))
        sess.set_status(pack_lock(True, self._evo_secs))
        self._evo_rows_dirty = True

    def _on_evo_revealed(self, sess):
        """Both queues are known and the radio is already released. Verify,
        seed, resolve, BANK THE RESULT, then animate (plan 5.5 step 5)."""
        rev = unpack_reveal(sess.peer_reveal)
        if rev is None:
            self._abort_evo("Bad data")
            return
        peer_queue, peer_salt = rev
        # Verify the reveal against the commitment. A mismatch means they tried
        # to change their queue after seeing ours - no contest (plan 5.5 step 3).
        expect = commit_hash(peer_queue, peer_salt, self._my_mon)
        if sess.peer_commit != pack_commit(expect):
            self._abort_evo("Bad data")
            return
        peer_queue = _clean_queue(peer_queue)
        if peer_queue is None:
            self._abort_evo("Bad data")
            return
        if peer_salt == self._my_salt:
            # Equal salts leave neither side "lower", so both would order
            # themselves identically and compute different fights. 1 in 2^32 by
            # accident; trivial for a hostile peer to force.
            self._abort_evo("Bad data")
            return
        seed, i_am_lower = evo_seed(self._my_salt, peer_salt)
        self._peer_queue = peer_queue
        self._evo = None
        self._invite_peer = None
        # The radio is already released (plan 5.5 step 5, constraint 1). Both
        # badges now hold identical inputs and run the SAME simulator, so they
        # reach the same winner without exchanging another byte.
        self._run_fight(self._my_queue, peer_queue, seed, i_am_lower)

    def _evo_failed(self, sess):
        """Turn a session failure into the right message, and the right cost.

        Every one of these is a NO CONTEST: the pet is left byte-identical and
        nothing is recorded (plan 5.7). A dropped link is not the player's
        fault and in a dense field it will happen often.
        """
        kind = sess.fail_kind
        name = (self._invite_peer or {}).get("name", "They")
        if kind == sess.F_OLD_PEER:
            self._evo = None
            self._invite_peer = None
            self._evo_too_old(name)
            return
        if kind == sess.F_VERSION:
            # A version frame can differ two ways and they are not the same
            # fault. If it PARSES, the peer is a real EVO badge on a different
            # proto/rules build - a genuine version problem. If it does not,
            # the frame was malformed, and calling that "a different version"
            # sends the player chasing an update they do not need.
            if unpack_version(sess.peer_ver or b"") is None:
                msg = "NO CONTEST\n" + name + " sent\nsomething odd"
            else:
                msg = "VERSION ERROR\n" + name + " runs a\ndifferent EMFMon"
            self._abort_evo(msg, raw=True)
            return
        self._abort_evo(sess.error or "Link error")

    # Reasons a session can end with nothing staked, and what to SAY about it.
    #
    # The old text was "Bad data / Stay close, retry" for everything, which got
    # two things wrong: it blamed the radio for what is usually a protocol
    # fault, and it never mentioned the part that actually matters to a player -
    # that a no contest costs them nothing (plan 5.7). A dropped link in a
    # crowd is common and is not their fault, so the screen should not read
    # like a punishment.
    _ABORT_MSG = {
        "Bad data": "NO CONTEST\ntheir badge sent\nsomething odd",
        "Link error": "NO CONTEST\nthe link dropped",
        "Cancelled": "CANCELLED",
        "No answer": "NO ANSWER\nthey did not\npick it up",
        "Timed out": "NO CONTEST\nthey took too long",
        "Out of step": "NO CONTEST\nthe two badges\nlost step",
    }

    def _abort_evo(self, msg, raw=False):
        if self.ble is not None:
            try:
                self.ble.cancel_handshake()
            except Exception as e:
                print("Battle: evo cancel:", e)
        self._evo = None
        self._invite_peer = None
        if raw:
            self.message = msg
        else:
            self.message = self._ABORT_MSG.get(msg, "NO CONTEST\n" + msg)
        self.state = "info"

    _REC_ROWS = 6  # ranked-log rows visible at once

    def _records_button(self, event):
        # LEFT/RIGHT scroll the ranked log on the right, the same way the queue
        # builder uses them to act on ITS right-hand column. Checked before the
        # menu sees them: ArcMenu ignores left/right but still stamps its
        # debounce clock, which would eat the next press.
        b = event.button
        if (BUTTON_TYPES["LEFT"] in b or BUTTON_TYPES["RIGHT"] in b):
            if self._rec_sel() == "Ranked":
                n = len(self.records.get("log", []))
                step = -self._REC_ROWS if BUTTON_TYPES["LEFT"] in b \
                    else self._REC_ROWS
                top = max(0, n - self._REC_ROWS)
                self.rec_scroll = min(top, max(0, self.rec_scroll + step))
                self._rec_rows_dirty = True
            return
        self._sync_arc()
        m = self._arc()
        act = m.button(event)
        if m.idx != self.rec_idx:
            self.rec_idx = m.idx
            self.rec_scroll = 0          # a different set starts at the top
            self._rec_rows_dirty = True
        if act == "back":
            self.state = "menu"
        # There is no "select": both sets are readable from here, so opening a
        # sub-screen would be a press that changes nothing but the framing.
        # rec_ranked survives as its own state for the History view, which is
        # reached from the pet menu with no records list behind it.

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
        elif st == "searching":
            self._draw_searching(ctx)
        elif st == "invited":
            self._draw_invited(ctx)
        elif st == "handshaking":
            self._draw_handshaking(ctx)
        elif st == "evo_select":
            self._draw_evo_select(ctx)
        elif st == "prac_select":
            self._draw_evo_select(ctx)
        elif st == "queues":
            if self._queue_screen is not None:
                self._queue_screen.draw(ctx)
        elif st in ("anim", "result"):
            # Guaranteed load. The preload on the search screen is an
            # optimisation; correctness must not depend on the player
            # having passed through it (Practice never does).
            self._ensure_fightview()
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
        """Red rim framing BATTLE MODE. Drawn AFTER the menu so the scrim does
        not dim it.

        A FULL circle, deliberately. The broken rim belongs to the fight screen
        alone, where the break down the middle separates your half from theirs;
        there are no halves in a menu, so the same shape here would be
        decoration borrowed from somewhere it meant something.
        """
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
        """A dead-end notice. First line is the heading, the rest is detail,
        and a no contest says outright that nothing was lost."""
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        lines = self.message.split("\n")
        n = len(lines)
        y = -18 - (n - 1) * 9
        for i, line in enumerate(lines):
            if i == 0:
                # First line is always a short LABEL, never the start of a
                # sentence - a heading reading "TROE has a" looks broken.
                ctx.font_size = 19
                ctx.rgb(*(_RESULT_RGB["D"]
                          if line.startswith(("NO ", "CANCEL", "NO ANSWER"))
                          else _RESULT_RGB["L"]))
            else:
                ctx.font_size = 15
                set_color(ctx, "label")
            ctx.move_to(0, y).text(line)
            y += 24 if i == 0 else 20
        if self.message.startswith("NO CONTEST"):
            ctx.font_size = 12
            ctx.rgb(*_RESULT_RGB["W"])
            ctx.move_to(0, y + 8).text("nothing lost")
        draw_hints(ctx, c="C ok", f="F back")

    def _rec_sel(self):
        """Which record set is under the cursor."""
        items = self._visible(self.rec_items)
        if not items:
            return "Ranked"
        return items[min(max(0, self.rec_idx), len(items) - 1)]

    def _draw_records(self, ctx):
        """List on the left, that set's detail on the right - the queue
        builder's shape, because it is the same question: pick one of a short
        list, read what it contains.

        It replaces a menu whose only job was to open one of two screens. Both
        fit beside the list, so the press that used to open them bought nothing
        but a change of framing.
        """
        self._sync_arc()
        m = self._arc()
        m.draw(ctx, hint=False)      # first: it lays down the scrim
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 17
        ctx.move_to(0, -100).text("Records")
        if self._rec_sel() == "Practice":
            self._draw_rec_practice_col(ctx)
        else:
            self._draw_rec_ranked_col(ctx)
        m.draw_hint(ctx)             # last: the call-outs stay on top
        # draw_hints only shows the stick alongside C, and this screen has no C
        # - but the stick is the ONLY control here (up/down pick the set,
        # left/right page the log), so it is the one glyph that must appear.
        # Called directly rather than by teaching the shared ArcMenu a new flag:
        # one screen's need is not worth a field every other screen inherits.
        draw_joystick_icon(ctx)

    def _draw_rec_ranked_col(self, ctx):
        """The ranked log, in the right column."""
        if self._rec_rows_dirty:
            self._build_rec_rows()
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        # W and L across the top of the column, each in its own outcome colour
        # so the tally never has to be inferred from "not a win".
        #
        # No draws. They are vanishingly rare - both mons have to fall on the
        # same tick - so the slot spent almost its whole life reading "0D",
        # which is a third of the tally saying nothing. The count is still
        # kept and still shown in the log rows when one happens; it just does
        # not hold a permanent place for an event most players never see.
        ctx.font_size = 22
        ctx.rgb(*_RESULT_RGB["W"])
        ctx.move_to(_REC_X - 24, -72).text(self._rec_head[0])
        ctx.rgb(*_RESULT_RGB["L"])
        ctx.move_to(_REC_X + 24, -72).text(self._rec_head[1])
        if not self._rec_rows:
            ctx.font_size = 12
            set_color(ctx, "label")
            ctx.move_to(_REC_X, 0).text("no fights yet")
            return
        ctx.font_size = 12
        for row, (main, _age_s, res) in enumerate(self._rec_rows):
            ctx.rgb(*_RESULT_RGB[res])
            ctx.move_to(_REC_X, _REC_TOP + row * _REC_ROW).text(main)
        if self._rec_foot is not None:
            # Centred across the whole screen, not over the column: it counts
            # the pages, and the pages are the screen. Same height as before -
            # it moves left to the middle, not up or down.
            ctx.font_size = 15
            ctx.rgb(1.0, 1.0, 1.0)
            ctx.move_to(0, 71).text(self._rec_foot)

    def _draw_rec_practice_col(self, ctx):
        """Practice has no log worth scrolling - just the tally."""
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        # Stacked, not side by side. Side by side the pair grew ACROSS the
        # column and reached back into the menu's selected row, so the size had
        # to keep shrinking to fit a width it did not have. Stacked, the only
        # limit is height - which this column has to spare - so the numbers can
        # stay big and a three-digit tally costs nothing.
        # One per quadrant, wins upper-right and losses lower-right, at the
        # same _RESULT_Y the fight screen puts its verdict at - so the two
        # numbers are as far apart as this side of the screen allows and read
        # as two separate facts rather than one stacked pair.
        ctx.font_size = 28
        ctx.rgb(*_RESULT_RGB["W"])
        ctx.move_to(_REC_X, -_RESULT_Y).text("%d" % self.records.get("pw", 0))
        ctx.rgb(*_RESULT_RGB["L"])
        ctx.move_to(_REC_X, _RESULT_Y).text("%d" % self.records.get("pl", 0))
        # Labels ABOVE their number, and light enough to actually read - they
        # were 0.55 grey on a dark scrim, which is the sort of "present but
        # invisible" that reads as a rendering fault rather than as a caption.
        # Stacking loses the left-to-right "W then L" the old pair got free, so
        # these are carrying real meaning, not decoration.
        ctx.font_size = 12
        ctx.rgb(0.80, 0.80, 0.84)
        ctx.move_to(_REC_X, -_RESULT_Y - 22).text("won")
        ctx.move_to(_REC_X, _RESULT_Y - 22).text("lost")

    def _build_rec_rows(self):
        """Pre-render the ranked screen's strings. Records are immutable while
        it's open, so this runs on entry and on scroll - not 60x/s."""
        log = self.records.get("log", [])
        start = min(self.rec_scroll, max(0, len(log) - self._REC_ROWS))
        rows = []
        for e in log[start:start + self._REC_ROWS]:
            res = e.get("r")
            if res not in _RESULT_RGB:
                res = "L"     # unreadable row: never flatter it into a win
            age = e.get("a")
            rows.append((
                res + " vs " + str(e.get("o", "???")),
                ("%dh" % age) if isinstance(age, int) else None,
                res,
            ))
        self._rec_rows = rows
        self._rec_head = ("%dW" % self.records.get("w", 0),
                          "%dL" % self.records.get("l", 0),
                          "%dD" % self.records.get("d", 0))
        self._rec_foot = None
        if len(log) > self._REC_ROWS:
            # Solid triangles, not button letters: the stick is what scrolls,
            # and naming buttons would contradict the discs, which never do.
            # These are real glyphs in the badge font (EMFCampFont.h lists
            # the arrow and triangle block), not drawn shapes - so they take
            # the text colour and scale with font_size like anything else.
            self._rec_foot = "%s %d-%d/%d %s" % (
                TRI_LEFT, start + 1,
                min(len(log), start + self._REC_ROWS), len(log), TRI_RIGHT)
        self._rec_rows_dirty = False

    def _draw_rec_ranked(self, ctx):
        if self._rec_rows_dirty:
            self._build_rec_rows()
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 17
        ctx.move_to(0, -96).text(self.rec_title or "Ranked")
        ctx.font_size = 20
        ctx.rgb(*_RESULT_RGB["W"]).move_to(-44, -70).text(self._rec_head[0])
        ctx.rgb(*_RESULT_RGB["L"]).move_to(0, -70).text(self._rec_head[1])
        ctx.rgb(*_RESULT_RGB["D"]).move_to(44, -70).text(self._rec_head[2])
        ctx.font_size = 13
        if not self._rec_rows:
            set_color(ctx, "label")
            ctx.move_to(0, -6).text("No ranked fights yet")
        else:
            for row, (main, age_s, res) in enumerate(self._rec_rows):
                y = -44 + row * 18
                ctx.font_size = 13   # reset: the age suffix below shrinks it
                ctx.rgb(*_RESULT_RGB[res])
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
            # A badge with no 0xF012 marker cannot be battled by this build, so
            # mark it here rather than letting the player invite it and only
            # find out after the connection (plan 5.1).
            evo = peer.get("evo", False)
            rows.append((
                ("> " if sel else "   ") + peer["name"] + ("" if evo else " (old)"),
                _signal_level(peer.get("rssi")),
                sel,
                evo,
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
            for row, (label, level, sel, evo) in enumerate(self._peer_rows):
                y = -48 + row * 22
                ctx.text_align = ctx.LEFT
                ctx.font_size = 14
                if not evo:
                    ctx.rgb(0.42, 0.42, 0.42)   # greyed out: cannot be battled
                elif sel:
                    ctx.rgb(0.9, 0.7, 0.1)
                else:
                    set_color(ctx, "label")
                ctx.move_to(-56, y).text(label)
                _draw_signal_bars(ctx, 42, y, level)
            ctx.text_align = ctx.CENTER
            draw_hints(ctx, c="C fight", f="F back")

    def _draw_invited(self, ctx):
        """The incoming-challenge popup.

        Not an ArcMenu: there is no list here, only yes or no, and C/F already
        say exactly that. What it DID need was the standard call-outs - it was
        still painting "C: accept  F: decline" as grey text along the bottom,
        from before the ringed discs existed, so the one screen a stranger
        makes you look at was the one screen that did not match the rest.
        """
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        peer = self._invite_peer or {}
        ctx.font_size = 20
        ctx.rgb(0.9, 0.7, 0.1).move_to(0, -52).text("CHALLENGE!")
        ctx.font_size = 18
        set_color(ctx, "label")
        ctx.move_to(0, -18).text(str(peer.get("name", "???")))
        ctx.font_size = 14
        ctx.rgb(0.6, 0.6, 0.6).move_to(0, 6).text("wants to battle")
        # No joystick glyph: the stick does nothing here, and showing it would
        # promise a list to scroll that does not exist.
        draw_hints(ctx, c="C accept", f="F decline", joy=False)

    def _prac_select_button(self, event):
        """Same picker, no clock and no commitment: C fights, F backs out."""
        self._sync_arc()
        m = self._arc()
        act = m.button(event)
        if self._sel_queues and m.idx != self._sel_idx:
            self._sel_idx = min(m.idx, len(self._sel_queues) - 1)
            self._my_queue = self._sel_queues[self._sel_idx][1]
            self._evo_rows_dirty = True
        if act == "select":
            self._start_practice_fight()
        elif act == "back":
            self.state = "menu"
            self._arc_state = None

    def _evo_select_button(self, event):
        # Picking a queue IS the selection phase (plan 8.2), so it runs on the
        # shared ArcMenu and reads like the builder the queues were made in -
        # list on the left, that queue's actions on the right.
        #
        # Locked means locked: once the commitment is on the wire the list stops
        # responding, because there are no take-backs after a commit-reveal
        # (plan 5.5) and a control that still moves would imply otherwise.
        locked = self._evo is not None and self._evo.locked
        self._sync_arc()
        m = self._arc()
        act = m.button(event)
        if locked:
            return
        if self._sel_queues and m.idx != self._sel_idx:
            self._sel_idx = min(m.idx, len(self._sel_queues) - 1)
            self._my_queue = self._sel_queues[self._sel_idx][1]
            self._evo_rows_dirty = True
        if act == "select":
            self._evo_lock_in()
        elif act == "back":
            # Bailing out before the commitment costs nothing (no contest), and
            # after it the session is already past this screen.
            self._abort_evo("Cancelled")

    def _build_evo_rows(self):
        """Pre-render the selection screen. Rebuilt on a state change or once a
        second, never per frame (plan 6.2)."""
        sess = self._evo
        q = self._my_queue or ALL_TACKLE
        rows = []
        for i in range(queue_len(q)):
            rows.append(ACTIONS[q[i]][0])
        peer_locked = False
        if sess is not None and sess.peer_status is not None:
            got = unpack_lock(sess.peer_status)
            if got is not None:
                peer_locked = got[0]
        self._evo_rows = (rows, peer_locked)
        self._evo_rows_dirty = False

    def _draw_evo_select(self, ctx):
        if self._evo_rows_dirty or self._evo_rows is None:
            self._build_evo_rows()
        rows, peer_locked = self._evo_rows
        locked = self._evo is not None and self._evo.locked
        # Practice borrows this screen whole, minus the two things that only
        # exist because a peer does: the countdown and their lock status. What
        # is left - pick a queue, see its actions, commit - is the same job.
        networked = self.state == "evo_select"
        self._sync_arc()
        self._arc().draw(ctx, hint=False)  # first: it lays down the scrim
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 15
        ctx.move_to(0, -96).text(
            ("vs " if networked else "practice vs ")
            + (self.opp or {}).get("name", "???"))
        # The countdown is ours. Their secs_left is never allowed to drive it.
        # At zero there is still up to 10s of grace before the link's hard
        # ceiling (ble_link._EVO_SELECT_TOTAL_MS), because their countdown starts
        # later than ours and the frames take time. Showing a frozen "0" through
        # that window reads as a hung badge, so say what is being waited on.
        #
        # Sits over the action column, not centred: the ArcMenu owns the left.
        if not networked:
            pass
        elif self._evo_secs > 0:
            ctx.font_size = 30
            ctx.rgb(*(_RESULT_RGB["L"] if self._evo_secs <= 5
                      else _RESULT_RGB["D"]))
            ctx.move_to(_SEL_ACT_X, -62).text("%d" % self._evo_secs)
        else:
            ctx.font_size = 14
            ctx.rgb(*_RESULT_RGB["D"])
            ctx.move_to(_SEL_ACT_X, -68).text("time's up")
            ctx.font_size = 11
            ctx.rgb(0.6, 0.6, 0.6)
            ctx.move_to(_SEL_ACT_X, -52).text("waiting...")
        # The actions of the queue under the cursor, on the right at a readable
        # size - the same column the builder uses, so what you picked here looks
        # like what you built there.
        #
        # Once locked, LOCKED IN! replaces that column outright. It is the only
        # thing that matters from then on, and leaving the actions up would
        # invite one more look at a choice that can no longer be changed.
        if locked:
            # Same treatment as the builder's SAVED! - green, in place of the
            # column, with one quiet line saying what it means. The two are the
            # only "that worked, and it is final" moments in the app, so they
            # are deliberately the same shape rather than merely similar.
            ctx.font_size = 24
            ctx.rgb(*_RESULT_RGB["W"])
            ctx.move_to(_SEL_ACT_X, -14).text("LOCKED")
            ctx.move_to(_SEL_ACT_X, 12).text("IN!")
            ctx.font_size = 12
            ctx.rgb(0.55, 0.55, 0.58)
            ctx.move_to(_SEL_ACT_X, 38).text("no take-backs")
        else:
            ctx.font_size = _SEL_ACT_SIZE
            set_color(ctx, "label")
            top = -((len(rows) - 1) * _SEL_ACT_ROW) / 2.0
            for i, label in enumerate(rows):
                ctx.move_to(_SEL_ACT_X, top + i * _SEL_ACT_ROW).text(label)
        ctx.font_size = 11
        if not networked:
            pass
        elif peer_locked:
            ctx.rgb(*_RESULT_RGB["W"]).move_to(_SEL_ACT_X, 74).text("they're locked")
        else:
            ctx.rgb(0.6, 0.6, 0.6).move_to(_SEL_ACT_X, 74).text("they're choosing")
        # last, so the C/F call-outs are never clipped by anything above
        self._arc().draw_hint(ctx)

    def _draw_handshaking(self, ctx):
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 16
        for i, line in enumerate(self.message.split("\n")):
            ctx.move_to(0, -14 + i * 22).text(line)
        # No joystick glyph: there is nothing to scroll while connecting, and
        # offering one would suggest the wait is doing more than waiting.
        draw_hints(ctx, f="F cancel", joy=False)



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
