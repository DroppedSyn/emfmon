"""Curved overlay menu for the badge's round screen.

A conventional vertical list wastes a circular display and hides everything
behind an opaque panel. ArcMenu instead right-aligns each row to the screen's
half-width AT THAT ROW'S HEIGHT, so the list curls around the bezel, and paints
only a translucent scrim - whatever the screen was already drawing (the mon, a
battle, ...) stays visible behind it.

Screen-agnostic and reusable: hand it a list of label strings, feed it button
events, and draw it last so it lands on top.

    self.menu = ArcMenu(["Small Heal x5", "Heal x22"], hint_c="C use")
    ...
    act = self.menu.button(event)          # None | "select" | "back"
    if act == "select":
        use(self.menu.idx)                 # or self.menu.selected
    elif act == "back":
        self.menu = None
    ...
    if self.menu is not None:
        self.menu.draw(ctx)                # after everything else

The owner decides what "select"/"back" mean and when the menu exists - ArcMenu
holds no lifecycle of its own, which is what makes it droppable into any screen.
"""
import math

from events.input import BUTTON_TYPES
from events.joystick import JOYSTICK_BUTTON_TYPES

try:                                    # MicroPython
    from time import ticks_diff, ticks_ms
except ImportError:                     # CPython (desktop tests)
    from time import monotonic as _monotonic

    def ticks_ms():
        return int(_monotonic() * 1000)

    def ticks_diff(a, b):
        return a - b


# Button call-outs sit by the button that performs them. On the 2026 frontboard
# the hexagon runs A top, B upper-right, C lower-right, D bottom, E lower-left,
# F upper-left - so "back" belongs top-left and "select" bottom-right.
#
# They also have to clear the menu's own rows, which occupy y in [-56, 56], and
# the circle narrows fast at the extremes: at y=+-84 only about +-83px of width
# is left. Hence these anchors - far enough out to miss the rows, far enough in
# to miss the bezel.
HINT_F_XY = (-52, -82)      # upper-left, by F
HINT_C_XY = (52, 82)        # lower-right, by C
HINT_RGB = (1.0, 0.83, 0.15)
HINT_SIZE = 15
# Keep call-out labels to ~7 characters: at these anchors the circle
# leaves about +-85px of width, and a longer one ("C challenge") runs
# off the bezel however you place it.
HINT_MAX_CHARS = 7


JOY_XY = (0, 100)           # bottom centre, clear of both call-outs
JOY_R = 5                   # ring radius
JOY_GAP = 2                 # ring -> arrow base
JOY_ARROW = 4               # arrow length
JOY_ARROW_W = 3             # arrow half-width at the base
JOY_EXTENT = JOY_R + JOY_GAP + JOY_ARROW    # furthest the glyph reaches
# Unit vectors N/E/S/W. A module constant, so iterating it allocates nothing -
# building this tuple inside the draw would be 5 objects every frame.
_JOY_DIRS = ((0, -1), (1, 0), (0, 1), (-1, 0))


def draw_joystick_icon(ctx, x=JOY_XY[0], y=JOY_XY[1], r=JOY_R, rgb=HINT_RGB):
    """Click-the-stick glyph: ring, centre dot, and four cardinal arrows.

    Says both things the stick does - press to select, push to scroll. Pure ctx
    primitives with no allocation and no state, so it is safe every frame.
    """
    ctx.rgb(*rgb)
    ctx.line_width = 2
    ctx.begin_path()
    ctx.arc(x, y, r, 0, 2 * math.pi, False)
    ctx.stroke()
    ctx.begin_path()
    ctx.arc(x, y, r * 0.5, 0, 2 * math.pi, False)
    ctx.fill()
    base = r + JOY_GAP
    tip = base + JOY_ARROW
    for dx, dy in _JOY_DIRS:
        # perpendicular is (-dy, dx): gives the two base corners either side
        bx, by = x + dx * base, y + dy * base
        ctx.begin_path()
        ctx.move_to(x + dx * tip, y + dy * tip)
        ctx.line_to(bx - dy * JOY_ARROW_W, by + dx * JOY_ARROW_W)
        ctx.line_to(bx + dy * JOY_ARROW_W, by - dx * JOY_ARROW_W)
        ctx.close_path()
        ctx.fill()


def draw_hints(ctx, c=None, f=None, rgb=HINT_RGB, size=HINT_SIZE, joy=True):
    """Draw button call-outs at their buttons. Shared by ArcMenu and by screens
    that roll their own drawing, so every call-out in the app lands in the same
    place, in the same colour, at the same size.

    Pass None to omit either. Labels are static per screen, so nothing is built
    per frame here. `joy` draws the joystick-click glyph alongside the C
    call-out, since the stick selects too - set False to suppress it.
    """
    if not c and not f:
        return
    if c and joy:
        draw_joystick_icon(ctx, rgb=rgb)
    ctx.text_align = ctx.CENTER
    ctx.text_baseline = ctx.MIDDLE
    ctx.font_size = size
    ctx.rgb(*rgb)
    if f:
        ctx.move_to(HINT_F_XY[0], HINT_F_XY[1]).text(f)
    if c:
        ctx.move_to(HINT_C_XY[0], HINT_C_XY[1]).text(c)


def pulse_k(period_ms=1600, lo=0.68):
    """Brightness multiplier in [lo, 1.0] that breathes on the wall clock.

    Shared by ArcMenu's selected row and any screen that wants the same
    heartbeat, so there is one implementation of the timing. Derived from
    ticks_ms() rather than accumulated deltas, so it cannot drift or stutter
    when a frame runs long - and it needs no update(), no stored state and
    allocates nothing per frame.
    """
    return lo + (1.0 - lo) * (0.5 + 0.5 * math.sin(
        (ticks_ms() % period_ms) * (2 * math.pi / period_ms)))


class ArcMenu:
    def __init__(self, items=None, idx=0, hint_c="C pick", hint_f="F back",
                 side="right", radius=118.0, row=28, span=2, scrim=0.55,
                 font=15, font_sel=31, font_hint=HINT_SIZE, debounce=160,
                 sel_rgb=(0.35, 0.75, 1.0), row_rgb=(0.62, 0.62, 0.62),
                 hint_rgb=HINT_RGB,
                 pulse=True, pulse_ms=1600, font_min=10):
        """items  list of label strings (already formatted - counts, ticks, ...)
        idx     initially selected row
        hint_c/hint_f  button call-outs, drawn at their own buttons
                (C lower-right, F upper-left). None to omit either.
        side    "left" or "right" edge to hug. Open on the SAME side as the
                button that opened the menu - a list that flies out from under
                your thumb reads as connected to it, one that appears on the
                far side reads as unrelated.
        radius  screen radius the rows hug (a little inside the real edge)
        row     vertical pitch between rows
        span    rows drawn either side of the selection
        scrim   backdrop alpha; 0 for none, 1 for opaque
        font/font_sel/font_hint  point sizes for unselected rows, the selected
                row, and the footer
        sel_rgb   colour of the selected row (a light shining blue by default)
        row_rgb   colour of the unselected rows
        hint_rgb  colour of the footer - the button call-outs, kept loud
        pulse     breathe the selected row's brightness. Driven straight off the
                wall clock inside draw(), so there is no update() to call, no
                state to keep and nothing allocated per frame.
        pulse_ms  one full bright->dim->bright cycle
        font_min  a row too wide for the circle at its height is shrunk to fit
                rather than clipped ("Suspicious Brick x1" lost its S at 31pt);
                this is the floor that shrinking will not go below
        debounce  ms during which a second press is swallowed. The joystick
                likes to fire twice in quick succession, which on a menu means a
                double-scroll or - worse - selecting a row you never saw. Also
                armed by set_items(), so the very press that opens a menu can't
                fall straight through and pick row 0.
        """
        self.items = list(items or ())
        self.idx = min(max(0, idx), max(0, len(self.items) - 1))
        self.hint_c = hint_c
        self.hint_f = hint_f
        self.side = side
        self.radius = radius
        self.row = row
        self.span = span
        self.scrim = scrim
        self.font = font
        self.font_sel = font_sel
        self.font_hint = font_hint
        self.sel_rgb = sel_rgb
        self.row_rgb = row_rgb
        self.hint_rgb = hint_rgb
        self.pulse = pulse
        self.pulse_ms = pulse_ms
        self.font_min = font_min
        self.debounce = debounce
        self._last_ms = ticks_ms()
        self._last_btn = None      # which button that was (see button())
        self._guard_any = True     # swallow the press that opened us, whatever
        # Fitted point size per visible row, recomputed only when the contents
        # or the selection change. Measuring every row every frame meant ~600
        # text_width() calls a second for a picture that never changed.
        self._row_sizes = None
        # Half-chord per visible row. Depends only on radius/row/span, so it is
        # fixed for the menu's lifetime - computing it in draw() was 5 sqrt()
        # per frame for numbers that never move.
        self._halves = None

    # --- content -----------------------------------------------------------
    def set_items(self, items):
        """Replace the labels, keeping the selection in range - call after an
        action changes what's on offer (an item spent, a peer gone).

        NEVER call this from draw(): it allocates a list, and doing that every
        frame is what fragments this badge's heap. Once per transition only.
        """
        self.items = list(items or ())
        self.idx = min(self.idx, max(0, len(self.items) - 1))
        self._row_sizes = None       # contents changed - remeasure
        self._last_ms = ticks_ms()   # the press that opened us must not select
        self._last_btn = None
        self._guard_any = True

    @property
    def selected(self):
        if not self.items:
            return None
        return self.items[self.idx]

    # --- input -------------------------------------------------------------
    def button(self, event):
        """UP/DOWN scroll (wrapping), C or the joystick centre selects, F backs
        out. Returns "select", "back", or None if the press did neither.

        Debouncing is PER BUTTON: a repeat of the same button inside the window
        is dropped (that's the joystick's double-fire), but a different button
        is always let through. Debouncing every button against every other made
        the natural scroll-then-select feel like it needed two goes at it.

        The exception is the press that opened the menu, which set_items() marks
        to be swallowed whatever it was - that one is a stale in-flight event
        rather than a bounce.
        """
        now = ticks_ms()
        b = getattr(event, "button", None)
        if b is None:
            return None     # not a button event we can read - ignore it
        if ticks_diff(now, self._last_ms) < self.debounce:
            if self._guard_any:
                return None
            same = False
            if b is not None and self._last_btn is not None:
                try:
                    same = b == self._last_btn
                except Exception:      # not comparable - fall back to identity
                    same = b is self._last_btn
            if same:
                return None
        self._guard_any = False
        self._last_ms = now
        self._last_btn = b
        if BUTTON_TYPES["CANCEL"] in b or not self.items:
            return "back"
        if BUTTON_TYPES["UP"] in b:
            self.idx = (self.idx - 1) % len(self.items)
            self._row_sizes = None       # different rows visible - remeasure
        elif BUTTON_TYPES["DOWN"] in b:
            self.idx = (self.idx + 1) % len(self.items)
            self._row_sizes = None
        elif (BUTTON_TYPES["CONFIRM"] in b
                or JOYSTICK_BUTTON_TYPES["SELECT"] in b):
            return "select"
        return None

    # --- drawing -----------------------------------------------------------
    def _fit(self, ctx, label, size, maxw):
        """Return the largest point size <= `size` at which `label` fits `maxw`.

        Text width scales about linearly with point size, so scaling by the
        overflow ratio lands in one step; we re-measure once and nudge again
        rather than trusting that, and never go below font_min. Called only when
        _row_sizes is stale, never per frame.
        """
        ctx.font_size = size
        try:
            w = ctx.text_width(label)
        except Exception:
            return size     # no measuring available - draw at the asked size
        if w <= maxw or w <= 0:
            return size
        size = max(self.font_min, int(size * maxw / w))
        ctx.font_size = size
        try:
            if ctx.text_width(label) > maxw and size > self.font_min:
                size = max(self.font_min, size - 1)
        except Exception:
            pass
        return size

    def _measure_halves(self):
        """Half-chord (less the edge inset) for each visible row. Geometry only,
        so this is computed once and reused for the menu's lifetime."""
        r2 = self.radius * self.radius
        halves = []
        for off in range(-self.span, self.span + 1):
            y = off * self.row
            halves.append(math.sqrt(max(0.0, r2 - y * y)) - 8)
        self._halves = halves

    def _measure_rows(self, ctx):
        """Fitted size for each visible row, indexed by offset + span."""
        if self._halves is None or len(self._halves) != 2 * self.span + 1:
            self._measure_halves()
        sizes = []
        for off in range(-self.span, self.span + 1):
            i = self.idx + off
            if i < 0 or i >= len(self.items):
                sizes.append(0)
                continue
            base = self.font_sel if off == 0 else self.font
            sizes.append(self._fit(ctx, self.items[i], base,
                                   2 * self._halves[off + self.span]))
        self._row_sizes = sizes

    def draw(self, ctx, hint=True):
        """Draw the menu over whatever is already on screen.

        Pass hint=False when the caller paints decoration of its own afterwards
        (a rim, a title), then call draw_hint() last: the button call-outs must
        end up on top of EVERYTHING, never clipped by something drawn later.
        """
        if not self.items:
            # Nothing to list, but STILL show the call-outs: a screen with no
            # rows and no hint looks hung, and button() reports "back" from here
            # regardless, so there really is a way out - say so.
            if hint:
                self.draw_hint(ctx)
            return
        if self.scrim > 0:
            ctx.rgba(0.0, 0.0, 0.0, self.scrim).arc(
                0, 0, self.radius + 2, 0, 2 * math.pi, False
            ).fill()
        span = self.span
        if self._row_sizes is None or len(self._row_sizes) != 2 * span + 1:
            self._measure_rows(ctx)
        halves = self._halves
        left = self.side == "left"
        ctx.text_align = ctx.LEFT if left else ctx.RIGHT
        ctx.text_baseline = ctx.MIDDLE
        for off in range(-span, span + 1):
            i = self.idx + off
            if i < 0 or i >= len(self.items):
                continue
            # half-chord at this height: further from the middle -> further in,
            # which is what curls the list around the bezel (precomputed)
            half = halves[off + span]
            ctx.font_size = self._row_sizes[off + span]
            if off == 0:
                r, g, b = self.sel_rgb
                if self.pulse:
                    k = pulse_k(self.pulse_ms)
                    r, g, b = r * k, g * k, b * k
                ctx.rgb(r, g, b)
            else:
                ctx.rgb(*self.row_rgb)
            ctx.move_to(-half if left else half, off * self.row).text(
                self.items[i])
        if hint:
            self.draw_hint(ctx)

    def draw_hint(self, ctx):
        """The button call-outs, each at its own button. Kept separate so a
        caller can guarantee they land last - see draw(hint=False)."""
        draw_hints(ctx, self.hint_c, self.hint_f,
                   self.hint_rgb, self.font_hint)
