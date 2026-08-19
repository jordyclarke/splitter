"""
POD Batch Splitter — Kodak Alaris s2050 / Kodak Capture Pro
==============================================================
Watches a folder for multi-page PDF batches and saves individual waybill
PDFs to POD_System/2_Output/{batch_name}/ named {waybill}.pdf.

Default watch folder: POD_System/1_Input/
Optional settings.ini: watch Kodak's existing output tree recursively.

Development:  pip install -r requirements.txt && python pod_splitter.py
Production:   run POD_Splitter.exe (built via GitHub Actions — see README.md)
"""

from __future__ import annotations

import base64
import configparser
import io
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

try:
    import requests
except ImportError:
    sys.exit("requests not found. Run:  pip install requests")

try:
    from PIL import Image, ImageEnhance, ImageOps
except ImportError:
    sys.exit("Pillow not found. Run:  pip install Pillow")

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("PyMuPDF not found. Run:  pip install PyMuPDF")

try:
    from pyzbar import pyzbar
except ImportError:
    sys.exit("pyzbar not found. Run:  pip install pyzbar")

try:
    import pytesseract
except ImportError:
    sys.exit("pytesseract not found. Run:  pip install pytesseract")

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:
    sys.exit("watchdog not found. Run:  pip install watchdog")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _app_root() -> Path:
    """Folder containing the script (dev) or POD_Splitter.exe (packaged)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_ROOT = _app_root()
POD_ROOT = APP_ROOT / "POD_System"
ENV_FILE = APP_ROOT / "env" / ".env"
SETTINGS_FILE = APP_ROOT / "settings.ini"
SETTINGS_EXAMPLE = APP_ROOT / "settings.ini.example"

TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# OpenAI vision — handwritten receiver field extraction.
# gpt-4o-mini: best cost/accuracy balance for handwriting (~10× cheaper than gpt-4o).
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_REQUEST_TIMEOUT = 90
AI_RENDER_SCALE = 2.0

# Crop band for the signature/name acceptance row (LDLS delivery log sheets).
SIGNATURE_REGION_TOP = 0.36
SIGNATURE_REGION_BOTTOM = 0.58

OPENAI_API_KEY: Optional[str] = None

# Receiver name extraction (OpenAI) — disabled until extraction quality is ready.
EXTRACT_RECEIVER_FIELDS = False

DIRS = {
    "input":   POD_ROOT / "1_Input",
    "output":  POD_ROOT / "2_Output",
    "archive": POD_ROOT / "3_Archive",
    "errors":  POD_ROOT / "4_Errors",
}

LOG_FILE = POD_ROOT / "processing_log.txt"
PROCESSED_LOG = POD_ROOT / "processed_batches.txt"

# Populated by load_settings() before the watcher starts.
WATCH_DIR: Path = DIRS["input"]
WATCH_RECURSIVE = False
ARCHIVE_SOURCE = True
PROCESS_EXISTING = True

_processed_paths: set[str] = set()

# Render resolution for barcode / OCR scanning (300 DPI ≈ scale 3 at 72 dpi base)
RENDER_SCALE = 3.0

# How long (seconds) to wait between file-size checks to confirm a file is
# fully written before processing begins.
FILE_SETTLE_INTERVAL = 2.0
FILE_SETTLE_CHECKS   = 4   # file must report the same size this many times

# Characters that are illegal in a waybill number
ILLEGAL_CHARS_RE = re.compile(r"[%#*\\\/:\"<>|?]")

# Regex: a valid waybill must contain at least one letter and one digit.
WAYBILL_ID_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).+$")

# Barcode scan scales — wide CODE39 barcodes (e.g. LDLS) decode at scale 2.0 only.
BARCODE_SCAN_SCALES = (3.0, 2.0, 2.5, 4.0)

# Printed waybill numbers in the PDF text layer (fallback when pyzbar misses).
WAYBILL_TEXT_RE = re.compile(
    r"\b(?:LDLS|DLS|BIC|AFS|MET|SBA|CLP|INV|A10|REV|SAV|SUZ)\d+[A-Z0-9-]*\b",
    re.I,
)
# Region (fraction of page height) to scan for barcodes / OCR fallback.
HEADER_REGION_FRACTION = 0.35
OCR_REGION_FRACTION = 0.30

# Image preprocessing for faint barcodes on thermal / low-contrast scans.
CONTRAST_BOOST = 2.0
SHARPNESS_BOOST = 1.5
OCR_RENDER_SCALE = 4.0  # used for barcode OCR fallback only

# When only a signature is present with no readable name, use this value.
SIGNATURE_ONLY_NAME = "signiture"

# Ignore short numeric-only values (account numbers like 9100, 8346).
MIN_WAYBILL_LENGTH = 6

# Known waybill prefixes seen in production samples.
WAYBILL_PREFIXES = ("LDLS", "DLS", "INV", "CLP", "A10", "AFS", "BIC", "MET", "SBA", "REV", "SAV", "SUZ")

# ClipSa barcodes read as INV… but the true waybill number is CLP…
INV_TO_CLP_RE = re.compile(r"^INV(?P<number>\d.*)$", re.I)

# Dis-Chem delivery log sheets print DLS… but the canonical prefix is LDLS…
DLS_TO_LDLS_RE = re.compile(r"^DLS(?P<number>\d.*)$", re.I)

# DSV ECO stickers — waybill is the printed Document / Consignment number, not the barcode.
DSV_MARKER_RE = re.compile(r"\bDSV\b", re.I)
DSV_DOCUMENT_RE = re.compile(
    r"Document\s*:?\s*(?P<doc>\d{8,12})\b",
    re.I,
)
DSV_CONSIGNMENT_RE = re.compile(
    r"Consignment\s*:?\s*(?P<con>\d{8,12})\b",
    re.I,
)
DSV_DOC_MIN_LEN = 8
DSV_DOC_MAX_LEN = 12

# AFS/BIC stickers have two barcodes: the short waybill at the top of the sticker
# (e.g. AFS1451475, BIC26005499) and a longer package barcode below that appends
# 0001 (e.g. AFS14514750001, BIC260054990001-0002). Always use the short sticker.
AFS_LONG_SUFFIX_RE = re.compile(r"^(AFS\d+?)000\d+$", re.I)
AFS_STICKER_RE = re.compile(r"^AFS\d{7,10}$", re.I)

BIC_LONG_SUFFIX_RE = re.compile(r"^(BIC\d+?)000\d+.*$", re.I)
BIC_STICKER_RE = re.compile(r"^BIC\d{7,10}$", re.I)

# (prefix, long-package regex, short-sticker regex, max sticker length for scoring)
STICKER_WAYBILL_RULES: tuple[tuple[str, re.Pattern[str], re.Pattern[str], int], ...] = (
    ("AFS", AFS_LONG_SUFFIX_RE, AFS_STICKER_RE, 12),
    ("BIC", BIC_LONG_SUFFIX_RE, BIC_STICKER_RE, 14),
)

# ---------------------------------------------------------------------------
# Pipeline result record (steps 4–9 will populate more fields over time)
# ---------------------------------------------------------------------------

@dataclass
class PODResult:
    waybill_number: str
    output_pdf: Path
    page_count: int
    source_batch: str
    ocr_fields: dict[str, str] = field(default_factory=dict)
    upload_status: Optional[str] = None
    system_update_status: Optional[str] = None
    success: bool = True
    error_message: Optional[str] = None

# ---------------------------------------------------------------------------
# Logging setup — writes to both console and log file
# ---------------------------------------------------------------------------

def _build_logger() -> logging.Logger:
    logger = logging.getLogger("PODSplitter")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # File handler — appended so history is preserved across restarts
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError as exc:
        logger.warning("Could not open log file %s: %s", LOG_FILE, exc)

    return logger


log = _build_logger()

# ---------------------------------------------------------------------------
# Directory bootstrap
# ---------------------------------------------------------------------------

def ensure_directories() -> None:
    for name, path in DIRS.items():
        path.mkdir(parents=True, exist_ok=True)
        log.debug("Directory ready: %s", path)


def load_settings() -> None:
    """Load watch folder and processing options from settings.ini (if present)."""
    global WATCH_DIR, WATCH_RECURSIVE, ARCHIVE_SOURCE, PROCESS_EXISTING

    WATCH_DIR = DIRS["input"]
    WATCH_RECURSIVE = False
    ARCHIVE_SOURCE = True
    PROCESS_EXISTING = True

    if not SETTINGS_FILE.is_file():
        log.info(
            "No settings.ini — watching %s (flat). "
            "Copy settings.ini.example to settings.ini to watch Kodak output.",
            WATCH_DIR,
        )
        return

    cfg = configparser.ConfigParser()
    cfg.read(SETTINGS_FILE, encoding="utf-8")

    if cfg.has_section("watch"):
        folder = cfg.get("watch", "folder", fallback="").strip()
        if folder:
            watch_path = Path(folder).expanduser()
            if not watch_path.is_absolute():
                watch_path = (APP_ROOT / watch_path).resolve()
            else:
                watch_path = watch_path.resolve()
            WATCH_DIR = watch_path
        WATCH_RECURSIVE = cfg.getboolean("watch", "recursive", fallback=True)

    if cfg.has_section("processing"):
        ARCHIVE_SOURCE = cfg.getboolean("processing", "archive_source", fallback=False)
        PROCESS_EXISTING = cfg.getboolean("processing", "process_existing", fallback=False)
    elif WATCH_DIR.resolve() != DIRS["input"].resolve():
        # External Kodak folder — leave originals in place by default.
        ARCHIVE_SOURCE = False
        PROCESS_EXISTING = False

    if not WATCH_DIR.is_dir():
        log.warning("Watch folder does not exist yet — creating: %s", WATCH_DIR)
        WATCH_DIR.mkdir(parents=True, exist_ok=True)


def load_processed_log() -> None:
    """Load paths of batches already split (avoids reprocessing on restart)."""
    global _processed_paths
    _processed_paths = set()
    if not PROCESSED_LOG.is_file():
        return
    for line in PROCESSED_LOG.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if entry:
            _processed_paths.add(entry)


def is_processed(path: Path) -> bool:
    return str(path.resolve()) in _processed_paths


def mark_processed(path: Path) -> None:
    key = str(path.resolve())
    if key in _processed_paths:
        return
    _processed_paths.add(key)
    try:
        PROCESSED_LOG.parent.mkdir(parents=True, exist_ok=True)
        with PROCESSED_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{key}\n")
    except OSError as exc:
        log.warning(
            "Could not write processed log %s (tracking kept in memory this session): %s",
            PROCESSED_LOG,
            exc,
        )


def is_under_watch_dir(path: Path) -> bool:
    try:
        path.resolve().relative_to(WATCH_DIR.resolve())
        return True
    except ValueError:
        return False


def iter_watch_pdfs() -> list[Path]:
    """Return unprocessed PDFs currently under the watch folder."""
    watch = WATCH_DIR.resolve()
    if WATCH_RECURSIVE:
        candidates = sorted(watch.rglob("*.pdf"))
    else:
        candidates = sorted(watch.glob("*.pdf"))
    return [pdf for pdf in candidates if not is_processed(pdf)]


def _error_dest_name(path: Path) -> str:
    """Build a unique error filename when watching nested Kodak folders."""
    if ARCHIVE_SOURCE or path.parent.resolve() == DIRS["input"].resolve():
        return path.name
    try:
        rel = path.resolve().relative_to(WATCH_DIR.resolve())
        return str(rel).replace("\\", "_").replace("/", "_")
    except ValueError:
        return path.name


def _relocate_batch(path: Path, dest_dir: Path, label: str) -> None:
    """Move or copy a batch to dest_dir depending on archive_source."""
    dest = dest_dir / _error_dest_name(path)
    if dest.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = dest_dir / f"{dest.stem}_{ts}{dest.suffix}"
    try:
        if ARCHIVE_SOURCE:
            shutil.move(str(path), str(dest))
            log.info("%s moved: %s", label, dest.name)
        else:
            shutil.copy2(str(path), str(dest))
            log.info("%s copied: %s (original left in Kodak folder)", label, dest.name)
    except Exception as exc:
        log.error("Could not %s %s: %s", "move" if ARCHIVE_SOURCE else "copy", path.name, exc)


# ---------------------------------------------------------------------------
# Tesseract configuration
# ---------------------------------------------------------------------------

def configure_tesseract() -> None:
    if os.path.isfile(TESSERACT_PATH):
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
        log.debug("Tesseract found at %s", TESSERACT_PATH)
    else:
        log.warning(
            "Tesseract not found at %s — barcode OCR fallback will be disabled. "
            "Download from: https://github.com/UB-Mannheim/tesseract/wiki",
            TESSERACT_PATH,
        )


def load_env_file(path: Path = ENV_FILE) -> None:
    """Load key=value pairs from env/.env into the process environment."""
    if not path.is_file():
        log.debug("No env file at %s", path)
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


def configure_openai() -> None:
    """Load the OpenAI API key and model from env/.env or the environment."""
    global OPENAI_API_KEY, OPENAI_MODEL
    load_env_file()
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip() or None
    OPENAI_MODEL = os.environ.get("OPENAI_MODEL", OPENAI_MODEL).strip() or "gpt-4o-mini"
    if OPENAI_API_KEY:
        log.info("OpenAI configured for handwritten field extraction (model: %s).", OPENAI_MODEL)
    else:
        log.warning(
            "OPENAI_API_KEY not set — copy env/.env.example to env/.env and add your key."
        )


TESSERACT_AVAILABLE: bool = False  # set after configure_tesseract()


# ---------------------------------------------------------------------------
# Barcode / OCR helpers
# ---------------------------------------------------------------------------

def _normalise_waybill_number(waybill: str) -> str:
    """
    Apply supplier-specific waybill corrections after barcode scan.
    - INV00720608 → CLP00720608 (ClipSa invoice barcode)
    - DLS926241 → LDLS926241 (Dis-Chem delivery log prefix)
    - AFS14514750001 → AFS1451475 (short AFS sticker, not package barcode)
    - BIC260054990001-0002 → BIC26005499 (short BIC sticker, not package barcode)
    """
    inv_match = INV_TO_CLP_RE.match(waybill)
    if inv_match:
        normalised = f"CLP{inv_match.group('number')}"
        if normalised != waybill:
            log.info("Waybill corrected: %s → %s", waybill, normalised)
        return normalised

    dls_match = DLS_TO_LDLS_RE.match(waybill)
    if dls_match:
        normalised = f"LDLS{dls_match.group('number')}"
        if normalised != waybill:
            log.info("Waybill corrected: %s → %s", waybill, normalised)
        return normalised

    for _prefix, long_re, _sticker_re, _max_len in STICKER_WAYBILL_RULES:
        match = long_re.match(waybill)
        if match:
            normalised = match.group(1)
            if normalised != waybill:
                log.info("Waybill corrected: %s → %s", waybill, normalised)
            return normalised

    return waybill


def _extract_dsv_document(text: str) -> Optional[str]:
    """
    Read the waybill from a DSV sticker's printed Document / Consignment field.
    The linear barcode on DSV labels is not the waybill ID.
    """
    if not DSV_MARKER_RE.search(text):
        return None

    doc_match = DSV_DOCUMENT_RE.search(text)
    if doc_match:
        return doc_match.group("doc")

    con_match = DSV_CONSIGNMENT_RE.search(text)
    if con_match:
        return con_match.group("con")

    return None


def _sanitise_dsv_document(raw: str) -> Optional[str]:
    """Validate a DSV Document / Consignment number."""
    cleaned = ILLEGAL_CHARS_RE.sub("", raw.strip())
    if not cleaned.isdigit():
        return None
    if not (DSV_DOC_MIN_LEN <= len(cleaned) <= DSV_DOC_MAX_LEN):
        return None
    return cleaned


def scan_dsv_sticker(page: fitz.Page) -> Optional[str]:
    """Read DSV Document number from the PDF text layer."""
    try:
        doc = _extract_dsv_document(page.get_text())
        if doc:
            waybill = _sanitise_dsv_document(doc)
            if waybill:
                log.debug("DSV text layer hit: %r", waybill)
                return waybill
    except Exception as exc:
        log.warning("DSV text scan error: %s", exc)
    return None


def scan_dsv_ocr(page: fitz.Page) -> Optional[str]:
    """OCR fallback for DSV stickers when the PDF text layer is empty."""
    if not TESSERACT_AVAILABLE:
        return None

    try:
        img = _pil_from_fitz_page(page, scale=OCR_RENDER_SCALE)
        config = "--oem 3 --psm 6"

        for variant in (img, _enhance_for_barcode(img)):
            text = pytesseract.image_to_string(variant, config=config)
            doc = _extract_dsv_document(text)
            if doc:
                waybill = _sanitise_dsv_document(doc)
                if waybill:
                    log.debug("DSV OCR hit: %r", waybill)
                    return waybill
    except Exception as exc:
        log.warning("DSV OCR error: %s", exc)

    return None


def _is_valid_waybill_id(value: str) -> bool:
    """
    Real waybill IDs always contain letters and digits (e.g. LDLS926241).
    Reject name stickers (LAMOLA), pure numeric store codes (138879), and
    long package tracking numbers (460096544394946449).
    """
    return bool(WAYBILL_ID_RE.match(value))


def _sanitise_barcode(raw: str) -> Optional[str]:
    """
    Strip whitespace and illegal characters from a raw barcode string.
    Returns the cleaned string if it looks like a waybill ID, otherwise None.
    """
    cleaned = raw.strip()
    cleaned = ILLEGAL_CHARS_RE.sub("", cleaned)
    cleaned = cleaned.strip()

    if not cleaned:
        return None

    # Ensure the last character is alphanumeric
    if not cleaned[-1].isalnum():
        cleaned = cleaned.rstrip("".join(c for c in cleaned if not c.isalnum()))
        cleaned = cleaned.strip()

    if not cleaned or not cleaned[-1].isalnum():
        return None

    if not _is_valid_waybill_id(cleaned):
        log.debug("Rejected non-waybill barcode: %r", cleaned)
        return None

    return _normalise_waybill_number(cleaned)


def _pil_from_fitz_page(page: fitz.Page, scale: float = RENDER_SCALE) -> Image.Image:
    """Render a PyMuPDF page to a Pillow Image at the requested scale."""
    mat = fitz.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def _header_crop(img: Image.Image, fraction: float = HEADER_REGION_FRACTION) -> Image.Image:
    """Crop to the top header band where waybill barcodes usually sit."""
    width, height = img.size
    return img.crop((0, 0, width, int(height * fraction)))


def _signature_crop(img: Image.Image) -> Image.Image:
    """Crop to the signature/name acceptance row where receivers sign."""
    width, height = img.size
    return img.crop((
        0,
        int(height * SIGNATURE_REGION_TOP),
        width,
        int(height * SIGNATURE_REGION_BOTTOM),
    ))


def _enhance_for_barcode(img: Image.Image) -> Image.Image:
    """
    Boost contrast and sharpness for faint thermal-printed barcodes.
    Grayscale conversion often helps pyzbar on noisy colour scans.
    """
    gray = ImageOps.grayscale(img)
    rgb = gray.convert("RGB")
    contrast = ImageEnhance.Contrast(rgb).enhance(CONTRAST_BOOST)
    return ImageEnhance.Sharpness(contrast).enhance(SHARPNESS_BOOST)


def _decode_barcodes(img: Image.Image) -> list[tuple[str, float, float]]:
    """
    Decode all barcodes in an image.
    Returns (sanitised_value, relative_y, relative_height) tuples.
    """
    width, height = img.size
    hits: list[tuple[str, float, float]] = []
    for symbol in pyzbar.decode(img):
        raw = symbol.data.decode("utf-8", errors="replace")
        waybill = _sanitise_barcode(raw)
        if not waybill:
            continue
        rel_y = symbol.rect.top / height if height else 0.0
        rel_h = symbol.rect.height / height if height else 0.0
        hits.append((waybill, rel_y, rel_h))
    return hits


def _score_waybill_candidate(value: str, rel_y: float, rel_h: float) -> float:
    """
    Rank barcode candidates when a page has multiple codes.
    Higher score = more likely to be the document waybill ID.
    """
    score = 0.0

    if not _is_valid_waybill_id(value):
        return -100.0

    if len(value) >= MIN_WAYBILL_LENGTH:
        score += 20
    elif value.isdigit():
        return -100.0

    # Long numeric tracking numbers (18+ digits) are not waybill IDs.
    if value.isdigit() and len(value) >= 15:
        return -100.0

    if any(ch.isalpha() for ch in value):
        score += 10

    if value.upper().startswith(WAYBILL_PREFIXES):
        score += 15

    # AFS/BIC: prefer the short sticker barcode at the top, not the long package code.
    upper = value.upper()
    for prefix, long_re, sticker_re, max_len in STICKER_WAYBILL_RULES:
        if not upper.startswith(prefix):
            continue
        if sticker_re.match(upper):
            score += 30
        if long_re.match(upper):
            score -= 50
        if len(value) > max_len:
            score -= 20
        break

    if "-" in value or "/" in value:
        score += 5

    # Header band — most supplier waybill barcodes live here.
    if 0.05 <= rel_y <= 0.40:
        score += 15
    elif rel_y < 0.05:
        score += 5

    score += min(len(value), 20) * 0.5
    score += rel_h * 10
    return score


def _pick_best_waybill(candidates: list[tuple[str, float, float]]) -> Optional[str]:
    """Choose the highest-scoring waybill candidate from a page."""
    if not candidates:
        return None

    ranked = sorted(
        candidates,
        key=lambda item: _score_waybill_candidate(item[0], item[1], item[2]),
        reverse=True,
    )
    best_value, best_y, _ = ranked[0]
    best_score = _score_waybill_candidate(best_value, best_y, ranked[0][2])

    if best_score < 0:
        return None

    if len(ranked) > 1:
        log.debug(
            "Waybill candidates: %s → picked %r (score=%.1f)",
            [(v, round(_score_waybill_candidate(v, y, h), 1)) for v, y, h in ranked],
            best_value,
            best_score,
        )

    return best_value


def _scan_image_variants(img: Image.Image) -> Optional[str]:
    """Try several preprocessed views of the same page region."""
    candidates: list[tuple[str, float, float]] = []

    for variant in (img, _enhance_for_barcode(img)):
        candidates.extend(_decode_barcodes(variant))

    return _pick_best_waybill(candidates)


def scan_barcodes(page: fitz.Page) -> Optional[str]:
    """
    Multi-pass barcode scan at several render scales.
    Wide CODE39 barcodes (LDLS delivery log sheets) only decode at lower scales.
    """
    try:
        all_candidates: list[tuple[str, float, float]] = []
        seen_scales: set[float] = set()

        for scale in BARCODE_SCAN_SCALES:
            if scale in seen_scales:
                continue
            seen_scales.add(scale)

            base_img = _pil_from_fitz_page(page, scale=scale)
            header_img = _header_crop(base_img)

            for img in (base_img, header_img):
                for variant in (img, _enhance_for_barcode(img)):
                    all_candidates.extend(_decode_barcodes(variant))

        waybill = _pick_best_waybill(all_candidates)
        if waybill:
            log.debug("pyzbar hit: %r", waybill)
        return waybill

    except Exception as exc:
        log.warning("pyzbar error on page: %s", exc)

    return None


def _normalize_scanned_text(text: str) -> str:
    """Fix common OCR glitches in scanned PDF text layers."""
    return (
        text.replace("LDI.S", "LDLS")
        .replace("LD1S", "LDLS")
        .replace("LDI.S", "LDLS")
        .replace("LDLS", "LDLS")
    )


def scan_text_layer(page: fitz.Page) -> Optional[str]:
    """
    Read the printed waybill number from the PDF text layer.
    Fallback for large LDLS barcodes that pyzbar misses at high render scales.
    """
    try:
        text = _normalize_scanned_text(page.get_text())
        candidates: list[tuple[str, float, float]] = []
        for match in WAYBILL_TEXT_RE.finditer(text):
            waybill = _sanitise_barcode(match.group())
            if waybill and len(waybill) >= MIN_WAYBILL_LENGTH:
                candidates.append((waybill, 0.08, 0.02))

        waybill = _pick_best_waybill(candidates)
        if waybill:
            log.debug("Text layer hit: %r", waybill)
        return waybill
    except Exception as exc:
        log.warning("Text layer scan error: %s", exc)

    return None


def scan_ocr_fallback(page: fitz.Page) -> Optional[str]:
    """
    OCR fallback: render the header region at high resolution, apply contrast
    enhancement, and read printed text under/near the barcode zone.
    """
    if not TESSERACT_AVAILABLE:
        return None

    try:
        img = _pil_from_fitz_page(page, scale=RENDER_SCALE)
        cropped = _header_crop(img, fraction=OCR_REGION_FRACTION)

        candidates: list[tuple[str, float, float]] = []
        config = "--oem 3 --psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-/"

        for variant in (cropped, _enhance_for_barcode(cropped)):
            text = pytesseract.image_to_string(variant, config=config)
            for token in text.split():
                waybill = _sanitise_barcode(token)
                if waybill and len(waybill) >= MIN_WAYBILL_LENGTH:
                    candidates.append((waybill, 0.15, 0.02))

        waybill = _pick_best_waybill(candidates)
        if waybill:
            log.debug("OCR fallback hit: cleaned=%r", waybill)
        return waybill
    except Exception as exc:
        log.warning("OCR fallback error: %s", exc)

    return None


def identify_waybill(page: fitz.Page) -> tuple[Optional[str], str]:
    """Steps 1–2: find barcode and identify the waybill number for a page."""
    # DSV stickers — Document number is the waybill, not the barcode.
    waybill = scan_dsv_sticker(page)
    if waybill:
        return waybill, "dsv"
    waybill = scan_dsv_ocr(page)
    if waybill:
        return waybill, "dsv-ocr"
    waybill = scan_barcodes(page)
    if waybill:
        return waybill, "barcode"
    waybill = scan_text_layer(page)
    if waybill:
        return waybill, "text"
    waybill = scan_ocr_fallback(page)
    if waybill:
        return waybill, "ocr"
    return None, "none"


def pod_pages_dir(waybill: str) -> Path:
    """Directory for rendered page images belonging to one split POD."""
    return DIRS["output"] / "pages" / waybill


def render_pod_page_images(pdf_path: Path, waybill: str) -> list[Path]:
    """
    Phase 2a: render each page of a split POD to PNG on disk.
    Images are stored under 2_Output/pages/{waybill}/ for separate AI processing.
    """
    pages_dir = pod_pages_dir(waybill)
    pages_dir.mkdir(parents=True, exist_ok=True)

    saved: list[Path] = []
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        log.warning("Could not open %s for page rendering: %s", pdf_path.name, exc)
        return saved

    try:
        for page_index in range(doc.page_count):
            page = doc.load_page(page_index)
            img = _pil_from_fitz_page(page, scale=AI_RENDER_SCALE)
            out_path = pages_dir / f"page_{page_index + 1:03d}.png"
            img.save(out_path, format="PNG")
            saved.append(out_path)
    finally:
        doc.close()

    if saved:
        log.info(
            "  [PAGES]  %s — %d page image(s) saved to %s",
            waybill,
            len(saved),
            pages_dir.relative_to(DIRS["output"]),
        )
    return saved


def extract_receiver_fields(
    waybill: str,
    page_images: list[Path],
    pdf_path: Path,
) -> dict[str, str]:
    """
    Phase 2b: run Gemini on saved page images and link receiver fields to the waybill.
    Called immediately after each split PDF is saved.
    """
    if not OPENAI_API_KEY:
        log.warning("Skipping AI field extraction for %s — no API key configured", waybill)
        return {}

    if not page_images:
        log.warning("  [AI] %s — no page images to process", waybill)
        return {}

    page_results: list[dict[str, Any]] = []
    for page_index, img_path in enumerate(page_images):
        try:
            with Image.open(img_path) as img:
                rgb = img.convert("RGB")
                page_num = page_index + 1
                if page_num == 1:
                    sig_path = img_path.with_name(
                        img_path.name.replace(".png", "_sig.png")
                    )
                    _signature_crop(rgb).save(sig_path, format="PNG")
                fields = _openai_extract_from_image(rgb, page_num)
        except OSError as exc:
            log.warning("Could not read page image %s: %s", img_path.name, exc)
            continue
        if fields:
            fields["_page_num"] = page_index + 1
            page_results.append(fields)
            log.debug("  Page %d AI fields: %s", page_index + 1, fields)

    merged = _merge_multi_page_fields(page_results)

    if merged:
        log.info(
            "  [AI] %s — name:%s  date:%s  time:%s  (from %d page(s))",
            waybill,
            merged.get("receiver_name", "—"),
            merged.get("receiver_date", "—"),
            merged.get("receiver_time", "—"),
            len(page_results),
        )
        _write_fields_sidecar(pdf_path, waybill, merged, page_images)
    else:
        log.warning("  [AI] %s — no receiver fields returned", waybill)

    return merged


def extract_ocr_fields(pdf_path: Path, waybill: str) -> dict[str, str]:
    """
    Convenience wrapper: render page images then extract receiver fields.
    Used when re-processing an already-saved split PDF.
    """
    page_images = render_pod_page_images(pdf_path, waybill)
    return extract_receiver_fields(waybill, page_images, pdf_path)


def _normalize_name_key(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def _names_are_similar(a: str, b: str) -> bool:
    if not a or not b or a == SIGNATURE_ONLY_NAME or b == SIGNATURE_ONLY_NAME:
        return False
    if _normalize_name_key(a) == _normalize_name_key(b):
        return True
    return SequenceMatcher(None, _normalize_name_key(a), _normalize_name_key(b)).ratio() >= 0.72


def _confidence_rank(value: Any) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(str(value).lower(), 0)


def _score_name_candidate(
    name: str,
    cluster_size: int,
    confidence: str,
    page_num: int = 99,
) -> float:
    if not name or name == SIGNATURE_ONLY_NAME:
        return -1.0
    score = cluster_size * 100.0
    score += _confidence_rank(confidence) * 25.0
    # Short nicknames like "Ya" are valid; don't require 4+ characters.
    if 2 <= len(name) <= 20:
        score += 10.0
    if name[:1].isupper():
        score += 5.0
    # Page 1 is the waybill/POD — attachment pages often have different signatures.
    if page_num == 1:
        score += 200.0
    return score


def _pick_best_name(page_results: list[dict[str, Any]]) -> Optional[str]:
    """Cluster similar names across pages and return the best candidate."""
    page1_results = [
        r for r in page_results
        if r.get("_page_num") == 1 and r.get("receiver_name")
    ]
    # Prefer the waybill page — invoice attachments often have unrelated signatures.
    if page1_results:
        page_results = page1_results

    entries = [
        {
            "name": r["receiver_name"],
            "confidence": r.get("_name_confidence", "medium"),
            "page_num": r.get("_page_num", 99),
        }
        for r in page_results
        if r.get("receiver_name") and r["receiver_name"] != SIGNATURE_ONLY_NAME
    ]
    if not entries:
        signiture_pages = sum(
            1 for r in page_results if r.get("receiver_name") == SIGNATURE_ONLY_NAME
        )
        if signiture_pages:
            return SIGNATURE_ONLY_NAME
        return None

    clusters: list[list[dict[str, str]]] = []
    for entry in entries:
        placed = False
        for cluster in clusters:
            if _names_are_similar(entry["name"], cluster[0]["name"]):
                cluster.append(entry)
                placed = True
                break
        if not placed:
            clusters.append([entry])

    best_cluster = max(
        clusters,
        key=lambda c: _score_name_candidate(
            c[0]["name"],
            len(c),
            max(_confidence_rank(x["confidence"]) for x in c),
            min(x["page_num"] for x in c),
        ),
    )

    best_entry = max(
        best_cluster,
        key=lambda e: _score_name_candidate(e["name"], len(best_cluster), e["confidence"]),
    )
    chosen = best_entry["name"]

    unique_names = {e["name"] for e in entries}
    if len(unique_names) > 1:
        log.info(
            "  [AI] name conflict across pages %s → picked %r",
            sorted(unique_names),
            chosen,
        )

    return chosen


def _pick_best_value(values: list[Any]) -> Optional[str]:
    """Pick the most common normalised value (used for date/time)."""
    if not values:
        return None
    counts: dict[str, int] = {}
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        key = text.lower()
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    best_key = max(counts, key=lambda k: (counts[k], len(k)))
    for value in values:
        text = str(value).strip()
        if text.lower() == best_key:
            return text
    return str(values[-1]).strip() or None


def _merge_multi_page_fields(page_results: list[dict[str, Any]]) -> dict[str, str]:
    """Merge per-page Gemini results into one record for the whole POD."""
    if not page_results:
        return {}

    merged: dict[str, str] = {}
    name = _pick_best_name(page_results)
    if name:
        merged["receiver_name"] = name

    dates = [r["receiver_date"] for r in page_results if r.get("receiver_date")]
    times = [r["receiver_time"] for r in page_results if r.get("receiver_time")]

    date = _pick_best_value(dates)
    time_value = _pick_best_value(times)
    if date:
        merged["receiver_date"] = date
    if time_value:
        merged["receiver_time"] = time_value

    return merged


def _image_to_png_base64(img: Image.Image) -> str:
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _parse_ai_json(text: str) -> dict[str, Any]:
    """Parse JSON returned by the vision model, tolerating markdown code fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("AI response was not a JSON object")

    fields: dict[str, Any] = {}
    for key in ("receiver_name", "receiver_date", "receiver_time"):
        value = str(data.get(key, "") or "").strip()
        if value:
            fields[key] = value

    confidence = str(data.get("name_confidence", "") or "").strip().lower()
    if confidence in ("high", "medium", "low"):
        fields["_name_confidence"] = confidence
    elif fields.get("receiver_name"):
        fields["_name_confidence"] = "medium"

    return fields


def _openai_extract_from_image(img: Image.Image, page_num: int = 1) -> dict[str, Any]:
    """Send a page image to OpenAI and extract handwritten receiver fields."""
    prompt = (
        f"You are reading page {page_num} of a scanned Proof of Delivery (POD) from South Africa.\n"
        "Find ONLY handwritten pen/ink text added by the person who received the goods.\n\n"
        "CRITICAL — do NOT use these as receiver_name:\n"
        "- Printed shop/business names in 'Deliver To' or recipient address lines "
        "(e.g. 'Japie Visser', 'Dischem Polokwane' — these are the STORE, not the receiver)\n"
        "- Pre-printed supplier text, barcodes, tables, logos, and stamps\n"
        "- Printed column headers or document timestamps\n\n"
    )
    if page_num == 1:
        prompt += (
            "You are given TWO images of the waybill page:\n"
            "- Image 1: cropped zoom of the signature/name acceptance row — "
            "use this for receiver_name. Read every letter carefully; "
            "short nicknames like 'Ya' are two letters, not one.\n"
            "- Image 2: full page — use for receiver_date and receiver_time.\n\n"
        )
    else:
        prompt += (
            "Look for handwriting in the 'Name and Surname of person accepting delivery' "
            "field, signature boxes, or anywhere pen was added by the receiver.\n"
            "Receivers often write short nicknames or abbreviations (e.g. 'Ya' instead of Tanya) — "
            "return EXACTLY what is handwritten, do not expand or replace with printed names.\n\n"
        )

    prompt += (
        "Extract these fields:\n"
        "1. receiver_name — handwritten name of the receiver. "
        f"If there is only an unreadable signature with no readable name, return exactly: {SIGNATURE_ONLY_NAME}\n"
        "2. receiver_date — handwritten date they signed (DD/MM/YYYY preferred)\n"
        "3. receiver_time — handwritten time they signed (HH:MM preferred)\n"
        "4. name_confidence — how confident you are in receiver_name: high, medium, or low\n\n"
        "Do NOT use printed header dates/times (e.g. document print timestamp).\n"
        "If handwriting is messy, give your best reading of what was actually written — "
        "prefer the handwritten field over any similar printed name elsewhere on the page.\n"
        "Return ONLY valid JSON with keys: receiver_name, receiver_date, receiver_time, name_confidence.\n"
        'Use empty string "" for any field not found.'
    )

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if page_num == 1:
        sig_crop = _signature_crop(img)
        content.extend([
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{_image_to_png_base64(sig_crop)}"},
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{_image_to_png_base64(img)}"},
            },
        ])
    else:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{_image_to_png_base64(img)}"},
        })

    payload: dict[str, Any] = {
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }

    try:
        response = requests.post(
            OPENAI_API_URL,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=OPENAI_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
        text = body["choices"][0]["message"]["content"]
        fields = _parse_ai_json(text)
        log.debug("OpenAI page fields: %s", fields)
        return fields
    except requests.HTTPError as exc:
        log.error("OpenAI API HTTP error: %s", exc)
        if exc.response is not None:
            log.debug("OpenAI response body: %s", exc.response.text[:500])
        return {}
    except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
        log.error("OpenAI API error: %s", exc)
        return {}


def _openai_extract_from_page(page: fitz.Page, page_num: int = 1) -> dict[str, Any]:
    """Render a PDF page and send it to OpenAI."""
    img = _pil_from_fitz_page(page, scale=AI_RENDER_SCALE)
    return _openai_extract_from_image(img, page_num)


def _write_fields_sidecar(
    pdf_path: Path,
    waybill: str,
    fields: dict[str, str],
    page_images: Optional[list[Path]] = None,
) -> None:
    """Save extracted receiver fields linked to the waybill as JSON alongside the PDF."""
    sidecar = pdf_path.with_suffix(".json")
    try:
        rel_images = [
            str(img.relative_to(DIRS["output"]))
            for img in (page_images or [])
        ]
    except ValueError:
        rel_images = [str(img) for img in (page_images or [])]

    payload = {
        "waybill_number": waybill,
        "pdf_file": pdf_path.name,
        "receiver_name": fields.get("receiver_name", ""),
        "receiver_date": fields.get("receiver_date", ""),
        "receiver_time": fields.get("receiver_time", ""),
        "page_images": rel_images,
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        sidecar.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log.debug("Wrote field sidecar: %s", sidecar.name)
    except OSError as exc:
        log.warning("Could not write sidecar %s: %s", sidecar.name, exc)


def upload_pod(pdf_path: Path, waybill: str, fields: dict[str, str]) -> str:
    """Step 7 (planned): upload the POD PDF to remote storage."""
    log.debug("Upload not yet implemented for %s", waybill)
    return "skipped"


def update_company_system(waybill: str, fields: dict[str, str], upload_status: str) -> str:
    """Step 8 (planned): push waybill status into your company's system."""
    log.debug("System update not yet implemented for %s", waybill)
    return "skipped"


def finalize_pod(result: PODResult) -> PODResult:
    """Post-save hook: optional receiver extraction and future upload/sync steps."""
    try:
        if EXTRACT_RECEIVER_FIELDS:
            page_images = render_pod_page_images(result.output_pdf, result.waybill_number)
            result.ocr_fields = extract_receiver_fields(
                result.waybill_number,
                page_images,
                result.output_pdf,
            )
            result.upload_status = upload_pod(
                result.output_pdf, result.waybill_number, result.ocr_fields
            )
            result.system_update_status = update_company_system(
                result.waybill_number, result.ocr_fields, result.upload_status or ""
            )
            log.info(
                "  [PIPELINE] %s — name:%s date:%s time:%s | upload:%s system:%s",
                result.waybill_number,
                result.ocr_fields.get("receiver_name", "—"),
                result.ocr_fields.get("receiver_date", "—"),
                result.ocr_fields.get("receiver_time", "—"),
                result.upload_status,
                result.system_update_status,
            )
        else:
            log.info(
                "  [DONE]   %s — %d page(s) → %s",
                result.waybill_number,
                result.page_count,
                result.output_pdf.name,
            )
    except Exception as exc:
        result.success = False
        result.error_message = str(exc)
        log.error("Post-processing failed for %s: %s", result.waybill_number, exc)

    return result


# ---------------------------------------------------------------------------
# File-settle guard
# ---------------------------------------------------------------------------

def wait_for_file_ready(path: Path) -> bool:
    """
    Poll the file size every FILE_SETTLE_INTERVAL seconds.
    Return True only when the size has been stable for FILE_SETTLE_CHECKS
    consecutive readings, meaning the scanner has finished writing.
    Kodak sometimes writes a temp file then renames it — allow brief disappearances.
    """
    log.info("Waiting for file to settle: %s", path.name)
    prev_size = -1
    stable_count = 0
    missing_count = 0

    for attempt in range(FILE_SETTLE_CHECKS * 10):  # hard cap
        time.sleep(FILE_SETTLE_INTERVAL)
        try:
            current_size = path.stat().st_size
            missing_count = 0
        except FileNotFoundError:
            missing_count += 1
            if missing_count <= 5:
                log.debug("File temporarily missing (Kodak write/rename?): %s", path.name)
                continue
            log.warning("File disappeared while waiting: %s", path)
            return False

        if current_size == prev_size and current_size > 0:
            stable_count += 1
            if stable_count >= FILE_SETTLE_CHECKS:
                log.info("File settled at %d bytes after %d checks.", current_size, attempt + 1)
                return True
        else:
            stable_count = 0
            prev_size = current_size

    log.error("File never settled (size kept changing): %s", path)
    return False


def batch_output_dir(input_path: Path) -> Path:
    """Create a dedicated subfolder under 2_Output for one batch's split PDFs."""
    base_name = ILLEGAL_CHARS_RE.sub("_", input_path.stem).strip("._ ") or "batch"
    out_dir = DIRS["output"] / base_name
    if out_dir.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = DIRS["output"] / f"{base_name}_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


# ---------------------------------------------------------------------------
# Core splitting engine
# ---------------------------------------------------------------------------

def process_batch(input_path: Path) -> None:
    """
    Main processing routine for a single batch PDF file.

    State machine:
      - active_waybill : str  — the waybill number currently being built
      - active_doc     : fitz.Document — the PDF being assembled for that waybill
    """
    log.info("=" * 70)
    log.info("Processing batch: %s", input_path.name)
    log.info("=" * 70)

    batch_out_dir = batch_output_dir(input_path)
    log.info("Batch output folder: %s", batch_out_dir)

    # Batch-level statistics
    total_pages       = 0
    saved_results: list[PODResult] = []
    error_pages:    list[int] = []

    # State machine variables
    active_waybill: Optional[str]         = None
    active_doc:     Optional[fitz.Document] = None
    active_page_count = 0

    def _save_active(waybill: str, doc: fitz.Document, pages: int) -> None:
        """Save the split PDF named after the waybill."""
        out_path = batch_out_dir / f"{waybill}.pdf"
        if out_path.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = batch_out_dir / f"{waybill}_{timestamp}.pdf"
            log.warning("Duplicate waybill %s — saving as %s", waybill, out_path.name)
        try:
            doc.save(str(out_path), garbage=4, deflate=True)
            doc.close()
            log.info("  [SAVED]  %s  (%d pages)", out_path.name, pages)

            result = PODResult(
                waybill_number=waybill,
                output_pdf=out_path,
                page_count=pages,
                source_batch=input_path.name,
            )
            saved_results.append(finalize_pod(result))
        except Exception as exc:
            log.error("Failed to save %s: %s", out_path, exc)
            doc.close()

    def _save_error_page(src_doc: fitz.Document, page_index: int, reason: str) -> None:
        """Copy a single unreadable page to 4_Errors for manual inspection."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        err_path = DIRS["errors"] / f"error_page{page_index + 1}_{timestamp}.pdf"
        try:
            err_doc = fitz.open()
            err_doc.insert_pdf(src_doc, from_page=page_index, to_page=page_index)
            err_doc.save(str(err_path), garbage=4, deflate=True)
            err_doc.close()
            error_pages.append(page_index + 1)
            log.warning("  [ERROR]  Page %d → %s  (%s)", page_index + 1, err_path.name, reason)
        except Exception as exc:
            log.error("Could not save error page %d: %s", page_index + 1, exc)

    # ------------------------------------------------------------------
    # Open the source batch document
    # ------------------------------------------------------------------
    try:
        src_doc = fitz.open(str(input_path))
    except Exception as exc:
        log.error("Cannot open PDF %s: %s", input_path, exc)
        _move_to_errors(input_path)
        return

    total_pages = src_doc.page_count
    log.info("Total pages in batch: %d", total_pages)

    # ------------------------------------------------------------------
    # Page-by-page loop
    # ------------------------------------------------------------------
    for page_index in range(total_pages):
        try:
            page = src_doc.load_page(page_index)
            page_num = page_index + 1
            log.debug("── Page %d / %d", page_num, total_pages)

            # Steps 1–2: find barcode and identify waybill
            waybill, source = identify_waybill(page)
            if waybill and source in ("ocr", "dsv", "dsv-ocr"):
                log.info("  Page %d: %s identified waybill %s", page_num, source, waybill)

            # ----------------------------------------------------------
            # State machine — step 3: determine document boundaries
            # ----------------------------------------------------------
            if waybill:
                # A new waybill starts on this page.
                if active_doc is not None and active_waybill is not None:
                    _save_active(active_waybill, active_doc, active_page_count)

                active_waybill = waybill
                active_doc = fitz.open()
                active_doc.insert_pdf(src_doc, from_page=page_index, to_page=page_index)
                active_page_count = 1
                log.info("  Page %d: [NEW WAYBILL] %s", page_num, waybill)

            else:
                # No barcode on this page — it's an attachment/invoice.
                if active_doc is not None:
                    active_doc.insert_pdf(src_doc, from_page=page_index, to_page=page_index)
                    active_page_count += 1
                    log.debug("  Page %d: appended to active waybill %s", page_num, active_waybill)
                else:
                    # No active container — orphan page, send to errors.
                    _save_error_page(src_doc, page_index, "no barcode and no active waybill")

        except Exception as exc:
            log.error("Unhandled error on page %d: %s", page_index + 1, exc)
            # Attempt to preserve the problematic page
            try:
                _save_error_page(src_doc, page_index, f"exception: {exc}")
            except Exception:
                pass  # already logged

    # ------------------------------------------------------------------
    # Flush the last active container
    # ------------------------------------------------------------------
    if active_doc is not None and active_waybill is not None:
        _save_active(active_waybill, active_doc, active_page_count)
    elif active_doc is not None:
        active_doc.close()

    src_doc.close()

    # ------------------------------------------------------------------
    # Housekeeping — archive or leave source in Kodak folder
    # ------------------------------------------------------------------
    mark_processed(input_path)
    if ARCHIVE_SOURCE:
        _relocate_batch(input_path, DIRS["archive"], "Batch archived")
    else:
        log.info("Source batch left in place: %s", input_path)

    # ------------------------------------------------------------------
    # Summary report
    # ------------------------------------------------------------------
    log.info("")
    log.info("─" * 70)
    log.info("BATCH COMPLETE: %s", input_path.name)
    log.info("  Output folder : %s", batch_out_dir)
    log.info("  Total pages   : %d", total_pages)
    log.info("  Waybills saved: %d", len(saved_results))
    for result in saved_results:
        log.info("    • %s (%d pages)", result.waybill_number, result.page_count)
    if error_pages:
        log.warning("  Error pages   : %s", error_pages)
    else:
        log.info("  Error pages   : none")
    log.info("─" * 70)
    log.info("")


def _move_to_errors(path: Path) -> None:
    """Move or copy a whole unprocessable file to 4_Errors."""
    _relocate_batch(path, DIRS["errors"], "Unprocessable file")


# ---------------------------------------------------------------------------
# Watchdog event handler
# ---------------------------------------------------------------------------

class BatchHandler(FileSystemEventHandler):
    """Respond to new .pdf files appearing in the watch folder."""

    def __init__(self) -> None:
        super().__init__()
        self._in_progress: set[str] = set()

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._handle(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        """Some software writes to a temp file then renames it."""
        if event.is_directory:
            return
        self._handle(Path(event.dest_path))

    def _handle(self, path: Path) -> None:
        if path.suffix.lower() != ".pdf":
            return
        if not is_under_watch_dir(path):
            return
        if is_processed(path):
            log.debug("Already processed, skipping: %s", path.name)
            return
        if str(path) in self._in_progress:
            return

        self._in_progress.add(str(path))
        log.info("Detected new file: %s", path.name)

        try:
            if not wait_for_file_ready(path):
                log.error("Skipping file that never settled: %s", path.name)
                _move_to_errors(path)
                return

            process_batch(path)

        except Exception as exc:
            log.critical("Fatal error while processing %s: %s", path.name, exc, exc_info=True)
            try:
                _move_to_errors(path)
            except Exception:
                pass
        finally:
            self._in_progress.discard(str(path))


# ---------------------------------------------------------------------------
# Startup scan — process any PDFs already sitting in Input at launch
# ---------------------------------------------------------------------------

def process_existing_files() -> None:
    existing = iter_watch_pdfs()
    if not existing:
        log.info("No unprocessed PDFs in watch folder.")
        return

    log.info("Found %d unprocessed PDF(s) in watch folder — processing now.", len(existing))
    for pdf in existing:
        log.info("Processing pre-existing file: %s", pdf.name)
        try:
            if not wait_for_file_ready(pdf):
                log.error("Pre-existing file never settled: %s", pdf.name)
                _move_to_errors(pdf)
                continue
            process_batch(pdf)
        except Exception as exc:
            log.critical("Fatal error on pre-existing file %s: %s", pdf.name, exc, exc_info=True)
            try:
                _move_to_errors(pdf)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("╔══════════════════════════════════════════════════════════════════╗")
    log.info("║         POD Batch Splitter — Kodak Alaris s2050                 ║")
    log.info("╚══════════════════════════════════════════════════════════════════╝")
    log.info("Started: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("App folder: %s", APP_ROOT)
    log.info("POD folder: %s", POD_ROOT)

    ensure_directories()
    load_settings()
    load_processed_log()
    configure_tesseract()

    if EXTRACT_RECEIVER_FIELDS:
        configure_openai()

    global TESSERACT_AVAILABLE
    TESSERACT_AVAILABLE = os.path.isfile(TESSERACT_PATH)

    log.info("Watch folder   : %s", WATCH_DIR)
    log.info("Watch recursive: %s", WATCH_RECURSIVE)
    log.info("Archive source : %s", ARCHIVE_SOURCE)
    log.info("Process existing: %s", PROCESS_EXISTING)

    if PROCESS_EXISTING:
        process_existing_files()

    # Start the watchdog observer
    event_handler = BatchHandler()
    observer = Observer()
    observer.schedule(event_handler, str(WATCH_DIR), recursive=WATCH_RECURSIVE)
    observer.start()
    log.info("Watching for new PDFs in: %s", WATCH_DIR)
    log.info("Split PDFs saved under: %s (one subfolder per batch)", DIRS["output"])
    log.info("Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)
            if not observer.is_alive():
                log.error("Observer thread died — restarting.")
                observer.stop()
                observer = Observer()
                observer.schedule(event_handler, str(WATCH_DIR), recursive=WATCH_RECURSIVE)
                observer.start()
    except KeyboardInterrupt:
        log.info("Shutdown requested by user.")
    finally:
        observer.stop()
        observer.join()
        log.info("POD Splitter stopped cleanly.")


if __name__ == "__main__":
    main()
