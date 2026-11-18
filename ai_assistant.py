"""Asystent AI (Google Gemini) dla DynamicBar.

Zawartość zakładki „Gemini" w rozwiniętym pasku:
  * AIAssistantPanel - czat: pole pytania (Enter wysyła), fioletowo-niebieski
                       pasek ładowania, przewijana odpowiedź (Markdown) oraz
                       przyciski „Kopiuj odpowiedź" i „Nowy czat".
                       Bez klucza API pokazuje kartę z przyciskiem otwierającym
                       okno Ustawień.
  * GeminiWorker     - QThread wysyłający zapytanie do API Gemini (REST,
                       wyłącznie biblioteka standardowa: urllib - bez
                       dodatkowych zależności).

Klucz API pochodzi z config.json (gemini_api_key); panel dostaje go od
modułu w main.py i odświeża po każdej zmianie konfiguracji.
"""

import html
import json
import math
import socket
import time
import urllib.error
import urllib.request

from PySide6.QtCore import QEvent, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPainterPath,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStackedLayout,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from widgets import BG_CARD, BG_DEEP, BORDER_SOFT, MUTED, SKY, TEXT, VIOLET

# --------------------------------------------------------------------------
# Ustawienia API
# --------------------------------------------------------------------------
# "gemini-flash-latest" to alias zawsze wskazujący aktualny model Flash.
# Jeśli wolisz konkretny model, wpisz tu np. "gemini-2.5-flash".
GEMINI_MODEL = "gemini-flash-latest"
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
REQUEST_TIMEOUT = 45          # sekundy
MAX_HISTORY_MESSAGES = 8      # ile ostatnich wiadomości (pytania + odpowiedzi) wysyłać jako kontekst
STATUS_WIDTH = 330            # maks. szerokość tekstu statusu (px)
PANEL_HEIGHT = 212

AI_VIOLET = "#8B5CF6"
AI_BLUE = "#3B82F6"

SYSTEM_PROMPT = (
    "Jesteś asystentem wbudowanym w mały, rozwijany pasek DynamicBar na pulpicie. "
    "Odpowiadaj po polsku (chyba że użytkownik pisze w innym języku), zwięźle i konkretnie. "
    "Możesz używać prostego Markdownu: krótkie akapity, listy, `kod` tam, gdzie to potrzebne."
)


# --------------------------------------------------------------------------
# Komunikacja z Gemini
# --------------------------------------------------------------------------
class GeminiError(Exception):
    """Błąd z czytelnym, polskim komunikatem dla użytkownika."""


def _http_error_text(code, body):
    detail = ""
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
        detail = str(parsed.get("error", {}).get("message", ""))[:200]
    except Exception:
        pass

    if code in (401, 403) or (code == 400 and "API key" in detail):
        return "Klucz API został odrzucony. Sprawdź go w ustawieniach."
    if code == 400:
        return "Nieprawidłowe zapytanie. " + detail
    if code == 404:
        return f"Model „{GEMINI_MODEL}” jest niedostępny dla tego klucza."
    if code == 429:
        return "Przekroczono limit zapytań Gemini. Spróbuj ponownie za chwilę."
    if code >= 500:
        return "Usługa Gemini jest chwilowo niedostępna. Spróbuj ponownie za chwilę."
    return f"Błąd serwera ({code}). {detail}".strip()


def ask_gemini(api_key, contents, model=GEMINI_MODEL, timeout=REQUEST_TIMEOUT):
    """Wysyła rozmowę do Gemini i zwraca tekst odpowiedzi.
    Rzuca GeminiError z polskim komunikatem w razie problemów."""
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.7},
    }
    request = urllib.request.Request(
        API_URL.format(model=model),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            # klucz w nagłówku, a nie w adresie URL - nie trafi do komunikatów błędów
            "x-goog-api-key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        raise GeminiError(_http_error_text(exc.code, body)) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise GeminiError("Upłynął limit czasu odpowiedzi. Spróbuj ponownie.") from None
        raise GeminiError("Brak połączenia z internetem.") from None
    except (socket.timeout, TimeoutError):
        raise GeminiError("Upłynął limit czasu odpowiedzi. Spróbuj ponownie.") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise GeminiError("Serwer zwrócił nieprawidłową odpowiedź.") from None
    except OSError:
        raise GeminiError("Błąd połączenia z serwerem Gemini.") from None

    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        if reason:
            raise GeminiError("Zapytanie zostało zablokowane przez filtry bezpieczeństwa Gemini.")
        raise GeminiError("Gemini nie zwróciło odpowiedzi.")

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and not part.get("thought")
    ).strip()
    if not text:
        if candidate.get("finishReason") in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST"):
            raise GeminiError("Odpowiedź została zablokowana przez filtry bezpieczeństwa Gemini.")
        raise GeminiError("Gemini zwróciło pustą odpowiedź.")
    return text


# Wątki żyją niezależnie od panelu (panel może zostać przebudowany w trakcie
# zapytania), więc trzymamy je tutaj aż do zakończenia.
_ACTIVE_WORKERS = set()


class GeminiWorker(QThread):
    """Wysyła jedno zapytanie do Gemini w osobnym wątku."""

    succeeded = Signal(int, str)  # (id zapytania, tekst odpowiedzi)
    failed = Signal(int, str)     # (id zapytania, komunikat błędu)

    def __init__(self, request_id, api_key, contents):
        super().__init__()
        self._request_id = request_id
        self._api_key = api_key
        self._contents = contents
        _ACTIVE_WORKERS.add(self)
        self.finished.connect(self._cleanup)

    def run(self):
        try:
            text = ask_gemini(self._api_key, self._contents)
        except GeminiError as exc:
            self.failed.emit(self._request_id, str(exc))
        except Exception as exc:  # nieoczekiwany błąd nie może zabić aplikacji
            self.failed.emit(self._request_id, f"Nieoczekiwany błąd: {exc}")
        else:
            self.succeeded.emit(self._request_id, text)

    def _cleanup(self):
        _ACTIVE_WORKERS.discard(self)
        self.deleteLater()


def shutdown_workers():
    """Zatrzymuje trwające zapytania przy zamykaniu programu."""
    for worker in list(_ACTIVE_WORKERS):
        worker.requestInterruption()
        if not worker.wait(500):
            worker.terminate()
            worker.wait(500)


# --------------------------------------------------------------------------
# Animacja ładowania
# --------------------------------------------------------------------------
class LoadingBar(QWidget):
    """Cienki pasek z fioletowo-niebieskim „przelewającym się" gradientem."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(4)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._active = False
        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self.update)

    def set_active(self, active):
        self._active = bool(active)
        if self._active:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = QRectF(self.rect())
        radius = rect.height() / 2
        track = QPainterPath()
        track.addRoundedRect(rect, radius, radius)
        painter.fillPath(track, QColor(255, 255, 255, 18))
        if not self._active:
            return

        painter.setClipPath(track)
        phase = (time.monotonic() * 0.9) % 2.0
        pos = phase if phase <= 1.0 else 2.0 - phase
        eased = 0.5 - 0.5 * math.cos(math.pi * pos)

        seg = rect.width() * 0.4
        x = -seg / 2 + eased * rect.width()
        gradient = QLinearGradient(x, 0, x + seg, 0)
        violet_clear, violet = QColor(AI_VIOLET), QColor(AI_VIOLET)
        blue, blue_clear = QColor(AI_BLUE), QColor(AI_BLUE)
        violet_clear.setAlpha(0)
        blue_clear.setAlpha(0)
        gradient.setColorAt(0.0, violet_clear)
        gradient.setColorAt(0.3, violet)
        gradient.setColorAt(0.7, blue)
        gradient.setColorAt(1.0, blue_clear)
        painter.fillRect(QRectF(x, 0, seg, rect.height()), gradient)


# --------------------------------------------------------------------------
# Panel czatu
# --------------------------------------------------------------------------
PANEL_QSS = f"""
QTextBrowser#AiView {{
    background: {BG_CARD}; color: {TEXT}; border: 1px solid {BORDER_SOFT};
    border-radius: 12px; padding: 6px 8px; font-size: 13px;
    selection-background-color: rgba(139, 92, 246, 0.55);
}}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 6px 2px 6px 0; }}
QScrollBar::handle:vertical {{
    background: rgba(139, 92, 246, 0.55); border-radius: 3px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {AI_BLUE}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QLineEdit#AiInput {{
    background: {BG_DEEP}; color: {TEXT}; border: 1px solid {BORDER_SOFT};
    border-radius: 14px; padding: 6px 12px; font-size: 13px;
    selection-background-color: rgba(139, 92, 246, 0.55);
}}
QLineEdit#AiInput:focus {{ border: 1px solid {AI_VIOLET}; }}
QLabel#AiStatus {{ color: {MUTED}; font-size: 11px; background: transparent; }}
QPushButton#AiBtn {{
    color: {MUTED}; background: transparent; border: 1px solid {BORDER_SOFT};
    border-radius: 11px; padding: 3px 10px; font-size: 11px;
}}
QPushButton#AiBtn:hover {{ color: #FFFFFF; border-color: {SKY}; }}
QPushButton#AiBtn:disabled {{ color: rgba(255, 255, 255, 0.25); border-color: rgba(255, 255, 255, 0.08); }}
QPushButton#AiPrimary {{
    color: #FFFFFF; border: none; border-radius: 14px; padding: 6px 16px;
    font-size: 12px; font-weight: 700;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {AI_VIOLET}, stop:1 {AI_BLUE});
}}
QPushButton#AiPrimary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #A78BFA, stop:1 #60A5FA);
}}
QPushButton#AiPrimary:disabled {{ background: rgba(255, 255, 255, 0.08); color: {MUTED}; }}
QFrame#AiSetupCard {{
    background: {BG_CARD}; border: 1px solid {BORDER_SOFT}; border-radius: 16px;
}}
QLabel#AiSetupIcon {{ color: {AI_VIOLET}; font-size: 26px; background: transparent; }}
QLabel#AiSetupTitle {{ color: {TEXT}; font-size: 15px; font-weight: 700; background: transparent; }}
QLabel#AiSetupText {{ color: {MUTED}; font-size: 12px; background: transparent; }}
"""


class AIAssistantPanel(QWidget):
    """Czat z Gemini do osadzenia w zakładce paska.

    api_key        - klucz z config.json (może być pusty),
    open_settings  - funkcja otwierająca okno Ustawień (przycisk w karcie braku klucza).
    """

    focus_released = Signal()  # pole tekstowe straciło fokus / zapytanie się skończyło

    def __init__(self, api_key="", open_settings=None, parent=None):
        super().__init__(parent)
        self._api_key = (api_key or "").strip()
        self._open_settings = open_settings
        self._history = []          # [{"role": "user"|"model", "parts": [{"text": ...}]}]
        self._request_id = 0
        self._worker = None
        self._loading = False
        self._last_answer = ""
        self._question = ""

        self.setObjectName("AiPanel")
        self.setFixedHeight(PANEL_HEIGHT)
        self.setStyleSheet(PANEL_QSS)

        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.timeout.connect(self._reset_copy_label)

        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(self._build_setup_page())
        self._stack.addWidget(self._build_chat_page())
        self._refresh_page()

    # ---------------------------------------------------------- budowa UI
    def _build_setup_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QFrame()
        card.setObjectName("AiSetupCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 14, 24, 14)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignCenter)

        icon = QLabel("✦")
        icon.setObjectName("AiSetupIcon")
        icon.setAlignment(Qt.AlignCenter)
        title = QLabel("Połącz asystenta Gemini")
        title.setObjectName("AiSetupTitle")
        title.setAlignment(Qt.AlignCenter)
        text = QLabel(
            "Aby zadawać pytania bezpośrednio z paska,\n"
            "wpisz swój klucz API Gemini w ustawieniach."
        )
        text.setObjectName("AiSetupText")
        text.setAlignment(Qt.AlignCenter)

        button = QPushButton("⚙  Otwórz ustawienia")
        button.setObjectName("AiPrimary")
        button.setCursor(Qt.PointingHandCursor)
        button.setFocusPolicy(Qt.NoFocus)
        button.clicked.connect(self._on_open_settings)

        link = QLabel(
            f'Darmowy klucz: <a href="https://aistudio.google.com/apikey" '
            f'style="color:{SKY};">aistudio.google.com/apikey</a>'
        )
        link.setObjectName("AiSetupText")
        link.setTextFormat(Qt.RichText)
        link.setOpenExternalLinks(True)
        link.setAlignment(Qt.AlignCenter)

        layout.addWidget(icon)
        layout.addWidget(title)
        layout.addWidget(text)
        layout.addSpacing(4)
        layout.addWidget(button, 0, Qt.AlignHCenter)
        layout.addWidget(link)
        outer.addWidget(card)
        return page

    def _build_chat_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.view = QTextBrowser()
        self.view.setObjectName("AiView")
        self.view.setOpenExternalLinks(True)
        self.view.setPlaceholderText("Zapytaj Gemini o cokolwiek…\nEnter wysyła pytanie.")
        layout.addWidget(self.view, 1)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.status = QLabel("✦ Gemini")
        self.status.setObjectName("AiStatus")
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.copy_btn = QPushButton("Kopiuj odpowiedź")
        self.copy_btn.setObjectName("AiBtn")
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        self.copy_btn.setFocusPolicy(Qt.NoFocus)
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._copy_answer)
        self.clear_btn = QPushButton("Nowy czat")
        self.clear_btn.setObjectName("AiBtn")
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.setFocusPolicy(Qt.NoFocus)
        self.clear_btn.clicked.connect(self._new_chat)
        actions.addWidget(self.status, 1)
        actions.addWidget(self.copy_btn)
        actions.addWidget(self.clear_btn)
        layout.addLayout(actions)

        self.loader = LoadingBar()
        layout.addWidget(self.loader)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.input = QLineEdit()
        self.input.setObjectName("AiInput")
        self.input.setPlaceholderText("Napisz pytanie i naciśnij Enter…")
        self.input.returnPressed.connect(self._send)
        self.input.installEventFilter(self)
        self.send_btn = QPushButton("Wyślij")
        self.send_btn.setObjectName("AiPrimary")
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setFocusPolicy(Qt.NoFocus)
        self.send_btn.clicked.connect(self._send)
        row.addWidget(self.input, 1)
        row.addWidget(self.send_btn)
        layout.addLayout(row)
        return page

    # ---------------------------------------------------------- API panelu
    def set_api_key(self, api_key):
        """Wywoływane przy zmianie konfiguracji; przełącza kartę „brak klucza"."""
        self._api_key = (api_key or "").strip()
        self._refresh_page()

    def is_loading(self):
        return self._loading

    def input_has_focus(self):
        return self.input.hasFocus()

    def focus_input(self):
        if self._api_key:
            self.input.setFocus()

    # ---------------------------------------------------------- zdarzenia
    def eventFilter(self, obj, event):
        if obj is self.input:
            if event.type() == QEvent.MouseButtonPress:
                # okno typu Tool nie aktywuje się samo - bez tego nie da się pisać
                self.window().activateWindow()
                self.input.setFocus()
            elif event.type() == QEvent.FocusOut:
                QTimer.singleShot(0, self.focus_released.emit)
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event):
        # kliknięcie w tło panelu nie powinno przypinać / odpinać paska
        event.accept()

    # ---------------------------------------------------------- logika
    def _refresh_page(self):
        self._stack.setCurrentIndex(1 if self._api_key else 0)

    def _on_open_settings(self):
        if callable(self._open_settings):
            self._open_settings()

    def _elide_status(self, text):
        text = " ".join(text.split())
        return QFontMetrics(self.status.font()).elidedText(text, Qt.ElideRight, STATUS_WIDTH)

    def _set_loading(self, loading):
        self._loading = bool(loading)
        self.loader.set_active(self._loading)
        self.send_btn.setEnabled(not self._loading)

    def _send(self):
        if self._loading:
            return
        question = self.input.text().strip()
        if not question:
            return
        if not self._api_key:
            self._refresh_page()
            return

        self.input.clear()
        self._question = question
        self._request_id += 1
        contents = self._history + [{"role": "user", "parts": [{"text": question}]}]

        self._last_answer = ""
        self.copy_btn.setEnabled(False)
        self._reset_copy_label()
        self.view.setHtml(f'<p style="color:{MUTED};">Gemini pisze odpowiedź…</p>')
        self.status.setText(self._elide_status("✦ Myślę… · " + question))
        self._set_loading(True)

        worker = GeminiWorker(self._request_id, self._api_key, contents)
        worker.succeeded.connect(self._on_success)
        worker.failed.connect(self._on_failure)
        self._worker = worker
        worker.start()

    def _on_success(self, request_id, text):
        if request_id != self._request_id:
            return  # odpowiedź na unieważnione zapytanie (np. po „Nowy czat")
        self._history.append({"role": "user", "parts": [{"text": self._question}]})
        self._history.append({"role": "model", "parts": [{"text": text}]})
        del self._history[:-MAX_HISTORY_MESSAGES]

        self._last_answer = text
        self.view.setMarkdown(text)
        self.view.verticalScrollBar().setValue(0)
        self.copy_btn.setEnabled(True)
        self.status.setText(self._elide_status("✦ " + self._question))
        self._set_loading(False)
        self.focus_released.emit()

    def _on_failure(self, request_id, message):
        if request_id != self._request_id:
            return
        self._last_answer = ""
        self.view.setHtml(
            '<p style="color:#F87171;"><b>Nie udało się uzyskać odpowiedzi</b></p>'
            f'<p style="color:{TEXT};">{html.escape(message)}</p>'
        )
        self.copy_btn.setEnabled(False)
        self.status.setText("✦ Błąd")
        self.input.setText(self._question)  # można od razu spróbować ponownie
        self._set_loading(False)
        self.focus_released.emit()

    def _copy_answer(self):
        if not self._last_answer:
            return
        QGuiApplication.clipboard().setText(self._last_answer)
        self.copy_btn.setText("Skopiowano ✓")
        self._copy_timer.start(1500)

    def _reset_copy_label(self):
        self.copy_btn.setText("Kopiuj odpowiedź")

    def _new_chat(self):
        self._request_id += 1  # unieważnia ewentualne trwające zapytanie
        self._history.clear()
        self._last_answer = ""
        self._question = ""
        self.view.clear()
        self.copy_btn.setEnabled(False)
        self._reset_copy_label()
        self._set_loading(False)
        self.status.setText("✦ Gemini · nowy czat")
        self.input.setFocus()


if __name__ == "__main__":
    # Szybki test samego panelu, bez całego paska.
    import sys

    import config
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    panel = AIAssistantPanel(
        config.get_setting("gemini_api_key", ""),
        open_settings=lambda: print("Tu otworzyłoby się okno ustawień"),
    )
    panel.setStyleSheet(PANEL_QSS + f"QWidget#AiPanel {{ background: {BG_DEEP}; }}")
    panel.setFixedWidth(584)
    panel.show()
    code = app.exec()
    shutdown_workers()
    sys.exit(code)
