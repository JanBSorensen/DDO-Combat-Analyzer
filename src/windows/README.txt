DDO COMBAT ANALYZER - WINDOWS TEST BUILD
========================================

FIRST USE
---------
1. Extract the entire ZIP file to a normal folder.
2. Run "SETUP DDO Combat Analyzer.cmd" once.
3. The setup installs/verifies Python, FFmpeg, required Python packages and Indra OCR.
4. The GUI opens automatically when setup finishes.

CONTINUOUS LIVE MODE
--------------------
Continuous live DPS is enabled by default.

- The rolling DPS/APM overlay can run continuously. The window is user-selectable (5, 10, 15, 20, 30, or 60 seconds); 15 seconds is the default.
- While idle, combat screenshots, live event CSV files and post-analysis data are NOT saved.
- Press the configured global recording hotkey (default: Pause/Break) to start a session recording.
- Press the same hotkey again to stop the recording and start post-analysis.
- DDO remains in focus; the analyzer window is not brought forward by the background workflow.
- Live DPS continues while post-analysis runs.
- When analysis finishes, two short beeps are played.
- The final post-analysis Session DPS is then shown in the live overlay for 10 seconds.
- This first version deliberately does not start another recording while the previous session is still being analyzed.

The hotkey can be changed in the Recording panel.
On Windows the recording hotkey uses a low-level keyboard hook. It only triggers while DDO is the foreground window; the matched key press is consumed so DDO does not receive it. Outside DDO, the key behaves normally.
The normal START RECORDING / STOP & ANALYZE buttons remain available.
If Continuous live DPS is disabled, recording uses the previous session-only live overlay workflow.

OVERLAY POSITION
----------------
The Recording panel provides standard positions:
Top left / Top center / Top right / Bottom left / Bottom center / Bottom right.

Choose "Position overlay" for a custom position. DDO is brought to the foreground and remains the active window.
Drag the sample overlay, then double-click the overlay to save. Right-click the overlay to cancel.
Custom positions are stored relative to the DDO client window, and the live overlay follows the DDO window if it moves.
During normal play the overlay remains click-through.

FOCUS BEHAVIOUR
---------------
The overlay is visible only while DDO is the foreground window.
If DDO loses focus, the live screen parser pauses instead of OCR-reading whatever application covers the Combat Log.
Rolling time continues normally, so older damage ages out while DDO is in the background.
When DDO returns to the foreground, live parsing resumes from a fresh visual baseline.

During a recording, DDO-focus gaps are written to focus_interruptions.csv and shown in Data quality.
Post-analysis starts a fresh reconstruction segment after each gap, because combat text that appeared while DDO was hidden cannot be recovered reliably from screen capture.
If Session DPS becomes ready while DDO is not foreground, its 10-second overlay display waits for DDO to become visible again.

OCR / TRACKING
--------------
The analyzer uses Indra OCR for both live and post-analysis.
The Recording panel also has an OCR threads setting: Auto, 2, 4, 6, or 8.
Auto uses 2 threads on a PC with 1-2 physical CPU cores and 4 threads on a PC
with 3 or more physical CPU cores. The same setting is used for live and
post-analysis OCR; changing it restarts the persistent live OCR helper.
The Recording panel has a Line height setting: Auto, 10 px, 12 px, 14 px, 16 px, 18 px or 20 px.
Auto remains the default and remembers the most recently detected good line height.
A dark, opaque Combat Log background gives the most reliable tracking and OCR.

Live OCR validates DDO number formatting before accepting a damage value. DDO always uses comma thousands separators, so values such as 11388 or 21319 are not accepted as-is. Suspicious values are re-read with isolated OCR variants; when the only valid DDO grouping is a comma read as digit 1, the value is repaired (for example 11388 -> 1,388). Ambiguous invalid values are rejected rather than creating a false DPS spike.

Live line reconstruction now defers a partially visible newest bottom row until it has scrolled fully into view. If recording stops first, that one pending row is flushed through a narrow Stop/EOF fallback instead of being lost. The normal 30-fps scroll matcher remains the fast path; only frames classified as uncertain->zero receive a second opinion from the more robust FastAlignment model, and only a confident positive result may override the fast matcher.

The older duplicated-one guard is also retained for errors that can still look correctly formatted, such as 9,197 -> 91,197.
During a recording, both raw and corrected live damage values are retained in live_damage_events.csv.

POST-ANALYSIS STATISTICS
------------------------
This build adds:

1. Blocked/resisted outgoing damage
   - dealt damage, blocked amount, pre-mitigation amount and blocked percentage
   - grouped by target, observed reduction type and damage type
   - canonical examples: DR / Silver, DR / Good, DR / Slash, Fire resistance

2. d20 attack-roll distribution by target
   - miss / grazing hit / hit / critical threat counts for observed rolls

3. Data-quality summary
   - visual/OCR line counts, live/post damage event counts, numeric corrections,
     suspicious unparsed damage-like lines, live/post event-count agreement,
     and DDO-focus interruption count/duration

4. normalized_events.csv
   - one chronological event stream for damage, healing, attacks, confirmations
     and explicit incoming miss/avoidance outcomes

5. Sneak attack statistics
   - total sneak damage, hit count, damage share, average sneak damage and attacks/minute

6. Session and active-combat DPS
   - Session DPS uses the first-to-last damage-batch span plus one median damage-batch interval. This corrects the usual first/last-event fencepost bias while ignoring dead time before the first hit and after the last hit.
   - Recording DPS (shown in the text report) still uses the full user-recorded duration.
   - Active combat DPS excludes long gaps between outgoing damage events using the configured inactivity threshold.

RESULT FILES
------------
Each recording is stored in its own session folder. Important output includes:
- COMBAT_REPORT.txt
- combat_log.txt
- damage_events.csv
- normalized_events.csv
- damage_by_target.csv
- outgoing_damage_by_type.csv
- damage_by_target_and_type.csv
- mitigation_by_target_and_type.csv
- dps_rolling.csv
- live_dps.csv
- live_damage_events.csv
- attack_events.csv
- attack_roll_distribution.csv
- crit_by_target.csv
- incoming_defense_events.csv
- incoming_defense_by_source.csv
- apm_rolling.csv
- analysis_summary.json
- post_analysis_timings.csv
- focus_interruptions.csv

After using "Replay live test", the same session folder also contains:
- replay_live_summary.json
- replay_live_damage_events.csv
- replay_live_dps.csv
- replay_live_tracking.csv
- replay_live_ocr_scan.csv

IMPORTANT
---------
Do not run SETUP or START directly from inside the ZIP file. Extract the whole ZIP first.


FIRST-RUN SETUP FLOW
--------------------
Choosing "Set up now" in the first-start guide runs the setup as one continuous flow:
1. DDO is brought to the foreground.
2. Select the Combat Log area from a frozen screenshot.
3. Overlay positioning starts automatically.
4. Drag the overlay and double-click it to save.
5. The analyzer returns when setup is complete.

The first-run flow skips the extra Combat Log confirmation dialog. Later use of
"Select / adjust log area" still changes only the capture area.

ABOUT
-----
The About window contains the project credit and the Danish line:
"Hav det godt eller opfør jer ordentligt"


FIRST-RUN CAPTURE AREA
----------------------
This distribution intentionally contains no preconfigured Combat Log area.
Use "Select / adjust log area" before recording or using Continuous live DPS.

The validated reference area is 885 x 522 px. If your selected area is less
than 70% of that width or height, the program warns you, but you can still
continue if the smaller selection is intentional.

OVERLAY FONT
------------
Overlay font size can be set to 12, 18, 24, 30, or 36 (default 24).


FIRST START / HOTKEY
--------------------
The analyzer requests Administrator privileges at startup. This is required so
the low-level Pause/Break hook can work while an elevated DDO client has focus.
The request is made through the installed Python/pythonw executable. The Windows
UAC dialog may therefore identify Python rather than DDO Combat Analyzer. This is
intentional: the custom native launcher was removed because reputation-based
security products can flag a newly built unsigned executable as a rare application.

On first start a short setup guide is shown. The About button displays the
project credit text. Reanalyze selected remains disabled until a session exists.

SESSION HISTORY
---------------
Use "Open session..." to browse recordings stored in this installation.
The list shows date/time, recording duration, total damage, Session DPS,
up to three targets, and whether the session has already been analyzed.

- Double-click an analyzed session, or choose "Open analysis", to reopen the
  saved results immediately without running OCR again.
- Choose "Reanalyze" to rerun post-analysis on that session's original capture.
- "Reanalyze selected" in the main window always applies to the session that
  is currently loaded/selected, and remains disabled when no session exists.
- "Session folder" opens the folder of the currently loaded session.
- "Replay live test" runs the CURRENT live reconstruction/OCR/parser against the
  selected session's saved capture. It writes separate replay_live_* diagnostics
  and compares replay live damage/events with the saved post-analysis result.
  The replay waits for OCR capacity instead of dropping lines, so it validates
  live-pipeline correctness rather than real-time throughput on the PC.

SORTABLE RESULT TABLES
----------------------
Tables in the result tabs can be sorted by clicking a column heading.
- Text columns sort alphabetically.
- Number and percentage columns sort numerically.
- Time columns sort by elapsed time.
- Click the same heading again to reverse the order.
- The active heading shows an up/down arrow.
- Missing values remain at the bottom.
The session-history list uses the same click-to-sort behaviour.

Live-process lifetime / single instance
---------------------------------------
The analyzer no longer scans for or force-closes Python processes at startup.
Only one analyzer GUI instance is allowed at a time through a normal Windows mutex.
Live helper processes receive the creating GUI's PID and watch that parent process;
if the GUI exits or crashes, the helper shuts itself down. The continuous-live helper
also treats a closed control pipe as a shutdown signal.

An orphan helper left behind by an older test build does not know this new protocol.
If an old overlay survives the first switch to this build, close that old Python helper
once in Task Manager or restart Windows. New builds should not create such orphans.

Blocked / DR display
--------------------
If no reduction/resistance was observed, the Reduction field is left blank.

TABLE LAYOUT
------------
Result tables use compact fixed-width text columns instead of stretching Target/Source across the entire window.
- Damage taken uses side-by-side Damage type and Source tables.
- Attacks / Crits uses sub-tabs for By target, Confirm rolls and d20 distribution.
- Event log keeps the table compact and shows full original OCR text below the selected row.

DR NORMALIZATION
----------------
Equivalent DDO/OCR forms are normalized before aggregation. For example Silver, Silver damage, Silver DR and Silver damage reduction all become DR / Silver. Slash variants become DR / Slash. Rows with no blocked/resisted damage are omitted from Blocked / DR; blocked damage with no identifiable bypass type is shown as Unknown DR.

MOB DETAILS
-----------
In the Targets tab, select a mob and choose "Details...", or simply double-click
its row. The same double-click shortcut also works on target rows in Blocked / DR
and Attacks / Crits.

The mob detail window contains:
- Overview: outgoing damage, hits, average hit, blocked/resisted damage, attack
  counts, crits, incoming damage, healing and net HP loss.
- Attacks: a complete natural d20 table from 1 through 20, including zero-count
  rolls, with miss/graze/normal-hit/crit-threat outcomes and linked crit confirms.
  The view also reports highest observed miss, lowest observed full hit and an
  evidence-based total-roll hit-threshold range. Natural 1/20 and grazing hits
  are excluded from threshold bounds.
- Crits: threat/confirm summary plus every confirmation roll and Combat Log line,
  with highest failed / lowest successful confirmation totals and the resulting
  observed confirmation-threshold range.
- Damage: outgoing damage split by damage type, regular/sneak damage and share.
- DR / Resistance: the normalized observed reduction types for that mob.
- Incoming damage: damage attributable to the mob, including named effects such as
  "Mob's spell/effect" when the Combat Log source can be associated with that mob.
  It also summarizes explicit defensive attack outcomes from lines such as
  "<mob> misses you" and categorizes Regular miss, Dodge, Blur, Displacement,
  Deflect and Other. Shares use direct hits plus those explicit miss/avoidance
  events; named spell/effect damage is excluded from that denominator.

Caster / DC analysis is intentionally not included in this build.

TEST NOTE - Deferred partial bottom row
---------------------------------------
This test build changes post-analysis reconstruction only. A clipped bottom
Combat Log row is no longer padded and sent directly to Indra OCR. It is held
until a later scrolling frame where the same row is fully visible, while its
original timestamp is preserved. No damage-parser spelling correction is used.
