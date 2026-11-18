# DynamicBar

A floating, expandable "pill" for your Windows desktop, inspired by the Dynamic Island. It sits at the edge of your screen as a tiny always-on-top bar with animated eyes, the weather and a clock. Hover over it (or click it) and it smoothly expands into a tabbed control panel.

Built with **Python** and **PySide6**, in a dark violet/neon style.

## Features

### Collapsed pill
- Animated digital eyes: they blink, glance around and follow your cursor
- Live clock and current weather (icon and temperature)

### Expandable panel (tabs)
| Tab | What it does |
|---|---|
| **Clock** | Time with seconds, full date in Polish, time zone and UTC offset |
| **Weather** | Current conditions for your city via [Open-Meteo](https://open-meteo.com/) (no API key needed) |
| **Music** | Controls any media app through Windows SMTC (Spotify, YouTube in a browser, VLC, ...): cover art, title, artist, prev/play/next, seek bar |
| **Clipboard** | History of the last 15 copied items (text, files and folders with system icons). Click an item to restore it |
| **Gemini** | Chat with Google Gemini straight from the bar (Markdown answers, copy button, conversation context) |
| **Controls** | System volume and mute, screen brightness, Wi-Fi and Bluetooth toggles |

### System alerts
A short animated pill notification appears when something happens:
- USB device plugged in or removed
- Wi-Fi connected or disconnected, Wi-Fi/Bluetooth radio on or off, airplane mode
- CAPS LOCK and NUM LOCK state changes
- Windows notifications

### Customization
- Position: top-left, top-center, top-right or bottom-center, plus X/Y pixel offsets with a live preview
- Expand on hover or on click, pin the bar open
- Enable or disable any module, animated or static eyes
- 12/24-hour clock, time zone, weather city and temperature unit
- Autostart with Windows
- Settings are stored in `%APPDATA%\DynamicBar\config.json` and reload live when the file changes
- System tray icon with a context menu

## Requirements

- Windows 10 or 11 (x64)
- Python 3.12 (3.9-3.13 supported by the WinRT packages)
- A free [Gemini API key](https://aistudio.google.com/apikey) for the AI tab (optional)

## Run from source

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Open the settings with the gear icon in the bar header and paste your Gemini API key there.

## Build the installer

1. Build the app with PyInstaller (one-folder mode):
   ```bat
   pyinstaller build.spec --noconfirm --clean
   ```
2. Install [Inno Setup 6](https://jrsoftware.org/isdl.php) and compile the installer (or open `setup.iss` and press **F9**):
   ```bat
   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" setup.iss
   ```
3. The result is `installer_output\DynamicBar_Setup.exe`.

The installer:
- installs to `Program Files\DynamicBar` and creates Desktop and Start Menu shortcuts
- updates an existing installation in place (same `AppId`), closing the running app first
- never touches your settings in `%APPDATA%\DynamicBar`
- offers an optional "Run DynamicBar at Windows startup" checkbox
- registers a full uninstaller (Settings > Apps) that removes the files and the autostart entry, and asks whether to delete your settings

## Project structure

| File | Purpose |
|---|---|
| `main.py` | Main window, expand/collapse animation, tabs, alerts, tray |
| `config.py` | Reading and writing `config.json` (atomic writes, validation, defaults) |
| `widgets.py` | Eyes, clock card, weather service and weather widgets |
| `media_player.py` | Windows SMTC media controller and player panel |
| `clipboard_manager.py` | Clipboard history monitor and panel |
| `ai_assistant.py` | Gemini chat panel and background worker |
| `sys_controls.py` | Volume (pycaw), brightness, Wi-Fi/Bluetooth radios (WinRT) |
| `system_monitor.py` | USB, network, key and notification monitoring thread |
| `settings_dialog.py` | Settings window, autostart and uninstall helpers |
| `build.spec`, `setup.iss`, `requirements.txt` | Packaging: PyInstaller, Inno Setup, dependencies |

## Adding your own module

Create a class that inherits from `Module`, decorate it with `@register_module`, and add its key to `DEFAULT_CONFIG["modules"]` in `config.py`. The tab appears automatically when the module is enabled.

## Tech stack

Python, PySide6 (Qt), pycaw, screen-brightness-control, WinRT bindings (`winrt-*`), Open-Meteo API, Google Gemini API, PyInstaller, Inno Setup.

## Privacy

Your Gemini API key is stored only on your computer in `config.json` and is sent only to Google's API. The clipboard history is kept in memory only and is never written to disk. Content flagged by password managers as excluded from clipboard monitoring is ignored.
