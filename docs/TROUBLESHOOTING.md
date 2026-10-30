# Troubleshooting and current limitations

## Combat Log area

The current packages contain no preconfigured capture area.

Use **Select / adjust log area** before recording or using Continuous live DPS.

The validated reference area is approximately 885 × 522 px. A selection below 70% of that width or height triggers a warning because clipped text or too few visible rows can reduce tracking/OCR reliability.

A dark, opaque Combat Log background is recommended.

## Live DPS and post-analysis can differ

Live DPS and post-analysis use the same OCR/parser family but not the same reconstruction conditions.

Replay live test is included specifically to run the current live pipeline against a saved capture and compare it with the saved post-analysis result.

In one validation session for this exact LiveReplay code snapshot:

- Replay live: **13,355,644 damage / 3,212 events**
- Post-analysis: **13,357,495 damage / 3,215 events**

The difference was three missed events totaling 1,851 damage during one 56-pixel scroll. The current live matcher did not obtain the same line-by-line fallback result as post-analysis in that case.

Replay diagnostics are written as:

- **replay_live_summary.json**
- **replay_live_damage_events.csv**
- **replay_live_dps.csv**
- **replay_live_tracking.csv**
- **replay_live_ocr_scan.csv**

## Focus gaps

The live parser pauses while DDO is not the foreground application. It does not OCR another application that covers the Combat Log.

During recording, these gaps are written to **focus_interruptions.csv**. Combat Log text that appeared while DDO was hidden may not be recoverable, so post-analysis starts a new reconstruction segment after each recorded focus gap.

## Windows hotkey / Administrator prompt

The Windows build requests Administrator privileges at startup because the low-level recording hotkey must work while an elevated DDO client has focus.

The UAC prompt may identify the installed Python/pythonw executable rather than DDO Combat Analyzer.

The default recording hotkey is Pause/Break. It is consumed only while DDO is the foreground window; outside DDO it behaves normally.

## Linux: X11 only

The current Linux package targets Linux Mint 22.3 Cinnamon under X11.

Wayland is not supported by the current capture/focus/global-hotkey implementation.

If DDO is not detected:

- make sure the game client itself is running,
- make sure the actual DDO game window exists,
- make sure the desktop session is X11.

## Linux logs

Useful files include:

- **_internal/startup_error.log**
- **_internal/startup_console.log**
- session **live_overlay_error.txt**
- session **ffmpeg.log**

## Windows logs

Useful files include:

- **_internal/startup_error.log**
- session **live_overlay_error.txt**
- session **ffmpeg.log**

## OCR threads

Available settings are Auto, 2, 4, 6 and 8.

Auto uses 2 threads on 1–2 physical CPU cores and 4 threads on systems with 3 or more physical CPU cores.

The same setting is used by live and post-analysis OCR.

## Line height

Available settings are Auto, 10, 12, 14, 16, 18 and 20 px.

Auto is the default and retains the most recently detected good line height.

## Reanalysis

**Reanalyze selected** is disabled when no usable session is selected.

An analyzed session can be reopened without rerunning OCR. Reanalysis uses the original saved capture for that session.
