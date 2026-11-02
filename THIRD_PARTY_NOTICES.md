# Third-party software and model notices

DDO Combat Analyzer's **original source code** is licensed under the
[Apache License, Version 2.0](LICENSE). This license does **not** relicense
third-party libraries, executables, model weights or game content. Copyright
and license terms for those components remain with their respective owners.

The current repository primarily contains Python source and launch/setup
scripts. Its setup scripts install or use the components below separately;
in particular, **Indra model weights and FFmpeg executables are not included
in the repository**.

## Direct dependencies and supporting tools

| Component | Upstream license / terms | Project and license information |
| --- | --- | --- |
| Python interpreter and standard library | Python Software Foundation License (PSF) and applicable version-specific notices | [Python license](https://docs.python.org/3/license.html) |
| MSS (`mss`), screen capture | MIT | [Python MSS](https://github.com/BoboTiG/python-mss) |
| NumPy (`numpy`) | BSD-3-Clause (with separately licensed components in some binary wheels) | [NumPy license](https://numpy.org/doc/stable/license.html) |
| OpenCV (`cv2`) | Apache-2.0 for OpenCV 4.5.0+; the `opencv-python` packaging code is MIT; wheels may include additional licensed components | [OpenCV license](https://opencv.org/license/), [opencv-python distribution notes](https://pypi.org/project/opencv-python/) |
| Indra OCR Python code (`indra-ocr`) | Apache-2.0 | [Indra source](https://github.com/datalab-to/indra) |
| **Indra OCR pretrained model weights** | **Modified OpenRAIL-M** (additional usage, commercial and competition-related conditions) | [Indra model license](https://huggingface.co/datalab-to/indra-ocr/blob/main/LICENSE) and [usage description](https://github.com/datalab-to/indra#license) |
| FFmpeg, external encoder/decoder | LGPL-2.1-or-later for the standard build, or GPL depending on included components and configuration. **Gyan's Windows FFmpeg builds are GPLv3.** | [FFmpeg licensing](https://ffmpeg.org/legal.html), [Gyan Windows builds](https://www.gyan.dev/ffmpeg/builds/) |
| python-xlib (`python-xlib`), Linux X11 support | LGPL-2.1-or-later | [python-xlib](https://github.com/python-xlib/python-xlib) |
| xdotool, external Linux X11 utility | BSD-3-Clause | [xdotool copyright and license](https://github.com/jordansissel/xdotool/blob/main/COPYRIGHT) |
| wmctrl, external Linux window-management utility | GPL-2.0-or-later | [wmctrl](https://github.com/dancor/wmctrl) |

The Indra installer may also obtain transitive dependencies such as ONNX
Runtime, pypdfium2 and huggingface_hub. Those components have their own
licenses. The applicable set of dependencies, bundled library notices, and
terms depends on the versions actually installed.

## Important distinction: Indra OCR source vs. models

Datalab publishes the Indra **code** under Apache-2.0, but the **model weights**
under a *modified* OpenRAIL-M license. The model terms are not interchangeable
with the Apache license and may restrict certain uses. Datalab currently
describes free research/personal use and startup eligibility subject to its
published conditions, as well as restrictions involving competing with its
API. **Read the complete model license** before downloading or using the
weights, and obtain appropriate permission where necessary.

The project does not bundle, modify or relicense those model weights. Downloading
a model separately does not remove its restrictions. The Apache-2.0 license
for this project's source code does not grant rights to use the model weights
outside their own terms.

## FFmpeg and distribution

DDO Combat Analyzer invokes an independently installed `ffmpeg` command-line
executable; it does not incorporate FFmpeg source code into this repository.
The actual license of the **FFmpeg executable** depends on its build.
In particular, the Windows setup script installs the Gyan build via
`winget`; Gyan identifies its Windows builds as **GPLv3**. Linux generally
uses FFmpeg provided by the system package manager.

If a future release **redistributes FFmpeg binaries** (or bundles any other
third-party executables or libraries), its distributor must comply with the
applicable license obligations, including any required license texts,
attributions and source-code availability. Merely applying Apache-2.0 to
this repository does not replace those obligations.

## Scope and maintenance

- This notice describes major direct dependencies and tools referenced by the
  present Windows and Linux source. It is not an exhaustive inventory of every
  transitive dependency or system library.
- Installed package versions are not all pinned; consult the actual
  package-specific license notices when building or redistributing an installer
  or executable.
- Dungeons & Dragons Online, its name, trademarks and game assets remain the
  property of their respective owners. DDO Combat Analyzer is an independent
  community project and is not affiliated with or endorsed by Standing Stone
  Games.

For questions involving redistribution or commercial licensing, consult the
relevant upstream license texts; this notice is informational and does not
replace them.
