"""DynamicBar - pływająca, rozwijana pigułka na pulpicie (PySide6).

Architektura:
  * DynamicBar  - bezramkowe okno; animuje rozmiar pigułki (właściwość `progress`
                  0.0 = zwinięta, 1.0 = rozwinięta), pozycjonuje ją wg config.json
                  i obsługuje hover / kliknięcie / przypinanie.
  * CompactView - zawartość zwiniętej pigułki (oczka + pogoda + zegar).
  * Module      - klasa bazowa zakładki w rozwiniętym pasku. Każdy moduł dostaje
                  siatkę kafelków (TileGrid) i dokłada do niej Tile lub kartę.
  * @register_module - rejestruje moduł; zakładka pojawia się automatycznie,
                  gdy odpowiadający jej klucz w config["modules"] jest włączony.
  * widgets.py  - oczka, zegar i pogoda (Open-Meteo) jako osobne widżety.
  * settings_dialog.py - okno ustawień (ikona zębatki w nagłówku paska,
                  menu zasobnika i menu kontekstowe).

  * sys_controls.py - panel szybkiej kontroli (głośność, jasność, Wi-Fi, Bluetooth)
                  używany przez zakładkę „Sterowanie".
  * system_monitor.py - wątek QThread wykrywający USB, Wi-Fi/Bluetooth/tryb
                  samolotowy, CAPS/NUM LOCK i powiadomienia Windows; DynamicBar
                  pokazuje je jako 3-sekundowy alert (AlertView, kolejka alertów).
                  Włączane/wyłączane kluczami alerts_enabled i alert_* w config.json.
  * media_player.py - odtwarzacz mediów (Windows SMTC przez winrt/winsdk):
                  MediaController działa w osobnym wątku asyncio, MediaPanel
                  (okładka, tytuł, wykonawca, prev/play/next, suwak) jest
                  zakładką „Muzyka". Klucz config["modules"]["media"]
                  (gdy go brak, moduł jest włączony).
  * ai_assistant.py - czat z Google Gemini (zakładka „Gemini"): zapytania HTTP w
                  osobnym wątku (QThread), klucz API z config["gemini_api_key"];
                  bez klucza pokazuje kartę z przyciskiem otwierającym Ustawienia.
                  Pasek pozostaje rozwinięty, gdy pole pytania ma fokus albo trwa
                  zapytanie (Module.keeps_open).
  * clipboard_manager.py - historia schowka (15 ostatnich wpisów: tekst oraz
                  pliki/foldery z Eksploratora z nazwami i ikonami). ClipboardHistory
                  należy do paska i działa, gdy zakładka „Schowek" jest włączona
                  (config["modules"]["clipboard"]; gdy klucza brak, jest włączona);
                  ClipboardPanel to lista z przyciskiem „Wyczyść historię".

Aby dodać nowy moduł wystarczy napisać klasę dziedziczącą po Module
(zob. ClockModule poniżej), ozdobić ją @register_module i dopisać klucz do
DEFAULT_CONFIG["modules"] w config.py.
"""

import math
import sys
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QFileSystemWatcher,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QDesktopServices,
    QGuiApplication,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QFontMetrics,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

import config
from ai_assistant import AIAssistantPanel, shutdown_workers
from clipboard_manager import ClipboardHistory, ClipboardPanel
from settings_dialog import SettingsDialog
from sys_controls import QuickControlsPanel
from media_player import MediaController, MediaPanel
from system_monitor import SystemMonitor
from widgets import (
    BG_CARD,
    BG_DEEP,
    BORDER_SOFT,
    MAGENTA,
    MUTED,
    SKY,
    TEXT,
    VIOLET,
    ClockCard,
    EyesWidget,
    WeatherBadge,
    WeatherCard,
    WeatherService,
    format_time,
)

# --------------------------------------------------------------------------
# Wymiary
# --------------------------------------------------------------------------
COLLAPSED_SIZE = (230, 38)
EXPANDED_SIZE = (730, 290)  # szeroki nagłówek: 6 zakładek + „Przypnij” + zębatka muszą się zmieścić
RADIUS_COLLAPSED = 19.0
RADIUS_EXPANDED = 28.0
SCREEN_MARGIN = 12  # odstęp pigułki od krawędzi ekranu

# Alert systemowy: kompaktowa pigułka z fioletowo-niebieską poświatą.
ALERT_SIZE = (400, 70)       # wraz z marginesem na poświatę
ALERT_INSET = 7              # margines poświaty wewnątrz okna
ALERT_DURATION_MS = 3000     # czas wyświetlania jednego alertu
ALERT_VIOLET = "#8B5CF6"
ALERT_BLUE = "#3B82F6"
ALERT_ICONS = {
    "usb": "🔌",
    "wifi": "📶",
    "bluetooth": "🔷",
    "airplane": "✈",
    "key": "⌨",
    "notification": "🔔",
}

APP_QSS = f"""
QLabel {{ color: {TEXT}; background: transparent; }}
QPushButton#Tab {{
    color: {MUTED}; background: transparent; border: 1px solid transparent;
    border-radius: 12px; padding: 5px 10px; font-size: 12px; font-weight: 600;
}}
QPushButton#Tab:hover {{ color: {TEXT}; background: {BG_CARD}; }}
QPushButton#Tab:checked {{
    color: #FFFFFF; background: rgba(139, 92, 246, 0.22);
    border: 1px solid rgba(139, 92, 246, 0.7);
}}
QPushButton#Pin {{
    color: {MUTED}; background: transparent; border: 1px solid {BORDER_SOFT};
    border-radius: 12px; padding: 5px 12px; font-size: 11px;
}}
QPushButton#Pin:hover {{ color: {TEXT}; border-color: {SKY}; }}
QPushButton#Pin:checked {{
    color: #FFFFFF; background: rgba(192, 38, 211, 0.25); border-color: {MAGENTA};
}}
QPushButton#Gear {{
    color: {MUTED}; background: transparent; border: 1px solid {BORDER_SOFT};
    border-radius: 12px; padding: 4px 9px; font-size: 14px;
}}
QPushButton#Gear:hover {{ color: #FFFFFF; border-color: {VIOLET}; background: rgba(139, 92, 246, 0.22); }}
QMenu {{
    background: {BG_CARD}; color: {TEXT}; border: 1px solid {BORDER_SOFT};
    padding: 4px;
}}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {VIOLET}; }}
"""


def lerp(a, b, t):
    return a + (b - a) * t


def clamp(value, low, high):
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# Kafelki
# --------------------------------------------------------------------------
class Tile(QFrame):
    """Pojedynczy kafelek: tytuł, wartość i opcjonalna podpowiedź.
    Emituje `clicked`, więc moduły mogą go używać jak przycisk."""

    clicked = Signal()

    def __init__(self, title, value="—", accent=VIOLET, hint="", parent=None):
        super().__init__(parent)
        self.setObjectName("Tile")
        self.setMinimumHeight(80)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            f"QFrame#Tile {{ background: {BG_CARD}; border: 1px solid {BORDER_SOFT};"
            f" border-radius: 14px; }}"
            f"QFrame#Tile:hover {{ border: 1px solid {accent}; }}"
        )

        self._title = QLabel(title.upper())
        self._title.setStyleSheet(
            f"color: {MUTED}; font-size: 10px; font-weight: 700; background: transparent;"
        )
        self._value = QLabel(value)
        self._value.setStyleSheet(
            f"color: {accent}; font-size: 22px; font-weight: 700; background: transparent;"
        )
        self._hint = QLabel(hint)
        self._hint.setStyleSheet(f"color: {MUTED}; font-size: 11px; background: transparent;")
        self._hint.setVisible(bool(hint))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        layout.addWidget(self._title)
        layout.addWidget(self._value)
        layout.addWidget(self._hint)
        layout.addStretch(1)

    def set_value(self, text):
        self._value.setText(text)

    def set_hint(self, text):
        self._hint.setText(text)
        self._hint.setVisible(bool(text))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
        else:
            super().mousePressEvent(event)


class TileGrid(QWidget):
    """Siatka kafelków z automatycznym zawijaniem do kolejnych wierszy.
    Przyjmuje dowolny widżet (kafelek lub kartę z widgets.py)."""

    def __init__(self, columns=3, parent=None):
        super().__init__(parent)
        self._columns = columns
        self._row = 0
        self._col = 0
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(10)
        for col in range(columns):
            self._layout.setColumnStretch(col, 1)

    def add_tile(self, tile, colspan=1):
        colspan = clamp(colspan, 1, self._columns)
        if self._col + colspan > self._columns:
            self._row += 1
            self._col = 0
        self._layout.addWidget(tile, self._row, self._col, 1, colspan)
        self._col += colspan
        if self._col >= self._columns:
            self._row += 1
            self._col = 0
        return tile


# --------------------------------------------------------------------------
# Moduły (zakładki)
# --------------------------------------------------------------------------
MODULE_REGISTRY = []


def register_module(cls):
    """Dekorator: dodaje klasę modułu do rejestru (kolejność = kolejność zakładek)."""
    MODULE_REGISTRY.append(cls)
    return cls


class Module(QWidget):
    """Klasa bazowa zakładki.

    Atrybuty klasy:
      id     - unikalna nazwa (domyślny klucz w config["modules"]),
      title  - tekst zakładki,
      icon   - krótki symbol przed tytułem,
      accent - kolor akcentu,
      keys   - klucze z config["modules"]; zakładka jest widoczna, gdy włączony
               jest choć jeden z nich (domyślnie tylko `id`),
      columns - liczba kolumn siatki kafelków.
    """

    id = ""
    title = ""
    icon = "•"
    accent = VIOLET
    keys = ()
    columns = 3

    @classmethod
    def enabled(cls, cfg):
        keys = cls.keys or (cls.id,)
        return any(cfg["modules"].get(key, False) for key in keys)

    def __init__(self, bar, cfg):
        super().__init__()
        self.bar = bar
        self.cfg = cfg
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.grid = TileGrid(self.columns)
        layout.addWidget(self.grid)
        layout.addStretch(1)
        self.build(self.grid)

    def key_enabled(self, key):
        return self.cfg["modules"].get(key, False)

    def keeps_open(self):
        """Czy pasek ma zostać rozwinięty mimo opuszczenia go kursorem
        (np. trwa pisanie pytania albo oczekiwanie na odpowiedź)."""
        return False

    # --- do nadpisania w modułach ---
    def build(self, grid):
        """Tutaj twórz kafelki i dodawaj je przez grid.add_tile(...)."""

    def on_activated(self):
        """Zakładka stała się widoczna (pasek rozwinięty + wybrana zakładka)."""

    def on_deactivated(self):
        """Zakładka przestała być widoczna."""

    def apply_config(self, cfg):
        """Wywoływane po zmianie config.json (bez przebudowy listy modułów)."""
        self.cfg = cfg


@register_module
class ClockModule(Module):
    id = "clock"
    title = "Zegar"
    icon = "◷"
    accent = SKY

    def build(self, grid):
        self.card = ClockCard()
        self.card.configure(self.cfg["timezone"], self.cfg["clock_24h"])
        grid.add_tile(self.card, colspan=3)

    def apply_config(self, cfg):
        super().apply_config(cfg)
        self.card.configure(cfg["timezone"], cfg["clock_24h"])


@register_module
class WeatherModule(Module):
    id = "weather"
    title = "Pogoda"
    icon = "☁"
    accent = VIOLET

    def build(self, grid):
        # dane pobiera wspólny serwis paska (self.bar.weather)
        self.card = WeatherCard(self.bar.weather)
        self.card.set_city_hint(self.cfg["weather_city"])
        grid.add_tile(self.card, colspan=3)

    def apply_config(self, cfg):
        super().apply_config(cfg)
        self.card.set_city_hint(cfg["weather_city"])


@register_module
class MediaModule(Module):
    """Odtwarzacz mediów (SMTC): okładka, tytuł, wykonawca, przyciski i suwak."""

    id = "media"
    title = "Muzyka"
    icon = "♫"
    accent = MAGENTA
    columns = 1

    @classmethod
    def enabled(cls, cfg):
        # brak klucza "media" w config.json oznacza: włączony
        return bool(cfg["modules"].get("media", True))

    def build(self, grid):
        # kontroler (wątek asyncio) należy do paska i przeżywa przebudowy zakładek
        self.panel = MediaPanel(self.bar.media)
        grid.add_tile(self.panel, colspan=self.columns)

    def on_activated(self):
        self.panel.activate()

    def on_deactivated(self):
        self.panel.deactivate()


@register_module
class ClipboardModule(Module):
    """Historia schowka: 15 ostatnich wpisów (tekst i pliki), klik przywraca wpis."""

    id = "clipboard"
    title = "Schowek"
    icon = "📋"
    accent = SKY
    columns = 1

    @classmethod
    def enabled(cls, cfg):
        # brak klucza "clipboard" w config.json oznacza: włączony
        return bool(cfg["modules"].get("clipboard", True))

    def build(self, grid):
        # historia (monitor QClipboard) należy do paska i przeżywa przebudowy zakładek
        self.panel = ClipboardPanel(self.bar.clipboard_history)
        grid.add_tile(self.panel, colspan=self.columns)


@register_module
class GeminiModule(Module):
    """Czat z Google Gemini (ai_assistant.py)."""

    id = "gemini"
    title = "Gemini"
    icon = "✦"
    accent = MAGENTA
    columns = 1

    def build(self, grid):
        self.panel = AIAssistantPanel(
            self.cfg["gemini_api_key"], open_settings=self.bar.open_settings
        )
        # po utracie fokusu / zakończeniu zapytania pasek może się zwinąć
        self.panel.focus_released.connect(self.bar.schedule_leave_check)
        grid.add_tile(self.panel, colspan=self.columns)

    def keeps_open(self):
        return self.panel.is_loading() or self.panel.input_has_focus()

    def apply_config(self, cfg):
        super().apply_config(cfg)
        self.panel.set_api_key(cfg["gemini_api_key"])


@register_module
class QuickControlsModule(Module):
    """Szybkie sterowanie - jedna zakładka, kafelki włączane osobnymi kluczami."""

    id = "quick"
    title = "Sterowanie"
    icon = "≡"  # zębatka jest zarezerwowana dla ustawień
    accent = SKY
    keys = ("volume", "brightness", "wifi", "bluetooth")
    columns = 1

    def build(self, grid):
        # panel (sys_controls.py) sam pokazuje tylko włączone klucze z config["modules"];
        # zmiana kluczy przebudowuje moduły, więc wystarczy podać je przy tworzeniu
        self.panel = QuickControlsPanel(self.cfg["modules"])
        grid.add_tile(self.panel, colspan=self.columns)

    def on_activated(self):
        self.panel.activate()

    def on_deactivated(self):
        self.panel.deactivate()


# --------------------------------------------------------------------------
# Zwinięta pigułka: oczka + pogoda + zegar
# --------------------------------------------------------------------------
class CompactView(QWidget):
    """Zawartość zwiniętej pigułki."""

    def __init__(self, weather, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 16, 0)
        layout.setSpacing(8)
        self.eyes = EyesWidget()
        self.weather = WeatherBadge(weather)
        self.label = QLabel("--:--")
        self.label.setStyleSheet(
            f"color: {TEXT}; font-size: 13px; font-weight: 700; background: transparent;"
        )
        layout.addWidget(self.eyes)
        layout.addStretch(1)
        layout.addWidget(self.weather)
        layout.addWidget(self.label)

    def apply_config(self, cfg):
        modules = cfg["modules"]
        show_eyes = modules.get("eyes", True)
        show_clock = modules.get("clock", True)
        self.eyes.setVisible(show_eyes)
        self.eyes.set_animated(cfg["animated_eyes"])
        self.weather.set_enabled(modules.get("weather", True))
        self.label.setVisible(show_clock or not show_eyes)
        if not show_clock:
            self.label.setText("DynamicBar")

    def set_time(self, text, show_clock):
        if show_clock:
            self.label.setText(text)


# --------------------------------------------------------------------------
# Alert systemowy
# --------------------------------------------------------------------------
class AlertView(QWidget):
    """Kompaktowy alert: ikona + tytuł + komunikat, fioletowo-niebieska
    pulsująca poświata. Rysuje własne tło, więc wygląda tak samo, gdy pasek
    jest zwinięty i gdy jest rozwinięty (wtedy zasłania nagłówek)."""

    TEXT_WIDTH = ALERT_SIZE[0] - 2 * ALERT_INSET - 80

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFixedSize(*ALERT_SIZE)

        self.icon = QLabel("🔔")
        self.icon.setFixedWidth(34)
        self.icon.setAlignment(Qt.AlignCenter)
        self.icon.setStyleSheet("font-size: 20px; background: transparent;")
        self.title = QLabel("")
        self.title.setStyleSheet(
            f"color: {ALERT_BLUE}; font-size: 10px; font-weight: 700; background: transparent;"
        )
        self.message = QLabel("")
        self.message.setStyleSheet(
            f"color: {TEXT}; font-size: 13px; font-weight: 700; background: transparent;"
        )

        texts = QVBoxLayout()
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(1)
        texts.addStretch(1)
        texts.addWidget(self.title)
        texts.addWidget(self.message)
        texts.addStretch(1)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(ALERT_INSET + 16, ALERT_INSET, ALERT_INSET + 16, ALERT_INSET)
        layout.setSpacing(10)
        layout.addWidget(self.icon)
        layout.addLayout(texts, 1)

    def set_alert(self, kind, title, message):
        self.icon.setText(ALERT_ICONS.get(kind, "🔔"))
        self.title.setText(title.upper())
        metrics = QFontMetrics(self.message.font())
        self.message.setText(metrics.elidedText(message, Qt.ElideRight, self.TEXT_WIDTH))
        self.update()

    def _gradient(self, rect, alpha):
        gradient = QLinearGradient(rect.topLeft(), rect.topRight())
        start, end = QColor(ALERT_VIOLET), QColor(ALERT_BLUE)
        start.setAlpha(alpha)
        end.setAlpha(alpha)
        gradient.setColorAt(0.0, start)
        gradient.setColorAt(1.0, end)
        return gradient

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = QRectF(self.rect()).adjusted(ALERT_INSET, ALERT_INSET, -ALERT_INSET, -ALERT_INSET)
        radius = rect.height() / 2
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        background = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        background.setColorAt(0.0, QColor(BG_CARD))
        background.setColorAt(1.0, QColor(BG_DEEP))
        painter.fillPath(path, background)

        pulse = 0.5 + 0.5 * math.sin(time.monotonic() * 5.0)  # 0..1
        strength = 0.6 + 0.4 * pulse
        for width, alpha in ((12, 20), (8, 38), (4, 70)):
            painter.setPen(QPen(self._gradient(rect, int(alpha * strength)), width))
            painter.drawPath(path)
        painter.setPen(QPen(self._gradient(rect, 255), 1.5))
        painter.drawPath(path)


# --------------------------------------------------------------------------
# Główne okno
# --------------------------------------------------------------------------
def make_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    grad = QLinearGradient(0, 0, 64, 0)
    grad.setColorAt(0, QColor(VIOLET))
    grad.setColorAt(0.5, QColor(MAGENTA))
    grad.setColorAt(1, QColor(SKY))
    painter.setBrush(QColor(BG_DEEP))
    painter.setPen(QPen(grad, 4))
    painter.drawRoundedRect(QRectF(4, 16, 56, 32), 16, 16)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(SKY))
    painter.drawEllipse(QPointF(22, 32), 5, 5)
    painter.drawEllipse(QPointF(42, 32), 5, 5)
    painter.end()
    return QIcon(pixmap)


class DynamicBar(QWidget):
    tick = Signal()  # co sekundę; moduły mogą podpiąć odświeżanie

    def __init__(self):
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle(config.APP_NAME)
        self.setStyleSheet(APP_QSS)

        self.cfg = config.load_config()
        self.weather = WeatherService(self)  # wspólne źródło pogody dla pigułki i zakładki
        self.media = MediaController(self)  # SMTC w osobnym wątku; start w _apply_media()
        self.clipboard_history = ClipboardHistory(self)  # start w _apply_clipboard()
        self._progress = 0.0
        self._target_expanded = False
        self._pinned = False
        self._anchor = QPoint(0, 0)
        self._h_anchor = "center"
        self._v_anchor = "top"
        self._tz = None
        self._modules = []
        self._tab_buttons = []
        self._settings = None  # otwarte okno ustawień (jedno naraz)

        # alerty systemowe
        self.monitor = None  # SystemMonitor (QThread), tworzony wg konfiguracji
        self._alert_progress = 0.0
        self._alert_queue = deque(maxlen=8)
        self._alert_active = False
        self._alert_current = None

        self._build_ui()
        self._build_menu()
        self._build_animation()
        self._build_timers()
        self._build_watcher()
        self._build_tray()
        self._connect_screens()

        self.apply_config(self.cfg, rebuild=True)
        self._on_tick()

    # ---------------------------------------------------------- budowa UI
    def _build_ui(self):
        # zwinięty widok
        self.compact = CompactView(self.weather, self)
        self._compact_fx = QGraphicsOpacityEffect(self.compact)
        self.compact.setGraphicsEffect(self._compact_fx)

        # rozwinięty widok (stały rozmiar, odsłaniany przez rosnące okno)
        self.body = QWidget(self)
        self.body.setFixedSize(*EXPANDED_SIZE)
        self._body_fx = QGraphicsOpacityEffect(self.body)
        self.body.setGraphicsEffect(self._body_fx)

        # alert systemowy (nakładka o stałym rozmiarze, odsłaniana przez okno)
        self.alert_view = AlertView(self)
        self._alert_fx = QGraphicsOpacityEffect(self.alert_view)
        self.alert_view.setGraphicsEffect(self._alert_fx)
        self.alert_view.hide()

        root = QVBoxLayout(self.body)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(6)
        self.tab_layout = QHBoxLayout()
        self.tab_layout.setSpacing(4)
        header.addLayout(self.tab_layout)
        header.addStretch(1)

        self.pin_btn = QPushButton("Przypnij")
        self.pin_btn.setObjectName("Pin")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setCursor(Qt.PointingHandCursor)
        self.pin_btn.setFocusPolicy(Qt.NoFocus)
        self.pin_btn.setMinimumWidth(82)  # „Przypięty” jest szersze niż „Przypnij”
        self.pin_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.pin_btn.clicked.connect(self.toggle_pin)
        header.addWidget(self.pin_btn)

        self.gear_btn = QPushButton("⚙")
        self.gear_btn.setObjectName("Gear")
        self.gear_btn.setToolTip("Ustawienia")
        self.gear_btn.setCursor(Qt.PointingHandCursor)
        self.gear_btn.setFocusPolicy(Qt.NoFocus)
        self.gear_btn.clicked.connect(self.open_settings)
        header.addWidget(self.gear_btn)
        root.addLayout(header)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        self.empty_label = QLabel("Brak włączonych modułów.\nWłącz je w ustawieniach (⚙).")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet(f"color: {MUTED}; font-size: 13px;")
        root.addWidget(self.empty_label, 1)

        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        self._tab_group.idClicked.connect(self._select_tab)

        self.alert_view.raise_()

    def _build_menu(self):
        self.menu = QMenu(self)
        self.act_toggle = QAction("Rozwiń", self)
        self.act_toggle.triggered.connect(self.toggle_pin)
        act_settings = QAction("Ustawienia…", self)
        act_settings.triggered.connect(self.open_settings)
        act_reload = QAction("Przeładuj konfigurację", self)
        act_reload.triggered.connect(self.reload_config)
        act_folder = QAction("Otwórz folder konfiguracji", self)
        act_folder.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(config.get_config_dir())))
        )
        self.act_alerts = QAction("Alerty systemowe", self)
        self.act_alerts.setCheckable(True)
        self.act_alerts.triggered.connect(self._toggle_alerts)
        act_test_alert = QAction("Pokaż testowy alert", self)
        act_test_alert.triggered.connect(
            lambda: self.show_alert("notification", "DynamicBar", "Testowy alert systemowy", force=True)
        )
        act_quit = QAction("Zakończ", self)
        act_quit.triggered.connect(QApplication.quit)
        self.menu.addAction(self.act_toggle)
        self.menu.addAction(act_settings)
        self.menu.addAction(act_reload)
        self.menu.addAction(act_folder)
        self.menu.addSeparator()
        self.menu.addAction(self.act_alerts)
        self.menu.addAction(act_test_alert)
        self.menu.addSeparator()
        self.menu.addAction(act_quit)
        self.menu.aboutToShow.connect(self._on_menu_about_to_show)

    def _on_menu_about_to_show(self):
        self.act_toggle.setText("Zwiń i odepnij" if self._pinned else "Rozwiń i przypnij")
        self.act_alerts.setChecked(bool(self.cfg.get("alerts_enabled", True)))

    def _toggle_alerts(self, checked):
        config.set_setting("alerts_enabled", bool(checked))
        self.reload_config()

    def _build_animation(self):
        self.animation = QPropertyAnimation(self, b"progress", self)
        self.alert_animation = QPropertyAnimation(self, b"alert_progress", self)
        # odświeżanie pulsującej poświaty, tylko gdy alert jest widoczny
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(33)
        self._pulse_timer.timeout.connect(self.alert_view.update)
        self._alert_hold = QTimer(self)
        self._alert_hold.setSingleShot(True)
        self._alert_hold.timeout.connect(self._alert_next)

    def _build_timers(self):
        self._leave_timer = QTimer(self)
        self._leave_timer.setSingleShot(True)
        self._leave_timer.timeout.connect(self._on_leave_timeout)

        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._on_tick)
        self._clock_timer.start()

    def _build_watcher(self):
        """Obserwuje config.json, żeby zmiany w pliku działały na żywo."""
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(300)
        self._reload_timer.timeout.connect(self.reload_config)

        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(lambda _p: self._reload_timer.start())
        self._watcher.fileChanged.connect(lambda _p: self._reload_timer.start())
        self._rewatch()

    def _rewatch(self):
        directory = str(config.get_config_dir())
        path = str(config.get_config_path())
        if directory not in self._watcher.directories():
            self._watcher.addPath(directory)
        if config.get_config_path().exists() and path not in self._watcher.files():
            self._watcher.addPath(path)

    def _build_tray(self):
        self.setWindowIcon(make_icon())
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(make_icon(), self)
            self.tray.setToolTip(config.APP_NAME)
            self.tray.setContextMenu(self.menu)
            self.tray.activated.connect(self._on_tray_activated)
            self.tray.show()

    def _connect_screens(self):
        app = QGuiApplication.instance()
        app.primaryScreenChanged.connect(lambda *_: self._watch_primary_screen())
        app.screenAdded.connect(lambda *_: self.reposition())
        app.screenRemoved.connect(lambda *_: self.reposition())
        self._watch_primary_screen()

    def _watch_primary_screen(self):
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            screen.availableGeometryChanged.connect(lambda *_: self.reposition())
        self.reposition()

    # ---------------------------------------------------------- ustawienia
    def open_settings(self):
        """Otwiera okno ustawień (lub wysuwa już otwarte na wierzch)."""
        if self._settings is not None:
            self._settings.show()
            self._settings.raise_()
            self._settings.activateWindow()
            return

        dialog = SettingsDialog()
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.saved.connect(self.reload_config)
        dialog.preview_changed.connect(self._preview_position)
        dialog.uninstall_started.connect(self._on_uninstall_started)
        dialog.finished.connect(self._on_settings_closed)
        self._settings = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _preview_position(self, position, offset_x, offset_y):
        """Podgląd na żywo: przesuwa pasek bez zapisywania konfiguracji."""
        self.reposition(
            {"bar_position": position, "offset_x": offset_x, "offset_y": offset_y}
        )

    def _on_settings_closed(self, _result=0):
        self._settings = None
        self.reposition()  # po „Anuluj” wraca na zapisaną pozycję

    def _on_uninstall_started(self):
        """Okno ustawień zaraz czyści konfigurację i zamyka program - nie
        pozwalamy, by obserwator pliku odtworzył config.json."""
        self._reload_timer.stop()
        self._watcher.blockSignals(True)
        if self.tray is not None:
            self.tray.hide()

    # ---------------------------------------------------------- konfiguracja
    def apply_config(self, cfg, rebuild=False):
        old_modules = self.cfg.get("modules")
        self.cfg = cfg

        try:
            self._tz = ZoneInfo(cfg["timezone"])
        except Exception:  # brak tzdata / zła nazwa -> czas lokalny
            self._tz = None

        self.weather.configure(
            city=cfg["weather_city"],
            unit=cfg["temperature_unit"],
            refresh_minutes=cfg["weather_refresh_minutes"],
            enabled=cfg["modules"].get("weather", True),
        )

        if rebuild or old_modules != cfg["modules"]:
            self._rebuild_modules()
        else:
            for module in self._modules:
                module.apply_config(cfg)

        self.compact.apply_config(cfg)
        self.reposition()
        self._on_tick()
        self._apply_media()
        self._apply_clipboard()
        self._apply_monitor()

    # ---------------------------------------------------------- schowek
    def _apply_clipboard(self):
        """Monitoruje schowek tylko wtedy, gdy zakładka „Schowek" jest włączona."""
        if ClipboardModule.enabled(self.cfg):
            self.clipboard_history.start()
        else:
            self.clipboard_history.stop()

    # ---------------------------------------------------------- odtwarzacz mediów
    def _apply_media(self):
        """Uruchamia wątek SMTC tylko wtedy, gdy zakładka „Muzyka" jest włączona."""
        if MediaModule.enabled(self.cfg):
            self.media.start()
        else:
            self.media.stop()

    # ---------------------------------------------------------- alerty systemowe
    def _apply_monitor(self):
        """Uruchamia, aktualizuje albo zatrzymuje wątek SystemMonitor wg config.json."""
        cfg = self.cfg
        usb = bool(cfg.get("alert_usb", True))
        network = bool(cfg.get("alert_network", True))
        keys = bool(cfg.get("alert_keys", True))
        notifications = bool(cfg.get("alert_notifications", True))
        enabled = bool(cfg.get("alerts_enabled", True)) and (usb or network or keys or notifications)

        if not enabled:
            self._stop_monitor()
            self._cancel_alerts()
            return

        if self.monitor is None:
            self.monitor = SystemMonitor(self)
            self.monitor.alert.connect(self.show_alert)
            self.monitor.configure(usb, network, keys, notifications)
            self.monitor.start()
        else:
            self.monitor.configure(usb, network, keys, notifications)

    def _stop_monitor(self):
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor.deleteLater()
            self.monitor = None

    def shutdown(self):
        """Wywoływane przy zamykaniu programu (aboutToQuit)."""
        self._stop_monitor()
        self.media.stop()
        self.clipboard_history.stop()
        shutdown_workers()  # trwające zapytania do Gemini

    def show_alert(self, kind, title, message, force=False):
        """Dodaje alert do kolejki; każdy widoczny jest ALERT_DURATION_MS."""
        if not force and not self.cfg.get("alerts_enabled", True):
            return
        item = (kind, title, message)
        if item == self._alert_current and self._alert_active:
            return
        if self._alert_queue and self._alert_queue[-1] == item:
            return
        self._alert_queue.append(item)
        if not self._alert_active:
            self._alert_next()

    def _alert_next(self):
        """Pokazuje następny alert z kolejki albo zwija alert, gdy kolejka jest pusta."""
        if not self._alert_queue:
            self._alert_active = False
            self._alert_current = None
            self._animate_alert(0.0)
            return
        self._alert_active = True
        self._alert_current = self._alert_queue.popleft()
        self.alert_view.set_alert(*self._alert_current)
        self._animate_alert(1.0)
        self._alert_hold.start(ALERT_DURATION_MS)

    def _cancel_alerts(self):
        self._alert_queue.clear()
        self._alert_hold.stop()
        if self._alert_active or self._alert_progress > 0.0:
            self._alert_active = False
            self._alert_current = None
            self._animate_alert(0.0)

    def _animate_alert(self, end):
        distance = abs(end - self._alert_progress)
        self.alert_animation.stop()
        self.alert_animation.setStartValue(self._alert_progress)
        self.alert_animation.setEndValue(end)
        self.alert_animation.setDuration(int(lerp(60, 260, distance)))
        self.alert_animation.setEasingCurve(QEasingCurve.OutCubic)
        self.alert_animation.start()

    def _get_alert_progress(self):
        return self._alert_progress

    def _set_alert_progress(self, value):
        self._alert_progress = float(value)
        if self._alert_progress > 0.001:
            if not self._pulse_timer.isActive():
                self._pulse_timer.start()
        else:
            self._pulse_timer.stop()
        self._apply_progress()

    alert_progress = Property(float, _get_alert_progress, _set_alert_progress)

    def reload_config(self):
        new_cfg = config.load_config(force_reload=True)
        if new_cfg != self.cfg:
            self.apply_config(new_cfg)
        self._rewatch()

    def now(self):
        return datetime.now(self._tz) if self._tz else datetime.now()

    def _on_tick(self):
        show_clock = self.cfg["modules"].get("clock", True)
        text = format_time(self.now(), self.cfg["clock_24h"])
        self.compact.set_time(text, show_clock)
        self.tick.emit()

    # ---------------------------------------------------------- moduły / zakładki
    def current_module(self):
        index = self.stack.currentIndex()
        if 0 <= index < len(self._modules):
            return self._modules[index]
        return None

    def _rebuild_modules(self):
        previous = self.current_module()
        previous_id = previous.id if previous else None

        for button in self._tab_buttons:
            self._tab_group.removeButton(button)
            button.deleteLater()
        self._tab_buttons.clear()
        for module in self._modules:
            self.stack.removeWidget(module)
            module.deleteLater()
        self._modules.clear()

        for cls in MODULE_REGISTRY:
            if not cls.enabled(self.cfg):
                continue
            module = cls(self, self.cfg)
            index = self.stack.addWidget(module)
            self._modules.append(module)

            button = QPushButton(f"{cls.icon}  {cls.title}")
            button.setObjectName("Tab")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setFocusPolicy(Qt.NoFocus)
            # zakładka nigdy nie jest węższa niż jej tekst (koniec z przyciętymi nazwami)
            button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self._tab_group.addButton(button, index)
            self.tab_layout.addWidget(button)
            self._tab_buttons.append(button)

        has_modules = bool(self._modules)
        self.stack.setVisible(has_modules)
        self.empty_label.setVisible(not has_modules)

        if has_modules:
            ids = [m.id for m in self._modules]
            self._select_tab(ids.index(previous_id) if previous_id in ids else 0, notify=False)
            if self._progress > 0.5:
                self.current_module().on_activated()

    def _select_tab(self, index, notify=True):
        if not (0 <= index < len(self._modules)):
            return
        old = self.current_module()
        self.stack.setCurrentIndex(index)
        self._tab_buttons[index].setChecked(True)
        if notify and self._target_expanded:
            if old is not None and old is not self._modules[index]:
                old.on_deactivated()
            self._modules[index].on_activated()

    # ---------------------------------------------------------- pozycjonowanie
    def reposition(self, preview=None):
        """Ustawia pasek wg konfiguracji albo, gdy podano `preview`
        (słownik bar_position/offset_x/offset_y), wg podglądu z ustawień."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        source = preview if preview is not None else self.cfg
        geo = screen.availableGeometry()
        vertical, horizontal = source["bar_position"].split("-")

        if horizontal == "left":
            ax = geo.left() + SCREEN_MARGIN
        elif horizontal == "right":
            ax = geo.left() + geo.width() - SCREEN_MARGIN
        else:
            ax = geo.left() + geo.width() // 2

        if vertical == "top":
            ay = geo.top() + SCREEN_MARGIN
        else:
            ay = geo.top() + geo.height() - SCREEN_MARGIN

        self._h_anchor = horizontal
        self._v_anchor = vertical
        self._anchor = QPoint(ax + source["offset_x"], ay + source["offset_y"])
        self._apply_progress()

    # ---------------------------------------------------------- animacja
    def _get_progress(self):
        return self._progress

    def _set_progress(self, value):
        self._progress = float(value)
        self._apply_progress()

    progress = Property(float, _get_progress, _set_progress)

    def _apply_progress(self):
        t = self._progress
        a = self._alert_progress
        width = round(max(
            lerp(COLLAPSED_SIZE[0], EXPANDED_SIZE[0], t),
            lerp(COLLAPSED_SIZE[0], ALERT_SIZE[0], a),
        ))
        height = round(max(
            lerp(COLLAPSED_SIZE[1], EXPANDED_SIZE[1], t),
            lerp(COLLAPSED_SIZE[1], ALERT_SIZE[1], a),
        ))

        if self._h_anchor == "left":
            x = self._anchor.x()
        elif self._h_anchor == "right":
            x = self._anchor.x() - width
        else:
            x = self._anchor.x() - width // 2
        y = self._anchor.y() if self._v_anchor == "top" else self._anchor.y() - height

        self.setGeometry(x, y, width, height)

        body_opacity = clamp((t - 0.45) / 0.55, 0.0, 1.0)
        alert_opacity = clamp((a - 0.35) / 0.65, 0.0, 1.0)
        compact_opacity = clamp(1.0 - t / 0.4, 0.0, 1.0) * (1.0 - clamp(a / 0.5, 0.0, 1.0))
        self._body_fx.setOpacity(body_opacity)
        self._compact_fx.setOpacity(compact_opacity)
        self._alert_fx.setOpacity(alert_opacity)
        self.body.setVisible(body_opacity > 0.01)
        self.compact.setVisible(compact_opacity > 0.01)
        self.alert_view.setVisible(alert_opacity > 0.01)
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w, h = self.width(), self.height()
        self.compact.setGeometry(0, 0, w, h)
        body_x = (w - EXPANDED_SIZE[0]) // 2
        body_y = 0 if self._v_anchor == "top" else h - EXPANDED_SIZE[1]
        self.body.move(body_x, body_y)
        alert_x = (w - ALERT_SIZE[0]) // 2
        alert_y = 0 if self._v_anchor == "top" else h - ALERT_SIZE[1]
        self.alert_view.move(alert_x, alert_y)
        self.alert_view.raise_()

    def set_expanded(self, expanded):
        if expanded == self._target_expanded:
            return
        self._target_expanded = expanded
        self._leave_timer.stop()

        end = 1.0 if expanded else 0.0
        distance = abs(end - self._progress)
        self.animation.stop()
        self.animation.setStartValue(self._progress)
        self.animation.setEndValue(end)
        if expanded:
            self.animation.setDuration(int(lerp(120, 320, distance)))
            self.animation.setEasingCurve(QEasingCurve.OutCubic)
        else:
            self.animation.setDuration(int(lerp(100, 260, distance)))
            self.animation.setEasingCurve(QEasingCurve.InOutCubic)
        self.animation.start()

        module = self.current_module()
        if module is not None:
            (module.on_activated if expanded else module.on_deactivated)()

    # ---------------------------------------------------------- mysz
    def enterEvent(self, event):
        self._leave_timer.stop()
        if self.cfg["expand_trigger"] == "hover":
            self.set_expanded(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if not self._pinned:
            self._leave_timer.start(250)
        super().leaveEvent(event)

    def _on_leave_timeout(self):
        if self._pinned or self.frameGeometry().contains(QCursor.pos()):
            return
        module = self.current_module()
        if self._progress > 0.5 and module is not None and module.keeps_open():
            return  # np. trwa pisanie pytania do Gemini
        self.set_expanded(False)

    def schedule_leave_check(self):
        """Moduł zgłasza, że już nie potrzebuje otwartego paska (np. utrata
        fokusu); pasek zwinie się, jeśli kursor jest poza nim i nie jest przypięty."""
        if not self._pinned and self._target_expanded:
            self._leave_timer.start(250)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._alert_active and self._alert_progress > 0.5:
                # kliknięcie w widoczny alert tylko go zamyka (bez przypinania)
                self._alert_queue.clear()
                self._alert_hold.stop()
                self._alert_next()
                event.accept()
                return
            self.toggle_pin()
            event.accept()
        else:
            super().mousePressEvent(event)

    def toggle_pin(self):
        """Kliknięcie: rozwija i przypina pasek albo odpina go (i zwija)."""
        self._pinned = not self._pinned
        self.pin_btn.setChecked(self._pinned)
        self.pin_btn.setText("Przypięty" if self._pinned else "Przypnij")
        if self._pinned:
            self.set_expanded(True)
        elif self.cfg["expand_trigger"] == "click" or not self.frameGeometry().contains(QCursor.pos()):
            self.set_expanded(False)

    def contextMenuEvent(self, event):
        self.menu.exec(event.globalPos())

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.toggle_pin()

    # ---------------------------------------------------------- rysowanie
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        t = self._progress
        # gdy alert zastępuje zwiniętą pigułkę, jej tło i obramowanie znikają
        alert_cover = clamp((self._alert_progress - 0.35) / 0.65, 0.0, 1.0)
        pill_fade = 1.0 - alert_cover * (1.0 - t)
        if pill_fade <= 0.01:
            return
        painter.setOpacity(pill_fade)
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        radius = min(lerp(RADIUS_COLLAPSED, RADIUS_EXPANDED, t), rect.height() / 2)
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        background = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        background.setColorAt(0.0, QColor(BG_CARD))
        background.setColorAt(0.35, QColor(BG_DEEP))
        background.setColorAt(1.0, QColor(BG_DEEP))
        painter.fillPath(path, background)

        border = QLinearGradient(rect.topLeft(), rect.topRight())
        border.setColorAt(0.0, QColor(VIOLET))
        border.setColorAt(0.5, QColor(MAGENTA))
        border.setColorAt(1.0, QColor(SKY))

        glow = QPen(border, 4)
        glow_color = QColor(MAGENTA)
        glow_color.setAlpha(int(lerp(25, 45, t)))
        glow.setBrush(glow_color)
        painter.setPen(glow)
        painter.drawPath(path)

        outline = QPen(border, 1.5)
        painter.setPen(outline)
        painter.setOpacity(lerp(0.75, 1.0, t) * pill_fade)
        painter.drawPath(path)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    app.setQuitOnLastWindowClosed(False)  # okno Tool nie powinno kończyć programu
    bar = DynamicBar()
    app.aboutToQuit.connect(bar.shutdown)  # czyste zatrzymanie wątku monitora
    bar.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
