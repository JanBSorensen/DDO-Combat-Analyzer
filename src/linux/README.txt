DDO COMBAT ANALYZER - LINUX MINT 22.3 / X11 TEST BUILD
======================================================

TARGET PLATFORM
---------------
This build is intended for:
- Linux Mint 22.3 Cinnamon, 64-bit
- X11 session (Wayland is not targeted in this test build)
- DDO running through Wine/Lutris or Steam/Proton
- The analyzer itself runs natively on Linux, outside Wine/Proton

FIRST USE
---------
1. Extract the entire ZIP to a normal folder.
2. Run "SETUP DDO Combat Analyzer.sh" once.
3. Setup installs the Linux system dependencies, creates a local .venv and installs
   MSS, OpenCV, NumPy, Indra OCR and python-xlib.
4. Setup also creates a Cinnamon application-menu launcher.
5. Start the analyzer with the menu entry or with START_DDO_COMBAT_ANALYZER.sh.

IMPORTANT: Do not run SETUP or START directly from inside the ZIP.

X11 / DDO WINDOW HANDLING
-------------------------
The Linux build detects the DDO game window through X11/EWMH and Wine window
metadata. It supports DDO launched through Wine/Lutris and Steam/Proton as long as
the actual game window is visible on the current X11 desktop.

If the analyzer says that DDO cannot be found:
- make sure the game client itself is running, not only the launcher;
- make sure you are logged in far enough for the DDO game window to exist;
- make sure the current desktop session is X11 rather than Wayland.

CONTINUOUS LIVE MODE
--------------------
Continuous live DPS is enabled by default.

- Rolling DPS/APM window: 5, 10, 15, 20, 30, or 60 seconds; 15 seconds is default.
- While idle, combat screenshots and session CSV files are not saved.
- Default recording hotkey: Pause/Break.
- The hotkey can also be changed to an F-key in the Recording panel.
- The X11 hotkey is grabbed only while DDO is the foreground application. Outside
  DDO the key is left alone.
- Press the hotkey once to start recording and again to stop and analyze.
- Live DPS can continue while post-analysis runs.
- Final Session DPS is shown briefly in the overlay after analysis.

OVERLAY
-------
The overlay is positioned relative to the DDO window and follows it when it moves.
Standard positions and custom positioning are available.

Linux/X11 does not use the Windows chroma-key overlay path. The Linux overlay uses
a conservative translucent dark Tk window. It is shown only while DDO is the
foreground application and is hidden when DDO loses focus.

Overlay font sizes: 12, 18, 24, 30, 36.

OCR THREADS
-----------
OCR threads can be set to Auto, 2, 4, 6, or 8.
Auto policy:
- 1-2 physical CPU cores -> 2 Indra threads
- 3+ physical CPU cores  -> 4 Indra threads

The same selection is used for live and post-analysis OCR. Batch size remains 12.

CAPTURE / COMBAT LOG AREA
-------------------------
This distribution contains no preconfigured Combat Log area.
Use "Select / adjust log area" before recording or using Continuous live DPS.

The validated reference area is about 885 x 522 px. A selection below 70% of that
width OR height is allowed, but the program warns because clipped or too-few rows
can reduce OCR/reconstruction reliability.

A dark opaque Combat Log background gives the most reliable OCR.

LIVE RECONSTRUCTION
-------------------
The current live pipeline includes:
- dynamic current-frame grid phase;
- deferred partially visible bottom row;
- full-height recovery after scrolling;
- Stop/EOF fallback for the final pending row;
- fast live scroll matching with robust FastAlignment second opinion on
  uncertain->zero frames;
- DDO number-format verification and isolated OCR rechecks for suspicious values.

POST ANALYSIS
-------------
The Linux build uses the same Indra OCR, reconstruction, parsing and report logic
as the corresponding Windows build, including Fast Grid, FastAlignment and the
EOF bottom-line fix.

Important output includes:
- COMBAT_REPORT.txt
- combat_log.txt
- damage_events.csv
- normalized_events.csv
- damage_by_target.csv
- outgoing_damage_by_type.csv
- damage_by_target_and_type.csv
- mitigation_by_target_and_type.csv
- dps_rolling.csv
- live_dps.csv
- live_damage_events.csv
- attack_events.csv
- attack_roll_distribution.csv
- crit_by_target.csv
- incoming_defense_events.csv
- incoming_defense_by_source.csv
- apm_rolling.csv
- analysis_summary.json
- post_analysis_timings.csv
- focus_interruptions.csv

REPLAY LIVE TEST
----------------
"Replay live test" runs the CURRENT live reconstruction/OCR/parser against the
saved video of an existing session. It writes separate files:
- replay_live_summary.json
- replay_live_damage_events.csv
- replay_live_dps.csv
- replay_live_tracking.csv
- replay_live_ocr_scan.csv

Replay waits for OCR capacity rather than dropping lines, so it tests algorithmic
live correctness rather than whether a particular PC can maintain real-time OCR.

SESSION HISTORY / REANALYSIS
----------------------------
"Open session..." lists recordings stored in this installation. Existing sessions
can be reopened or reanalyzed from their saved capture video. "Reanalyze selected"
remains disabled when no usable session is selected.

FOCUS BEHAVIOUR
---------------
Live screen parsing pauses whenever DDO is not the active X11 window. It does not
OCR whatever application happens to cover the Combat Log. Recording focus gaps are
stored in focus_interruptions.csv, and post-analysis starts a fresh reconstruction
segment after a focus gap.

SETUP FILES
-----------
There is deliberately only one START script:
  START_DDO_COMBAT_ANALYZER.sh

The setup script is:
  SETUP DDO Combat Analyzer.sh

Setup creates a .venv inside the program folder and a user-level Cinnamon desktop
launcher under ~/.local/share/applications.

TROUBLESHOOTING
---------------
- Startup errors: _internal/startup_error.log
- Launcher console log: _internal/startup_console.log
- Live overlay errors for a session: live_overlay_error.txt
- FFmpeg recording errors for a session: ffmpeg.log

If an older test build left an analyzer helper running, close that old Python
process once or log out/restart. New helpers watch the parent GUI and also stop on
control-pipe closure.

KNOWN PLATFORM LIMITATION
-------------------------
This package is specifically an X11 build. Do not use it under Wayland for testing
capture, global hotkeys or DDO focus detection; select Cinnamon (X11) at login.
