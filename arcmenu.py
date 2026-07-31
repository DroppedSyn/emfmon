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
# Pushed out to r~100 so each sits close to the button it names. They used to
# be pulled in because they were WORDS, and words at the rim read as clutter
# competing with the menu - a small ringed letter does not, so it can go where
# it actually belongs. Still clears the rows (|y| <= 56) and the bezel: centre
# at 100 plus ring 10 plus stroke leaves ~9px of margin.
HINT_F_XY = (-54, -84)      # upper-left, by F
HINT_C_XY = (54, 84)        # lower-right, by C
HINT_RGB = (1.0, 0.83, 0.15)
HINT_SIZE = 12
# Pulled in from the bezel and sized down: out at the rim these read as clutter
# competing with the menu itself, rather than as a quiet guide. They still have
# to clear the menu's rows (|y| <= 56) and stay inside the edge.

# Button call-outs are ROUND BADGES carrying the button's own letter, not
# labelled text. C is select and F is back EVERYWHERE in this app and on the
# badge itself, so the verb was never the information - which button was. A
# letter in a coloured disc says that faster than a word, survives being small,
# and reads at a glance instead of asking to be parsed.
#
# Green/red rather than the old uniform amber: go and back are opposites, and
# colour carries that without a word. Matches the result banner's palette so
# green means the same thing everywhere in the app.
HINT_C_RGB = (0.20, 0.80, 0.35)     # select / confirm
HINT_F_RGB = (0.90, 0.28, 0.25)     # back / cancel
HINT_R = 10                         # ring radius
HINT_RING_W = 2
HINT_LETTER_SIZE = 12


# --- glyphs the badge font actually has ------------------------------------
# EMFCampFont.h's index carries an arrow and triangle block, so these are real
# glyphs: they take the current colour, scale with font_size, and centre with
# the string they sit in. A drawn path does none of that without being told
# each time, and drifts from the text beside it the moment a size changes.
#
# The full block, for whoever needs one that is not here:
#   arrows      \u2190 \u2191 \u2192 \u2193 \u2194 \u2195 \u2196 \u2197 \u2198 \u2199 \u21e9
#   triangles   \u25b2 \u25b3 \u25b6 \u25bc \u25c0
#   shapes      \u25a1 \u25c7 \u25cb \u2b21 \u2b22 \u2b23 \u2715 \u2663 \u23ce
TRI_LEFT = "\u25c0"
TRI_RIGHT = "\u25b6"
TRI_UP = "\u25b2"
TRI_DOWN = "\u25bc"
TRI_UP_HOLLOW = "\u25b3"

# Default row sizes. Named because callers now state them: the menu is one
# shared object, so a screen that wants its own size must set it, and every
# other screen must set it back rather than inherit whatever ran last.
FONT_ROW = 15
FONT_SEL = 31

JOY_XY = (0, 92)            # bottom centre, in line with the call-outs above
JOY_R = 5                   # ring radius
JOY_GAP = 2                 # ring -> arrow base
JOY_ARROW = 4               # arrow length
JOY_ARROW_W = 3             # arrow half-width at the base
# Unit vectors N/E/S/W. A module constant, so iterating it allocates nothing -
# building this tuple inside the draw would be 5 objects every frame.
_JOY_DIRS = ((0, -1), (1, 0), (0, 1), (-1, 0))


def draw_button_badge(ctx, x, y, letter, rgb, r=HINT_R):
    """The button's letter inside a ring, both in the button's colour.

    A ring rather than a filled disc: filled reads as a solid blob at this size
    and fights the menu for attention, where an outline stays a quiet guide -
    which is what a call-out should be. The letter takes the ring's colour, so
    green and red carry "go" and "back" with no second colour to reason about.

    Pure ctx primitives, no allocation - safe to call every frame.
    """
    ctx.rgb(*rgb)
    ctx.line_width = HINT_RING_W
    ctx.begin_path()
    ctx.arc(x, y, r, 0, 2 * math.pi, False)
    ctx.stroke()
    ctx.text_align = ctx.CENTER
    ctx.text_baseline = ctx.MIDDLE
    ctx.font_size = HINT_LETTER_SIZE
    # nudged down a touch: MIDDLE sits a hair high inside a ring
    ctx.move_to(x, y + 1).text(letter)


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


def draw_hints(ctx, c=None, f=None, rgb=None, size=HINT_SIZE, joy=True):
    """Draw button call-outs at their buttons. Shared by ArcMenu and by screens
    that roll their own drawing, so every call-out in the app lands in the same
    place, in the same colour, at the same size.

    `c` and `f` are truthy-to-show, and their TEXT is deliberately ignored: the
    call-outs are discs carrying the button letter (see draw_button_badge).
    Callers still pass "C pick", "C fight", "C lock in" and so on, and those
    strings stay useful as documentation of what the screen does - but C is
    select and F is back everywhere, so the verb was never what the player
    needed from a call-out.

    `rgb` overrides both colours if a screen really needs to; leave it None for
    the standard green/red. `joy` draws the joystick-click glyph alongside C,
    since the stick selects too - set False to suppress it.
    """
    if not c and not f:
        return
    c_rgb = rgb or HINT_C_RGB
    f_rgb = rgb or HINT_F_RGB
    if c and joy:
        draw_joystick_icon(ctx, rgb=c_rgb)
    if f:
        draw_button_badge(ctx, HINT_F_XY[0], HINT_F_XY[1], "F", f_rgb)
    if c:
        draw_button_badge(ctx, HINT_C_XY[0], HINT_C_XY[1], "C", c_rgb)


def _advances(ctx, text, size):
    """Per-glyph advances, measured as CUMULATIVE PREFIXES.

    `text_width(ch)` on its own is what that character occupies, which is not
    what the string spends on it: the font's advance includes side bearings and
    may kern against its neighbour. Summing single characters therefore packs
    some pairs too tightly and not others - the play screen's title put its l
    against the P at 20pt while the rest of the word sat correctly.

    Measuring prefixes gives each glyph the width the string ACTUALLY grows by
    when that glyph is added, which is the advance in context, kerning included.
    It also sums exactly to the whole string's width by construction.

    Two earlier attempts are worth recording, because both were wrong in
    instructive ways: rescaling single-character widths to the string's total
    assumed the deficiency was UNIFORM, and it is not - it is per pair. Adding a
    flat gap to every glyph then fixed P-l and over-spaced everything else, for
    the same reason.
    """
    n = len(text)
    if not n:
        return [], 0.0
    try:
        cum = [ctx.text_width(text[:i + 1]) for i in range(n)]
        widths = [cum[0]] + [cum[i] - cum[i - 1] for i in range(1, n)]
        # A prefix that does not grow means the measurement is not usable for
        # this glyph; fall back for that one rather than stacking two on a point.
        widths = [w if w > 0.0 else size * 0.6 for w in widths]
    except Exception:                       # ctx can't measure - even spacing
        widths = [size * 0.6] * n
    total = 0.0
    for w in widths:
        total += w
    return widths, total


def arc_text_layout(ctx, text, radius, centre_rad, size, max_span=None,
                    min_size=8, flip=False):
    """Lay `text` along a circle so it curls with the bezel.

    `centre_rad` is measured from 12 o'clock, positive clockwise, and the string
    is centred on it. Returns a list of (char, x, y, rotation) - each glyph's
    point on the circle plus the angle that makes it tangent to it.

    `flip` is for the BOTTOM of the circle. Tangent rotation is what makes text
    curl correctly along the upper rim, and it is exactly what turns it upside
    down along the lower one - a string at 6 o'clock reads correctly only to
    someone holding the badge the other way up. With flip, glyphs are rotated
    another half turn and laid out counter-clockwise, so the string still reads
    left to right to someone holding the badge normally. Default off, so every
    existing upper-rim caller is untouched.

    `max_span` (radians) caps how much of the circle the string may occupy: if
    it would run wider, the point size is scaled down to fit, never below
    `min_size`. Needed wherever a variable string shares the rim with fixed
    labels - a long name must not grow into them.

    This MEASURES every glyph, so call it once and keep the result. Titles don't
    change, and doing this per frame would be exactly the per-frame measurement
    we took out of the menu rows.
    """
    ctx.font_size = size
    widths, total = _advances(ctx, text, size)
    if max_span and total / radius > max_span:
        size = max(min_size, int(size * max_span * radius / total))
        ctx.font_size = size
        widths, total = _advances(ctx, text, size)
    out = []
    if flip:
        # Start at the far (clockwise) end and walk back, so the string still
        # runs left-to-right for the reader once each glyph is turned over.
        a = centre_rad + (total / radius) * 0.5
        for i in range(len(text)):
            w = widths[i]
            th = a - (w * 0.5) / radius
            out.append((text[i], radius * math.sin(th), -radius * math.cos(th),
                        th + math.pi))
            a -= w / radius
        return out
    a = centre_rad - (total / radius) * 0.5   # arc length / radius = radians
    for i in range(len(text)):
        w = widths[i]
        th = a + (w * 0.5) / radius           # glyph sits at its own midpoint
        out.append((text[i], radius * math.sin(th), -radius * math.cos(th), th))
        a += w / radius
    return out


def draw_arc_text(ctx, layout):
    """Draw a layout from arc_text_layout. Assumes CENTER/MIDDLE alignment, so
    each glyph lands centred on its point and rotated tangent to the circle."""
    for ch, x, y, rot in layout:
        ctx.save()
        ctx.translate(x, y)
        ctx.rotate(rot)
        ctx.move_to(0, 0).text(ch)
        ctx.restore()


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
                 scrim_edge=0.45, row_pad=34.0, settle=600,
                 font=FONT_ROW, font_sel=FONT_SEL, font_hint=HINT_SIZE,
                 debounce=160,
                 sel_rgb=(0.35, 0.75, 1.0), row_rgb=(0.80, 0.80, 0.82),
                 hint_rgb=None,
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
        settle  ms after set_items during which presses are ignored, so a
                stray event from the press that OPENED the menu cannot move or
                select anything. 250 ms was not enough - measured strays at up
                to 400 ms.
        row_pad how much of the chord a row may NOT use, in px. Rows are
                left-aligned at one edge and were allowed the full width of the
                circle at their height - so the selected row at font_sel could
                run clear across to the far bezel, straight into a curved title
                drawn there (battle.py's BATTLE MODE tail sits at about x=99,
                y=-3, level with the selected row). Reserving a strip stops
                that; _fit shrinks the point size to suit rather than letting
                text overflow.
        scrim_edge  EXTRA alpha at the side the rows sit on, fading to plain
                `scrim` at the far side. 0 restores the old flat backdrop.
                It needs to be BIG to read: the pet screen is already almost
                black, so painting more black over black changes nothing except
                where there is actually something to obscure - the pet, the
                poops, the bars, and the button call-outs. At 0.45 the row side
                reaches full opacity, which is what stops the pet screen's
                "Menu" call-out (app.py:1315, x=-94) showing through from
                underneath - a stale label peeking out from under a menu reads
                as a rendering bug rather than as transparency.
        font/font_sel/font_hint  point sizes for unselected rows, the selected
                row, and the footer
        sel_rgb   colour of the selected row (a light shining blue by default)
        row_rgb   colour of the unselected rows
        hint_rgb  OVERRIDE for both call-out discs. Leave None for the
                standard green C / red F, which is what carries "go" and
                "back" without a word. Setting it makes both the same colour
                and throws that away - only do it if a screen genuinely needs
                to
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
        # The look and the guards first, because `configure()` at the end of
        # this block calls set_items(), which arms them.
        #
        # `idx` must exist BEFORE that: set_items clamps the CURRENT selection
        # against the new list, so it reads self.idx. configure() sets the real
        # value straight after. Found by the suite the moment the constructor
        # started delegating - it is the one ordering the delegation introduces.
        self.idx = 0
        self.radius = radius
        self.row = row
        self.span = span
        self.scrim = scrim
        self.scrim_edge = scrim_edge
        self.row_pad = row_pad
        self.settle = settle
        self._set_ms = ticks_ms()
        self._grad_ok = None       # None = untried, False = fall back to flat
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

        # THE SAME BLOCK configure() owns - items, the clamp, side and the two
        # hints - used to be written out here as well. Two copies of a
        # non-obvious clamp is exactly what review plan 1.1 is about, and this
        # one had already outlived its own comment elsewhere. One place now.
        self.configure(items, idx=idx, side=side, hint_c=hint_c,
                       hint_f=hint_f, font=font, font_sel=font_sel)
    def configure(self, items, idx=0, side="right", hint_c=None, hint_f=None,
                  font=FONT_ROW, font_sel=FONT_SEL):
        """Load a screen's whole menu state in ONE call. Use this, not the
        fields, from any screen.

        THE FIVE CALL SITES THIS REPLACES HAD ALREADY DRIFTED, which is review
        plan 1.1's whole thesis: `m.font, m.font_sel = ...; set_items(...);
        m.idx = min(max(0, i), max(0, len(m.items) - 1)); m.side/hint_c/hint_f`
        was written out longhand in app.py, battle.py (x3) and queues.py, and
        app.py had ended up setting the fonts AFTER set_items while its own
        comment said "Before set_items". The clamp - which is not obvious, and
        is wrong in two different ways if you get either bound backwards - was
        copied verbatim five times.

        ORDER MATTERS and is fixed here so no caller has to know it. Fonts
        first: `set_items` clears the row measurement, so the sizes it will be
        remeasured at must already be in place. Then the items, then the
        selection clamped against the list that actually landed.

        This is deliberately NOT the full "set every field it governs" that
        plan 2.1 asks for. It covers the five a screen already sets, which is
        behaviour-preserving; the other 23 are a look, and changing those needs
        its own session with eyes on hardware (review plan 2.4).
        """
        self.font, self.font_sel = font, font_sel
        self.set_items(items)
        self.idx = min(max(0, idx), max(0, len(self.items) - 1))
        self.side = side
        self.hint_c = hint_c
        self.hint_f = hint_f
        return self

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
        self._set_ms = self._last_ms
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
        # SETTLE WINDOW. set_items marks the menu freshly opened, and anything
        # arriving in the moment after that is left over from opening it, not
        # aimed at it. Observed on hardware: selecting a row with the joystick
        # emits a stray UP 250-400 ms later, which wrapped a brand-new menu
        # from row 0 straight to its LAST row - so entering Battle landed on
        # Records and entering Action queues landed on "+ new queue".
        #
        # `_guard_any` already existed for exactly this ("the press that opened
        # us must not select"), but it was only ever consulted INSIDE the
        # debounce window below. A press 375 ms later skips that block entirely,
        # so the guard was never reached. It needs its own window, and one long
        # enough to outlast the stray event rather than the 160 ms bounce.
        if self._guard_any:
            if ticks_diff(now, self._set_ms) < self.settle:
                # DIRECTIONAL ONLY. The stray is an UP, so guarding UP and DOWN
                # catches it - and guarding anything else was the second bug
                # this window caused.
                #
                # It used to swallow EVERY press for the full 600 ms, which made
                # a fresh menu completely deaf: a player pressing every 180 ms
                # lost three of four, which is the "item use needs several
                # presses" report, and CANCEL was eaten too so there was no way
                # back out either (review plan 2.2, 4).
                #
                # The two are not symmetric, which is what settles the trade. A
                # swallowed CONFIRM costs a press. An unswallowed stray UP wraps
                # a brand-new menu from row 0 to its LAST row and the next
                # CONFIRM then picks something the player never chose - on the
                # main menu that row is `New pet`. A lost press is an
                # irritation; a wrong selection is destructive.
                #
                # The residue, stated so it is not rediscovered as a new bug: a
                # DELIBERATE scroll inside the first 600 ms is still lost. That
                # is accepted - it cannot be told apart from the stray, and the
                # stray is the dangerous one.
                if BUTTON_TYPES["UP"] in b or BUTTON_TYPES["DOWN"] in b:
                    return None
            self._guard_any = False
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
            # Reserve row_pad at the far end so a long row cannot reach a
            # title curled round the opposite rim.
            avail = 2 * self._halves[off + self.span] - self.row_pad
            if avail < 40.0:            # never squeeze a row to nothing
                avail = 40.0
            sizes.append(self._fit(ctx, self.items[i], base, avail))
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
            self._draw_scrim(ctx)
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

    def _draw_scrim(self, ctx):
        """The backdrop, deepest on the side the rows sit on.

        Rows hug one side, so that side is where text needs contrast and the
        other is where you still want to see the pet. A flat scrim compromises
        between the two; a gradient does not.

        ONE gradient-filled circle - `ctx` has linear_gradient/add_stop, so
        there is no reason to fake a ramp with banded rectangles. The earlier
        version did exactly that, and it cost twelve fills and showed visible
        seams between the bands. This is a single fill: cheaper than the flat
        scrim it replaces was, and smooth by construction.

        Falls back to the flat scrim if the gradient calls are unavailable, and
        remembers that decision rather than retrying every frame.
        """
        r = self.radius + 2
        if self.scrim_edge > 0 and self._grad_ok is not False:
            near = self.scrim + self.scrim_edge
            if near > 1.0:
                near = 1.0
            left = self.side == "left"
            try:
                ctx.linear_gradient(-r if left else r, 0, r if left else -r, 0)
                ctx.add_stop(0.0, [0, 0, 0], near)
                ctx.add_stop(1.0, [0, 0, 0], self.scrim)
                ctx.arc(0, 0, r, 0, 2 * math.pi, False).fill()
                self._grad_ok = True
                return
            except Exception as e:
                if self._grad_ok is None:
                    print("ArcMenu: no gradient support, using flat scrim:", e)
                self._grad_ok = False
        ctx.rgba(0.0, 0.0, 0.0, self.scrim).arc(
            0, 0, r, 0, 2 * math.pi, False).fill()

    def draw_hint(self, ctx):
        """The button call-outs, each at its own button. Kept separate so a
        caller can guarantee they land last - see draw(hint=False)."""
        draw_hints(ctx, self.hint_c, self.hint_f,
                   self.hint_rgb, self.font_hint)
