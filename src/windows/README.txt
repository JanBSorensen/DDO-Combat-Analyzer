DDO Combat Analyzer - Windows

Defaults for both new installations and installations updated to this release:
Light mode + Indra Direct (recognition-only ONNX).
This is applied ONCE. You can select Full or Indra Full in the GUI afterwards;
your selected choice is preserved on subsequent starts.

INSTALLATION
Extract the release ZIP to a folder, not from inside the ZIP.
Run SETUP DDO Combat Analyzer.cmd once.
Then start START DDO Combat Analyzer.cmd.
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
Non-invasive: only screen capture, no game memory access.

Source: https://github.com/JanBSorensen/DDO-Combat-Analyzer
See LICENSE and THIRD_PARTY_NOTICES.md.
