# Attack and critical confirmation statistics V2

The V2 analyzer reports separate d20 attack and critical-confirmation distributions, preserving observed 1–20 rolls. Critical outcomes are `confirmed`, `resisted`, `failed`, or `unknown`.

- A resisted crit confirms the d20 roll but does not deal critical damage.
- Missing attack lines are not synthesized from confirmation messages. Inconsistent attack coverage suppresses attack-based rates and averages.
- Confirmation rolls are linked one-to-one only to recent attacks, with a strict 0.75 second / 24-line limit.
- Average crit damage is an explicitly labeled physical-event estimate, with per-attack audit exports.
- Both Windows and Linux use the same statistics logic. The separate .NET prototype is not changed.

This branch prepares the reviewed Python V2 code before merging into `main`.