DDO COMBAT ANALYZER - LINUX
============================

TARGET
------
Linux Mint 22.3 Cinnamon on X11.

DDO may run through Wine/Lutris or Steam/Proton while the analyzer runs natively on Linux.

START
-----
First-time setup:
  SETUP DDO Combat Analyzer.sh

Normal start:
  START_DDO_COMBAT_ANALYZER.sh

MAIN FEATURES
-------------
- Combat Log area selection
- high-frequency recording
- live rolling DPS/APM
- post-combat analysis
- session history and reanalysis
- Replay live test
- per-target combat details

MAIN DEPENDENCIES
-----------------
- Python
- MSS
- OpenCV
- NumPy
- Indra OCR
- python-xlib
- FFmpeg
- xdotool
- wmctrl

SOURCE
------
Application source is stored in the _internal folder.

The current Linux implementation is for X11, not Wayland.
