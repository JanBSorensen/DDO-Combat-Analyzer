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
A dark, opaque Combat Log background helps recognition. A focus interruption
may cause some lines to be lost if the log scrolls while the game is unfocused.
The analyzer is non-invasive: it reads screenshots rather than game memory.

Source: https://github.com/JanBSorensen/DDO-Combat-Analyzer
License and third-party notices are supplied in LICENSE and THIRD_PARTY_NOTICES.md.


PERFORMANCE MODE: FULL OR LIGHT (ONE APPLICATION)
------------------------------------------------
Full is default and retains lossless FFV1 video, independent reanalysis,
Replay Live and configured OCR settings.

Light runs 20-FPS live tracking with at most two Indra threads. It uses
the live helper as the only screen reader: no FFmpeg video or redundant
per-row screenshot diagnostics. The entire Combat Log selection and
damage, sneak, attack/critical parsing are unchanged. Light saves OCR
evidence for "Rebuild from OCR", but no independent video replay.

Both modes save performance_comparison.json and live_resource_usage.json.
CPU/RAM data are for the OCR helper, not for DDO or the entire computer.
