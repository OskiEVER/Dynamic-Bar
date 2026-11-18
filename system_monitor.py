"""system_monitor.py - monitor zdarzeń systemowych Windows 11 dla DynamicBar.

Cały monitoring działa w jednym wątku QThread (SystemMonitor) i zgłasza
zdarzenia wyłącznie sygnałem `alert(kind, title, message)`, który główne okno
odbiera w wątku GUI.

Wykrywane zdarzenia:
  * USB            - ukryte okno + RegisterDeviceNotification (WM_DEVICECHANGE),
                     bez zależności zewnętrznych; nazwa urządzenia z rejestru.
  * Wi-Fi          - połączenie/rozłączenie (netsh wlan) oraz radio Wi-Fi (WinRT).
  * Bluetooth      - radio włączone/wyłączone (WinRT Windows.Devices.Radios).
  * Tryb samolotowy- rejestr RadioManagement\\SystemRadioState.
  * CAPS/NUM LOCK  - GetKeyState.
  * Powiadomienia  - WinRT UserNotificationListener; gdy dostęp jest niedostępny
                     (typowo dla niespakietowanej aplikacji Python), bezpieczny
                     fallback: odczyt kopii bazy wpndatabase.db centrum powiadomień.

Opcjonalne pakiety (bez nich działa USB, klawisze, tryb samolotowy i połączenie
Wi-Fi, a Bluetooth/radio Wi-Fi/UserNotificationListener są pomijane):

    pip install winrt-runtime winrt-Windows.Foundation winrt-Windows.Foundation.Collections ^
                winrt-Windows.Devices.Radios winrt-Windows.UI.Notifications ^
                winrt-Windows.UI.Notifications.Management

Kind zdarzenia: "usb" | "wifi" | "bluetooth" | "airplane" | "key" | "notification".
"""

import asyncio
import ctypes
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QThread, Signal

log = logging.getLogger("DynamicBar.monitor")

IS_WINDOWS = sys.platform == "win32"

# --------------------------------------------------------------------------
# Stałe Win32
# --------------------------------------------------------------------------
WM_DEVICECHANGE = 0x0219
DBT_DEVICEARRIVAL = 0x8000
DBT_DEVICEREMOVECOMPLETE = 0x8004
DBT_DEVTYP_DEVICEINTERFACE = 5
DEVICE_NOTIFY_WINDOW_HANDLE = 0
PM_REMOVE = 0x0001
QS_ALLINPUT = 0x04FF

VK_CAPITAL = 0x14
VK_NUMLOCK = 0x90

CREATE_NO_WINDOW = 0x08000000

POLL_INTERVAL_MS = 100       # krok pętli (klawisze, komunikaty okna)
NETWORK_EVERY = 2.0          # s
NOTIFICATIONS_EVERY = 1.5    # s
USB_DEDUPE_S = 1.5
AIRPLANE_SUPPRESS_S = 4.0    # tryb samolotowy przełącza radia - nie dubluj alertów
MAX_NOTIFICATIONS_PER_POLL = 2


# --------------------------------------------------------------------------
# Struktury Win32 (tylko Windows)
# --------------------------------------------------------------------------
if IS_WINDOWS:

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class DEV_BROADCAST_HDR(ctypes.Structure):
        _fields_ = [
            ("dbch_size", wintypes.DWORD),
            ("dbch_devicetype", wintypes.DWORD),
            ("dbch_reserved", wintypes.DWORD),
        ]

    class DEV_BROADCAST_DEVICEINTERFACE_W(ctypes.Structure):
        _fields_ = [
            ("dbcc_size", wintypes.DWORD),
            ("dbcc_devicetype", wintypes.DWORD),
            ("dbcc_reserved", wintypes.DWORD),
            ("dbcc_classguid", GUID),
            ("dbcc_name", ctypes.c_wchar * 1),
        ]

    class WNDCLASSW(ctypes.Structure):
        pass

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )
    WNDCLASSW._fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]

    # GUID_DEVINTERFACE_USB_DEVICE {A5DCBF10-6530-11D2-901F-00C04FB951ED}
    GUID_USB_DEVICE = GUID(
        0xA5DCBF10,
        0x6530,
        0x11D2,
        (ctypes.c_ubyte * 8)(0x90, 0x1F, 0x00, 0xC0, 0x4F, 0xB9, 0x51, 0xED),
    )

    # Osobne instancje DLL, żeby nie zmieniać argtypes globalnych ctypes.windll.
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    _user32.DefWindowProcW.restype = ctypes.c_ssize_t
    _user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    _user32.RegisterClassW.restype = wintypes.ATOM
    _user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    _user32.UnregisterClassW.restype = wintypes.BOOL
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.RegisterDeviceNotificationW.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
    ]
    _user32.RegisterDeviceNotificationW.restype = ctypes.c_void_p
    _user32.UnregisterDeviceNotification.argtypes = [ctypes.c_void_p]
    _user32.UnregisterDeviceNotification.restype = wintypes.BOOL
    _user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT,
    ]
    _user32.PeekMessageW.restype = wintypes.BOOL
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.restype = ctypes.c_ssize_t
    _user32.MsgWaitForMultipleObjects.argtypes = [
        wintypes.DWORD, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, wintypes.DWORD,
    ]
    _user32.MsgWaitForMultipleObjects.restype = wintypes.DWORD
    _user32.GetKeyState.argtypes = [ctypes.c_int]
    _user32.GetKeyState.restype = ctypes.c_short
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _kernel32.GetOEMCP.restype = wintypes.UINT


# --------------------------------------------------------------------------
# USB: ukryte okno odbierające WM_DEVICECHANGE
# --------------------------------------------------------------------------
class _UsbWindow:
    """Ukryte okno najwyższego poziomu (nigdy niepokazywane) zarejestrowane
    na powiadomienia o interfejsach USB. Musi żyć i być obsługiwane w tym samym
    wątku (pump), w którym je utworzono."""

    CLASS_NAME = "DynamicBarUsbMonitorWindow"

    def __init__(self):
        self.events = []  # (arrival: bool, ścieżka urządzenia)
        self._hwnd = None
        self._notify = None
        self._hinst = _kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._on_message)  # referencja chroni przed GC

        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = self._hinst
        wc.lpszClassName = self.CLASS_NAME
        _user32.RegisterClassW(ctypes.byref(wc))  # błąd "już istnieje" ignorujemy

        self._hwnd = _user32.CreateWindowExW(
            0, self.CLASS_NAME, "DynamicBarUsbMonitor", 0,
            0, 0, 0, 0, None, None, self._hinst, None,
        )
        if not self._hwnd:
            raise OSError("Nie udało się utworzyć okna monitora USB")

        flt = DEV_BROADCAST_DEVICEINTERFACE_W()
        flt.dbcc_size = ctypes.sizeof(DEV_BROADCAST_DEVICEINTERFACE_W)
        flt.dbcc_devicetype = DBT_DEVTYP_DEVICEINTERFACE
        flt.dbcc_classguid = GUID_USB_DEVICE
        self._notify = _user32.RegisterDeviceNotificationW(
            self._hwnd, ctypes.byref(flt), DEVICE_NOTIFY_WINDOW_HANDLE
        )
        if not self._notify:
            log.warning("RegisterDeviceNotification nie powiodło się: %s", ctypes.get_last_error())

    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_DEVICECHANGE and wparam in (DBT_DEVICEARRIVAL, DBT_DEVICEREMOVECOMPLETE):
                if lparam:
                    hdr = ctypes.cast(lparam, ctypes.POINTER(DEV_BROADCAST_HDR)).contents
                    if hdr.dbch_devicetype == DBT_DEVTYP_DEVICEINTERFACE:
                        offset = DEV_BROADCAST_DEVICEINTERFACE_W.dbcc_name.offset
                        path = ctypes.wstring_at(lparam + offset)
                        self.events.append((wparam == DBT_DEVICEARRIVAL, path))
        except Exception:  # wyjątek w callbacku nie może wyciec do Win32
            log.exception("Błąd w procedurze okna USB")
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def wait_and_pump(self, timeout_ms):
        """Czeka maksymalnie timeout_ms na komunikaty i je obsługuje."""
        _user32.MsgWaitForMultipleObjects(0, None, False, timeout_ms, QS_ALLINPUT)
        msg = wintypes.MSG()
        while _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    def drain(self):
        events, self.events = self.events, []
        return events

    def close(self):
        if self._notify:
            _user32.UnregisterDeviceNotification(self._notify)
            self._notify = None
        if self._hwnd:
            _user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        _user32.UnregisterClassW(self.CLASS_NAME, self._hinst)


def _usb_friendly_name(path):
    """Nazwa urządzenia USB z rejestru (wpis zostaje po odłączeniu) albo VID:PID."""
    match = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", path)
    fallback = "USB %s:%s" % match.groups() if match else "urządzenie USB"
    parts = path.split("#")
    if len(parts) < 3:
        return fallback
    try:
        import winreg

        key_path = r"SYSTEM\CurrentControlSet\Enum\USB\%s\%s" % (parts[1], parts[2])
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            for value_name in ("FriendlyName", "DeviceDesc"):
                try:
                    value, _ = winreg.QueryValueEx(key, value_name)
                except OSError:
                    continue
                name = str(value).split(";")[-1].strip()
                if name:
                    return name
    except OSError:
        pass
    return fallback


# --------------------------------------------------------------------------
# Sieć / radia
# --------------------------------------------------------------------------
def _read_airplane_mode():
    """True/False albo None, gdy system nie udostępnia tej wartości."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\RadioManagement"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "SystemRadioState")
            return bool(value)
    except OSError:
        return None


def _read_wifi_ssid():
    """SSID aktualnie połączonej sieci albo None (rozłączono / brak karty).
    Parsowanie niezależne od języka systemu: szukamy linii „SSID : …"."""
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            timeout=3,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    encoding = "cp%d" % _kernel32.GetOEMCP()
    try:
        text = result.stdout.decode(encoding, errors="replace")
    except LookupError:
        text = result.stdout.decode("utf-8", errors="replace")
    for line in text.splitlines():
        match = re.match(r"^\s*SSID\s*:\s*(.+?)\s*$", line)  # BSSID nie pasuje
        if match:
            return match.group(1)
    return None


async def _await(operation):
    return await operation


# --------------------------------------------------------------------------
# Powiadomienia: fallback z bazy centrum powiadomień
# --------------------------------------------------------------------------
class _ToastDbReader:
    """Odczyt nowych toastów z %LOCALAPPDATA%\\Microsoft\\Windows\\Notifications\\
    wpndatabase.db (kopiowanej do katalogu tymczasowego, bo plik jest używany
    przez system). To nieudokumentowany format - traktuj jako fallback."""

    def __init__(self):
        base = os.environ.get("LOCALAPPDATA")
        self.path = (
            Path(base) / "Microsoft" / "Windows" / "Notifications" / "wpndatabase.db"
            if base else None
        )
        self._signature = None
        self._last_id = None  # None = jeszcze nie zasiano (pierwszy odczyt bez alertów)

    def available(self):
        return self.path is not None and self.path.exists()

    def _current_signature(self):
        parts = []
        for suffix in ("", "-wal"):
            candidate = Path(str(self.path) + suffix)
            try:
                stat = candidate.stat()
                parts.append((stat.st_mtime_ns, stat.st_size))
            except OSError:
                parts.append(None)
        return tuple(parts)

    def poll(self):
        """Zwraca listę (aplikacja, tekst) nowych powiadomień."""
        signature = self._current_signature()
        if signature == self._signature:
            return []
        self._signature = signature

        results = []
        try:
            with tempfile.TemporaryDirectory(prefix="dynamicbar_wpn_") as tmp:
                target = Path(tmp) / "wpn.db"
                for suffix in ("", "-wal", "-shm"):
                    source = Path(str(self.path) + suffix)
                    if source.exists():
                        shutil.copy2(source, Path(str(target) + suffix))
                connection = sqlite3.connect(str(target))
                try:
                    if self._last_id is None:
                        row = connection.execute("SELECT MAX(Id) FROM Notification").fetchone()
                        self._last_id = (row[0] or 0) if row else 0
                        return []
                    rows = connection.execute(
                        "SELECT n.Id, h.PrimaryId, n.Payload FROM Notification n "
                        "LEFT JOIN NotificationHandler h ON n.HandlerId = h.RecordId "
                        "WHERE n.Id > ? AND n.Type = 'toast' ORDER BY n.Id",
                        (self._last_id,),
                    ).fetchall()
                    if rows:
                        self._last_id = rows[-1][0]
                    for _id, primary, payload in rows:
                        app, text = self._parse(primary, payload)
                        if text:
                            results.append((app, text))
                finally:
                    connection.close()
        except (OSError, sqlite3.Error) as exc:
            log.debug("Odczyt wpndatabase.db nie powiódł się: %s", exc)
        return results

    @staticmethod
    def _parse(primary, payload):
        app = "Powiadomienie"
        if primary:
            name = str(primary).split("!")[0].split("_")[0].split(".")[-1]
            if name:
                app = name
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8-sig", errors="replace")
        try:
            root = ET.fromstring(payload)
        except (ET.ParseError, TypeError):
            return app, ""
        texts = [(el.text or "").strip() for el in root.iter("text")]
        return app, " — ".join(t for t in texts if t)


# --------------------------------------------------------------------------
# Wątek monitora
# --------------------------------------------------------------------------
class SystemMonitor(QThread):
    """Wątek monitorujący zdarzenia systemowe.

    Sygnał alert(kind, title, message) jest emitowany z tego wątku; Qt dostarcza
    go do slotu w wątku GUI. Opcje można zmieniać w trakcie działania
    (configure) - włączenie opcji ustawia nowy punkt odniesienia, więc nie
    pojawi się alert o stanie sprzed włączenia.
    """

    alert = Signal(str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.opt_usb = True
        self.opt_network = True
        self.opt_keys = True
        self.opt_notifications = True

        self._usb_window = None
        self._usb_seen = {}

        self._keys_primed = False
        self._caps = None
        self._num = None

        self._net_primed = False
        self._net_state = {}
        self._suppress_until = 0.0
        self._radio_loop = None
        self._radio_ready = None  # None = jeszcze nie próbowano, False = niedostępne

        self._notif_primed = False
        self._listener = None
        self._db_reader = None
        self._seen_ids = set()

    # ------------------------------------------------------------ API
    def configure(self, usb=True, network=True, keys=True, notifications=True):
        if keys and not self.opt_keys:
            self._keys_primed = False
        if network and not self.opt_network:
            self._net_primed = False
        if notifications and not self.opt_notifications:
            self._notif_primed = False
            self._seen_ids = set()
            self._db_reader = None
        self.opt_usb = bool(usb)
        self.opt_network = bool(network)
        self.opt_keys = bool(keys)
        self.opt_notifications = bool(notifications)

    def stop(self, timeout_ms=6000):
        self.requestInterruption()
        self.wait(timeout_ms)

    def _emit(self, kind, title, message):
        self.alert.emit(kind, title, message)

    # ------------------------------------------------------------ pętla
    def run(self):
        if not IS_WINDOWS:
            log.info("SystemMonitor działa tylko w Windows - wątek kończy pracę.")
            return

        self._radio_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._radio_loop)
        try:
            self._usb_window = _UsbWindow()
        except OSError:
            log.exception("Monitor USB niedostępny")
            self._usb_window = None

        last_network = 0.0
        last_notifications = 0.0
        try:
            while not self.isInterruptionRequested():
                if self._usb_window is not None:
                    self._usb_window.wait_and_pump(POLL_INTERVAL_MS)
                    self._handle_usb(self._usb_window.drain())
                else:
                    self.msleep(POLL_INTERVAL_MS)

                now = time.monotonic()
                try:
                    if self.opt_keys:
                        self._poll_keys()
                    if self.opt_network and now - last_network >= NETWORK_EVERY:
                        last_network = now
                        self._poll_network(now)
                    if self.opt_notifications and now - last_notifications >= NOTIFICATIONS_EVERY:
                        last_notifications = now
                        self._poll_notifications()
                except Exception:  # pojedynczy błąd nie może zabić monitora
                    log.exception("Błąd podczas odpytywania zdarzeń systemowych")
        finally:
            if self._usb_window is not None:
                self._usb_window.close()
                self._usb_window = None
            if self._radio_loop is not None:
                self._radio_loop.close()
                self._radio_loop = None

    # ------------------------------------------------------------ USB
    def _handle_usb(self, events):
        if not events or not self.opt_usb:
            return
        now = time.monotonic()
        for arrival, path in events:
            key = (arrival, path.lower())
            if now - self._usb_seen.get(key, 0.0) < USB_DEDUPE_S:
                continue
            self._usb_seen[key] = now
            name = _usb_friendly_name(path)
            if "root hub" in name.lower():
                continue
            self._emit("usb", "USB", ("Podłączono: " if arrival else "Odłączono: ") + name)
        if len(self._usb_seen) > 64:
            self._usb_seen = {k: t for k, t in self._usb_seen.items() if now - t < 5.0}

    # ------------------------------------------------------------ klawisze
    def _poll_keys(self):
        caps = bool(_user32.GetKeyState(VK_CAPITAL) & 1)
        num = bool(_user32.GetKeyState(VK_NUMLOCK) & 1)
        if not self._keys_primed:
            self._caps, self._num, self._keys_primed = caps, num, True
            return
        if caps != self._caps:
            self._caps = caps
            self._emit("key", "Klawiatura", "CAPS LOCK: " + ("WŁĄCZONY" if caps else "WYŁĄCZONY"))
        if num != self._num:
            self._num = num
            self._emit("key", "Klawiatura", "NUM LOCK: " + ("WŁĄCZONY" if num else "WYŁĄCZONY"))

    # ------------------------------------------------------------ sieć / radia
    def _read_radios(self):
        """{"wifi": bool, "bt": bool} z WinRT albo {} gdy niedostępne."""
        if self._radio_ready is False:
            return {}
        try:
            from winrt.windows.devices.radios import Radio, RadioKind, RadioState

            if self._radio_ready is None:
                self._radio_loop.run_until_complete(_await(Radio.request_access_async()))
                self._radio_ready = True
            radios = self._radio_loop.run_until_complete(_await(Radio.get_radios_async()))
            result = {}
            for radio in radios:
                if radio.kind == RadioKind.WI_FI:
                    result["wifi"] = radio.state == RadioState.ON
                elif radio.kind == RadioKind.BLUETOOTH:
                    result["bt"] = radio.state == RadioState.ON
            return result
        except ImportError:
            log.info("Brak pakietu winrt-Windows.Devices.Radios - pomijam radia Wi-Fi/Bluetooth.")
            self._radio_ready = False
        except Exception as exc:
            log.warning("Odczyt radii WinRT nie powiódł się: %s", exc)
            self._radio_ready = False
        return {}

    def _poll_network(self, now):
        radios = self._read_radios()
        wifi_radio = radios.get("wifi")
        ssid = _read_wifi_ssid()
        if wifi_radio is False:
            wifi_state = "off"
        elif ssid:
            wifi_state = "connected:" + ssid
        else:
            wifi_state = "disconnected"

        state = {
            "airplane": _read_airplane_mode(),
            "wifi": wifi_state,
            "bt": radios.get("bt"),
        }

        if not self._net_primed:
            self._net_state, self._net_primed = state, True
            return

        previous = self._net_state
        self._net_state = state

        if state["airplane"] is not None and previous.get("airplane") not in (None, state["airplane"]):
            self._suppress_until = now + AIRPLANE_SUPPRESS_S
            self._emit(
                "airplane", "Tryb samolotowy", "WŁĄCZONY" if state["airplane"] else "WYŁĄCZONY"
            )
            return

        if now < self._suppress_until:
            return  # radia zmieniają się razem z trybem samolotowym

        if previous.get("wifi") != state["wifi"]:
            if state["wifi"] == "off":
                message = "Radio WYŁĄCZONE"
            elif state["wifi"].startswith("connected:"):
                message = "POŁĄCZONO z " + state["wifi"].split(":", 1)[1]
            else:
                message = "ROZŁĄCZONO"
            self._emit("wifi", "Wi-Fi", message)

        if state["bt"] is not None and previous.get("bt") not in (None, state["bt"]):
            self._emit("bluetooth", "Bluetooth", "WŁĄCZONY" if state["bt"] else "WYŁĄCZONY")

    # ------------------------------------------------------------ powiadomienia
    def _init_notifications(self):
        """Próbuje UserNotificationListener; przy niepowodzeniu włącza fallback."""
        try:
            from winrt.windows.ui.notifications.management import (
                UserNotificationListener,
                UserNotificationListenerAccessStatus,
            )

            listener = UserNotificationListener.current
            status = self._radio_loop.run_until_complete(_await(listener.request_access_async()))
            if status == UserNotificationListenerAccessStatus.ALLOWED:
                self._listener = listener
                log.info("Powiadomienia: UserNotificationListener (WinRT).")
                return
            log.info("UserNotificationListener niedostępny (%s) - używam fallbacku.", status)
        except ImportError:
            log.info("Brak pakietów winrt dla powiadomień - używam fallbacku.")
        except Exception as exc:
            log.info("UserNotificationListener nie działa (%s) - używam fallbacku.", exc)

        self._listener = None
        reader = _ToastDbReader()
        if reader.available():
            self._db_reader = reader
            log.info("Powiadomienia: fallback wpndatabase.db.")
        else:
            log.info("Powiadomienia: brak dostępnego źródła - funkcja wyłączona.")

    def _poll_notifications(self):
        if not self._notif_primed:
            self._listener = None
            self._db_reader = None
            self._init_notifications()
            self._notif_primed = True
            if self._listener is not None:
                self._collect_listener(seed=True)
            elif self._db_reader is not None:
                self._db_reader.poll()  # zasiew punktu odniesienia
            return

        if self._listener is not None:
            items = self._collect_listener(seed=False)
        elif self._db_reader is not None:
            items = self._db_reader.poll()
        else:
            return

        for app, text in items[:MAX_NOTIFICATIONS_PER_POLL]:
            self._emit("notification", app, text)
        extra = len(items) - MAX_NOTIFICATIONS_PER_POLL
        if extra > 0:
            self._emit("notification", "Powiadomienia", "+%d nowych powiadomień" % extra)

    def _collect_listener(self, seed):
        """Nowe toasty z UserNotificationListener jako lista (aplikacja, tekst)."""
        from winrt.windows.ui.notifications import NotificationKinds

        try:
            notifications = self._radio_loop.run_until_complete(
                _await(self._listener.get_notifications_async(NotificationKinds.TOAST))
            )
        except Exception as exc:
            log.warning("Odczyt powiadomień nie powiódł się: %s", exc)
            return []

        current_ids = set()
        items = []
        for notification in notifications:
            nid = notification.id
            current_ids.add(nid)
            if seed or nid in self._seen_ids:
                continue
            try:
                app = notification.app_info.display_info.display_name or "Powiadomienie"
                binding = notification.notification.visual.get_binding("ToastGeneric")
                texts = [el.text for el in binding.get_text_elements() if el.text]
            except Exception:
                continue
            if texts:
                items.append((app, " — ".join(texts)))
        self._seen_ids = current_ids
        return items
