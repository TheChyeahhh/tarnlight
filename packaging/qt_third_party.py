"""
Maintenance script, not part of the build. It writes licenses/qt-third-party.txt: the third-party code that Qt compiles
into the Qt files the app ships (notices.QT_FILES), each with its copyright lines and licence text, taken from Qt's own
attribution data (the qt_attribution.json files in Qt's source). Run it when the Qt version changes, then set
QT_VERSION in notices.py to match:

    python packaging\\qt_third_party.py 6.11.2

It needs git and the internet: it fetches only the attribution and licence files of qtbase and qtsvg at that tag from
github.com/qt.
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The parts of Qt's attribution data that are inside the shipped files on Windows: Qt Core, Qt GUI (with the Direct3D
# and Vulkan parts of its rendering layer), Qt SVG and the Windows platform plugin. Left out: code for other systems
# (Android, macOS, Wayland, X11, ARM), for modules the app does not ship (Qt Network, Qt SQL, Qt Test), for the image
# plugins the spec leaves out (libjpeg) and for the build system.
PARTS = [
    # Qt Core
    "unicode-character-database", "unicode-cldr", "pcre2", "pcre2-sljit", "zlib", "doubleconversion", "easing",
    "tinycbor", "tika-mimetypes", "rfc6234", "sha1", "sha3_endian", "sha3_keccak", "blake2", "md4", "md5", "siphash",
    "tlexpected",
    # Qt GUI
    "freetype", "freetype-zlib", "freetype-bdf", "freetype-pcf", "grayraster", "harfbuzz-ng", "emoji-segmenter",
    "libpng", "md4c", "smooth-scaling-algorithm", "xserverhelper", "aglfn", "icc-srgb-color-profile", "webgradients",
    "opengl-headers", "opengl-es2-headers", "vulkan-xml-spec", "vulkanmemoryallocator", "d3d12memoryallocator",
    "rhi-miniengine-d3d12-mipmap",
    # the Windows platform plugin
    "wintab",
    # Qt SVG
    "xsvg",
]
REPOS = ("qtbase", "qtsvg")


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True)


def fetch(version, into):
    for repo in REPOS:
        git("clone", "--quiet", "--depth", "1", "--branch", f"v{version}", "--filter=blob:none", "--no-checkout",
            f"https://github.com/qt/{repo}.git", str(into / repo))
        git("sparse-checkout", "set", "--no-cone", "**/qt_attribution.json", "/LICENSES/*", cwd=into / repo)
        git("checkout", "--quiet", f"v{version}", cwd=into / repo)


def entries(into):
    for repo in REPOS:
        for path in sorted((into / repo).rglob("qt_attribution.json")):
            data = json.loads(path.read_text("utf-8"), strict=False)
            for entry in data if isinstance(data, list) else [data]:
                yield repo, path.parent, entry


def chosen(licence_id):
    """The licences a part is used under: the first of a choice ("A OR B"), all of a combination ("A AND B")."""
    return licence_id.split(" OR ")[0].strip("()").split(" AND ")


def main():
    if len(sys.argv) != 2: sys.exit(__doc__)
    version = sys.argv[1]
    with tempfile.TemporaryDirectory() as tmp:
        into = Path(tmp)
        fetch(version, into)
        found = {e["Id"]: (repo, folder, e) for repo, folder, e in entries(into) if e.get("Id") in PARTS}
        missing = [p for p in PARTS if p not in found]
        if missing: sys.exit(f"not in Qt {version}'s attribution data: {missing}")
        own = {}  # a part's own licence file, which has to be checked out first
        for part in PARTS:
            repo, folder, e = found[part]
            if e.get("LicenseFile"):
                own[part] = (folder / e["LicenseFile"]).resolve()
                path = own[part].relative_to((into / repo).resolve()).as_posix()
                git("sparse-checkout", "add", f"/{path}", cwd=into / repo)
        used = []
        for part in PARTS: used += [i for i in chosen(found[part][2]["LicenseId"]) if i not in used]
        texts = {}
        for licence in used:
            path = next(p for p in (into / repo / "LICENSES" / f"{licence}.txt" for repo in REPOS) if p.is_file())
            texts[licence] = path.read_text("utf-8", "replace").strip()
        freetype_year = max(re.findall(r"(?:19|20)[0-9]{2}", str(found["freetype"][2]["Copyright"])))
        out = [f"Third-party code inside Qt {version}", "",
               "The Qt files in this app contain the code below, each under its own licence. This list is Qt's own",
               f"attribution data (qt_attribution.json in qtbase and qtsvg at tag v{version}) for Qt Core, Qt GUI, Qt SVG",
               "and the Windows platform plugin. Where a part offers a choice of licences, it is used under the first",
               "one named. The full licence texts follow the list.", "",
               # the credit the FreeType licence asks for, with the newest year in FreeType's own copyright lines
               f"Portions of this software are copyright (c) {freetype_year} The FreeType Project (www.freetype.org).",
               "All rights reserved.", ""]
        shown = {}  # licence file text, whitespace folded -> the part it was shown under
        for part in PARTS:
            _repo, _folder, e = found[part]
            copyright = e.get("Copyright") or []
            copyright = copyright.splitlines() if isinstance(copyright, str) else copyright
            version_ = e.get("Version") if e.get("Version") and len(e["Version"]) < 20 else ""
            out += ["-" * 78, f"{e['Name']} {version_}".strip(), " ".join(e.get("QtUsage", "").split()),
                    f"Licence: {e['LicenseId']}"]
            if e.get("Homepage"): out.append(f"Home: {e['Homepage']}")
            out += ["", *copyright, ""]
            if part in own:
                text = own[part].read_text("utf-8", "replace").strip()
                key = " ".join(text.split())
                same = [i for i in chosen(e["LicenseId"]) if " ".join(texts[i].split()) == key]
                if same:
                    out += [f"Its licence file is the {same[0]} text below.", ""]
                elif key in shown:
                    out += [f"Its licence file is the same as {shown[key]}'s above.", ""]
                else:
                    shown[key] = e["Name"]
                    out += [f"----- its licence file ({own[part].name}) -----", "", text, ""]
        out += ["=" * 78, "Licence texts", "=" * 78, ""]
        for licence in used: out += [f"----- {licence} -----", "", texts[licence], ""]
    target = HERE / "licenses" / "qt-third-party.txt"
    target.write_text("\n".join(out), encoding="utf-8", newline="\n")
    print(f"wrote {target}: {len(PARTS)} parts, {len(used)} licences")


if __name__ == "__main__":
    main()
