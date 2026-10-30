# Architecture

## Design principles

DDO Combat Analyzer is intentionally non-invasive. Its primary input is the visible DDO Combat Log.

The application is split conceptually into four pipelines:

1. capture,
2. visual reconstruction,
3. OCR,
4. parsing and reporting.

## Post-analysis pipeline

The post-analysis path prioritizes reconstruction quality over immediate latency.

### Capture

- User selects the Combat Log region.
- The region is captured at approximately 30 FPS.
- Frames are converted to grayscale.
- A lightweight change detector decides which frames need to be stored.
- Forced anchor frames are periodically retained.
- Changed frames are stored losslessly.

### Reconstruction

Adjacent stored frames are visually aligned.

The analyzer estimates vertical Combat Log movement, normally in multiples of the configured line height. Newly exposed line cells are extracted rather than OCRing every full frame.

### OCR

Newly exposed line images are OCRed.

Numeric corrections are applied conservatively inside numeric fields, for example likely OCR confusions such as:

- S -> 5
- O -> 0
- I / l / | -> 1

### Parsing

Recognized events include outgoing damage, sneak-attack damage, healing, and selected incoming-damage forms.

Generated outputs include damage totals, target/type breakdowns, event CSVs, reconstruction diagnostics, and rolling DPS data.

## Live pipeline

The first live implementation repeatedly OCRed a large visible Combat Log block and attempted to infer new lines from OCR text overlap.

Testing showed this was too fragile:

- OCRing the whole region was slow.
- repeated combat text made text-overlap alignment ambiguous,
- OCR artifacts could become false anchors,
- many real damage lines were visible in OCR output but were rejected as not-new.

The replacement design is:

```text
high-frequency screen capture
        |
        v
visual scroll detection
        |
        v
new line extraction + timestamp
        |
        v
asynchronous OCR queue
        |
        v
tolerant live damage parser
        |
        v
rolling 15-second DPS
```

This lets image tracking run much faster than OCR and preserves event timing even when OCR finishes slightly later.

## Platform separation

Shared analysis logic should gradually move into `src/common/`.

Platform-specific concerns remain under:

- `src/windows/`
- `src/linux/`

Examples include process launching, window focus, global hotkeys, overlay behavior, and installer scripts.
