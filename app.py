"""EMFMon - a Tamagotchi-style pet for the Tildagon badge.

The pet is a randomly-coloured square or triangle that wanders the screen and
has four 0-100 stats (higher = better): Health, Food, Fun, Clean. Food/Fun/Clean
decay over real time; a need below 25% turns red and, on the 30-minute health
tick, drops Health by 10%. Once Health is low the pet has a small chance of
dying on each 20-minute death roll. The pet grows from a dot to full size, leaves
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

from .arcmenu import ArcMenu, ticks_diff, ticks_ms
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
    try:
        random.seed(seed & 0xFFFFFFFF)
    except Exception:
        pass


_seed_rng()

# --- Tunables --------------------------------------------------------------
# HOUR_MS governs age/health/death only (the "hourly" tick). Needs decay on
# their own real-time schedule below, so changing HOUR_MS does NOT change how
# fast the pet gets hungry.
HOUR_MS = 3600_000  # one "hour" of pet time = one real hour (age only)
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
}
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
GROW_MS = 43200_000  # on-time to grow from a tiny dot to full size (~12 hours)

# Action feedback animation length (ms).
ANIM_MS = 800

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
        "hour_acc": 0.0,   # -> age tick
        "health_acc": 0.0,  # -> health tick
        "death_acc": 0.0,  # -> death roll
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
    """A tiny always-on-top overlay that shows a 'mon!' tag on the home screen
    (and over any app) whenever the pet needs attention - like the battery icon."""

    def __init__(self):
        super().__init__()
        self.show = False

    def draw(self, ctx):
        if not self.show:
            return
        ctx.save()
        # small red "mon!" tag in the top-right corner
        cx, cy = 82, -52
        ctx.rgb(0.9, 0.15, 0.15).rectangle(cx - 21, cy - 9, 42, 18).fill()
        ctx.rgb(1, 1, 1)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 14
        ctx.move_to(cx, cy).text("mon!")
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
        self.view = "pet"          # "pet" | "battle"
        self.dialog = None
        self.battle = None         # optional battle addon controller (battle.py)
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
        self._anim_type = None     # current action animation type, or None
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
        """Snapshot the outgoing mon's battle record for its history entry and
        clear the live one, so the successor starts from 0W-0L.

        Returns None if the battle addon isn't importable - the history entry is
        still written, it just has no record attached.
        """
        try:
            from .battle import archive_records, blank_records
        except Exception as e:
            print("EMFMon: archive records failed:", e)
            return None
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
        # Every HEALTH_TICK_MS (30 min): lose HEALTH_DROP if any need is in the
        # red (below RED_AT), otherwise slowly recover when well cared for.
        pet = self.pet
        if any(pet[s] < RED_AT for s in ("food", "fun", "clean")):
            youth = max(0.0, HEALTH_MATURE_AGE - pet["age"]) / HEALTH_MATURE_AGE
            drop = HEALTH_DROP * (1.0 + HEALTH_AGE_BONUS * youth)
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
        self.icon.show = pet["alive"] and any(
            pet[s] < NOTIFY_AT for s in ("food", "fun", "clean")
        )

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
            return  # the text dialog owns the buttons while open
        if self.view == "battle":
            return  # Battle registers its own ButtonDownEvent handler (battle.py)
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
        if BUTTON_TYPES["CANCEL"] in event.button:
            self.minimise()
            return
        if not self.pet["alive"]:
            if BUTTON_TYPES["CONFIRM"] in event.button:
                self._hatch_new()
            return
        if BUTTON_TYPES["UP"] in event.button:
            self._do_action("food")
        elif BUTTON_TYPES["DOWN"] in event.button:
            self._do_action("fun")  # Play - moved to D to clear the OS back button
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
        spec = ITEMS.get(iid)
        if spec is None or pet.get("inv", {}).get(iid, 0) <= 0:
            return False
        heal = spec.get("heal")
        if heal is not None:
            if pet["health"] >= 100:
                return False      # never burn a restorative at full HP
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
        self._anim_type = action  # kick off the feedback animation
        self._anim_t = 0.0
        self._dirty = True        # written by the next autosave (<=3s)

    # Order matters: the destructive one is last, and the two most-used are
    # first, so a mis-scroll lands on something harmless.
    _MAIN_MENU = ("Items", "Battle", "History", "Rename", "New pet")

    def _open_arc(self, items, on_select, on_back=None, idx=0,
                  hint_c="C pick", hint_f="F back", side="right"):
        """Open a curved overlay menu. on_select(idx)/on_back(idx) are called
        with the row that was current when it closed; the menu is torn down
        first, so a handler is free to open another one. `side` should match the
        button that opened it (LEFT button -> "left")."""
        m = self.arcmenu               # reconfigured, never reallocated
        m.set_items(items)
        m.idx = min(max(0, idx), max(0, len(m.items) - 1))
        m.hint_c = hint_c
        m.hint_f = hint_f
        m.side = side
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
            elif value == "New pet":
                self._confirm_new_pet()

        # opened by the LEFT button, so it flies out from the left edge
        self._open_arc(list(self._MAIN_MENU), on_select, side="left")

    def _confirm_new_pet(self):
        """New pet REPLACES a living mon - it is archived to history and a
        stranger hatches in its place. That is the only irreversible thing in
        the app, so it does not happen on a single press."""
        if not self.pet.get("alive", True):
            self._hatch_new()      # nothing to lose - the mon is already gone
            return
        name = str(self.pet.get("name", "your mon"))

        def on_select(idx):
            if idx == 1:
                self._hatch_new()
            else:
                self._open_menu()  # "Cancel" goes back where they came from

        self._open_arc(
            ["Cancel", "Replace " + name],
            on_select,
            on_back=lambda idx: self._open_menu(),
            idx=0,                 # default to the safe option, never the ruin
            side="left",           # (default "C pick" call-out: "C confirm"
            #                        is 9 chars and overruns the bezel there)
        )

    def _open_battle(self):
        # Battle is an OPTIONAL addon - if it fails to import or construct, the
        # pet must carry on unaffected, so swallow everything and stay on the pet.
        try:
            from .battle import Battle
            self.battle = Battle(self, opened_by=self._arc_event)
            self._battle_draw_errs = 0
            self.view = "battle"
        except Exception as e:
            print("EMFMon: battle unavailable:", e)
            self.battle = None
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
            ["%s x%d" % (ITEMS[i]["label"], inv.get(i, 0)) for i in carried],
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
            self._battle_draw_errs = 0
            self.view = "battle"
        except Exception as e:
            print("EMFMon: records view unavailable:", e)
            self.battle = None
            self.view = "pet"

    def _close_battle(self):
        if self.battle is not None:
            try:
                self.battle.close()
            except Exception as e:
                print("EMFMon: battle close error:", e)
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

    def _rename(self):
        self.dialog = TextDialog("Name your pet:", self)
        self.overlays = [self.dialog]

    # --- foreground update (movement + overlays) ---------------------------
    def update(self, delta):
        if self.view == "battle" and self.battle is not None:
            try:
                self.battle.update(delta)
            except Exception as e:
                print("EMFMon: battle update error:", e)
                self._close_battle()
                return True
            if self.battle.done:
                self._close_battle()
            return True
        if self.dialog is not None:
            # drive the text dialog to completion, then apply the new name
            if self.dialog._result is not None:
                if self.dialog._result:
                    name = self.dialog.text.strip().upper()[:8]
                    if name:
                        self.pet["name"] = name
                        self._save_state()
                self.overlays = []
                self.dialog = None
            return True
        # ArcMenu is pure overlay - it has no update of its own, and the pet
        # deliberately keeps simulating behind it
        if self.pet["alive"]:
            self._move(delta)
            if self._anim_type is not None:
                self._anim_t += delta
                if self._anim_t >= ANIM_MS:
                    self._anim_type = None
        return True

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

        if self.view == "battle" and self.battle is not None:
            try:
                self.battle.draw(ctx)
                self._battle_draw_errs = 0
            except Exception as e:
                print("EMFMon: battle draw error:", e)
                # bail out of the battle view if drawing keeps failing, so a
                # persistent error can't strand the user on a broken screen
                self._battle_draw_errs = getattr(self, "_battle_draw_errs", 0) + 1
                if self._battle_draw_errs >= 5:
                    self._close_battle()
            ctx.restore()
            return

        clear_background(ctx)

        if self.pet["alive"]:
            self._draw_poops(ctx)
            self._draw_pet(ctx)
            self._draw_action_anim(ctx)
            self._draw_actions(ctx)
        else:
            self._draw_dead(ctx)

        self._draw_bars(ctx)
        if self.arc is not None:
            self.arc.draw(ctx)      # overlay: drawn last, over everything
        ctx.restore()
        self.draw_overlays(ctx)

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
        p = min(1.0, self._anim_t / ANIM_MS)  # 0 -> 1 over the animation
        fade = 1.0 - p
        px, py = self._x, self._y
        a = self._anim_type
        if a == "food":
            # an orange pellet drops from above onto the pet
            fy = py - 40 * (1.0 - p)
            ctx.rgb(0.9, 0.55, 0.15).arc(px, fy, 5, 0, 2 * math.pi, False).fill()
        elif a == "fun":  # Play
            # a yellow sparkle burst radiating outward
            r = 6 + p * 28
            ctx.rgba(1.0, 0.9, 0.2, fade)
            for i in range(6):
                ang = i * (math.pi / 3) + p * 1.2
                ctx.arc(
                    px + math.cos(ang) * r,
                    py + math.sin(ang) * r,
                    2 + 3 * fade,
                    0,
                    2 * math.pi,
                    False,
                ).fill()
        elif a == "clean":
            # light-blue bubbles rising and fading
            ctx.rgba(0.6, 0.85, 1.0, fade)
            for i in range(5):
                ctx.arc(
                    px + (i - 2) * 9,
                    py - p * 36 - i * 3,
                    3 + (i % 2),
                    0,
                    2 * math.pi,
                    False,
                ).fill()
        elif a == "injection":  # Heal
            # a green "+" floats up above the pet and fades
            cy = py - 22 - p * 20
            ctx.rgba(0.2, 0.9, 0.35, fade)
            ctx.rectangle(px - 2, cy - 8, 4, 16).fill()
            ctx.rectangle(px - 8, cy - 2, 16, 4).fill()

    def _draw_actions(self, ctx):
        ctx.font_size = 13
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        # label at each edge -> which button triggers it
        ctx.move_to(0, -104).text("Food")        # UP     (top)
        ctx.move_to(-94, 30).text("Menu")        # LEFT   (lower-left)
        ctx.move_to(94, -24).text("Clean")       # RIGHT  (upper-right)
        ctx.move_to(0, 108).text("Play")         # DOWN   (bottom, under bars)
        # CONFIRM opens the item wheel; dimmed when the pouch is empty.
        # Deliberately NOT _carried_items() - this runs every frame, and building
        # a list 60x/s is the allocation pattern that fragmented the heap into an
        # OOM reboot in _update_searching. Short-circuits on the first item held.
        if self._has_items():
            set_color(ctx, "label")
        else:
            ctx.rgb(0.4, 0.4, 0.4)
        ctx.move_to(86, 30).text("Items")  # CONFIRM (lower-right)

    def _draw_dead(self, ctx):
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        set_color(ctx, "label")
        ctx.font_size = 22
        ctx.move_to(0, -40).text("R.I.P.")
        ctx.font_size = 16
        ctx.move_to(0, -12).text(f"{self.pet['name']}  ({self.pet['age']}h)")
        ctx.font_size = 13
        ctx.move_to(0, 20).text("CONFIRM: new pet")

    _BAR_ROWS = (("HP", "health"), ("Food", "food"),
                 ("Fun", "fun"), ("Clean", "clean"))

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

        # name + age
        set_color(ctx, "label")
        ctx.text_align = ctx.CENTER
        ctx.font_size = 12
        ctx.move_to(0, -88).text(f"{self.pet['name']}   {self.pet['age']}h")
        # personality + life stage, a small subtitle under the name
        stage = _life_stage(self.pet["age"])
        sub = (TRAIT_LABEL.get(self.pet.get("trait"), "") + "  " + STAGE_LABEL.get(stage, "")).strip()
        if sub:
            ctx.font_size = 9
            ctx.rgb(0.55, 0.55, 0.6)
            ctx.move_to(0, -76).text(sub)


__app_export__ = EMFMon
