# EMFMon

A Tamagotchi-style virtual pet for the [EMF Tildagon badge](https://tildagon.badge.emfcamp.org/).

Your pet is a randomly-coloured shape — one of eight (square, triangle, circle,
diamond, pentagon, hexagon, octagon or star) — born with its own **personality**.
It hatches as a tiny dot and **grows with your badge over time**, evolving through
life stages (baby → child → adult → crowned elder) while it wanders the screen and
three needs — **Food, Fun, Clean** — slowly drain. **Health** doesn't drain on its
own; it suffers when you let the other three run empty. Keep it fed, entertained
and clean, or it might not make it.

It runs in the **background**, so your pet keeps living (and needing you) even while
you're using other apps on the badge.

> 📖 **[Read the full manual](emfmon_manual.md)** for every number — exact decay
> rates, health-tick damage, death odds, and how long a neglected pet really has.

## Controls

| Button | Action |
| --- | --- |
| **UP** | Food |
| **DOWN** | Play |
| **RIGHT** | Clean |
| **LEFT** | Menu — Items / Battle / History / Rename / New pet |
| **CONFIRM** (C button) | Open your item pouch — pick and use an item in there |
| **CANCEL** | Exit |

The joystick's up/down/left/right mirror the corner buttons. The joystick **centre
press is ignored on the pet screen** (it was flaky enough to open the menu and
instantly pick Rename), but works inside the item pouch.

CONFIRM doesn't heal directly — it opens the pouch, so a stray press can't burn a
heal item.

## Needs & health

Your pet has four stats, all 0–100 (higher is better), shown as bars:

- **Food**, **Fun**, **Clean** drain in real time — roughly empty in **~10 / ~15 /
  ~20 minutes** respectively from full. Top each one up with its button.
- A need below **25%** turns its bar **red** and starts hurting **Health**.
- **Health** doesn't drain on its own. On a periodic health check it **drops by
  10 per need that's in the red** — so one slipped need costs 10, but letting all
  three go costs **30 a tick**. Neglect compounds. It slowly **recovers** (+6)
  while the pet is well looked after (all needs at 50%+).
- Health hitting **0 doesn't kill your pet** — below 20 it's rolled at **10% every
  20 minutes**, so there's usually time to rescue it. A typical adult survives
  around **4 hours** of unbroken neglect before the odds turn against it.
- The pet's **face reacts to its mood**: it smiles when happy, frowns when a need is
  low, and shows `X_X` when Health is critical.

Younger pets are more **fragile** — their health checks come faster and hit a little
harder — so newborns need closer attention than grown pets.

## Personalities

Every pet is **born with a personality** that tweaks how fast some of its needs
drain. It's shown as a subtitle under the pet's name.

| Trait | Effect |
| --- | --- |
| **Greedy** | Food drains **1.6× faster** — feed it more often |
| **Playful** | Fun drains **1.6× faster** — gets bored quickly |
| **Messy** | Clean drains **1.6× faster** — gets grubby fast (more poop) |
| **Tidy** | Clean drains **0.5×** — stays clean, barely poops |
| **Hardy** | Food, Fun **and** Clean drain **0.7×** — low-maintenance all round |

Personality is fixed for a pet's life and only affects **need decay** — Health is
never directly changed by it.

## Life stages

As it ages (in on-time hours), your pet **evolves** through four stages. Stages are
cosmetic — they change how the pet looks, not the difficulty.

| Stage | Age | Look |
| --- | --- | --- |
| **Baby** | 0–2 h | tiny, big eyes, no mouth |
| **Child** | 2–6 h | small, bigger cute eyes + full face |
| **Adult** | 6–48 h | full-size, normal face |
| **Elder** | 48 h+ | normal face + a little **gold crown** 👑 |

The pet also **grows in size** from a tiny dot to full size over its first ~12 hours
of on-time, independent of the stage it's in.

**Older pets are hardier**: each hour of age reduces need-decay by ~5% (down to a
floor), so a well-aged pet is easier to keep happy than a demanding newborn — even a
Greedy one mellows with age.

## More mechanics

- **Poop**: your pet leaves a brown dot each time it gets a bit dirtier. The **Clean**
  action wipes them all away.
- **Heal inventory**: you gain **two Small Heals every 30 minutes** of on-time,
  **+15 Health** each, stored up to 30. Open the pouch with **C** and use one — the
  game refuses rather than wastes it if you're already at full HP. Bigger heals
  exist in the game but have no source yet.
- **`mon!` tag**: a persistent alert appears on the home screen when a need is getting
  low, even while EMFMon is in the background, so you know when to check in.
- **History**: past pets are remembered — their name, shape, and how old they got.
- **Rename**: give your pet a name via the badge's text entry (Menu → Rename).
- **New pet**: hatch a fresh one any time from the menu (the old one is logged to history).

Note: the badge has no real-world clock, so the pet ages, grows and decays over the
time the badge is **switched on and running** — not wall-clock time while it's powered
off.

## Battles (Menu → Battle)

Your pet can **fight other badges** over **Bluetooth LE** — no WiFi, no network, no
pairing. Just two badges near each other.

- **Find opponent** — your badge lists nearby EMFMon players; pick one and send an
  invite, and they get to **accept or decline**. Once both agree, the mons trade shots
  and the winner is decided by a small **strength** edge on a forgiving coin-flip (even
  a weak pet wins about a quarter of the time). The **loser drops to 25% health** and
  the **winner to 75%**, so battling burns through your heal items.
- **Practice** — a free solo spar against a random opponent: it plays out fully but
  **costs no health and doesn't count** toward your ranked record.
- **Records** — a **Ranked** tally (with a scrolling list of who you beat/lost to) and a
  separate **Practice** win/loss counter.
- **Strength** is a mostly-hidden stat: pets are born middling and it **creeps up the
  longer you keep them healthy**, so a well-cared-for pet fights a little better.

To battle, your pet must be alive, an **adult (6h+)** and at **full health** — this
applies to **Practice too**, so a battered pet can't spar either.

There's no WiFi requirement: BLE advertises on fixed channels every badge listens
on. (Earlier versions used ESP-NOW, which followed each badge's WiFi association
and so struggled to find peers in a crowd — that's exactly why it was replaced.)

## Licence

MIT
