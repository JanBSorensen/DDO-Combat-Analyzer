# DDO Combat Analyzer

DDO Combat Analyzer is a non-invasive combat analysis tool for **Dungeons & Dragons Online (DDO)**.

It reads the visible Combat Log from screen capture and provides live and post-combat analysis using **Indra OCR**.

## Repository contents

- **Windows version:** `src/windows/`
- **Linux version:** `src/linux/`
- **Architecture summary:** `docs/ARCHITECTURE.md`
- **Troubleshooting:** `docs/TROUBLESHOOTING.md`

## Main functions

- Select a Combat Log capture area.
- Record the Combat Log at high frequency.
- Show live rolling DPS/APM.
- Run post-combat analysis from the saved recording.
- Browse and reanalyze saved sessions.
- Replay the live-analysis pipeline on an existing recording.
- Show per-target combat details.

## OCR and capture

The current code uses:

- Indra OCR
- MSS
- OpenCV
- NumPy
- FFmpeg

## Windows

The Windows version includes:

- GUI launcher
- setup/start scripts
- global recording hotkey
- live overlay
- session recording and analysis

See `src/windows/README.txt`.

## Linux

The Linux version targets **Linux Mint 22.3 Cinnamon on X11** and DDO running through Wine/Lutris or Steam/Proton.

It includes Linux-specific X11 window, focus, hotkey and overlay handling.

See `src/linux/README.txt`.

## Disclaimer

This is an independent community project and is not affiliated with or endorsed by Standing Stone Games.
