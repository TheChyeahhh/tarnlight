"""
Build the Windows app from a clean checkout:

    python packaging\\build.py

Use Python 3.11 from python.org: its LICENSE.txt, which also covers the libraries it ships, goes into the notices,
next to the kept licence text for the code inside Python 3.11's own modules. The script deletes build\\ and dist\\, makes a fresh venv in build\\venv, installs the pinned build set
(requirements-build.txt) and this checkout into it, runs PyInstaller with tarnlight.spec, and leaves:

    dist\\Tarnlight\\                        the app: Tarnlight.exe, tarnlight-cli.exe, _internal\\ and the licence files
    dist\\Tarnlight-<version>-windows.zip    the same folder, zipped, as attached to a GitHub release

Nothing is installed outside build\\.
"""
import shutil
import subprocess
import sys
import tomllib
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD, DIST = ROOT / "build", ROOT / "dist"


def run(*cmd):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], cwd=ROOT, check=True)


def size(path):
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main():
    if sys.platform != "win32": sys.exit("The app build is Windows only for now.")
    if sys.version_info[:2] != (3, 11): sys.exit("Build with Python 3.11 (see notices.PYTHON_VERSION).")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    for folder in (BUILD, DIST):
        if folder.exists(): shutil.rmtree(folder)  # fails if the app from an earlier build is still running
    venv.create(BUILD / "venv", with_pip=True)
    python = BUILD / "venv" / "Scripts" / "python.exe"
    pip = (python, "-m", "pip", "--disable-pip-version-check", "--no-input")
    run(*pip, "install", "-r", ROOT / "packaging" / "requirements-build.txt")
    run(*pip, "install", "--no-deps", ROOT)
    run(*pip, "check")  # the pinned set satisfies what pyproject.toml asks for
    run(python, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
        "--distpath", DIST, "--workpath", BUILD / "pyinstaller", ROOT / "packaging" / "tarnlight.spec")
    app = DIST / "Tarnlight"
    archive = Path(shutil.make_archive(str(DIST / f"Tarnlight-{version}-windows"), "zip",
                                       root_dir=DIST, base_dir="Tarnlight"))
    print(f"\n{app}  {size(app) / 1e6:.1f} MB\n{archive}  {archive.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
