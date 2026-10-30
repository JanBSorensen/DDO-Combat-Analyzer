# Architecture

This document describes the code currently present in the Windows and Linux LiveReplay test builds.

## Main components

Both platform trees contain the same main application modules.

### DDO Combat Analyzer.pyw

Tkinter GUI and results interface. It controls recording, analysis, session history, configuration, detailed result views and Replay live test.

### continuous_live.py

Runs continuous live mode, including live screen tracking, OCR coordination, hotkey/session control and the transition between recording and post-analysis.

### live_overlay.py

Live DPS/APM overlay and live Combat Log processing. The current live path uses Indra OCR and visual line/scroll tracking.

### record_optimized.py

Records the selected Combat Log region at a 30 FPS target rate. Changed frames and forced anchors are stored losslessly for post-analysis.

The recorder identifies the current code line as v5.3.

### process_latest.py

Post-analysis reconstruction, OCR, parsing, statistics and report generation.

The post-analysis path includes Fast Grid/line-height handling, FastAlignment fallback, OCR batching, numeric correction, combat-event normalization, damage/DR/attack/crit statistics and DPS/APM calculations.

### replay_live_test.py

Runs the current live reconstruction/OCR/parser against a saved recording. Replay output uses separate replay_live_* files and does not overwrite the original live data.

### select_log_area_gui.py

Frozen-screen Combat Log area selection.

### overlay_positioner.py

Standard and custom overlay positioning relative to the DDO client window.

### cpu_utils.py

Physical CPU-core detection and OCR-thread selection helpers.

### launch_gui.py

Platform launcher and startup error handling.

### config.json

Default capture, tracking, analysis, live-DPS, OCR-thread, line-height, overlay and UI settings.

## Live pipeline

The current live processing path is:

~~~text
DDO Combat Log pixels
        |
        v
30 FPS visual tracking
        |
        v
grid phase + scroll detection
        |
        v
newly exposed line handling
        |
        v
Indra OCR
        |
        v
live parser + numeric validation/correction
        |
        v
rolling DPS/APM + live event files
~~~

The current implementation includes:

- dynamic current-frame grid phase,
- deferred partially visible bottom row,
- full-height recovery after scrolling,
- Stop/EOF fallback for a final pending row,
- fast scroll matching,
- FastAlignment second opinion for uncertain->zero cases,
- DDO comma-thousands validation,
- isolated OCR rechecks for suspicious numeric values,
- duplicated-one protection,
- raw and corrected live damage values.

## Post-analysis pipeline

The post-analysis path is:

~~~text
saved lossless changed frames
        |
        v
frame alignment / scroll reconstruction
        |
        v
visual line extraction
        |
        v
Indra OCR
        |
        v
combat-event parsing and normalization
        |
        v
reports, CSVs and GUI statistics
~~~

Post-analysis produces damage/healing events, target/type summaries, normalized events, DR/resistance observations, attack and crit data, incoming-defense data, rolling DPS/APM data, session summaries and timing/data-quality diagnostics.

## Focus handling

Live parsing is active only while DDO is the foreground window.

During a recording, focus interruptions are stored in **focus_interruptions.csv**. Post-analysis starts a new reconstruction segment after a focus gap instead of assuming that hidden Combat Log text was captured.

## Windows-specific code

The Windows build uses native Windows APIs for:

- DDO foreground-window detection,
- low-level recording hotkey handling,
- overlay behavior,
- process launching and focus handling.

The hotkey is consumed only while DDO is the foreground window.

## Linux-specific code

The Linux build adds **linux_x11.py**.

It handles:

- X11/EWMH DDO-window detection,
- foreground/focus checks,
- global Pause/Break/F-key hotkeys,
- X11 window positioning,
- Wine/Lutris/Steam/Proton window matching.

The Linux implementation targets Cinnamon/X11 and does not target Wayland.

## OCR

Both builds use Indra OCR.

Selectable OCR thread counts are Auto, 2, 4, 6 and 8. Auto resolves to:

- 2 threads on 1–2 physical CPU cores,
- 4 threads on 3 or more physical CPU cores.

The same thread setting is used by live and post-analysis OCR.
