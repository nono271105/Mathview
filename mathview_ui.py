"""
MathView — interface.

Ce module ne contient que l'affichage : fenêtre principale, thème, aperçu KaTeX
et overlay de capture d'écran. Aucune logique OCR : la fenêtre expose des
signaux (actions de l'utilisateur) et des méthodes (ordres d'affichage) que le
contrôleur de `mathview_app.py` relie à la logique métier.

Direction visuelle : un bureau en ardoise sur lequel est posée une feuille à
petits carreaux. La formule y est composée à l'encre ; le surligneur jaune ne
sert qu'à marquer ce qui est actif (format choisi, sélection, focus).
"""

import sys
import json
import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QTimer, QUrl, QRect, QRectF, QPointF, QObject
from PySide6.QtGui import (
    QGuiApplication, QCursor, QColor, QPainter, QPen, QFont,
    QKeySequence, QShortcut, QPixmap, QImage, QPainterPath, QIcon
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTextEdit, QLabel, QCheckBox, QFrame,
    QButtonGroup, QSystemTrayIcon, QMenu
)
from PySide6.QtWebEngineWidgets import QWebEngineView

RESOURCES_DIR = Path(__file__).resolve().parent / "resources"

# Raccourci de capture. Sous macOS, « Meta » désigne la touche Contrôle de Qt.
SNIP_SHORTCUT_LABEL = "⌃⌥S"
SNIP_SHORTCUT_QT = "Meta+Alt+S"


def float_over_fullscreen(widget: QWidget, level: int | None = None):
    """
    macOS : autorise la fenêtre à apparaître sur l'espace actif, y compris au-dessus
    d'une application en plein écran (Chrome, par exemple), sans changer d'espace.
    Nécessite pyobjc (pip install pyobjc-framework-Cocoa) ; ignoré sinon.
    """
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        import objc  # type: ignore
        view = objc.objc_object(c_void_p=ctypes.c_void_p(int(widget.winId())))
        window = view.window()
        if level is not None:
            window.setLevel_(level)
        # MoveToActiveSpace (1 << 1) | FullScreenAuxiliary (1 << 8)
        window.setCollectionBehavior_((1 << 1) | (1 << 8))
    except Exception as err:
        logging.debug(f"float_over_fullscreen ignoré: {err}")


def build_menu_bar_icon() -> QIcon:
    """Icône monochrome « template » : macOS l'adapte au thème clair/sombre."""
    size, ratio = 22, 2.0
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.setPen(QColor("black"))
    font = QFont("Helvetica Neue")
    font.setPixelSize(19)
    font.setWeight(QFont.Weight.DemiBold)
    painter.setFont(font)
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, "Σ")
    painter.end()

    icon = QIcon(pixmap)
    icon.setIsMask(True)
    return icon


# --------------------------------------------------------------------------- #
#  Design tokens
# --------------------------------------------------------------------------- #
class Palette:
    # Le bureau
    DESK = "#262D36"          # ardoise, fond de fenêtre
    RAISED = "#2F3742"        # panneaux et survols
    WELL = "#1F252D"          # zone de saisie en creux
    LINE = "#414B59"          # filets
    TEXT = "#E8ECF3"
    MUTED = "#9DA9BA"
    DISABLED = "#6B7686"
    # La feuille
    PAPER = "#F5F7FC"
    PAPER_PRESSED = "#E1E6F4"
    GRID = "#E0E6F5"
    INK = "#1D2B63"
    INK_MUTED = "#5F6B98"
    INK_DARK = "#1F2430"      # texte posé sur le surligneur
    # L'unique accent : le surligneur
    HIGHLIGHT = "#F5D547"
    # États (sémantique uniquement)
    OK = "#7CCBA2"
    BUSY = "#9DB4F5"
    WARN = "#E9B44C"
    BAD = "#EE8B83"

    SANS = '"SF Pro Text", "Helvetica Neue", "Segoe UI", sans-serif'
    SERIF = '"Georgia", "Times New Roman", serif'
    MONO = '"SF Mono", Menlo, Consolas, monospace'


def _tokens() -> dict:
    """Jetons utilisables dans les gabarits (@NOM@ et @NOM_RGB@ pour les couleurs)."""
    tokens = {}
    for name, value in vars(Palette).items():
        if name.startswith("_"):
            continue
        tokens[name] = value
        if isinstance(value, str) and value.startswith("#") and len(value) == 7:
            r, g, b = (int(value[i:i + 2], 16) for i in (1, 3, 5))
            tokens[f"{name}_RGB"] = f"{r}, {g}, {b}"
    return tokens


def _render(template: str) -> str:
    for name, value in sorted(_tokens().items(), key=lambda kv: -len(kv[0])):
        template = template.replace(f"@{name}@", value)
    return template


STYLESHEET = """
QMainWindow { background-color: @DESK@; }
QWidget { color: @TEXT@; font-family: @SANS@; font-size: 13px; }
QToolTip { background-color: @PAPER@; color: @INK@; border: none; padding: 4px 8px; }

#appTitle {
    font-family: @SERIF@;
    font-size: 24px;
    font-weight: 600;
    color: @TEXT@;
}

#statusBadge {
    font-size: 12px;
    font-weight: 600;
    padding: 3px 11px;
    border-radius: 11px;
    color: @MUTED@;
    border: 1px solid @LINE@;
}
#statusBadge[kind="ready"] { color: @OK@;   border-color: rgba(@OK_RGB@, 0.55); }
#statusBadge[kind="busy"]  { color: @BUSY@; border-color: rgba(@BUSY_RGB@, 0.55); }
#statusBadge[kind="warn"]  { color: @WARN@; border-color: rgba(@WARN_RGB@, 0.55); }
#statusBadge[kind="error"] { color: @BAD@;  border-color: rgba(@BAD_RGB@, 0.55); }

#toolBtn {
    background-color: transparent;
    color: @MUTED@;
    font-weight: 600;
    padding: 5px 10px;
    border-radius: 6px;
    border: 1px solid transparent;
}
#toolBtn:hover { color: @TEXT@; background-color: @RAISED@; }
#toolBtn:checked { color: @HIGHLIGHT@; border-color: @LINE@; }
#toolBtn:focus { border-color: @HIGHLIGHT@; }

#btnPrimary {
    background-color: @PAPER@;
    color: @INK@;
    font-size: 15px;
    font-weight: 700;
    padding: 11px 20px;
    border-radius: 7px;
    border: 1px solid @PAPER@;
}
#btnPrimary:hover { background-color: #FFFFFF; border-color: #FFFFFF; }
#btnPrimary:pressed { background-color: @PAPER_PRESSED@; border-color: @PAPER_PRESSED@; }
#btnPrimary:focus { border-color: @HIGHLIGHT@; }

#btnSecondary {
    background-color: transparent;
    color: @TEXT@;
    font-weight: 600;
    padding: 11px 16px;
    border-radius: 7px;
    border: 1px solid @LINE@;
}
#btnSecondary:hover { background-color: @RAISED@; }
#btnSecondary:focus { border-color: @HIGHLIGHT@; }
#btnSecondary:disabled { color: @DISABLED@; border-color: @RAISED@; background-color: transparent; }

#previewFrame { background-color: @PAPER@; border: 1px solid @LINE@; border-radius: 0px; }
#sourceCard, #settingsPanel {
    background-color: @RAISED@;
    border: 1px solid @LINE@;
    border-radius: 7px;
}

#sectionLabel { color: @MUTED@; font-size: 12px; font-weight: 600; }

#formatPill {
    background-color: transparent;
    color: @MUTED@;
    font-size: 12px;
    font-weight: 600;
    padding: 4px 11px;
    border-radius: 5px;
    border: 1px solid @LINE@;
}
#formatPill:hover { color: @TEXT@; background-color: @RAISED@; }
#formatPill:checked { background-color: @HIGHLIGHT@; color: @INK_DARK@; border-color: @HIGHLIGHT@; }
#formatPill:focus { border-color: @TEXT@; }

#btnCopy {
    background-color: transparent;
    color: @TEXT@;
    font-weight: 700;
    padding: 5px 16px;
    border-radius: 6px;
    border: 1px solid @LINE@;
}
#btnCopy:hover { background-color: @RAISED@; }
#btnCopy:focus { border-color: @HIGHLIGHT@; }
#btnCopy[copied="true"] { color: @HIGHLIGHT@; border-color: @HIGHLIGHT@; }

#codeEditor {
    background-color: @WELL@;
    color: @TEXT@;
    font-family: @MONO@;
    font-size: 13px;
    border: 1px solid @LINE@;
    border-radius: 7px;
    padding: 10px;
    selection-background-color: @HIGHLIGHT@;
    selection-color: @INK_DARK@;
}
#codeEditor:focus { border-color: @HIGHLIGHT@; }

QCheckBox { color: @TEXT@; font-size: 12px; spacing: 8px; }
QCheckBox::indicator {
    width: 15px;
    height: 15px;
    border-radius: 4px;
    border: 1px solid @LINE@;
    background: @WELL@;
}
QCheckBox::indicator:checked { background: @HIGHLIGHT@; border-color: @HIGHLIGHT@; }
"""


# --------------------------------------------------------------------------- #
#  Aperçu KaTeX (feuille à petits carreaux)
# --------------------------------------------------------------------------- #
PREVIEW_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<link rel="stylesheet" href="katex/katex.min.css">
<script src="katex/katex.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    background-color: @PAPER@;
    background-image:
      linear-gradient(@GRID@ 1px, transparent 1px),
      linear-gradient(90deg, @GRID@ 1px, transparent 1px);
    background-size: 24px 24px;
    color: @INK@;
    font-family: @SANS@;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: auto;
    user-select: text;
  }
  #container {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    min-height: 96px;
    padding: 16px 28px;
  }
  .hint {
    color: @INK_MUTED@;
    font-size: 14px;
    line-height: 1.65;
    text-align: center;
    max-width: 38ch;
  }
  .hint kbd {
    font: inherit;
    font-weight: 600;
    color: @INK@;
    background: @PAPER@;
    border: 1px solid @INK_MUTED@;
    border-radius: 4px;
    padding: 0 6px;
  }
  .katex { font-size: 1.5em !important; color: @INK@ !important; }
  .katex-display { margin: 0 !important; max-width: 100%; overflow-x: auto; overflow-y: hidden; }
  .error {
    color: #8C2A22;
    background: #FBE9E6;
    border: 1px solid #E7B5AE;
    border-radius: 6px;
    padding: 8px 12px;
    font: 12px "SF Mono", Menlo, monospace;
    max-width: 90%;
  }
  .spinner {
    width: 28px;
    height: 28px;
    border: 3px solid rgba(@INK_RGB@, 0.18);
    border-top-color: @INK@;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spinner { animation-duration: 2.4s; } }
</style>
</head>
<body>
  <div id="container"></div>
  <script>
    const HINT = '<p class="hint">Capturez une formule avec <kbd>⌃⌥S</kbd> ou collez une image avec <kbd>⌘V</kbd>.</p>';
    const container = document.getElementById('container');
    container.innerHTML = HINT;

    function setLatex(latex) {
      if (!latex || !latex.trim()) { container.innerHTML = HINT; return; }
      try {
        katex.render(latex, container, { displayMode: true, throwOnError: false, trust: true });
      } catch (e) {
        showError(e.message);
      }
    }
    function setLoading() { container.innerHTML = '<div class="spinner" role="status" aria-label="Analyse en cours"></div>'; }
    function showError(message) {
      const box = document.createElement('div');
      box.className = 'error';
      box.textContent = message;
      container.replaceChildren(box);
    }
  </script>
</body>
</html>"""


# --------------------------------------------------------------------------- #
#  Overlay de capture (précis au pixel près, façon Apple)
# --------------------------------------------------------------------------- #
class SnipOverlay(QWidget):
    """
    Overlay plein écran « figé » pour une capture au pixel près.

    La sélection est calculée en pixels PHYSIQUES entiers. C'est exactement ce
    rectangle qui est (1) dessiné à l'écran et (2) découpé dans l'image capturée :
    ce qui est affiché est ce qui est capturé.
    """
    snip_captured = Signal(object)  # QImage
    snip_cancelled = Signal()

    MIN_SIZE_PX = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        # Une application « accessoire » est rarement active : le panneau ne doit pas se masquer
        always_show = getattr(Qt.WidgetAttribute, "WA_MacAlwaysShowToolWindow", None)
        if always_show is not None:
            self.setAttribute(always_show, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

        self.full_pixmap = QPixmap()
        self.full_image = QImage()
        self.screen_geo = QRect()
        self.is_dragging = False
        self.begin = QPointF()
        self.end = QPointF()

    # -- Démarrage -------------------------------------------------------- #
    def start_snip(self):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if not screen:
            self.snip_cancelled.emit()
            return

        self.screen_geo = screen.geometry()
        self.full_pixmap = screen.grabWindow(0)  # résolution native
        if self.full_pixmap.isNull():
            self.snip_cancelled.emit()
            return
        self.full_image = self.full_pixmap.toImage()

        self.is_dragging = False
        self.begin = QPointF()
        self.end = QPointF()

        # Pas de showFullScreen() : sur macOS il crée un Space natif et peut
        # réduire la fenêtre, ce qui décale la sélection. On cale une fenêtre
        # sans bordure exactement sur la géométrie de l'écran.
        self.setScreen(screen)
        self.setGeometry(self.screen_geo)
        self.show()
        self._elevate_above_menu_bar()
        self.setGeometry(self.screen_geo)
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def _elevate_above_menu_bar(self):
        """Au-dessus de la barre de menus et des applications en plein écran (macOS)."""
        float_over_fullscreen(self, level=25)  # NSMainMenuWindowLevel + 1

    # -- Géométrie : logique <-> physique --------------------------------- #
    def _scale(self):
        w = max(self.width(), 1)
        h = max(self.height(), 1)
        return self.full_image.width() / w, self.full_image.height() / h

    def _physical_rect(self) -> QRect:
        sx, sy = self._scale()
        iw, ih = self.full_image.width(), self.full_image.height()

        x1, x2 = sorted((self.begin.x(), self.end.x()))
        y1, y2 = sorted((self.begin.y(), self.end.y()))

        left = max(0, min(iw, int(round(x1 * sx))))
        right = max(0, min(iw, int(round(x2 * sx))))
        top = max(0, min(ih, int(round(y1 * sy))))
        bottom = max(0, min(ih, int(round(y2 * sy))))
        return QRect(left, top, right - left, bottom - top)

    def _logical_rect(self, phys: QRect) -> QRectF:
        sx, sy = self._scale()
        return QRectF(phys.x() / sx, phys.y() / sy, phys.width() / sx, phys.height() / sy)

    # -- Dessin ----------------------------------------------------------- #
    def paintEvent(self, event):
        if self.full_pixmap.isNull():
            return

        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self.full_pixmap)

        veil = QColor(Palette.DESK)
        veil.setAlpha(150)
        phys = self._physical_rect() if self.is_dragging else QRect()

        if self.is_dragging and phys.width() > 0 and phys.height() > 0:
            sel = self._logical_rect(phys)

            # Assombrit tout sauf la sélection
            path = QPainterPath()
            path.setFillRule(Qt.FillRule.OddEvenFill)
            path.addRect(QRectF(self.rect()))
            path.addRect(sel)
            painter.fillPath(path, veil)

            # Filet dessiné à l'extérieur de la zone capturée
            bw = 1.0
            outer = sel.adjusted(-bw / 2, -bw / 2, bw / 2, bw / 2)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(Palette.PAPER), bw))
            painter.drawRect(outer)

            # Poignées d'angle au surligneur
            c_len = min(14.0, sel.width() / 3, sel.height() / 3)
            if c_len > 3:
                painter.setPen(QPen(QColor(Palette.HIGHLIGHT), 2.0, Qt.PenStyle.SolidLine,
                                    Qt.PenCapStyle.FlatCap))
                l, t, r, b = outer.left(), outer.top(), outer.right(), outer.bottom()
                for (cx, cy, dx, dy) in ((l, t, 1, 1), (r, t, -1, 1),
                                         (l, b, 1, -1), (r, b, -1, -1)):
                    painter.drawLine(QPointF(cx, cy), QPointF(cx + dx * c_len, cy))
                    painter.drawLine(QPointF(cx, cy), QPointF(cx, cy + dy * c_len))

            # Dimensions en pixels physiques réels
            dim_text = f"{phys.width()} × {phys.height()} px"
            painter.setFont(QFont("Helvetica Neue", 11, QFont.Weight.DemiBold))
            tw = painter.fontMetrics().horizontalAdvance(dim_text)

            pill_w, pill_h = tw + 18, 24
            pill_x = int(sel.left())
            pill_y = int(sel.bottom()) + 8
            if pill_y + pill_h > self.height():
                pill_y = int(sel.top()) - pill_h - 8
            if pill_x + pill_w > self.width():
                pill_x = int(sel.right()) - pill_w
            pill_x = max(pill_x, 4)
            pill_y = max(pill_y, 4)

            pill_rect = QRect(pill_x, pill_y, pill_w, pill_h)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(Palette.DESK))
            painter.drawRoundedRect(pill_rect, 6, 6)
            painter.setPen(QColor(Palette.TEXT))
            painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, dim_text)
        else:
            painter.fillRect(self.rect(), veil)

            instruction = "Glissez pour capturer la formule. Échap pour annuler."
            painter.setFont(QFont("Helvetica Neue", 13, QFont.Weight.Medium))
            tw = painter.fontMetrics().horizontalAdvance(instruction)
            prompt_rect = QRect((self.width() - tw - 36) // 2, 45, tw + 36, 36)

            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(Palette.DESK))
            painter.drawRoundedRect(prompt_rect, 8, 8)
            painter.setPen(QColor(Palette.TEXT))
            painter.drawText(prompt_rect, Qt.AlignmentFlag.AlignCenter, instruction)

    # -- Souris / clavier -------------------------------------------------- #
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.cancel()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_dragging = True
            self.begin = QPointF(event.position())
            self.end = QPointF(event.position())
            self.update()

    def mouseMoveEvent(self, event):
        if self.is_dragging:
            self.end = QPointF(event.position())
            self.update()

    def mouseReleaseEvent(self, event):
        if not self.is_dragging or event.button() != Qt.MouseButton.LeftButton:
            return
        self.end = QPointF(event.position())
        self.is_dragging = False

        phys = self._physical_rect()
        self.hide()

        if phys.width() < self.MIN_SIZE_PX or phys.height() < self.MIN_SIZE_PX:
            self.snip_cancelled.emit()
            return

        self.snip_captured.emit(self.full_image.copy(phys))

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Q):
            self.cancel()
            event.accept()
        else:
            super().keyPressEvent(event)

    def cancel(self):
        self.is_dragging = False
        self.hide()
        self.snip_cancelled.emit()


# --------------------------------------------------------------------------- #
#  Surface de dépôt (glisser-déposer)
# --------------------------------------------------------------------------- #
class DropSurface(QWidget):
    file_dropped = Signal(str)
    qimage_dropped = Signal(object)

    IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".webp")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasUrls() or mime.hasImage():
            event.acceptProposedAction()

    def dropEvent(self, event):
        mime = event.mimeData()
        if mime.hasUrls():
            for url in mime.urls():
                path = url.toLocalFile()
                if path.lower().endswith(self.IMAGE_SUFFIXES):
                    self.file_dropped.emit(path)
                    event.acceptProposedAction()
                    return
        elif mime.hasImage():
            self.qimage_dropped.emit(mime.imageData())
            event.acceptProposedAction()


# --------------------------------------------------------------------------- #
#  Icône de la barre des menus
# --------------------------------------------------------------------------- #
class MenuBarItem(QObject):
    capture_requested = Signal()
    show_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.available = QSystemTrayIcon.isSystemTrayAvailable()

        self.tray = QSystemTrayIcon(build_menu_bar_icon(), self)
        self.tray.setToolTip("MathView")

        self.menu = QMenu()  # référence conservée : sinon le menu est détruit
        capture = self.menu.addAction(f"Capturer une formule  ({SNIP_SHORTCUT_LABEL})")
        show = self.menu.addAction("Afficher MathView")
        self.menu.addSeparator()
        quit_action = self.menu.addAction("Quitter MathView")

        capture.triggered.connect(self.capture_requested)
        show.triggered.connect(self.show_requested)
        quit_action.triggered.connect(self.quit_requested)

        self.tray.setContextMenu(self.menu)
        self.tray.show()

    def notify(self, title: str, message: str):
        if self.available:
            self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.NoIcon, 3500)


# --------------------------------------------------------------------------- #
#  Fenêtre principale
# --------------------------------------------------------------------------- #
class MathViewWindow(QMainWindow):
    # Intentions de l'utilisateur (écoutées par le contrôleur)
    image_captured = Signal(object)     # QImage
    file_dropped = Signal(str)
    qimage_dropped = Signal(object)
    paste_requested = Signal()
    retry_requested = Signal()
    copy_requested = Signal()
    format_selected = Signal(str)
    output_edited = Signal(str)
    autocopy_toggled = Signal(bool)
    quit_requested = Signal()

    FORMATS = [
        ("LaTeX-$", "$…$"),
        ("LaTeX-$$", "$$…$$"),
        ("Raw", "Brut"),
        ("SymPy", "SymPy"),
        ("Env", "equation*"),
    ]
    DEFAULT_FORMAT = "LaTeX-$"

    def __init__(self):
        super().__init__()
        self.setWindowTitle("MathView")
        self.resize(720, 640)
        self.setMinimumSize(560, 460)

        self._has_source_image = False
        self._snip_pending = False
        self._restore_after_snip = True

        self.snip_overlay = SnipOverlay()
        self.snip_overlay.snip_captured.connect(self._on_snip_captured)
        self.snip_overlay.snip_cancelled.connect(self._on_snip_cancelled)

        self.menu_bar_item = MenuBarItem(self)
        self.menu_bar_item.capture_requested.connect(self.begin_snip)
        self.menu_bar_item.show_requested.connect(self.present)
        self.menu_bar_item.quit_requested.connect(self.quit_requested)

        self.copy_feedback_timer = QTimer(self)
        self.copy_feedback_timer.setSingleShot(True)
        self.copy_feedback_timer.setInterval(1800)
        self.copy_feedback_timer.timeout.connect(self._reset_copy_button)

        self._build_ui()
        for button in self.findChildren(QPushButton):
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.centralWidget().setFocus()
        self.setStyleSheet(_render(STYLESHEET))
        self._setup_shortcuts()
        self._load_preview()
        self.set_status("ready", "Prêt")

    # -- Construction ------------------------------------------------------ #
    def _build_ui(self):
        surface = DropSurface(self)
        surface.file_dropped.connect(self.file_dropped)
        surface.qimage_dropped.connect(self.qimage_dropped)
        self.setCentralWidget(surface)

        root = QVBoxLayout(surface)
        root.setContentsMargins(22, 18, 22, 20)
        root.setSpacing(14)

        root.addLayout(self._build_header())
        root.addWidget(self._build_settings_panel())
        root.addLayout(self._build_action_row())
        root.addWidget(self._build_source_card())
        root.addWidget(self._build_preview(), stretch=3)
        root.addLayout(self._build_format_row())
        root.addWidget(self._build_editor(), stretch=2)

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setSpacing(8)

        title = QLabel("MathView")
        title.setObjectName("appTitle")
        header.addWidget(title)
        header.addStretch()

        self.status_badge = QLabel()
        self.status_badge.setObjectName("statusBadge")
        header.addWidget(self.status_badge)

        self.btn_pin = QPushButton("Épingler")
        self.btn_pin.setObjectName("toolBtn")
        self.btn_pin.setCheckable(True)
        self.btn_pin.setToolTip("Garder la fenêtre au premier plan")
        self.btn_pin.toggled.connect(self._toggle_always_on_top)
        header.addWidget(self.btn_pin)

        self.btn_settings = QPushButton("Réglages")
        self.btn_settings.setObjectName("toolBtn")
        self.btn_settings.setCheckable(True)
        self.btn_settings.setToolTip("Afficher les réglages")
        self.btn_settings.toggled.connect(lambda on: self.settings_panel.setVisible(on))
        header.addWidget(self.btn_settings)
        return header

    def _build_settings_panel(self) -> QFrame:
        self.settings_panel = QFrame()
        self.settings_panel.setObjectName("settingsPanel")
        self.settings_panel.setVisible(False)
        layout = QVBoxLayout(self.settings_panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        self.chk_autocopy = QCheckBox("Copier le résultat après chaque capture")
        self.chk_autocopy.setChecked(True)
        self.chk_autocopy.toggled.connect(self.autocopy_toggled)
        self.chk_preview_img = QCheckBox("Afficher la vignette de l'image source")
        self.chk_preview_img.setChecked(False)
        self.chk_preview_img.toggled.connect(lambda _: self._refresh_source_card())
        layout.addWidget(self.chk_autocopy)
        layout.addWidget(self.chk_preview_img)
        return self.settings_panel

    def _build_action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        self.btn_snip = QPushButton(f"Capturer une formule  ({SNIP_SHORTCUT_LABEL})")
        self.btn_snip.setObjectName("btnPrimary")
        self.btn_snip.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_snip.clicked.connect(self.begin_snip)
        row.addWidget(self.btn_snip, stretch=3)

        self.btn_paste = QPushButton("Coller  (⌘V)")
        self.btn_paste.setObjectName("btnSecondary")
        self.btn_paste.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_paste.clicked.connect(self.paste_requested)
        row.addWidget(self.btn_paste, stretch=1)

        self.btn_retry = QPushButton("Réessayer")
        self.btn_retry.setObjectName("btnSecondary")
        self.btn_retry.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_retry.setEnabled(False)
        self.btn_retry.clicked.connect(self.retry_requested)
        row.addWidget(self.btn_retry, stretch=1)
        return row

    def _build_source_card(self) -> QFrame:
        self.source_img_card = QFrame()
        self.source_img_card.setObjectName("sourceCard")
        self.source_img_card.setVisible(False)
        layout = QHBoxLayout(self.source_img_card)
        layout.setContentsMargins(10, 8, 10, 8)
        self.lbl_source_img = QLabel()
        self.lbl_source_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_source_img.setMaximumHeight(90)
        layout.addWidget(self.lbl_source_img)
        return self.source_img_card

    def _build_preview(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("previewFrame")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)

        self.web_view = QWebEngineView()
        self.web_view.setObjectName("webView")
        self.web_view.page().setBackgroundColor(QColor(Palette.PAPER))
        self.web_view.setMinimumHeight(140)
        layout.addWidget(self.web_view)
        return frame

    def _build_format_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)

        lbl = QLabel("Format")
        lbl.setObjectName("sectionLabel")
        row.addWidget(lbl)
        row.addSpacing(4)

        self.current_format = self.DEFAULT_FORMAT
        self.format_group = QButtonGroup(self)
        self.format_group.setExclusive(True)
        self.format_btns = {}
        for fmt_id, label in self.FORMATS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setObjectName("formatPill")
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.setChecked(fmt_id == self.current_format)
            self.format_group.addButton(btn)
            btn.clicked.connect(lambda _=False, f=fmt_id: self._on_format_clicked(f))
            row.addWidget(btn)
            self.format_btns[fmt_id] = btn

        row.addStretch()

        self.btn_copy = QPushButton("Copier")
        self.btn_copy.setObjectName("btnCopy")
        self.btn_copy.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_copy.clicked.connect(self.copy_requested)
        row.addWidget(self.btn_copy)
        return row

    def _build_editor(self) -> QTextEdit:
        self.editor = QTextEdit()
        self.editor.setObjectName("codeEditor")
        self.editor.setPlaceholderText("Le code LaTeX apparaîtra ici. Vous pouvez le modifier directement.")
        self.editor.setMinimumHeight(80)
        self.editor.setAcceptRichText(False)
        self.editor.textChanged.connect(lambda: self.output_edited.emit(self.editor.toPlainText()))
        return self.editor

    def _setup_shortcuts(self):
        # Raccourci local (le raccourci global est enregistré par mathview_app)
        self.shortcut_snip = QShortcut(QKeySequence(SNIP_SHORTCUT_QT), self)
        self.shortcut_snip.activated.connect(self.begin_snip)

        # Sans Dock ni menu d'application, ⌘W masque la fenêtre et ⌘Q quitte
        self.shortcut_hide = QShortcut(QKeySequence.StandardKey.Close, self)
        self.shortcut_hide.activated.connect(self.hide)
        self.shortcut_quit = QShortcut(QKeySequence.StandardKey.Quit, self)
        self.shortcut_quit.activated.connect(self.quit_requested)

        self.shortcut_paste = QShortcut(QKeySequence.StandardKey.Paste, self)
        self.shortcut_paste.activated.connect(self._on_paste_shortcut)

    def _load_preview(self):
        html = _render(PREVIEW_HTML)
        base_url = QUrl.fromLocalFile(str(RESOURCES_DIR) + "/")
        self.web_view.setHtml(html, baseUrl=base_url)

    # -- API d'affichage (appelée par le contrôleur) ----------------------- #
    @staticmethod
    def _repolish(widget: QWidget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def set_status(self, kind: str, text: str):
        """kind : 'ready' | 'busy' | 'warn' | 'error'."""
        self.status_badge.setText(f"●  {text}")
        self.status_badge.setProperty("kind", kind)
        self._repolish(self.status_badge)

    def set_output_text(self, text: str):
        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)

    def output_text(self) -> str:
        return self.editor.toPlainText()

    def render_latex(self, latex: str):
        self.web_view.page().runJavaScript(f"setLatex({json.dumps(latex)});")

    def show_loading(self):
        self.web_view.page().runJavaScript("setLoading();")

    def show_error(self, message: str):
        self.web_view.page().runJavaScript(f"showError({json.dumps(message)});")

    def set_retry_enabled(self, enabled: bool):
        self.btn_retry.setEnabled(enabled)

    def set_source_preview(self, png_bytes: bytes):
        pixmap = QPixmap()
        pixmap.loadFromData(png_bytes)
        self.lbl_source_img.setPixmap(
            pixmap.scaledToHeight(80, Qt.TransformationMode.SmoothTransformation)
        )
        self._has_source_image = True
        self._refresh_source_card()

    def flash_copied(self):
        self.btn_copy.setText("Copié")
        self.btn_copy.setProperty("copied", True)
        self._repolish(self.btn_copy)
        self.copy_feedback_timer.start()

    @property
    def auto_copy_enabled(self) -> bool:
        return self.chk_autocopy.isChecked()

    # -- Interactions internes --------------------------------------------- #
    def begin_snip(self):
        if self._snip_pending or self.snip_overlay.isVisible():
            return  # déjà en cours (raccourci local et global déclenchés ensemble)
        self._snip_pending = True

        # Fenêtre visible : on la masque le temps de la capture. Sinon (usage depuis
        # la barre des menus), on ne la fait pas apparaître, afin de rester sur
        # l'application en cours, y compris en plein écran.
        self._restore_after_snip = self.isVisible()
        delay = 0
        if self._restore_after_snip:
            self.hide()
            delay = 140  # laisse l'animation de fermeture de macOS se terminer
        QTimer.singleShot(delay, self._start_overlay)

    def _start_overlay(self):
        self._snip_pending = False
        self.snip_overlay.start_snip()

    def _on_snip_captured(self, image: QImage):
        if self._restore_after_snip:
            self.present()
        self.image_captured.emit(image)

    def _on_snip_cancelled(self):
        if self._restore_after_snip:
            self.present()

    def present(self):
        """Affiche la fenêtre sur l'espace actif, au-dessus d'un éventuel plein écran."""
        self.show()
        float_over_fullscreen(self)
        self.raise_()
        self.activateWindow()

    def notify(self, title: str, message: str):
        """Retour discret quand la fenêtre est masquée : bip et notification."""
        QApplication.beep()
        self.menu_bar_item.notify(title, message)

    def closeEvent(self, event):
        # Fermer la fenêtre ne quitte pas l'application : elle reste dans la barre des menus
        if self.menu_bar_item.available:
            event.ignore()
            self.hide()
        else:
            event.accept()
            self.quit_requested.emit()

    def _on_paste_shortcut(self):
        mime = QApplication.clipboard().mimeData()
        if mime is not None and mime.hasImage():
            self.paste_requested.emit()
        elif self.editor.hasFocus():
            self.editor.paste()  # collage de texte ordinaire dans l'éditeur
        else:
            self.paste_requested.emit()  # le contrôleur signalera l'absence d'image

    def _on_format_clicked(self, fmt_id: str):
        self.current_format = fmt_id
        self.format_selected.emit(fmt_id)

    def _refresh_source_card(self):
        self.source_img_card.setVisible(self.chk_preview_img.isChecked() and self._has_source_image)

    def _toggle_always_on_top(self, checked: bool):
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.present()

    def _reset_copy_button(self):
        self.btn_copy.setText("Copier")
        self.btn_copy.setProperty("copied", False)
        self._repolish(self.btn_copy)