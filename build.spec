# -*- mode: python ; coding: utf-8 -*-
# Budowanie:  pyinstaller build.spec --noconfirm --clean
# Wynik:      dist\DynamicBar\DynamicBar.exe  (tryb onedir - wymagany przez setup.iss)

import importlib.util
import os

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)


def has(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


hiddenimports = []
datas = []
binaries = []


def safe(func, *args):
    try:
        return func(*args)
    except Exception as exc:  # brak pakietu nie ma przerywać budowania
        print(f"[build.spec] pominięto {args}: {exc}")
        return []


# --- media_player.py ładuje WinRT dynamicznie (importlib) - PyInstaller tego nie widzi ---
WINRT_MODULES = [
    "winrt",
    "winrt.system",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.media.control",
    "winrt.windows.storage.streams",
    "winrt.windows.devices.radios",
    "winrt.windows.ui.notifications",
    "winrt.windows.ui.notifications.management",
]
hiddenimports += [m for m in WINRT_MODULES if has(m)]
if has("winrt"):
    hiddenimports += safe(collect_submodules, "winrt")
    binaries += safe(collect_dynamic_libs, "winrt")

# alternatywnie, gdyby ktoś używał winsdk zamiast winrt
if has("winsdk"):
    hiddenimports += safe(collect_submodules, "winsdk")
    binaries += safe(collect_dynamic_libs, "winsdk")

# --- sys_controls.py: pycaw + comtypes + screen_brightness_control ---
for pkg in ("pycaw", "comtypes", "screen_brightness_control"):
    if has(pkg):
        hiddenimports += safe(collect_submodules, pkg)

hiddenimports += [
    "wmi",
    "win32api",
    "win32con",
    "win32com",
    "win32com.client",
    "pythoncom",
    "pywintypes",
    "winreg",
    "sqlite3",
    "zoneinfo",
]

# --- strefy czasowe (settings_dialog używa available_timezones) ---
if has("tzdata"):
    datas += safe(collect_data_files, "tzdata")
    hiddenimports.append("tzdata")

# --- opcjonalna ikona (assets\DynamicBar.ico) ---
ICON = os.path.join("assets", "DynamicBar.ico")
icon_arg = ICON if os.path.exists(ICON) else None

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PyQt5", "PyQt6", "unittest", "pydoc"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DynamicBar",
    debug=False,
    strip=False,
    upx=False,          # UPX zwiększa liczbę fałszywych alarmów antywirusów
    console=False,      # aplikacja okienkowa (do debugowania ustaw True)
    icon=icon_arg,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="DynamicBar",
)
