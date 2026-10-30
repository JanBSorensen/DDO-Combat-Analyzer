# Architecture

The Windows and Linux versions share the same main application structure.

## Main files

- **DDO Combat Analyzer.pyw** — GUI and session/result views.
- **continuous_live.py** — continuous live mode and recording control.
- **live_overlay.py** — live DPS/APM overlay and live Combat Log processing.
- **record_optimized.py** — high-frequency Combat Log recording.
- **process_latest.py** — post-combat reconstruction, OCR and analysis.
- **replay_live_test.py** — reruns the live pipeline on a saved recording.
- **select_log_area_gui.py** — Combat Log area selection.
- **overlay_positioner.py** — overlay placement.
- **cpu_utils.py** — CPU-core detection and OCR-thread selection.
- **launch_gui.py** — application startup and error handling.
- **config.json** — default application settings.

## Live processing

Live mode tracks the Combat Log visually, identifies newly exposed lines, OCRs them with Indra, and updates rolling DPS/APM.

## Post-analysis

Post-analysis reconstructs the Combat Log from saved lossless frames, OCRs the reconstructed lines, and generates the session analysis.

## Platform-specific code

Windows uses native Windows APIs for window focus, hotkeys and overlay handling.

Linux adds **linux_x11.py** for X11 window detection, focus, hotkeys and positioning.
