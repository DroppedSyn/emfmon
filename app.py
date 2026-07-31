"""EMFMon - a Tamagotchi-style pet for the Tildagon badge.

The pet is a randomly-coloured square or triangle that wanders the screen and
has four 0-100 stats (higher = better): Health, Food, Fun, Clean. Food/Fun/Clean
decay over real time; each need below 25% turns red and, on the 30-minute health
tick, drops Health by 10% - so all three in the red costs 30%, and neglect
compounds. Once Health is low the pet has a small chance of dying on each
20-minute death roll. The pet grows from a dot to full size, leaves
"poop" dots as it gets dirty (Clean wipes them), and accrues one heal item every
30 min. Runs in the background, persists to a state file, keeps a history of past
pets, and shows a "mon!" tag on the home screen when it needs attention.

Buttons (foreground):
  UP=Food  DOWN=Play  RIGHT=Clean  CONFIRM=Heal(spend a heal item)
  LEFT=menu (rename / history / new pet)   CANCEL=exit
"""

import json
import math
import random

import app
from app_components import TextDialog, clear_background
from app_components.tokens import set_color

from .arcmenu import (
    ArcMenu,
    arc_text_layout,
    draw_arc_text,
    draw_hints,
    FONT_ROW,
    FONT_SEL,
    ticks_diff,
    ticks_ms,
)
from events.input import BUTTON_TYPES, ButtonDownEvent
from events.joystick import JOYSTICK_BUTTON_TYPES
from system.eventbus import eventbus
from system.notification.events import ShowNotificationEvent
from system.scheduler import scheduler


def _seed_rng():
    # Seed the RNG from hardware entropy so every badge doesn't hatch the SAME
    # pet. `random` is seeded once at boot from esp_random(), but that early in
    # boot (before RF is up) freshly-flashed badges can get near-identical seeds
    # -> the same sequence -> everyone gets the first shape in SHAPES (square).
    # os.urandom is the ESP32 hardware RNG and, by the time the app launches
    # (WiFi has been up to download it), has full entropy. Mix in ticks too.
    seed = 0
    try:
        import time
        seed ^= time.ticks_us() & 0xFFFFFFFF
    except Exception:
        pass
    try:
        import os
        b = os.urandom(4)
        seed ^= b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)
    except Exception:
        pass
    # A per-badge constant, and the only source here that cannot go missing or
    # come up empty. It carries no per-BOOT entropy so it does not replace the
    # two above; what it guarantees is that if those both fail the badges still
    # diverge from EACH OTHER instead of all hatching one shared pet.
    try:
        import machine
        for i, byte in enumerate(machine.unique_id()):
            seed ^= byte << ((i % 4) * 8)
    except Exception:
        pass
    # Belt and braces. Seed 0 is a DEGENERATE state for MicroPython's PRNG
    # rather than merely a boring one: measured on a Tildagon, random.seed(0)
    # makes the shape draw come out SHAPES[0] - a square - every single time.
    # Only reachable now if all three sources above failed.
    if seed == 0:
        seed = 0x9E3779B9
    try:
        random.seed(seed & 0xFFFFFFFF)
    except Exception:
        pass


_seed_rng()

# --- Tunables --------------------------------------------------------------
# HOUR_MS governs age/health/death only (the "hourly" tick). Needs decay on
# their own real-time schedule below, so changing HOUR_MS does NOT change how
# fast the pet gets hungry.
# ---------------------------------------------------------------------------
# DEV TIME SCALE - MUST BE 1 IN ANY BUILD A PLAYER TOUCHES.
#
# Divides the two clocks that gate a mon's LIFE STAGE: HOUR_MS (age, adult at
# 6 h) and GROW_MS (size, full at 12 h). At 60 an "hour" passes in a minute, so
# a newborn is an adult in six minutes and full-size in twelve - which is what
# makes it possible to test anything that needs an adult mon without losing a
# day to it.
#
# It does NOT touch the needs (they decay on their own real-time schedule, see
# below) or the health/death ticks, so a scaled build is not a faithful pet -
# it is a rig for reaching a life stage quickly.
#
# `blelab/badge/selftest.py` ASSERTS this is 1, reading the deployed source off
# the badge. That is deliberate: a forgotten dev clock cannot be caught by
# looking at the app - a mon just quietly lives sixty times too fast - so the
# release gate refuses instead of trusting anyone to remember.
_DEV_TIME_SCALE = 1

HOUR_MS = 3600_000 // _DEV_TIME_SCALE  # one "hour" of pet time (age only)
DEATH_MS = 1200_000  # a death roll is made this often (every 20 minutes)
HEALTH_TICK_MS = 1800_000  # health tick interval at maturity (every 30 minutes)
# A newborn's health ticks this often instead; the interval eases up to
# HEALTH_TICK_MS by HEALTH_MATURE_AGE hours. Faster ticks make neglect of a young
# pet actually show on the health bar (a 30-min tick is invisible in a session).
HEALTH_TICK_YOUNG_MS = 600_000  # 10 minutes for a newborn
# How long (real minutes) each need takes to fall from full (100) to empty (0).
# Real-time and independent of HOUR_MS -> food gets hungry in ~10 min at any
# speed. Health is NOT in here: it only moves on the hourly tick.
MINUTES_TO_EMPTY = {"food": 10.0, "fun": 15.0, "clean": 20.0}
# Older pets are hardier: each hour of age reduces need-decay by this fraction,
# down to DECAY_MIN_MULT (decay slows but never stops or reverses).
DECAY_AGE_REDUCTION = 0.05
DECAY_MIN_MULT = 0.1
RED_AT = 25.0        # a need below this shows red AND hurts health (25%)
NOTIFY_AT = 30.0     # show the "mon!" alert below this (>= RED_AT, an early warning)
ACTION_GAIN = {"food": 35.0, "fun": 35.0, "clean": 40.0, "injection": 30.0}
# --- inventory -------------------------------------------------------------
# Consumables the mon carries. Items are restoratives and buffs ONLY: Play and
# Clean are free actions and are never items. Adding a new one is a single entry
# here plus, if it does something other than restore health, a branch in
# _use_item(); the inventory screen, HUD, save migration, validation and the
# grant loop are all driven off this table.
#   label    shown on the Inventory screen
#   short    shown on the pet HUD (space is tight - keep it to ~7 chars)
#   heal     HP restored on use; None for an item that isn't a restorative
#   cap      most the mon can carry
#   gain_ms  granted per this much on-time; None = never granted automatically,
#            it has to come from somewhere else (battle reward, trade, ...) by
#            doing inv[id] = inv.get(id, 0) + 1
#   gain_n   how many are granted each time (optional, defaults to 1)
ITEMS = {
    # The heal ladder. Only Small Heal is granted by time - the bigger ones need
    # a source (battle reward, trade) once one exists.
    "small": {
        "label": "Small Heal", "short": "S.Heal",
        "heal": 15.0, "cap": 30, "gain_ms": 1800_000, "gain_n": 2,
    },
    "heal": {
        "label": "Heal", "short": "Heal",
        "heal": 30.0, "cap": 30, "gain_ms": None,
    },
    "medium": {
        "label": "Medium Heal", "short": "M.Heal",
        "heal": 50.0, "cap": 20, "gain_ms": None,
    },
    "greater": {
        "label": "Greater Heal", "short": "G.Heal",
        "heal": 100.0, "cap": 5, "gain_ms": None,
    },
    # THE TRAINER'S FLASK. Never runs out - `infinite` means using it does not
    # decrement the count, so one is all anyone ever needs.
    #
    # It is a cheat and it is meant to be. Losing a ranked battle costs HP
    # (plan 14), and that cost is the only reason picking a fight means
    # anything; a free refill makes every loss free. So the flask is NOT
    # granted by time, by age, or by winning - `gain_ms: None` and nothing
    # grants it - and it can only arrive by being put there deliberately.
    # Carrying one says "this badge is a test rig", which is exactly the thing
    # a test rig should say out loud.
    "flask": {
        "label": "Trainer's Flask", "short": "Flask",
        "heal": 100.0, "cap": 1, "gain_ms": None, "infinite": True,
    },
}
# Hand every mon a Trainer's Flask (see ITEMS["flask"]). ONE switch, because
# there has to be exactly one thing to turn off before this is given to
# players - a cheat that takes two edits to remove is a cheat that ships.
#
# OFF as of 2026-08-01, for release. The switch now also STRIPS a flask from a
# save written while it was on (see _load_state), because "no new flasks" would
# have left every rig badge holding an infinite heal for good. And
# `blelab/badge/selftest.py` asserts it is off in the deployed source, the same
# way it does for _DEV_TIME_SCALE - a switch nobody checks is a switch that
# ships on.
FLASK_FOR_ALL = False

HEALTH_DROP = 10.0   # health lost each health tick when any need is below RED_AT
# Younger pets are more fragile: extra health damage that fades to nothing as the
# pet matures. At age 0 the drop is HEALTH_DROP * (1 + HEALTH_AGE_BONUS); by
# HEALTH_MATURE_AGE hours it settles to plain HEALTH_DROP (the "as is" baseline).
HEALTH_AGE_BONUS = 0.6      # up to +60% damage for a newborn
HEALTH_MATURE_AGE = 12.0    # hours of age at which damage settles to baseline
HEALTH_HEAL = 6.0    # health regained each health tick when well cared for
HEALTH_RISK = 20.0   # below this health, death is rolled (every DEATH_MS)
DEATH_CHANCE = 0.1   # 1-in-10 each death roll when at risk ("let's not be mean")

SHAPES = (
    "square",
    "triangle",
    "circle",
    "diamond",
    "pentagon",
    "hexagon",
    "octagon",
    "star",
)
NAME_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Personality traits: each pet is born with one, tweaking how fast some needs
# decay (a multiplier on that stat's decay). Adds flavour + replayability.
TRAITS = ("greedy", "playful", "messy", "tidy", "hardy")
TRAIT_DECAY = {
    "greedy": {"food": 1.6},           # always hungry
    "playful": {"fun": 1.6},           # bores easily
    "messy": {"clean": 1.6},           # gets grubby fast
    "tidy": {"clean": 0.5},            # stays clean
    "hardy": {"food": 0.7, "fun": 0.7, "clean": 0.7},  # low-maintenance
}
TRAIT_LABEL = {
    "greedy": "Greedy", "playful": "Playful", "messy": "Messy",
    "tidy": "Tidy", "hardy": "Hardy",
}

# Life stages by age (hours of on-time) - cosmetic: babies have no mouth and
# bigger eyes, children keep the bigger eyes, elders (24h+) earn a gold crown.
STAGE_CHILD_AGE = 2   # baby:  0-2 h
STAGE_ADULT_AGE = 6   # child: 2-6 h
STAGE_ELDER_AGE = 48  # adult: 6-48 h, elder: 48 h+
STAGE_LABEL = {"baby": "Baby", "child": "Child", "adult": "Adult", "elder": "Elder"}


def _life_stage(age):
    if age < STAGE_CHILD_AGE:
        return "baby"
    if age < STAGE_ADULT_AGE:
        return "child"
    if age < STAGE_ELDER_AGE:
        return "adult"
    return "elder"

# Pet size grows over real running time: a tiny dot at first, full size at GROW_MS.
PET_MIN_SIZE = 1.5
PET_MAX_SIZE = 16.0
GROW_MS = 43200_000 // _DEV_TIME_SCALE  # on-time to full size (~12 hours)

# Action feedback animation length (ms).
ANIM_MS = 800
# Play and Feed are a WALK, not a flash, and 800 ms is not enough to cross the
# screen and read as deliberate. They get their own bound rather than making
# every animation longer - the injection "+" is feedback and should stay brisk.
FETCH_MS = 2300
# Feed is the SAME throw and the same walk, and then it is over: the mon eats
# the thing. No carry, so it needs only the two legs' worth of time.
FEED_MS = 1250
# Clean washes the whole display and is deliberately the quickest of the three:
# a slow wash reads as a filter over the app rather than an event that happened
# to it (plan 21.3).
WASH_MS = 650

# Movement bounds - kept clear of the name/Food labels above and the bars below.
MOVE_CX, MOVE_CY, MOVE_R = 0, -10, 46
MOVE_SPEED = 0.014  # px per ms of wander speed (lower = slower, gentler)

# Cleanliness "poop" dots: one drops each time Clean falls another POOP_STEP;
# the Clean action wipes them all away.
POOP_STEP = 25.0
MAX_POOPS = 4

# Strength: a mostly-hidden stat used only by the optional battle addon
# (battle.py). Born middle-ish so there are no god-tier newborns, and it creeps
# up slowly while the pet is kept healthy (fitness). Battle influence is small
# and clamped, so it never makes a pet unbeatable.
STRENGTH_MIN = 1
STRENGTH_MAX = 10
STRENGTH_BIRTH = (4, 5, 5, 6, 6, 7)  # middle-biased birth roll
FIT_HEALTH_MIN = 90.0    # health must be at least this to build fitness
FIT_GAIN_MS = 7200_000   # +1 strength per this much on-time kept healthy (~2h)

try:
    _DIR = __file__.rsplit("/", 1)[0]
except NameError:
    _DIR = "/apps/emfmon"
STATE_PATH = _DIR + "/state.json"
HISTORY_PATH = _DIR + "/history.json"


def _random_name():
    return "".join(random.choice(NAME_LETTERS) for _ in range(4))


def _random_colour():
    # Bright, readable colours only (avoid near-black).
    return [round(0.4 + 0.6 * random.random(), 3) for _ in range(3)]


def _random_poop_pos():
    # a random spot in the pet's central area (clear of the bars/labels)
    a = random.random() * 2 * math.pi
    r = random.random() * 50
    return [round(MOVE_CX + math.cos(a) * r, 1), round(MOVE_CY + math.sin(a) * r, 1)]


def _fill_polygon(ctx, x, y, s, n, rot):
    # a regular n-sided polygon of radius s, first vertex at angle `rot`
    ctx.begin_path()
    step = 2 * math.pi / n
    for i in range(n):
        a = rot + i * step
        px, py = x + math.cos(a) * s, y + math.sin(a) * s
        if i == 0:
            ctx.move_to(px, py)
        else:
            ctx.line_to(px, py)
    ctx.close_path()
    ctx.fill()


def _fill_star(ctx, x, y, s):
    # a 5-point star (alternating outer/inner radius)
    ctx.begin_path()
    for i in range(10):
        a = -math.pi / 2 + i * (math.pi / 5)
        r = s if i % 2 == 0 else s * 0.42
        px, py = x + math.cos(a) * r, y + math.sin(a) * r
        if i == 0:
            ctx.move_to(px, py)
        else:
            ctx.line_to(px, py)
    ctx.close_path()
    ctx.fill()


def _new_pet():
    return {
        "name": _random_name(),
        "shape": random.choice(SHAPES),
        "trait": random.choice(TRAITS),  # personality, tweaks decay (TRAIT_DECAY)
        "colour": _random_colour(),
        "strength": random.choice(STRENGTH_BIRTH),  # battle stat (battle.py)
        "fit_acc": 0.0,    # on-time-kept-healthy accumulated toward +1 strength
        "age": 0,          # whole hours of on-time survived
        "grow_ms": 0.0,    # on-time accumulated toward full size (GROW_MS)
        # Tick accumulators are PERSISTED so age/health/death/heal count on-time
        # across restarts, the same way grow_ms and the needs already do.
        #
        # DELIBERATELY no wall-clock catch-up, and please don't add one. The pet
        # advances on ACCUMULATED ON-TIME only: switch the badge off and it is
        # paused, not ageing. Two reasons:
        #   1. There is no clock to do it with. The RTC is never NTP-synced
        #      outside an OTA check, so time.localtime() reads 2000-01-01 (this
        #      is also why battle records stamp the mon's age, not a date).
        #   2. It would be a mass extinction. Replaying elapsed time means
        #      replaying the decay: an adult is at death-risk after ~2.5h of
        #      neglect and likely dead by ~5h, so any overnight charge would
        #      wipe out every mon that isn't an elder.
        # Deltas come from ticks_diff(), which measures ELAPSED ms, and the
        # accumulators drain with `while`, so a starved loop catches up exactly
        # - the model loses no time while the badge is actually running.
        "hour_acc": 0.0,   # -> age tick
        "health_acc": 0.0,  # -> health tick
        "death_acc": 0.0,  # -> death roll
        # PAUSED (plan 23). Suspends every accumulator below - decay, health,
        # the death roll, age and growth - so the badge can be used as a badge
        # without the mon paying for it. Per-mon, so a successor starts unpaused
        # without _hatch_new having to say so.
        "paused": False,
        "inv": {},         # item id -> count carried (see ITEMS)
        "acc": {},         # item id -> on-time accumulated toward the next grant
        "health": 100.0,
        "food": 100.0,
        "fun": 100.0,
        "clean": 100.0,
        "poops": [],          # brown dots on screen; Clean wipes them away
        "clean_mark": 100.0,  # Clean level the poop count is measured down from
        "alive": True,
    }


class AlertIcon(app.App):
    """A tiny always-on-top overlay badging the home screen (and any other app)
    whenever the pet needs attention - like the battery icon.

    A round EMF/mon badge rather than a rectangle: it sits on a round screen,
    over other people's apps, so it should look deliberate rather than like a
    dialog someone forgot to close.
    """

    # Top-right, sized so the FAR CORNER stays inside the bezel - a rectangle
    # out here is limited by its corner, not its edge.
    _CX, _CY, _W, _H = 72, -46, 40, 32

    def __init__(self):
        super().__init__()
        self.show = False

    def draw(self, ctx):
        if not self.show:
            return
        # PERF: this runs on EVERY render frame of WHATEVER app is foregrounded,
        # so it is worth knowing what it costs. Measured on-badge:
        #   this badge ~3.3ms | a realistic app frame ~51ms | empty frame ~18ms
        # so it is about 7% of a frame - roughly 1.4fps, imperceptible. The
        # end_frame blit dominates everything; 60fps was never on the table.
        # Cheap/dear primitives: rect fill 41us, circle fill 609us, circle
        # STROKE 1453us - so prefer fills over strokes. Mixing font sizes costs
        # nothing measurable. Hidden - the common case - this returns in ~12us.
        ctx.save()
        cx, cy, w, h = self._CX, self._CY, self._W, self._H
        # dark border as a slightly larger rect underneath: this lands on top of
        # arbitrary apps and needs an edge that survives a light background
        ctx.rgb(0.25, 0.02, 0.02).rectangle(
            cx - w // 2, cy - h // 2, w, h).fill()
        ctx.rgb(0.9, 0.15, 0.15).rectangle(
            cx - w // 2 + 2, cy - h // 2 + 2, w - 4, h - 4).fill()
        ctx.rgb(1, 1, 1)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 8          # EMF rides smaller above the name
        ctx.move_to(cx, cy - 7).text("EMF")
        ctx.font_size = 10
        ctx.move_to(cx, cy + 6).text("mon")
        ctx.restore()


# One shared AlertIcon overlay for the whole session - starting a fresh one on
# each relaunch would accumulate orphaned overlays on the home screen.
_alert_icon = None


def _get_alert_icon():
    global _alert_icon
    if _alert_icon is None:
        _alert_icon = AlertIcon()
        scheduler.start_app(_alert_icon, always_on_top=True)
    return _alert_icon


# Only the most recently launched EMFMon simulates and saves. minimise() (the
# CANCEL exit) pops the foreground but does NOT stop the app's background task,
# and the launcher builds a fresh instance on every launch - so old instances
# keep running their background_update loop and would race on state.json,
# reverting your actions ~15s later. The newest instance claims _active_mon in
# __init__; stale instances no-op. (Same relaunch-accumulation issue that the
# AlertIcon singleton above already guards against.)
_active_mon = None


class EMFMon(app.App):
    def __init__(self):
        super().__init__()
        # Claim the active slot so any older instance left running in the
        # background (see _active_mon) stops simulating and saving.
        global _active_mon
        _active_mon = self
        self.pet = self._load_state() or _new_pet()
        self.history = self._load_history()
        self.view = "pet"          # "pet" | "battle" | "trainer"
        self.dialog = None
        self._dialog_apply = None  # what to do with the text, set per dialog
        self._quit_pending = False   # minimise() runs from update(), not input
        self.battle = None         # optional battle addon controller (battle.py)
        # NOT the trainer FILE - battle.py's _trainer() is that. This is the
        # read-only screen (plan 8.1.2), held like `battle` above.
        self.trainer_screen = None
        # Curved overlay menu (arcmenu.ArcMenu) or None. It draws ON the pet
        # view rather than replacing it, so the mon stays visible behind it.
        # ONE menu object for the app's lifetime, reconfigured per use via
        # set_items(). battle.py borrows this same instance, so every menu in
        # EMFMon shares one allocation and one set of styling.
        self.arcmenu = ArcMenu()
        self.arc = None            # -> self.arcmenu while an overlay is showing
        self._arc_select = None    # handlers for the open overlay (see _open_arc)
        self._arc_back = None
        # The press currently being dispatched from an overlay. Anything opened
        # from a menu handler that subscribes to the eventbus is handed this
        # same event afterwards and must ignore it (Battle._opened_by). Set here
        # so a future caller that opens Battle from OUTSIDE the menu path gets
        # None rather than an AttributeError.
        self._arc_event = None
        # Wall-clock lockout for the PET screen, armed when an overlay closes.
        # Without it a double-fired scroll (or a late-delivered press) lands on
        # the pet the instant the wheel shuts and fires an action: UP is Food,
        # RIGHT is Clean, so using an item appeared to play a random animation
        # as the pet action overwrote the injection one.
        self._lock_t0 = ticks_ms()
        self._lock_ms = 0
        self.inv_idx = 0           # remembered wheel position between opens
        # cached name string and curved trait/stage layouts (see _draw_bars)
        self._name_text = None
        self._nl_name = None
        self._nl_age = None
        self._trait_layout = None
        self._stage_layout = None
        self._nl_trait = None
        self._nl_stage = None
        self._anim_type = None     # current action animation type, or None
        # Where Play/Feed threw the thing, and where the mon set off from. Both
        # are fixed when the animation starts: deriving the walk from a target
        # that moves would have the mon chase a ball that keeps running away.
        self._fetch_x = 0.0
        self._fetch_y = 0.0
        self._fetch_from = (0.0, 0.0)
        self._drop_x = 0.0         # where the mon puts the ball down: the rim of
        self._drop_y = 0.0         # its circle, nearest the button it chose
        self._carry_btn = None     # chosen at the START, so the carry has a
        self._fetch_scored = False # destination; _ball_btn is set on arrival
        self._anim_was_carry = False
        self._fetch_side = 1       # alternates, with a wobble - see _start_fetch
        self._wash_grad = None      # None untried, True works, False fall back
        # FETCH, the Play mini-game. `_play_game` says one is running; `_ball_btn`
        # is the button currently holding the ball, or None while it is in the
        # air or being carried.
        self._play_game = False
        self._ball_btn = None
        self._play_title = None    # curved "Play!", measured once
        self._anim_t = 0.0         # ms elapsed in the current animation
        # (the age/health/heal/death accumulators now live in the pet dict so
        # they persist across restarts - see _new_pet)
        self._save_acc = 0.0       # ms accumulated toward the next autosave
        self._dirty = False        # a user action is waiting to be written
        # Shared always-on-top "mon!" indicator on the home screen (started once
        # for the whole session; see _get_alert_icon).
        self.icon = _get_alert_icon()
        self.icon.show = False
        # Pet position/velocity for the wandering animation.
        self._x, self._y = MOVE_CX, MOVE_CY
        ang = random.random() * 2 * math.pi
        self._vx = math.cos(ang) * MOVE_SPEED
        self._vy = math.sin(ang) * MOVE_SPEED
        eventbus.on(ButtonDownEvent, self._on_button, self)

    # --- persistence -------------------------------------------------------
    def _load_state(self):
        try:
            with open(STATE_PATH) as f:
                pet = json.loads(f.read())
            # tolerate older/partial files by filling defaults
            base = _new_pet()
            base.update(pet)
            # guard against a corrupt colour/shape that would crash draw()
            c = base.get("colour")
            if not (
                isinstance(c, list)
                and len(c) == 3
                and all(isinstance(v, (int, float)) for v in c)
            ):
                base["colour"] = _random_colour()
            if base.get("shape") not in SHAPES:
                base["shape"] = random.choice(SHAPES)
            if base.get("trait") not in TRAITS:
                base["trait"] = random.choice(TRAITS)
            # coerce numeric fields - a wrong-typed value from a corrupt or
            # hand-edited save would otherwise crash the background simulation
            d = _new_pet()  # pristine defaults (its numeric fields are fixed)
            for k in ("age", "strength"):
                try:
                    base[k] = max(0, int(base[k]))  # never negative
                except (TypeError, ValueError):
                    base[k] = d[k]
            # a negative age would make the health-tick interval <= 0 and hang
            # the badge in an infinite while-loop; max(0, ...) above prevents it.
            base["strength"] = min(STRENGTH_MAX, max(STRENGTH_MIN, base["strength"]))
            self._migrate_inventory(base, pet)
            for k in (
                "grow_ms", "hour_acc", "health_acc", "death_acc",
                "fit_acc", "health", "food", "fun", "clean", "clean_mark",
            ):
                try:
                    base[k] = float(base[k])
                except (TypeError, ValueError):
                    base[k] = d[k]
            # clamp to sane ranges so a corrupt/hand-edited save can't misbehave
            for k in ("health", "food", "fun", "clean", "clean_mark"):
                base[k] = min(100.0, max(0.0, base[k]))
            for k in ("grow_ms", "hour_acc", "health_acc",
                      "death_acc", "fit_acc"):
                base[k] = max(0.0, base[k])
            base["grow_ms"] = min(GROW_MS, base["grow_ms"])
            # A hand-edited save could put anything here, and `paused` gates the
            # whole simulation - a truthy string would freeze the mon forever
            # with no way to tell why.
            base["paused"] = bool(base.get("paused", False))
            # poops must be a list of [x, y] number pairs
            poops = base.get("poops")
            if isinstance(poops, list):
                base["poops"] = [
                    p
                    for p in poops
                    if isinstance(p, (list, tuple))
                    and len(p) == 2
                    and all(isinstance(v, (int, float)) for v in p)
                ]
            else:
                base["poops"] = []
            if not isinstance(base.get("alive"), bool):
                base["alive"] = True
            return base
        except Exception:
            return None

    def _migrate_inventory(self, base, raw):
        """Normalise inv/item/acc, folding in pre-inventory saves.

        Before the inventory a mon carried a bare `heals` int plus a `heal_acc`
        accumulator; those become inv["heal"] and acc["heal"]. Ids that aren't in
        ITEMS are dropped rather than carried forever, so retiring an item can't
        leave junk in every save - and counts are clamped to each item's cap the
        way `heals` used to be clamped to MAX_HEALS.
        """
        inv = base.get("inv")
        if not isinstance(inv, dict):
            inv = {}
        clean = {}
        for iid, spec in ITEMS.items():
            try:
                n = int(inv.get(iid, 0))
            except (TypeError, ValueError):
                n = 0
            clean[iid] = min(spec["cap"], max(0, n))
        if FLASK_FOR_ALL:
            clean["flask"] = 1
        else:
            # AND TAKE IT BACK. The loop above copies whatever the save holds,
            # so without this the switch would only mean "no NEW flasks" - a
            # badge that ever ran a rig build would keep an infinite heal
            # forever, and that is the same cheat shipping by a slower route.
            clean["flask"] = 0
        if "inv" not in raw and "heals" in raw:
            try:
                clean["heal"] = min(
                    ITEMS["heal"]["cap"], max(0, int(raw["heals"]))
                )
            except (TypeError, ValueError):
                pass
        base["inv"] = clean

        acc = base.get("acc")
        if not isinstance(acc, dict):
            acc = {}
        cacc = {}
        for iid in ITEMS:
            try:
                cacc[iid] = max(0.0, float(acc.get(iid, 0.0)))
            except (TypeError, ValueError):
                cacc[iid] = 0.0
        if "acc" not in raw and "heal_acc" in raw:
            try:
                cacc["heal"] = max(0.0, float(raw["heal_acc"]))
            except (TypeError, ValueError):
                pass
        base["acc"] = cacc

        # superseded fields - drop them so they stop being re-saved. "item" was
        # a short-lived "active item" the wheel assigned; C now opens the wheel
        # and you use what you select, so nothing is assigned any more.
        base.pop("heals", None)
        base.pop("heal_acc", None)
        base.pop("item", None)

    def _save_state(self):
        if self is not _active_mon:
            return  # never let a stale instance overwrite the live save
        try:
            with open(STATE_PATH, "w") as f:
                f.write(json.dumps(self.pet))
        except Exception as e:
            print("EMFMon: save failed:", e)

    def _load_history(self):
        try:
            with open(HISTORY_PATH) as f:
                return json.loads(f.read())
        except Exception:
            return []

    def _save_history(self):
        try:
            with open(HISTORY_PATH, "w") as f:
                f.write(json.dumps(self.history))
        except Exception as e:
            print("EMFMon: history save failed:", e)

    def _archive_battle_records(self):
        """Snapshot the outgoing mon's battle record for its history entry, clear
        the live one so the successor starts from 0W-0L, and take the mon's
        legacy action into the trainer pool (plan 14.3).

        Returns None if the battle addon isn't importable - the history entry is
        still written, it just has no record attached.

        THE MON-IS-LEAVING HOOK. Both departures already funnel through here -
        _die() and _hatch_new() - which is why the legacy grant lives here rather
        than being called twice. Plan 14.3 names both paths as hook points; this
        is the one place that is already both of them, so a third way for a mon
        to leave inherits the behaviour instead of forgetting it.
        """
        try:
            # records.py, NOT battle.py - importing the combat model to
            # archive a W/L record cost 1126 ms of blocking compile on this
            # path, which also writes history.json and state.json (plan 6.2.3).
            # collect_legacy is in the same module for exactly that reason: it
            # needs the trait->action table, and reading it from battle.py would
            # put the whole combat model back on this path.
            from .records import archive_records, blank_records, collect_legacy
        except Exception as e:
            print("EMFMon: archive records failed:", e)
            return None
        # Before the record is archived, because this reads the pet and the
        # archive does not touch it - but ordered explicitly rather than by
        # accident, since both write files on a path that already writes two.
        try:
            # The return value (the granted action id, or None) is DELIBERATELY
            # ignored, and the player is not told. Naming the action would need
            # ACTIONS out of battle.py, which is the import this whole path
            # exists to avoid - and an unnamed "you inherited an action" is worse
            # than silence beside a notification that a mon just died. The
            # Trainer screen's `actions n / 5` and the queue builder are where it
            # shows up. FLAGGED: if the owner wants it announced by name, the
            # cheap route is action LABELS beside TRAIT_ACTION in records.py, not
            # an import here.
            collect_legacy(self.pet)
        except Exception as e:
            # A failed grant must not cost the player their history entry or
            # their cleared record. Losing an inherited action is a
            # disappointment; losing the mon's record is data.
            print("EMFMon: legacy collection failed:", e)
        rec = archive_records()
        if self.battle is not None:
            # a battle view open right now holds the old record in memory and
            # would save it straight back over the file we just cleared
            try:
                self.battle.records = blank_records()
            except Exception:
                pass
        return rec

    # --- simulation (runs in background AND foreground) --------------------
    def background_update(self, delta):
        if self is not _active_mon:
            return  # a stale instance from an earlier launch - stay quiet
        # The framework does NOT catch exceptions raised here (background_task
        # has no try/except and the scheduler's background error monitor is
        # disabled), so an unhandled error would SILENTLY freeze the pet for the
        # whole session. Guard it: log and let the next tick continue.
        try:
            self._simulate(delta)
        except Exception as e:
            print("EMFMon bg error:", e)

    def _simulate(self, delta):
        # Runs every tick whether foreground or not (update() only does the
        # foreground visuals); all time-based simulation lives here.
        pet = self.pet
        if not pet["alive"]:
            return
        # PAUSED (plan 23): nothing happens. Guarded HERE, once, rather than at
        # each accumulator - six guards would be six chances to leave one
        # accruing, and "nothing happens" has to be true without exceptions for
        # the rule to be worth having.
        #
        # Note what this does NOT do: it does not clear anything. A mon paused at
        # 0 HP keeps its death risk and the next roll is still coming when it
        # resumes. Pause stops time; it never undoes something already in flight.
        if pet.get("paused"):
            return

        # Needs decay on a real-time schedule (MINUTES_TO_EMPTY), independent of
        # the HOUR_MS tick, so food empties in ~10 real minutes at any speed.
        # Older pets decay more slowly (see DECAY_AGE_REDUCTION). Health is not
        # touched here - it changes on the health tick below.
        decay_mult = max(DECAY_MIN_MULT, 1.0 - DECAY_AGE_REDUCTION * pet["age"])
        trait_decay = TRAIT_DECAY.get(pet.get("trait"), {})
        for stat, mins in MINUTES_TO_EMPTY.items():
            m = decay_mult * trait_decay.get(stat, 1.0)  # personality tweak
            pet[stat] = max(
                0.0, pet[stat] - delta * 100.0 / (mins * 60_000.0) * m
            )

        # grow from a tiny dot to full size over GROW_MS of running time
        pet["grow_ms"] = min(GROW_MS, pet.get("grow_ms", 0.0) + delta)

        # grant items on their own schedules (ITEMS[id]["gain_ms"]). Items with
        # gain_ms None are never granted here - they come from elsewhere.
        inv = pet.setdefault("inv", {})
        acc = pet.setdefault("acc", {})
        for iid, spec in ITEMS.items():
            every = spec["gain_ms"]
            if not every:
                continue
            a = acc.get(iid, 0.0) + delta
            cap = spec["cap"]
            n = spec.get("gain_n", 1)
            while a >= every:
                a -= every
                # at cap the timer still cycles, so a full pouch doesn't bank
                # an instant refill the moment one is spent
                cur = inv.get(iid, 0)
                if cur < cap:
                    inv[iid] = min(cap, cur + n)
            acc[iid] = a

        # fitness: strength creeps up slowly while the pet is kept healthy
        # (only counts on-time spent at high health; never decreases)
        if pet.get("health", 0.0) >= FIT_HEALTH_MIN and pet.get("strength", 0) < STRENGTH_MAX:
            pet["fit_acc"] = pet.get("fit_acc", 0.0) + delta
            while pet["fit_acc"] >= FIT_GAIN_MS and pet["strength"] < STRENGTH_MAX:
                pet["fit_acc"] -= FIT_GAIN_MS
                pet["strength"] += 1

        # drop a poop dot each time Clean has fallen another POOP_STEP
        poops = pet["poops"]
        target = int((pet.get("clean_mark", 100.0) - pet["clean"]) / POOP_STEP)
        target = max(0, min(MAX_POOPS, target))
        while len(poops) < target:
            poops.append(_random_poop_pos())

        self._update_notifications()

        pet["hour_acc"] = pet.get("hour_acc", 0.0) + delta
        while pet["hour_acc"] >= HOUR_MS:
            pet["hour_acc"] -= HOUR_MS
            self._hourly_tick()

        # Health tick: young pets tick faster (down to HEALTH_TICK_YOUNG_MS),
        # easing to HEALTH_TICK_MS (30 min) by HEALTH_MATURE_AGE hours.
        maturity = min(1.0, max(0.0, pet["age"] / HEALTH_MATURE_AGE))
        tick_ms = HEALTH_TICK_YOUNG_MS + (HEALTH_TICK_MS - HEALTH_TICK_YOUNG_MS) * maturity
        pet["health_acc"] = pet.get("health_acc", 0.0) + delta
        while pet["health_acc"] >= tick_ms:
            pet["health_acc"] -= tick_ms
            self._health_tick()

        # Death roll on its own faster cadence (every DEATH_MS = 20 min).
        pet["death_acc"] = pet.get("death_acc", 0.0) + delta
        while pet["death_acc"] >= DEATH_MS:
            pet["death_acc"] -= DEATH_MS
            if pet["health"] < HEALTH_RISK and random.random() < DEATH_CHANCE:
                self._die()
                break

        # Persist on a timer rather than on every action. Each save is a
        # json.dumps plus a littlefs write of ~400 bytes; doing that per button
        # press meant several flash writes a second while a user mashed
        # Food/Play, for at most 15s of extra crash protection. A pending user
        # action shortens the interval so it isn't left hanging for long.
        self._save_acc += delta
        if self._save_acc >= (3_000 if self._dirty else 15_000):
            self._save_acc = 0.0
            self._dirty = False
            self._save_state()

    def _hourly_tick(self):
        self.pet["age"] += 1

    def _health_tick(self):
        # Every HEALTH_TICK_MS (30 min): lose HEALTH_DROP PER NEED in the red
        # (below RED_AT), otherwise slowly recover when well cared for.
        #
        # The drop used to be flat `any(...)`, so one forgotten need cost exactly
        # as much health as total abandonment - the sim could not tell a busy
        # owner from a badge left in a pocket all day. Scaling by the count keeps
        # mild neglect exactly as forgiving as it was (1 red = the old 10/tick ->
        # 4.5h to reach death risk at maturity) while making real neglect bite
        # (2 red = 2.5h, 3 red = 1.5h). Nothing here is wall-clock: this only
        # runs on accumulated ON-TIME, so a badge that is off or asleep still
        # cannot kill a pet.
        pet = self.pet
        reds = sum(1 for s in ("food", "fun", "clean") if pet[s] < RED_AT)
        if reds:
            youth = max(0.0, HEALTH_MATURE_AGE - pet["age"]) / HEALTH_MATURE_AGE
            drop = HEALTH_DROP * reds * (1.0 + HEALTH_AGE_BONUS * youth)
            pet["health"] = max(0.0, pet["health"] - drop)
        elif pet["food"] >= 50 and pet["fun"] >= 50 and pet["clean"] >= 50:
            pet["health"] = min(100.0, pet["health"] + HEALTH_HEAL)

    def _die(self):
        pet = self.pet
        pet["alive"] = False
        self.icon.show = False  # clear the home-screen alert; the pet is gone
        self.history.insert(
            0,
            {
                "name": pet["name"],
                "age": pet["age"],
                "shape": pet["shape"],
                "rec": self._archive_battle_records(),
            },
        )
        self.history = self.history[:20]  # keep the 20 most recent
        self._save_history()
        self._save_state()
        eventbus.emit(
            ShowNotificationEvent(f"{pet['name']} has died at {pet['age']}h :(")
        )

    def _update_notifications(self):
        pet = self.pet
        # Persistent home-screen "!" icon while any need is below NOTIFY_AT.
        # A PAUSED mon never shows it: its bars cannot move, so an alert would
        # point at needs that will never change and read as a badge nagging
        # about something the player has already dealt with (plan 23.4).
        self.icon.show = (pet["alive"] and not pet.get("paused") and any(
            pet[s] < NOTIFY_AT for s in ("food", "fun", "clean")
        ))

    def _hatch_new(self):
        # if we're replacing a still-living pet (Menu -> New pet), log it so it
        # isn't lost from the history
        if self.pet.get("alive"):
            self.history.insert(
                0,
                {
                    "name": self.pet["name"],
                    "age": self.pet["age"],
                    "shape": self.pet["shape"],
                    "rec": self._archive_battle_records(),
                },
            )
            self.history = self.history[:20]
            self._save_history()
        # a pet that already died was archived (and its record cleared) by
        # _die(), so there is nothing left to take here.
        self.pet = _new_pet()
        self._anim_type = None
        self.icon.show = False
        self._save_state()

    # --- input -------------------------------------------------------------
    def _on_button(self, event: ButtonDownEvent):
        # MUST NOT raise: the eventbus stops the owning app if a handler throws,
        # which presents as the whole app freezing - buttons dead, pet stopped -
        # with nothing on screen to say why. battle.py already wraps its handler
        # for this reason; this one was unprotected, so ANY error on ANY button
        # path (a bad save, a draw-state slip, an addon import) killed EMFMon.
        try:
            self._on_button_inner(event)
        except Exception as e:
            print("EMFMon: button error:", e)

    def _on_button_inner(self, event):
        if self.dialog is not None:
            # THE KEYBOARD OWNS EVERY BUTTON. We take none of them.
            #
            # F used to close the dialog here. That was wrong, and the comment
            # justifying it was wrong in a checkable way: it claimed "TextDialog
            # ignores CANCEL entirely... there is no backspace or navigation
            # being stolen here". The keyboard's own layout is SIX GROUPS ON SIX
            # BUTTONS - `A-F | G-L | DONE | M-R | S-Z | More`, read straight off
            # the badge's screen reader - so F is one of its keys and we were
            # stealing it. Owner hit the clash immediately (rev 72).
            #
            # THE COST, so it is not rediscovered as a bug: there is no longer a
            # dedicated "abandon" button. The way out is to clear the text and
            # press DONE - `update()` applies nothing for an empty result (an
            # empty name is a finish that applies nothing), so that discards
            # exactly as a cancel would.
            return  # the text dialog owns every button while it is open
        if self._addon() is not None:
            return  # the addon registers its own ButtonDownEvent handler
                    # (battle.py's Battle, trainer_ui.py's TrainerScreen)
        if self.arc is not None:
            self._arc_button(event)   # the overlay owns every button while open,
            return                    # including CANCEL (closes it, not the app)
        if self._input_locked():
            return  # an overlay just closed - don't let its bounce hit the pet
        # Ignore the joystick centre press on the pet screen - it's flaky (opens
        # the menu then instantly selects Rename). It IS accepted inside the item
        # wheel above, where there's nothing destructive for a stray press to hit.
        if JOYSTICK_BUTTON_TYPES["SELECT"] in event.button:
            return
        if self._play_game:
            # A GAME IS RUNNING, and it owns the screen. Back stops it; the
            # button holding the ball throws it again; everything else waits.
            # Feeding mid-fetch would have the mon abandon the ball it is
            # walking to, which reads as the game breaking rather than as the
            # player multitasking.
            if BUTTON_TYPES["CANCEL"] in event.button:
                self._end_play()
            elif self._ball_btn is not None and \
                    BUTTON_TYPES[self._ball_btn] in event.button:
                self._throw_again()
            return
        if BUTTON_TYPES["CANCEL"] in event.button:
            self._confirm_quit()
            return
        if not self.pet["alive"]:
            if BUTTON_TYPES["CONFIRM"] in event.button:
                self._hatch_new()
            return
        if BUTTON_TYPES["UP"] in event.button:
            self._do_action("food")
        elif BUTTON_TYPES["DOWN"] in event.button:
            self._start_play()      # Play - moved to D to clear the OS back button
        elif BUTTON_TYPES["RIGHT"] in event.button:
            self._do_action("clean")
        elif BUTTON_TYPES["CONFIRM"] in event.button:
            self._open_inventory()   # C opens the pouch; using is done in there
        elif BUTTON_TYPES["LEFT"] in event.button:
            self._open_menu()

    def _arm_input_lock(self, ms=250):
        """Ignore pet-screen buttons for `ms` of REAL time. Wall clock, never
        accumulated delta - a single slow frame would spend a delta budget whole
        (measured on-badge), which is what defeated the battle lockout."""
        self._lock_t0 = ticks_ms()
        self._lock_ms = ms

    def _input_locked(self):
        return ticks_diff(ticks_ms(), self._lock_t0) < self._lock_ms

    def _arc_button(self, event):
        """Route a press to the open ArcMenu and act on what it reports."""
        act = self.arc.button(event)
        if act is None:
            return                            # just scrolled
        self._arm_input_lock()                # the overlay is about to close
        idx = self.arc.idx
        # Remember the press we're dispatching: anything opened from here that
        # subscribes to the eventbus (Battle) is handed this same event
        # afterwards and must ignore it - see Battle._opened_by.
        self._arc_event = event
        on_select, on_back = self._arc_select, self._arc_back
        # tear down BEFORE dispatching, so a handler can open another menu
        self.arc = None
        self._arc_select = self._arc_back = None
        self.view = "pet"
        if act == "select":
            if on_select is not None:
                on_select(idx)
        elif on_back is not None:
            on_back(idx)

    def _has_items(self):
        """Is the mon carrying anything at all? Allocation-free - safe to call
        from draw(), unlike _carried_items()."""
        inv = self.pet.get("inv", {})
        for i in ITEMS:
            if inv.get(i, 0) > 0:
                return True
        return False

    def _carried_items(self):
        """Item ids the mon actually has, in ITEMS order. An item at zero isn't
        shown - an empty wheel is noise, and the HUD already says you have none."""
        inv = self.pet.get("inv", {})
        return [i for i in ITEMS if inv.get(i, 0) > 0]

    def _use_item(self, iid):
        """Spend one of `iid`. Returns True if it was actually consumed."""
        pet = self.pet
        if pet.get("paused"):
            # Plan 23.3: pause means NOTHING happens, with no exception list.
            # Allowing heals would have made pause a repair bay, and "nothing
            # happens except these things" is a rule nobody holds in their head.
            eventbus.emit(ShowNotificationEvent(
                "%s is paused" % pet.get("name", "Your mon")))
            return False
        spec = ITEMS.get(iid)
        if spec is None or pet.get("inv", {}).get(iid, 0) <= 0:
            return False
        heal = spec.get("heal")
        if heal is not None:
            if pet["health"] >= 100:
                return False      # never burn a restorative at full HP
            if not spec.get("infinite"):
                pet["inv"][iid] -= 1
            pet["health"] = min(100.0, pet["health"] + heal)
            self._anim_type = "injection"   # the green "+" feedback
            self._anim_t = 0.0
            self._dirty = True   # written by the next autosave (<=3s)
            return True
        # non-restorative items (buffs) get their branch here when they land
        return False

    def _do_action(self, action):
        pet = self.pet
        if action == "injection":
            pet["health"] = min(100.0, pet["health"] + ACTION_GAIN["injection"])
        else:
            pet[action] = min(100.0, pet[action] + ACTION_GAIN[action])
        if action == "clean":
            pet["poops"] = []                 # washing wipes the mess away
            pet["clean_mark"] = pet["clean"]  # re-measure poops from here
        if action in ("fun", "food"):
            self._start_fetch()
        self._anim_type = action  # kick off the feedback animation
        self._anim_t = 0.0
        self._dirty = True        # written by the next autosave (<=3s)

    # Order matters: the destructive one is last, and the two most-used are
    # first, so a mis-scroll lands on something harmless.
    # Six entries. Plan 8.1 asks for `Trainer` here and warns to check the arc
    # still reads cleanly at six before committing to the order - so LOOK AT IT
    # ON THE BADGE, and if it is crowded, that is an argument about this tuple
    # rather than about the screen it opens.
    #
    # Trainer sits next to Battle because that is where its numbers come from,
    # and before History because History is about mons you no longer have.
    # `Battle`, NOT `evo_CONNECT`. Plan 8.1 specifies the rename and it was
    # built at rev 71 - the owner looked at it on the badge and reverted it at
    # rev 72: `Battle` reads better. That is the call 8.1 itself asks for
    # ("LOOK AT IT ON THE BADGE"), so this is the section being followed, not
    # ignored.
    #
    # What 8.1 actually wanted still holds: ONE radio entry, with room for
    # MonStation (17.3) to arrive as a ROW in battle.py's `menu_items` rather
    # than as a second top-level entry. The name was never what delivered that.
    _MAIN_MENU = ("Items", "Battle", "Trainer", "History", "Rename", "New pet")

    def _open_arc(self, items, on_select, on_back=None, idx=0,
                  hint_c="C pick", hint_f="F back", side="right"):
        """Open a curved overlay menu. on_select(idx)/on_back(idx) are called
        with the row that was current when it closed; the menu is torn down
        first, so a handler is free to open another one. `side` should match the
        button that opened it (LEFT button -> "left")."""
        m = self.arcmenu               # reconfigured, never reallocated
        # Stated, not inherited: battle.py's selection screen sets its own sizes
        # on this same object, and whatever ran last must not decide how the pet
        # menu looks. `configure` also fixes the ORDER - fonts before set_items,
        # which this call site had drifted away from while its own comment still
        # claimed otherwise (review plan 1.1).
        m.configure(items, idx=idx, side=side, hint_c=hint_c, hint_f=hint_f)
        self.arc = m
        self._arc_select = on_select
        self._arc_back = on_back
        self.view = "pet"          # overlays draw ON the pet, never instead

    def _open_menu(self):
        def on_select(idx):
            value = self._MAIN_MENU[idx]
            if value == "Rename":
                self._rename()
            elif value == "History":
                self._show_history_menu()
            elif value == "Items":
                self._open_inventory()
            elif value == "Battle":
                self._open_battle()
            elif value == "Trainer":
                self._open_trainer()
            elif value == "New pet":
                self._confirm_new_pet()

        # opened by the LEFT button, so it flies out from the left edge
        self._open_arc(list(self._MAIN_MENU), on_select, side="left")

    def _confirm(self, action, on_yes, on_cancel=None, also=None):
        """THE CONFIRM WINDOW. An "are you sure", named for what it is rather
        than for whichever screen happened to need it first.

        `action` is the affirmative row's label and should say what will happen
        ("Quit to menu", "Replace BLEE"), not "Yes" - a player reading one row
        out of context must still know what they are agreeing to.

        `also` is an OPTIONAL third row, as `(label, callback)`. It exists for
        the exit menu, which offers pausing beside quitting (plan 23), and it is
        a parameter here rather than a second menu hand-rolled beside this one
        precisely because that copy is what `code_review_plan.md` 1.1 is about -
        two call sites passing the same non-obvious constants to _open_arc IS the
        un-named primitive. Callers that pass nothing are untouched.

        The conventions live HERE so they cannot drift apart between callers:
        Cancel is always row 0 and always the landing row, so a stray press
        lands on the harmless option; the window opens from the left, because
        "C confirm" overruns the bezel on the right. The extra row sits BETWEEN
        Cancel and the affirmative one, so the most destructive option stays
        furthest from the landing row.
        """
        rows = ["Cancel"]
        if also is not None:
            rows.append(also[0])
        rows.append(action)

        def on_select(idx):
            if idx == len(rows) - 1:
                on_yes()
            elif also is not None and idx == 1:
                also[1]()
            elif on_cancel is not None:
                on_cancel()

        self._open_arc(
            rows,
            on_select,
            on_back=None if on_cancel is None else (lambda idx: on_cancel()),
            idx=0,
            side="left",
        )

    def _confirm_quit(self):
        """F on the pet screen is the OS back button, and it sits right next to
        everything else - so it gets the same second press New pet gets.

        Worth saying what quitting does and does not do, because the answer is
        reassuring and players assume the worse one: EMFMon keeps simulating in
        the background, so the mon is not paused, abandoned or lost. Hence
        "Quit to menu" - the badge's menu, not the end of anything.
        """
        # DEFERRED, not called here. minimise() pops the foreground app, and
        # doing that from inside a button dispatch tears down the thing
        # currently walking its own handler list - which froze the badge hard
        # enough to need a power cycle. The old F-quits-immediately path got
        # away with it by luck of being one frame shallower; running it from
        # the confirm overlay's select callback is one level deeper, inside
        # _arc_button, and it does not.
        #
        # So: raise a flag and leave. update() runs it on the next frame, with
        # the dispatch finished and nothing left on the stack.
        # Pause sits in THIS menu because this is where a player goes when they
        # are done with the mon for now, which is exactly when they want it
        # (plan 23). The label says which way it will go, so the row is never a
        # question about what state you are currently in.
        pet = self.pet
        name = pet.get("name", "your mon")
        if pet.get("alive", True):
            also = (("Resume " + name) if pet.get("paused")
                    else ("Pause " + name), self._toggle_pause)
        else:
            also = None          # nothing to pause: it is already over
        self._confirm("Quit to menu", self._request_quit, also=also)

    def _toggle_pause(self):
        """Stop or restart the mon's clock (plan 23).

        Saves immediately rather than waiting for the periodic write: pausing is
        a deliberate act taken right before the player puts the badge down or
        walks off with it, so it is the single worst thing to lose to a battery
        going flat fifteen seconds later.
        """
        pet = self.pet
        pet["paused"] = not pet.get("paused", False)
        self._update_notifications()   # a paused mon drops its alert at once
        self._save_state()
        eventbus.emit(ShowNotificationEvent(
            "%s %s" % (pet.get("name", "Your mon"),
                       "paused" if pet["paused"] else "resumed")))

    def _request_quit(self):
        self._quit_pending = True

    def _confirm_new_pet(self):
        """New pet REPLACES a living mon - it is archived to history and a
        stranger hatches in its place. That is the only irreversible thing in
        the app, so it does not happen on a single press."""
        if not self.pet.get("alive", True):
            self._hatch_new()      # nothing to lose - the mon is already gone
            return
        name = str(self.pet.get("name", "your mon"))
        # Cancel returns to the menu this was reached from, rather than to the
        # pet screen - backing out of a choice should land where the choice was
        # offered.
        self._confirm("Replace " + name, self._hatch_new, self._open_menu)

    def _open_battle(self):
        # A PAUSED mon does not fight, and this REFUSES rather than resuming for
        # them (plan 23.3). Auto-resume was the friendlier option and was
        # rejected: pause is a deliberate declaration, and silently undoing it
        # hands the player back a decaying mon they believe is safe. A mon that
        # fought while paused would also take a battle's HP cost without any of
        # the time that cost is measured against.
        #
        # Refused HERE, at the one door into every battle screen, rather than in
        # battle.py - which would mean the combat model importing on a path that
        # exists to say no (plan 6.2.2).
        if self.pet.get("paused"):
            eventbus.emit(ShowNotificationEvent(
                "%s is paused" % self.pet.get("name", "Your mon")))
            return
        # Battle is an OPTIONAL addon - if it fails to import or construct, the
        # pet must carry on unaffected, so swallow everything and stay on the pet.
        try:
            from .battle import Battle
            self.battle = Battle(self, opened_by=self._arc_event)
            self._addon_draw_errs = 0
            self.view = "battle"
        except Exception as e:
            print("EMFMon: battle unavailable:", e)
            self.battle = None
            self.view = "pet"

    def _open_trainer(self):
        """The Trainer screen (plan 8.1.2). Same optional-addon rule as
        _open_battle: if it fails, the pet carries on and we stay on the pet.

        `trainer_ui` imports records.py and nothing from battle.py, which is what
        keeps battle.py's ~1.1 s compile off the MAIN menu (plan 6.2.2). Imported
        here rather than at module scope for the same reason - a screen nobody
        opens should cost nothing.
        """
        try:
            from .trainer_ui import TrainerScreen
            self.trainer_screen = TrainerScreen(self, opened_by=self._arc_event)
            self._addon_draw_errs = 0
            self.view = "trainer"
        except Exception as e:
            print("EMFMon: trainer unavailable:", e)
            self.trainer_screen = None
            self.view = "pet"

    def _addon(self):
        """The full-screen addon that owns the display, or None.

        Battle and the Trainer screen are the same KIND of thing - they take the
        whole screen, run their own button handler, and are polled for `done` -
        so update(), draw() and the button guard ask this rather than each
        naming the two views. A third such screen adds a line here and inherits
        the error handling instead of copying it.
        """
        if self.view == "battle":
            return self.battle
        if self.view == "trainer":
            return self.trainer_screen
        return None

    def _close_addon(self):
        """Tear down whichever addon is open and return to the pet."""
        addon = self._addon()
        if addon is not None:
            try:
                addon.close()
            except Exception as e:
                print("EMFMon: addon close error:", e)
        self.battle = None
        self.trainer_screen = None
        self.view = "pet"

    def _open_inventory(self):
        carried = self._carried_items()
        if not carried:
            eventbus.emit(ShowNotificationEvent("No items"))
            return
        inv = self.pet.get("inv", {})

        def use(idx):
            self.inv_idx = idx
            if idx < len(carried):
                self._use_item(carried[idx])

        self._open_arc(
            # An infinite item shows no count. "x1" would be a lie by omission:
            # it reads as the last one, which is the opposite of what it is.
            ["%s %s" % (ITEMS[i]["label"],
                        "*" if ITEMS[i].get("infinite")
                        else "x%d" % inv.get(i, 0))
             for i in carried],
            use,
            # F out of the wheel goes back to the main menu it was opened from,
            # unless C opened it straight from the pet screen
            on_back=self._remember_inv_idx,
            # reopen where they left off, but an item may have been spent since
            idx=min(self.inv_idx, len(carried) - 1),
            hint_c="C use",
        )

    def _remember_inv_idx(self, idx):
        self.inv_idx = idx

    def _history_label(self, h):
        """`NAME - 32h`, plus `3W/5L` when that mon has an archived record."""
        base = "%s - %sh" % (h.get("name", "?"), h.get("age", 0))
        rec = h.get("rec")
        if isinstance(rec, dict) and (rec.get("w") or rec.get("l")):
            base += "  %dW/%dL" % (rec.get("w", 0), rec.get("l", 0))
        return base

    def _open_battle_records(self, h):
        # Read-only view of a past mon's record, reusing the battle addon's
        # ranked-records screen. Same optional-addon rule as _open_battle.
        try:
            from .battle import Battle
            self.battle = Battle(
                self,
                records=h["rec"],
                state="rec_ranked",
                title=str(h.get("name", "?")),
                opened_by=self._arc_event,   # same guard as _open_battle
            )
            self._addon_draw_errs = 0
            self.view = "battle"
        except Exception as e:
            print("EMFMon: records view unavailable:", e)
            self.battle = None
            self.view = "pet"

    def _show_history_menu(self):
        if self.history:
            items = [self._history_label(h) for h in self.history]
        else:
            items = ["No deaths yet"]

        def on_select(idx):
            # picking a past mon opens its archived battle record; the
            # placeholder row (empty history) does nothing
            h = self.history[idx] if idx < len(self.history) else None
            if h is not None and isinstance(h.get("rec"), dict):
                self._open_battle_records(h)

        # F here steps back to the main menu rather than all the way out, and it
        # stays on the same side as the menu it came from
        self._open_arc(items, on_select, on_back=lambda idx: self._open_menu(),
                       side="left")

    def _open_text_dialog(self, prompt, apply_fn):
        """THE text-entry window, named for what it is rather than for the pet
        rename that happened to need it first (review plan 1.1).

        `apply_fn(text)` is called with the raw entered text when the player
        CONFIRMS, and not at all when they cancel - so every caller owns its own
        cleaning rules. The pet forces upper case and eight characters; the
        trainer name keeps its case. Neither of those belongs here.
        """
        self.dialog = TextDialog(prompt, self)
        self._dialog_apply = apply_fn
        self.overlays = [self.dialog]

    def _close_dialog(self):
        """Take the dialog down. ONE place, so a caller cannot clear `dialog`
        and forget `overlays` and leave a ghost drawn over everything.

        `_cleanup()` is what unregisters the dialog's own eventbus handler. On
        the completion path it has already run and calling it again is harmless
        (verified); on the cancel path it is the ONLY thing that runs it, and
        without it every cancelled rename would leak a live button handler onto
        the bus for the rest of the session (plan 6.3).
        """
        if self.dialog is None:
            return
        # ARM THE LOCK, exactly as _arc_button does when an ArcMenu overlay
        # closes. These are the same event - an overlay coming down - and only
        # one of them used to guard the press that bleeds through to the screen
        # underneath (review plan 1.1: two callers, one shared consequence, one
        # of them remembering).
        #
        # It matters most on the pet screen, where F is _confirm_quit(). Cancel
        # a rename with F, then tap F again because you are not sure it took,
        # and the second press asks whether you want to QUIT. The completion
        # path has it too: "Done" also comes through here, so the first press
        # after finishing a rename used to land on the pet.
        self._arm_input_lock()
        try:
            self.dialog._cleanup()
        except Exception as e:
            print("EMFMon: dialog cleanup failed:", e)
        self.overlays = []
        self.dialog = None
        self._dialog_apply = None

    def _rename(self):
        def apply(text):
            name = text.strip().upper()[:8]
            if name:
                self.pet["name"] = name
                self._save_state()

        self._open_text_dialog("Name your pet:", apply)

    # --- foreground update (movement + overlays) ---------------------------
    def update(self, delta):
        # First thing, before anything can open a screen this would strand.
        if self._quit_pending:
            self._quit_pending = False
            self.minimise()
            return True
        # A text dialog is MODAL over everything, including a full-screen addon,
        # so it resolves BEFORE the addon dispatch. It used to sit after, which
        # was harmless only because nothing but the pet screen opened one: a
        # dialog raised from an addon would never have been applied and could
        # never have been dismissed, leaving the badge stuck on it.
        if self.dialog is not None:
            # `_result` IS THE TEXT, not a flag - None while the dialog is open,
            # and the entered string once it completes. Verified on hardware:
            # confirming with nothing typed sets it to "", which is why the two
            # conditions below are not one. `is not None` means "they finished";
            # truthiness means "they finished with something", and an empty name
            # is a finish that applies nothing.
            #
            # This used to read the text back out of `dialog.text` instead. Same
            # value, but it made the code look like `_result` was a boolean and
            # left the empty case working by accident rather than by intent.
            result = self.dialog._result
            if result is not None:
                if result and self._dialog_apply is not None:
                    try:
                        self._dialog_apply(result)
                    except Exception as e:
                        # A bad applier must not strand the player in the dialog.
                        print("EMFMon: dialog apply failed:", e)
                self._close_dialog()
            return True
        addon = self._addon()
        if addon is not None:
            try:
                addon.update(delta)
            except Exception as e:
                print("EMFMon: addon update error:", e)
                self._close_addon()
                return True
            if addon.done:
                self._close_addon()
            return True
        # ArcMenu is pure overlay - it has no update of its own, and the pet
        # deliberately keeps simulating behind it.
        #
        # PAUSED stops the wander and the action animation too (plan 23). These
        # are the only two things on `delta` that live OUTSIDE _simulate, so
        # they are the only two that its guard cannot reach - and a mon still
        # ambling about is the loudest possible contradiction of a screen that
        # says PAUSED. An animation in flight is left frozen mid-way rather than
        # cleared: resuming continues it, which is what every other paused
        # quantity does.
        if self.pet["alive"] and not self.pet.get("paused"):
            if self._anim_type in self._FETCH:
                # The WALK is the animation (plan 21.1). Drive the existing
                # position rather than adding a second one to keep in sync with
                # it - _move picks up from wherever this leaves the mon.
                self._walk_to_fetch()
            else:
                self._move(delta)
            if self._anim_type is not None:
                self._anim_t += delta
                if self._anim_t >= self._anim_ms():
                    was = self._anim_type
                    self._anim_was_carry = self._carries()
                    self._anim_type = None
                    if was in self._FETCH:
                        self._fetch_landed()
        return True

    # Play and Feed are THE SAME ANIMATION with a different colour and payload.
    # Not two implementations that happen to look alike - if they ever diverge,
    # this revamp has become code_review_plan.md 1.1 (plan 21.2).
    # rgb, radius, and whether the mon CARRIES it afterwards. Play fetches - it
    # brings the ball back to you. Feed does not: the mon eats the thing where it
    # lands, which is the whole difference between the two and the only thing
    # that differs in the code (plan 21.2 - same throw, same approach).
    _FETCH = {
        "fun":  ((1.00, 0.85, 0.20), 6.0, True),    # a yellow ball, fetched
        "food": ((1.00, 0.45, 0.65), 5.0, False),   # a pink morsel, eaten
    }
    # Three phases, as fractions of FETCH_MS: the throw, the walk out to the
    # ball, and the CARRY back to a button. The ball does not teleport - the mon
    # picks it up and takes it, which is the whole point of it being fetch.
    _FETCH_THROW = 0.24
    _FETCH_CARRY = 0.58        # walk ends / carry begins

    # Clean washes the WHOLE screen, right to left, because cleaning is the one
    # care action about the environment rather than the mon (plan 21.3).
    _WASH_W = 150.0            # how wide the wet band is
    _WASH_RGB = [0.45, 0.80, 1.0]

    def _carries(self):
        """Does the current fetch end with the ball being brought back?"""
        spec = self._FETCH.get(self._anim_type)
        return bool(spec and spec[2])

    def _anim_ms(self):
        """How long the CURRENT animation runs. Each kind gets its own bound
        rather than one constant stretched to fit the longest of them."""
        if self._anim_type in self._FETCH:
            return FETCH_MS if self._carries() else FEED_MS
        if self._anim_type == "clean":
            return WASH_MS
        return ANIM_MS

    def _start_play(self):
        """Play: start a game of fetch, or throw again if one is running.

        The press no longer grants fun by itself. Happiness comes from the mon
        actually GETTING the ball (see _fetch_landed), so playing is something
        you do with it rather than something you do to it.
        """
        self._play_game = True
        self._throw_again()

    def _throw_again(self):
        """Throw the ball out. The mon fetches it and takes it somewhere."""
        self._ball_btn = None
        self._start_fetch()
        self._anim_type = "fun"
        self._anim_t = 0.0
        self._dirty = True

    def _score_fetch(self):
        """The mon has the ball. That is what raises happiness."""
        pet = self.pet
        pet["fun"] = min(100.0, pet["fun"] + ACTION_GAIN["fun"])
        self._update_notifications()
        self._dirty = True

    def _fetch_landed(self):
        """The animation finished.

        For a fetch, the carry is over and the button now holds the ball. For a
        Feed there is nothing to hand over - the morsel was eaten on arrival.
        """
        if self._play_game and self._anim_was_carry:
            self._ball_btn = self._carry_btn

    def _end_play(self):
        """Back: the game is over. The ball goes with it - a ball left on a
        button after the game ended would sit there labelling a control it no
        longer owns."""
        self._play_game = False
        self._ball_btn = None
        self._anim_type = None

    def _start_fetch(self):
        """Pick a side and a landing spot, and remember where the mon set off.

        The side ALTERNATES WITH A WOBBLE - it usually swaps, but not always.
        A fixed side is wallpaper by the tenth press, and strict alternation is
        just a slower kind of predictable (plan 21.1).
        """
        if random.random() < 0.8:
            self._fetch_side = -self._fetch_side
        # Land inside the wander circle, not at the bezel: the mon has to be
        # able to stand there, and _move takes over again from wherever this
        # leaves it.
        self._fetch_x = MOVE_CX + self._fetch_side * MOVE_R * 0.85
        self._fetch_y = MOVE_CY + (random.random() - 0.5) * MOVE_R
        self._fetch_from = (self._x, self._y)
        self._fetch_scored = False
        # Pick the destination NOW, so the carry has somewhere to go. The label
        # does not change until the mon actually gets there (_ball_btn).
        self._carry_btn = random.choice(self._FETCH_TARGETS)
        bx, by, _ = self._BTN_POS[self._carry_btn]
        # The mon cannot stand on a button - they are outside its wander circle -
        # so it walks to the point on the rim nearest one and leaves the ball
        # there. The ball then belongs to the button, not to the mon.
        dx, dy = bx - MOVE_CX, by - MOVE_CY
        d = math.sqrt(dx * dx + dy * dy) or 1.0
        self._drop_x = MOVE_CX + dx / d * MOVE_R
        self._drop_y = MOVE_CY + dy / d * MOVE_R

    @staticmethod
    def _ease(w):
        """Smoothstep. Eases in and out, so a leg reads as a creature deciding
        to move rather than a sprite being slid."""
        w = 0.0 if w < 0.0 else (1.0 if w > 1.0 else w)
        return w * w * (3.0 - 2.0 * w)

    def _walk_to_fetch(self):
        """Two legs: out to the ball, then back to a button with it.

        Position is derived from _anim_t, not accumulated per frame: a walk that
        integrates its own velocity drifts when frames are slow, and both legs
        have to land exactly on their target or the mon appears to miss the ball
        and then to drop it somewhere that is not the button.
        """
        p = min(1.0, self._anim_t / self._anim_ms())
        t0 = self._FETCH_THROW
        if not self._carries():
            # FEEDING. One leg: go to the thing and eat it. The walk gets ALL
            # the time after the throw rather than stopping two thirds of the
            # way through and standing there.
            #
            # Branched explicitly rather than by setting t1 = 1.0 and falling
            # through: at exactly p == 1.0 the `p < t1` test below is FALSE, and
            # a Feed would have run the carry leg and scored happiness. One slow
            # frame away from happening.
            w = self._ease(0.0 if p <= t0 else (p - t0) / (1.0 - t0))
            fx, fy = self._fetch_from
            self._x = fx + (self._fetch_x - fx) * w
            self._y = fy + (self._fetch_y - fy) * w
            return
        t1 = self._FETCH_CARRY
        if p < t1:
            # Leg one: stand still while the throw is in the air, then go and
            # get it.
            w = self._ease(0.0 if p <= t0 else (p - t0) / (t1 - t0))
            fx, fy = self._fetch_from
            self._x = fx + (self._fetch_x - fx) * w
            self._y = fy + (self._fetch_y - fy) * w
            return
        # Leg two: carrying it to the rim nearest the button it chose.
        w = self._ease((p - t1) / (1.0 - t1))
        self._x = self._fetch_x + (self._drop_x - self._fetch_x) * w
        self._y = self._fetch_y + (self._drop_y - self._fetch_y) * w
        # REACHING THE BALL is what earns the happiness (owner's spec), which is
        # here - the start of the carry - not at the end of the animation. Once
        # per fetch, hence the flag: this runs every frame of the carry.
        if not self._fetch_scored:
            self._fetch_scored = True
            self._score_fetch()

    # Feeding grants its food on the PRESS, in _do_action, exactly as it always
    # has. Only the fetch game moves a stat on arrival, and only ever `fun` -
    # this used to run for both, so a Feed quietly handed out happiness too.

    def _move(self, delta):
        self._x += self._vx * delta
        self._y += self._vy * delta
        dx, dy = self._x - MOVE_CX, self._y - MOVE_CY
        dist = math.sqrt(dx * dx + dy * dy)
        if dist > MOVE_R:
            # reflect the velocity off the circular boundary
            nx, ny = dx / (dist or 1), dy / (dist or 1)
            dot = self._vx * nx + self._vy * ny
            self._vx -= 2 * dot * nx
            self._vy -= 2 * dot * ny
            self._x = MOVE_CX + nx * MOVE_R
            self._y = MOVE_CY + ny * MOVE_R
            # a little wobble so it doesn't get stuck in a loop, then
            # renormalise so the wander speed stays constant (no drift)
            self._vx += (random.random() - 0.5) * 0.008
            self._vy += (random.random() - 0.5) * 0.008
            mag = math.sqrt(self._vx * self._vx + self._vy * self._vy) or 1
            self._vx = self._vx / mag * MOVE_SPEED
            self._vy = self._vy / mag * MOVE_SPEED

    # --- drawing -----------------------------------------------------------
    def draw(self, ctx):
        ctx.save()

        addon = self._addon()
        if addon is not None:
            try:
                addon.draw(ctx)
                self._addon_draw_errs = 0
            except Exception as e:
                print("EMFMon: addon draw error:", e)
                # bail out of the addon view if drawing keeps failing, so a
                # persistent error can't strand the user on a broken screen
                self._addon_draw_errs = getattr(self, "_addon_draw_errs", 0) + 1
                if self._addon_draw_errs >= 5:
                    self._close_addon()
            if self.dialog is not None:
                # Modal, so it draws ON TOP of the addon. Without this the
                # dialog would be invisible over a full-screen addon - the
                # overlay pass below is on the pet path, which we never reach.
                self.draw_overlays(ctx)
            ctx.restore()
            return

        clear_background(ctx)

        if self.pet["alive"]:
            self._draw_poops(ctx)
            self._draw_pet(ctx)
            self._draw_action_anim(ctx)
            if self._play_game:
                self._draw_play_hud(ctx)     # replaces the bars and the legend
            else:
                self._draw_actions(ctx)
        else:
            self._draw_dead(ctx)

        if not self._play_game:
            self._draw_bars(ctx)
        if self.pet.get("paused") and self.pet.get("alive", True):
            self._draw_paused(ctx)          # last: over the frozen mon
        if self.arc is not None:
            self.arc.draw(ctx)      # overlay: drawn last, over everything
        ctx.restore()
        self.draw_overlays(ctx)

    # Pause bars: two rectangles, not a font glyph. §19.6's rule is to use the
    # font where it HAS the shape - it carries arrows and triangles, and it does
    # not carry a pause symbol, so drawing one is the honest option rather than
    # gambling on a codepoint that renders as a blank square on the badge.
    _PAUSE_BAR_W = 15
    _PAUSE_BAR_H = 46
    _PAUSE_BAR_GAP = 14
    _PAUSE_SYM_Y = -34          # centre of the bars
    _PAUSED_TEXT_Y = 22

    def _draw_paused(self, ctx):
        """The PAUSED state, in the middle, over a mon that is not moving.

        Centre rather than tucked under the name (where it first went), because
        the middle is where the eye already is - it is where the mon is - and a
        state that stops everything should not be announced in the margin.
        """
        ctx.rgb(*self._PAUSED_RGB)
        half = self._PAUSE_BAR_GAP / 2.0
        top = self._PAUSE_SYM_Y - self._PAUSE_BAR_H / 2.0
        for x in (-half - self._PAUSE_BAR_W, half):
            ctx.rectangle(x, top, self._PAUSE_BAR_W, self._PAUSE_BAR_H).fill()
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 24
        ctx.move_to(0, self._PAUSED_TEXT_Y).text("PAUSED")
        ctx.font_size = 10
        ctx.rgb(0.72, 0.72, 0.78)
        ctx.move_to(0, self._PAUSED_TEXT_Y + 18).text("F to resume")

    def _draw_poops(self, ctx):
        # little brown blobs the pet has left; Clean wipes them away
        # (a light, warm brown so it stays visible on the dark screen)
        ctx.rgb(0.72, 0.48, 0.22)
        for px, py in self.pet.get("poops", []):
            ctx.arc(px, py, 4, 0, 2 * math.pi, False).fill()

    def _draw_pet(self, ctx):
        r, g, b = self.pet["colour"]
        # size grows over real running time: tiny dot -> full size in GROW_MS
        grow = min(1.0, self.pet.get("grow_ms", 0.0) / GROW_MS)
        s = PET_MIN_SIZE + (PET_MAX_SIZE - PET_MIN_SIZE) * grow
        x, y = self._x, self._y
        ctx.rgb(r, g, b)
        shape = self.pet["shape"]
        face_cy = y  # face centred by default
        if shape == "square":
            ctx.rectangle(x - s, y - s, 2 * s, 2 * s).fill()
        elif shape == "circle":
            ctx.arc(x, y, s, 0, 2 * math.pi, False).fill()
        elif shape == "triangle":
            # drawn apex-up (not a centred regular tri), so drop the face lower
            ctx.begin_path()
            ctx.move_to(x, y - s)
            ctx.line_to(x + s, y + s)
            ctx.line_to(x - s, y + s)
            ctx.close_path()
            ctx.fill()
            face_cy = y + s * 0.35
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
            ctx.arc(x, y, s, 0, 2 * math.pi, False).fill()  # unknown -> circle

        # A face, once the pet is big enough for it to actually render.
        stage = _life_stage(self.pet["age"])
        if s >= 5:
            pet = self.pet
            if pet["health"] < RED_AT:
                mood = "dying"  # health critical - X_X, near death
            elif any(pet[st] < RED_AT for st in ("food", "fun", "clean")):
                mood = "unhappy"  # a need is low - frown
            else:
                mood = "happy"
            self._draw_face(ctx, x, face_cy, s, mood, stage)
        # elders wear a little gold crown, sitting on top of the body
        if stage == "elder" and s >= 4:
            self._draw_crown(ctx, x, y - s, s)

    def _draw_face(self, ctx, x, cy, s, mood, stage="adult"):
        # young pets get bigger, cuter eyes
        eye_scale = 1.3 if stage in ("baby", "child") else 1.0
        eye_dx = s * 0.34
        eye_y = cy - s * 0.15
        ctx.line_width = max(1.0, s * 0.09)
        if mood == "dying":
            # X_X eyes (two crossed strokes each)
            ctx.rgb(0, 0, 0)
            er = s * 0.16 * eye_scale
            for sx in (-eye_dx, eye_dx):
                ex = x + sx
                ctx.begin_path()
                ctx.move_to(ex - er, eye_y - er)
                ctx.line_to(ex + er, eye_y + er)
                ctx.move_to(ex + er, eye_y - er)
                ctx.line_to(ex - er, eye_y + er)
                ctx.stroke()
        else:
            # round eyes with a black pupil (readable on any body colour)
            for sx in (-eye_dx, eye_dx):
                ctx.rgb(1, 1, 1).arc(x + sx, eye_y, s * 0.2 * eye_scale, 0, 2 * math.pi, False).fill()
                ctx.rgb(0, 0, 0).arc(x + sx, eye_y, s * 0.09 * eye_scale, 0, 2 * math.pi, False).fill()
        if stage == "baby":
            return  # babies have no mouth yet
        # mouth: smile when happy, frown otherwise
        ctx.rgb(0, 0, 0)
        ctx.begin_path()
        if mood == "happy":
            ctx.arc(x, cy + s * 0.05, s * 0.32, 0.18 * math.pi, 0.82 * math.pi, False)
        else:
            ctx.arc(x, cy + s * 0.55, s * 0.32, 1.18 * math.pi, 1.82 * math.pi, False)
        ctx.stroke()

    def _draw_crown(self, ctx, x, yb, s):
        # a little three-point gold crown perched on an elder's head (base at yb)
        w = s * 0.8
        h = s * 0.55
        ctx.rgb(1.0, 0.84, 0.0)
        ctx.begin_path()
        ctx.move_to(x - w, yb)
        ctx.line_to(x - w, yb - h * 0.55)
        ctx.line_to(x - w * 0.45, yb - h * 0.2)
        ctx.line_to(x, yb - h)
        ctx.line_to(x + w * 0.45, yb - h * 0.2)
        ctx.line_to(x + w, yb - h * 0.55)
        ctx.line_to(x + w, yb)
        ctx.close_path()
        ctx.fill()

    def _draw_action_anim(self, ctx):
        if self._anim_type is None:
            return
        a = self._anim_type
        p = min(1.0, self._anim_t / self._anim_ms())
        if a in self._FETCH:
            self._draw_fetch(ctx, a, p)
            return
        if a == "clean":
            self._draw_wash(ctx, p)
            return
        # Heal is UNCHANGED. Plan 21 revamps Play, Feed and Clean; this one was
        # not asked about and is not touched.
        fade = 1.0 - p
        px, py = self._x, self._y
        if a == "injection":  # Heal
            # a green "+" floats up above the pet and fades
            cy = py - 22 - p * 20
            ctx.rgba(0.2, 0.9, 0.35, fade)
            ctx.rectangle(px - 2, cy - 8, 4, 16).fill()
            ctx.rectangle(px - 8, cy - 2, 16, 4).fill()

    def _draw_fetch(self, ctx, kind, p):
        """Play and Feed: a thing is thrown in, and the mon goes and gets it.

        ONE animation with a colour and a payload (plan 21.2). The player reads
        both as the same act - you give the mon a thing, the mon comes and takes
        it - and the code says so by having one method rather than two that
        drift apart.
        """
        rgb, radius, carries = self._FETCH[kind]
        t = self._FETCH_THROW
        if p < t:
            # In flight: from off the rim on the chosen side, arcing down to
            # where it lands. sin() gives the lob without a second constant.
            q = p / t
            x0 = self._fetch_side * 128.0
            x = x0 + (self._fetch_x - x0) * q
            y = (MOVE_CY - 70.0) + (self._fetch_y - (MOVE_CY - 70.0)) * q
            y -= math.sin(q * math.pi) * 22.0
            ctx.rgb(*rgb).arc(x, y, radius, 0, 2 * math.pi, False).fill()
            return
        if not carries:
            # FED. The morsel sits where it fell until the mon gets there, and
            # then it is gone - eaten, not carried anywhere. It shrinks over the
            # last moments of the approach so it reads as being eaten rather
            # than as simply being switched off.
            bite = max(0.0, min(1.0, (1.0 - p) / 0.18))
            if bite > 0.0:
                ctx.rgb(*rgb).arc(self._fetch_x, self._fetch_y, radius * bite,
                                  0, 2 * math.pi, False).fill()
            return
        if p < self._FETCH_CARRY:
            # Landed, and being walked toward. It sits where it fell.
            ctx.rgb(*rgb).arc(self._fetch_x, self._fetch_y, radius,
                              0, 2 * math.pi, False).fill()
            return
        # Carried. Drawn ON the mon, slightly ahead of it, so the mon is
        # visibly taking the thing somewhere rather than herding it.
        ahead = 1.0 if self._drop_x >= self._x else -1.0
        ctx.rgb(*rgb).arc(self._x + ahead * 7.0, self._y + 4.0, radius,
                          0, 2 * math.pi, False).fill()

    def _draw_wash(self, ctx, p):
        """One gradient fill that travels across the display.

        A gradient PANE, not drawn water: plan 6.2 settled this once already for
        the ArcMenu scrim, where a banded fake cost twelve fills and showed seams
        that one gradient does not. Falls back to a flat band if the gradient
        calls are missing, and remembers that rather than retrying every frame.
        """
        # Right to left, and it must clear the screen entirely at both ends -
        # starting mid-screen would look like a wipe that had already begun.
        lead = 130.0 - p * (260.0 + self._WASH_W)
        if self._wash_grad is not False:
            try:
                ctx.linear_gradient(lead + self._WASH_W, 0, lead, 0)
                ctx.add_stop(0.0, self._WASH_RGB, 0.0)
                ctx.add_stop(0.5, self._WASH_RGB, 0.55)
                ctx.add_stop(1.0, self._WASH_RGB, 0.0)
                ctx.rectangle(-120, -120, 240, 240).fill()
                self._wash_grad = True
                return
            except Exception as e:
                if self._wash_grad is None:
                    print("EMFMon: no gradient support, flat wash:", e)
                self._wash_grad = False
        ctx.rgba(self._WASH_RGB[0], self._WASH_RGB[1], self._WASH_RGB[2], 0.35)
        ctx.rectangle(lead, -120, self._WASH_W, 240).fill()

    # Where each button's label is drawn, and what it says. ONE table, because
    # the ball has to land exactly where the word is - two copies of these
    # coordinates would drift the first time a label moved.
    #
    # MENU and EXIT are deliberately absent from the fetch targets below: a ball
    # parked on either would take one press to clear before you could leave, and
    # a pet game must never stand between a player and the way out.
    _BTN_POS = {
        "UP":      (0, -104, "Food"),
        "RIGHT":   (94, -24, "Clean"),
        "DOWN":    (0, 108, "Play"),
        "CONFIRM": (86, 30, "Items"),
        "LEFT":    (-94, 30, "Menu"),
    }
    # EVERY button except Back. During a game they are all inert anyway - the
    # only two presses that do anything are the one holding the ball and F - so
    # Menu is as good a place to leave a ball as any. Back is excluded because
    # Back is how you stop, and a game you cannot leave is not a game.
    _FETCH_TARGETS = ("UP", "RIGHT", "DOWN", "CONFIRM", "LEFT")
    _BALL_RGB = (1.00, 0.85, 0.20)
    # Back's disc goes red while a game is running. Yellow is what a call-out
    # normally is; red is the one that means stop, and this is the only press
    # on the screen that does.
    _PLAY_BACK_RGB = (0.90, 0.28, 0.28)

    _BALL_DX = 30          # how far the ball sits from the word

    def _draw_ball_marker(self, ctx, key):
        """The ball, beside the button holding it, ALWAYS TOWARD THE CENTRE.

        It used to sit a fixed 30px to the LEFT of the label, which put Menu's
        ball at x=-124 on a screen that ends at -120 - it simply vanished off
        the edge. The offset now follows the sign of the button's own x, so it
        lands inside the screen whichever rim the label is on.

        One method because there were three copies of that offset, which is what
        made one wrong for a year and the other two right (review plan 1.1).
        """
        x, y, _ = self._BTN_POS[key]
        dx = self._BALL_DX if x < 0 else -self._BALL_DX
        ctx.rgb(*self._BALL_RGB)
        ctx.arc(x + dx, y, 4, 0, 2 * math.pi, False).fill()
        ctx.rgb(*self._BALL_RGB)

    def _btn_label(self, key):
        """What this button says right now - its own word, or `pickup` if the
        mon has left a ball on it."""
        return "pickup" if self._ball_btn == key else self._BTN_POS[key][2]

    def _draw_actions(self, ctx):
        ctx.font_size = 13
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        # label at each edge -> which button triggers it
        # EVERY button that can hold a ball, from the one table. Menu was drawn
        # by hand here and kept saying "Menu" after it became a valid target -
        # so the mon took the ball there, nothing on screen changed, and the
        # other four were correctly inert. That reads as the whole game being
        # broken, and it is why this loop covers all of them rather than the
        # three I happened to convert first.
        for key in ("UP", "RIGHT", "DOWN", "LEFT"):
            x, y, _ = self._BTN_POS[key]
            if self._ball_btn == key:
                # The word alone is easy to miss out at the bezel. A ball beside
                # it says which control has the thing, not just that a word
                # changed.
                self._draw_ball_marker(ctx, key)
            else:
                set_color(ctx, "label")
            ctx.move_to(x, y).text(self._btn_label(key))
        # CONFIRM opens the item wheel; dimmed when the pouch is empty.
        # Deliberately NOT _carried_items() - this runs every frame, and building
        # a list 60x/s is the allocation pattern that fragmented the heap into an
        # OOM reboot in _update_searching. Short-circuits on the first item held.
        if self._has_items():
            set_color(ctx, "label")
        else:
            ctx.rgb(0.4, 0.4, 0.4)
        if self._ball_btn == "CONFIRM":
            self._draw_ball_marker(ctx, "CONFIRM")
        ctx.move_to(86, 30).text(self._btn_label("CONFIRM"))

    # The game's own HUD. Deliberately LESS than the pet screen draws: one bar
    # instead of four, no button legend, no trait/stage arcs. It is cheaper than
    # what it replaces - roughly 6 text draws and 8 fills become 2 and 2, plus a
    # cached title - so this is a simplification that happens to look better
    # rather than a flourish that costs something.
    # `Play!` - reverted from `Game` at rev 74 on the owner's look, which is the
    # only test that matters for a title (review plan 2.4). The 21.1c argument
    # that `Play` is the button and `Game` is the place was tidy and wrong on
    # screen.
    _PLAY_TITLE = "Play!"
    _PLAY_TITLE_SIZE = 20
    _PLAY_TITLE_R = 118.0 - 1.5 - 18
    _PLAY_TITLE_MID = 52 * math.pi / 180      # the battle screen's angle
    _PLAY_TITLE_RGB = (1.00, 0.85, 0.20)      # the ball's own yellow

    def _draw_play_hud(self, ctx):
        """What the screen shows while a game of fetch is running.

        Everything the game does not need is GONE: the other three bars, every
        button name, the trait and stage arcs. What is left is the mon, the ball,
        how happy it is, and the one button that stops.
        """
        if self._play_title is None:
            self._play_title = arc_text_layout(
                ctx, self._PLAY_TITLE, self._PLAY_TITLE_R,
                self._PLAY_TITLE_MID, self._PLAY_TITLE_SIZE)
        ctx.font_size = self._PLAY_TITLE_SIZE
        ctx.rgb(*self._PLAY_TITLE_RGB)
        draw_arc_text(ctx, self._play_title)

        # Fun only - it is the only stat this screen can move.
        ctx.text_align = ctx.LEFT
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 11
        bw, bh, x0, y = 44, 7, -22, 86
        set_color(ctx, "label")
        ctx.move_to(-60, y + bh / 2).text("Fun")
        val = self.pet["fun"]
        ctx.rgb(0.25, 0.25, 0.25).rectangle(x0, y, bw, bh).fill()
        if val < RED_AT:
            ctx.rgb(0.9, 0.15, 0.15)
        else:
            ctx.rgb(0.2, 0.8, 0.35)
        ctx.rectangle(x0, y, bw * max(0.0, min(1.0, val / 100.0)), bh).fill()

        # The only button with a name is the one holding the ball, and it only
        # has one once the mon has taken it there.
        ctx.text_align = ctx.CENTER
        if self._ball_btn is not None:
            bx, by, _ = self._BTN_POS[self._ball_btn]
            ctx.font_size = 13
            self._draw_ball_marker(ctx, self._ball_btn)
            ctx.move_to(bx, by).text("pickup")
        self._draw_play_hint(ctx)

    def _draw_play_hint(self, ctx):
        """While a game runs, F is the only press on the screen that does
        something other than throw the ball - so it is the only one called out,
        and it is RED because it means stop rather than "a button does this"."""
        draw_hints(ctx, f="F stop", rgb=self._PLAY_BACK_RGB, joy=False)

    def _draw_dead(self, ctx):
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 22
        ctx.move_to(0, -40).text("R.I.P.")
        ctx.font_size = 16
        ctx.move_to(0, -12).text(f"{self.pet['name']}  ({self.pet['age']}h)")
        ctx.font_size = 13
        set_color(ctx, "label")
        ctx.move_to(0, 20).text("start again?")
        # No joystick glyph: there is nothing to scroll here, only the one
        # decision. F is absent because there is nowhere to go back TO - a dead
        # pet screen has no previous screen, and offering an out that does not
        # exist is worse than offering none.
        draw_hints(ctx, c="C new pet", joy=False)

    _BAR_ROWS = (("HP", "health"), ("Food", "food"),
                 ("Fun", "fun"), ("Clean", "clean"))
    _NAME_RGB = (0.35, 0.75, 1.0)   # same shining blue as a selected menu row
    # The same shining blue as the mon's name and a selected menu row - a colour
    # this app already uses for "this is yours / this is chosen". Not red, which
    # would read as a fault, and not the call-out yellow, which means "a button
    # does this" everywhere else.
    _PAUSED_RGB = _NAME_RGB
    # Trait + life stage ride the upper-right rim as two stacked arcs, in the
    # same yellow as the button call-outs, and deliberately SMALLER than the
    # name so it reads as secondary. Centred between the "Food" label at
    # 0 deg and "Clean" at ~76 deg, and span-capped so a long trait ("Playful")
    # cannot grow into Clean - it shrinks to fit instead.
    _ARC_MID = 38.0 * math.pi / 180
    _ARC_MAX_SPAN = 52.0 * math.pi / 180
    _TRAIT_R = 100.0
    _TRAIT_SIZE = 10
    _STAGE_R = 85.0
    _STAGE_SIZE = 9
    _ARC_RGB = (1.0, 0.83, 0.15)

    def _draw_bars(self, ctx):
        ctx.text_align = ctx.LEFT
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 11
        pet = self.pet
        bw, bh = 44, 7          # bar size (shorter, to leave room for words)
        lx = -60                # label x (full words, left-aligned)
        x0 = -22                # bar x
        y0 = 56
        # _BAR_ROWS is a class constant: this runs every frame on the always-on
        # screen, and rebuilding the row tuples here allocated 5 objects a frame
        for i, (label, key) in enumerate(self._BAR_ROWS):
            val = pet[key]
            y = y0 + i * 12
            set_color(ctx, "label")
            ctx.move_to(lx, y + bh / 2).text(label)
            # bar background
            ctx.rgb(0.25, 0.25, 0.25).rectangle(x0, y, bw, bh).fill()
            # bar fill (red when low, green otherwise)
            if val < RED_AT:
                ctx.rgb(0.9, 0.15, 0.15)
            else:
                ctx.rgb(0.2, 0.8, 0.35)
            ctx.rectangle(x0, y, bw * max(0.0, min(1.0, val / 100.0)), bh).fill()

        # Name + age, back where it reads best: straight, centred, above the
        # bars. CACHED - this runs every frame and the old f-string built a
        # fresh string each time. The key is compared field by field rather
        # than by joining, so the common case allocates nothing at all.
        name = self.pet["name"]
        age = self.pet["age"]
        if self._name_text is None or name != self._nl_name or age != self._nl_age:
            self._name_text = "%s  %dh" % (name, age)
            self._nl_name = name
            self._nl_age = age
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 12
        ctx.rgb(*self._NAME_RGB)
        ctx.move_to(0, -88).text(self._name_text)


        # Trait and life stage curl around the upper-right rim, stacked on two
        # radii, in the call-out yellow - the old grey subtitle was hard to read
        # and it was crowding the name. Also cached: only the stage ever changes.
        stage = _life_stage(age)
        trait = self.pet.get("trait")
        if (self._trait_layout is None or trait != self._nl_trait
                or stage != self._nl_stage):
            self._trait_layout = arc_text_layout(
                ctx, TRAIT_LABEL.get(trait, ""), self._TRAIT_R, self._ARC_MID,
                self._TRAIT_SIZE, max_span=self._ARC_MAX_SPAN)
            self._stage_layout = arc_text_layout(
                ctx, STAGE_LABEL.get(stage, ""), self._STAGE_R, self._ARC_MID,
                self._STAGE_SIZE, max_span=self._ARC_MAX_SPAN)
            self._nl_trait = trait
            self._nl_stage = stage
        ctx.rgb(*self._ARC_RGB)
        ctx.font_size = self._TRAIT_SIZE
        draw_arc_text(ctx, self._trait_layout)
        ctx.font_size = self._STAGE_SIZE
        draw_arc_text(ctx, self._stage_layout)


__app_export__ = EMFMon
