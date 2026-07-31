# EMFMon — the full manual

A Tamagotchi-style virtual pet for the [EMF Tildagon badge](https://tildagon.badge.emfcamp.org/).

This is the complete reference: every number, every mechanic, and what they mean
in practice. If you just want the overview, the README is shorter. This document
is for people who want to know exactly how long they've got.

Accurate as of **v1.0.23 + BATTLE_EVO!** (unreleased).

> If a number here disagrees with the badge, the badge is right and this is a
> bug — please say so. A manual that has lied once stops being trusted for the
> numbers it still gets right.

---

## Contents

1. [The one rule that explains everything](#1-the-one-rule-that-explains-everything)
2. [Controls](#2-controls)
3. [Your pet](#3-your-pet)
4. [The four stats](#4-the-four-stats)
5. [Needs: how fast they drain](#5-needs-how-fast-they-drain)
6. [Health: how it's lost and regained](#6-health-how-its-lost-and-regained)
7. [Death: the real numbers](#7-death-the-real-numbers)
8. [Life stages and growth](#8-life-stages-and-growth)
9. [Personalities](#9-personalities)
10. [Items](#10-items)
11. [Poop](#11-poop)
12. [Battles](#12-battles)
13. [Actions](#13-actions)
14. [Action queues](#14-action-queues)
15. [Battlepoints, levels and collecting actions](#15-battlepoints-levels-and-collecting-actions)
16. [Pausing](#16-pausing)
17. [When it goes wrong](#17-when-it-goes-wrong)
18. [Etiquette](#18-etiquette)
19. [History, records and starting over](#19-history-records-and-starting-over)
20. [Quick reference](#20-quick-reference)

---

## 1. The one rule that explains everything

**Your pet only lives while the badge is on.**

The Tildagon has no real-world clock it can trust, so EMFMon counts **on-time**
— time the badge is actually switched on and running — and nothing else.

- Badge off overnight? Your pet did not age, did not get hungry, did not get
  sick. It was **stopped**, not neglected. (There is also a real Pause you can
  switch on while the badge stays awake — see §16.)
- Badge on in your pocket all afternoon? That's real time passing, and your pet
  felt every minute of it.

This is deliberate. Without it, plugging in to charge overnight would replay
eight hours of decay at once and wipe out every pet that wasn't ancient.

**Practical upshot:** a pet is never in danger from a badge sitting switched off
in a bag. It's in danger from a badge left switched **on** and forgotten.

---

## 2. Controls

| Button | Action |
|---|---|
| **UP** | Feed (+35 Food) |
| **DOWN** | Play (+35 Fun) |
| **RIGHT** | Clean (+40 Clean, wipes all poop) |
| **CONFIRM** (C) | Open your item pouch — you pick and use an item in there |
| **LEFT** | Menu: **Items · Battle · Trainer · History · Rename · New pet** |
| **CANCEL** | Exit |

CONFIRM doesn't heal directly — it opens the pouch, and using an item is a
second press inside it. That's deliberate: it stops a stray press burning a
heal. (Menu → Items goes to the same place.)

The joystick's up/down/left/right mirror the corner buttons. The joystick
**centre press is ignored on the pet screen** — it was flaky enough to open the
menu and instantly select Rename, so it's disabled there. It does work inside
the item pouch, where nothing destructive can happen.

Feeding, playing and cleaning are **free and unlimited**. Only healing costs an
item.

---

## 3. Your pet

Every pet is generated fresh with:

- **A shape** — one of eight, drawn in a random bright colour: square, triangle,
  circle, diamond, pentagon, hexagon, octagon, star. All equally likely.
- **A name** — four random letters. Rename it whenever you like.
- **A personality** — one of five, fixed for life ([§9](#9-personalities)).
- **A strength** — 4 to 7, middle-biased. It no longer affects battles;
  see the note at the top of [§12](#12-battles).

Its face reacts to how it's doing: content when well kept, a frown when a need
is low, and `X_X` when Health is below 25.

---

## 4. The four stats

All four run 0–100, higher is better.

| Stat | Drains on its own? | How to raise it |
|---|---|---|
| **Food** | yes | Feed (+35) |
| **Fun** | yes | Play (+35) |
| **Clean** | yes | Clean (+40) |
| **Health** | **no** | recovers on its own when well kept, or use an item |

**Health never drains by itself.** It only falls as a *consequence* of the other
three being neglected. That's the whole game: keep the three topped up, and
Health looks after itself.

A need below **25** turns its bar **red** and starts costing Health. A need
below **30** raises the `mon!` alert on the badge home screen — an early warning
that fires *before* any damage begins.

---

## 5. Needs: how fast they drain

From full to empty, at base rate:

| Need | Full → empty |
|---|---|
| Food | 10 minutes |
| Fun | 15 minutes |
| Clean | 20 minutes |

Two things slow this down.

**Age.** Each hour of age reduces decay by 5%, down to a floor of 10% of the
base rate. Older pets are dramatically easier to keep:

| Age | Decay rate | Food full → empty |
|---|---|---|
| 0 h (newborn) | 100% | 10 min |
| 6 h | 70% | 14 min |
| 12 h | 40% | 25 min |
| 18 h + | 10% (floor) | 100 min |

**Personality.** Multiplies one or more needs ([§9](#9-personalities)).

So a newborn is a handful and an elder is nearly self-sufficient. This is the
main reason old pets survive neglect that would kill a young one.

---

## 6. Health: how it's lost and regained

Health changes only on the **health tick**, a periodic check. Young pets are
checked more often *and* take more damage:

| Age | Tick every | Damage per tick, 1 need red | 3 needs red |
|---|---|---|---|
| 0 h | 10 min | 16 | 48 |
| 2 h | 13 min | 15 | 45 |
| 6 h | 20 min | 13 | 39 |
| 12 h + | 30 min | 10 | 30 |

**Damage scales with how many needs are red.** One forgotten need costs 10 per
tick; all three cost 30. Letting everything slide is three times worse than
letting one thing slip — which is exactly as it should be, and wasn't true
before v1.0.23.

**Recovery:** if **all three** needs are at 50 or above at the moment of a tick,
Health regains **6**. That's slow — 0 to 100 is about 8.5 hours of good care —
so items are the practical way back up from a bad patch.

Note the gap between 25 and 50: with a need sitting in that band you take no
damage, but you don't heal either.

---

## 7. Death: the real numbers

**Health reaching 0 does not kill your pet.** This surprises people. Zero Health
only makes it *eligible* to die.

Once Health is **below 20**, the game rolls a **10% chance of death every 20
minutes**. A pet at 19 Health is in exactly as much danger as one at 0 — the
threshold is flat.

**Phase 1 — total neglect, until death is even possible:**

| Pet | First need red | All three red | Death rolls begin |
|---|---|---|---|
| Adult (16 h), messy | 38 min | 57 min | **2h 00m** |
| Adult (16 h), no trait | 38 min | 1h 16m | **2h 30m** |
| Child (6 h) | 11 min | 22 min | **1h 30m** |
| Newborn | 8 min | 16 min | **1h 00m** |

**Phase 2 — the rolls themselves**, once Health is under 20:

| Outcome | Rolls | Time |
|---|---|---|
| 25% of pets dead by | 3 | 1h 00m |
| **half dead by** | 7 | **2h 20m** |
| 75% dead by | 14 | 4h 40m |
| 90% dead by | 22 | 7h 20m |
| average | 10 | 3h 20m |

**Total, for a typical adult: about 4h 20m of unbroken neglect** before it's
more likely dead than not. A quarter of pets cling on past 6h 40m purely on
luck.

Remember [§1](#1-the-one-rule-that-explains-everything): that's 4h 20m of the
badge being **switched on**. Powered off, it's indefinite.

**If you find your pet on empty:** it is very probably still alive, and you have
time. Feed, play and clean immediately — that stops the damage at the next tick
— then spend heal items to climb back out of the danger band.

---

## 8. Life stages and growth

| Stage | Age | Appearance |
|---|---|---|
| **Baby** | 0–2 h | tiny, big eyes, no mouth |
| **Child** | 2–6 h | small, big eyes, full face |
| **Adult** | 6–48 h | full size, normal face |
| **Elder** | 48 h + | normal face plus a gold crown |

Stages are **cosmetic** — they change how the pet looks, not the rules. What
actually changes with age is decay rate ([§5](#5-needs-how-fast-they-drain)) and
health-tick fragility ([§6](#6-health-how-its-lost-and-regained)), both of which
improve continuously rather than at stage boundaries.

Separately, the pet **grows in size** from a dot to full size over its first
**12 hours** of on-time.

One rule does key off stage: you must be an **adult (6 h+)** to battle.

---

## 9. Personalities

Fixed at birth, shown on the pet screen. Affects **need decay only** — never
Health directly.

| Trait | Effect | In practice |
|---|---|---|
| **Greedy** | Food drains 1.6× | feed it constantly |
| **Playful** | Fun drains 1.6× | bores fast |
| **Messy** | Clean drains 1.6× | grubby, lots of poop |
| **Tidy** | Clean drains 0.5× | stays clean, barely poops |
| **Hardy** | all three drain 0.7× | the easy mode pet |

Age reduction applies on top, so even a Greedy pet mellows considerably by
elderhood.

---

## 10. Items

Open the pouch with **CONFIRM** (or Menu → Items) and use one from there. You
can't heal a pet already at full Health — the item is refused rather than
wasted.

| Item | Restores | Carry cap | How you get it |
|---|---|---|---|
| **Small Heal** | 15 | 30 | **2 every 30 minutes** of on-time |
| **Heal** | 30 | 30 | not yet obtainable |
| **Medium Heal** | 50 | 20 | not yet obtainable |
| **Greater Heal** | 100 | 5 | not yet obtainable |

Only Small Heals are granted by time. The larger ones exist in the game and will
have sources later (battle rewards, trades) — for now they're reserved.

**Income vs cost:** 2 Small Heals per 30 minutes is 60 Health per hour of
on-time. A lost battle costs 75 Health. So battling is roughly self-funding if
you fight about once an hour, and a drain if you fight more.

---

## 11. Poop

Your pet drops a poop dot each time Clean falls another 25 points, up to 4 on
screen at once. **Clean** wipes them all.

Purely cosmetic — poop is a *symptom* of low Clean, not an extra penalty. Messy
pets produce the most, Tidy pets almost none.

---

## 12. Battles

> **Strength no longer affects battles.** It was the whole of the old battle
> system — a hidden 1–10 stat that weighted one dice roll. Battles are decided
> by your queue now, and strength does nothing in them. It still grows (+1 per
> 2 hours held at 90+ Health, born 4–7, never falls) and it is still shown, but
> it is a record of how well you have looked after something rather than a
> weapon. Nothing else in the game reads it.

**Menu → Battle.** Two badges fight over **Bluetooth LE** — no WiFi, no network,
no pairing. Just be near each other.

> If you played an older EMFMon: battles used to be one dice roll weighted by a
> hidden Strength stat. That's gone. **You now bring a plan, and the plan
> fights.** Strength no longer affects battles at all — see the note below.

**To battle, your pet must be:**

- alive, and **not paused** (§16)
- an **adult** (6 h+)
- at **exactly 100 Health**

### How a fight actually works

You don't press anything during a fight. You decide everything *before* it, and
then you watch.

1. **Both badges show a 20-second selection window.** Pick which of your saved
   queues to bring. Both sides lock in — or get locked in automatically when the
   clock runs out.
2. **The badges swap sealed queues.** Neither can see the other's choice until
   both are committed, so nobody can counter-pick. (If you're curious: it's a
   hash commitment. If you're not: nobody can cheat, and that's the point.)
3. **The radio disconnects, and both badges fight the same battle separately.**
   Same seed, same rules, same result. This is why a fight survives you walking
   away mid-animation.
4. **20 seconds of combat, at 4 ticks per second** — 80 ticks, then time's up.

### The queue is a rotation, not a plan A

Your queue is **3 to 5 actions**, and it **loops**. A 3-action queue runs about
twice in a fight. There is no "saving" an action for later — everything you
picked will fire, repeatedly, in order.

**Cost is the number that matters, and it's the one everybody ignores.** Each
action costs ticks before your *next* one can fire. Disinfect costs 7 and comes
round constantly; Mud Sling costs 18 and you'll see it perhaps four times in a
whole fight. A queue of expensive actions looks devastating and does nothing,
because it barely fires.

**Repeating yourself is punished.** Firing the same action twice in a row is
**14% weaker** the second time. Alternating beats spamming.

### Both mons start on 100 HP

First to zero loses. If the 20 seconds run out with both alive, **the higher HP
wins** — an exact tie is a draw, which happens in well under 1% of fights.

**Elders (48 h+) get +2 HP.** That's the entire aura: no special move, no
damage bonus. It's small on purpose, and it is earned by keeping something alive
for two days.

**The cost is real:** winner drops to **75 Health**, loser to **25**. A draw
costs both of you the same as winning. A **no contest** costs nothing at all
(§17).

### Practice

A free spar against a randomly generated opponent. Plays out fully, costs no
Health, and is tracked on its own counter that never touches your ranked record.

**Practice awards no battlepoints, ever.** That's deliberate — you can practice
as often as you like, and it would otherwise be a way to farm a score by
pressing one button repeatedly.

---

## 13. Actions

Six actions. Everyone is born knowing **Tackle** plus **one** decided by their
personality (§9), and you collect the rest the long way (§15).

| Action | Cost | Effect | What it's for |
|---|---|---|---|
| **Tackle** | 8t | 17–23 damage | Cheap and steady. Fires often, and often is what wins. |
| **Disinfect** | 7t | heals 10–14 | The cheapest thing there is, so it comes round faster than anything else. |
| **Brace** | 8t | blocks 10, for 14 ticks | For cowards. Cowards win a lot of fights. |
| **Gobble** | 12t | 15–21 damage, steals 30% | Damage that pays you back. |
| **Prank** | 12t | 13–17 damage, delays them 4 ticks (cap 14) | Buys time rather than dealing it. |
| **Mud Sling** | 18t | 13–17 damage, bleeds for 20 ticks | Expensive, and it keeps working after it lands. |

**Which one you're born with:**

| Personality | Action |
|---|---|
| Hardy | Brace |
| Tidy | Disinfect |
| Greedy | Gobble |
| Playful | Prank |
| Messy | Mud Sling |

You can read all of this on the badge: **Action queues → LEFT**.

---

## 14. Action queues

**Menu → Battle → Action queues.**

- **Row 0 is your default queue**, generated from your mon's personality. You
  can't edit it and you don't need to — it always works, so you can never be
  left with nothing to field.
- **Build your own** with the bottom row. Push RIGHT to edit a saved queue,
  then cycle each slot.
- **3 to 5 actions.** Fewer isn't allowed; more won't fit on the radio.

**A queue you can't field shows a big red `!`.** Queues outlive mons — the
greedy mon that could field Gobble dies, the tidy one that replaces it can't,
and the queue is still sitting there. Rather than silently rewriting your build,
the badge marks it and uses your default for that fight. Collect the action
(§15) and the queue works again, permanently.

---

## 15. Battlepoints, levels and collecting actions

Your **mon** is temporary. Your **trainer record** is not — it survives every
death, every replacement, every fresh start. **Menu → Trainer.**

### Battlepoints

Ranked wins score. Nothing else does.

| Result | Points |
|---|---|
| Win by knockout | **100 + your remaining HP** (so 101–142) |
| Win on HP at the time cap | 50 + your remaining HP |
| Draw | 25 |
| Loss | 0 |
| Practice | **0, always** |

A win on 40 HP scores 140; a win on 2 HP scores 102. Surviving comfortably is
worth more than surviving barely, which is the whole idea.

Battlepoints are a **score, not a currency.** Nothing is bought with them, they
never go down, and losing costs you nothing but the points you didn't win.

### Trainer level

**Level is the cube root of your battlepoints**, capped at 999.

| Level | Points needed | Roughly |
|---|---|---|
| 4 | 64 | your first win |
| 12 | 1,728 | a good weekend |
| 20 | 8,000 | about 70 wins |
| 50 | 125,000 | over a thousand |
| 999 | 997,002,999 | nine million wins. Good luck. |

Early levels come fast and then it gets brutal, which is intentional.

### Collecting actions

You start knowing two actions and can end up knowing all six. **You inherit them
from your own mons.**

When a mon **reaches adulthood (6 h+) and then leaves you** — by dying, or by
being replaced — it leaves you its personality's action, permanently. That
action is then available to *every* mon you ever raise, whatever its
personality.

Two things worth knowing, because both are deliberate:

- **A mon that dies young leaves nothing.** The gate is adulthood, so there is
  no shortcut in hatching and neglecting mons in a loop. You cannot rush six
  hours.
- **Retiring a mon counts exactly the same as it dying.** Replace a mon you love
  at any point after it grows up and you still get its action. Good care is
  never the slower route.

Beating someone takes nothing from them. There are no trophies and nothing to
lose.

Expect it to take a while: personalities are random, so collecting all five
inherited actions takes about **eleven adult mons** on average, not five.

---

## 16. Pausing

**F on the pet screen → Pause.**

A paused mon is **completely stopped**: no hunger, no health loss, no ageing, no
growing, and no death roll. The screen says so, in blue, and the "!" alert goes
quiet.

This is for using your badge as a badge. EMFMon keeps simulating in the
background while you're in other apps, so an afternoon in the schedule app is an
afternoon of decay — pausing is how you say "hold it there" without switching
the badge off entirely.

**Pause means nothing happens, with no exceptions.** You can't feed a paused
mon, heal it, clean it or battle with it. Resume first — it's the same menu, and
the row will say `Resume`.

One thing it is not: an escape. Pausing at 0 Health **freezes** the danger, it
doesn't clear it. Resume and the death roll is still coming.

---

## 17. When it goes wrong

Battles fail sometimes. Radios are radios, and a camp full of badges is a hostile
place for one.

**None of these cost you anything.** Not health, not a loss on your record, not
points. Your mon is exactly as it was.

| Message | What happened |
|---|---|
| **NO CONTEST** | Something went wrong mid-battle — the link dropped, they walked off, or their badge sent something the protocol refused. No result, no cost. |
| **NO ANSWER** | They never responded to the invite. Possibly they didn't notice. |
| **CANCELLED** | One of you backed out before it started. |
| **TOO OLD** | Their badge is running an older EMFMon that doesn't speak this battle system. |
| **VERSION ERROR** | Both badges speak the new system but disagree about the rules. One of you needs an update. |

### If the badge restarts while you are looking for an opponent

**It is not your mon, and nothing is lost.** Your pet, your record, your
battlepoints and your queues are all written to storage and survive a restart —
you will land back at the launcher and can open EMFMon again exactly where you
were.

This is a fault in the badge's own software rather than in EMFMon: **any** app
that keeps the Bluetooth radio switched on can trigger it, and it has been
reproduced with a twenty-line test app containing none of EMFMon's code. It is
being chased separately.

Until it is fixed, the practical advice is the boring kind: **don't leave the
Find opponent screen open for long stretches.** Go in, find your opponent,
fight, come out. Practice and everything else on the badge are unaffected —
the radio is only up while you are actually looking for someone.

If you see **NO CONTEST** repeatedly with one particular person, it's almost
certainly distance. Get closer and try once more.

---

## 18. Etiquette

None of this is enforced by the badge. All of it decides whether the game is
pleasant to be near.

**Ask first, out loud.** A challenge is a request for someone's attention and a
bite out of their battery. Saying "fancy a fight?" takes two seconds and turns
an ambush into a game. If you only remember one line from this section, that's
the one — the rest is elaboration.

**One invite, then wait.** The badge won't stop you sending a second, and that's
deliberate: rate-limiting would punish someone legitimately retrying after a
dropped link, which is bad radio rather than bad manners. So it's on you.

**A declined challenge isn't an insult.** Battery, a queue they're still
building, or a conversation they'd rather finish. No is a complete answer.

**Ask about their health first.** Losing costs 75 Health and it takes real time
to earn back. Someone nursing a mon has a genuine reason to say no, and it isn't
about you.

**Don't hover.** A fight resolves in twenty seconds, the badges have already
disconnected by the time you're watching the animation, and standing over
someone does not make you win.

**Elders are meant to be lopsided.** That +2 HP is two days of keeping something
alive. Fighting one is a choice, not a trap — and losing to one is the correct
outcome and worth doing anyway.

---

## 19. History, records and starting over

- **History** remembers your past pets — name, how old they got, their shape,
  and their final battle record. Kept for the 20 most recent.
- **New pet** hatches a fresh one at any time. A living pet you replace is filed
  to history first, so nothing is lost.
- When a pet **dies**, it's filed automatically and the badge tells you how old
  it was.

---

## 20. Quick reference

```
NEEDS          Food 10 min      Fun 15 min      Clean 20 min   (full -> empty, base)
               x0.05 slower per hour of age, floor 10% of base

RED            below 25   -> costs Health          "mon!" alert below 30
ACTIONS        Feed +35   Play +35   Clean +40   Small Heal +15

HEALTH TICK    every 10 min (newborn) -> 30 min (12 h+)
               -10 per RED NEED per tick at maturity (so -30 with all three)
               +6 per tick when all three needs are 50+

DEATH          only below 20 Health: 10% every 20 min
               typical adult: ~2 h neglect to reach risk, ~2h20m more to likely death

STAGES         baby 0-2 h    child 2-6 h    adult 6-48 h    elder 48 h+
GROWTH         dot -> full size over 12 h
ITEMS          2 Small Heals per 30 min, cap 30

BATTLE         adult + 100 Health + not paused.  Bluetooth LE, no WiFi needed.
               20s to pick a queue, then 20s of fight (80 ticks, 4/second).
               Both start on 100 HP.  Elder (48h+) gets +2.
               Winner -> 75 HP, loser -> 25 HP.  Draw costs the same as a win.
               NO CONTEST costs nothing at all.

QUEUE          3-5 actions, and it LOOPS.  Cost decides how often each fires.
               Same action twice in a row is 14% weaker.

ACTIONS        Tackle    8t   17-23 dmg
               Disinfect 7t   heals 10-14
               Brace     8t   blocks 10 for 14 ticks
               Gobble    12t  15-21 dmg, steals 30%
               Prank     12t  13-17 dmg, delays 4 (cap 14)
               Mud Sling 18t  13-17 dmg, bleeds 20 ticks

POINTS         KO win 100 + HP left   HP win 50 + HP left   draw 25   loss 0
               Practice ALWAYS 0.   Level = cube root of points, max 999.
COLLECTING     an ADULT mon that dies or is replaced leaves you its action,
               forever.  A mon that dies young leaves nothing.

STRENGTH       born 4-7, +1 per 2 h held at 90+ Health, max 10.
               Does NOT affect battles any more.

EVERYTHING counts ON-TIME only. A badge switched off is a pet stopped.
               There is also a real Pause in the exit menu - see section 16.
```

---

## Licence

MIT
