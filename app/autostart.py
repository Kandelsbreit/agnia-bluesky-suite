from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "AgniaBlueskySuite"


def launch_command(start_minimized: bool = True) -> str:
    tray_arg = " --tray" if start_minimized else ""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"{tray_arg}'
    python_exe = Path(sys.executable)
    pythonw = python_exe.with_name("pythonw.exe") if sys.platform == "win32" else python_exe
    script = Path(__file__).resolve().parent.parent / "main.py"
    return f'"{pythonw}" "{script}"{tray_arg}'


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except OSError:
        return False


def set_enabled(enabled: bool, start_minimized: bool = True) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Автозапуск через реестр доступен только в Windows"
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launch_command(start_minimized))
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True, "Автозапуск включён" if enabled else "Автозапуск выключен"
    except OSError as exc:
        return False, f"Не удалось изменить автозапуск: {exc}"


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])

