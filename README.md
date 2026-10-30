# DDO Combat Analyzer

DDO Combat Analyzer is a non-invasive combat analysis tool for **Dungeons & Dragons Online (DDO)**.

The project captures the on-screen Combat Log, reconstructs scrolling combat text with OCR, and provides both:

- **Live DPS estimates** during combat
- **Post-combat analysis** based on reconstructed Combat Log data

The Windows version is the primary development target. A Linux version is also maintained for users running DDO through Wine, Lutris, or Steam.

> **Status:** Active development. The post-analysis pipeline is currently more mature than the live-DPS pipeline.

## Goals

- No memory injection or game modification
- Capture only the user-selected Combat Log area
- Provide useful DPS feedback during combat
- Reconstruct combat events after combat for more detailed analysis
- Keep setup simple for non-technical users
- Support both Windows and Linux

## Platforms

### Windows

Primary platform. The application uses screen capture, OCR, and a desktop GUI.

See [src/windows/README.md](src/windows/README.md).

### Linux

Linux support targets DDO running under Wine / Lutris / Steam.

See [src/linux/README.md](src/linux/README.md).

## Current analysis approach

### Post-analysis

The recorder captures the selected Combat Log area at high frequency and stores changed frames losslessly. After combat, the analyzer:

1. aligns adjacent saved frames,
2. detects Combat Log scrolling,
3. extracts newly exposed text lines,
4. OCRs those lines,
5. parses damage and healing events,
6. produces DPS and combat statistics.

Post-analysis DPS is calculated from the reconstructed Combat Log and should generally be more accurate than the live estimate.

### Live DPS

The live system is being redesigned around:

1. fast visual tracking of Combat Log scrolling,
2. extraction of only newly exposed text lines,
3. asynchronous OCR of those new lines,
4. a tolerant live damage parser,
5. rolling 15-second DPS.

The goal is to avoid repeatedly OCRing the entire visible Combat Log.

## Repository layout

```text
DDO-Combat-Analyzer/
├─ README.md
├─ CHANGELOG.md
├─ .gitignore
├─ docs/
│  ├─ ARCHITECTURE.md
│  └─ TROUBLESHOOTING.md
├─ src/
│  ├─ common/
│  ├─ windows/
│  └─ linux/
└─ tests/
```

## Privacy and game integrity

DDO Combat Analyzer is designed to be non-invasive. It analyzes pixels from the user-selected Combat Log region and does not require modification of the DDO client.

## Releases

Finished end-user packages will be published through **GitHub Releases** rather than committed directly to the source tree.

Planned release assets:

- `DDO_Combat_Analyzer_Windows_<version>.zip`
- `DDO_Combat_Analyzer_Linux_<version>.zip`

## License

A project license has not yet been selected. Dependency and OCR-engine license compatibility will be reviewed before the repository is made public.

## Disclaimer

This is an independent community project and is not affiliated with or endorsed by Standing Stone Games.
