# EMFMon

A Tamagotchi-style virtual pet for the [EMF Tildagon badge](https://tildagon.badge.emfcamp.org/).

Your pet is a randomly-coloured square or triangle with a little face. It wanders
the screen and has four needs — **Health, Food, Fun, Clean** — that slowly drain
over real time. Keep it fed, entertained, clean and healthy, or it might not make it.

## Controls

| Button | Action |
| --- | --- |
| **UP** | Food |
| **DOWN** | Play |
| **RIGHT** | Clean |
| **CONFIRM** | Heal (injection) |
| **LEFT** | Menu (rename / history / new pet) |
| **CANCEL** | Exit |

## Features

- Needs decay in real time (food gets hungry in ~10 min); bars turn **red** below 25%
- The pet's **face** reacts to its mood — smiles when happy, frowns when a need is
  low, and shows `X_X` when its health is critical
- A persistent **`!` icon** appears on the home screen (like the battery icon) when
  the pet needs attention, even when the app is in the background
- Grows from a tiny dot to full size as it ages
- Persists to the badge filesystem; keeps a **history** of past pets and how old
  they got
- Rename your pet using the badge text entry

## Licence

MIT
