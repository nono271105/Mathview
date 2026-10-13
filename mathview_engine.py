"""
MathView — moteur OCR UniMERNet (sans Qt).

Ce module ne dépend pas de l'interface : il peut être testé seul, ce qui isole
les problèmes de chargement du modèle de ceux de l'application.

    python mathview_engine.py formule.png            # test sur le processeur
    MATHVIEW_DEVICE=mps python mathview_engine.py formule.png   # test sur le GPU du Mac
"""

import os
import re
import sys
import warnings
import faulthandler
import logging
from pathlib import Path

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
os.environ.setdefault("NO_ALBUMENTATIONS_UPDATE", "1")

from PIL import Image, ImageOps, ImageStat

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")   # opérations MPS manquantes -> processeur
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# "cpu" : choix sûr. "auto" ou "mps" : utilise le GPU du Mac s'il est disponible.
UNIMER_DEVICE = os.environ.get("MATHVIEW_DEVICE", "cpu")

_PRELOAD_ERROR = None


def preload():
    """
    Importe torch et unimernet tout de suite, dans le thread principal et avant Qt.
    Par précaution : ces bibliothèques natives sont plus sûres lorsqu'elles sont
    initialisées ainsi que dans un thread secondaire, après Qt WebEngine.
    """
    global _PRELOAD_ERROR
    if all((UNIMER_DIR / name).is_file() for name in UNIMER_FILES):
        # Modèle déjà présent : plus aucune requête réseau. Ni une panne, ni une mise à jour
        # de Hugging Face ne peut alors affecter l'application.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    try:
        import torch  # noqa: F401
        import unimernet.models.unimernet.unimernet  # noqa: F401
        import unimernet.processors  # noqa: F401
    except Exception as err:  # l'erreur sera relevée au chargement du modèle
        _PRELOAD_ERROR = err
        logging.warning("Préchargement d'UniMERNet impossible : %s", err)


UNIMER_VARIANT = "base"  # "base" (1,3 Go, le plus précis), "small" (773 Mo) ou "tiny" (441 Mo)
UNIMER_REPO = f"wanderkid/unimernet_{UNIMER_VARIANT}"
UNIMER_DIR = Path(__file__).resolve().parent / "models" / f"unimernet_{UNIMER_VARIANT}"
UNIMER_FILES = (
    "config.json", "preprocessor_config.json",
    "tokenizer.json", "tokenizer_config.json", "pytorch_model.pth",
)
UNIMER_IMAGE_SIZE = (192, 672)  # (hauteur, largeur) attendues par le modèle
UNIMER_MAX_TOKENS = 1024


def tidy_latex(latex: str) -> str:
    """
    UniMERNet sépare tous les jetons par des espaces (« \\frac { 1 } { 2 } »). Le rendu est
    identique, mais le code est illisible une fois collé dans Word ou Overleaf. On ne retire
    que les espaces sans effet : à l'intérieur et avant les accolades, autour de « ^ » et « _ »,
    et aux abords des parenthèses et de la ponctuation. Les espaces qui séparent une commande
    de la lettre suivante (« \\hbar m ») ou deux groupes (« } \\frac ») sont conservés.
    """
    latex = re.sub(r"\s+", " ", latex.strip())
    latex = re.sub(r"\s+(?=[{}^_])", "", latex)             # avant { } ^ _
    latex = re.sub(r"(?<=[{^_])\s+", "", latex)             # après { ^ _
    latex = re.sub(r"(\\(?:psi|phi|varphi|Psi|Phi|sin|cos|tan|ln|log|exp))\s+(?=\()", r"\1", latex)  # \psi (x) -> \psi(x)
    latex = re.sub(r"(?<=[(\[])\s+", "", latex)            # après ( [
    latex = re.sub(r"\s+(?=[)\],.;])", "", latex)          # avant ) ] , . ;
    return latex


def prepare_for_ocr(img: Image.Image) -> Image.Image:
    """UniMERNet a appris sur du texte sombre sur fond clair : on inverse les captures sombres."""
    rgb = img.convert("RGB")
    if ImageStat.Stat(rgb.convert("L")).mean[0] < 110:
        return ImageOps.invert(rgb)
    return rgb


def ensure_unimer_files(on_status=None) -> Path:
    """Télécharge le modèle au premier lancement (puis réutilise la copie locale)."""
    def missing():
        return [name for name in UNIMER_FILES if not (UNIMER_DIR / name).is_file()]

    if missing():
        if on_status:
            on_status("Téléchargement du modèle (première utilisation)…")
        from huggingface_hub import snapshot_download
        snapshot_download(
            repo_id=UNIMER_REPO,
            local_dir=str(UNIMER_DIR),
            allow_patterns=["*.json", "*.pth"],
        )
    if missing():
        raise RuntimeError(f"Fichiers du modèle manquants dans {UNIMER_DIR} : {', '.join(missing())}")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    return UNIMER_DIR


class UniMERNetOCR:
    """Enveloppe d'UniMERNet : charge le modèle une fois ; l'instance s'appelle sur une image PIL."""

    def __init__(self, model_dir: Path):
        if _PRELOAD_ERROR is not None:
            raise RuntimeError(f"Bibliothèques indisponibles : {_PRELOAD_ERROR}")
        import torch
        from omegaconf import OmegaConf
        from unimernet.models.unimernet.unimernet import UniMERModel
        from unimernet.processors import load_processor

        self._torch = torch

        # Configuration équivalente au demo.yaml du dépôt, sans passer par Config / Task.
        cfg = OmegaConf.create({
            "arch": "unimernet",
            "load_pretrained": False,   # les poids sont chargés et contrôlés plus bas
            "load_finetuned": False,
            "model_name": "unimernet",
            "tokenizer_name": "nougat",
            "tokenizer_config": {"path": str(model_dir)},
            "model_config": {"model_name": str(model_dir), "max_seq_len": UNIMER_MAX_TOKENS},
        })
        model = UniMERModel.from_config(cfg)
        self._load_weights(model, model_dir / "pytorch_model.pth")

        mps_ok = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
        use_mps = UNIMER_DEVICE in ("auto", "mps") and mps_ok
        self.device = torch.device("mps" if use_mps else "cpu")
        self.model = model.to(self.device).eval()
        self.vis_processor = load_processor(
            "formula_image_eval", OmegaConf.create({"image_size": list(UNIMER_IMAGE_SIZE)})
        )
        logging.info("UniMERNet prêt sur %s", self.device)

    def _load_weights(self, model, path: Path):
        torch = self._torch
        checkpoint = torch.load(str(path), map_location="cpu", weights_only=True)
        state = checkpoint.get("model", checkpoint)
        result = model.load_state_dict(state, strict=False)

        # load_state_dict(strict=False) est silencieux : on refuse un modèle aux poids aléatoires.
        total = max(len(state), 1)
        if len(result.missing_keys) > total // 2 or len(result.unexpected_keys) > total // 2:
            raise RuntimeError(
                "Les poids ne correspondent pas au modèle "
                f"({len(result.missing_keys)} clés manquantes, {len(result.unexpected_keys)} inattendues). "
                "Vérifiez la version de transformers (4.42.4 exigée)."
            )
        if result.missing_keys or result.unexpected_keys:
            logging.warning("Poids partiellement appariés : %d manquantes, %d inattendues",
                            len(result.missing_keys), len(result.unexpected_keys))

    def _generate(self, tensor) -> str:
        output = self.model.generate({"image": tensor.to(self.device)})
        return tidy_latex(output["pred_str"][0])

    def __call__(self, image: Image.Image) -> str:
        tensor = self.vis_processor(prepare_for_ocr(image))
        if tensor is None:
            raise ValueError("Image vide ou illisible.")
        tensor = tensor.unsqueeze(0)
        try:
            return self._generate(tensor)
        except RuntimeError as err:
            if self.device.type == "cpu":
                raise
            logging.warning("Échec sur %s (%s) : repli sur le processeur.", self.device, err)
            self.device = self._torch.device("cpu")
            self.model.to(self.device)
            return self._generate(tensor)


if __name__ == "__main__":
    import time
    from pathlib import Path

    _venv_python = Path(__file__).resolve().parent / "venv-unimer" / "bin" / "python"
    if _venv_python.is_file() and sys.executable != str(_venv_python):
        os.execv(str(_venv_python), [str(_venv_python)] + sys.argv)

    faulthandler.enable()
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        sys.exit("Usage : python mathview_engine.py image.png [nombre_d_essais]")
    attempts = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    preload()
    start = time.time()
    engine = UniMERNetOCR(ensure_unimer_files(print))
    print(f"Modèle chargé en {time.time() - start:.1f} s sur « {engine.device} »")

    # Plusieurs essais : le premier inclut le préchauffage (surtout sur MPS), pas les suivants.
    image = Image.open(sys.argv[1])
    for attempt in range(1, attempts + 1):
        start = time.time()
        result = engine(image)
        print(f"Essai {attempt} : {time.time() - start:.1f} s")
    print(result)