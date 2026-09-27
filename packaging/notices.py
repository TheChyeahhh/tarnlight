"""
Licence files for the built app. tarnlight.spec calls write() once PyInstaller has collected everything:

  LICENSE.txt                Tarnlight's own licence (Apache-2.0)
  THIRD_PARTY_NOTICES.txt    every package inside the app with its version, licence and licence text, plus Python
                             itself, the two fonts and the third-party code inside Qt
  QT_NOTICE.txt              Qt and PySide6 are LGPL-3.0: how they are used and where their source is
  licenses/LGPL-3.0.txt, licenses/GPL-3.0.txt

The package list is not hand-kept: every file PyInstaller collected is traced back to the installed package it came
from, so a new dependency shows up on the next build. It runs inside the build venv and reads that venv's metadata.

Some shipped files have C code compiled into them whose licence no package metadata carries: zstd inside zstandard,
the libraries inside Python's own modules, and the third-party code inside Qt. Those texts are kept in licenses/, each
for one version, and the build stops when the bundled version changes so the text gets checked.
"""
import importlib.metadata as md
import os
import platform
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
QT = {"pyside6-essentials", "pyside6-addons", "pyside6", "shiboken6"}
LICENCE_FILE = re.compile(r"(licen[cs]e|copying|notice|authors)", re.I)
RULE = "=" * 78

ZSTD_VERSION = "1.5.7"  # licenses/zstd-LICENSE.txt: zstd's LICENSE at this tag, from github.com/facebook/zstd
PYTHON_VERSION = (3, 11)  # licenses/python-3.11-incorporated-software.txt
QT_VERSION = "6.11.2"  # licenses/qt-third-party.txt, written by qt_third_party.py for this Qt version
# The Qt files licenses/qt-third-party.txt covers. A Qt file that is not named here stops the build: find the
# third-party code it carries (Qt's qt_attribution.json files), add that to PARTS in qt_third_party.py, run it again,
# then add the file here.
QT_FILES = {"qt6core.dll", "qt6gui.dll", "qt6widgets.dll", "qt6opengl.dll", "qt6openglwidgets.dll", "qt6svg.dll",
            "qwindows.dll", "qdirect2d.dll", "qminimal.dll", "qoffscreen.dll", "qmodernwindowsstyle.dll", "qico.dll",
            "qsvg.dll", "qsvgicon.dll"}


def kept(name):
    return HERE / "licenses" / name


def check_versions(analyses):
    """Stop if a bundled library is not the version its kept licence text is for, or a Qt file is not covered."""
    import zstandard
    from PySide6.QtCore import qVersion
    problems = []
    zstd = ".".join(map(str, zstandard.ZSTD_VERSION))
    if zstd != ZSTD_VERSION:
        problems.append(f"zstandard bundles zstd {zstd}, but licenses/zstd-LICENSE.txt is for {ZSTD_VERSION}: "
                        f"replace it with zstd's LICENSE at tag v{zstd} and set ZSTD_VERSION")
    if sys.version_info[:2] != PYTHON_VERSION:
        problems.append(f"licenses/python-3.11-incorporated-software.txt is for Python 3.11; this is "
                        f"{platform.python_version()}. Build with Python 3.11 from python.org")
    if qVersion() != QT_VERSION:
        problems.append(f"Qt is {qVersion()}, but licenses/qt-third-party.txt is for {QT_VERSION}: run "
                        f"packaging/qt_third_party.py {qVersion()} and set QT_VERSION")
    shipped = {Path(dest).name.lower() for a in analyses for dest, _src, _kind in a.binaries
               if Path(dest).parts[0] == "PySide6"
               and ("plugins" in Path(dest).parts or Path(dest).name.lower().startswith("qt6"))}
    if shipped - QT_FILES:
        problems.append(f"these Qt files are not covered by licenses/qt-third-party.txt: {sorted(shipped - QT_FILES)} "
                        "(see QT_FILES in notices.py)")
    if problems: sys.exit("notices:\n  " + "\n  ".join(problems))


def canon(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def bundled(analyses, workpath):
    """The installed packages the app's files came from. Stops the build if a file comes from anywhere unexpected."""
    owners = {}
    for dist in md.distributions():
        for f in dist.files or ():
            owners[os.path.normcase(os.path.abspath(dist.locate_file(f)))] = dist
    python = tuple(os.path.normcase(os.path.abspath(p)) + os.sep for p in (sys.base_prefix, workpath))
    found, unknown = {}, set()
    for a in analyses:
        for _dest, src, _kind in [*a.pure, *a.binaries, *a.datas]:
            if not src or src == "-": continue
            path = os.path.normcase(os.path.abspath(src))
            if path in owners:
                dist = owners[path]; found[canon(dist.metadata["Name"])] = dist
            elif not path.startswith(python):  # Python's own files, and base_library.zip made from them
                unknown.add(src)
    if unknown:
        sys.exit("notices: these collected files belong to no installed package, so their licence is unknown:\n  "
                 + "\n  ".join(sorted(unknown)))
    return found


def licence_name(dist):
    m = dist.metadata
    if m.get("License-Expression"): return m["License-Expression"]
    text = (m.get("License") or "").strip()
    if text and "\n" not in text and len(text) <= 200: return text
    classifiers = [c.split(" :: ")[-1] for c in m.get_all("Classifier") or () if c.startswith("License ::")]
    return ", ".join(classifiers) or "see the licence text below"


def home(dist):
    m = dist.metadata
    urls = dict(u.split(", ", 1) for u in m.get_all("Project-URL") or () if ", " in u)
    for key in ("Homepage", "homepage", "Home", "Source", "Source Code", "Repository", "GitHub"):
        if key in urls: return urls[key]
    return m.get("Home-page") or (next(iter(urls.values())) if urls else "")


def licence_texts(dist):
    """The licence files a package ships in its .dist-info folder (PEP 639 puts them under licenses/)."""
    files = [f for f in dist.files or () if f.parts[0].endswith(".dist-info")
             and (f.parts[1:2] == ("licenses",) or (len(f.parts) == 2 and LICENCE_FILE.match(f.name)))]
    for f in sorted(files, key=lambda f: (len(f.parts), str(f))):
        name = "/".join(f.parts[2:] if f.parts[1:2] == ("licenses",) else f.parts[1:])
        yield name, Path(f.locate()).read_text("utf-8", "replace")


def section(title, lines, texts=()):
    out = [RULE, title, RULE, *lines, ""]
    for name, text in texts:
        out += [f"----- {name} -----", "", text.strip("\n"), ""]
    return "\n".join(out) + "\n"


def third_party(found, fonts):
    parts = ["Third-party software in Tarnlight\n\n"
             "Tarnlight itself is under the Apache-2.0 licence (LICENSE.txt). The app also contains the software\n"
             "below, each under its own licence. This file is generated at build time from the packages the app\n"
             "was built from.\n\n"]
    for key in sorted(found):
        dist = found[key]
        if key == "tarnlight": continue
        lines = [f"Version: {dist.version}", f"Licence: {licence_name(dist)}", f"Home: {home(dist)}"]
        if key in QT:
            # The wheel's only licence file is Qt's commercial notice, which does not apply here.
            lines.append("Used under LGPL-3.0: see QT_NOTICE.txt and licenses/LGPL-3.0.txt, licenses/GPL-3.0.txt.")
            parts.append(section(dist.metadata["Name"], lines))
            continue
        texts = list(licence_texts(dist))
        if not texts: sys.exit(f"notices: {dist.metadata['Name']} ships no licence file")
        if key == "zstandard":  # its compiled modules have the zstd C library inside
            lines.append(f"Its compiled modules contain the Zstandard C library (zstd) {ZSTD_VERSION}, BSD-3-Clause.")
            texts.append((f"zstd {ZSTD_VERSION} LICENSE", kept("zstd-LICENSE.txt").read_text("utf-8")))
        parts.append(section(dist.metadata["Name"], lines, texts))
    python_licence = Path(sys.base_prefix) / "LICENSE.txt"
    text = python_licence.read_text("utf-8", "replace") if python_licence.is_file() else ""
    if "libffi" not in text:  # python.org's Windows build lists what it ships (libffi, OpenSSL, ...); others may not
        sys.exit(f"notices: {python_licence} is missing or does not cover the libraries this Python ships. "
                 "Build with Python from python.org.")
    import pyexpat, _decimal
    parts.append(section(f"Python {platform.python_version()}",
                         ["Licence: Python Software Foundation License (PSF-2.0). Below: the LICENSE.txt of the Python",
                          "build the app was made with, including the libraries that build ships, then the licences of",
                          "code inside Python's own modules, such as expat "
                          f"{pyexpat.EXPAT_VERSION.removeprefix('expat_')} (pyexpat) and libmpdec "
                          f"{_decimal.__libmpdec_version__} (_decimal).",
                          "Home: https://www.python.org"],
                         [("LICENSE.txt", text),
                          ("code inside Python's own modules",
                           kept("python-3.11-incorporated-software.txt").read_text("utf-8"))]))
    parts.append(section("Fonts: IBM Plex Sans and JetBrains Mono",
                         ["Licence: SIL Open Font License 1.1 (OFL-1.1). The font files are in _internal/tarnlight/fonts."],
                         [(f.name, f.read_text("utf-8", "replace")) for f in fonts]))
    parts.append(section(f"Third-party code inside Qt {QT_VERSION}",
                         ["Qt itself is covered in QT_NOTICE.txt. The Qt files in _internal/PySide6 also contain the",
                          "code below, each part under its own licence."],
                         [("qt-third-party.txt", kept("qt-third-party.txt").read_text("utf-8"))]))
    return "".join(parts)


def qt_notice():
    import PySide6
    from PySide6.QtCore import qVersion
    qt, pyside = qVersion(), PySide6.__version__
    return f"""Qt and PySide6 in Tarnlight

Tarnlight uses Qt {qt} and PySide6 {pyside} (with Shiboken6), made by The Qt Company and
the Qt Project, under the GNU Lesser General Public License version 3 (LGPL-3.0). The LGPL-3.0
adds to the GNU General Public License version 3; both texts are in the licenses folder
(licenses/LGPL-3.0.txt and licenses/GPL-3.0.txt).

- They are used unmodified: the official PySide6-Essentials and shiboken6 packages from PyPI.
- They stay separate shared libraries, in _internal/PySide6 and _internal/shiboken6 (the Qt6*.dll
  files, the Qt plugins and the PySide6 and shiboken6 modules, which are loose files there, not
  packed into the two programs). You may replace them with your own build of the same version.
- Qt {qt} source: https://download.qt.io/archive/qt/{'.'.join(qt.split('.')[:2])}/{qt}/
- PySide6 and Shiboken6 {pyside} source:
  https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{pyside}-src/
  https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v{pyside}
- Qt contains third-party code under its own licences. The parts inside the Qt files shipped here,
  with their licence texts, are at the end of THIRD_PARTY_NOTICES.txt. Qt's full list is at
  https://doc.qt.io/qt-{qt.split('.')[0]}/licenses-used-in-qt.html

Tarnlight itself is under the Apache-2.0 licence (LICENSE.txt). Qt is a trademark of The Qt
Company Ltd. Tarnlight is not made or endorsed by The Qt Company.
"""


def write(app_dir, analyses, workpath):
    app = Path(app_dir)
    check_versions(analyses)
    found = bundled(analyses, workpath)
    missing = {"pyside6-essentials", "shiboken6", "tarnlight"} - set(found)
    if missing: sys.exit(f"notices: expected packages not found among the collected files: {sorted(missing)}")
    fonts = sorted(Path(f.locate()) for f in found["tarnlight"].files if re.fullmatch(r"OFL-.*\.txt", f.name))
    if len(fonts) != 2: sys.exit(f"notices: expected two font licences in the tarnlight package, found {len(fonts)}")
    (app / "THIRD_PARTY_NOTICES.txt").write_text(third_party(found, fonts), encoding="utf-8")
    (app / "QT_NOTICE.txt").write_text(qt_notice(), encoding="utf-8")
    (app / "licenses").mkdir(exist_ok=True)
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt"): shutil.copyfile(HERE / "licenses" / name, app / "licenses" / name)
    shutil.copyfile(HERE.parent / "LICENSE", app / "LICENSE.txt")
    print(f"notices: {len(found) - 1} third-party packages: {', '.join(sorted(k for k in found if k != 'tarnlight'))}")
