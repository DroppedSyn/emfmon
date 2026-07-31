"""The Trainer screen. Plan 8.1.2.

Who *you* are, as opposed to who your mon is: trainer level, battlepoints, the
ranked record, and how much of the action pool you have collected. Read-only,
one screen, no submenus - consistent with 8.1.1.

SEPARATE MODULE ON PURPOSE, and it is the same reason records.py exists. This
screen hangs off the MAIN menu, so opening it must not pay battle.py's 1126 ms
of blocking compile against a 5 s watchdog (plan 6.2.2) - a stall that once
rebooted a badge just for opening Battle. So this module imports records.py and
the drawing helpers, and NOTHING from battle.py.

That is a live constraint, not a historical note. Everything on this screen is
tempting to source from the combat model: the action names for the pool, the
outcome colours the records screen uses, ACTIONS to count the collectible set.
One `from .battle import ...` puts the whole combat model on the main menu.
Restate the number (records.py's N_COLLECTIBLE) or leave the detail out.

Per-frame: nothing here changes while the screen is open - a battle cannot end
behind it, and a mon cannot age into a level. So every string is built ONCE on
entry and the draw is pure text placement (plan 6.2, 8.1.2).
"""

from app_components import clear_background
from app_components.tokens import set_color
from events.input import BUTTON_TYPES, ButtonDownEvent
from system.eventbus import eventbus

import math

from .arcmenu import arc_text_layout, draw_arc_text, draw_hints
from .records import (
    DEFAULT_TRAINER_NAME,
    N_ACTIONS,
    N_COLLECTIBLE,
    _load_records,
    clean_trainer_name,
    level_progress,
    load_trainer,
    save_trainer,
)

# --- layout ----------------------------------------------------------------
# Centred coordinates on the 240x240 round display. EVERYTHING rides the rim, in
# two stacks curling inward from it: identity in the upper right, the tallies in
# the lower left. The middle is left empty on purpose - a round screen has least
# room at its edges, so giving each line its own radius is what stops any two of
# them ever meeting.

# A FULL ring, the same construction battle.py frames BATTLE MODE with, in the
# app's shining blue - the colour already worn by a selected menu row, the mon's
# nameplate and the queue-screen title, so this is the palette being reused
# rather than a fourth colour being invented.
#
# It was white, and white was only ever chosen as NOT-red: red is the battle
# screen's colour and means a fight is on, so this one took the default. A rim
# colour of its own means the screen is identifiable before a word is read,
# which is what the other two already get.
#
# Owner's call, plan 8.1.2. If it reads too bright against the bezel on real
# hardware, deepen it the way battle.py deepens its ring from its title
# (0.95,0.18,0.18 -> 0.85,0.12,0.12) rather than picking a new hue.
_RING_W = 3
_RING_R = 120 - _RING_W / 2 - 1
_RING_RGB = (0.35, 0.75, 1.0)

# Title and level are ONE curved string on ONE radius - "TRAINER  lvl: 4" reads
# as a single line rather than a name with a caption. Span-capped, because
# "lvl: 999" is two characters longer than "lvl: 4" and must shrink to fit
# rather than run round the bezel. At 108 degrees the widest case measures 107.3
# on desk metrics, so there is very little spare and the badge's real font may
# shrink it slightly - which is the cap working, not the cap being wrong.
_ARC_MID = 52 * math.pi / 180         # clockwise from 12 o'clock
_ARC_MAX_SPAN = 108 * math.pi / 180
_TITLE_R = _RING_R - _RING_W / 2 - 18
_TITLE_SIZE = 18

# The XP bar sits directly beneath that line, on the same centre angle so the
# two read as one block curling with the bezel.
_PROG_R = _TITLE_R - 20
_PROG_W = 8                           # thick enough to read as a bar, not a hair
_PROG_SPAN = 92 * math.pi / 180
_PROG_BG_RGB = (0.24, 0.24, 0.28)
_PROG_FG_RGB = (1.0, 1.0, 1.0)

# The lower-left stack, flipped so it reads the right way up: W/L/D outermost,
# BA inside it. Mirrors the upper-right stack, and leaves the middle for the
# name - a round display has least room at its edges, so the things that must
# never collide are the ones given their own radius.
_REC_MID = 232 * math.pi / 180
_REC_MAX_SPAN = 92 * math.pi / 180
_REC_R = _RING_R - _RING_W / 2 - 14
_REC_SIZE = 17

# Label and value share one arc and one size, so each is ONE measured string -
# the label recedes by being dimmer rather than smaller, which gets the same
# read without butting two separately measured arcs together.
#
# BP joins the UPPER stack, under the XP bar - the bar fills toward the level and
# the score is what fills it, so the three belong together. NOT flipped: it is on
# the top half now. It is also the biggest text on the screen, which is why it
# gets the widest span: at r=60 a 20pt string eats angle fast, and the cap is set
# so a six-figure score still fits at full size rather than so nothing ever
# shrinks. It holds to 123,456 bp - level 50, about 1,100 wins.
_BP_R = _PROG_R - 18
_BP_SIZE = 20
_BP_MAX_SPAN = 130 * math.pi / 180

# BA takes the slot BP left, directly above the record on the lower rim.
_STAT_MAX_SPAN = 100 * math.pi / 180
_STAT_SIZE = 16
_BA_R = _REC_R - 22
_LABEL_K = 0.55       # how far the label drops behind its own value
_LABEL_LEN = 4        # "BP: " / "BA: " - where the label ends and the value
                      # starts, and the reason both are written with one space
                      # after the colon rather than however many looked right

_TITLE_RGB = (1.0, 1.0, 1.0)

# Orange for the score, yellow for the pool - the badge's existing call-out
# yellow, so it is a colour already in the vocabulary rather than a new one.
#
# NOTHING on this screen pulses. It did, on pulse_k, and it was removed: a
# reference screen you open to read a number does not need to breathe at you,
# and the pulse was competing with the one place motion means something (a
# selected menu row, which is telling you where the stick is).
_BP_RGB = (1.0, 0.55, 0.12)
_BA_RGB = (1.0, 0.83, 0.15)

# The trainer's name, tilted across the middle - the one part of this screen that
# is yours rather than earned, and the only thing in the empty centre. Tilted
# because a horizontal word in the middle of a round screen reads as a caption
# for the rings around it, where a tilted one reads as a signature.
# POSITIVE tilt. Screen y runs downward, so a positive rotation takes the text
# from upper left to lower right - reading downhill. It was negative and ran the
# other way, uphill, which is the mirror image and looked like a mistake.
_NAME_TILT = 19 * math.pi / 180
_NAME_Y = 6
_NAME_SIZE = 36       # the size a short name gets; long ones step down
_NAME_MIN_SIZE = 13
_NAME_MAX_W = 150     # along the tilt, which is the direction it can overrun
_NAME_RGB = (1.0, 1.0, 1.0)

# Outcome colours. The records screen has its own _RESULT_RGB in battle.py and
# these are deliberately NOT imported from it - that import is the whole thing
# this module exists to avoid (6.2.2). Green/red/white is the obvious reading of
# won/lost/drew, so the two agreeing costs nothing and the copy cannot break a
# main-menu screen the way the import would.
_W_RGB = (0.25, 0.85, 0.35)
_L_RGB = (0.92, 0.26, 0.26)
_D_RGB = (1.0, 1.0, 1.0)


def _thousands(n):
    """1728 -> "1,728". No locale on the badge, and battlepoints are the one
    number here that reaches six figures (plan 14.2 wants six digits to render
    without the layout breaking), where an unseparated run stops being legible.

    Built once per screen entry, so the string work costs nothing per frame.
    """
    s = "%d" % n
    if len(s) < 4:
        return s
    out = []
    for i, ch in enumerate(s):
        if i and (len(s) - i) % 3 == 0:
            out.append(",")
        out.append(ch)
    return "".join(out)


def _draw_segments(ctx, layout, segments):
    """Draw one arc layout in coloured runs.

    `segments` is a sequence of (end_index, rgb): each run is drawn from where
    the last ended up to `end_index`. One layout means the whole string was
    measured and spaced in a single pass - only the colour changes between runs,
    so nothing has to be butted together and nothing can drift apart.
    """
    start = 0
    for end, rgb in segments:
        ctx.rgb(*rgb)
        draw_arc_text(ctx, layout[start:end])
        start = end


def _label_value(rgb, n):
    """Segments for a "LABEL: value" arc: dim label, full-strength value.

    The label recedes by being dimmer rather than smaller, so the whole line
    stays one measured string with nothing to butt together (see _STAT_SIZE).
    """
    r, g, b = rgb
    return ((_LABEL_LEN, (r * _LABEL_K, g * _LABEL_K, b * _LABEL_K)),
            (n, rgb))


def _record_parts(w, l, d):
    """("W 3   L 1   D 0", (a, b)) - the record and where to change colour.

    `a` and `b` are indices into the string: [:a] is the win tally, [a:b] the
    loss, [b:] the draw. Each gap belongs to the slice before it, which is why
    the gaps are spaces and their colour never matters.
    """
    gap = "   "
    w_s, l_s, d_s = "W %d" % w, "L %d" % l, "D %d" % d
    a = len(w_s) + len(gap)
    b = a + len(l_s) + len(gap)
    return w_s + gap + l_s + gap + d_s, (a, b)


class TrainerScreen:
    """A full-screen addon, same contract app.py drives Battle through:
    update(delta), draw(ctx), close(), and a `done` flag it polls.

    `opened_by` is the ButtonDownEvent that opened this screen. app.py's arc
    overlay dispatches on the press and THEN the event reaches our own handler,
    so without this the opening press would also close us - the same guard
    battle.py's Battle takes for the same reason.
    """

    def __init__(self, app, opened_by=None):
        self.app = app
        self.done = False
        self._opened_by = opened_by
        # Register our OWN button handler rather than relying on app.py to
        # delegate, which is the pattern battle.py already proved. app.py's
        # _on_button_inner returns early for our view, so nothing else competes.
        #
        # SUBSCRIBE LAST, and nothing that can throw may follow it. A caller
        # cannot clean up a constructor that raised - it never got a reference,
        # so app.py's `except` can only do `self.trainer_screen = None`. Anything
        # throwing AFTER we subscribe therefore leaves this object orphaned on
        # the eventbus, invisibly handling every button press for the life of
        # the app, and doubling each time the screen is reopened.
        #
        # battle.py:1145 does the same thing for the same reason. This used to
        # subscribe before `_build()`, which reads two files; that was safe only
        # because `_build()` swallows everything, i.e. safe by a distant guard
        # rather than by construction, and one narrowed `except` away from real.
        self._input_handler = None
        self._rows = None
        self._rename_pending = False   # C sets this; update() acts on it
        self._dialog_open = False      # last frame's view of app.dialog
        self._build()
        self._subscribe()

    # --- state -------------------------------------------------------------
    def _build(self):
        """Read both files and pre-render every string. Runs ONCE, on entry.

        Both loads are defensive already (they return blanks rather than raising
        on a corrupt or missing file), but this is a screen and a screen must not
        be the thing that takes the badge down - so the whole build is wrapped
        and falls back to a readable zeroed screen.
        """
        try:
            tr = load_trainer(N_ACTIONS)
            rec = _load_records()
            bp = tr.get("bp", 0)
            pool = tr.get("pool", ())
            lvl, into, span, to_next = level_progress(bp)
            self._set_name(tr.get("name") or DEFAULT_TRAINER_NAME)
            self._title = "TRAINER  lvl: %d" % lvl
            self._bp = _thousands(bp)
            # At 999 there is no next level: span is 0 and the bar reads full.
            # Guarded rather than divided - level_progress says so explicitly.
            #
            # A float, and that does NOT breach plan 4.8's integer-only rule.
            # That rule exists so three runtimes agree on a fight's outcome; this
            # number is a bar width, computed once, never sent anywhere and never
            # compared. app.py's own health bars divide by 100.0 the same way.
            self._frac = (into / span) if span else 1.0
            self._to_next = ("%s bp to %d" % (_thousands(to_next), lvl + 1)
                             if span else "max level")
            # ONE string, so the rim spacing is laid out in one pass, but drawn
            # in three COLOURED slices - green won, red lost, white drew. The cut
            # points are character indices into that string, remembered here
            # because they depend on how many digits each tally has and
            # recomputing them at draw time would be arithmetic per frame.
            #
            # Gaps are three spaces: run together at "W 0 L 0 D 0" the owner read
            # the whole line as a single word, and colour alone would not have
            # fixed that - it is the spacing that makes them three tallies.
            self._record, self._rec_cuts = _record_parts(
                rec.get("w", 0), rec.get("l", 0), rec.get("d", 0))
            # Collected out of what is actually COLLECTIBLE (five trait
            # actions), not out of MAX_POOL's storage headroom - a player told
            # "3 / 12" is being promised nine actions the game does not have.
            # Clamped because a hand-edited pool can hold ids beyond the
            # collectible set, and "6 / 5" reads as a bug rather than a boast.
            # "3/5" not "3 / 5": on a rim every character is arc length, and
            # the spaces bought nothing a slash was not already saying.
            self._pool = "%d/%d" % (min(len(pool), N_COLLECTIBLE),
                                    N_COLLECTIBLE)
            self._rows = True
        except Exception as e:
            print("Trainer: build failed:", e)
            self._set_name(DEFAULT_TRAINER_NAME)
            self._title = "TRAINER  lvl: 1"
            self._bp = "0"
            self._frac = 0.0
            self._to_next = ""
            self._record, self._rec_cuts = _record_parts(0, 0, 0)
            self._pool = "0/%d" % N_COLLECTIBLE
            self._rows = True
        # Arc layouts MEASURE every glyph, so they are built once here with the
        # strings rather than per frame - which is the per-frame measurement the
        # menu rows had taken out of them. Deferred to the first draw because
        # measuring needs a ctx, and __init__ has none.
        self._arcs = None

    def _set_name(self, name):
        """Store the name with a provisional size. The real one comes from
        _fit_name() once there is a ctx to measure with."""
        self._name = name
        self._name_size = _NAME_SIZE

    def _fit_name(self, ctx):
        """Shrink the name until it fits, using the BADGE's font metrics.

        This was estimated at 0.6 of the point size per character, which is the
        same guess arc_text_layout falls back to and is only ever approximately
        right - so a long name still overran on hardware while the desk thought
        it fit. Measuring is the fix: ctx.text_width is what the badge will
        actually draw, and shrink-to-fit off a real width is exactly what
        max_span already does for the curved text.

        Runs with the arc measurement, so once per name rather than per frame.
        """
        size = _NAME_SIZE
        ctx.font_size = size
        try:
            w = ctx.text_width(self._name)
        except Exception:
            # No measurement available: fall back to the estimate rather than
            # drawing at full size and hoping.
            w = len(self._name) * size * 0.6
        if w > _NAME_MAX_W and w > 0:
            size = max(_NAME_MIN_SIZE, int(size * _NAME_MAX_W / w))
        self._name_size = size

    def _subscribe(self):
        if self._input_handler is None:
            self._input_handler = self._handle_input
            eventbus.on_async(ButtonDownEvent, self._input_handler, self.app)

    def _unsubscribe(self):
        if self._input_handler is None:
            return
        try:
            eventbus.remove(ButtonDownEvent, self._input_handler, self.app)
        except Exception as e:
            print("Trainer: handler removal failed:", e)
        self._input_handler = None

    def update(self, delta):
        """The pet keeps simulating behind this screen - app.py owns that. What
        this owns is the rename dialog's two edges.

        WHY THIS SCREEN GOES DEAF WHILE THE DIALOG IS OPEN, rather than checking
        a flag: the eventbus keeps sync handlers and async handlers in separate
        registries. TextDialog registers a SYNC one, so it runs inside emit;
        this screen uses on_async, so its handler is SCHEDULED and arrives later.
        Pressing Done therefore completes the dialog, app.py clears it, and only
        THEN does this screen's handler run - seeing no dialog open, and
        helpfully reopening the rename. The new dialog looks identical but empty,
        which reads exactly as "Done did not work, press it again".

        No amount of checking `app.dialog` fixes that, because by the time the
        check runs the dialog is legitimately gone. Not being subscribed does.

        Both edges are handled HERE and never from the button handler, because
        the thing being mutated is our own subscription and doing that from
        inside our own dispatch is how the bus gets modified mid-iteration.
        """
        dialog_open = getattr(self.app, "dialog", None) is not None
        if self._dialog_open and not dialog_open:
            self._subscribe()               # it closed: start listening again
        if self._rename_pending and not dialog_open:
            self._rename_pending = False
            self._unsubscribe()             # go deaf BEFORE it opens
            self._open_rename()
            dialog_open = getattr(self.app, "dialog", None) is not None
        self._dialog_open = dialog_open

    def close(self):
        self._unsubscribe()

    # --- input -------------------------------------------------------------
    async def _handle_input(self, event):
        """ASYNC, because `eventbus.on_async` takes a coroutine function and
        silently does nothing useful with a plain one - which is exactly how this
        shipped first: F did not back out and there was no other way off the
        screen. battle.py's handler has always been `async def` for the same
        reason; this one only tested its own logic, by calling on_button's code
        directly, so nothing it asserted ever touched the registration.

        MUST NOT raise: the eventbus stops the owning app if a handler throws,
        which presents as the whole badge freezing with nothing on screen to say
        why (app.py:815 says the same about its own handler).
        """
        try:
            self.on_button(event)
        except Exception as e:
            print("Trainer: input error:", e)

    def on_button(self, event):
        # The rename dialog owns every button while it is open. app.py's own
        # handler already steps aside for it, but ours is registered separately
        # on the eventbus and would otherwise still fire - so F would close this
        # screen out from under the dialog the player is typing into.
        if getattr(self.app, "dialog", None) is not None:
            return
        if self._opened_by is not None:
            # The press that opened us is still in flight. Swallow exactly that
            # one event object, then stop checking.
            if event is self._opened_by:
                self._opened_by = None
                return
            self._opened_by = None
        if BUTTON_TYPES["CANCEL"] in event.button:
            self.done = True
        elif BUTTON_TYPES["CONFIRM"] in event.button:
            # FLAGGED, not opened. See update() - a dialog built inside this
            # dispatch receives the press that built it.
            self._rename_pending = True

    def _open_rename(self):
        """C: name the trainer. The name is the only thing on this screen the
        player owns rather than earns, so it is the only thing C can do here.

        Called from update(), never from the button handler.
        """
        def apply(text):
            name = clean_trainer_name(text)
            if name:
                # Re-read rather than writing back a dict this screen has been
                # holding: a battle could have banked points into the same file
                # since this screen opened, and writing a stale copy would undo
                # them (plan 14.4 - one writer at a time, read-modify-write).
                tr = load_trainer(N_ACTIONS)
                tr["name"] = name
                save_trainer(tr)
            # Rebuild either way: on cancel this is not called at all, so
            # reaching here means the player confirmed and expects to see it.
            self._build()

        try:
            self.app._open_text_dialog("Trainer name:", apply)
        except Exception as e:
            print("Trainer: rename unavailable:", e)

    # --- drawing -----------------------------------------------------------
    def _measure(self, ctx):
        """Lay out the three curved strings. Once, not per frame - each of these
        measures every glyph it contains."""
        self._bp_text = "BP: " + self._bp
        self._ba_text = "BA: " + self._pool
        self._fit_name(ctx)
        self._arcs = (
            arc_text_layout(ctx, self._title, _TITLE_R, _ARC_MID, _TITLE_SIZE,
                            max_span=_ARC_MAX_SPAN),
            arc_text_layout(ctx, self._record, _REC_R, _REC_MID, _REC_SIZE,
                            max_span=_REC_MAX_SPAN, flip=True),
            # BP is on the TOP half now, so it is not flipped and it shares the
            # title's centre angle rather than the record's.
            arc_text_layout(ctx, self._bp_text, _BP_R, _ARC_MID, _BP_SIZE,
                            max_span=_BP_MAX_SPAN),
            arc_text_layout(ctx, self._ba_text, _BA_R, _REC_MID, _STAT_SIZE,
                            max_span=_STAT_MAX_SPAN, flip=True),
        )

    def draw(self, ctx):
        clear_background(ctx)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        if self._arcs is None:
            self._measure(ctx)
        title, record, bp, ba = self._arcs

        # The ring first, so nothing else has to draw around it.
        ctx.line_width = _RING_W
        ctx.rgb(*_RING_RGB)
        ctx.begin_path()
        ctx.arc(0, 0, _RING_R, 0, 2 * math.pi, False)
        ctx.stroke()

        # Identity on the upper-right rim: who you are and what level, one line.
        ctx.rgb(*_TITLE_RGB)
        ctx.font_size = _TITLE_SIZE
        draw_arc_text(ctx, title)

        # The XP bar, between the two. Track then fill, so a part-filled arc
        # reads against something rather than floating.
        a0 = _ARC_MID - _PROG_SPAN / 2 - math.pi / 2
        ctx.line_width = _PROG_W
        ctx.rgb(*_PROG_BG_RGB)
        ctx.begin_path()
        ctx.arc(0, 0, _PROG_R, a0, a0 + _PROG_SPAN, False)
        ctx.stroke()
        filled = _PROG_SPAN * max(0.0, min(1.0, self._frac))
        if filled > 0:
            ctx.rgb(*_PROG_FG_RGB)
            ctx.begin_path()
            ctx.arc(0, 0, _PROG_R, a0, a0 + filled, False)
            ctx.stroke()

        # The ranked record, outermost on the lower-left rim, right way up, in
        # three coloured runs of one layout - green won, red lost, white drew.
        a, b = self._rec_cuts
        ctx.font_size = _REC_SIZE
        _draw_segments(ctx, record, ((a, _W_RGB), (b, _L_RGB),
                                     (len(record), _D_RGB)))

        # BA sits directly above it, still on the lower rim. BP has moved to the
        # top half, under the XP bar.
        ctx.font_size = _STAT_SIZE
        _draw_segments(ctx, ba, _label_value(_BA_RGB, len(ba)))
        ctx.font_size = _BP_SIZE
        _draw_segments(ctx, bp, _label_value(_BP_RGB, len(bp)))

        # The name, tilted across the empty middle.
        ctx.save()
        ctx.translate(0, _NAME_Y)
        ctx.rotate(_NAME_TILT)
        ctx.font_size = self._name_size
        ctx.rgb(*_NAME_RGB)
        ctx.move_to(0, 0).text(self._name)
        ctx.restore()

        # C renames, F backs out. No joystick glyph: there is nothing to scroll.
        draw_hints(ctx, c="C rename", f="F back", joy=False)
