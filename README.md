# Electoral Roll OCR

Traceable OCR extraction for scanned electoral-roll PDFs. RapidOCR is the configured default for voter cards and uses CPU ONNX Runtime. Tesseract remains available and reads page-one targets for reconciliation. OCR results remain unverified candidates and uncertain fields are included in the review sheet.

## Windows setup (Python 3.12)

Install Tesseract OCR and confirm its executable path. Edit `ocr.tesseract_cmd` in `config.yaml` if it is not at `C:/Program Files/Tesseract-OCR/tesseract.exe`.

```powershell
py -3.12 -m venv .venv312
.\.venv312\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Set `ocr.tesseract_cmd` in `config.yaml` to the path of your local `tesseract.exe` before running OCR. Set `ocr.engine` to `tesseract` to use Tesseract for voter cards, or override it on a command with `--ocr-engine rapidocr` or `--ocr-engine tesseract`.

## Batch execution

```powershell
python -m app.cli process-folder --input-dir "data/input" --output-dir "data/fixed_output"
python -m app.cli summarize-folder --output-dir "data/fixed_output"
```

Use a new output directory when the original workbooks are open in Excel or should be preserved. The summary prints targets versus extracted counts, field coverage, review rows, and page errors.

To reuse the raw text already stored in existing OCR workbooks and fill only blank fields when their labels and values are clear, run:

```powershell
python -m app.cli repair-folder --input-dir "data/fixed_output" --output-dir "data/pattern_output"
python -m app.cli summarize-folder --output-dir "data/pattern_output"
```

This reuses the saved raw text, tolerates OCR punctuation substitutions around field labels, and reads serials when they appear immediately before the EPIC number in the card header. It uses explicit patterns, not a connected external LLM. It keeps absent IDs blank, records alternate ID readings for review, and marks selected IDs and header serials as unverified candidates. It fills only blank fields and does not overwrite already-filled person fields.

Or process one file:

```powershell
python -m app.cli process --pdf "data/input/roll.pdf" --output "data/output/roll_ocr.xlsx"
```

Page range overrides are available with `--start-page` and `--end-page`. The default starts at page 2 and evaluates pages through the end. `grid.cards_bottom` is the bottom margin ratio; the supplied PDFs use 0.035 so the grid covers nearly the full page. Tune the grid for other layouts. For Tesseract, `processing.max_workers` configures page parallelism, capped by `max_workers_limit`. RapidOCR uses one page worker by default to avoid duplicating ONNX model memory. `diagnostics.debug_dir` enables saved card crops with PDF/page/row/column filenames.

## Workbook

Each PDF produces a workbook with `Voters`, `Reconciliation Report`, `Serial Review`, `Review Required`, `Page Diagnostics`, and `Target Evidence`. The first-page OCR text and elector-table OCR candidates are retained. A target that cannot be read is reported as `TARGET_EXTRACTION_FAILED`; no page numbers are substituted as a guessed total. Voter-ID character-confusion alternatives are listed as candidates beside the untouched raw OCR text. Serial reads that disagree across crop views are left blank and listed as alternatives. No OCR match is marked as verified. Page diagnostics separate accepted voter cards from inked grid slots, so maps and tables do not appear as extracted voter rows.

## Known limits

- Card and field OCR remains imperfect, especially serial numbers with small or clipped digits. The pipeline preserves uncertain alternatives instead of resolving them from card position. The annotated crops are saved in `data/card_debug/`.
- EPIC candidates generated from O/0, I/1, S/5, and similar confusions remain unverified; compare them with the source card before using them.
- Matching totals do not establish record-level correctness. Review the original PDF alongside `Review Required`, `Voters`, and `Serial Review`.
# OCR engines

RapidOCR is the configured default as a CPU-only ONNX Runtime engine, and reads
each voter card in one pass. It uses two page workers with capped ONNX thread
counts by default; adjust `processing.rapidocr_workers` in `config.yaml` based
on available memory. Tesseract remains available with
`--ocr-engine tesseract`; page-one target extraction continues to use Tesseract.
Install RapidOCR with `python -m pip install -r requirements.txt`. On these
four PDFs it substantially improved field completeness and reduced summed
page processing time in the initial one-worker run. A clean four-page benchmark
completed about one-third faster with two RapidOCR workers than with one, using
about 0.94 GB of memory on this machine. More workers increased memory and did
not finish the sample promptly, so four workers are not the default.

Example full-folder comparison (use separate output folders):

```powershell
python -m app.cli process-folder --input-dir data/input --output-dir data/tesseract_output --ocr-engine tesseract
python -m app.cli process-folder --input-dir data/input --output-dir data/rapidocr_output --ocr-engine rapidocr
python -m app.cli summarize-folder --output-dir data/tesseract_output
python -m app.cli summarize-folder --output-dir data/rapidocr_output
python -m app.cli compare-workbooks --baseline data/tesseract_output/NAME_ocr.xlsx --candidate data/rapidocr_output/NAME_ocr.xlsx --output data/engine_comparison.xlsx
```

The comparison workbook reports agreements, disagreements, and fields populated
by only one engine. These are OCR-to-OCR comparisons, not accuracy scores;
review the listed differences against the source PDF.

To process every input PDF through both engines and create a full unfiltered
side-by-side workbook plus a separate best-available/consensus review workbook
for each PDF:

```powershell
python -m app.cli process-both --input-dir data/input --output-dir data/both_engines --rapidocr-workers 2
```

To generate those two outputs from engine workbooks already on disk without
rerunning OCR:

```powershell
python -m app.cli combine-engines --tesseract-dir data/rerun_output --rapidocr-dir data/rapidocr_output --output-dir data/combined_engines
```

The unfiltered workbook retains every card row, both engines' field values, and
both raw OCR texts. The best-available workbook prefers RapidOCR and falls back
to Tesseract when RapidOCR has no value; each field includes a status. The
`Consensus Only` sheet includes only matching non-empty readings. All other
values appear in the review queue. Engine agreement is not proof of truth, so
check the source PDF before treating the candidate workbook as final.

