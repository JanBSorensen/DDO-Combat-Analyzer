# DDO Combat Analyzer

DDO Combat Analyzer is a non-invasive combat analysis tool for **Dungeons & Dragons Online (DDO)**.

It reads the visible Combat Log from screen capture and provides live and post-combat analysis using **Indra OCR**. **Indra Direct ONNX and Light mode** are now the defaults for both new and updated installations.

## Downloads

Download the current platform-specific release ZIP (no Git installation required):

- **[Windows ZIP](https://github.com/JanBSorensen/DDO-Combat-Analyzer/releases/latest/download/DDO-Combat-Analyzer-Windows.zip)** — extract all files and run `SETUP DDO Combat Analyzer.cmd` once, followed by `START DDO Combat Analyzer.cmd`.
- **[Linux ZIP](https://github.com/JanBSorensen/DDO-Combat-Analyzer/releases/latest/download/DDO-Combat-Analyzer-Linux.zip)** — for Linux Mint/Cinnamon on X11. Extract and run `bash "SETUP DDO Combat Analyzer.sh"`, then `./START_DDO_COMBAT_ANALYZER.sh`.

Both ZIPs are built together from the same `main` commit whenever this repository is updated.
See the [GitHub Releases page](https://github.com/JanBSorensen/DDO-Combat-Analyzer/releases) for version history.

## Repository contents

- **Windows version:** `src/windows/`
- **Linux version:** `src/linux/`
- **Architecture summary:** `docs/ARCHITECTURE.md`
- **Troubleshooting:** `docs/TROUBLESHOOTING.md`

## Full and Light modes

Choose **Light** (default) or **Full** from the Recording panel on Windows or
Linux Mint/Cinnamon X11. Both modes use the same selected Combat Log width
and the same Indra damage, attack, critical-hit and sneak-attack parsing.

- **Full:** 30-FPS (or configured) tracking, normal OCR threads, lossless FFV1
  video, independent post reanalysis and Replay Live.
- **Light:** 20-FPS live tracking, maximum 2 OCR threads, no independent
  FFmpeg/video capture, no redundant per-row screenshot diagnostics. Live
  OCR results are saved and can be rebuilt, but cannot be independently
  replayed or reanalyzed from video.

Each session includes a comparable performance summary; CPU/RAM data only
cover the live OCR helper, not DDO, GUI, FFmpeg or the entire computer.

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

- Indra Direct (recognition-only ONNX, default), or Indra Full (fallback)
- ONNX Runtime
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

## License

The original DDO Combat Analyzer source code in this repository is licensed under the
[Apache License, Version 2.0](LICENSE) (`Apache-2.0`).

Third-party programs, Python packages, OCR model weights, and game assets have
their own license terms and are **not relicensed** under Apache 2.0.
See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for details.

In particular, the Indra OCR *code* is Apache-2.0, but its downloadable
*model weights* use a modified OpenRAIL-M license with additional conditions.
FFmpeg's license depends on the executable build used.

## Disclaimer

This is an independent community project and is not affiliated with or endorsed by Standing Stone Games.


### November 2026: Light default and quality safeguards

Light and Indra Direct are selected by default for both new and upgraded Windows and Linux installations (20 FPS, up to 2 OCR threads). A one-time migration updates these two preferences without touching capture area, other settings or saved sessions; subsequent user selections are preserved. Full remains available for FFV1 video, independent Reanalyze and Replay Live. An **opaque black DDO Combat Log background is required** for reliable OCR. The analyzer warns about nonblack backgrounds and improves wrapped damage/sneak attack parsing, B/8 numeric OCR verification and event coverage diagnostics.
