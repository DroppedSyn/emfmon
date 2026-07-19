# EMFMon

A Tamagotchi-style virtual pet for the [EMF Tildagon badge](https://tildagon.badge.emfcamp.org/).

Your pet is a randomly-coloured shape — one of eight (square, triangle, circle,
diamond, pentagon, hexagon, octagon or star) — born with its own **personality**.
It hatches as a tiny dot and **grows with your badge over time**, evolving through
life stages (baby → child → adult → crowned elder) while it wanders the screen and
four needs — **Health, Food, Fun, Clean** — slowly drain. Keep it fed, entertained,
clean and healthy, or it might not make it.

It runs in the **background**, so your pet keeps living (and needing you) even while
you're using other apps on the badge.

## Controls

| Button | Action |
| --- | --- |
| **UP** | Food |
| **DOWN** | Play |
| **RIGHT** | Clean |
| **LEFT** | Menu (rename / history / new pet) |
| **CONFIRM** (C button) | Heal — spends a heal item |
| **CANCEL** | Exit |

The joystick's up/down/left/right mirror the corner buttons.

## Needs & health

Your pet has four stats, all 0–100 (higher is better), shown as bars:

- **Food**, **Fun**, **Clean** drain in real time — roughly empty in **~10 / ~15 /
  ~20 minutes** respectively from full. Top each one up with its button.
- A need below **25%** turns its bar **red** and starts hurting **Health**.
- **Health** doesn't drain on its own. On a periodic health check it **drops** while
  any need is in the red, and slowly **recovers** while the pet is well looked after
  (all needs at 50%+). If Health falls low, the pet risks **dying** — so don't let
  needs sit red for long.
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
- **Heal inventory**: you gain **one heal item every 30 minutes** (stored up to a cap).
  Spend one with the **C button** to top up Health — but not while already at full HP.
- **`mon!` tag**: a persistent alert appears on the home screen when a need is getting
  low, even while EMFMon is in the background, so you know when to check in.
- **History**: past pets are remembered — their name, shape, and how old they got.
- **Rename**: give your pet a name via the badge's text entry (Menu → Rename).
- **New pet**: hatch a fresh one any time from the menu (the old one is logged to history).

Note: the badge has no real-world clock, so the pet ages, grows and decays over the
time the badge is **switched on and running** — not wall-clock time while it's powered
off.

## Licence

MIT
