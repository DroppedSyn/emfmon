"""The Action queues screen. Plan section 8.1.1.

ONE SCREEN, NO SUBMENUS. Owner: "All options to create view or whatever on the
action queues is done directly in that screen. no more menues." There is no
picker overlay, no naming dialog and no navigating away - creating and deleting
are side effects of editing, and the deepest path in the whole app becomes
Menu -> evo_CONNECT -> Action queues, three levels.

Its own module, and imported LAZILY by battle.py when the player opens it. That
is a watchdog measure, not tidiness: MicroPython compiles at import with no
cached .mpy, battle.py already costs ~1.7 s of blocking compile in a single
frame of the draw loop, and the 5 s task WDT has already rebooted this badge
once (plan 6.2.2). A whole screen does not go on top of that for the benefit of
players who never open it.

            ACTION QUEUES              <- curved, like the battle screen
    Default                Tackle
  * Saved 1                Gobble           <- the selected queue's actions,
    + new queue            Tackle              on the right, readable size
                           Tackle
     (F)                       (C)

Rows are the app's shared ArcMenu, so the queue list scrolls, sizes and places
its button call-outs exactly like every other menu - rather than this screen
inventing its own conventions, which is what made its keys feel foreign.
"""

import math

from app_components import clear_background
from app_components.tokens import set_color
from events.input import BUTTON_TYPES

from .arcmenu import (arc_text_layout, draw_arc_text, draw_hints,
                      FONT_ROW, FONT_SEL, pulse_k)
from .records import MAX_QUEUES, MAX_QUEUE_LEN, MIN_QUEUE_LEN, save_trainer

# The action column, on the right, opposite the rows.
_ACT_X = 44
_SAVED_RGB = (0.20, 0.80, 0.35)   # the app's one "this went well" green
_ACT_ROW = 19
_ACT_SIZE = 15              # bigger than the old 9pt slot chips: these are the
#                             thing you are actually reading while you edit
_ACT_RGB = (0.80, 0.80, 0.84)
_ACT_SEL_RGB = (1.00, 1.00, 1.00)
_ACT_BAD_RGB = (0.85, 0.35, 0.35)
# Chevrons flanking the slot being edited - the guidance, placed on the thing
# it describes rather than in a legend at the bottom.
_ACT_CHEV_RGB = (0.55, 0.80, 1.00)
_ACT_CHEV_SIZE = 13
_ACT_CHEV_DX = 40
# The call-out yellow, and a line that sits just above the joystick glyph at
# y=92 rather than below it.
_CUE_RGB = (1.0, 0.83, 0.15)
_MOVES_CUE_Y = 78

# Title, curved along the upper-right rim at the same angle the battle screen
# uses for BATTLE MODE, so the two read as the same app.
#
# _TITLE_SPAN is the part that matters here: 13 characters at 15pt would wrap
# most of the quadrant and run down into the action column at x=44. Capping the
# span makes arc_text_layout shrink the point size until it fits, so the title
# stays in the rim above the actions instead of colliding with them.
# The I in ACTION sits tight against the T on this arc. Investigated and left
# alone deliberately: it is not advance widths (two fixes at the font metrics
# changed nothing) and not pixel rounding (rotating the whole string changed
# nothing), which leaves the T's crossbar leaning in as it rotates tangent to a
# 99px circle - and no amount of spacing fixes that. BATTLE MODE never shows it
# because it has no narrow glyphs. Not worth more than this comment.
_TITLE_TEXT = "ACTION QUEUES"
_TITLE_SIZE = 15
_TITLE_R = 99.0
_TITLE_MID = 52 * math.pi / 180     # clockwise from 12 o'clock, as battle.py
_TITLE_SPAN = 78 * math.pi / 180
_TITLE_RGB = (0.55, 0.80, 1.00)


class QueueScreen:
    """Two MODES on one screen, never two screens.

    list mode - UP/DOWN pick a row, CONFIRM makes it active, RIGHT edits a
                saved one, CANCEL leaves.
    edit mode - UP/DOWN move between slots, LEFT/RIGHT change the action in
                place, CONFIRM keeps, CANCEL discards.

    UP/DOWN navigates in BOTH modes. It reads as one control scheme rather than
    two, because the vertical list on screen is the thing being moved through
    either way - which is what every other menu in the app does.
    """

    def __init__(self, actions, trainer, pet, default_for, innate_for,
                 arcmenu, blurbs=None, effect_for=None):
        # Everything the combat model owns is passed IN rather than imported,
        # so this module never pulls battle.py back in behind itself.
        self.actions = actions            # the ACTIONS table
        # One sentence per action, aligned with `actions` (plan 8.1.3). Passed
        # in like everything else the combat model owns. Defaulted so a caller
        # that predates the moves list still constructs - the list then shows
        # numbers without prose rather than failing to open.
        self.blurbs = blurbs or ()
        # battle.action_effect. Injected rather than reimplemented here: the
        # kind constants it switches on live in battle.py, and importing them
        # would drag the combat model onto this screen (plan 6.2.2).
        self._effect_for = effect_for
        self.trainer = trainer            # the loaded trainer dict
        self.pet = pet
        self._default_for = default_for   # default_queue_for(pet)
        self._innate_for = innate_for     # innate_actions_for(pet)
        self.done = False
        self.dirty = False                # anything worth saving?
        self.editing = False
        self._saved = False               # showing the SAVED! confirmation
        self.moves = False                # showing the moves reference
        self._move_idx = 0
        self._mv_arcs = None              # curved neighbour names, measured once
        self.row = trainer.get("active", 0)
        self.slot = 0
        self._draft = None                # the queue being edited, as a list
        self._acts = None                 # cached action labels for the right
        self.menu = arcmenu               # the app's shared ArcMenu
        self._menu_dirty = True
        self._title = None                # curved title, measured once
        self._clamp_row()

    # --- what the player can field ----------------------------------------
    def available(self):
        """Innate + collected, in a stable order (plan 4.3).

        Phase 3 has no collected pool yet - 14.3 fills it in Phase 6 - so this
        is usually just Tackle plus the mon's trait action. That is still a real
        choice, because the 14% repeat penalty makes ORDER matter.
        """
        acts = list(self._innate_for(self.pet))
        for aid in self.trainer.get("pool", ()):
            if aid not in acts:
                acts.append(aid)
        return acts

    # --- rows --------------------------------------------------------------
    def _queues(self):
        return self.trainer.setdefault("queues", [])

    def n_rows(self):
        """Row 0 is the virtual default; then saved queues; then, unless the
        cap is reached, '+ build a queue'."""
        n = 1 + len(self._queues())
        if len(self._queues()) < MAX_QUEUES:
            n += 1
        return n

    def _is_build_row(self, row):
        return (len(self._queues()) < MAX_QUEUES
                and row == 1 + len(self._queues()))

    def queue_at(self, row):
        """The ids for a row, or None for '+ build a queue'."""
        if row == 0:
            return [i for i in self._default_for(self.pet) if i != 0xFF]
        if self._is_build_row(row):
            return None
        qs = self._queues()
        idx = row - 1
        return qs[idx] if 0 <= idx < len(qs) else None

    def valid(self, row):
        """Is every entry in this row fieldable by the CURRENT mon (plan 14.4b)?

        Saved queues are trainer-level but actions are partly innate, so a queue
        saved by a greedy mon contains Gobble and becomes unusable when the next
        mon is tidy. This will happen constantly - traits are random per mon and
        saved queues are exactly the thing players keep across them.

        Row 0 can never fail: it is derived live from the current mon, which is
        what guarantees a player always has at least one usable queue.
        """
        q = self.queue_at(row)
        if q is None:
            return True
        have = self.available()
        return all(a in have for a in q)

    def _clamp_row(self):
        n = self.n_rows()
        if self.row >= n:
            self.row = n - 1
        if self.row < 0:
            self.row = 0

    def invalidate(self):
        self._acts = None
        self._menu_dirty = True

    # --- input -------------------------------------------------------------
    def button(self, event):
        if self.moves:
            self._moves_button(event)
        elif self._saved:
            self._saved_button(event)
        elif self.editing:
            self._edit_button(event.button)
        else:
            self._list_button(event)

    def _saved_button(self, event):
        """After a save, F is the only way on.

        The confirmation replaces the queue it is confirming, so there is
        nothing left on screen to act on - and a C that still did something
        would invite a second press on a queue already written.
        """
        if BUTTON_TYPES["CANCEL"] in event.button:
            self._saved = False
            self.invalidate()

    def _list_button(self, event):
        """Navigation is the ArcMenu's, so scrolling, wrapping, debouncing and
        the joystick all behave exactly as they do in every other menu.

        RIGHT is checked FIRST and never reaches the menu: ArcMenu ignores it
        (returns None) but still stamps its debounce clock, which would eat the
        next press.
        """
        b = event.button
        if BUTTON_TYPES["LEFT"] in b:
            # The moves reference (plan 8.1.3). LEFT is the free control here -
            # RIGHT already means "edit this queue", so the pair reads as
            # out-to-the-reference and in-to-the-work. Intercepted BEFORE the
            # menu for the same reason RIGHT is: ArcMenu ignores it but still
            # stamps its debounce clock, which would eat the next press.
            self.moves = True
            self._move_idx = 0
            self._menu_dirty = True
            return
        if BUTTON_TYPES["RIGHT"] in b:
            # Saved queues only. The default row is a function of the trait;
            # wanting to change it means wanting a custom queue, which is what
            # the bottom row is for.
            if self.row > 0 and not self._is_build_row(self.row):
                self._enter_edit()
            return
        self._sync_menu()
        act = self.menu.button(event)
        if self.menu.idx != self.row:
            self.row = self.menu.idx
            self._acts = None            # different queue shown on the right
        if act == "back":
            self.done = True
        elif act == "select":
            if self._is_build_row(self.row):
                self._create()
            elif self.valid(self.row):
                # An unusable queue cannot be made active - it would be sent
                # over the wire and hand the opponent a no contest through no
                # fault of theirs (plan 14.4b).
                self.trainer["active"] = self.row
                self.dirty = True
                self.invalidate()

    def _moves_button(self, event):
        """The reference is READ-ONLY: F leaves, the stick scrolls, C does
        nothing. Per the records screen, a C call-out that offers nothing is
        not drawn.

        LEFT/RIGHT cycle, because the selector is horizontal and because that
        is already what the chevrons mean in edit mode - the same gesture moves
        through actions in both places.
        """
        b = event.button
        if BUTTON_TYPES["CANCEL"] in b:
            self.moves = False
            self._menu_dirty = True     # the queue list needs its rows back
            return
        n = len(self.actions)
        if BUTTON_TYPES["RIGHT"] in b:
            self._move_idx = (self._move_idx + 1) % n
            self._mv_arcs = None        # different neighbours to measure
        elif BUTTON_TYPES["LEFT"] in b:
            self._move_idx = (self._move_idx - 1) % n
            self._mv_arcs = None

    def _create(self):
        """'+ build a queue' - seeded from the default rather than blank, so a
        player starts from something that works and adjusts."""
        self._queues().append(list(self.queue_at(0)))
        self.row = len(self._queues())      # the row just created
        self.dirty = True
        self._enter_edit()

    def _enter_edit(self):
        q = self.queue_at(self.row)
        if q is None:
            return
        self._draft = list(q)               # edit a COPY, so CANCEL can discard
        self.slot = 0
        self.editing = True
        self.invalidate()

    def _add_row(self):
        """Index of the virtual '+' row, or None when the queue is full."""
        return (len(self._draft) if len(self._draft) < MAX_QUEUE_LEN
                else None)

    def _edit_button(self, b):
        if BUTTON_TYPES["CONFIRM"] in b:
            self._commit_edit()
            return
        if BUTTON_TYPES["CANCEL"] in b:
            self._discard_edit()
            return
        add = self._add_row()
        last = len(self._draft) if add is not None else len(self._draft) - 1
        if BUTTON_TYPES["UP"] in b:
            self.slot = last if self.slot <= 0 else self.slot - 1
            self._acts = None
        elif BUTTON_TYPES["DOWN"] in b:
            self.slot = 0 if self.slot >= last else self.slot + 1
            self._acts = None
        elif BUTTON_TYPES["RIGHT"] in b:
            if add is not None and self.slot == add:
                self._draft.append(0)       # a new slot starts on Tackle
                self._acts = None
            else:
                self._cycle(1)
        elif BUTTON_TYPES["LEFT"] in b:
            if not (add is not None and self.slot == add):
                self._cycle(-1)

    def _cycle(self, step):
        """Change the action under the cursor IN PLACE, wrapping.

        This is what removes the picker: selecting a slot never opens a list,
        LEFT/RIGHT steps through what this mon can field and the row redraws.

        EMPTY is one of the values, at the end of the cycle - not something you
        fall off the end into. Plan 8.1.1 has deleting be "cycle a slot to
        empty", which assumed the collected pool would make that list long. It
        does not yet: a new trainer has Tackle plus one trait action, so
        stepping off the end deleted a slot on the second press, every time. As
        an explicit value you can see coming it stays discoverable and stops
        being an accident.
        """
        if not self._draft or self.slot >= len(self._draft):
            return
        opts = self.available()
        cur = self._draft[self.slot]
        n = len(opts) + 1                  # ...actions..., then EMPTY, then wrap
        # EMPTY is the LAST index, not index 0 - treating it as 0 made the
        # cycle oscillate between the last action and empty, and the first
        # action became unreachable.
        if cur is None:
            i = len(opts)
        else:
            i = opts.index(cur) if cur in opts else 0
        i = (i + step) % n
        self._draft[self.slot] = None if i == len(opts) else opts[i]
        self._acts = None

    def _commit_edit(self):
        qs = self._queues()
        idx = self.row - 1
        # Empty slots are dropped HERE, not while editing - so a slot you
        # cycled past empty on the way somewhere else is not lost under you.
        self._draft = [i for i in self._draft if i is not None]
        if not self._draft:
            # every slot emptied - the queue is deleted
            if 0 <= idx < len(qs):
                del qs[idx]
            self._fix_active_after_delete(self.row)
        elif len(self._draft) < MIN_QUEUE_LEN:
            # too short to field. Keep the player in edit mode rather than
            # silently padding something they did not ask for.
            self.editing = True
            self.invalidate()
            return
        elif 0 <= idx < len(qs):
            qs[idx] = list(self._draft)
            self._saved = True     # only a real save says SAVED!
        self.editing = False
        self._draft = None
        self.dirty = True
        self._clamp_row()
        self.invalidate()

    def _fix_active_after_delete(self, deleted_row):
        a = self.trainer.get("active", 0)
        if a == deleted_row:
            a = 0                    # fall back to the default, always valid
        elif a > deleted_row:
            a -= 1
        self.trainer["active"] = a

    def _discard_edit(self):
        # A queue created this visit and then abandoned should not linger.
        if self._draft is not None and self.row - 1 == len(self._queues()) - 1:
            q = self.queue_at(self.row)
            if q is not None and not q:
                del self._queues()[self.row - 1]
        self.editing = False
        self._draft = None
        self._clamp_row()
        self.invalidate()

    def save(self):
        if self.dirty:
            save_trainer(self.trainer)
            self.dirty = False

    # --- drawing -----------------------------------------------------------
    def _label(self, row):
        if row == 0:
            return "Default"
        if self._is_build_row(row):
            return "+ new queue"
        return "Saved %d" % row

    def _sync_menu(self):
        """Load the current list into the shared ArcMenu. Runs on a change,
        never per frame - set_items remeasures, and doing that 60x/s is the
        allocation pattern that fragmented the heap before (plan 6.2).

        The moves reference does NOT use this: it is a one-line horizontal
        selector, not a list, so there is nothing for the widget to do.
        """
        if not self._menu_dirty:
            return
        # Stated, not inherited - see ArcMenu.configure, which also owns the
        # fonts-before-set_items ordering so no caller has to remember it.
        self.menu.configure([self._label(r) for r in range(self.n_rows())],
                            idx=self.row, side="left",
                            hint_c="C use", hint_f="F back")
        self._menu_dirty = False

    def _build_acts(self):
        """The action names for the row under the cursor. Rebuilt on a move or
        an edit, never per frame."""
        ids = list(self._draft) if self.editing else self.queue_at(self.row)
        if ids is None:
            self._acts = None
            return
        have = self.available()
        self._acts = [("- empty -", True) if i is None
                      else (self.actions[i][0], i in have) for i in ids]

    def draw(self, ctx):
        if self.moves:
            self._draw_moves(ctx)
            return
        if self._acts is None:
            self._build_acts()
        self._sync_menu()
        clear_background(ctx)

        # rows + scrim first, call-outs LAST so nothing clips them
        self.menu.draw(ctx, hint=False)
        self._draw_title(ctx)
        if self._saved:
            self._draw_saved(ctx)
        else:
            self._draw_actions(ctx)
            if not self.editing and not self.valid(self.row):
                self._draw_unusable(ctx)     # over the queue it is about
            self._draw_edit_cue(ctx)
            self._draw_footer(ctx)
        if self._saved:
            # only F does anything now, so only F is offered
            draw_hints(ctx, f="F back", joy=False)
        elif self.editing:
            # edit mode rebinds the buttons, so it draws its own call-outs
            draw_hints(ctx, c="C keep", f="F discard")
        else:
            self.menu.draw_hint(ctx)

    # The moves reference: a selector on one line at the top, everything else
    # centred down the body. Nothing hugs a column, so nothing can collide with
    # a column beside it - which is what the two previous layouts did.
    _MV_TITLE_Y = -96
    _MV_SEL_Y = -72
    # The neighbours CURVE along the bezel. Flat text out there ran off the round
    # screen and had to be shrunk to nearly nothing to fit - curving is what buys
    # the width back, so they can be read.
    #
    # DOWN THE SIDES, not level with the selector. Level was the obvious place
    # and it put the left one straight under the F call-out: that disc sits at
    # 33 degrees from 12 o'clock and occupies about 25-41, while a 42-degree span
    # centred at the selector's height reached back to 26. Centred at 66 the
    # longest name reaches back only to 48, clear of F by about 13px of rim, and
    # low enough down the edge to flank the selector rather than crowd it.
    _MV_NEIGHBOUR_R = 106.0
    _MV_NEIGHBOUR_SIZE = 12
    _MV_NEIGHBOUR_SPAN = 42 * math.pi / 180
    _MV_NEIGHBOUR_MID = 66 * math.pi / 180
    # White. They were the dim grey used for captions and were unreadable that
    # small on a dark scrim - these are names you are choosing between, not
    # decoration around the one you have.
    _MV_NEIGHBOUR_RGB = (1.00, 1.00, 1.00)
    # The app's shining blue, as used for the mon's name and a selected row.
    _MV_TITLE_RGB = (0.35, 0.75, 1.0)
    _MV_CHEV_DX = 44
    _MV_EFFECT_Y = -26
    _MV_COST_Y = 20
    _MV_BLURB_Y = 48
    _MV_LOCKED_Y = 88
    _MV_NAME_RGB = (1.00, 1.00, 1.00)
    _MV_COST_RGB = (1.00, 0.83, 0.15)
    _MV_LOCKED_RGB = (0.62, 0.45, 0.45)

    def _build_mv_arcs(self, ctx, i, n):
        """Lay the two neighbour names along the rim. Once per selection, never
        per frame - arc_text_layout MEASURES every glyph, and doing that 60x/s
        is the per-frame measurement the menu rows had taken out of them."""
        mid = self._MV_NEIGHBOUR_MID
        self._mv_arcs = (
            arc_text_layout(ctx, self.actions[(i - 1) % n][0],
                            self._MV_NEIGHBOUR_R, -mid,
                            self._MV_NEIGHBOUR_SIZE,
                            max_span=self._MV_NEIGHBOUR_SPAN),
            arc_text_layout(ctx, self.actions[(i + 1) % n][0],
                            self._MV_NEIGHBOUR_R, mid,
                            self._MV_NEIGHBOUR_SIZE,
                            max_span=self._MV_NEIGHBOUR_SPAN),
        )

    def _draw_moves(self, ctx):
        """A one-line selector at the top, and the whole body for the detail.

        Not a list. The rows-on-the-left shape 8.1.3 assumed spends half the
        screen on names you are not reading, and the description then has to
        share the remaining half with the numbers - which is exactly how the
        first two cuts ended up clipping. A horizontal selector costs one line
        and hands the entire body to the thing you opened this to read.

        The neighbours either side are shown small and dim, so the set has a
        size and a position in it - a lone name with arrows tells you neither.

        Every number straight out of ACTIONS, never retyped. COST LEADS and is
        the only figure given its own colour: it is the least obvious and most
        important number in the model, because players read power first and
        build queues of expensive actions that rarely fire, when a queue is a
        rotation and cost is how long each slot holds it.
        """
        clear_background(ctx)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE

        ctx.rgb(*self._MV_TITLE_RGB)
        ctx.font_size = 15
        ctx.move_to(0, self._MV_TITLE_Y).text("MOVES")

        n = len(self.actions)
        i = self._move_idx
        act = self.actions[i]
        y = self._MV_SEL_Y

        if self._mv_arcs is None:
            self._build_mv_arcs(ctx, i, n)
        ctx.font_size = self._MV_NEIGHBOUR_SIZE
        ctx.rgb(*self._MV_NEIGHBOUR_RGB)
        for layout in self._mv_arcs:
            draw_arc_text(ctx, layout)

        ctx.font_size = 13
        ctx.rgb(*_ACT_CHEV_RGB)
        ctx.move_to(-self._MV_CHEV_DX, y).text("<")
        ctx.move_to(self._MV_CHEV_DX, y).text(">")

        ctx.font_size = 19
        ctx.rgb(*(self._MV_NAME_RGB if i in self.available()
                  else self._MV_LOCKED_RGB))
        ctx.move_to(0, y).text(act[0])

        # --- the body, which is the whole screen -------------------------
        # WHAT IT DOES leads, then what it costs, then what it is for. The cost
        # is still the number that decides a queue - a cheap action fires more
        # often than an expensive one whatever its power (plan 8.1.3) - so it
        # keeps its own colour and its own line rather than being folded into
        # the sentence, where it would stop being a figure you can compare.
        if self._effect_for is not None:
            ctx.rgb(*self._MV_NAME_RGB)
            for j, line in enumerate(self._effect_for(act).split("\n")):
                ctx.font_size = 22 if j == 0 else 13
                ctx.move_to(0, self._MV_EFFECT_Y + j * 22).text(line)

        # FIXED y, not flowed under the effect above it. Half the actions have a
        # second effect line and half do not, so a flowed cost jumps up and down
        # as you cycle - and this is a selector, meant to be flicked through.
        # An action with one effect line simply leaves the second one empty.
        ctx.font_size = 13
        ctx.rgb(*self._MV_COST_RGB)
        ctx.move_to(0, self._MV_COST_Y).text("%dt to fire" % act[1])

        ctx.font_size = 11
        ctx.rgb(0.76, 0.76, 0.80)
        blurb = (self.blurbs[i] if i < len(self.blurbs) else "")
        for j, line in enumerate(blurb.split("\n")):
            ctx.move_to(0, self._MV_BLURB_Y + j * 13).text(line)

        if i not in self.available():
            # Out of reach for THIS mon, shown rather than hidden: knowing an
            # action exists and you cannot bring it is information, where
            # omitting it looks like a shorter game (plan 8.1.3).
            ctx.font_size = 10
            ctx.rgb(*self._MV_LOCKED_RGB)
            ctx.move_to(0, self._MV_LOCKED_Y).text("not yours yet")

        # F only. Nothing here can be acted on, so no C disc (records screen).
        draw_hints(ctx, f="F back", joy=True)

    def _draw_saved(self, ctx):
        """SAVED!, where the queue was.

        It replaces the action column rather than sitting beside it: the queue
        is written and there is nothing left to read there, and a confirmation
        that shares the screen with the thing it confirms reads as a label on
        it rather than as something that just happened.
        """
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 24
        ctx.rgb(*_SAVED_RGB)
        ctx.move_to(_ACT_X, -8).text("SAVED!")
        ctx.font_size = 12
        ctx.rgb(0.55, 0.55, 0.58)
        ctx.move_to(_ACT_X, 18).text("queue written")

    def _draw_title(self, ctx):
        if self._title is None:
            self._title = arc_text_layout(ctx, _TITLE_TEXT, _TITLE_R,
                                          _TITLE_MID, _TITLE_SIZE,
                                          max_span=_TITLE_SPAN)
        ctx.font_size = _TITLE_SIZE
        k = pulse_k() if self.editing else 1.0
        r, g, b = _TITLE_RGB
        ctx.rgb(r * k, g * k, b * k)
        draw_arc_text(ctx, self._title)

    def _draw_actions(self, ctx):
        """The selected queue's actions, on the right, at a size you can
        actually read - this is the thing you look at while editing.

        In edit mode the current slot is flanked by chevrons. Those ARE the
        guidance: they sit on the thing they act on and say "left and right
        change this", which a line of text at the bottom of the screen cannot
        do - and that line had nowhere to live anyway, since the C and F discs
        own the bottom corners.
        """
        if self._acts is None:
            return
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        n = len(self._acts)
        add = n if (self.editing and n < MAX_QUEUE_LEN) else -1
        rows = n + (1 if add >= 0 else 0)
        top = -((rows - 1) * _ACT_ROW) / 2.0
        for i, (name, ok) in enumerate(self._acts):
            y = top + i * _ACT_ROW
            here = self.editing and i == self.slot
            ctx.font_size = _ACT_SIZE
            if not ok:
                ctx.rgb(*_ACT_BAD_RGB)     # this mon cannot field it (14.4b)
            elif here:
                ctx.rgb(*_ACT_SEL_RGB)
            else:
                ctx.rgb(*_ACT_RGB)
            ctx.move_to(_ACT_X, y).text(name)
            if here:
                ctx.font_size = _ACT_CHEV_SIZE
                ctx.rgb(*_ACT_CHEV_RGB)
                ctx.move_to(_ACT_X - _ACT_CHEV_DX, y).text("<")
                ctx.move_to(_ACT_X + _ACT_CHEV_DX, y).text(">")
        if add >= 0:
            y = top + add * _ACT_ROW
            here = self.slot == add
            ctx.font_size = _ACT_SIZE
            ctx.rgb(*(_ACT_SEL_RGB if here else (0.45, 0.45, 0.48)))
            ctx.move_to(_ACT_X, y).text("+ add")
            if here:
                ctx.font_size = _ACT_CHEV_SIZE
                ctx.rgb(*_ACT_CHEV_RGB)
                ctx.move_to(_ACT_X + _ACT_CHEV_DX, y).text(">")

    # An unusable queue is marked with a big "!" ON the queue rather than
    # explained in a caption at the bottom. The caption was six words of small
    # red text at the far edge of the screen from the thing it was about, and
    # a player scanning the list had to read it to find out which row it meant.
    # The mark cannot be misattributed: it is drawn over the actions it refers
    # to. What is WRONG stays in the manual (14.4b) rather than on the screen.
    _BANG_SIZE = 64
    _BANG_RGB = (0.92, 0.32, 0.32)

    def _draw_unusable(self, ctx):
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = self._BANG_SIZE
        ctx.rgb(*self._BANG_RGB)
        ctx.move_to(_ACT_X, 0).text("!")

    def _draw_edit_cue(self, ctx):
        """A single chevron beside an editable queue, saying RIGHT opens it.

        Replaces the "D: edit" legend that used to sit under the joystick
        glyph. Deleting that outright would have left nothing at all pointing
        at edit mode - the one action on this screen with no other way in - so
        it moves to the language the editor already speaks: a chevron ON the
        thing it acts on, pointing the way the stick goes.
        """
        if self.editing or self._acts is None:
            return
        if self.row <= 0 or self._is_build_row(self.row):
            return          # the default row is derived; the build row is C
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = _ACT_CHEV_SIZE
        k = pulse_k()
        r, g, b = _ACT_CHEV_RGB
        ctx.rgb(r * k, g * k, b * k)
        ctx.move_to(_ACT_X + _ACT_CHEV_DX + 14, 0).text(">")

    def _draw_footer(self, ctx):
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 10
        if self.editing:
            # The chevrons carry the guidance now, so there is no control
            # legend here to collide with the C and F discs at y=+-84. Only a
            # warning, and it goes BELOW them where nothing else sits.
            if len(self._draft) < MIN_QUEUE_LEN:
                ctx.rgb(0.9, 0.4, 0.4).move_to(0, 104).text(
                    "min %d actions" % MIN_QUEUE_LEN)
            return
        # LEFT is not a control anyone guesses, so say it. The chevron points
        # the way the press goes, matching the edit cue's ">" on the other side.
        # In the call-out yellow and ABOVE the joystick glyph, because it names
        # a control - the grey down at 104 read as a caption for the screen.
        ctx.rgb(*_CUE_RGB)
        ctx.move_to(0, _MOVES_CUE_Y).text("< moves")
        if not self.valid(self.row):
            pass          # said by the mark over the queue - see _draw_actions
        elif not self.valid(self.trainer.get("active", 0)):
            # The chosen queue outlived the mon that could field it (14.4b).
            # Say so rather than silently substituting - rewriting someone's
            # build is what players resent - but do not leave them wondering
            # why the fight looked different.
            ctx.rgb(0.9, 0.4, 0.4).move_to(0, 86).text(
                "* unusable - using Default")
        # (the old "D: edit" legend lived here, sitting under the joystick
        # glyph and in the old text style - see _draw_edit_cue for what says it
        # now)
