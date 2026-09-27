# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the Windows app: one folder, two launchers that share one _internal folder.

  Tarnlight.exe       windowed, no console window: double-click it to open the console (gui_launcher.py)
  tarnlight-cli.exe   a console program: the full command line, the same as `python -m tarnlight` (cli_launcher.py)

Run it through packaging/build.py, which makes a clean venv first. Tarnlight must be pip-installed (not editable) in the
venv that runs PyInstaller: the fonts are collected from the installed package. After the build, notices.py writes the
licence files next to the two programs.
"""
import os
import re
import sys
import tomllib

from PyInstaller.utils.hooks import collect_data_files

sys.path.insert(0, SPECPATH)
import notices  # packaging/notices.py

ROOT = os.path.dirname(SPECPATH)
with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
    VERSION = tomllib.load(f)["project"]["version"]


def version_file(name, description):
    """version_info.txt with this build's values filled in, written to the work folder."""
    numbers = [int(n) for n in re.match(r"\d+(?:\.\d+)*", VERSION).group().split(".")][:4]
    numbers += [0] * (4 - len(numbers))
    with open(os.path.join(SPECPATH, "version_info.txt"), encoding="utf-8") as f:
        text = f.read()
    for key, value in {"VERSION_TUPLE": repr(tuple(numbers)), "VERSION": VERSION, "NAME": name, "DESCRIPTION": description}.items():
        text = text.replace(f"@{key}@", value)
    path = os.path.join(workpath, f"version_info-{name}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


# Left out of the app. opengl32sw.dll is a 20 MB software OpenGL fallback: the window paints without OpenGL. The app
# opens no image files, so it needs none of these image format plugins (PNG is built into Qt6Gui), and it takes no
# TUIO touch input. Qt6Network.dll comes along although nothing shipped links to it (PySide6.QtNetwork is excluded).
# Fewer Qt files also means less third-party code inside Qt to credit (see notices.QT_FILES).
LEFT_OUT = {"opengl32sw.dll", "qjpeg.dll", "qtiff.dll", "qwebp.dll", "qicns.dll", "qtga.dll", "qwbmp.dll", "qgif.dll",
            "qtuiotouchplugin.dll", "qt6network.dll"}


def analysis(launcher):
    a = Analysis([os.path.join(SPECPATH, launcher)],
                 datas=collect_data_files("tarnlight"),  # the two fonts and their licences
                 # pyqtgraph.opengl needs PyOpenGL, which is not installed. QtNetwork is unused, and with it
                 # PyInstaller would search PATH for OpenSSL DLLs for Qt and could take another program's copy.
                 # QtTest is for tests: pyqtgraph tries to import it and carries on without it.
                 excludes=["tkinter", "pyqtgraph.opengl", "PySide6.QtNetwork", "PySide6.QtTest"],
                 # PySide6 and shiboken6 are LGPL-3.0: their Python files go into _internal as loose .py files, not
                 # into the archive inside the two exes, so a user can replace them (see QT_NOTICE.txt).
                 module_collection_mode={"PySide6": "py", "shiboken6": "py"})
    # Qt's own translations are never loaded: the app has no translator. Keep PySide6.QtOpenGL itself: pyqtgraph
    # imports it as it loads.
    # The Universal C Runtime (ucrtbase.dll and its api-ms-win-*.dll forwarders) is part of Windows 10 and 11. A build
    # machine can have extra copies on PATH (GitHub's has one in a Java install), and PyInstaller would take those.
    a.binaries = [b for b in a.binaries if os.path.basename(b[0]).lower() not in LEFT_OUT
                  and not os.path.basename(b[0]).lower().startswith("api-ms-win-") and os.path.basename(b[0]).lower() != "ucrtbase.dll"]
    a.datas = [d for d in a.datas if "translations" not in os.path.normpath(d[0]).split(os.sep)]
    return a


def program(a, name, console, description):
    return EXE(PYZ(a.pure), a.scripts, [], exclude_binaries=True, name=name, console=console,
               icon="NONE",  # no icon file yet: Windows shows its default program icon
               upx=False, version=version_file(name, description))


gui, cli = analysis("gui_launcher.py"), analysis("cli_launcher.py")
app = COLLECT(program(gui, "Tarnlight", False, "Tarnlight"),
              program(cli, "tarnlight-cli", True, "Tarnlight command line"),
              gui.binaries, gui.datas, cli.binaries, cli.datas,
              name="Tarnlight", upx=False)
notices.write(os.path.join(DISTPATH, "Tarnlight"), [gui, cli], workpath)
