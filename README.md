# POD Batch Splitter

Splits Kodak scan batches into individual waybill PDFs named `{waybill}.pdf`.

## What it does

1. Watches a folder for multi-page PDF batches (default `POD_System/1_Input/`, or Kodak output via `settings.ini`)
2. Detects waybill barcodes (LDLS, BIC, AFS, CLP, etc.)
3. Appends invoice pages (no barcode) to the active waybill
4. Saves `{waybill}.pdf` to `POD_System/2_Output/`
5. Optionally archives the original batch to `POD_System/3_Archive/`

## Branch deployment (no Python required)

1. Download **POD_Splitter-Windows.zip** from [GitHub Actions](../../actions) or Releases
2. Unzip to e.g. `D:\POD_Splitter\`
3. Copy `settings.ini.example` → `settings.ini` and set your Kodak PODS folder
4. Run **Start POD Splitter.bat** — leave the window open while scanning

No Kodak Capture Pro changes needed when using `settings.ini` (recursive watch, originals left in place).

See `packaging/README.txt` for branch instructions.

## Development (Mac / Linux)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Mac only — zbar for pyzbar:
brew install zbar
export DYLD_LIBRARY_PATH="/opt/homebrew/lib:$DYLD_LIBRARY_PATH"

python pod_splitter.py
```

### Test a batch locally

```bash
python scripts/run_batch_test.py /path/to/pdfs/
python scripts/run_batch_test.py rename.pdf
```

Output goes to `test_run/` (gitignored).

## Build Windows `.exe` (GitHub Actions)

Push a version tag to trigger a release build:

```bash
git tag v1.0.0
git push origin v1.0.0
```

Or run **Build Windows Release** manually from the Actions tab (`workflow_dispatch`).

The workflow uploads `POD_Splitter-Windows.zip` as an artifact (and attaches it to Releases for tags).

## Project layout

```
pod_splitter.py          Main application
pod_splitter.spec        PyInstaller config (Windows)
packaging/               Branch README + start script
scripts/run_batch_test.py   Dev test runner
env/.env.example         Optional config (AI extraction disabled)
.github/workflows/       CI build
```

## Kodak Capture Pro

**Recommended:** copy `settings.ini.example` to `settings.ini` and set:

```ini
[watch]
folder = C:\Users\...\SCANNING\POD SCANS\PODS
recursive = true

[processing]
archive_source = false
process_existing = false
```

The splitter watches Kodak's existing dated output folders and leaves originals in place.

| Setting | Value (legacy mode) |
|---|---|
| Output format | PDF |
| Output folder | `…\POD_System\1_Input` |
| One PDF per batch | Yes |

## Cost

Split-only mode (current): **$0** — runs locally, no API calls.
