"""Freeze Ember for this OS with PyInstaller: dist/Ember/ holding Ember.exe
(Windows) or Ember (Linux), plus its _internal/ folder. Ship the folder.

    python -m pip install pyinstaller pywebview      # Linux: pywebview[gtk]
    python packaging/pyinstaller/build.py

The executable is the window (native/window.py), the server (--server) and
the Claude Code tees (devtools_hooks.py …) in one; the release workflow runs
this on Windows and Linux. macOS uses packaging/macos/build-app.sh instead.
The installers wrap dist/Ember: packaging/windows/ember.iss (Inno Setup) and
packaging/linux/build-appimage.sh.
"""
import os
import re
import sys
from pathlib import Path

import PyInstaller.__main__

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / "build" / "pyinstaller"


def version_file():
    """Ember.exe's Properties → Details, from the one version number in
    server.py."""
    v = re.search(r'^VERSION = "([^"]+)"', (REPO / "server.py").read_text(encoding="utf-8"),
                  re.M).group(1)
    n = tuple((list(map(int, re.findall(r"\d+", v))) + [0] * 4)[:4])
    strings = {"CompanyName": "Pierre Beaucoral", "FileDescription": "Ember",
               "FileVersion": v, "InternalName": "Ember", "OriginalFilename": "Ember.exe",
               "ProductName": "Ember", "ProductVersion": v,
               "LegalCopyright": "MIT License, Pierre Beaucoral"}
    f = BUILD / "version.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        f"VSVersionInfo(ffi=FixedFileInfo(filevers={n}, prodvers={n}, mask=0x3f, flags=0x0,"
        " OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),"
        " kids=[StringFileInfo([StringTable('040904B0', ["
        + ", ".join(f"StringStruct({k!r}, {val!r})" for k, val in strings.items())
        + "])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])])\n", encoding="utf-8")
    return f


def main():
    data = [("index.html", "."), ("addons.json", "."), ("vendor", "vendor"),
            ("docs/assets/layout.svg", "docs/assets"),          # Home's tour picture
            ("launchers/linux/claude-devtools.svg", ".")]      # --install's icon
    args = [str(REPO / "native" / "window.py"), "--name", "Ember", "--noconfirm",
            "--distpath", str(REPO / "dist"), "--workpath", str(BUILD),
            "--specpath", str(BUILD),
            # window.py puts these on sys.path at run time; tell the analyser
            "--paths", str(REPO), "--paths", str(REPO / "tools"),
            "--hidden-import", "server", "--hidden-import", "devtools_hooks",
            "--hidden-import", "winconpty"]
    for src, dest in data:
        args += ["--add-data", f"{REPO / src}{os.pathsep}{dest}"]
    if sys.platform == "win32":
        # no console window; window.py rebuilds stdio for hooks and the server
        args += ["--windowed", "--icon", str(REPO / "launchers" / "windows" / "claude-devtools.ico"),
                 "--version-file", str(version_file())]
    PyInstaller.__main__.run(args)


if __name__ == "__main__":
    main()
