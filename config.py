"""Zarządzanie konfiguracją aplikacji DynamicBar.

Ustawienia są przechowywane w pliku config.json w folderze
%APPDATA%\\DynamicBar (czyli AppData\\Roaming\\DynamicBar), dzięki czemu
nie znikają podczas aktualizacji programu.
"""

import copy
import json
import os
import tempfile
import threading
from pathlib import Path

APP_NAME = "DynamicBar"
CONFIG_FILENAME = "config.json"

VALID_POSITIONS = (
    "top-left",
    "top-center",
    "top-right",
    "bottom-left",
    "bottom-center",
    "bottom-right",
)

VALID_TRIGGERS = ("hover", "click")
VALID_TEMPERATURE_UNITS = ("celsius", "fahrenheit")

OFFSET_LIMIT = 10000
WEATHER_REFRESH_RANGE = (5, 180)  # minuty

DEFAULT_CONFIG = {
    "bar_position": "top-center",
    # Przesunięcie paska względem pozycji bazowej, w pikselach ekranu
    # (X dodatni = w prawo, Y dodatni = w dół).
    "offset_x": 0,
    "offset_y": 0,
    # "hover" - pasek rozwija się po najechaniu, kliknięcie go przypina;
    # "click" - pasek rozwija się dopiero po kliknięciu.
    "expand_trigger": "hover",
    # false = oczka są statyczne (całkowite ukrycie: modules.eyes = false)
    "animated_eyes": True,
    "weather_city": "Kielce",
    # "celsius" albo "fahrenheit"
    "temperature_unit": "celsius",
    # Co ile minut odświeżać pogodę (5-180).
    "weather_refresh_minutes": 15,
    "timezone": "Europe/Warsaw",
    # true = 24-godzinny zegar, false = 12-godzinny (AM/PM)
    "clock_24h": True,
    "gemini_api_key": "",
    "autostart": False,
    # --- alerty systemowe (system_monitor.py) ---
    # Główny przełącznik; false = monitor w ogóle się nie uruchamia.
    "alerts_enabled": True,
    "alert_usb": True,            # podłączenie / odłączenie USB
    "alert_network": True,        # Wi-Fi, Bluetooth, tryb samolotowy
    "alert_keys": True,           # CAPS LOCK / NUM LOCK
    "alert_notifications": True,  # powiadomienia systemowe Windows
    "modules": {
        "eyes": True,
        "clock": True,
        "weather": True,
        "gemini": True,
        "volume": True,
        "brightness": True,
        "wifi": True,
        "bluetooth": True,
    },
}

_lock = threading.RLock()
_cache = None


def get_config_dir():
    """Zwraca folder konfiguracji: %APPDATA%\\DynamicBar."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        base = Path(appdata)
    else:
        base = Path.home() / "AppData" / "Roaming"
    return base / APP_NAME


def get_config_path():
    """Zwraca pełną ścieżkę do pliku config.json."""
    return get_config_dir() / CONFIG_FILENAME


def _default_copy():
    return copy.deepcopy(DEFAULT_CONFIG)


def _sanitize(raw):
    """Łączy wczytane dane z domyślnymi i poprawia niepoprawne typy."""
    result = _default_copy()
    if not isinstance(raw, dict):
        return result

    for key, default_value in DEFAULT_CONFIG.items():
        if key not in raw:
            continue
        value = raw[key]

        if key == "modules":
            if isinstance(value, dict):
                for module_name, module_default in default_value.items():
                    if module_name in value and isinstance(value[module_name], bool):
                        result["modules"][module_name] = value[module_name]
                for module_name, module_value in value.items():
                    if module_name not in default_value and isinstance(module_value, bool):
                        result["modules"][module_name] = module_value
            continue

        if isinstance(default_value, bool):
            if isinstance(value, bool):
                result[key] = value
        elif isinstance(default_value, int):
            # bool jest podklasą int, więc odrzucamy go jawnie
            if isinstance(value, int) and not isinstance(value, bool):
                result[key] = max(-OFFSET_LIMIT, min(OFFSET_LIMIT, value))
        elif isinstance(default_value, str):
            if isinstance(value, str):
                result[key] = value.strip()

    if result["bar_position"] not in VALID_POSITIONS:
        result["bar_position"] = DEFAULT_CONFIG["bar_position"]
    result["expand_trigger"] = result["expand_trigger"].lower()
    if result["expand_trigger"] not in VALID_TRIGGERS:
        result["expand_trigger"] = DEFAULT_CONFIG["expand_trigger"]
    result["temperature_unit"] = result["temperature_unit"].lower()
    if result["temperature_unit"] not in VALID_TEMPERATURE_UNITS:
        result["temperature_unit"] = DEFAULT_CONFIG["temperature_unit"]
    low, high = WEATHER_REFRESH_RANGE
    result["weather_refresh_minutes"] = max(low, min(high, result["weather_refresh_minutes"]))
    if not result["weather_city"]:
        result["weather_city"] = DEFAULT_CONFIG["weather_city"]
    if not result["timezone"]:
        result["timezone"] = DEFAULT_CONFIG["timezone"]

    return result


def _write_atomic(path, data):
    """Bezpieczny zapis: najpierw plik tymczasowy, potem podmiana."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix="config_", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            json.dump(data, tmp_file, indent=4, ensure_ascii=False)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


def _backup_corrupted(path):
    """Zachowuje uszkodzony plik jako config.json.bak."""
    backup = path.with_name(path.name + ".bak")
    try:
        os.replace(path, backup)
    except OSError:
        pass


def save_config(config):
    """Zapisuje całą konfigurację na dysk. Zwraca True, gdy się udało."""
    global _cache
    with _lock:
        clean = _sanitize(config)
        try:
            _write_atomic(get_config_path(), clean)
        except OSError:
            return False
        _cache = clean
        return True


def load_config(force_reload=False):
    """Wczytuje konfigurację. Gdy plik nie istnieje lub jest uszkodzony,
    tworzy go na nowo z ustawieniami domyślnymi."""
    global _cache
    with _lock:
        if _cache is not None and not force_reload:
            return copy.deepcopy(_cache)

        path = get_config_path()

        if not path.exists():
            config = _default_copy()
            save_config(config)
            _cache = config
            return copy.deepcopy(config)

        try:
            with open(path, "r", encoding="utf-8") as config_file:
                raw = json.load(config_file)
        except (json.JSONDecodeError, UnicodeDecodeError):
            _backup_corrupted(path)
            config = _default_copy()
            save_config(config)
            _cache = config
            return copy.deepcopy(config)
        except OSError:
            config = _default_copy()
            _cache = config
            return copy.deepcopy(config)

        config = _sanitize(raw)
        if config != raw:
            save_config(config)
        _cache = config
        return copy.deepcopy(config)


def reset_config():
    """Przywraca ustawienia domyślne i zapisuje je na dysk."""
    with _lock:
        config = _default_copy()
        save_config(config)
        return copy.deepcopy(config)


def get_setting(key, default=None):
    """Zwraca pojedyncze ustawienie. Obsługuje zapis z kropką,
    np. get_setting("modules.weather")."""
    with _lock:
        value = load_config()
        for part in key.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return default
        return value


def set_setting(key, value):
    """Ustawia i zapisuje pojedyncze ustawienie. Obsługuje zapis z kropką,
    np. set_setting("modules.weather", False). Zwraca True, gdy się udało."""
    with _lock:
        config = load_config()
        parts = key.split(".")

        if len(parts) == 1:
            if parts[0] not in DEFAULT_CONFIG:
                return False
            config[parts[0]] = value
        elif len(parts) == 2 and parts[0] == "modules":
            if not isinstance(value, bool):
                return False
            config["modules"][parts[1]] = value
        else:
            return False

        if parts[0] != "modules":
            expected = value.strip() if isinstance(value, str) else value
            if _sanitize(config)[parts[0]] != expected:
                return False

        return save_config(config)


def is_module_enabled(module_name):
    """Sprawdza, czy dany moduł jest włączony."""
    return bool(get_setting("modules." + module_name, False))


def set_module_enabled(module_name, enabled):
    """Włącza lub wyłącza dany moduł."""
    return set_setting("modules." + module_name, bool(enabled))


if __name__ == "__main__":
    current = load_config()
    print("Ścieżka konfiguracji:", get_config_path())
    print(json.dumps(current, indent=4, ensure_ascii=False))
