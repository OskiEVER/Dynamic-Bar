"""media_player.py - odtwarzacz mediów dla DynamicBar (Windows 11, PySide6).

Korzysta z Windows Global System Media Transport Controls (SMTC), więc działa
z każdą aplikacją i przeglądarką, która rejestruje się w systemowym panelu
multimediów: Spotify, YouTube/Chrome/Edge/Firefox, VLC, Media Player itd.

Architektura:
  * MediaState      - niezmienny snapshot stanu (utwór, okładka, przyciski, pozycja).
  * MediaController - QObject; w osobnym wątku uruchamia pętlę asyncio, która
                      rozmawia z WinRT (zdarzenia SMTC + lekki polling jako
                      zabezpieczenie). Do GUI wysyła tylko sygnał state_changed,
                      więc interfejs nigdy nie czeka na WinRT. Komendy
                      (poprzedni / play-pause / następny / przewijanie) trafiają
                      do pętli przez run_coroutine_threadsafe.
  * MediaPanel      - widżet UI: okładka, tytuł, wykonawca, trzy przyciski
                      i suwak. Pozycję między próbkami z backendu wylicza lokalnie
                      (QTimer 200 ms, tylko gdy zakładka jest widoczna).

Wymagane biblioteki (jedna z dwóch opcji):
  pip install winrt-runtime winrt-Windows.Media.Control winrt-Windows.Storage.Streams winrt-Windows.Foundation winrt-Windows.Foundation.Collections
  albo (starszy, ale prostszy pakiet):
  pip install winsdk
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from widgets import BG_CARD, BG_DEEP, BORDER_SOFT, MAGENTA, MUTED, SKY, TEXT, VIOLET

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Import WinRT: najpierw modularne `winrt`, potem `winsdk`
# --------------------------------------------------------------------------
_Manager = _Buffer = _DataReader = _InputStreamOptions = None
BACKEND = None
IMPORT_ERROR = ""

for _pkg in ("winrt", "winsdk"):
    try:
        _ctl = importlib.import_module(f"{_pkg}.windows.media.control")
        _streams = importlib.import_module(f"{_pkg}.windows.storage.streams")
        _Manager = _ctl.GlobalSystemMediaTransportControlsSessionManager
        _Buffer = _streams.Buffer
        _DataReader = _streams.DataReader
        _InputStreamOptions = _streams.InputStreamOptions
        BACKEND = _pkg
        break
    except Exception as _exc:  # brak pakietu / system inny niż Windows
        IMPORT_ERROR = f"{_pkg}: {_exc}"

AVAILABLE = BACKEND is not None

INSTALL_HINT = (
    "Zainstaluj: pip install winsdk\n"
    "lub winrt-runtime winrt-Windows.Media.Control winrt-Windows.Storage.Streams "
    "winrt-Windows.Foundation winrt-Windows.Foundation.Collections"
)

TICKS_PER_SECOND = 10_000_000  # SMTC liczy pozycję w tickach po 100 ns
MAX_ART_BYTES = 8 * 1024 * 1024
COVER_SIZE = 150


# --------------------------------------------------------------------------
# Stan
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class MediaState:
    has_session: bool = False
    app_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    art: bytes | None = None
    art_id: int = 0  # zmienia się, gdy zmieni się okładka (tani odpowiednik porównania bajtów)
    status: str = "stopped"  # playing / paused / stopped
    can_prev: bool = False
    can_next: bool = False
    can_play_pause: bool = False
    can_seek: bool = False
    position: float = 0.0  # sekundy, w chwili sampled_at
    duration: float = 0.0
    rate: float = 1.0
    sampled_at: float = 0.0  # time.monotonic()

    @property
    def is_playing(self):
        return self.status == "playing"

    def position_at(self, now):
        """Pozycja w chwili `now` (time.monotonic()), z ekstrapolacją podczas odtwarzania."""
        pos = self.position
        if self.is_playing:
            pos += max(0.0, now - self.sampled_at) * self.rate
        if self.duration > 0:
            pos = min(pos, self.duration)
        return max(0.0, pos)

    def signature(self):
        """Pola, których zmiana wymaga ponownego wysłania stanu do GUI."""
        return (
            self.has_session, self.app_id, self.title, self.artist, self.album,
            self.art_id, self.status, self.can_prev, self.can_next,
            self.can_play_pause, self.can_seek, round(self.duration, 1),
            round(self.rate, 2),
        )


def _secs(value):
    """TimeSpan -> sekundy (winrt/winsdk zwracają timedelta)."""
    try:
        return value.total_seconds()
    except AttributeError:
        return float(value) / TICKS_PER_SECOND


def _age_seconds(moment):
    """Ile sekund minęło od `last_updated_time` (0, gdy wartość jest niewiarygodna)."""
    try:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - moment).total_seconds()
    except Exception:
        return 0.0
    return age if 0.0 <= age < 6 * 3600 else 0.0


# --------------------------------------------------------------------------
# Backend: wątek asyncio + SMTC
# --------------------------------------------------------------------------
class MediaController(QObject):
    """Most między SMTC (WinRT) a GUI. Cała komunikacja z WinRT dzieje się w
    osobnym wątku; GUI dostaje wyłącznie sygnał `state_changed(MediaState)`."""

    state_changed = Signal(object)

    POLL_ACTIVE = 1.0  # sekundy: pasek rozwinięty i widoczna zakładka
    POLL_IDLE = 3.0    # zdarzenia SMTC i tak budzą pętlę natychmiast

    def __init__(self, parent=None):
        super().__init__(parent)
        self.last_state = MediaState()
        self._thread = None
        self._loop = None
        self._wake = None
        self._running = False
        self._interval = self.POLL_IDLE

        # poniższe pola dotyka wyłącznie wątek asyncio
        self._manager = None
        self._mgr_tokens = []
        self._hooked = None  # (session, app_id, [(remove_name, token), ...])
        self._sent = None
        self._art = None
        self._art_id = 0
        self._art_key = None
        self._art_tries = 0

    # ------------------------------------------------------ API dla GUI
    @property
    def available(self):
        return AVAILABLE

    def start(self):
        if not AVAILABLE:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._running = True
        self._thread = threading.Thread(target=self._run, name="MediaController", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        self._poke()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        self.last_state = MediaState()

    def set_active(self, active):
        """Szybszy polling, gdy zakładka jest widoczna; wolniejszy w tle."""
        self._interval = self.POLL_ACTIVE if active else self.POLL_IDLE
        self._poke()

    def skip_previous(self):
        self._submit(self._do_previous)

    def skip_next(self):
        self._submit(self._do_next)

    def toggle_play_pause(self):
        self._submit(self._do_toggle)

    def seek(self, seconds):
        self._submit(lambda: self._do_seek(float(seconds)))

    # ------------------------------------------------------ wątek asyncio
    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._wake = asyncio.Event()
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception:
            log.exception("Pętla odtwarzacza mediów zakończyła się błędem")
        finally:
            self._loop = None
            self._wake = None
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    def _poke(self, *_args):
        """Budzi pętlę (bezpieczne z dowolnego wątku, także z callbacków WinRT)."""
        loop, wake = self._loop, self._wake
        if loop is None or wake is None:
            return
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:  # pętla już zamknięta
            pass

    async def _main(self):
        self._sent = None
        self._art = None
        self._art_key = None
        try:
            self._manager = await _Manager.request_async()
        except Exception:
            log.exception("Nie udało się uzyskać menedżera sesji multimediów")
            return
        self._hook_manager()
        try:
            while self._running:
                try:
                    await self._refresh()
                except Exception:
                    log.debug("Odświeżenie stanu nie powiodło się", exc_info=True)
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                await asyncio.sleep(0.05)  # skleja serie zdarzeń w jedno odświeżenie
        finally:
            self._unhook_session()
            self._unhook_manager()
            self._manager = None

    # --- zdarzenia SMTC (opcjonalne - polling i tak działa) ---
    def _hook_manager(self):
        self._mgr_tokens = []
        for name in ("current_session_changed", "sessions_changed"):
            try:
                token = getattr(self._manager, f"add_{name}")(self._poke)
                self._mgr_tokens.append((f"remove_{name}", token))
            except Exception:
                pass

    def _unhook_manager(self):
        for remover, token in self._mgr_tokens:
            try:
                getattr(self._manager, remover)(token)
            except Exception:
                pass
        self._mgr_tokens = []

    def _hook_session(self, session, app_id):
        if self._hooked is not None and self._hooked[1] == app_id:
            return
        self._unhook_session()
        tokens = []
        for name in ("media_properties_changed", "playback_info_changed", "timeline_properties_changed"):
            try:
                token = getattr(session, f"add_{name}")(self._poke)
                tokens.append((f"remove_{name}", token))
            except Exception:
                pass
        self._hooked = (session, app_id, tokens)

    def _unhook_session(self):
        if self._hooked is None:
            return
        session, _app_id, tokens = self._hooked
        self._hooked = None
        for remover, token in tokens:
            try:
                getattr(session, remover)(token)
            except Exception:
                pass

    # --- odczyt stanu ---
    async def _refresh(self):
        session = self._manager.get_current_session()
        if session is None:
            self._unhook_session()
            self._art_key = None
            self._art = None
            self._publish(MediaState(sampled_at=time.monotonic()))
            return

        app_id = str(session.source_app_user_model_id or "")
        self._hook_session(session, app_id)

        info = session.get_playback_info()
        controls = info.controls
        status = {4: "playing", 5: "paused"}.get(int(info.playback_status), "stopped")
        rate = 1.0
        try:
            if info.playback_rate:
                rate = float(info.playback_rate)
        except Exception:
            pass

        title = artist = album = ""
        props = None
        try:
            props = await session.try_get_media_properties_async()
        except Exception:
            pass
        if props is not None:
            title = props.title or ""
            artist = props.artist or ""
            album = props.album_title or ""

        # okładka: wczytywana tylko przy zmianie utworu (+ kilka ponowień,
        # bo np. Spotify dokłada miniaturę z opóźnieniem)
        key = (app_id, title, artist, album)
        if key != self._art_key:
            self._art_key = key
            self._art = None
            self._art_tries = 0
            self._art_id += 1
        if self._art is None and props is not None and self._art_tries < 6:
            self._art_tries += 1
            thumbnail = getattr(props, "thumbnail", None)
            if thumbnail is not None:
                data = await self._read_thumbnail(thumbnail)
                if data:
                    self._art = data
                    self._art_id += 1

        position = duration = 0.0
        try:
            timeline = session.get_timeline_properties()
            start = _secs(timeline.start_time)
            end = _secs(timeline.end_time)
            duration = max(0.0, end - start)
            position = _secs(timeline.position) - start
            if status == "playing":
                position += _age_seconds(timeline.last_updated_time) * rate
            if duration > 0:
                position = min(position, duration)
            position = max(0.0, position)
        except Exception:
            pass

        seek_supported = bool(controls.is_playback_position_enabled)
        state = MediaState(
            has_session=True,
            app_id=app_id,
            title=title,
            artist=artist,
            album=album,
            art=self._art,
            art_id=self._art_id,
            status=status,
            can_prev=bool(controls.is_previous_enabled),
            can_next=bool(controls.is_next_enabled),
            can_play_pause=bool(
                controls.is_play_pause_toggle_enabled
                or controls.is_play_enabled
                or controls.is_pause_enabled
            ),
            can_seek=seek_supported and duration > 0,
            position=position,
            duration=duration,
            rate=rate,
            sampled_at=time.monotonic(),
        )
        self._publish(state)

    async def _read_thumbnail(self, reference):
        try:
            stream = await reference.open_read_async()
            size = int(getattr(stream, "size", 0) or 0)
            capacity = min(size, MAX_ART_BYTES) if size > 0 else 2 * 1024 * 1024
            buffer = _Buffer(capacity)
            await stream.read_async(buffer, capacity, _InputStreamOptions.READ_AHEAD)
            length = int(buffer.length)
            if length <= 0:
                return None
            data = bytearray(length)
            _DataReader.from_buffer(buffer).read_bytes(data)
            try:
                stream.close()
            except Exception:
                pass
            return bytes(data)
        except Exception:
            log.debug("Nie udało się wczytać okładki", exc_info=True)
            return None

    def _publish(self, state):
        """Wysyła stan do GUI tylko, gdy coś się zmieniło albo pozycja odbiega
        od tego, co GUI sam wylicza (przewinięcie, pauza, skok)."""
        previous = self._sent
        changed = previous is None or previous.signature() != state.signature()
        if not changed:
            expected = previous.position_at(state.sampled_at)
            changed = abs(expected - state.position) > 1.2
        if changed:
            self._sent = state
            self.last_state = state
            self.state_changed.emit(state)

    # --- komendy ---
    def _submit(self, factory):
        loop = self._loop
        if loop is None or not self._running:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._guard(factory), loop)
        except Exception:
            log.debug("Nie udało się zlecić komendy", exc_info=True)

    async def _guard(self, factory):
        try:
            await factory()
        except Exception:
            log.debug("Komenda odtwarzacza nie powiodła się", exc_info=True)
        finally:
            if self._wake is not None:
                self._wake.set()
                await asyncio.sleep(0.3)  # aplikacje zmieniają stan z lekkim opóźnieniem
                self._wake.set()

    def _current(self):
        return self._manager.get_current_session() if self._manager is not None else None

    async def _do_previous(self):
        session = self._current()
        if session is not None:
            await session.try_skip_previous_async()

    async def _do_next(self):
        session = self._current()
        if session is not None:
            await session.try_skip_next_async()

    async def _do_toggle(self):
        session = self._current()
        if session is not None:
            await session.try_toggle_play_pause_async()

    async def _do_seek(self, seconds):
        session = self._current()
        if session is None:
            return
        try:
            start = _secs(session.get_timeline_properties().start_time)
        except Exception:
            start = 0.0
        ticks = int(max(0.0, seconds + start) * TICKS_PER_SECOND)
        await session.try_change_playback_position_async(ticks)


# --------------------------------------------------------------------------
# Pomocnicze UI
# --------------------------------------------------------------------------
def _qcolor(value, fallback):
    color = QColor(value)
    return color if color.isValid() else QColor(fallback)


def _font(pixel_size, bold=False):
    font = QFont()
    font.setPixelSize(pixel_size)
    font.setBold(bold)
    return font


def format_time(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


_KNOWN_APPS = (
    ("spotify", "Spotify"),
    ("msedge", "Edge"),
    ("edge", "Edge"),
    ("chrome", "Chrome"),
    ("firefox", "Firefox"),
    ("brave", "Brave"),
    ("opera", "Opera"),
    ("vlc", "VLC"),
    ("zunemusic", "Media Player"),
    ("media.player", "Media Player"),
    ("tidal", "TIDAL"),
    ("itunes", "iTunes"),
    ("applemusic", "Apple Music"),
)


def app_name(app_id):
    low = app_id.lower()
    for needle, name in _KNOWN_APPS:
        if needle in low:
            return name
    base = app_id.split("!")[0].split("_")[0].rsplit("\\", 1)[-1]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base.split(".")[-1] or app_id


class ElidedLabel(QLabel):
    """Etykieta, która skraca tekst wielokropkiem zamiast rozpychać układ."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(0)
        self.set_full_text(text)

    def set_full_text(self, text):
        self._full = text
        self.setToolTip(text)
        self._refit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refit()

    def _refit(self):
        metrics = QFontMetrics(self.font())
        self.setText(metrics.elidedText(self._full, Qt.ElideRight, max(10, self.width())))


class CoverWidget(QWidget):
    """Zaokrąglona okładka (przycięta do kwadratu) albo gradientowy placeholder."""

    def __init__(self, size, parent=None):
        super().__init__(parent)
        self._size = size
        self._pixmap = None
        self.setFixedSize(size, size)

    def set_pixmap(self, pixmap):
        """Skaluje raz (przy zmianie utworu), a nie przy każdym repaint."""
        if pixmap is None or pixmap.isNull():
            self._pixmap = None
        else:
            ratio = self.devicePixelRatioF()
            target = int(self._size * ratio)
            scaled = pixmap.scaled(
                target, target, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
            )
            x = (scaled.width() - target) // 2
            y = (scaled.height() - target) // 2
            scaled = scaled.copy(x, y, target, target)
            scaled.setDevicePixelRatio(ratio)
            self._pixmap = scaled
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        rect = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(rect, 16, 16)
        painter.setClipPath(path)

        if self._pixmap is not None:
            painter.drawPixmap(0, 0, self._pixmap)
        else:
            gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            gradient.setColorAt(0.0, QColor(BG_CARD))
            gradient.setColorAt(1.0, QColor(BG_DEEP))
            painter.fillRect(rect, gradient)
            glyph = _qcolor(VIOLET, "#8B5CF6")
            glyph.setAlpha(150)
            painter.setPen(glyph)
            font = QFont("Segoe UI Symbol")
            font.setPixelSize(int(self._size * 0.42))
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignCenter, "♪")

        painter.setClipping(False)
        painter.setPen(QPen(_qcolor(BORDER_SOFT, "#2B3050"), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 16, 16)


class IconButton(QPushButton):
    """Okrągły przycisk z rysowaną ikoną (prev / next / play / pause) -
    bez glifów czcionki, więc wygląda tak samo na każdym systemie."""

    def __init__(self, kind, diameter, primary=False, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._primary = primary
        self.setFixedSize(diameter, diameter)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_Hover, True)

    def set_kind(self, kind):
        if kind != self._kind:
            self._kind = kind
            self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        enabled = self.isEnabled()
        hover = enabled and self.underMouse()
        down = enabled and self.isDown()
        painter.setOpacity(1.0 if enabled else 0.3)

        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setPen(Qt.NoPen)
        if self._primary:
            gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            gradient.setColorAt(0.0, QColor(VIOLET))
            gradient.setColorAt(1.0, QColor(MAGENTA))
            painter.setBrush(gradient)
            painter.drawEllipse(rect)
            if hover or down:
                painter.setBrush(QColor(255, 255, 255, 25 if hover and not down else 0))
                painter.drawEllipse(rect)
            if down:
                painter.setBrush(QColor(0, 0, 0, 50))
                painter.drawEllipse(rect)
        elif hover or down:
            fill = _qcolor(VIOLET, "#8B5CF6")
            fill.setAlpha(70 if down else 40)
            painter.setBrush(fill)
            painter.drawEllipse(rect)

        painter.setBrush(_qcolor(TEXT, "#FFFFFF") if enabled else _qcolor(MUTED, "#888888"))
        cx, cy = rect.center().x(), rect.center().y()
        s = rect.width() * 0.19  # jednostka ikony

        if self._kind == "play":
            painter.drawPolygon(QPolygonF([
                QPointF(cx - 0.8 * s, cy - 1.25 * s),
                QPointF(cx - 0.8 * s, cy + 1.25 * s),
                QPointF(cx + 1.3 * s, cy),
            ]))
        elif self._kind == "pause":
            for offset in (-1.15, 0.45):
                painter.drawRoundedRect(QRectF(cx + offset * s, cy - 1.15 * s, 0.7 * s, 2.3 * s), 1.2, 1.2)
        elif self._kind == "prev":
            painter.drawRoundedRect(QRectF(cx - 1.35 * s, cy - 1.1 * s, 0.4 * s, 2.2 * s), 1.0, 1.0)
            painter.drawPolygon(QPolygonF([
                QPointF(cx + 1.3 * s, cy - 1.15 * s),
                QPointF(cx + 1.3 * s, cy + 1.15 * s),
                QPointF(cx - 0.75 * s, cy),
            ]))
        elif self._kind == "next":
            painter.drawRoundedRect(QRectF(cx + 0.95 * s, cy - 1.1 * s, 0.4 * s, 2.2 * s), 1.0, 1.0)
            painter.drawPolygon(QPolygonF([
                QPointF(cx - 1.3 * s, cy - 1.15 * s),
                QPointF(cx - 1.3 * s, cy + 1.15 * s),
                QPointF(cx + 0.75 * s, cy),
            ]))


class SeekBar(QWidget):
    """Suwak postępu rysowany ręcznie. Gdy `seekable` jest False, jest tylko
    podglądem (bez uchwytu, bez reakcji na mysz)."""

    dragChanged = Signal(float)  # 0..1, podczas przeciągania
    committed = Signal(float)    # 0..1, po puszczeniu myszy

    MARGIN = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(18)
        self.setMouseTracking(True)
        self._value = 0.0
        self._seekable = False
        self._dragging = False
        self._hover = False

    @property
    def dragging(self):
        return self._dragging

    def set_value(self, value):
        if self._dragging:
            return
        value = max(0.0, min(1.0, value))
        if abs(value - self._value) > 0.0005:
            self._value = value
            self.update()

    def set_seekable(self, seekable):
        if seekable != self._seekable:
            self._seekable = seekable
            self.setCursor(Qt.PointingHandCursor if seekable else Qt.ArrowCursor)
            self.setToolTip("" if seekable else "Ta aplikacja nie obsługuje przewijania")
            self.update()

    def _fraction(self, x):
        span = max(1.0, self.width() - 2 * self.MARGIN)
        return max(0.0, min(1.0, (x - self.MARGIN) / span))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._seekable:
            self._dragging = True
            self._value = self._fraction(event.position().x())
            self.dragChanged.emit(self._value)
            self.update()
        event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._value = self._fraction(event.position().x())
            self.dragChanged.emit(self._value)
            self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._dragging and event.button() == Qt.LeftButton:
            self._dragging = False
            self.committed.emit(self._value)
            self.update()
        event.accept()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        margin = self.MARGIN
        span = self.width() - 2 * margin
        cy = self.height() / 2
        active = self._seekable and (self._hover or self._dragging)
        thickness = 6.0 if active else 4.0

        groove = QRectF(margin, cy - thickness / 2, span, thickness)
        painter.setPen(Qt.NoPen)
        painter.setBrush(_qcolor(BORDER_SOFT, "#2B3050"))
        painter.drawRoundedRect(groove, thickness / 2, thickness / 2)

        fill_width = span * self._value
        if fill_width > 0.5:
            gradient = QLinearGradient(groove.topLeft(), groove.topRight())
            gradient.setColorAt(0.0, QColor(VIOLET))
            gradient.setColorAt(1.0, QColor(SKY))
            painter.setOpacity(1.0 if self._seekable else 0.55)
            painter.setBrush(gradient)
            painter.drawRoundedRect(
                QRectF(margin, cy - thickness / 2, fill_width, thickness),
                thickness / 2, thickness / 2,
            )
            painter.setOpacity(1.0)

        if self._seekable:
            radius = 7.0 if active else 5.0
            painter.setBrush(QColor("#FFFFFF"))
            painter.drawEllipse(QPointF(margin + fill_width, cy), radius, radius)


# --------------------------------------------------------------------------
# Panel zakładki „Muzyka"
# --------------------------------------------------------------------------
class MediaPanel(QWidget):
    """Okładka + tytuł + wykonawca + [⏮ ⏯ ⏭] + suwak postępu."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._ctl = controller
        self._state = MediaState()
        self._art_id = -1
        self._hold_until = 0.0  # chwilowe „zamrożenie" pozycji po przewinięciu

        self.cover = CoverWidget(COVER_SIZE)

        self.app_lbl = ElidedLabel("")
        self.app_lbl.setFont(_font(10, True))
        self.app_lbl.setStyleSheet(f"color: {SKY}; background: transparent;")
        self.title_lbl = ElidedLabel("")
        self.title_lbl.setFont(_font(18, True))
        self.title_lbl.setStyleSheet(f"color: {TEXT}; background: transparent;")
        self.artist_lbl = ElidedLabel("")
        self.artist_lbl.setFont(_font(13))
        self.artist_lbl.setStyleSheet(f"color: {MUTED}; background: transparent;")
        self.hint_lbl = QLabel("")
        self.hint_lbl.setWordWrap(True)
        self.hint_lbl.setFont(_font(11))
        self.hint_lbl.setStyleSheet(f"color: {MUTED}; background: transparent;")
        self.hint_lbl.setVisible(False)

        self.cur_lbl = QLabel("--:--")
        self.tot_lbl = QLabel("--:--")
        for label, align in ((self.cur_lbl, Qt.AlignLeft), (self.tot_lbl, Qt.AlignRight)):
            label.setFont(_font(11))
            label.setFixedWidth(46)
            label.setAlignment(align | Qt.AlignVCenter)
            label.setStyleSheet(f"color: {MUTED}; background: transparent;")
        self.seek = SeekBar()
        self.seek.dragChanged.connect(self._on_drag)
        self.seek.committed.connect(self._on_seek_committed)

        self.btn_prev = IconButton("prev", 38)
        self.btn_play = IconButton("play", 46, primary=True)
        self.btn_next = IconButton("next", 38)
        self.btn_prev.clicked.connect(self._ctl.skip_previous)
        self.btn_play.clicked.connect(self._ctl.toggle_play_pause)
        self.btn_next.clicked.connect(self._ctl.skip_next)

        seek_row = QHBoxLayout()
        seek_row.setSpacing(6)
        seek_row.addWidget(self.cur_lbl)
        seek_row.addWidget(self.seek, 1)
        seek_row.addWidget(self.tot_lbl)

        controls = QHBoxLayout()
        controls.setSpacing(14)
        controls.addStretch(1)
        controls.addWidget(self.btn_prev)
        controls.addWidget(self.btn_play)
        controls.addWidget(self.btn_next)
        controls.addStretch(1)

        column = QVBoxLayout()
        column.setContentsMargins(0, 2, 0, 0)
        column.setSpacing(3)
        column.addWidget(self.app_lbl)
        column.addWidget(self.title_lbl)
        column.addWidget(self.artist_lbl)
        column.addWidget(self.hint_lbl)
        column.addStretch(1)
        column.addLayout(seek_row)
        column.addLayout(controls)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(16)
        outer.addWidget(self.cover, 0, Qt.AlignTop)
        outer.addLayout(column, 1)

        # lokalne odświeżanie suwaka między próbkami z backendu
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)

        self._ctl.state_changed.connect(self.set_state)
        self.set_state(self._ctl.last_state)

    # ------------------------------------------------------ cykl życia
    def activate(self):
        self._ctl.set_active(True)
        self._timer.start()
        self._tick()

    def deactivate(self):
        self._ctl.set_active(False)
        self._timer.stop()

    def mousePressEvent(self, event):
        # kliknięcie w tło panelu nie powinno przypinać / odpinać paska
        event.accept()

    # ------------------------------------------------------ stan
    def set_state(self, state):
        if time.monotonic() < self._hold_until and state.art_id == self._state.art_id:
            # tuż po przewinięciu zachowaj naszą (optymistyczną) pozycję
            state = replace(
                state, position=self._state.position, sampled_at=self._state.sampled_at
            )
        self._state = state

        if state.art_id != self._art_id:
            self._art_id = state.art_id
            pixmap = QPixmap()
            if state.art and pixmap.loadFromData(state.art):
                self.cover.set_pixmap(pixmap)
            else:
                self.cover.set_pixmap(None)

        if not AVAILABLE:
            self.app_lbl.set_full_text("")
            self.title_lbl.set_full_text("Brak biblioteki WinRT")
            self.artist_lbl.set_full_text("")
            self.hint_lbl.setText(INSTALL_HINT)
            self.hint_lbl.setVisible(True)
        elif state.has_session:
            self.hint_lbl.setVisible(False)
            self.app_lbl.set_full_text(app_name(state.app_id).upper())
            self.title_lbl.set_full_text(state.title or "Nieznany utwór")
            self.artist_lbl.set_full_text(state.artist or state.album)
        else:
            self.hint_lbl.setVisible(False)
            self.app_lbl.set_full_text("")
            self.title_lbl.set_full_text("Nic nie jest odtwarzane")
            self.artist_lbl.set_full_text("Uruchom Spotify, YouTube lub VLC")

        self.btn_prev.setEnabled(state.can_prev)
        self.btn_next.setEnabled(state.can_next)
        self.btn_play.setEnabled(state.can_play_pause)
        self.btn_play.set_kind("pause" if state.is_playing else "play")
        self.seek.set_seekable(state.can_seek)
        self._tick()

    def _tick(self):
        if self.seek.dragging:
            return
        state = self._state
        if not state.has_session:
            self.seek.set_value(0.0)
            self.cur_lbl.setText("--:--")
            self.tot_lbl.setText("--:--")
            return
        position = state.position_at(time.monotonic())
        duration = state.duration
        self.seek.set_value(position / duration if duration > 0 else 0.0)
        self.cur_lbl.setText(format_time(position))
        self.tot_lbl.setText(format_time(duration) if duration > 0 else "--:--")

    # ------------------------------------------------------ suwak
    def _on_drag(self, fraction):
        self.cur_lbl.setText(format_time(fraction * self._state.duration))

    def _on_seek_committed(self, fraction):
        state = self._state
        if not state.can_seek or state.duration <= 0:
            return
        target = fraction * state.duration
        self._ctl.seek(target)
        self._state = replace(state, position=target, sampled_at=time.monotonic())
        self._hold_until = time.monotonic() + 1.2
        self._tick()
