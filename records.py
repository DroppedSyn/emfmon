"""Persistent battle records. Plan section 6.2.3.

SMALL ON PURPOSE. This exists so the death-and-replacement path does not have to
import the combat model.

`app.py` archives a mon's record when it dies or is replaced (app.py:591), and
that used to be `from .battle import archive_records, blank_records` - dragging
in all of `battle.py`, the combat model, the sanitisers, the protocol framing
and the whole battle UI, to call two small functions on a path that has nothing
to do with battling. Measured at **1126 ms of blocking compile** on the badge:
MicroPython compiles at import, with no cached `.mpy`, so module size is a stall
in one frame of the draw loop against a 5 s task watchdog (plan 6.2.2). Phase 3
makes `battle.py` considerably bigger, so that only gets worse.

Nothing that fights, talks or draws belongs in here. Keep it that way: the value
of this module is entirely in what it does NOT import.

Plan 14.4 gives `trainer.json` the same home for the same reason - section 14.3
grants inherited actions on death or humane replacement, which is this exact
path, so the trainer file must not drag the combat model in either.

Phase 3 uses the trainer file's `queues` and `active`. Its `pool` (14.3) and
`bp` (14.2) are written and preserved from the start, so Phase 6 adds behaviour
rather than a migration.

Phase 6 adds the battlepoints award and the trainer level (14.2). Both live here
rather than in battle.py, and that is deliberate rather than convenient: they are
arithmetic over the trainer file, they import nothing, and the screen that
renders them (8.1.2) has no reason to pull in the combat model. "Nothing that
fights, talks or draws" above is a rule about IMPORT WEIGHT, and a pure integer
formula costs none - but do not let anything follow them in here that needs
ACTIONS, a socket or a ctx.
"""

import json

from .app import _DIR, _life_stage

BATTLES_PATH = _DIR + "/battles.json"
TRAINER_PATH = _DIR + "/trainer.json"

MAX_LOG = 20             # battle history entries kept

# --- trainer file bounds (plan 14.4) ---------------------------------------
# Everything here is bounded, because everything here is loaded from a file that
# can be corrupt and will one day be edited by someone curious.
MAX_POOL = 12            # distinct collected action ids - STORAGE headroom, not
                         # a target. See N_COLLECTIBLE for what a player can
                         # actually reach; showing this as a denominator would
                         # promise nine actions that do not exist.
MAX_QUEUES = 5           # saved queues
MAX_QUEUE_LEN = 5        # entries per queue (plan 4.5)
MIN_QUEUE_LEN = 3

# Which action a trait bequeaths (plan 14.3). LIVES HERE, not in battle.py, and
# battle.py imports it from this module - it is the one table both the combat
# model and the pet-death path need, and the death path must not import the
# combat model to get it (plan 6.2.3). Every mon has exactly one non-Tackle
# innate action; Tackle is innate to all and is therefore never collected.
#
# BUMP `rules_ver` ON ANY CHANGE HERE. It decides what a mon can field, so two
# badges disagreeing about it disagree about the fight (battle.py's note above
# ACTIONS says the same, and moving this table did not move that rule).
#
# Keyed by trait STRING, so this one stays a dict - TRAITS.index() would be
# worse (plan 6.2.1 #1).
TRAIT_ACTION = {"greedy": 1, "playful": 2, "messy": 3, "tidy": 4, "hardy": 5}

# What a player can COLLECT: derived, not restated, so it cannot drift from the
# table above (plan 14.2's rule about the trainer level, applied to a count).
# Completing the set is a coupon-collector problem - about 11 adult mons, not 5.
N_COLLECTIBLE = len(TRAIT_ACTION)

# len(ACTIONS) - the id bound the sanitisers need, and the ONE number here that
# is still a restatement of battle.py, because it counts a table that genuinely
# lives there. Deliberately NOT asserted against it at import: a mismatch should
# show a wrong count on one screen, which is a bug, where an assert would refuse
# to boot, which is a brick in someone's hand at a festival.
N_ACTIONS = 6


def blank_records():
    """A fresh, empty record - what a newly hatched mon starts with.

    `d` (draws) is new for BATTLE_EVO!: a simultaneous KO, or the 20 s cap with
    exactly equal HP, is a real result and costs both mons the same as a win
    (plan 5.7). It must be added HERE and in _load_records() together - see the
    comment there.
    """
    return {"w": 0, "l": 0, "d": 0, "log": [], "pw": 0, "pl": 0}


def archive_records():
    """Take the current mon's battle record and clear the file for the next one.

    Records used to be global: battles.json was never reset, so a new mon
    inherited its predecessor's W/L and opponent log. Now app.py calls this when
    a mon dies or is replaced, stores the returned snapshot on the history entry,
    and the successor starts from zero.
    """
    rec = _load_records()
    try:
        with open(BATTLES_PATH, "w") as f:
            f.write(json.dumps(blank_records()))
    except Exception as e:
        print("Records: reset failed:", e)
    return rec


def save_records(rec):
    """Write a record back. Callers own the "is this a live mon's record?"
    question - an ARCHIVED record is history and must never be written back
    (battle.py guards that with its `archived` flag)."""
    try:
        with open(BATTLES_PATH, "w") as f:
            f.write(json.dumps(rec))
    except Exception as e:
        print("Records: save failed:", e)


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
        # This REBUILDS a fixed-key dict and silently drops anything it does not
        # name, so every new counter must be added here as well as in
        # blank_records() - otherwise it writes fine and then resets to zero on
        # the next load, with no error anywhere.
        return {
            "w": max(0, int(data.get("w", 0))),      # ranked wins
            "l": max(0, int(data.get("l", 0))),      # ranked losses
            "d": max(0, int(data.get("d", 0))),      # ranked draws (EVO)
            "log": log,                               # ranked opponent history
            "pw": max(0, int(data.get("pw", 0))),    # practice wins
            "pl": max(0, int(data.get("pl", 0))),    # practice losses
        }
    except Exception:
        return blank_records()


# --- the trainer file (plan 14.4) ------------------------------------------
# Trainer-level, and it OUTLIVES EVERY MON. That is the whole reason it is a
# separate file from state.json: _die() and _hatch_new() must not touch it, and
# because it lives elsewhere, they cannot.
#
# Phase 3 uses `queues` and `active`. `pool` (plan 14.3) and `bp` (14.2) are
# written and preserved from the start so Phase 6 needs no migration - they are
# simply not read yet.


# The trainer's own name, as opposed to the mon's. Every badge ships with the
# same default on purpose: a screen that says "TRAINER" and nothing else invites
# nobody to change it, where one that already says a name makes renaming it the
# obvious thing to do. It is also the only field here a player types, so it is
# the only one that needs cleaning rather than merely bounding.
DEFAULT_TRAINER_NAME = "Josh"
# Longer than a pet's eight, because the two are constrained differently. A pet
# name shares a line with its age and has nowhere to shrink to; the trainer name
# gets the whole middle of the screen to itself and is fitted to it by
# measurement, so a long one gets SMALLER rather than getting CUT. Twelve is
# where that stops working - past it the name is too small to read before it is
# too wide to fit, so the cap is the point where scaling gives up, not a
# style choice.
MAX_TRAINER_NAME = 12


def clean_trainer_name(s):
    """Whatever was typed -> something drawable, or "" if there is nothing left.

    Case is PRESERVED, unlike the pet's name which is forced upper. A trainer is
    a person and "Josh" should stay "Josh"; a mon is a creature with a shouty
    Tamagotchi name. Returning "" rather than the default lets the caller tell
    "they cleared it" from "they typed something", which is what stops an
    accidental empty confirm wiping the name to blank.
    """
    try:
        s = str(s)
    except Exception:
        return ""
    out = []
    for ch in s.strip()[:MAX_TRAINER_NAME]:
        # Printable ASCII only. The badge font has no glyph for a control
        # character and draws a blank, so a name of them looks like a bug.
        if 32 <= ord(ch) < 127:
            out.append(ch)
    return "".join(out).strip()


def blank_trainer():
    return {"name": DEFAULT_TRAINER_NAME, "pool": [], "bp": 0,
            "queues": [], "active": 0}


def _clean_queue_list(raw, n_actions):
    """Sanitise saved queues off disk. Drops what it cannot understand rather
    than carrying junk into the queue builder (same defensive posture as
    _load_state, app.py:428)."""
    out = []
    if not isinstance(raw, list):
        return out
    for q in raw[:MAX_QUEUES]:
        if not isinstance(q, list):
            continue
        ids = []
        for v in q[:MAX_QUEUE_LEN]:
            try:
                i = int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= i < n_actions:
                ids.append(i)
        if ids:                      # an all-junk queue is dropped, not kept
            out.append(ids)
    return out


def load_trainer(n_actions):
    """`n_actions` is len(ACTIONS), passed IN rather than imported - this module
    must not pull in the combat model, which is the entire point of it
    existing (plan 6.2.3)."""
    try:
        with open(TRAINER_PATH) as f:
            data = json.loads(f.read())
        if not isinstance(data, dict):
            raise ValueError("bad trainer file")
    except Exception:
        return blank_trainer()
    pool = []
    raw_pool = data.get("pool")
    if isinstance(raw_pool, list):
        for v in raw_pool:
            try:
                i = int(v)
            except (TypeError, ValueError):
                continue
            # distinct ids only - inheriting a trait you already have is a
            # no-op, not a duplicate (plan 14.3)
            if 0 <= i < n_actions and i not in pool and len(pool) < MAX_POOL:
                pool.append(i)
    try:
        bp = max(0, int(data.get("bp", 0)))
    except (TypeError, ValueError):
        bp = 0
    queues = _clean_queue_list(data.get("queues"), n_actions)
    try:
        active = int(data.get("active", 0))
    except (TypeError, ValueError):
        active = 0
    # `active` indexes the DISPLAYED list, whose row 0 is the virtual default
    # queue (plan 8.1.1), so it runs 0..len(queues) inclusive.
    if not 0 <= active <= len(queues):
        active = 0
    # Like _load_records, this REBUILDS a fixed-key dict and drops anything it
    # does not name - so a new field must be added HERE as well as in
    # blank_trainer(), or it writes fine and resets on the next load.
    name = clean_trainer_name(data.get("name", "")) or DEFAULT_TRAINER_NAME
    return {"name": name, "pool": pool, "bp": bp, "queues": queues,
            "active": active}


# --- battlepoints and the trainer level (plan 14.2) ------------------------
# An arcade score. NOT a currency: nothing is bought with these, nothing
# subtracts, and the plan is explicit that a speculative economy is the
# complexity to leave out - so do not add a spend path here without the owner
# asking for one.

# The four outcomes of plan 5.7, owned here because the scoring formula below
# switches on them and battle.py's R_* used to be a second set of the same four
# string literals. Two copies of a taxonomy is one copy too many: they agreed by
# coincidence, and a rename on either side would have gone unnoticed until a
# fight scored zero. battle.py now aliases these.
OUT_WIN = "win"
OUT_LOSE = "lose"
OUT_DRAW = "draw"
OUT_NONE = "nocontest"      # protocol failure: scores nothing, records nothing

BP_KO_WIN = 100             # a KO - 99.57% of wins
BP_HP_WIN = 50              # ahead on HP at the tick cap, measured at 0.00%
BP_DRAW = 25                # a real dead heat, not a no contest
MAX_LEVEL = 999             # 999**3 = 997,002,999 bp - about nine million wins


def battlepoints_for(outcome, winner_hp, ko):
    """Points for one ranked fight. Plan 14.2's formula, and nothing else.

        KO win  ->  100 + winner's remaining HP     # 101-142, median 110
        HP win  ->   50 + winner's remaining HP     # less decisive
        draw    ->   25
        loss    ->    0

    `winner_hp` is what `simulate()` returns third, and decisiveness lives
    ENTIRELY in it: a 42 HP win scores 142, a 2 HP squeaker 102. The plan
    measured the winner's remaining HP at p10 2 / p50 10 / p90 23, which is why
    it is the scoring input - the original design scored off "KO vs not a KO"
    and that was measured at 99.57% / 0.00%, a tier that would never have fired.

    `ko` separates the two win tiers. It is NOT `ticks < CAP_TICKS`: the fight
    loop runs `range(CAP_TICKS + 1)` and a KO landing on the final tick returns
    the cap as its tick count, so a tick comparison mislabels that fight a 50
    and quietly docks it 50 points. Caller derives it from the loser's HP.

    A LOSS AND A NO CONTEST BOTH SCORE ZERO, and that is not the same thing
    happening twice - a loss is a result the caller still records, a no contest
    must leave the pet byte-identical with nothing written at all (plan 5.7).
    Zero here is the arithmetic; the caller owns the difference.

    Practice is not a case in this function. It scores zero by never reaching
    it - plan 14.6 puts scoring after `_apply_result`'s practice return, because
    the auto-repeat practice loop would otherwise farm the score in minutes.
    """
    if outcome == OUT_WIN:
        return (BP_KO_WIN if ko else BP_HP_WIN) + max(0, int(winner_hp))
    if outcome == OUT_DRAW:
        return BP_DRAW
    return 0


def trainer_level(bp):
    """`level = cbrt(bp)`, clamped 1..999. Plan 14.2.

    Integer cube root by bisection - ten iterations at most, no floats anywhere,
    per plan 4.8. `bp ** (1/3)` would be a float, and the badge is one runtime of
    three that must agree; `int(999.9999997)` is 999 on one and 1000 on another,
    and that class of bug is the reason the whole model is integer-only.

    Cubic is chosen for a fast, satisfying early curve and a brutal tail: level 4
    is your first win, 12 is one camp, and 999 needs about nine million wins.

    NEVER STORE THE RESULT. Derive it on demand, every time - one less field to
    persist, migrate or corrupt, and it can then never disagree with the score it
    came from (same principle as innate actions, plan 4.3).
    """
    try:
        bp = int(bp)
    except (TypeError, ValueError):
        return 1
    lo, hi = 1, MAX_LEVEL
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if mid * mid * mid <= bp:
            lo = mid
        else:
            hi = mid - 1
    return lo


def level_progress(bp):
    """(level, bp into this level, bp the level spans, bp to the next).

    For the trainer screen's bar and the exact figure beside it (plan 8.1.2) -
    the number is what makes the bar mean anything, so both come from here
    rather than the bar being drawn from one calculation and labelled from
    another.

    `into + to_next == span` always holds where there IS a next level, which is
    what lets the bar and its label come from one call. At MAX_LEVEL there is no
    next level and the last three are 0: a full bar with nothing to fill, so
    callers must not divide by the span without checking it.
    """
    # Coerced HERE as well as in trainer_level, which coerces its own copy and
    # cannot hand the cleaned value back. Without this, junk off a hand-edited
    # trainer file gets a level (trainer_level swallows it) and then throws on
    # `bp - base` one line later - a crash while drawing a screen, which is the
    # severity class that outranks everything else in the review plan.
    try:
        bp = max(0, int(bp))
    except (TypeError, ValueError):
        bp = 0
    lvl = trainer_level(bp)
    if lvl >= MAX_LEVEL:
        return lvl, 0, 0, 0
    # Level 1 starts at 0 bp, not at 1**3, and this is the clamp in trainer_level
    # showing through rather than an off-by-one: cbrt(bp) floors to 0 for bp 0..7
    # and the clamp lifts all of it to level 1, so level 1 genuinely spans EIGHT
    # scores (0..7) while every level above it spans its own cube upward. Using
    # 1**3 as the base here made `into` negative at 0 bp; clamping that to zero
    # hid the negative and left the bar one bp short of its own span, so a brand
    # new trainer's bar would never read empty and none of it would add up.
    base = 0 if lvl <= 1 else lvl * lvl * lvl
    nxt = (lvl + 1) * (lvl + 1) * (lvl + 1)
    return lvl, bp - base, nxt - base, nxt - bp


# --- action collection (plan 14.3) -----------------------------------------
# You inherit from your OWN mons. There is no trophy mechanic: beating an opponent
# takes nothing from them, and that was explicitly rejected - do not reintroduce
# it without the owner asking.


def collect_legacy(pet):
    """A mon is leaving. Take what it bequeaths into the trainer pool.

    Returns the action id granted, or None if nothing was. Callers use the return
    only to decide whether to tell the player; NOTHING depends on it.

    Called on BOTH ways a mon can leave - death and humane replacement - because
    if only death granted the legacy the game would actively punish good care: you
    would have to neglect a mon you liked in order to progress. A mon can be
    retired at any point after adulthood instead.

    **The adult gate is the anti-farm measure and it is the whole design.** A
    newborn reaches death-risk in about an hour, so if any death granted the trait
    action, the fastest route to a full pool would be deliberate neglect - all
    five traits farmable in one long evening of killing pets, which is the exact
    opposite of what a Tamagotchi is for. Requiring ADULT (6 h+) kills that
    outright, because you cannot rush six hours of on-time, and it makes the
    inheritance a reward for RAISING something.

    Six hours is deliberately the same gate battle.py uses to allow a fight
    (battle.py's `_life_stage(...) not in ("adult", "elder")` check): a mon old
    enough to fight is old enough to bequeath. If this ever feels too easy, RAISE
    THE GATE rather than adding a cooldown - the age requirement is what makes it
    unfarmable, and a cooldown would only slow down someone already farming.

    Writes the trainer file on the transition and never otherwise (14.4).
    """
    if not isinstance(pet, dict):
        return None
    # Coerced before the gate, because _life_stage compares against ints and a
    # string age raises TypeError inside it. app.py wraps this call, so that
    # would be caught rather than fatal - but a helper should not need its caller
    # to be careful, and this one is reached from the pet-death path where the
    # history entry is being written.
    #
    # Fails CLOSED: an age we cannot read grants nothing. That is the
    # unexploitable direction - the gate exists so a young death leaves nothing,
    # so anything unreadable must land on the same side as young.
    try:
        age = float(pet.get("age", 0))
    except (TypeError, ValueError):
        return None
    # Same expression as the battle gate, not a re-derivation of "6 hours".
    if _life_stage(age) not in ("adult", "elder"):
        return None          # died or retired too young: leaves nothing
    aid = TRAIT_ACTION.get(pet.get("trait"))
    if aid is None:
        return None          # no trait, or one this build does not know
    tr = load_trainer(N_ACTIONS)
    pool = tr.get("pool")
    if not isinstance(pool, list):
        pool = []
    if aid in pool:
        return None          # inheriting a trait you already have is a NO-OP,
                             # not a duplicate, and not worth a file write
    if len(pool) >= MAX_POOL:
        return None          # cannot happen with 5 collectibles and a cap of 12,
                             # and is still checked: this writes to a file whose
                             # bounds the loader enforces, so respect them here.
    pool.append(aid)
    tr["pool"] = pool
    save_trainer(tr)
    return aid


def save_trainer(tr):
    """Write RARELY - on save, death, or collection. Never per frame (14.4)."""
    try:
        with open(TRAINER_PATH, "w") as f:
            f.write(json.dumps(tr))
    except Exception as e:
        print("Records: trainer save failed:", e)
