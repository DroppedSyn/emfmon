"""The fight screen's renderer - split out of battle.py for the WATCHDOG.

Every byte in `battle.py` is read from flash and compiled in ONE frame when the
player opens Battle, and that frame is measured against a 5 s task watchdog
(plan 6.2.2). Opening the battle MENU had crept back to ~2.4 s - against the
2.9 s that once actually rebooted a badge - so the 20 KB of drawing that only a
FIGHT needs no longer sits on that path.

Nothing here runs until a fight starts. `Battle._ensure_fightview()` imports
this module and installs everything below onto the class, so the methods keep
`self` and every call site in battle.py is unchanged.

WHY INSTALL ONTO THE CLASS rather than convert to functions taking a Battle:
the block references 51 distinct `self.*` attributes and calls its own helpers
through `self`. Rewriting all of that would be a large edit to code with no test
coverage. Moving it VERBATIM and re-attaching it is provably behaviour-
preserving, and `blelab/fightdraw_trace.py` checks exactly that - 810 frames and
76,740 ctx calls, byte-identical before and after.

The import is triggered from two places (see battle.py):
  - opportunistically on the SEARCH screen, where a 200 ms hitch is invisible
    and the player is reading a peer list anyway
  - guaranteed before the first fight frame, so the opportunistic one is an
    optimisation and never a correctness requirement
"""

import math

from app_components.tokens import set_color

from .arcmenu import draw_hints
from .battle import (
    ACTIONS,
    ALL_TACKLE,
    CHIP,
    GUARD,
    HEAL,
    LEECH,
    SLOW,
    LOSE_HEALTH,
    WIN_HEALTH,
    _ACTION_FLASH_MS,
    _BAR_GHOST,
    _BAR_R,
    _BAR_T,
    _BAR_TRACK,
    _BRING_GAP,
    _BRING_R,
    _BRING_RGB,
    _BRING_TICK,
    _BRING_W,
    _DICE_MS,
    _GLOW_A,
    _GLOW_OFF,
    _HIT_FLASH_MS,
    _INTRO_MS,
    _MOVE_FADE,
    _MOVE_LOG_N,
    _MOVE_ROW,
    _MOVE_X,
    _MOVE_Y,
    _MY_ARC,
    _MY_NAME_Y,
    _MY_XY,
    _OPP_ARC,
    _OPP_NAME_Y,
    _OPP_XY,
    _PLAYBACK_MS,
    _QCOL_HOT_RGB,
    _QCOL_MID,
    _QCOL_RGB,
    _QCOL_ROW,
    _QCOL_SEL_SIZE,
    _QCOL_SIZE,
    _QCOL_X,
    _RESULT_LOSE_RGB,
    _RESULT_RGB,
    _RESULT_WIN_RGB,
    _RESULT_X,
    _RESULT_Y,
    _VS_LEAN,
    _VS_MS,
    _VS_RESULT_SIZE,
    _VS_RGB,
    _VS_SIZE,
    _bar_colour,
    _clamp01,
    _draw_mon,
    _rgb3,
    queue_len,
)


def _combat_colours(self):
    """(myR,myG,myB, oppR,oppG,oppB), built once per battle. Opponent colour
    arrives over the air, so it is coerced defensively - and caching keeps
    that off the per-frame path."""
    if self._combat_rgb is None:
        self._combat_rgb = (
            _rgb3(self.app.pet.get("colour"))
            + _rgb3((self.opp or {}).get("colour")))
    return self._combat_rgb

_SCRIM_BASE = 0.10        # everywhere, so text lifts off the mons
_SCRIM_EDGE = 0.22        # extra on the left, where the queue column is

def _draw_battle_scrim(self, ctx):
    """One gradient-filled circle, darkest on the left. Same single-fill
    approach as ArcMenu._draw_scrim - a banded fake costs twelve fills and
    shows seams. Falls back to flat and remembers, rather than retrying the
    gradient every frame."""
    r = 120.0
    if self._grad_ok is not False:
        try:
            ctx.linear_gradient(-r, 0, r, 0)
            ctx.add_stop(0.0, [0, 0, 0], self._SCRIM_BASE + self._SCRIM_EDGE)
            ctx.add_stop(1.0, [0, 0, 0], self._SCRIM_BASE)
            ctx.arc(0, 0, r, 0, 2 * math.pi, False).fill()
            self._grad_ok = True
            return
        except Exception as e:
            if self._grad_ok is None:
                print("Battle: no gradient, flat scrim:", e)
            self._grad_ok = False
    ctx.rgba(0.0, 0.0, 0.0, self._SCRIM_BASE).arc(
        0, 0, r, 0, 2 * math.pi, False).fill()

def _push_move(self, my_a, opp_a):
    """Record what just fired. Shifts the fixed log in place - no new list,
    no slicing, so a 60-tick fight allocates nothing here."""
    log = self._move_log
    for i in range(len(log) - 1, 0, -1):
        dst, src = log[i], log[i - 1]
        dst[0], dst[1], dst[2] = src[0], src[1] + 1, src[2]
    # Ours wins the slot when both fire on the same tick: it is the one the
    # player chose, and the opponent's is already shown by its own effect.
    if my_a >= 0:
        log[0][0], log[0][1], log[0][2] = my_a, 0, 1
    else:
        log[0][0], log[0][1], log[0][2] = opp_a, 0, 0

def _draw_move_log(self, ctx):
    """What just happened, on the right: newest at the top, each entry
    sliding outward and fading as the next one lands."""
    ctx.text_align = ctx.CENTER
    ctx.text_baseline = ctx.MIDDLE
    for i in range(_MOVE_LOG_N):
        aid, age, mine = self._move_log[i]
        if aid < 0 or age >= _MOVE_FADE:
            continue
        k = 1.0 - age / float(_MOVE_FADE)
        ctx.font_size = 15 if i == 0 else 12
        if mine:
            r, g, b = _RESULT_RGB["W"]
        else:
            r, g, b = _RESULT_RGB["L"]
        ctx.rgb(r * k, g * k, b * k)
        ctx.move_to(_MOVE_X + (1.0 - k) * 10, _MOVE_Y + i * _MOVE_ROW)
        ctx.text(ACTIONS[aid][0])

def _draw_queue_column(self, ctx):
    """Your action queue down the left, the slot about to fire lit.

    The queue is the one decision the player actually made, and until now it
    was invisible the moment the fight started - so a rotation they built
    played out as a sequence they could not follow.
    """
    q = self._my_queue or ALL_TACKLE
    n = queue_len(q)
    if n <= 0:
        return
    cur = self._my_slot % n if self._my_slot >= 0 else -1
    ctx.text_align = ctx.CENTER
    ctx.text_baseline = ctx.MIDDLE
    top = _QCOL_MID - ((n - 1) * _QCOL_ROW) / 2.0
    for i in range(n):
        y = top + i * _QCOL_ROW
        if i == cur:
            # Size and colour only. It had a pulse and a glow to match the
            # result words, and that cost five text draws per frame at
            # 19pt - fine on the result screen, which is static, but this
            # one redraws beside the mons, the bars, the effects and the
            # move log every frame of the fight, and it dropped the frame
            # rate visibly. The size gap was doing the work anyway.
            ctx.font_size = _QCOL_SEL_SIZE
            ctx.rgb(*_QCOL_HOT_RGB)
            ctx.move_to(_QCOL_X, y).text(ACTIONS[q[i]][0])
        else:
            ctx.font_size = _QCOL_SIZE
            ctx.rgb(*_QCOL_RGB)
            ctx.move_to(_QCOL_X, y).text(ACTIONS[q[i]][0])

def _draw_broken_ring(self, ctx, rgb, width=_BRING_W, radius=_BRING_R):
    """A rim broken at top and bottom centre, each end turning inward.

    Two arcs and four short lines, no allocation, safe per frame. Colour and
    weight come from the caller so BATTLE MODE and the fight screen share
    one construction rather than one look - they are the same frame around
    two halves of the same thing, and should stay that way when it changes.
    """
    ctx.line_width = width
    ctx.rgb(*rgb)
    half = math.pi / 2.0
    for sign in (1.0, -1.0):
        # right half runs 12->6 clockwise, left half 6->12; the gap sits
        # symmetrically about each pole so the break reads as deliberate
        a0 = -half + _BRING_GAP if sign > 0 else half + _BRING_GAP
        a1 = half - _BRING_GAP if sign > 0 else 3 * half - _BRING_GAP
        ctx.begin_path()
        ctx.arc(0, 0, radius, a0, a1, False)
        ctx.stroke()
        for a in (a0, a1):
            ca, sa = math.cos(a), math.sin(a)
            ctx.begin_path()
            ctx.move_to(ca * radius, sa * radius)
            ctx.line_to(ca * (radius - _BRING_TICK),
                        sa * (radius - _BRING_TICK))
            ctx.stroke()

def _draw_battle(self, ctx):
    pet = self.app.pet
    opp = self.opp or {}
    mx, my = _MY_XY
    ox, oy = _OPP_XY
    mr, mg, mb, orr, og, ob = self._combat_colours()
    # Backdrop first, deepest on the left where the queue column sits -
    # the ArcMenu's gradient, toned well down: this one sits under a fight
    # rather than under a list, so it only has to lift text off the mons.
    self._draw_battle_scrim(ctx)
    self._draw_broken_ring(ctx, _BRING_RGB)
    intro = _clamp01(self.anim_t / _INTRO_MS)
    my_dead = self.state == "result" and not self.i_won
    opp_dead = self.state == "result" and self.i_won
    mxx = mx - (1.0 - intro) * 60
    oxx = ox + (1.0 - intro) * 60
    _draw_mon(ctx, mxx, my, 20, pet.get("shape", "circle"),
              pet.get("colour", [0.6, 0.6, 0.6]), fainted=my_dead)
    _draw_mon(ctx, oxx, oy, 20, opp.get("shape", "circle"),
              opp.get("colour", [0.6, 0.6, 0.6]), fainted=opp_dead)
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
    if self.state == "anim" and self.anim_t < _VS_MS:
        self._draw_vs(ctx)
    elif self.state == "anim" and self.anim_t < _VS_MS + _DICE_MS:
        self._draw_dice_off(ctx)
    elif self.state == "anim":
        # effects first, then the name over the top of them
        self._draw_effects(ctx, mxx, my, oxx, oy)
        self._draw_queue_column(ctx)
        self._draw_move_log(ctx)
    if self.state == "result":
        self._draw_result_banner(ctx)
    # Call-outs last, and only where the button actually does something.
    # A Practice fight can be skipped; a networked one must play out so the
    # peer's result stays in sync (plan 4.11), so there is deliberately no
    # F here for those - a disc promising a skip we would refuse is worse
    # than no disc. Found by auditing for screens that draw NO call-outs,
    # after the connecting screen turned up still painting "F: cancel":
    # a working button with nothing to grep for is the same bug, quieter.
    if self.state == "result":
        draw_hints(ctx, f="F done", joy=False)
    elif self.state == "anim" and self.is_practice:
        draw_hints(ctx, f="F skip", joy=False)

def _draw_dice_off(self, ctx):
    """The initiative roll, before the mons move (plan 8.3).

    It is the one moment in an auto-battle where something visibly hangs in
    the balance, and it costs nothing mechanically - the rolls were decided
    with the fight. 5% of fights re-roll on a tie, which is why a d20 was
    chosen over a raw compare: the tie is worth showing.
    """
    rolls = self._sim_rolls or ((0, 0),)
    ra, rb = rolls[-1]
    if not self._i_am_a:
        ra, rb = rb, ra
    # Same two quadrants the result screen uses, with the same VS between
    # them: yours upper-left, theirs lower-right. The roll and the verdict
    # are the same claim made twice - who is ahead - so a player who learns
    # where to look during the dice-off is already looking there when the
    # fight ends. Reuses _RESULT_X/_RESULT_Y and _draw_vs outright rather
    # than restating the geometry, so the two cannot drift apart.
    self._draw_vs(ctx, _VS_RESULT_SIZE)
    ctx.text_align = ctx.CENTER
    ctx.text_baseline = ctx.MIDDLE
    # Under the VS rather than on the rim: it labels the roll, and the roll
    # is what the VS sits between. White so it reads as a caption on the
    # card instead of a fourth colour competing with the two numbers.
    ctx.font_size = 14
    ctx.rgb(1.0, 1.0, 1.0)
    ctx.move_to(0, 28).text("Initiative!")
    # COLOURED BY WHOSE THEY ARE, NOT BY WHO WON - owner's call, plan 8.3.
    #
    # Yours green, theirs red, every time. This used to give green to the
    # WINNING roll wherever it sat, which meant the colours swapped sides from
    # fight to fight and green only told you "this number is bigger" - which the
    # two numbers already say by themselves. It was the one thing on the card
    # that was pure redundancy.
    #
    # Fixed colours make the pair readable at a glance instead: your number is
    # always in the same place AND the same colour, so who moves first is read
    # off which side is brighter rather than by comparing digits. `ra` is
    # already swapped to be yours above.
    #
    # The cost, so nobody rediscovers it as a bug: green-means-winner was the
    # only thing marking the moment of victory in the roll itself. Losing the
    # dice-off now reads as neutral. That is the accepted trade - whose-is-whose
    # matters more, because the fight that follows shows who is ahead for the
    # next twelve seconds.
    ctx.font_size = 30
    self._draw_radiant(ctx, "%d" % ra, _RESULT_X, -_RESULT_Y, _RESULT_WIN_RGB)
    self._draw_radiant(ctx, "%d" % rb, -_RESULT_X, _RESULT_Y, _RESULT_LOSE_RGB)
    if len(rolls) > 1:
        ctx.font_size = 11
        ctx.rgb(0.6, 0.6, 0.6).move_to(0, 96).text(
            "%d re-rolls" % (len(rolls) - 1))

def _draw_action_flash(self, ctx, mr, mg, mb, orr, og, ob):
    """The name of each action as it fires. Labels come straight from
    ACTIONS, so there is no second copy of them to drift."""
    ctx.text_align = ctx.CENTER
    ctx.text_baseline = ctx.MIDDLE
    ctx.font_size = 13
    k = self._action_flash / _ACTION_FLASH_MS
    if k > 1.0:
        k = 1.0
    if self._my_action >= 0:
        ctx.rgb(mr * k, mg * k, mb * k)
        ctx.move_to(_MY_XY[0], _MY_NAME_Y - 18).text(
            ACTIONS[self._my_action][0])
    if self._opp_action >= 0:
        ctx.rgb(orr * k, og * k, ob * k)
        ctx.move_to(_OPP_XY[0], _OPP_NAME_Y + 18).text(
            ACTIONS[self._opp_action][0])

# --- per-action visuals (plan 8.3.1) -----------------------------------
# Six mechanically different actions used to look identical apart from a
# flashed word. They differ in what they DO, so they differ in what they
# draw, and a player should be able to tell a leech from a chip without
# reading. Everything here is a lookup into the event buffer or a value
# derived from the tick counter - no allocation, no re-simulation (6.2).
_FX_HIT = (1.0, 0.92, 0.55)      # a plain strike
_FX_LEECH = (0.35, 0.95, 0.45)   # life coming back
_FX_SLOW = (0.70, 0.55, 1.00)    # held up
_FX_CHIP = (0.65, 0.45, 0.20)    # mud, still burning
_FX_HEAL = (0.45, 0.90, 1.00)    # cleaned up
_FX_GUARD = (0.60, 0.75, 1.00)   # braced

def _draw_effects(self, ctx, mx, my, ox, oy):
    """One frame of the fight's effects: the two actions that just fired,
    plus any status that is still running."""
    # progress through the current tick, 0..1 - drives every animation
    # below, so effects finish exactly as the next tick lands
    f = self._anim_acc / _PLAYBACK_MS
    if f > 1.0:
        f = 1.0
    my_chip, my_guard, opp_chip, opp_guard = self._tick_status(
        self._anim_tick)

    # persistent first, so a strike lands ON TOP of the state it hits
    if my_guard:
        self._draw_guard(ctx, mx, my)
    if opp_guard:
        self._draw_guard(ctx, ox, oy)
    if my_chip:
        self._draw_chip(ctx, mx, my, f)
    if opp_chip:
        self._draw_chip(ctx, ox, oy, f)

    if self._action_flash <= 0.0:
        return
    if self._my_action >= 0:
        self._draw_action_fx(ctx, self._my_action, mx, my, ox, oy, f)
    if self._opp_action >= 0:
        self._draw_action_fx(ctx, self._opp_action, ox, oy, mx, my, f)

def _draw_action_fx(self, ctx, aid, sx, sy, tx, ty, f):
    """The instantaneous half: what `aid` looks like as it fires, from
    (sx,sy) at (tx,ty)."""
    kind = ACTIONS[aid][2]
    if kind == GUARD:
        return                       # its whole visual is the persistent one
    if kind == HEAL:
        # self-directed: motes rising off the mon that used it
        ctx.rgb(*self._FX_HEAL)
        for i in range(3):
            ctx.arc(sx - 10 + i * 10, sy - 14 - 16 * f, 2.0 + 1.5 * (1 - f),
                    0, 2 * math.pi, False).fill()
        return
    # everything else travels: a strike from the actor to the target
    px = sx + (tx - sx) * f
    py = sy + (ty - sy) * f
    if kind == LEECH:
        col = self._FX_LEECH
    elif kind == SLOW:
        col = self._FX_SLOW
    elif kind == CHIP:
        col = self._FX_CHIP
    else:
        col = self._FX_HIT
    ctx.rgb(*col).arc(px, py, 4.5, 0, 2 * math.pi, False).fill()
    if f > 0.72:                     # impact flare at the target
        r = 12.0 * (f - 0.72) / 0.28
        ctx.line_width = 2
        ctx.rgb(*col)
        ctx.begin_path()
        ctx.arc(tx, ty, 8 + r, 0, 2 * math.pi, False)
        ctx.stroke()
    if kind == LEECH and f > 0.5:
        # ...and life coming BACK, which is what makes it a leech
        g = (f - 0.5) / 0.5
        ctx.rgb(*self._FX_LEECH)
        ctx.arc(tx + (sx - tx) * g, ty + (sy - ty) * g, 3.0,
                0, 2 * math.pi, False).fill()
    if kind == SLOW and f > 0.72:
        # the victim visibly held up
        ctx.rgb(*self._FX_SLOW)
        ctx.line_width = 2
        for d in (-1, 1):
            ctx.begin_path()
            ctx.arc(tx, ty, 15, 2.6 + d * 0.5, 3.7 + d * 0.5, False)
            ctx.stroke()

def _draw_chip(self, ctx, x, y, f):
    """Mud Sling's 20 ticks of chip: something LEFT BEHIND, still burning.
    Drips fall on a loop keyed to the tick, so it reads as ongoing rather
    than as a fresh hit."""
    ctx.rgb(*self._FX_CHIP)
    for i in range(3):
        p = (f + i * 0.33) % 1.0
        ctx.arc(x - 9 + i * 9, y + 8 + 12 * p, 1.8 * (1.0 - p),
                0, 2 * math.pi, False).fill()

def _draw_guard(self, ctx, x, y):
    """Brace's 14 ticks: a shield arc facing the incoming diagonal, so it
    reads as defensive and as STILL UP rather than as a one-off."""
    ctx.line_width = 2
    ctx.rgb(*self._FX_GUARD)
    a = math.atan2(-y, -x)           # face the middle, where hits come from
    ctx.begin_path()
    ctx.arc(x, y, 26, a - 0.8, a + 0.8, False)
    ctx.stroke()

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

def _draw_vs(self, ctx, size=_VS_SIZE):
    """The VS card. Leaned, glowing, dead centre.

    Drawn under a save/restore so the rotation cannot leak into whatever
    draws next - a transform left on the context is the kind of bug that
    shows up three screens away.
    """
    ctx.save()
    ctx.rotate(_VS_LEAN)
    ctx.text_align = ctx.CENTER
    ctx.text_baseline = ctx.MIDDLE
    ctx.font_size = size
    self._draw_radiant(ctx, "VS", 0, 0, _VS_RGB)
    ctx.restore()

def _draw_radiant(self, ctx, word, x, y, rgb):
    """One word with a still glow around it - see _GLOW_OFF."""
    r, g, b = rgb
    for dx, dy in _GLOW_OFF:
        ctx.rgba(r, g, b, _GLOW_A)
        ctx.move_to(x + dx, y + dy).text(word)
    ctx.rgb(r, g, b)
    ctx.move_to(x, y).text(word)

def _draw_result_banner(self, ctx):
    """The verdict, one word per left quadrant.

    Both words ride the left because that is YOUR side of the screen all
    fight long - the mon, the queue column, the health bar - so the verdict
    lands where the player has been looking, and leaves the opponent's half
    showing what happened to them.

    Single-word results stay centred: DRAW has nothing to split across two
    quadrants, and stacking one word in a corner would read as the first
    half of something the screen forgot to finish.
    """
    ctx.text_align = ctx.CENTER
    ctx.text_baseline = ctx.MIDDLE
    res = self.result
    ctx.font_size = 26
    sub_x, sub_y = 0, 24
    if res == self.R_DRAW:
        ctx.rgb(*_RESULT_RGB["D"]).move_to(0, 0).text("DRAW")
    elif res == self.R_NONE:
        ctx.font_size = 22
        ctx.rgb(*_RESULT_RGB["D"]).move_to(0, 0).text("NO CONTEST")
    else:
        won = self.i_won
        rgb = _RESULT_WIN_RGB if won else _RESULT_LOSE_RGB
        # YOU upper-left, the verdict lower-RIGHT, VS between them: the
        # two words sit on the diagonal and the card they fought under
        # stays in the middle of it.
        self._draw_vs(ctx, _VS_RESULT_SIZE)
        ctx.font_size = 26
        self._draw_radiant(ctx, "YOU", _RESULT_X, -_RESULT_Y, rgb)
        self._draw_radiant(ctx, "WIN!" if won else "LOSE",
                           -_RESULT_X, _RESULT_Y, rgb)
        # under the second word, clear of the C disc at bottom-right
        sub_x, sub_y = -_RESULT_X, _RESULT_Y + 28
    ctx.font_size = 13
    set_color(ctx, "label")
    if res == self.R_NONE:
        # Nothing was staked, so say so - otherwise a no contest reads as a
        # draw the player was charged for (plan 5.7).
        ctx.move_to(sub_x, sub_y).text("nothing staked")
    elif self.is_practice:
        ctx.move_to(sub_x, sub_y).text("practice - no cost")
    else:
        hp = LOSE_HEALTH if res == self.R_LOSE else WIN_HEALTH
        ctx.move_to(sub_x, sub_y).text("HP -> %d" % int(hp))
    draw_hints(ctx, f="F back")


# --- installation ----------------------------------------------------------
# Names are listed explicitly rather than swept out of globals(): a sweep would
# silently also install anything this module imports, and a typo in a name here
# fails loudly at install time instead of at the first frame of a fight.
_METHODS = (

    '_combat_colours',
    '_draw_battle_scrim',
    '_push_move',
    '_draw_move_log',
    '_draw_queue_column',
    '_draw_broken_ring',
    '_draw_battle',
    '_draw_dice_off',
    '_draw_action_flash',
    '_draw_effects',
    '_draw_action_fx',
    '_draw_chip',
    '_draw_guard',
    '_draw_bar',
    '_draw_vs',
    '_draw_radiant',
    '_draw_result_banner',
)

_CONSTANTS = (
    '_SCRIM_BASE',
    '_SCRIM_EDGE',
    '_FX_HIT',
    '_FX_LEECH',
    '_FX_SLOW',
    '_FX_CHIP',
    '_FX_HEAL',
    '_FX_GUARD',
)


def install(cls):
    """Attach the renderer to Battle. Idempotent."""
    g = globals()
    for name in _METHODS + _CONSTANTS:
        setattr(cls, name, g[name])

