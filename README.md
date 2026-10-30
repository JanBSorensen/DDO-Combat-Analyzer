# DDO Combat Analyzer

DDO Combat Analyzer is a non-invasive combat analysis tool for **Dungeons & Dragons Online (DDO)**. It reads the visible Combat Log from screen capture and uses **Indra OCR** for live and post-combat analysis.

This repository contains the current source snapshot for both platforms:

- **Windows:** DDO_Combat_Analyzer_Windows_LiveReplay_TEST
- **Linux:** DDO_Combat_Analyzer_Linux_Mint_22_3_LiveReplay_TEST

The recorder identifies the current code line as **v5.3**.

## Current features

### Continuous live mode

Continuous live DPS is enabled by default.

- Rolling DPS/APM window: 5, 10, 15, 20, 30, or 60 seconds; 15 seconds is the default.
- Default recording hotkey: Pause/Break.
- Recording can also be started and stopped from the GUI.
- Live DPS can continue while post-analysis runs.
- Final post-analysis Session DPS is shown in the overlay for 10 seconds.
- The overlay is shown only while DDO is the foreground window.
- Live parsing pauses while DDO is not in the foreground.
- Recording focus gaps are logged in **focus_interruptions.csv**.

### Live reconstruction and OCR

The current live pipeline uses:

- 30 FPS visual tracking,
- dynamic current-frame grid phase,
- deferred handling of a partially visible newest bottom row,
- full-height recovery after scrolling,
- Stop/EOF fallback for a final pending row,
- fast live scroll matching,
- FastAlignment as a second opinion on frames classified as uncertain->zero,
- Indra OCR,
- DDO number-format validation and isolated OCR rechecks for suspicious values,
- raw and corrected live damage values in **live_damage_events.csv**.

OCR threads are selectable as Auto, 2, 4, 6, or 8. Auto uses 2 threads on systems with 1–2 physical CPU cores and 4 threads on systems with 3 or more physical cores.

Line height can be Auto, 10, 12, 14, 16, 18, or 20 px.

### Post-analysis

Post-analysis uses Indra OCR and saved lossless capture data to reconstruct the Combat Log and calculate combat statistics.

Current output includes:

- total and per-target damage,
- damage by type,
- sneak attack statistics,
- blocked/resisted outgoing damage and observed DR/resistance types,
- d20 attack-roll distribution,
- critical-threat and confirmation information,
- incoming damage and explicit defensive outcomes,
- normalized chronological events,
- Session DPS,
- Recording DPS,
- Active combat DPS,
- rolling DPS/APM data,
- data-quality diagnostics.

### Session history and reanalysis

The GUI can:

- browse saved sessions,
- reopen already analyzed sessions without rerunning OCR,
- reanalyze a selected session from its saved capture,
- open the selected session folder,
- run **Replay live test** on a saved capture.

Replay live test runs the current live reconstruction/OCR/parser against an existing saved session and writes separate replay_live_* diagnostic files without overwriting the original live files.

### Mob details

Target rows can be opened in a detailed view containing:

- overall damage/hit information,
- natural d20 attack-roll table,
- observed hit-threshold evidence,
- critical confirmation details,
- damage by type,
- DR/resistance observations,
- incoming damage and explicit defensive outcomes.

## Windows

Source: [src/windows](src/windows)

The Windows build uses a low-level keyboard hook for the recording hotkey and requests Administrator privileges at startup so the hook can work when an elevated DDO client has focus.

Dependencies in the current package:

- MSS
- OpenCV
- NumPy
- Indra OCR
- FFmpeg

Detailed package instructions are in [src/windows/README.txt](src/windows/README.txt).

## Linux

Source: [src/linux](src/linux)

The current Linux build targets:

- Linux Mint 22.3 Cinnamon, 64-bit
- **X11**
- DDO through Wine/Lutris or Steam/Proton
- the analyzer running natively on Linux

Linux adds X11-specific window detection, focus handling, global hotkeys and overlay handling in **linux_x11.py**.

Dependencies in the current package include the Windows Python dependencies plus **python-xlib**; setup also uses FFmpeg, **xdotool** and **wmctrl**.

Detailed package instructions are in [src/linux/README.txt](src/linux/README.txt).

## Source layout

~~~text
src/
├─ windows/
│  ├─ README.txt
│  ├─ SETUP DDO Combat Analyzer.cmd
│  ├─ START DDO Combat Analyzer.cmd
│  └─ _internal/
│     ├─ DDO Combat Analyzer.pyw
│     ├─ continuous_live.py
│     ├─ cpu_utils.py
│     ├─ launch_gui.py
│     ├─ live_overlay.py
│     ├─ overlay_positioner.py
│     ├─ process_latest.py
│     ├─ record_optimized.py
│     ├─ replay_live_test.py
│     ├─ select_log_area_gui.py
│     ├─ config.json
│     └─ requirements.txt
└─ linux/
   ├─ README.txt
   ├─ SETUP DDO Combat Analyzer.sh
   ├─ START_DDO_COMBAT_ANALYZER.sh
   └─ _internal/
      ├─ DDO Combat Analyzer.pyw
      ├─ continuous_live.py
      ├─ cpu_utils.py
      ├─ launch_gui.py
      ├─ linux_x11.py
      ├─ live_overlay.py
      ├─ overlay_positioner.py
      ├─ process_latest.py
      ├─ record_optimized.py
      ├─ replay_live_test.py
      ├─ select_log_area_gui.py
      ├─ config.json
      └─ requirements.txt
~~~

## Capture area

The packages contain no preconfigured Combat Log area.

The validated reference area is approximately **885 × 522 px**. If the selected area is less than 70% of that width or height, the GUI warns the user but still allows the selection.

A dark, opaque Combat Log background gives the most reliable tracking and OCR.

## Current platform limitation

The Linux package is specifically an **X11** build. Wayland is not supported by the current Linux window/focus/hotkey implementation.

## Game integrity

The analyzer works from screen capture of the user-selected Combat Log region. It does not modify or inject into the DDO client.

## Disclaimer

This is an independent community project and is not affiliated with or endorsed by Standing Stone Games.
