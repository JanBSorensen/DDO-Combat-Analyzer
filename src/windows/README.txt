DDO COMBAT ANALYZER - WINDOWS
============================

INSTALLATION
1. Extract the complete release ZIP into a normal folder. Do not start
   the program from inside the compressed ZIP.
2. Run "SETUP DDO Combat Analyzer.cmd" once for Python and OCR dependencies.
3. Run "START DDO Combat Analyzer.cmd" to open the application.
4. Select the Combat Log capture area before starting a session.

CHECK FOR UPDATES
The program checks the latest official GitHub Release in the background when
it starts. Choose "Check for updates" to check on demand. When an official new
release exists, choose "Update now", "Manual download" or "Later".

Automatic updates only start with your approval. A separate updater downloads
and verifies the ZIP against the SHA-256 checksum published by GitHub. The
analyzer closes, program files are replaced, and the app restarts. Existing
config.json, selected Combat Log area, saved combat_* sessions, model downloads,
Python and FFmpeg installations are not overwritten. A failed file replacement
triggers an attempt to restore the old code. Internet access to GitHub is
required to check for updates; without it, the current program keeps working.

RECORDING & ANALYSIS
- Start/stop a combat session with Pause/Break.
- Live overlay reports DPS and APM.
- Stop & Analyze creates the fast live report from the combat session.
- Reanalyze (FFV1) independently processes the saved lossless recording.
- Replay live test independently runs the live pipeline over the saved video.
- Comparing all three independently processed data sets can reveal OCR or
  reconstruction differences.

NOTES
Keep DDO's Combat Log visible and in the foreground during recording.
A SOLID OPAQUE BLACK Combat Log background is REQUIRED for reliable capture.
The live background monitor warns when the visible background looks nonblack.
A focus interruption
may cause some lines to be lost if the log scrolls while the game is unfocused.
The analyzer is non-invasive: it reads screenshots rather than game memory.

Source: https://github.com/JanBSorensen/DDO-Combat-Analyzer
License and third-party notices are supplied in LICENSE and THIRD_PARTY_NOTICES.md.

PERFORMANCE MODES
=================
Light is the default for NEW installations: 20-FPS OCR tracking, at most
two OCR threads, no FFV1 video; a report can be rebuilt from saved OCR.
Full saves FFV1 video for independent Reanalyze and Replay Live.
Switch between modes from the application GUI. Updating does not replace
an existing user's config.json or explicit Full/Light selection.

QUALITY / FIXES (NOV 2026)
==========================
- Wrapped Combat Log messages (including sneak attacks) are reconstructed,
  up to four rows, with source coverage diagnostics.
- Suspicious numeric OCR values receive a targeted verification rather than
  blind correction.
- An opaque black Combat Log background is REQUIRED. The live monitor checks
  approximately every 2 seconds. Full reanalysis and replay warn if the saved
  background is not black (recording is not forcibly blocked).
- CPU/RAM session monitoring includes analyzer GUI/helper processes but
  excludes the DDO game.
- The advanced post-analysis, damage report and UI are retained.
