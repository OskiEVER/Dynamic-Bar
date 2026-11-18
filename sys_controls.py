"""sys_controls.py - panel szybkiej kontroli systemu dla DynamicBar (PySide6).

Zawiera:
  * suwak głośności systemowej (pycaw) z odczytem w % i przyciskiem wyciszenia,
  * suwak jasności ekranu (screen-brightness-control) - odczyt i zapis w wątku
    roboczym, odporny na monitory zewnętrzne bez DDC/CI,
  * przełączniki Wi-Fi i Bluetooth (radia Windows przez WinRT) z podświetleniem
    aktywnego stanu w fioletowo-niebieskim gradiencie.

Instalacja zależności (Windows):
    pip install pycaw screen-brightness-control
    pip install winrt-runtime winrt-Windows.Devices.Radios ^
                winrt-Windows.Foundation winrt-Windows.Foundation.Collections
  (alternatywnie, na Pythonie <= 3.12:  pip install winsdk)

Każdy element degraduje się łagodnie: gdy biblioteki lub sprzętu brakuje,
kontrolka zostaje wyszarzona z podpowiedzią, a aplikacja działa dalej.

Użycie w main.py:
    panel = QuickControlsPanel(cfg["modules"])   # klucze: volume/brightness/wifi/bluetooth
    panel.activate() / panel.deactivate()        # start/stop odpytywania
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from widgets import BG_CARD, BORDER_SOFT, MUTED, SKY, TEXT, VIOLET

# --------------------------------------------------------------------------
# Radia Windows (Wi-Fi / Bluetooth) przez WinRT
# --------------------------------------------------------------------------
try:
    from winrt.windows.devices.radios import (
        Radio,
        RadioAccessStatus,
        RadioKind,
        RadioState,
    )
except Exception:  # noqa: BLE001 - brak pakietu lub system inny niż Windows
    try:
        from winsdk.windows.devices.radios import (
            Radio,
            RadioAccessStatus,
            RadioKind,
            RadioState,
        )
    except Exception:  # noqa: BLE001
        Radio = RadioAccessStatus = RadioKind = RadioState = None

RADIO_AVAILABLE = Radio is not None

# Jeden wątek roboczy: operacje sprzętowe (DDC/CI, WinRT) nie blokują interfejsu
# i nie nakładają się na siebie.
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sys_controls")


def _radio_kind(key):
    return RadioKind.WI_FI if key == "wifi" else RadioKind.BLUETOOTH


async def _get_radios(key):
    status = await Radio.request_access_async()
    if status != RadioAccessStatus.ALLOWED:
        raise PermissionError("Windows odmówił dostępu do radia")
    radios = await Radio.get_radios_async()
    kind = _radio_kind(key)
    return [r for r in radios if r.kind == kind]


def radio_read(key):
    """Zwraca (stan, błąd). stan: True/False albo None, gdy brak adaptera."""
    if not RADIO_AVAILABLE:
        return None, "Brak pakietu winrt/winsdk"

    async def run():
        radios = await _get_radios(key)
        if not radios:
            return None
        return any(r.state == RadioState.ON for r in radios)

    try:
        state = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)
    return state, ("" if state is not None else "Nie znaleziono adaptera")


def radio_write(key, on):
    """Włącza/wyłącza radio. Zwraca (nowy_stan, błąd)."""
    if not RADIO_AVAILABLE:
        return None, "Brak pakietu winrt/winsdk"

    async def run():
        radios = await _get_radios(key)
        if not radios:
            raise RuntimeError("Nie znaleziono adaptera")
        for radio in radios:
            result = await radio.set_state_async(RadioState.ON if on else RadioState.OFF)
            if result != RadioAccessStatus.ALLOWED:
                raise PermissionError("Windows odmówił zmiany stanu radia")
        return on

    try:
        return asyncio.run(run()), ""
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


# --------------------------------------------------------------------------
# Jasność ekranu (screen-brightness-control)
# --------------------------------------------------------------------------
def _load_sbc():
    try:
        import screen_brightness_control as sbc

        return sbc
    except Exception:  # noqa: BLE001
        return None


def _as_values(raw):
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    return [int(v) for v in raw if isinstance(v, (int, float))]


def brightness_read():
    """Zwraca jasność 0-100 pierwszego działającego monitora albo None.

    Monitory zewnętrzne bez DDC/CI zgłaszają wyjątki lub puste listy -
    wtedy próbujemy każdy wyświetlacz osobno i w razie porażki zwracamy None.
    """
    sbc = _load_sbc()
    if sbc is None:
        return None
    try:
        values = _as_values(sbc.get_brightness())
        if values:
            return max(0, min(100, values[0]))
    except Exception:  # noqa: BLE001
        pass
    try:
        count = len(sbc.list_monitors())
    except Exception:  # noqa: BLE001
        count = 0
    for index in range(count):
        try:
            values = _as_values(sbc.get_brightness(display=index))
        except Exception:  # noqa: BLE001
            continue
        if values:
            return max(0, min(100, values[0]))
    return None


def brightness_write(value):
    """Ustawia jasność; True, jeśli choć jeden monitor ją przyjął."""
    sbc = _load_sbc()
    if sbc is None:
        return False
    try:
        sbc.set_brightness(value)
        return True
    except Exception:  # noqa: BLE001
        pass
    ok = False
    try:
        count = len(sbc.list_monitors())
    except Exception:  # noqa: BLE001
        count = 0
    for index in range(count):
        try:
            sbc.set_brightness(value, display=index)
            ok = True
        except Exception:  # noqa: BLE001
            continue
    return ok


# --------------------------------------------------------------------------
# Głośność systemowa (pycaw)
# --------------------------------------------------------------------------
class VolumeBackend:
    """Opakowanie IAudioEndpointVolume. Wywoływane w wątku GUI (szybkie, COM)."""

    def __init__(self):
        self._endpoint = None
        self.error = ""
        self._ensure()

    @staticmethod
    def _open():
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        device = AudioUtilities.GetSpeakers()
        endpoint = getattr(device, "EndpointVolume", None)  # nowsze wersje pycaw
        if endpoint is not None:
            return endpoint
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL

        iface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return cast(iface, POINTER(IAudioEndpointVolume))

    def _ensure(self):
        if self._endpoint is None:
            try:
                self._endpoint = self._open()
                self.error = ""
            except Exception as exc:  # noqa: BLE001
                self._endpoint = None
                self.error = str(exc) or "Brak urządzenia audio"
        return self._endpoint is not None

    def _drop(self):
        # np. zmiana urządzenia wyjściowego - przy następnym wywołaniu otwórz na nowo
        self._endpoint = None

    @property
    def available(self):
        return self._ensure()

    def get(self):
        if not self._ensure():
            return None
        try:
            return int(round(self._endpoint.GetMasterVolumeLevelScalar() * 100))
        except Exception:  # noqa: BLE001
            self._drop()
            return None

    def set(self, percent):
        if not self._ensure():
            return False
        try:
            self._endpoint.SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100.0, None)
            return True
        except Exception:  # noqa: BLE001
            self._drop()
            return False

    def get_mute(self):
        if not self._ensure():
            return False
        try:
            return bool(self._endpoint.GetMute())
        except Exception:  # noqa: BLE001
            self._drop()
            return False

    def set_mute(self, muted):
        if not self._ensure():
            return False
        try:
            self._endpoint.SetMute(1 if muted else 0, None)
            return True
        except Exception:  # noqa: BLE001
            self._drop()
            return False


# --------------------------------------------------------------------------
# Interfejs
# --------------------------------------------------------------------------
PANEL_QSS = f"""
QFrame#CtrlCard {{
    background: {BG_CARD}; border: 1px solid {BORDER_SOFT}; border-radius: 14px;
}}
QLabel#CtrlTitle {{
    color: {MUTED}; font-size: 10px; font-weight: 700; background: transparent;
}}
QLabel#CtrlValue {{
    color: {TEXT}; font-size: 13px; font-weight: 700; background: transparent;
}}
QLabel#CtrlHint {{
    color: {MUTED}; font-size: 10px; background: transparent;
}}
QSlider::groove:horizontal {{
    height: 6px; border-radius: 3px; background: rgba(255, 255, 255, 0.10);
}}
QSlider::sub-page:horizontal {{
    height: 6px; border-radius: 3px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {VIOLET}, stop:1 {SKY});
}}
QSlider::handle:horizontal {{
    width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
    background: #FFFFFF; border: 2px solid {VIOLET};
}}
QSlider::handle:horizontal:hover {{ border: 2px solid {SKY}; }}
QSlider:disabled::sub-page:horizontal {{ background: rgba(255, 255, 255, 0.12); }}
QSlider::handle:horizontal:disabled {{ background: {MUTED}; border: 2px solid {BORDER_SOFT}; }}
QPushButton#Mute {{
    color: {MUTED}; background: transparent; border: 1px solid {BORDER_SOFT};
    border-radius: 10px; padding: 2px 8px; font-size: 12px;
}}
QPushButton#Mute:hover {{ color: {TEXT}; border-color: {SKY}; }}
QPushButton#Mute:checked {{
    color: #FFFFFF; background: rgba(139, 92, 246, 0.30); border-color: {VIOLET};
}}
QPushButton#Toggle {{
    color: {MUTED}; background: {BG_CARD}; border: 1px solid {BORDER_SOFT};
    border-radius: 14px; padding: 10px 12px; font-size: 13px; font-weight: 700;
}}
QPushButton#Toggle:hover {{ color: {TEXT}; border-color: {SKY}; }}
QPushButton#Toggle:checked {{
    color: #FFFFFF; border: 1px solid {VIOLET};
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(139, 92, 246, 0.55), stop:1 rgba(59, 130, 246, 0.55));
}}
QPushButton#Toggle:checked:hover {{ border: 1px solid {SKY}; }}
QPushButton#Toggle:disabled {{
    color: {MUTED}; background: transparent; border: 1px dashed {BORDER_SOFT};
}}
"""


class SliderCard(QFrame):
    """Karta: tytuł, odczyt w %, opcjonalny przycisk i suwak 0-100."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("CtrlCard")

        self.title = QLabel(title.upper())
        self.title.setObjectName("CtrlTitle")
        self.value = QLabel("—")
        self.value.setObjectName("CtrlValue")
        self.hint = QLabel("")
        self.hint.setObjectName("CtrlHint")
        self.hint.setVisible(False)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setCursor(Qt.PointingHandCursor)
        self.slider.setFocusPolicy(Qt.NoFocus)

        self.top = QHBoxLayout()
        self.top.setSpacing(8)
        self.top.addWidget(self.title)
        self.top.addWidget(self.hint, 1)
        self.top.addWidget(self.value)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 10)
        layout.setSpacing(6)
        layout.addLayout(self.top)
        layout.addWidget(self.slider)

    def set_percent(self, percent):
        """Ustawia suwak bez emitowania sygnałów (odświeżanie z systemu)."""
        self.slider.blockSignals(True)
        self.slider.setValue(percent)
        self.slider.blockSignals(False)
        self.value.setText(f"{percent}%")

    def set_unavailable(self, reason):
        self.slider.setEnabled(False)
        self.value.setText("—")
        self.hint.setText(reason)
        self.hint.setVisible(True)
        self.setToolTip(reason)

    def set_available(self):
        self.slider.setEnabled(True)
        self.hint.setVisible(False)
        self.setToolTip("")


class QuickControlsPanel(QWidget):
    """Panel: głośność, jasność, Wi-Fi, Bluetooth. Pokazuje tylko włączone klucze."""

    # sygnały dla wyników z wątku roboczego (dostarczane w wątku GUI)
    _brightness_read = Signal(object)
    _brightness_written = Signal(bool)
    _radio_state = Signal(str, object, str)  # klucz, stan (True/False/None), błąd

    VOLUME_POLL_MS = 1000
    BRIGHTNESS_DEBOUNCE_MS = 150

    def __init__(self, enabled_keys, parent=None):
        super().__init__(parent)
        self.setStyleSheet(PANEL_QSS)
        self._enabled = enabled_keys
        self._volume = None
        self._brightness_ok = None  # None = jeszcze nie sprawdzono
        self._pending_brightness = None
        self._radio_buttons = {}
        self._radio_busy = set()

        self._brightness_read.connect(self._on_brightness_read)
        self._brightness_written.connect(self._on_brightness_written)
        self._radio_state.connect(self._on_radio_state)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        if self._on("volume"):
            self._build_volume(layout)
        if self._on("brightness"):
            self._build_brightness(layout)
        if self._on("wifi") or self._on("bluetooth"):
            self._build_toggles(layout)
        layout.addStretch(1)

        self._volume_timer = QTimer(self)
        self._volume_timer.setInterval(self.VOLUME_POLL_MS)
        self._volume_timer.timeout.connect(self._poll_volume)

        self._brightness_timer = QTimer(self)
        self._brightness_timer.setSingleShot(True)
        self._brightness_timer.setInterval(self.BRIGHTNESS_DEBOUNCE_MS)
        self._brightness_timer.timeout.connect(self._flush_brightness)

    def _on(self, key):
        return bool(self._enabled.get(key, False))

    # ------------------------------------------------------------ budowa
    def _build_volume(self, layout):
        self.volume_backend = VolumeBackend()
        self.volume_card = SliderCard("Głośność")
        self.mute_btn = QPushButton("🔇")
        self.mute_btn.setObjectName("Mute")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setToolTip("Wycisz")
        self.mute_btn.setCursor(Qt.PointingHandCursor)
        self.mute_btn.setFocusPolicy(Qt.NoFocus)
        self.volume_card.top.addWidget(self.mute_btn)
        layout.addWidget(self.volume_card)

        self.volume_card.slider.valueChanged.connect(self._on_volume_changed)
        self.mute_btn.clicked.connect(self._on_mute_clicked)
        self._refresh_volume(force=True)

    def _build_brightness(self, layout):
        self.brightness_card = SliderCard("Jasność")
        self.brightness_card.set_unavailable("Sprawdzam…")
        self.brightness_card.slider.valueChanged.connect(self._on_brightness_changed)
        layout.addWidget(self.brightness_card)

    def _build_toggles(self, layout):
        row = QHBoxLayout()
        row.setSpacing(10)
        specs = (("wifi", "📶  Wi-Fi"), ("bluetooth", "ᛒ  Bluetooth"))
        for key, label in specs:
            if not self._on(key):
                continue
            button = QPushButton(label)
            button.setObjectName("Toggle")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setFocusPolicy(Qt.NoFocus)
            button.setEnabled(RADIO_AVAILABLE)
            if not RADIO_AVAILABLE:
                button.setToolTip("Zainstaluj pakiet winrt (lub winsdk), aby sterować radiami")
            button.clicked.connect(lambda checked, k=key: self._on_radio_clicked(k, checked))
            self._radio_buttons[key] = button
            row.addWidget(button, 1)
        layout.addLayout(row)

    # ------------------------------------------------------------ cykl życia
    def activate(self):
        """Pasek rozwinięty i zakładka widoczna: odśwież stany i włącz odpytywanie."""
        if self._on("volume"):
            self._refresh_volume(force=True)
            self._volume_timer.start()
        if self._on("brightness"):
            _EXECUTOR.submit(self._read_brightness_job)
        for key in self._radio_buttons:
            if RADIO_AVAILABLE:
                _EXECUTOR.submit(self._read_radio_job, key)

    def deactivate(self):
        self._volume_timer.stop()

    # ------------------------------------------------------------ głośność
    def _refresh_volume(self, force=False):
        card = self.volume_card
        if not self.volume_backend.available:
            card.set_unavailable("Brak audio")
            self.mute_btn.setEnabled(False)
            card.setToolTip(self.volume_backend.error)
            return
        if card.slider.isSliderDown() and not force:
            return  # użytkownik właśnie przeciąga suwak
        card.set_available()
        self.mute_btn.setEnabled(True)
        level = self.volume_backend.get()
        if level is not None:
            card.set_percent(level)
        self._sync_mute_button()

    def _poll_volume(self):
        self._refresh_volume()

    def _sync_mute_button(self):
        muted = self.volume_backend.get_mute()
        self.mute_btn.setChecked(muted)
        self.mute_btn.setToolTip("Wyłącz wyciszenie" if muted else "Wycisz")

    def _on_volume_changed(self, value):
        self.volume_card.value.setText(f"{value}%")
        if self.volume_backend.set(value) and self.volume_backend.get_mute() and value > 0:
            self.volume_backend.set_mute(False)  # przesunięcie suwaka zdejmuje wyciszenie
            self._sync_mute_button()

    def _on_mute_clicked(self, checked):
        self.volume_backend.set_mute(checked)
        self._sync_mute_button()

    # ------------------------------------------------------------ jasność
    def _read_brightness_job(self):
        value = brightness_read()
        self._safe_emit(self._brightness_read, value)

    def _on_brightness_read(self, value):
        card = self.brightness_card
        if value is None:
            self._brightness_ok = False
            card.set_unavailable("Niedostępna (monitor bez DDC/CI?)")
            return
        self._brightness_ok = True
        if card.slider.isSliderDown():
            return
        card.set_available()
        card.set_percent(value)

    def _on_brightness_changed(self, value):
        self.brightness_card.value.setText(f"{value}%")
        self._pending_brightness = value
        self._brightness_timer.start()  # debounce: zapis dopiero po chwili spokoju

    def _flush_brightness(self):
        value = self._pending_brightness
        if value is None:
            return
        self._pending_brightness = None
        _EXECUTOR.submit(self._write_brightness_job, value)

    def _write_brightness_job(self, value):
        self._safe_emit(self._brightness_written, brightness_write(value))

    def _on_brightness_written(self, ok):
        if not ok:
            self._brightness_ok = False
            self.brightness_card.set_unavailable("Nie udało się ustawić jasności")
            # spróbuj ponownie odczytać, może monitor wróci po chwili
            QTimer.singleShot(3000, lambda: _EXECUTOR.submit(self._read_brightness_job))

    # ------------------------------------------------------------ Wi-Fi / Bluetooth
    def _read_radio_job(self, key):
        state, error = radio_read(key)
        self._safe_emit(self._radio_state, key, state, error)

    def _on_radio_clicked(self, key, checked):
        if key in self._radio_busy:
            return
        self._radio_busy.add(key)
        button = self._radio_buttons[key]
        button.setEnabled(False)  # blokada na czas zmiany stanu
        _EXECUTOR.submit(self._write_radio_job, key, checked)

    def _write_radio_job(self, key, on):
        state, error = radio_write(key, on)
        if state is None:  # nieudane - odczytaj faktyczny stan
            actual, _ = radio_read(key)
            self._safe_emit(self._radio_state, key, actual, error)
        else:
            self._safe_emit(self._radio_state, key, state, "")

    def _on_radio_state(self, key, state, error):
        self._radio_busy.discard(key)
        button = self._radio_buttons.get(key)
        if button is None:
            return
        if state is None:
            button.setChecked(False)
            button.setEnabled(False)
            button.setToolTip(error or "Niedostępne")
            return
        button.setEnabled(True)
        button.setChecked(bool(state))
        button.setToolTip(error or ("Włączone" if state else "Wyłączone"))

    # ------------------------------------------------------------ pomocnicze
    @staticmethod
    def _safe_emit(signal, *args):
        """Emituje sygnał z wątku roboczego; ignoruje zamknięty już panel."""
        try:
            signal.emit(*args)
        except RuntimeError:
            pass
