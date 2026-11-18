"""clipboard_manager.py - historia schowka dla DynamicBar (PySide6).

Elementy:
  * ClipEntry         - jeden wpis historii: tekst albo lista plików/folderów.
  * ClipboardHistory  - obserwuje QClipboard (sygnał dataChanged), trzyma
                        ostatnie MAX_ITEMS wpisów (najnowszy na górze) i umie
                        przywrócić wpis do schowka systemowego. Należy do
                        DynamicBar, więc przeżywa przebudowy zakładek.
  * ClipboardPanel    - zawartość zakładki „Schowek": lista (ikona + nazwa),
                        komunikat „Skopiowano!" i przycisk „Wyczyść historię".

Uwagi:
  * Pliki/foldery skopiowane w Eksploratorze Windows przychodzą jako lista URL
    (CF_HDROP) - pokazujemy ich nazwy i systemowe ikony (QFileIconProvider).
  * Zawartość oznaczona przez menedżery haseł jako wyłączona z monitorowania
    (ExcludeClipboardContentFromMonitorProcessing) nie trafia do historii.
  * Historia jest tylko w pamięci - nic nie jest zapisywane na dysk.
"""

from dataclasses import dataclass

from PySide6.QtCore import (
    QByteArray,
    QDir,
    QFileInfo,
    QMimeData,
    QObject,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileIconProvider,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from widgets import BG_CARD, BORDER_SOFT, MAGENTA, MUTED, SKY, TEXT, VIOLET

MAX_ITEMS = 15
MAX_TEXT_CHARS = 200_000   # dłuższych tekstów nie trzymamy w historii
PREVIEW_CHARS = 90         # tyle znaków tekstu widać na liście
TOAST_MS = 1500            # czas wyświetlania „Skopiowano!"
ICON_SIZE = 20

# Formaty, którymi menedżery haseł proszą, by nie zapisywać zawartości.
_EXCLUDE_FORMATS = (
    "ExcludeClipboardContentFromMonitorProcessing",
    'application/x-qt-windows-mime;value="ExcludeClipboardContentFromMonitorProcessing"',
)
_DROP_EFFECT_FORMAT = 'application/x-qt-windows-mime;value="Preferred DropEffect"'
_DROP_EFFECT_COPY = QByteArray(b"\x01\x00\x00\x00")


@dataclass(frozen=True)
class ClipEntry:
    kind: str                 # "text" albo "files"
    text: str = ""
    paths: tuple = ()         # ścieżki w zapisie natywnym (C:\...)

    def display_name(self):
        if self.kind == "files":
            first = QFileInfo(self.paths[0]).fileName() or self.paths[0]
            extra = len(self.paths) - 1
            return f"{first}  (+{extra})" if extra > 0 else first
        flat = " ".join(self.text.split())
        if len(flat) > PREVIEW_CHARS:
            flat = flat[: PREVIEW_CHARS - 1] + "…"
        return flat

    def tooltip(self):
        if self.kind == "files":
            return "\n".join(self.paths[:20]) + ("\n…" if len(self.paths) > 20 else "")
        return self.text[:600] + ("…" if len(self.text) > 600 else "")


# --------------------------------------------------------------------------
# Historia + monitor schowka
# --------------------------------------------------------------------------
class ClipboardHistory(QObject):
    changed = Signal()  # historia się zmieniła (nowy wpis, czyszczenie)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []          # najnowszy na początku
        self._clipboard = None
        self._running = False
        self._expect = None       # wpis, który sami przed chwilą wstawiliśmy
        self._retries = 0

        # Windows potrafi zgłosić zmianę kilka razy pod rząd -> krótki debounce
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._capture)

        self._expect_timer = QTimer(self)
        self._expect_timer.setSingleShot(True)
        self._expect_timer.setInterval(1500)
        self._expect_timer.timeout.connect(self._forget_expect)

    # ---------------------------------------------------------- cykl życia
    def start(self):
        if self._running:
            return
        self._clipboard = QGuiApplication.clipboard()
        self._clipboard.dataChanged.connect(self._schedule_capture)
        self._running = True
        self._capture()  # zapamiętaj to, co jest w schowku już teraz

    def stop(self):
        if not self._running:
            return
        self._debounce.stop()
        self._expect_timer.stop()
        try:
            self._clipboard.dataChanged.disconnect(self._schedule_capture)
        except (RuntimeError, TypeError):
            pass
        self._running = False

    # ---------------------------------------------------------- dostęp
    def items(self):
        return list(self._items)

    def clear(self):
        self._items.clear()
        self.changed.emit()

    # ---------------------------------------------------------- przechwytywanie
    def _schedule_capture(self):
        self._retries = 0
        self._debounce.start(40)

    def _forget_expect(self):
        self._expect = None

    def _capture(self):
        mime = self._clipboard.mimeData()
        # inna aplikacja może chwilowo blokować schowek -> kilka ponowień
        if (mime is None or not mime.formats()) and self._retries < 3:
            self._retries += 1
            self._debounce.start(100)
            return
        self._retries = 0

        entry = self._read(mime)
        if entry is None:
            return
        if entry == self._expect:
            # to my przywróciliśmy wpis z listy - bez zmiany kolejności
            self._expect = None
            return
        self._push(entry)

    @staticmethod
    def _read(mime):
        if mime is None:
            return None
        for fmt in _EXCLUDE_FORMATS:
            if mime.hasFormat(fmt):
                return None

        if mime.hasUrls():
            paths = tuple(
                QDir.toNativeSeparators(url.toLocalFile())
                for url in mime.urls()
                if url.isLocalFile() and url.toLocalFile()
            )
            if paths:
                return ClipEntry("files", paths=paths)

        if mime.hasText():
            text = mime.text()
            if text.strip() and len(text) <= MAX_TEXT_CHARS:
                return ClipEntry("text", text=text)
        return None

    def _push(self, entry):
        if entry in self._items:
            self._items.remove(entry)  # duplikat wędruje na górę
        self._items.insert(0, entry)
        del self._items[MAX_ITEMS:]
        self.changed.emit()

    # ---------------------------------------------------------- przywracanie
    def restore(self, entry):
        """Wstawia wpis z powrotem do schowka systemowego."""
        if self._clipboard is None:
            self._clipboard = QGuiApplication.clipboard()
        mime = QMimeData()
        if entry.kind == "files":
            mime.setUrls([QUrl.fromLocalFile(path) for path in entry.paths])
            mime.setData(_DROP_EFFECT_FORMAT, _DROP_EFFECT_COPY)  # wklej jako kopię
        else:
            mime.setText(entry.text)
        self._expect = entry
        self._expect_timer.start()
        self._clipboard.setMimeData(mime)


# --------------------------------------------------------------------------
# Panel (zakładka „Schowek")
# --------------------------------------------------------------------------
PANEL_QSS = f"""
QLabel#ClipTitle {{ color: {MUTED}; font-size: 10px; font-weight: 700; background: transparent; }}
QLabel#ClipToast {{ color: {SKY}; font-size: 12px; font-weight: 700; background: transparent; }}
QPushButton#ClipClear {{
    color: {MUTED}; background: transparent; border: 1px solid {BORDER_SOFT};
    border-radius: 11px; padding: 4px 12px; font-size: 11px;
}}
QPushButton#ClipClear:hover {{ color: #FFFFFF; border-color: {MAGENTA}; background: rgba(192, 38, 211, 0.25); }}
QPushButton#ClipClear:disabled {{ color: {BORDER_SOFT}; border-color: {BORDER_SOFT}; background: transparent; }}
QListWidget#ClipList {{
    background: {BG_CARD}; border: 1px solid {BORDER_SOFT}; border-radius: 12px;
    padding: 4px; outline: 0; color: {TEXT}; font-size: 12px;
}}
QListWidget#ClipList::item {{ padding: 4px 8px; border-radius: 8px; }}
QListWidget#ClipList::item:hover {{ background: rgba(139, 92, 246, 0.20); }}
QListWidget#ClipList::item:selected {{ background: rgba(139, 92, 246, 0.35); color: #FFFFFF; }}
QListWidget#ClipList::item:disabled {{ color: {MUTED}; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER_SOFT}; border-radius: 4px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {VIOLET}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""


def _text_icon():
    """Mała ikona „T" dla wpisów tekstowych."""
    pixmap = QPixmap(ICON_SIZE, ICON_SIZE)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(VIOLET))
    painter.drawRoundedRect(1, 1, ICON_SIZE - 2, ICON_SIZE - 2, 5, 5)
    painter.setPen(QColor("#FFFFFF"))
    font = QFont()
    font.setPixelSize(13)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "T")
    painter.end()
    return QIcon(pixmap)


class ClipboardPanel(QWidget):
    LIST_HEIGHT = 168  # mieści się w rozwiniętym pasku (EXPANDED_SIZE w main.py)

    def __init__(self, history, parent=None):
        super().__init__(parent)
        self.history = history
        self._icons = QFileIconProvider()
        self._icon_cache = {}
        self._text_icon = _text_icon()
        self.setStyleSheet(PANEL_QSS)

        self.title = QLabel("")
        self.title.setObjectName("ClipTitle")
        self.toast = QLabel("")
        self.toast.setObjectName("ClipToast")
        self.toast.setMinimumWidth(90)  # stała szerokość - układ nie skacze
        self.toast.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.clear_btn = QPushButton("Wyczyść historię")
        self.clear_btn.setObjectName("ClipClear")
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.setFocusPolicy(Qt.NoFocus)
        self.clear_btn.clicked.connect(self._on_clear)

        top = QHBoxLayout()
        top.setContentsMargins(2, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(self.title)
        top.addStretch(1)
        top.addWidget(self.toast)
        top.addWidget(self.clear_btn)

        self.list = QListWidget()
        self.list.setObjectName("ClipList")
        self.list.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setFixedHeight(self.LIST_HEIGHT)
        self.list.itemClicked.connect(self._on_item_clicked)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(top)
        layout.addWidget(self.list)

        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(lambda: self.toast.setText(""))

        self.history.changed.connect(self.refresh)
        self.refresh()

    # ---------------------------------------------------------- lista
    def _icon_for(self, entry):
        if entry.kind != "files":
            return self._text_icon
        path = entry.paths[0]
        icon = self._icon_cache.get(path)
        if icon is None:
            icon = self._icons.icon(QFileInfo(path))
            self._icon_cache[path] = icon
            if len(self._icon_cache) > 64:  # nie rośnij bez końca
                self._icon_cache.pop(next(iter(self._icon_cache)))
        return icon

    def refresh(self):
        entries = self.history.items()
        self.list.clear()
        for entry in entries:
            item = QListWidgetItem(self._icon_for(entry), entry.display_name())
            item.setToolTip(entry.tooltip())
            self.list.addItem(item)
        if not entries:
            placeholder = QListWidgetItem("Historia jest pusta - skopiuj tekst lub pliki (Ctrl+C)")
            placeholder.setFlags(Qt.NoItemFlags)
            self.list.addItem(placeholder)
        self.clear_btn.setEnabled(bool(entries))
        self.title.setText(f"HISTORIA SCHOWKA  ·  {len(entries)}/{MAX_ITEMS}")

    # ---------------------------------------------------------- akcje
    def _show_toast(self, text):
        self.toast.setText(text)
        self._toast_timer.start(TOAST_MS)

    def _on_item_clicked(self, item):
        entries = self.history.items()
        row = self.list.row(item)
        if not (0 <= row < len(entries)):
            return
        entry = entries[row]
        if entry.kind == "files" and not any(QFileInfo(p).exists() for p in entry.paths):
            self._show_toast("Pliku już nie ma")
            return
        self.history.restore(entry)
        self._show_toast("Skopiowano!")
        self.list.clearSelection()

    def _on_clear(self):
        self.history.clear()
        self._show_toast("Wyczyszczono")
