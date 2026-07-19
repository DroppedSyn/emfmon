# EMFMon

A Tamagotchi-style virtual pet for the [EMF Tildagon badge](https://tildagon.badge.emfcamp.org/).

Your pet is a randomly-coloured shape — one of eight (square, triangle, circle,
diamond, pentagon, hexagon, octagon or star) — born with its own **personality**.
It hatches as a tiny dot and **grows with your badge over time**, evolving through
life stages (baby → child → adult → crowned elder) while it wanders the screen and
four needs — **Health, Food, Fun, Clean** — slowly drain. Keep it fed, entertained,
clean and healthy, or it might not make it.

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

## Features

- Four needs — **Food, Fun, Clean, Health** — with bars that turn **red** below 25%
- **Real-time decay**: Food gets hungry in ~10 min, Fun ~15, Clean ~20
- The pet's **face reacts to its mood** — smiles when happy, frowns when a need is
  low, and shows `X_X` when its health is critical
- **Grows** from a tiny dot to full size the more it's looked after, through
  visible **life stages** — baby (0-2h, big eyes, no mouth) → child (2-6h) →
  adult (6-48h) → **elder** (48h+), who wears a little gold crown
- A **personality** each pet is born with, tweaking how fast its needs drain —
  **Greedy** (hungry fast), **Playful** (bored fast), **Messy** (dirties fast),
  **Tidy** (stays clean) or **Hardy** (low-maintenance); shown under its name
- Leaves **poop** as it gets dirty; the Clean action wipes it away
- A **heal inventory** — you gain one heal item every 30 min; spend one with the
  C button to top up Health
- A persistent **`mon!` tag** appears on the home screen when the pet needs
  attention, even while the app is in the background
- **Persists** across sessions; keeps a **history** of past pets and how old they got
- **Rename** your pet using the badge text entry

Note: the badge has no real-world clock, so the pet ages and grows over the time
the badge is switched on and running — not wall-clock time while it's powered off.

## Licence

MIT
