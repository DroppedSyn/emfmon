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
from app_components import Menu, TextDialog, clear_background
from app_components.tokens import set_color
from events.input import BUTTON_TYPES, ButtonDownEvent
from events.joystick import JOYSTICK_BUTTON_TYPES
from system.eventbus import eventbus
from system.notification.events import ShowNotificationEvent
from system.scheduler import scheduler

# --- Tunables --------------------------------------------------------------
# HOUR_MS governs age/health/death only (the "hourly" tick). Needs decay on
# their own real-time schedule below, so changing HOUR_MS does NOT change how
# fast the pet gets hungry.
HOUR_MS = 3600_000  # one "hour" of pet time = one real hour (age only)
DEATH_MS = 1200_000  # a death roll is made this often (every 20 minutes)
HEALTH_TICK_MS = 1800_000  # health is adjusted this often (every 30 minutes)
# How long (real minutes) each need takes to fall from full (100) to empty (0).
# Real-time and independent of HOUR_MS -> food gets hungry in ~10 min at any
# speed. Health is NOT in here: it only moves on the hourly tick.
MINUTES_TO_EMPTY = {"food": 10.0, "fun": 15.0, "clean": 20.0}
RED_AT = 25.0        # a need below this shows red AND hurts health (25%)
NOTIFY_AT = 30.0     # show the "mon!" alert below this (>= RED_AT, an early warning)
ACTION_GAIN = {"food": 35.0, "fun": 35.0, "clean": 40.0, "injection": 30.0}
HEAL_GAIN_MS = 1800_000  # you gain one heal item every 30 minutes (start with 0)
MAX_HEALS = 9            # cap on stored heal items (keeps the count tidy)
HEALTH_DROP = 10.0   # health lost each health tick when any need is below RED_AT
HEALTH_HEAL = 6.0    # health regained each health tick when well cared for
HEALTH_RISK = 20.0   # below this health, death is rolled (every DEATH_MS)
DEATH_CHANCE = 0.1   # 1-in-10 each death roll when at risk ("let's not be mean")

SHAPES = ("square", "triangle")
NAME_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Pet size grows over real running time: a tiny dot at first, full size at GROW_MS.
PET_MIN_SIZE = 1.5
PET_MAX_SIZE = 16.0
GROW_MS = 1800_000  # running-time to grow from a tiny dot to full size (30 min)

# Action feedback animation length (ms).
ANIM_MS = 800

# Movement bounds - kept clear of the name/Food labels above and the bars below.
MOVE_CX, MOVE_CY, MOVE_R = 0, -10, 46
MOVE_SPEED = 0.014  # px per ms of wander speed (lower = slower, gentler)

# Cleanliness "poop" dots: one drops each time Clean falls another POOP_STEP;
# the Clean action wipes them all away.
POOP_STEP = 25.0
MAX_POOPS = 4

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


def _new_pet():
    return {
        "name": _random_name(),
        "shape": random.choice(SHAPES),
        "colour": _random_colour(),
        "age": 0,          # whole hours survived
        "grow_ms": 0.0,    # running-time accumulated toward full size (GROW_MS)
        "heals": 0,        # heal items in inventory (gain 1 per HEAL_GAIN_MS)
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


class EMFMon(app.App):
    def __init__(self):
        super().__init__()
        self.pet = self._load_state() or _new_pet()
        self.history = self._load_history()
        self.view = "pet"          # "pet" | "menu"
        self.menu = None
        self.dialog = None
        self._anim_type = None     # current action animation type, or None
        self._anim_t = 0.0         # ms elapsed in the current animation
        self._hour_acc = 0.0       # ms accumulated toward the next hour tick
        self._health_acc = 0.0     # ms accumulated toward the next health tick
        self._heal_acc = 0.0       # ms accumulated toward the next heal item
        self._death_acc = 0.0      # ms accumulated toward the next death roll
        self._save_acc = 0.0       # ms accumulated toward the next autosave
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
            return base
        except Exception:
            return None

    def _save_state(self):
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

    # --- simulation (runs in background AND foreground) --------------------
    def background_update(self, delta):
        # background_update is called for every app whether focused or not, so
        # ALL time-based simulation lives here (update() only does foreground
        # visuals) to avoid double-counting when the app is in the foreground.
        pet = self.pet
        if not pet["alive"]:
            return

        # Needs decay on a real-time schedule (MINUTES_TO_EMPTY), independent of
        # the HOUR_MS tick, so food empties in ~10 real minutes at any speed.
        # Health is not touched here - it changes on the health tick below.
        for stat, mins in MINUTES_TO_EMPTY.items():
            pet[stat] = max(0.0, pet[stat] - delta * 100.0 / (mins * 60_000.0))

        # grow from a tiny dot to full size over GROW_MS of running time
        pet["grow_ms"] = min(GROW_MS, pet.get("grow_ms", 0.0) + delta)

        # gain one heal item every HEAL_GAIN_MS (up to MAX_HEALS)
        self._heal_acc += delta
        while self._heal_acc >= HEAL_GAIN_MS:
            self._heal_acc -= HEAL_GAIN_MS
            pet["heals"] = min(MAX_HEALS, pet.get("heals", 0) + 1)

        # drop a poop dot each time Clean has fallen another POOP_STEP
        poops = pet["poops"]
        target = int((pet.get("clean_mark", 100.0) - pet["clean"]) / POOP_STEP)
        target = max(0, min(MAX_POOPS, target))
        while len(poops) < target:
            poops.append(_random_poop_pos())

        self._update_notifications()

        self._hour_acc += delta
        while self._hour_acc >= HOUR_MS:
            self._hour_acc -= HOUR_MS
            self._hourly_tick()

        # Health tick (every HEALTH_TICK_MS = 30 min): -10% if any need is red.
        self._health_acc += delta
        while self._health_acc >= HEALTH_TICK_MS:
            self._health_acc -= HEALTH_TICK_MS
            self._health_tick()

        # Death roll on its own faster cadence (every DEATH_MS = 20 min).
        self._death_acc += delta
        while self._death_acc >= DEATH_MS:
            self._death_acc -= DEATH_MS
            if pet["health"] < HEALTH_RISK and random.random() < DEATH_CHANCE:
                self._die()
                break

        self._save_acc += delta
        if self._save_acc >= 15_000:
            self._save_acc = 0.0
            self._save_state()

    def _hourly_tick(self):
        self.pet["age"] += 1

    def _health_tick(self):
        # Every HEALTH_TICK_MS (30 min): lose HEALTH_DROP if any need is in the
        # red (below RED_AT), otherwise slowly recover when well cared for.
        pet = self.pet
        if any(pet[s] < RED_AT for s in ("food", "fun", "clean")):
            pet["health"] = max(0.0, pet["health"] - HEALTH_DROP)
        elif pet["food"] >= 50 and pet["fun"] >= 50 and pet["clean"] >= 50:
            pet["health"] = min(100.0, pet["health"] + HEALTH_HEAL)

    def _die(self):
        pet = self.pet
        pet["alive"] = False
        self.icon.show = False  # clear the home-screen alert; the pet is gone
        self.history.insert(
            0,
            {"name": pet["name"], "age": pet["age"], "shape": pet["shape"]},
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
                },
            )
            self.history = self.history[:20]
            self._save_history()
        self.pet = _new_pet()
        self._hour_acc = 0.0
        self._health_acc = 0.0
        self._heal_acc = 0.0
        self._death_acc = 0.0
        self._anim_type = None
        self.icon.show = False
        self._save_state()

    # --- input -------------------------------------------------------------
    def _on_button(self, event: ButtonDownEvent):
        if self.dialog is not None:
            return  # the text dialog owns the buttons while open
        if self.view == "menu":
            return  # the Menu widget handles its own buttons
        if BUTTON_TYPES["CANCEL"] in event.button:
            self.minimise()
            return
        if not self.pet["alive"]:
            if BUTTON_TYPES["CONFIRM"] in event.button:
                self._hatch_new()
            return
        # Joystick centre press (SELECT) opens the menu - checked before CONFIRM
        # because JOYFIRE also carries CONFIRM (which the C button uses to heal).
        if JOYSTICK_BUTTON_TYPES["SELECT"] in event.button:
            self._open_menu()
        elif BUTTON_TYPES["UP"] in event.button:
            self._do_action("food")
        elif BUTTON_TYPES["DOWN"] in event.button:
            self._do_action("fun")  # Play - moved to D to clear the OS back button
        elif BUTTON_TYPES["RIGHT"] in event.button:
            self._do_action("clean")
        elif BUTTON_TYPES["CONFIRM"] in event.button:
            # Heal (the C / CONFIRM button, not the joystick); spend an item,
            # but not when already at full health
            if self.pet.get("heals", 0) > 0 and self.pet["health"] < 100:
                self.pet["heals"] -= 1
                self._do_action("injection")
        elif BUTTON_TYPES["LEFT"] in event.button:
            self._open_menu()

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
        self._save_state()

    def _open_menu(self):
        def on_select(value, idx):
            self._close_menu()
            if value == "Rename":
                self._rename()
            elif value == "History":
                self.view = "menu"
                self._show_history_menu()
            elif value == "New pet":
                self._hatch_new()

        self.menu = Menu(
            self,
            menu_items=["Rename", "History", "New pet", "Back"],
            select_handler=on_select,
            back_handler=self._close_menu,
        )
        self.view = "menu"

    def _show_history_menu(self):
        if self.history:
            items = [
                f"{h.get('name', '?')} - {h.get('age', 0)}h" for h in self.history
            ]
        else:
            items = ["No deaths yet"]
        items.append("Back")

        def on_select(value, idx):
            self._close_menu()

        self.menu = Menu(
            self,
            menu_items=items,
            select_handler=on_select,
            back_handler=self._close_menu,
        )
        self.view = "menu"

    def _close_menu(self, *args):
        if self.menu is not None:
            self.menu._cleanup()
            self.menu = None
        self.view = "pet"

    def _rename(self):
        self.dialog = TextDialog("Name your pet:", self)
        self.overlays = [self.dialog]

    # --- foreground update (movement + overlays) ---------------------------
    def update(self, delta):
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
        if self.menu is not None:
            self.menu.update(delta)
            return True
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
        clear_background(ctx)

        if self.view == "menu" and self.menu is not None:
            self.menu.draw(ctx)
            ctx.restore()
            return

        if self.pet["alive"]:
            self._draw_poops(ctx)
            self._draw_pet(ctx)
            self._draw_action_anim(ctx)
            self._draw_actions(ctx)
        else:
            self._draw_dead(ctx)

        self._draw_bars(ctx)
        ctx.restore()
        self.draw_overlays(ctx)

    def _draw_poops(self, ctx):
        # little brown blobs the pet has left; Clean wipes them away
        ctx.rgb(0.4, 0.24, 0.08)
        for px, py in self.pet.get("poops", []):
            ctx.arc(px, py, 4, 0, 2 * math.pi, False).fill()

    def _draw_pet(self, ctx):
        r, g, b = self.pet["colour"]
        # size grows over real running time: tiny dot -> full size in GROW_MS
        grow = min(1.0, self.pet.get("grow_ms", 0.0) / GROW_MS)
        s = PET_MIN_SIZE + (PET_MAX_SIZE - PET_MIN_SIZE) * grow
        x, y = self._x, self._y
        ctx.rgb(r, g, b)
        if self.pet["shape"] == "square":
            ctx.rectangle(x - s, y - s, 2 * s, 2 * s).fill()
            face_cy = y
        else:
            # separate path calls (line_to does not chain) - matches the
            # proven pattern in lib/simple_tildagon.py
            ctx.begin_path()
            ctx.move_to(x, y - s)
            ctx.line_to(x + s, y + s)
            ctx.line_to(x - s, y + s)
            ctx.close_path()
            ctx.fill()
            face_cy = y + s * 0.35  # sits lower, over the triangle's body

        # A face, once the pet is big enough for it to actually render.
        if s >= 5:
            pet = self.pet
            if pet["health"] < RED_AT:
                mood = "dying"  # health critical - X_X, near death
            elif any(pet[st] < RED_AT for st in ("food", "fun", "clean")):
                mood = "unhappy"  # a need is low - frown
            else:
                mood = "happy"
            self._draw_face(ctx, x, face_cy, s, mood)

    def _draw_face(self, ctx, x, cy, s, mood):
        eye_dx = s * 0.34
        eye_y = cy - s * 0.15
        ctx.line_width = max(1.0, s * 0.09)
        if mood == "dying":
            # X_X eyes (two crossed strokes each)
            ctx.rgb(0, 0, 0)
            er = s * 0.16
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
                ctx.rgb(1, 1, 1).arc(x + sx, eye_y, s * 0.2, 0, 2 * math.pi, False).fill()
                ctx.rgb(0, 0, 0).arc(x + sx, eye_y, s * 0.09, 0, 2 * math.pi, False).fill()
        # mouth: smile when happy, frown otherwise
        ctx.rgb(0, 0, 0)
        ctx.begin_path()
        if mood == "happy":
            ctx.arc(x, cy + s * 0.05, s * 0.32, 0.18 * math.pi, 0.82 * math.pi, False)
        else:
            ctx.arc(x, cy + s * 0.55, s * 0.32, 1.18 * math.pi, 1.82 * math.pi, False)
        ctx.stroke()

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
        # Heal shows how many heal items you have; dimmed when you have none
        heals = self.pet.get("heals", 0)
        if heals > 0:
            set_color(ctx, "label")
        else:
            ctx.rgb(0.4, 0.4, 0.4)
        ctx.move_to(86, 30).text("Heal x%d" % heals)  # CONFIRM (lower-right)

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

    def _draw_bars(self, ctx):
        ctx.text_align = ctx.LEFT
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 11
        rows = (
            ("HP", self.pet["health"]),
            ("Food", self.pet["food"]),
            ("Fun", self.pet["fun"]),
            ("Clean", self.pet["clean"]),
        )
        bw, bh = 44, 7          # bar size (shorter, to leave room for words)
        lx = -60                # label x (full words, left-aligned)
        x0 = -22                # bar x
        y0 = 56
        for i, (label, val) in enumerate(rows):
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


__app_export__ = EMFMon
