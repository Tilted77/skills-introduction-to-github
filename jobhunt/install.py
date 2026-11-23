"""One-time setup: installs what the tool needs, brings in your personal files, saves your API key,
and (on Windows) puts a "Job Hunt" shortcut on the desktop.

Run by install.ps1, or by hand:  python install.py
It is safe to run again; it keeps your files and settings.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PERSONAL = "jobhunt-personal"          # the zip of your own files, as downloaded from the chat
KEEP = ("config.toml", "profile.toml")  # single files allowed out of that zip; plus resume.* and data/


def venv_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(command: list, what: str) -> None:
    print(f"  {what}...", flush=True)
    if subprocess.run([str(c) for c in command], cwd=ROOT).returncode != 0:
        raise SystemExit(f"\nSetup stopped: {what.lower()} failed. Copy the messages above into the chat for help.")


def install_software(root: Path, browser: bool = True) -> None:
    python = venv_python(root)
    if not python.exists():
        run([sys.executable, "-m", "venv", root / ".venv"], "Creating a private Python environment")
    run([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", root / "requirements.txt"],
        "Installing the tool's libraries (a minute or two)")
    if browser:
        run([python, "-m", "playwright", "install", "chromium"], "Downloading the browser used to fill forms")


def find_personal_package(folders: list[Path]) -> Path | None:
    """The newest jobhunt-personal*.zip in the given folders (a browser may name a second copy "... (1).zip")."""
    found = [p for folder in folders if folder.is_dir() for p in folder.glob(f"{PERSONAL}*.zip")]
    return max(found, key=lambda p: p.stat().st_mtime, default=None)


def unpack_personal(package: Path, root: Path) -> list[str]:
    """Copy your files out of the zip. Only the expected names are taken; nothing can land outside `root`."""
    taken = []
    with zipfile.ZipFile(package) as archive:
        for item in archive.infolist():
            name = item.filename.replace("\\", "/")
            parts = name.split("/")
            wanted = name in KEEP or (len(parts) == 1 and name.startswith("resume.")) or parts[0] == "data"
            if item.is_dir() or not wanted or ".." in parts or name.startswith("/"):
                continue
            target = root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)
            taken.append(name)
    return taken


def desktop_folders() -> list[Path]:
    folders = [Path.home() / "Desktop"]
    for variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):  # OneDrive can take over the Desktop
        if os.environ.get(variable):
            folders.append(Path(os.environ[variable]) / "Desktop")
    return [f for f in folders if f.is_dir()]


def write_shortcut(root: Path, folder: Path) -> Path:
    """A double-clickable file that opens the menu."""
    shortcut = folder / "Job Hunt.bat"
    lines = ["@echo off", "title Job Hunt", f'cd /d "{root}"', r'".venv\Scripts\python.exe" -m jobhunt menu', "pause"]
    shortcut.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
    return shortcut


def main(argv: list[str]) -> None:
    print("\nSetting up Job Hunt in", ROOT)
    install_software(ROOT, browser="--skip-browser" not in argv)

    print("\nYour files")
    package = find_personal_package([Path.home() / "Downloads", ROOT])
    if package:
        taken = unpack_personal(package, ROOT)
        print(f"  Copied {len(taken)} files from {package.name} (your profile, search settings, resume and drafts).")
    elif (ROOT / "profile.toml").exists():
        print("  Already in place.")
    else:
        for name in KEEP:
            shutil.copy(ROOT / name.replace(".toml", ".example.toml"), ROOT / name)
        print(f"  I could not find {PERSONAL}.zip in your Downloads folder, so I created blank settings.")
        print(f"  Download {PERSONAL}.zip from the chat and run this setup again to bring in your own files.")

    sys.path.insert(0, str(ROOT))
    from jobhunt import config  # imported late: only the standard library is needed for it

    print("\nYour Claude API key")
    if config.load_api_key():
        print("  Already saved.")
    else:
        print("  The key lets the tool score new jobs and write cover letters. It starts with sk-ant-")
        while True:
            key = input("  Paste it here and press Enter (or just press Enter to do this later): ").strip()
            if not key:
                print("  Skipped. You can add it later from the menu.")
                break
            try:
                config.save_api_key(key)
                print("  Saved.")
                break
            except config.ConfigError as e:
                print(f"  {e}")

    print("\nAll set.")
    if os.name == "nt":
        places = desktop_folders() or [ROOT]
        shortcut = write_shortcut(ROOT, places[0])
        print(f'Double-click "{shortcut.name}" ({shortcut.parent}) to start. Choose 1 to open your applications.')
    else:
        print(f"Start with:  {venv_python(ROOT)} -m jobhunt menu")


if __name__ == "__main__":
    main(sys.argv[1:])
