# Troubleshooting

This document will grow as the Windows and Linux source trees are imported.

## Capture area

Reliable reconstruction depends on capturing enough of the Combat Log.

A very small capture region can make scroll reconstruction less reliable.

## Live DPS differs greatly from post-analysis

Live DPS is an estimate and may temporarily lag because OCR is performed asynchronously.

If live DPS is dramatically lower than post-analysis, useful diagnostics include:

- live OCR output,
- detected new-line counts,
- OCR queue backlog,
- parsed live events,
- post-analysis damage events.

## Windows console windows

Normal GUI use should not require persistent console windows. Platform-specific launchers should use no-console/no-window process creation where appropriate.

## Linux

Linux support depends on the user's Wine/Lutris/Steam setup and desktop environment. Window focus and overlay behavior may differ from Windows.
