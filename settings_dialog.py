"""Okno ustawień DynamicBar (PySide6) w fioletowo-czarnej stylistyce.

Zawiera:
  * SettingsDialog   - główne okno ustawień (pozycja, przesunięcie, pogoda,
                       strefa czasowa, moduły, klucz Gemini, autostart,
                       odinstalowanie),
  * ApiKeyHelpDialog - prosta instrukcja „Jak zdobyć klucz API Gemini?",
  * funkcje pomocnicze do autostartu (winreg) i deinstalacji.

Okno samo wczytuje i zapisuje config.json przez moduł config. Po zapisie
emituje sygnał `saved` - main.py przeładowuje wtedy konfigurację.
"""

import os
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

try:  # winreg istnieje tylko w Windows
    import winreg
except ImportError:  # pragma: no cover
    winreg = None

import config
from widgets import BG_CARD, BG_DEEP, BORDER_SOFT, MAGENTA, MUTED, SKY, TEXT, VIOLET

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
AI_STUDIO_URL = "https://aistudio.google.com/"

# (tekst w oknie, wartość w config.json)
POSITIONS = (
    ("Góra – środek", "top-center"),
    ("Góra – lewo", "top-left"),
    ("Góra – prawo", "top-right"),
    ("Dół – środek", "bottom-center"),
)
# Etykiety dla pozycji, których nie ma na liście wyżej, a mogą być w starym config.json.
EXTRA_POSITION_LABELS = {
    "bottom-left": "Dół – lewo",
    "bottom-right": "Dół – prawo",
}

MODULE_LABELS = {
    "eyes": "Oczka",
    "clock": "Zegar",
    "weather": "Pogoda",
    "gemini": "Gemini (asystent AI)",
    "volume": "Głośność",
    "brightness": "Jasność",
    "wifi": "Wi-Fi",
    "bluetooth": "Bluetooth",
}

FALLBACK_TIMEZONES = (
    "Europe/Warsaw", "Europe/London", "Europe/Berlin", "Europe/Paris",
    "Europe/Kiev", "America/New_York", "America/Chicago", "America/Los_Angeles",
    "Asia/Tokyo", "Asia/Shanghai", "Australia/Sydney", "UTC",
)

SLIDER_RANGE = 1000  # zakres suwaka w pikselach (pole liczbowe pozwala na więcej)

DIALOG_QSS = f"""
QDialog {{ background: {BG_DEEP}; }}
QLabel {{ color: {TEXT}; background: transparent; font-size: 12px; }}
QLabel#Title {{ font-size: 20px; font-weight: 700; }}
QLabel#Muted {{ color: {MUTED}; font-size: 11px; }}
QLabel#CardTitle {{ color: {SKY}; font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
QLabel#StepTitle {{ font-size: 13px; font-weight: 700; }}
QLabel#Badge {{
    color: #FFFFFF; font-size: 13px; font-weight: 700; border-radius: 14px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {VIOLET}, stop:1 {MAGENTA});
}}
QFrame#Card {{ background: {BG_CARD}; border: 1px solid {BORDER_SOFT}; border-radius: 14px; }}
QFrame#Note {{
    background: rgba(192, 38, 211, 0.12); border: 1px solid {MAGENTA}; border-radius: 12px;
}}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER_SOFT}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {VIOLET}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QLineEdit, QComboBox, QSpinBox {{
    background: {BG_DEEP}; color: {TEXT}; border: 1px solid {BORDER_SOFT};
    border-radius: 8px; padding: 6px 10px; font-size: 12px;
    selection-background-color: {VIOLET};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {VIOLET}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{
    width: 0; height: 0; border-left: 5px solid transparent;
    border-right: 5px solid transparent; border-top: 6px solid {MUTED};
}}
QComboBox QAbstractItemView {{
    background: {BG_CARD}; color: {TEXT}; border: 1px solid {BORDER_SOFT};
    selection-background-color: {VIOLET}; outline: none;
}}
QCheckBox {{ color: {TEXT}; spacing: 8px; font-size: 12px; background: transparent; }}
QCheckBox:disabled {{ color: {MUTED}; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 5px;
    border: 1px solid {MUTED}; background: {BG_DEEP};
}}
QCheckBox::indicator:hover {{ border-color: {SKY}; }}
QCheckBox::indicator:checked {{
    border: 1px solid {MAGENTA};
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {VIOLET}, stop:1 {MAGENTA});
}}
QCheckBox::indicator:disabled {{ border-color: {BORDER_SOFT}; }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER_SOFT}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {VIOLET}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: #FFFFFF; width: 12px; height: 12px; margin: -6px 0;
    border-radius: 8px; border: 2px solid {VIOLET};
}}
QSlider::handle:horizontal:hover {{ border-color: {SKY}; }}
QPushButton {{
    color: {TEXT}; background: rgba(139, 92, 246, 0.14); border: 1px solid {BORDER_SOFT};
    border-radius: 10px; padding: 7px 14px; font-size: 12px; font-weight: 600;
}}
QPushButton:hover {{ border-color: {VIOLET}; background: rgba(139, 92, 246, 0.26); }}
QPushButton:pressed {{ background: rgba(139, 92, 246, 0.4); }}
QPushButton#Primary {{
    color: #FFFFFF; border: none; padding: 8px 22px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {VIOLET}, stop:1 {MAGENTA});
}}
QPushButton#Primary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #A78BFA, stop:1 #D946EF);
}}
QPushButton#Danger {{ border: 1px solid {MAGENTA}; background: transparent; }}
QPushButton#Danger:hover {{ background: rgba(192, 38, 211, 0.28); }}
QMessageBox {{ background: {BG_DEEP}; }}
"""


# --------------------------------------------------------------------------
# Autostart (rejestr Windows)
# --------------------------------------------------------------------------
def autostart_command():
    """Polecenie wpisywane do rejestru: zbudowany .exe albo pythonw + main.py."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")  # bez okna konsoli
    runner = pythonw if pythonw.exists() else exe
    script = Path(__file__).resolve().with_name("main.py")
    return f'"{runner}" "{script}"'


def is_autostart_enabled():
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, config.APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled):
    """Dodaje lub usuwa wpis autostartu. Zwraca True, gdy się udało."""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, config.APP_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, config.APP_NAME)
                except FileNotFoundError:
                    pass  # i tak go nie było
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# Deinstalacja
# --------------------------------------------------------------------------
def find_uninstall_command():
    """Szuka polecenia deinstalatora zarejestrowanego w Windows
    (Ustawienia > Aplikacje). Zwraca tekst polecenia albo None."""
    if winreg is None:
        return None

    sources = (
        (winreg.HKEY_CURRENT_USER, 0),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
    )
    wanted = config.APP_NAME.lower()
    for hive, view in sources:
        try:
            root = winreg.OpenKey(hive, UNINSTALL_KEY, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, name) as sub:
                        display = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                        if not display.strip().lower().startswith(wanted):
                            continue
                        command = winreg.QueryValueEx(sub, "UninstallString")[0]
                except OSError:
                    continue
                if command:
                    return str(command)

    # program spakowany do .exe z deinstalatorem obok (np. Inno Setup)
    if getattr(sys, "frozen", False):
        for name in ("unins000.exe", "uninstall.exe"):
            candidate = Path(sys.executable).parent / name
            if candidate.exists():
                return f'"{candidate}"'
    return None


def launch_detached(command):
    """Uruchamia polecenie niezależnie od naszego procesu. True, gdy się udało."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    try:
        subprocess.Popen(command, shell=True, close_fds=True, creationflags=flags)
        return True
    except OSError:
        return False


def clear_config_files():
    """Usuwa config.json, jego kopię .bak i pliki tymczasowe (oraz pusty folder)."""
    directory = config.get_config_dir()
    targets = [
        directory / config.CONFIG_FILENAME,
        directory / (config.CONFIG_FILENAME + ".bak"),
    ]
    try:
        targets.extend(directory.glob("config_*.tmp"))
    except OSError:
        pass
    for target in targets:
        try:
            target.unlink()
        except OSError:
            pass
    try:
        directory.rmdir()  # tylko jeśli pusty
    except OSError:
        pass


# --------------------------------------------------------------------------
# Elementy interfejsu
# --------------------------------------------------------------------------
class OffsetControl(QWidget):
    """Wiersz: etykieta + suwak + pole liczbowe (zsynchronizowane)."""

    changed = Signal(int)

    def __init__(self, label, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        caption = QLabel(label)
        caption.setFixedWidth(78)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(-SLIDER_RANGE, SLIDER_RANGE)
        self.spin = QSpinBox()
        self.spin.setRange(-config.OFFSET_LIMIT, config.OFFSET_LIMIT)
        self.spin.setSuffix(" px")
        self.spin.setButtonSymbols(QSpinBox.NoButtons)
        self.spin.setFixedWidth(80)

        layout.addWidget(caption)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.spin)

        self.slider.valueChanged.connect(self.spin.setValue)
        self.spin.valueChanged.connect(self._on_spin)

    def _on_spin(self, value):
        self.slider.blockSignals(True)
        self.slider.setValue(max(-SLIDER_RANGE, min(SLIDER_RANGE, value)))
        self.slider.blockSignals(False)
        self.changed.emit(value)

    def value(self):
        return self.spin.value()

    def set_value(self, value):
        self.spin.setValue(int(value))


class ApiKeyHelpDialog(QDialog):
    """Prosta instrukcja zdobycia klucza API Gemini, krok po kroku."""

    STEPS = (
        (
            "Wejdź na Google AI Studio",
            "Otwórz w przeglądarce stronę aistudio.google.com i zaloguj się "
            "swoim kontem Google (takim jak do Gmaila).",
        ),
        (
            "Kliknij „Get API key”",
            "Przycisk znajdziesz w menu po lewej stronie. Jeśli widzisz inny "
            "układ strony, poszukaj napisu z „API key”.",
        ),
        (
            "Kliknij „Create API key”",
            "Jeśli strona zapyta o projekt, wybierz dowolny z listy albo "
            "pozwól jej utworzyć nowy. Klucz pojawi się po kilku sekundach.",
        ),
        (
            "Skopiuj klucz",
            "To długi ciąg liter i cyfr, zwykle zaczynający się od „AIza”. "
            "Kliknij ikonę kopiowania obok niego.",
        ),
        (
            "Wklej klucz tutaj",
            "Wróć do ustawień DynamicBar, kliknij pole „Klucz API Gemini”, "
            "wklej klucz (Ctrl+V) i kliknij „Zapisz”.",
        ),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Jak zdobyć klucz API Gemini?")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setStyleSheet(DIALOG_QSS)
        self.setFixedWidth(500)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        title = QLabel("Jak zdobyć klucz API Gemini?")
        title.setObjectName("Title")
        intro = QLabel(
            "Klucz jest darmowym „hasłem” dla asystenta AI. To zajmuje około "
            "minutę i wystarczy zrobić to raz."
        )
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(intro)

        for number, (heading, description) in enumerate(self.STEPS, start=1):
            root.addWidget(self._step_row(number, heading, description))

        note = QFrame()
        note.setObjectName("Note")
        note_layout = QVBoxLayout(note)
        note_layout.setContentsMargins(14, 10, 14, 10)
        note_text = QLabel(
            "Traktuj klucz jak hasło: nie wysyłaj go nikomu i nie publikuj "
            "w internecie. Jest zapisany tylko na tym komputerze, w pliku config.json."
        )
        note_text.setWordWrap(True)
        note_layout.addWidget(note_text)
        root.addWidget(note)

        buttons = QHBoxLayout()
        open_btn = QPushButton("Otwórz aistudio.google.com")
        open_btn.setObjectName("Primary")
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(AI_STUDIO_URL)))
        close_btn = QPushButton("Zamknij")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        buttons.addStretch(1)
        buttons.addWidget(close_btn)
        buttons.addWidget(open_btn)
        root.addLayout(buttons)

    @staticmethod
    def _step_row(number, heading, description):
        card = QFrame()
        card.setObjectName("Card")
        row = QHBoxLayout(card)
        row.setContentsMargins(12, 10, 14, 10)
        row.setSpacing(12)

        badge = QLabel(str(number))
        badge.setObjectName("Badge")
        badge.setAlignment(Qt.AlignCenter)
        badge.setFixedSize(28, 28)

        column = QVBoxLayout()
        column.setSpacing(2)
        head = QLabel(heading)
        head.setObjectName("StepTitle")
        body = QLabel(description)
        body.setObjectName("Muted")
        body.setWordWrap(True)
        column.addWidget(head)
        column.addWidget(body)

        row.addWidget(badge, 0, Qt.AlignTop)
        row.addLayout(column, 1)
        return card


# --------------------------------------------------------------------------
# Główne okno ustawień
# --------------------------------------------------------------------------
class SettingsDialog(QDialog):
    saved = Signal()                      # zapisano config.json
    preview_changed = Signal(str, int, int)  # podgląd pozycji: pozycja, X, Y
    uninstall_started = Signal()          # tuż przed czyszczeniem i deinstalacją

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{config.APP_NAME} – Ustawienia")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setStyleSheet(DIALOG_QSS)
        self.setMinimumSize(500, 520)
        self.resize(540, 740)

        self._loading = True
        self.cfg = config.load_config(force_reload=True)
        self._build_ui()
        self._load_values()
        self._loading = False

    # ------------------------------------------------------------ budowa UI
    @staticmethod
    def _card(title):
        frame = QFrame()
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(10)
        heading = QLabel(title.upper())
        heading.setObjectName("CardTitle")
        layout.addWidget(heading)
        return frame, layout

    @staticmethod
    def _hint(text):
        label = QLabel(text)
        label.setObjectName("Muted")
        label.setWordWrap(True)
        return label

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        title = QLabel("Ustawienia")
        title.setObjectName("Title")
        subtitle = self._hint("Zmiany zapiszą się po kliknięciu „Zapisz”.")
        root.addWidget(title)
        root.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 8, 0)
        content_layout.setSpacing(12)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        content_layout.addWidget(self._build_position_card())
        content_layout.addWidget(self._build_weather_card())
        content_layout.addWidget(self._build_modules_card())
        content_layout.addWidget(self._build_gemini_card())
        content_layout.addWidget(self._build_system_card())
        content_layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Anuluj")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        save = QPushButton("Zapisz")
        save.setObjectName("Primary")
        save.setCursor(Qt.PointingHandCursor)
        save.setDefault(True)
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        root.addLayout(buttons)

    def _build_position_card(self):
        card, layout = self._card("Położenie paska")

        layout.addWidget(QLabel("Pozycja na ekranie"))
        self.position_combo = QComboBox()
        for label, value in POSITIONS:
            self.position_combo.addItem(label, value)
        layout.addWidget(self.position_combo)

        self.off_x = OffsetControl("Przesunięcie X")
        self.off_y = OffsetControl("Przesunięcie Y")
        layout.addWidget(self.off_x)
        layout.addWidget(self.off_y)

        row = QHBoxLayout()
        row.addWidget(self._hint("X: dodatni = w prawo, Y: dodatni = w dół. Zmiany widać od razu."), 1)
        reset = QPushButton("Wyzeruj")
        reset.setCursor(Qt.PointingHandCursor)
        reset.clicked.connect(self._reset_offsets)
        row.addWidget(reset)
        layout.addLayout(row)

        self.position_combo.currentIndexChanged.connect(self._emit_preview)
        self.off_x.changed.connect(self._emit_preview)
        self.off_y.changed.connect(self._emit_preview)
        return card

    def _build_weather_card(self):
        card, layout = self._card("Pogoda i zegar")

        layout.addWidget(QLabel("Miasto (pogoda)"))
        self.city_edit = QLineEdit()
        self.city_edit.setPlaceholderText("np. Kielce")
        layout.addWidget(self.city_edit)

        layout.addWidget(QLabel("Lokalizacja zegara (strefa czasowa)"))
        self.tz_combo = QComboBox()
        self.tz_combo.setEditable(True)
        self.tz_combo.setInsertPolicy(QComboBox.NoInsert)
        try:
            zones = sorted(available_timezones())
        except Exception:
            zones = []
        self.tz_combo.addItems(zones or list(FALLBACK_TIMEZONES))
        layout.addWidget(self.tz_combo)
        layout.addWidget(self._hint("Wybierz z listy lub wpisz, np. Europe/Warsaw."))
        return card

    def _build_modules_card(self):
        card, layout = self._card("Oczka i moduły")

        self.chk_animated = QCheckBox("Animowane oczka")
        layout.addWidget(self.chk_animated)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(8)
        self.module_checks = {}
        for index, key in enumerate(config.DEFAULT_CONFIG["modules"]):
            check = QCheckBox(MODULE_LABELS.get(key, key.capitalize()))
            self.module_checks[key] = check
            grid.addWidget(check, index // 2, index % 2)
        layout.addLayout(grid)
        layout.addWidget(self._hint("Wyłączony moduł znika z paska i z zakładek."))

        self.module_checks["eyes"].toggled.connect(self.chk_animated.setEnabled)
        return card

    def _build_gemini_card(self):
        card, layout = self._card("Google Gemini")

        layout.addWidget(QLabel("Klucz API Gemini"))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("Wklej tutaj swój klucz (AIza…)")
        self.key_toggle = QPushButton("Pokaż")
        self.key_toggle.setCheckable(True)
        self.key_toggle.setCursor(Qt.PointingHandCursor)
        self.key_toggle.setFixedWidth(76)
        self.key_toggle.toggled.connect(self._toggle_key_visibility)
        row.addWidget(self.key_edit, 1)
        row.addWidget(self.key_toggle)
        layout.addLayout(row)

        help_btn = QPushButton("Jak zdobyć klucz API?")
        help_btn.setCursor(Qt.PointingHandCursor)
        help_btn.clicked.connect(self._show_api_help)
        layout.addWidget(help_btn, 0, Qt.AlignLeft)
        return card

    def _build_system_card(self):
        card, layout = self._card("System")

        self.chk_autostart = QCheckBox("Uruchamiaj razem z systemem Windows")
        if winreg is None:
            self.chk_autostart.setEnabled(False)
            self.chk_autostart.setToolTip("Autostart jest dostępny tylko w Windows.")
        layout.addWidget(self.chk_autostart)

        layout.addSpacing(6)
        layout.addWidget(
            self._hint(
                "Odinstalowanie usuwa wpis autostartu, opcjonalnie Twoje "
                "ustawienia, i uruchamia deinstalator programu."
            )
        )
        uninstall = QPushButton("Odinstaluj program")
        uninstall.setObjectName("Danger")
        uninstall.setCursor(Qt.PointingHandCursor)
        uninstall.clicked.connect(self._uninstall)
        layout.addWidget(uninstall, 0, Qt.AlignLeft)
        return card

    # ------------------------------------------------------------ dane
    def _load_values(self):
        cfg = self.cfg

        position = cfg["bar_position"]
        index = self.position_combo.findData(position)
        if index < 0:  # np. stare "bottom-left" z config.json
            self.position_combo.addItem(EXTRA_POSITION_LABELS.get(position, position), position)
            index = self.position_combo.count() - 1
        self.position_combo.setCurrentIndex(index)
        self.off_x.set_value(cfg["offset_x"])
        self.off_y.set_value(cfg["offset_y"])

        self.city_edit.setText(cfg["weather_city"])
        self.tz_combo.setCurrentText(cfg["timezone"])

        self.chk_animated.setChecked(cfg["animated_eyes"])
        for key, check in self.module_checks.items():
            check.setChecked(cfg["modules"].get(key, False))
        self.chk_animated.setEnabled(self.module_checks["eyes"].isChecked())

        self.key_edit.setText(cfg["gemini_api_key"])
        # stan autostartu bierzemy z rejestru - to on jest prawdą
        self.chk_autostart.setChecked(is_autostart_enabled() if winreg else cfg["autostart"])

    # ------------------------------------------------------------ akcje
    def _emit_preview(self, *_):
        if self._loading:
            return
        self.preview_changed.emit(
            self.position_combo.currentData(), self.off_x.value(), self.off_y.value()
        )

    def _reset_offsets(self):
        self.off_x.set_value(0)
        self.off_y.set_value(0)

    def _toggle_key_visibility(self, visible):
        self.key_edit.setEchoMode(QLineEdit.Normal if visible else QLineEdit.Password)
        self.key_toggle.setText("Ukryj" if visible else "Pokaż")

    def _show_api_help(self):
        ApiKeyHelpDialog(self).exec()

    def _save(self):
        timezone = self.tz_combo.currentText().strip()
        if timezone:
            try:
                ZoneInfo(timezone)
            except Exception:
                QMessageBox.warning(
                    self,
                    "Strefa czasowa",
                    f"Nie można wczytać strefy czasowej „{timezone}”.\n\n"
                    "Wpisz nazwę w formacie Kontynent/Miasto (np. Europe/Warsaw). "
                    "Jeśli nazwa jest poprawna, w Windows może brakować pakietu "
                    "tzdata (pip install tzdata).",
                )
                return

        cfg = config.load_config(force_reload=True)
        cfg["bar_position"] = self.position_combo.currentData()
        cfg["offset_x"] = self.off_x.value()
        cfg["offset_y"] = self.off_y.value()
        cfg["weather_city"] = self.city_edit.text().strip()
        cfg["timezone"] = timezone
        cfg["animated_eyes"] = self.chk_animated.isChecked()
        cfg["gemini_api_key"] = self.key_edit.text().strip()
        for key, check in self.module_checks.items():
            cfg["modules"][key] = check.isChecked()

        if winreg is not None:
            want = self.chk_autostart.isChecked()
            if want != is_autostart_enabled() and not set_autostart(want):
                QMessageBox.warning(
                    self,
                    "Autostart",
                    "Nie udało się zmienić wpisu autostartu w rejestrze Windows. "
                    "Pozostałe ustawienia zostaną zapisane.",
                )
            cfg["autostart"] = is_autostart_enabled()
        else:
            cfg["autostart"] = False

        if not config.save_config(cfg):
            QMessageBox.critical(
                self,
                "Zapis ustawień",
                f"Nie udało się zapisać pliku:\n{config.get_config_path()}",
            )
            return

        self.saved.emit()
        self.accept()

    def _uninstall(self):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Odinstaluj program")
        box.setText(f"Czy na pewno chcesz odinstalować {config.APP_NAME}?")
        box.setInformativeText(
            "Zostanie usunięty wpis autostartu, a następnie uruchomiony "
            "deinstalator. Program zamknie się."
        )
        clear_check = QCheckBox("Usuń także moje ustawienia (config.json, w tym klucz API)")
        box.setCheckBox(clear_check)
        confirm = box.addButton("Odinstaluj", QMessageBox.DestructiveRole)
        cancel = box.addButton("Anuluj", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not confirm:
            return

        # main.py wyłącza obserwowanie pliku, żeby nie odtworzył config.json
        self.uninstall_started.emit()

        set_autostart(False)
        if clear_check.isChecked():
            clear_config_files()

        command = find_uninstall_command()
        launched = bool(command) and launch_detached(command)

        if not launched:
            # bezpieczny fallback: pokazujemy listę aplikacji i zamykamy program
            if sys.platform == "win32":
                QDesktopServices.openUrl(QUrl("ms-settings:appsfeatures"))
            QMessageBox.information(
                self,
                "Odinstaluj program",
                "Nie znaleziono deinstalatora tej aplikacji.\n\n"
                "Wpis autostartu został usunięty, a program zostanie teraz zamknięty. "
                "Aby usunąć go całkowicie, skasuj folder programu albo znajdź "
                f"„{config.APP_NAME}” w Ustawienia > Aplikacje.",
            )

        QApplication.quit()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    dialog = SettingsDialog()
    dialog.show()
    sys.exit(app.exec())
