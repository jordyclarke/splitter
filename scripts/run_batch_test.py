#!/usr/bin/env python3
"""
Dev-only: merge PDFs from a folder into one batch and run the split pipeline.

Usage (from project root):
    python scripts/run_batch_test.py /path/to/pdf/folder
    python scripts/run_batch_test.py rename.pdf   # single file
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pod_splitter as ps  # noqa: E402


def merge_pdfs(sources: list[Path], output: Path) -> int:
    merged = fitz.open()
    for src in sources:
        doc = fitz.open(src)
        merged.insert_pdf(doc)
        doc.close()
        print(f"  + {src.name} ({merged.page_count} pages total so far)")
    page_count = merged.page_count
    merged.save(str(output), garbage=4, deflate=True)
    merged.close()
    return page_count


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/run_batch_test.py /path/to/pdf/or/folder")
        sys.exit(1)

    source = Path(sys.argv[1]).expanduser().resolve()
    if source.is_file() and source.suffix.lower() == ".pdf":
        sources = [source]
    elif source.is_dir():
        sources = sorted(source.glob("*.pdf"))
    else:
        print(f"Not a PDF file or folder: {source}")
        sys.exit(1)

    if not sources:
        print(f"No PDFs found in {source}")
        sys.exit(1)

    test_root = ROOT / "test_run"
    ps.POD_ROOT = test_root
    ps.DIRS = {
        "input": test_root / "1_Input",
        "output": test_root / "2_Output",
        "archive": test_root / "3_Archive",
        "errors": test_root / "4_Errors",
    }
    ps.LOG_FILE = test_root / "processing_log.txt"

    for path in ps.DIRS.values():
        path.mkdir(parents=True, exist_ok=True)

    batch_name = f"BATCH_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    batch_path = ps.DIRS["input"] / batch_name

    print(f"Merging {len(sources)} PDF(s) into {batch_name}...")
    page_count = merge_pdfs(sources, batch_path)
    print(f"Batch ready: {page_count} pages → {batch_path}")

    ps.configure_tesseract()
    ps.TESSERACT_AVAILABLE = __import__("os").path.isfile(ps.TESSERACT_PATH)

    print("\nRunning split pipeline...")
    ps.process_batch(batch_path)

    outputs = sorted(ps.DIRS["output"].rglob("*.pdf"))
    print(f"\nDone. {len(outputs)} waybill PDF(s) under {ps.DIRS['output']}:")
    for pdf in outputs:
        doc = fitz.open(pdf)
        pages = doc.page_count
        doc.close()
        print(f"  • {pdf.name}  ({pages} page(s))")


if __name__ == "__main__":
    main()
