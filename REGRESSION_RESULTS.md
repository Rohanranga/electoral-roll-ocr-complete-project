# Regression results

Rerun on 2026-10-10 with Python 3.12, the four supplied PDFs, and four page workers. The new workbooks are in `data/fixed_output/`; prior workbooks were not overwritten. All four PDF targets were reread from their first-page elector table. There were no page-processing errors.

| Part | Target | Records | Male / target | Female / target | Third / target | Serial values | Missing serials | Duplicate serials | Out of range | EPIC candidates | Missing EPIC | Missing name / age / gender | Review rows | Pages |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 3 | 652 | 652 | 306 / 327 | 320 / 325 | 0 / 0 | 81 | 580 | 5 | 3 | 527 | 125 | 43 / 41 / 26 | 652 | 25 |
| 5 | 643 | 643 | 295 / 298 | 340 / 345 | 0 / 0 | 92 | 562 | 8 | 0 | 530 | 113 | 34 / 36 / 8 | 643 | 26 |
| 7 | 672 | 672 | 321 / 329 | 335 / 343 | 0 / 0 | 80 | 597 | 4 | 0 | 545 | 127 | 45 / 47 / 16 | 672 | 28 |
| 10 | 821 | 821 | 402 / 413 | 401 / 408 | 0 / 0 | 100 | 738 | 10 | 1 | 665 | 156 | 49 / 50 / 18 | 821 | 31 |

The totals now match all four source targets. This does not verify record-level correctness: all rows remain in `Review Required`, and every workbook has field-level reconciliation failures. Serial OCR is still a major limitation. The code now leaves conflicting serial reads blank and stores the alternatives, which reduces false assignments but leaves many serials for manual review. OCR-confusion EPIC alternatives are also candidates only; verify them visually against the PDF before use.

## Changes in this run

- The serial crop now covers the printed number area, sweeps several vertical positions to handle crop alignment, and retains disagreement instead of selecting a number by majority. Serial numbers are never inferred from the card's grid position.
- Voter-ID crop includes the full top margin. Plausible O/0, I/1, S/5, B/8, and similar confusion candidates are listed alongside the untouched raw OCR evidence; they are not marked verified.
- A separate crop reads the gender line when full-card OCR misses it.
- Page diagnostics report accepted voter cards separately from inked grid slots. The map pages in these PDFs now show zero voter cards instead of appearing as candidate records.
- Reconciliation reports missing voter IDs and serials outside the expected range.
- `python -m app.cli summarize-folder` prints the key workbook metrics for evaluation.

Automated tests: 11 passed with `python -m unittest discover -s tests -v`; `compileall` passed. No manual verification of every elector card was performed. Review the source PDF alongside `Voters`, `Review Required`, `Serial Review`, and `Target Evidence`.
