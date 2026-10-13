"""
MathView — logique et point d'entrée.

Ce module contient tout ce qui n'est pas de l'affichage : chargement du modèle
UniMERNet, reconnaissance dans un thread, mise en forme des résultats, presse-papiers
et orchestration. L'interface vit dans `mathview_ui.py` ; le contrôleur ci-dessous
relie les signaux de la fenêtre aux traitements.

Lancement :  python mathview_app.py
"""

import warnings
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

import sys
import os
from pathlib import Path

# Si exécuté hors de l'environnement virtuel dédié (Python 3.12 obligatoire pour transformers 4.42.4),
# bascule automatiquement sur l'interpréteur de venv-unimer.
_venv_python = Path(__file__).resolve().parent / "venv-unimer" / "bin" / "python"
if _venv_python.is_file() and sys.executable != str(_venv_python):
    os.execv(str(_venv_python), [str(_venv_python)] + sys.argv)

# Variables d'environnement : à poser AVANT l'import de Qt WebEngine (via mathview_ui)
os.environ["NO_ALBUMENTATIONS_UPDATE"] = "1"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"

import io
import re
import ctypes
import ctypes.util
import logging
import faulthandler

faulthandler.enable()  # en cas de plantage natif, affiche où il s'est produit

# Initialisation de numpy et torch dans le thread principal pour éviter les conflits
# de bibliothèques natives (Accelerate BLAS) sur macOS avec les threads secondaires Qt.
import numpy  # noqa: F401
import torch  # noqa: F401

from mathview_engine import ensure_unimer_files, UniMERNetOCR  # noqa: E402

from PIL import Image  # noqa: E402

from PySide6.QtCore import Signal, Slot, QThread, QTimer, QObject, QBuffer, QIODevice  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mathview_ui import MathViewWindow  # noqa: E402

try:
    from latex2sympy2 import latex2sympy
except ImportError:
    latex2sympy = None


# --------------------------------------------------------------------------- #
#  Fonctions pures : formats et images
# --------------------------------------------------------------------------- #
def to_sympy_expr(latex: str) -> str:
    if not latex2sympy:
        return latex
    try:
        normalized = re.sub(r'operatorname\*?{(\w+)}', r'\1', latex)
        return str(latex2sympy(f'${normalized}$'))
    except Exception as err:
        return f"Erreur SymPy: {err}"


def format_prediction(raw: str, fmt: str) -> str:
    clean = raw.strip()
    if fmt == "LaTeX-$":
        return f"${clean}$"
    if fmt == "LaTeX-$$":
        return f"$${clean}$$"
    if fmt == "Raw":
        return clean
    if fmt == "SymPy":
        return to_sympy_expr(clean)
    if fmt == "Env":
        return f"\\begin{{equation*}}\n  {clean}\n\\end{{equation*}}"
    return clean


def strip_math_delimiters(text: str) -> str:
    """Retire les délimiteurs $...$ ou $$...$$ pour obtenir la formule brute."""
    text = text.strip()
    if text.startswith("$$") and text.endswith("$$") and len(text) >= 4:
        return text[2:-2].strip()
    if text.startswith("$") and text.endswith("$") and len(text) >= 2:
        return text[1:-1].strip()
    return text


def normalize_image(img: Image.Image) -> Image.Image:
    """RGB sur fond blanc : une image transparente ne doit pas devenir noire."""
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return img.convert("RGB")


def qimage_to_pil(qimage: QImage) -> Image.Image:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.ReadWrite)
    qimage.save(buffer, "PNG")
    return normalize_image(Image.open(io.BytesIO(bytes(buffer.data()))))


def pil_to_png_bytes(img: Image.Image) -> bytes:
    stream = io.BytesIO()
    img.save(stream, format="PNG")
    return stream.getvalue()


# --------------------------------------------------------------------------- #
#  Intégration macOS : application de barre des menus et raccourci global
# --------------------------------------------------------------------------- #
MENU_BAR_ONLY = True  # True : pas d'icône dans le Dock ni dans ⌘Tab


def make_menu_bar_app():
    """
    Passe l'application en mode « accessoire » (comme LSUIElement). C'est ce qui lui
    permet d'apparaître au-dessus d'une application en plein écran sans que macOS
    ne vous renvoie vers un autre espace.
    """
    if sys.platform != "darwin" or not MENU_BAR_ONLY:
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory  # type: ignore
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception as err:
        logging.warning(
            "Mode barre des menus indisponible (pip install pyobjc-framework-Cocoa) : %s", err
        )


class GlobalHotkey(QObject):
    """
    Raccourci clavier global via l'API Carbon (RegisterEventHotKey) : il fonctionne même
    lorsque MathView n'est pas l'application active et n'exige aucune permission système.
    """
    pressed = Signal()

    _EVENT_CLASS_KEYBOARD = int.from_bytes(b"keyb", "big")
    _EVENT_HOTKEY_PRESSED = 5
    _SIGNATURE = int.from_bytes(b"mvhk", "big")

    KEY_S = 0x01
    MOD_OPTION = 1 << 11
    MOD_CONTROL = 1 << 12

    class _EventTypeSpec(ctypes.Structure):
        _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]

    class _EventHotKeyID(ctypes.Structure):
        _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

    _HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

    def __init__(self, key_code: int = KEY_S, modifiers: int = MOD_CONTROL | MOD_OPTION, parent=None):
        super().__init__(parent)
        self._key_code = key_code
        self._modifiers = modifiers
        self._carbon = None
        self._handler = None  # référence indispensable : sinon le rappel est collecté
        self._hotkey_ref = ctypes.c_void_p()

    def register(self) -> bool:
        if sys.platform != "darwin":
            return False
        try:
            path = ctypes.util.find_library("Carbon") or "/System/Library/Frameworks/Carbon.framework/Carbon"
            carbon = ctypes.CDLL(path)

            carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
            carbon.InstallEventHandler.restype = ctypes.c_int32
            carbon.InstallEventHandler.argtypes = [
                ctypes.c_void_p, self._HANDLER, ctypes.c_uint32,
                ctypes.POINTER(self._EventTypeSpec), ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            carbon.RegisterEventHotKey.restype = ctypes.c_int32
            carbon.RegisterEventHotKey.argtypes = [
                ctypes.c_uint32, ctypes.c_uint32, self._EventHotKeyID,
                ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
            ]
            carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]

            def on_event(_call_ref, _event, _user_data):
                self.pressed.emit()
                return 0  # noErr

            self._handler = self._HANDLER(on_event)
            target = carbon.GetApplicationEventTarget()
            spec = self._EventTypeSpec(self._EVENT_CLASS_KEYBOARD, self._EVENT_HOTKEY_PRESSED)
            status = carbon.InstallEventHandler(
                target, self._handler, 1, ctypes.byref(spec), None, None
            )
            if status != 0:
                raise OSError(f"InstallEventHandler a échoué ({status})")

            hotkey_id = self._EventHotKeyID(self._SIGNATURE, 1)
            status = carbon.RegisterEventHotKey(
                self._key_code, self._modifiers, hotkey_id, target, 0,
                ctypes.byref(self._hotkey_ref),
            )
            if status != 0:
                raise OSError(f"RegisterEventHotKey a échoué ({status})")

            self._carbon = carbon
            return True
        except Exception as err:
            logging.warning("Raccourci global indisponible : %s", err)
            return False

    def unregister(self):
        if self._carbon is not None and self._hotkey_ref:
            self._carbon.UnregisterEventHotKey(self._hotkey_ref)
            self._carbon = None


# --------------------------------------------------------------------------- #
#  Threads
# --------------------------------------------------------------------------- #
class ModelLoaderThread(QThread):
    loaded = Signal(object)
    error = Signal(str)
    status = Signal(str)

    def run(self):
        try:
            self.status.emit("Chargement du modèle…")
            model_dir = ensure_unimer_files(self.status.emit)
            self.status.emit("Chargement du modèle…")
            self.loaded.emit(UniMERNetOCR(model_dir))
        except Exception as e:
            self.error.emit(f"{type(e).__name__} : {e}")


class OCRWorker(QThread):
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, model, image: Image.Image):
        super().__init__()
        self.model = model
        self.image = image

    def run(self):
        try:
            if self.model is None:
                raise RuntimeError("Modèle OCR non initialisé.")
            prediction = self.model(self.image)
            self.finished.emit(prediction.strip())
        except Exception as e:
            self.error.emit(str(e))


# --------------------------------------------------------------------------- #
#  Contrôleur
# --------------------------------------------------------------------------- #
class MathViewController(QObject):
    def __init__(self, window: MathViewWindow):
        super().__init__(window)
        self.ui = window

        # État
        self.raw_latex = ""
        self.current_format = window.current_format
        self.auto_copy = window.auto_copy_enabled
        self.current_image: Image.Image | None = None
        self.pending_image: Image.Image | None = None
        self.model = None
        self.loader_thread: ModelLoaderThread | None = None
        self.worker: OCRWorker | None = None

        # Rendu différé pendant la saisie dans l'éditeur
        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.setInterval(120)
        self.render_timer.timeout.connect(self.sync_math_preview)

        self._connect_ui()

        # Chargement du modèle en arrière-plan au démarrage
        QTimer.singleShot(50, self.load_model_async)

    def _connect_ui(self):
        ui = self.ui
        ui.image_captured.connect(self.on_qimage)
        ui.qimage_dropped.connect(self.on_qimage)
        ui.file_dropped.connect(self.on_file_dropped)
        ui.paste_requested.connect(self.paste_from_clipboard)
        ui.retry_requested.connect(self.retry_current_image)
        ui.copy_requested.connect(self.copy_to_clipboard)
        ui.format_selected.connect(self.on_format_changed)
        ui.output_edited.connect(self.on_output_edited)
        ui.autocopy_toggled.connect(self.on_autocopy_toggled)

    # -- Modèle ------------------------------------------------------------ #
    def load_model_async(self):
        self.ui.set_status("warn", "Chargement du modèle…")
        self.loader_thread = ModelLoaderThread(self)
        self.loader_thread.status.connect(lambda text: self.ui.set_status("warn", text))
        self.loader_thread.loaded.connect(self.on_model_loaded)
        self.loader_thread.error.connect(self.on_model_load_error)
        self.loader_thread.start()

    @Slot(object)
    def on_model_loaded(self, model):
        self.model = model
        self.ui.set_status("ready", "Prêt")
        if self.pending_image is not None:
            img = self.pending_image
            self.pending_image = None
            self.process_image(img)

    @Slot(str)
    def on_model_load_error(self, err_msg: str):
        logging.error(f"Erreur initialisation modèle: {err_msg}")
        self.ui.set_status("error", "Modèle indisponible")
        self.ui.show_error(f"Le modèle n'a pas pu être chargé : {err_msg}")

    @Slot(bool)
    def on_autocopy_toggled(self, enabled: bool):
        self.auto_copy = enabled

    # -- Entrées d'image --------------------------------------------------- #
    @Slot(object)
    def on_qimage(self, qimage: QImage):
        self.process_image(qimage_to_pil(qimage))

    @Slot(str)
    def on_file_dropped(self, path: str):
        try:
            self.process_image(normalize_image(Image.open(path)))
        except Exception as e:
            logging.error(f"Erreur chargement image déposée: {e}")
            self.ui.set_status("error", "Image illisible")
            QTimer.singleShot(2000, lambda: self.ui.set_status("ready", "Prêt"))

    def paste_from_clipboard(self):
        qimage = QApplication.clipboard().image()
        if not qimage.isNull():
            self.process_image(qimage_to_pil(qimage))
        else:
            self.ui.set_status("warn", "Le presse-papiers ne contient pas d'image")
            QTimer.singleShot(2000, lambda: self.ui.set_status("ready", "Prêt"))

    def retry_current_image(self):
        if self.current_image:
            self.process_image(self.current_image)

    # -- Reconnaissance ---------------------------------------------------- #
    def process_image(self, pil_img: Image.Image):
        self.current_image = pil_img.copy()
        self.ui.set_retry_enabled(True)
        self.ui.set_source_preview(pil_to_png_bytes(pil_img))
        self.ui.show_loading()

        if self.model is None:
            self.pending_image = pil_img.copy()
            self.ui.set_status("warn", "Chargement du modèle…")
            return

        self.ui.set_status("busy", "Analyse en cours…")

        # Reconnaissance dans un thread pour garder l'interface fluide
        self.worker = OCRWorker(self.model, pil_img)
        self.worker.finished.connect(self.on_ocr_success)
        self.worker.error.connect(self.on_ocr_error)
        self.worker.start()

    @Slot(str)
    def on_ocr_success(self, raw_prediction: str):
        self.raw_latex = raw_prediction
        self.ui.set_status("ready", "Terminé")

        # Fenêtre masquée = capture lancée depuis la barre des menus : le résultat
        # est alors toujours copié, car il n'y a aucun autre endroit où le lire.
        background = not self.ui.isVisible()

        self.ui.set_output_text(format_prediction(self.raw_latex, self.current_format))
        self.sync_math_preview()

        if self.auto_copy or background:
            self.copy_to_clipboard()
            if background:
                self.ui.notify("Formule copiée", self.ui.output_text().strip()[:120])
        else:
            QTimer.singleShot(1500, lambda: self.ui.set_status("ready", "Prêt"))

    @Slot(str)
    def on_ocr_error(self, err_msg: str):
        logging.error(f"Erreur OCR: {err_msg}")
        self.ui.set_status("error", "Échec de l'analyse")
        self.ui.show_error(err_msg)
        if not self.ui.isVisible():
            self.ui.notify("Échec de l'analyse", err_msg[:120])

    # -- Format, édition, aperçu ------------------------------------------- #
    @Slot(str)
    def on_format_changed(self, fmt_id: str):
        self.current_format = fmt_id
        if self.raw_latex:
            self.ui.set_output_text(format_prediction(self.raw_latex, fmt_id))
            if self.auto_copy:
                self.copy_to_clipboard()

    @Slot(str)
    def on_output_edited(self, text: str):
        self.raw_latex = strip_math_delimiters(text)
        self.render_timer.start()

    def sync_math_preview(self):
        self.ui.render_latex(strip_math_delimiters(self.raw_latex))

    # -- Presse-papiers ---------------------------------------------------- #
    def copy_to_clipboard(self):
        text = self.ui.output_text().strip()
        if not text:
            return
        QApplication.clipboard().setText(text)
        self.ui.flash_copied()


# --------------------------------------------------------------------------- #
#  Point d'entrée
# --------------------------------------------------------------------------- #
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("MathView")
    app.setQuitOnLastWindowClosed(False)  # l'application reste dans la barre des menus
    make_menu_bar_app()

    window = MathViewWindow()
    window.quit_requested.connect(app.quit)
    controller = MathViewController(window)  # noqa: F841 (maintient la référence)

    hotkey = GlobalHotkey(parent=app)
    hotkey.pressed.connect(window.begin_snip)
    if not hotkey.register():
        logging.warning("Seul le raccourci local est actif (l'application doit être au premier plan).")
    app.aboutToQuit.connect(hotkey.unregister)

    window.present()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()