# DDO Combat Analyzer

DDO Combat Analyzer is a non-invasive combat analysis tool for **Dungeons & Dragons Online (DDO)**.

It reads the visible Combat Log from screen capture and provides live and post-combat analysis using **Indra OCR**.

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
