"""Widżety paska DynamicBar: oczka, zegar z datą i pogoda (Open-Meteo).

Moduł jest niezależny od main.py (nie importuje go), dzięki czemu main.py
może bezpiecznie importować stąd paletę kolorów i widżety.

Zawartość:
  * EyesWidget     - animowane cyfrowe oczka (mruganie, zerkanie, śledzenie kursora)
  * ClockCard      - zegar i data na żywo dla wybranej strefy czasowej
  * WeatherService - pobieranie pogody z Open-Meteo w tle (bez klucza API)
  * WeatherIcon    - ikona pogody rysowana wektorowo (bez czcionek emoji)
  * WeatherBadge   - mały widżet (ikona + temperatura) do zwiniętej pigułki
  * WeatherCard    - większa karta pogody do zakładki "Pogoda"
"""

import json
import math
import random
import time
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import (
    QObject,
    QPointF,
    QRectF,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRadialGradient,
)
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

# --------------------------------------------------------------------------
# Paleta (współdzielona z main.py)
# --------------------------------------------------------------------------
BG_DEEP = "#0B0B0F"
BG_CARD = "#16161E"
BORDER_SOFT = "#262634"
VIOLET = "#8B5CF6"
MAGENTA = "#C026D3"
SKY = "#38BDF8"
TEXT = "#E7E7EE"
MUTED = "#8A8AA0"

DAYS_PL = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
MONTHS_PL = [
    "stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca",
    "lipca", "sierpnia", "września", "października", "listopada", "grudnia",
]

CARD_QSS = (
    f"QFrame#Card {{ background: {BG_CARD}; border: 1px solid {BORDER_SOFT};"
    f" border-radius: 14px; }}"
)


def lerp(a, b, t):
    return a + (b - a) * t


def clamp(value, low, high):
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# Pomocnicze funkcje czasu
# --------------------------------------------------------------------------
def make_timezone(name):
    """Zwraca ZoneInfo albo None (brak tzdata / zła nazwa -> czas lokalny)."""
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def format_time(dt, use_24h=True, seconds=False):
    if use_24h:
        return dt.strftime("%H:%M:%S" if seconds else "%H:%M")
    hour = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    text = f"{hour}:{dt.minute:02d}"
    if seconds:
        text += f":{dt.second:02d}"
    return f"{text} {suffix}"


def format_date_pl(dt):
    return f"{DAYS_PL[dt.weekday()].capitalize()}, {dt.day} {MONTHS_PL[dt.month - 1]} {dt.year}"


def format_utc_offset(dt):
    offset = dt.utcoffset() if dt.tzinfo else dt.astimezone().utcoffset()
    if offset is None:
        return ""
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


# --------------------------------------------------------------------------
# 1. Oczka
# --------------------------------------------------------------------------
class EyesWidget(QWidget):
    """Dwoje świecących oczu: delikatne mruganie co kilka sekund, od czasu do
    czasu zerknięcie na bok, a gdy kursor jest blisko - patrzą w jego stronę.

    Wyłączanie: config["modules"]["eyes"] = false ukrywa oczka,
    config["animated_eyes"] = false zostawia je statyczne.
    """

    EYE_W = 12.0
    EYE_H = 17.0
    GAP = 11.0
    BLINK_DURATION = 0.17  # sekundy
    FOLLOW_RADIUS = 260.0  # px - w tym promieniu oczy śledzą kursor
    GLOW_RADIUS = 15.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(58, 30)
        self._animated = True
        self._look = QPointF(0, 0)
        self._idle_target = QPointF(0, 0)
        self._open = 1.0
        self._blink_start = None
        self._double_blink = False

        now = time.monotonic()
        self._last = now
        self._next_blink = now + random.uniform(2.0, 4.0)
        self._next_glance = now + random.uniform(1.5, 3.5)
        self._glance_end = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._step)

    # --- sterowanie ---
    def set_animated(self, animated):
        self._animated = bool(animated)
        if not self._animated:
            self._look = QPointF(0, 0)
            self._idle_target = QPointF(0, 0)
            self._open = 1.0
            self._blink_start = None
            self._glance_end = 0.0
        else:
            self._last = time.monotonic()
        self._sync_timer()
        self.update()

    def _sync_timer(self):
        if self._animated and self.isVisible():
            self._timer.start()
        else:
            self._timer.stop()

    def showEvent(self, event):
        super().showEvent(event)
        self._last = time.monotonic()
        self._sync_timer()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    # --- logika animacji ---
    def _eye_center(self, index):
        total = self.EYE_W * 2 + self.GAP
        left = (self.width() - total) / 2
        return QPointF(
            left + self.EYE_W / 2 + index * (self.EYE_W + self.GAP),
            self.height() / 2,
        )

    def _step(self):
        now = time.monotonic()
        dt = min(0.1, now - self._last)
        self._last = now

        # -- dokąd patrzeć --
        cursor = QPointF(QCursor.pos())
        center = QPointF(self.mapToGlobal(self.rect().center()))
        dx, dy = cursor.x() - center.x(), cursor.y() - center.y()
        dist = math.hypot(dx, dy)

        if dist < self.FOLLOW_RADIUS:
            self._glance_end = 0.0
            self._idle_target = QPointF(0, 0)
            if dist > 0.5:
                strength = clamp(dist / 90.0, 0.0, 1.0)
                target = QPointF(dx / dist * strength, dy / dist * strength)
            else:
                target = QPointF(0, 0)
        else:
            if self._glance_end and now >= self._glance_end:
                # koniec zerknięcia - wracamy na środek
                self._glance_end = 0.0
                self._idle_target = QPointF(0, 0)
            elif not self._glance_end and now >= self._next_glance:
                direction = random.choice((-1.0, 1.0))
                self._idle_target = QPointF(direction, random.uniform(-0.3, 0.2))
                self._glance_end = now + random.uniform(0.7, 1.3)
                self._next_glance = self._glance_end + random.uniform(2.5, 6.0)
            target = self._idle_target

        k = 1.0 - math.exp(-dt * 12.0)
        new_look = QPointF(
            self._look.x() + (target.x() - self._look.x()) * k,
            self._look.y() + (target.y() - self._look.y()) * k,
        )

        # -- mruganie --
        new_open = 1.0
        if self._blink_start is None and now >= self._next_blink:
            self._blink_start = now
            self._next_blink = now + random.uniform(2.8, 6.0)
            self._double_blink = random.random() < 0.15
        if self._blink_start is not None:
            phase = (now - self._blink_start) / self.BLINK_DURATION
            if phase >= 1.0:
                if self._double_blink:
                    self._double_blink = False
                    self._blink_start = now
                    phase = 0.0
                else:
                    self._blink_start = None
                    phase = None
            if phase is not None:
                new_open = 1.0 - 0.93 * math.sin(math.pi * phase)

        moved = (
            abs(new_look.x() - self._look.x()) > 0.002
            or abs(new_look.y() - self._look.y()) > 0.002
        )
        if moved or new_open != self._open:
            self._look = new_look
            self._open = new_open
            self.update()

    # --- rysowanie ---
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        openness = self._open
        squish = 1.0 + 0.10 * (1.0 - openness)  # przy mrugnięciu oko lekko się rozszerza

        for index in range(2):
            c = self._eye_center(index)
            c = QPointF(c.x() + self._look.x() * 2.6, c.y() + self._look.y() * 1.8)
            w = self.EYE_W * squish
            h = max(1.6, self.EYE_H * openness)
            rect = QRectF(c.x() - w / 2, c.y() - h / 2, w, h)

            # subtelna poświata
            glow_r = self.GLOW_RADIUS
            glow = QRadialGradient(c, glow_r)
            glow.setColorAt(0.0, QColor(139, 92, 246, 95))
            glow.setColorAt(0.5, QColor(56, 189, 248, 38))
            glow.setColorAt(1.0, QColor(56, 189, 248, 0))
            painter.setPen(Qt.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(c, glow_r, glow_r * (0.6 + 0.4 * openness))

            # korpus oka: gradient niebieski -> fioletowy
            body = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            body.setColorAt(0.0, QColor("#7DD3FC"))
            body.setColorAt(1.0, QColor(VIOLET))
            painter.setBrush(body)
            radius = min(w, h) / 2
            painter.drawRoundedRect(rect, radius, radius)

            # refleksy - dodają życia, ale nie są przesadzone
            if openness > 0.5:
                hx = rect.left() + w * 0.34 + self._look.x() * 0.8
                hy = rect.top() + h * 0.26 + self._look.y() * 0.5
                painter.setBrush(QColor(255, 255, 255, 220))
                painter.drawEllipse(QRectF(hx - 1.8, hy - 1.8, 3.6, 3.6 * openness))
                painter.setBrush(QColor(255, 255, 255, 110))
                painter.drawEllipse(
                    QRectF(rect.right() - w * 0.42, rect.bottom() - h * 0.28, 1.8, 1.8 * openness)
                )


# --------------------------------------------------------------------------
# 2. Zegar i data
# --------------------------------------------------------------------------
class ClockCard(QFrame):
    """Godzina z sekundami, pełna data po polsku i nazwa strefy czasowej.
    Odświeża się tylko wtedy, gdy jest widoczna."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setStyleSheet(CARD_QSS)
        self.setMinimumHeight(120)

        self._tz = None
        self._tz_name = ""
        self._use_24h = True
        self._last_text = None

        self._time = QLabel("--:--:--")
        self._time.setStyleSheet(
            f"color: {SKY}; font-size: 40px; font-weight: 700; background: transparent;"
        )
        self._date = QLabel("—")
        self._date.setStyleSheet(
            f"color: {TEXT}; font-size: 14px; font-weight: 600; background: transparent;"
        )
        self._zone = QLabel("")
        self._zone.setStyleSheet(f"color: {MUTED}; font-size: 11px; background: transparent;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(0)
        layout.addWidget(self._time)
        layout.addWidget(self._date)
        layout.addSpacing(4)
        layout.addWidget(self._zone)
        layout.addStretch(1)

        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.refresh)

    def configure(self, timezone, use_24h=True):
        self._tz_name = timezone or ""
        self._tz = make_timezone(self._tz_name) if self._tz_name else None
        self._use_24h = bool(use_24h)
        self._last_text = None
        self.refresh()

    def now(self):
        return datetime.now(self._tz) if self._tz else datetime.now()

    def refresh(self):
        now = self.now()
        text = format_time(now, self._use_24h, seconds=True)
        if text != self._last_text:
            self._last_text = text
            self._time.setText(text)
        self._date.setText(format_date_pl(now))
        zone = self._tz_name if self._tz else "Czas lokalny"
        offset = format_utc_offset(now)
        self._zone.setText(f"{zone}  ·  {offset}" if offset else zone)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()


# --------------------------------------------------------------------------
# 3. Pogoda (Open-Meteo)
# --------------------------------------------------------------------------
# Kody WMO -> (opis po polsku, rodzaj ikony)
WEATHER_CODES = {
    0: ("Bezchmurnie", "clear"),
    1: ("Prawie bezchmurnie", "partly"),
    2: ("Częściowe zachmurzenie", "partly"),
    3: ("Pochmurno", "cloud"),
    45: ("Mgła", "fog"),
    48: ("Mgła szronowa", "fog"),
    51: ("Lekka mżawka", "drizzle"),
    53: ("Mżawka", "drizzle"),
    55: ("Gęsta mżawka", "drizzle"),
    56: ("Marznąca mżawka", "drizzle"),
    57: ("Marznąca mżawka", "drizzle"),
    61: ("Lekki deszcz", "rain"),
    63: ("Deszcz", "rain"),
    65: ("Silny deszcz", "rain"),
    66: ("Marznący deszcz", "rain"),
    67: ("Marznący deszcz", "rain"),
    71: ("Lekki śnieg", "snow"),
    73: ("Śnieg", "snow"),
    75: ("Silny śnieg", "snow"),
    77: ("Ziarna śnieżne", "snow"),
    80: ("Przelotny deszcz", "rain"),
    81: ("Przelotny deszcz", "rain"),
    82: ("Ulewa", "rain"),
    85: ("Przelotny śnieg", "snow"),
    86: ("Przelotny śnieg", "snow"),
    95: ("Burza", "thunder"),
    96: ("Burza z gradem", "thunder"),
    99: ("Burza z gradem", "thunder"),
}


def describe_weather(code):
    return WEATHER_CODES.get(code, ("Pogoda", "cloud"))


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT = 6


def _get_json(url, params):
    full_url = url + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(full_url, headers={"User-Agent": "DynamicBar/1.0"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _geocode(city):
    name = city.split(",")[0].strip()
    payload = _get_json(
        GEOCODING_URL, {"name": name, "count": 1, "language": "pl", "format": "json"}
    )
    results = payload.get("results") or []
    if not results:
        raise LookupError(f"Nie znaleziono miasta: {name}")
    first = results[0]
    return {
        "name": first.get("name", name),
        "country": first.get("country_code", ""),
        "lat": first["latitude"],
        "lon": first["longitude"],
    }


def _fetch_current(geo, unit):
    payload = _get_json(
        FORECAST_URL,
        {
            "latitude": geo["lat"],
            "longitude": geo["lon"],
            "current": "temperature_2m,weather_code,is_day,wind_speed_10m",
            "temperature_unit": unit,
            "wind_speed_unit": "kmh",
            "timezone": "auto",
        },
    )
    current = payload.get("current")
    if not current:
        raise ValueError("Pusta odpowiedź z Open-Meteo")
    return current


def _friendly_error(exc):
    if isinstance(exc, LookupError):
        return str(exc)
    if isinstance(exc, OSError):  # URLError, timeout, brak sieci
        return "Brak połączenia"
    return "Błąd pobierania pogody"


class _FetchSignals(QObject):
    done = Signal(int, object, object)
    error = Signal(int, str)


class _FetchTask(QRunnable):
    """Pobiera pogodę w puli wątków, żeby nie blokować interfejsu."""

    def __init__(self, token, city, unit, geo, signals):
        super().__init__()
        self._token = token
        self._city = city
        self._unit = unit
        self._geo = geo
        self._signals = signals

    def run(self):
        try:
            geo = self._geo or _geocode(self._city)
            data = _fetch_current(geo, self._unit)
        except Exception as exc:  # noqa: BLE001 - każdy błąd sieci/JSON ma być obsłużony
            self._signals.error.emit(self._token, _friendly_error(exc))
            return
        self._signals.done.emit(self._token, geo, data)


class WeatherService(QObject):
    """Jedno źródło pogody dla całego paska (pigułka i zakładka dzielą dane).

    Sygnały:
      updated(dict) - nowe dane: city, country, temperature, unit, wind, code,
                      is_day, description, kind, fetched_at
      failed(str)   - komunikat błędu (po minucie następuje ponowna próba)
    """

    updated = Signal(object)
    failed = Signal(str)

    RETRY_MS = 60_000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.city = ""
        self.unit = "celsius"
        self.enabled = False
        self.last = None
        self._geo = {}
        self._token = 0

        self._signals = _FetchSignals(self)
        self._signals.done.connect(self._on_done)
        self._signals.error.connect(self._on_error)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.setInterval(self.RETRY_MS)
        self._retry.timeout.connect(self.refresh)

    def configure(self, city, unit, refresh_minutes, enabled):
        city = (city or "").strip()
        unit = "fahrenheit" if unit == "fahrenheit" else "celsius"
        source_changed = city != self.city or unit != self.unit
        newly_enabled = bool(enabled) and not self.enabled

        self.city, self.unit, self.enabled = city, unit, bool(enabled)
        self._timer.setInterval(max(1, int(refresh_minutes)) * 60_000)

        if source_changed:
            self.last = None

        if not self.enabled or not self.city:
            self._timer.stop()
            self._retry.stop()
            self._token += 1  # odrzuć odpowiedzi w locie
            return

        if not self._timer.isActive():
            self._timer.start()
        if source_changed or newly_enabled:
            self.refresh()

    def refresh(self):
        if not self.enabled or not self.city:
            return
        self._retry.stop()
        self._token += 1
        geo = self._geo.get(self.city.lower())
        task = _FetchTask(self._token, self.city, self.unit, geo, self._signals)
        QThreadPool.globalInstance().start(task)

    @Slot(int, object, object)
    def _on_done(self, token, geo, current):
        if token != self._token:
            return  # nieaktualna odpowiedź (zmieniono miasto/jednostkę)
        self._geo[self.city.lower()] = geo
        code = int(current.get("weather_code", -1))
        description, kind = describe_weather(code)
        self.last = {
            "city": geo["name"],
            "country": geo["country"],
            "temperature": float(current["temperature_2m"]),
            "unit": "°F" if self.unit == "fahrenheit" else "°C",
            "wind": float(current.get("wind_speed_10m", 0.0)),
            "code": code,
            "is_day": bool(current.get("is_day", 1)),
            "description": description,
            "kind": kind,
            "fetched_at": datetime.now(),
        }
        self.updated.emit(self.last)

    @Slot(int, str)
    def _on_error(self, token, message):
        if token != self._token:
            return
        self.failed.emit(message)
        self._retry.start()


# --------------------------------------------------------------------------
# Ikona pogody (rysowana wektorowo w siatce 24x24)
# --------------------------------------------------------------------------
_SUN = QColor("#FBBF24")
_MOON = QColor("#C4B5FD")
_CLOUD = QColor("#B4BEF5")
_RAIN = QColor(SKY)
_SNOW = QColor("#E0F2FE")
_FOG = QColor("#9CA3C7")


def _cloud_path():
    path = QPainterPath()
    path.setFillRule(Qt.WindingFill)
    path.addRoundedRect(QRectF(3.5, 12.0, 17.0, 7.0), 3.5, 3.5)
    path.addEllipse(QPointF(9.0, 11.5), 4.0, 4.0)
    path.addEllipse(QPointF(14.0, 9.5), 5.0, 5.0)
    return path


def _draw_cloud(p, dx=0.0, dy=0.0, scale=1.0):
    p.save()
    p.translate(dx, dy)
    p.scale(scale, scale)
    p.setPen(Qt.NoPen)
    p.setBrush(_CLOUD)
    p.drawPath(_cloud_path())
    p.restore()


def _draw_sun(p, cx, cy, r):
    p.setPen(Qt.NoPen)
    p.setBrush(_SUN)
    p.drawEllipse(QPointF(cx, cy), r, r)
    pen = QPen(_SUN, 1.5)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    for i in range(8):
        a = i * math.pi / 4
        p.drawLine(
            QPointF(cx + math.cos(a) * (r + 1.9), cy + math.sin(a) * (r + 1.9)),
            QPointF(cx + math.cos(a) * (r + 3.7), cy + math.sin(a) * (r + 3.7)),
        )


def _draw_moon(p, cx, cy, r):
    disc = QPainterPath()
    disc.addEllipse(QPointF(cx, cy), r, r)
    cut = QPainterPath()
    cut.addEllipse(QPointF(cx + r * 0.55, cy - r * 0.35), r * 0.85, r * 0.85)
    p.setPen(Qt.NoPen)
    p.setBrush(_MOON)
    p.drawPath(disc.subtracted(cut))


def _draw_lines(p, color, segments, width=1.6):
    pen = QPen(color, width)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    for (x1, y1), (x2, y2) in segments:
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))


def draw_weather_icon(p, kind, is_day=True):
    """Rysuje ikonę w układzie 24x24 (painter musi być wcześniej przeskalowany)."""
    if kind == "clear":
        if is_day:
            _draw_sun(p, 12, 12, 4.4)
        else:
            _draw_moon(p, 12, 12, 7.0)
    elif kind == "partly":
        if is_day:
            _draw_sun(p, 8.5, 8.5, 3.2)
        else:
            _draw_moon(p, 8.5, 8.5, 5.0)
        _draw_cloud(p, 4.0, 5.0, 0.8)
    elif kind == "cloud":
        _draw_cloud(p, 0.0, 1.0)
    elif kind == "fog":
        _draw_cloud(p, 0.0, -3.0)
        _draw_lines(p, _FOG, [((4.5, 18.0), (19.5, 18.0)), ((7.0, 21.0), (17.0, 21.0))], 1.4)
    elif kind == "drizzle":
        _draw_cloud(p, 0.0, -2.5)
        _draw_lines(p, _RAIN, [((9.0, 18.0), (8.2, 20.2)), ((14.0, 18.0), (13.2, 20.2))])
    elif kind == "rain":
        _draw_cloud(p, 0.0, -2.5)
        _draw_lines(
            p, _RAIN,
            [((8.0, 17.8), (7.0, 21.2)), ((12.0, 17.8), (11.0, 21.2)), ((16.0, 17.8), (15.0, 21.2))],
        )
    elif kind == "snow":
        _draw_cloud(p, 0.0, -2.5)
        p.setPen(Qt.NoPen)
        p.setBrush(_SNOW)
        for x, y in ((8.0, 18.8), (12.0, 21.0), (16.0, 18.8)):
            p.drawEllipse(QPointF(x, y), 1.1, 1.1)
    elif kind == "thunder":
        _draw_cloud(p, 0.0, -2.5)
        bolt = QPolygonF(
            [
                QPointF(13.2, 14.0), QPointF(9.5, 19.2), QPointF(12.0, 19.2),
                QPointF(10.8, 23.0), QPointF(15.5, 17.3), QPointF(12.9, 17.3),
                QPointF(14.3, 14.0),
            ]
        )
        p.setPen(Qt.NoPen)
        p.setBrush(_SUN)
        p.drawPolygon(bolt)


class WeatherIcon(QWidget):
    def __init__(self, size=48, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._kind = "cloud"
        self._is_day = True

    def set_condition(self, kind, is_day=True):
        self._kind = kind
        self._is_day = bool(is_day)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.scale(self.width() / 24.0, self.height() / 24.0)
        draw_weather_icon(painter, self._kind, self._is_day)


def _format_temp(data):
    return f"{round(data['temperature'])}{data['unit']}"


class WeatherBadge(QWidget):
    """Mała ikona + temperatura do zwiniętej pigułki. Ukryta, dopóki nie ma danych."""

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self._enabled = True
        self._has_data = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._icon = WeatherIcon(20)
        self._label = QLabel("")
        self._label.setStyleSheet(
            f"color: {TEXT}; font-size: 13px; font-weight: 700; background: transparent;"
        )
        layout.addWidget(self._icon)
        layout.addWidget(self._label)

        service.updated.connect(self.set_weather)
        if service.last:
            self.set_weather(service.last)
        else:
            self._refresh_visibility()

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self._refresh_visibility()

    def set_weather(self, data):
        self._has_data = bool(data)
        if data:
            self._icon.set_condition(data["kind"], data["is_day"])
            self._label.setText(_format_temp(data))
            self.setToolTip(f"{data['city']}: {data['description']}")
        self._refresh_visibility()

    def _refresh_visibility(self):
        self.setVisible(self._enabled and self._has_data)


class WeatherCard(QFrame):
    """Karta pogody do zakładki. Kliknięcie odświeża dane."""

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self._service = service
        self.setObjectName("Card")
        self.setStyleSheet(CARD_QSS)
        self.setMinimumHeight(110)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Kliknij, aby odświeżyć")

        self._icon = WeatherIcon(64)
        self._temp = QLabel("—")
        self._temp.setStyleSheet(
            f"color: {VIOLET}; font-size: 34px; font-weight: 700; background: transparent;"
        )
        self._desc = QLabel("Ładowanie…")
        self._desc.setStyleSheet(
            f"color: {TEXT}; font-size: 14px; font-weight: 600; background: transparent;"
        )
        self._meta = QLabel(service.city)
        self._meta.setStyleSheet(f"color: {MUTED}; font-size: 11px; background: transparent;")

        info = QVBoxLayout()
        info.setSpacing(0)
        info.addWidget(self._temp)
        info.addWidget(self._desc)
        info.addSpacing(3)
        info.addWidget(self._meta)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(16)
        layout.addWidget(self._icon, 0, Qt.AlignVCenter)
        layout.addLayout(info, 1)

        service.updated.connect(self.set_weather)
        service.failed.connect(self.set_error)
        if service.last:
            self.set_weather(service.last)

    def set_city_hint(self, city):
        """Pokazuje nazwę miasta z configu, dopóki nie przyjdą dane."""
        if not self._service.last:
            self._meta.setText(city)

    def set_weather(self, data):
        self._icon.set_condition(data["kind"], data["is_day"])
        self._temp.setText(_format_temp(data))
        self._desc.setText(data["description"])
        place = data["city"] + (f", {data['country']}" if data["country"] else "")
        self._meta.setText(
            f"{place}  ·  wiatr {data['wind']:.0f} km/h  ·  {data['fetched_at']:%H:%M}"
        )

    def set_error(self, message):
        self._desc.setText(message)
        if not self._service.last:
            self._temp.setText("—")
            self._meta.setText(f"{self._service.city}  ·  kliknij, aby ponowić")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._service.refresh()
            event.accept()
        else:
            super().mousePressEvent(event)
