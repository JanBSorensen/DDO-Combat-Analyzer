DDO Combat Analyzer - Linux Mint/Cinnamon X11

Defaults for both new installations and installations updated to this release:
Light mode + Indra Direct (recognition-only ONNX).
This is applied ONCE. You can select Full or Indra Full in the GUI afterwards;
your selected choice is preserved on subsequent starts.

INSTALLATION
Extract the release ZIP to a folder, not from inside the ZIP.
Run bash 'SETUP DDO Combat Analyzer.sh' once.
Then start ./START_DDO_COMBAT_ANALYZER.sh.
Setup installs onnxruntime and downloads/reuses Indra v1.1.0 rec.onnx.
Model weights are not bundled with this ZIP.

UPGRADING
The official verified GitHub updater preserves config.json, capture area
and saved combat sessions. On first launch after upgrading, a one-time
migration selects Light and Indra Direct. Missing ONNX dependencies/model
are installed by a background helper; see _internal/direct_setup.log for
details. If Direct is not ready, Live OCR waits instead of showing false DPS.

Light uses 20 FPS and at most two OCR threads, with no video recording.
Light can rebuild reports from saved OCR. Full retains independent FFV1
video reanalysis and Replay Live. Continuous Live is enabled in Light.
Use an opaque solid black DDO Combat Log background.
When selecting a capture area, include only the scrolling Combat Log text.
Do NOT include the bottom chat input row (Say, Group, Guild or another
channel), scrollbar or window borders: these can cause missed combat lines.
If attack or damage counts look wrong, reselect the capture area.
Non-invasive: only screen capture, no game memory access.

Source: https://github.com/JanBSorensen/DDO-Combat-Analyzer
See LICENSE and THIRD_PARTY_NOTICES.md.
