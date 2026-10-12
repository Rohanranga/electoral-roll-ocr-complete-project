import argparse, yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from pathlib import Path
from openpyxl import load_workbook
from .ocr import configure_tesseract, parse_fields, epic_candidates, select_voter_id, serial_header_candidate
from .extractor import extract_targets, extract_all
from .reconcile import reconcile, write_workbook
from .compare import compare_workbooks, build_dual_outputs
from .dashboard import serve_dashboard

def process_one(pdf_path, output_path, cfg, start_page=None, end_page=None):
    cfg=deepcopy(cfg)
    cfg.setdefault("diagnostics", {})["debug_dir"] = str(
        Path(output_path).resolve().parent / f"{Path(output_path).stem}_images"
    )
    targets=extract_targets(str(pdf_path),cfg)
    print(f"\n=== {Path(pdf_path).name} ===",flush=True)
    print("Page 1 targets:",{k:v for k,v in targets.items() if k.startswith("target_")},flush=True)
    if targets.get("target_total") is None:
        print("WARNING: could not confidently read official total from page 1; inspect workbook evidence.",flush=True)
    rows,diagnostics=extract_all(str(pdf_path),cfg,start_page,end_page)
    report,duplicates,missing,duplicate_ids,out_of_range=reconcile(rows,targets)
    write_workbook(output_path,rows,targets,report,duplicates,missing,duplicate_ids,diagnostics,out_of_range)
    print(f"Finished {Path(pdf_path).name}: {len(rows)} records -> {output_path}",flush=True)
    for r in report:
        print(f" - {r['check']}: target={r['target']} actual={r['actual']} status={r['status']}")
    return rows,report

def run_parallel_jobs(items, worker, max_workers):
    """Run independent PDFs concurrently and report each result as it finishes."""
    if max_workers <= 1:
        for item in items:
            try: worker(item)
            except Exception as e: print(f"FAILED {Path(item).name}: {e}",flush=True)
        return
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures={pool.submit(worker,item):item for item in items}
        for future in as_completed(futures):
            item=futures[future]
            try: future.result()
            except Exception as e: print(f"FAILED {Path(item).name}: {e}",flush=True)

def main():
    p=argparse.ArgumentParser(description="Electoral roll OCR with page-1 target reconciliation")
    sub=p.add_subparsers(dest="cmd",required=True)
    run=sub.add_parser("process",help="Process one PDF")
    run.add_argument("--pdf",required=True)
    run.add_argument("--output",required=True)
    run.add_argument("--config",default="config.yaml")
    run.add_argument("--ocr-engine",choices=("tesseract","rapidocr"),default=None)
    run.add_argument("--start-page",type=int,default=None)
    run.add_argument("--end-page",type=int,default=None)
    batch=sub.add_parser("process-folder",help="Process every PDF in a folder; each file gets its own page-1 target")
    batch.add_argument("--input-dir",default="data/input")
    batch.add_argument("--output-dir",default="data/output")
    batch.add_argument("--config",default="config.yaml")
    batch.add_argument("--ocr-engine",choices=("tesseract","rapidocr"),default=None)
    batch_workers=batch.add_mutually_exclusive_group()
    batch_workers.add_argument("--rapidocr-workers",type=int,default=None,help="Parallel voter pages within one PDF (RapidOCR only)")
    batch_workers.add_argument("--pdf-workers",type=int,default=None,help="Process multiple PDFs concurrently; each PDF uses one page worker")
    summary=sub.add_parser("summarize-folder",help="Summarize completed OCR workbooks for evaluation")
    summary.add_argument("--output-dir",default="data/output")
    dashboard=sub.add_parser("dashboard",help="Open a local interactive OCR review dashboard with source-card images")
    dashboard.add_argument("--workbook-dir",default="data/output",help="Folder containing *_ocr.xlsx workbooks and their _images folders")
    dashboard.add_argument("--host",default="127.0.0.1",help="Local bind address (default: 127.0.0.1)")
    dashboard.add_argument("--port",type=int,default=8765)
    repair=sub.add_parser("repair-folder",help="Fill only missing fields in existing OCR workbooks using saved raw OCR text")
    repair.add_argument("--input-dir",default="data/output")
    repair.add_argument("--output-dir",default="data/pattern_output")
    compare=sub.add_parser("compare-workbooks",help="Compare two engine workbooks and export differences for review")
    compare.add_argument("--baseline",required=True,help="Baseline workbook, typically Tesseract")
    compare.add_argument("--candidate",required=True,help="Candidate workbook, typically RapidOCR")
    compare.add_argument("--output",required=True)
    both=sub.add_parser("process-both",help="Run Tesseract and RapidOCR, then export all reads and consensus-review workbooks")
    both.add_argument("--input-dir",default="data/input")
    both.add_argument("--output-dir",default="data/both_engines")
    both.add_argument("--config",default="config.yaml")
    both_workers=both.add_mutually_exclusive_group()
    both_workers.add_argument("--rapidocr-workers",type=int,default=None,help="Parallel voter pages within one PDF (RapidOCR only)")
    both_workers.add_argument("--pdf-workers",type=int,default=None,help="Process multiple PDFs concurrently; each PDF uses one page worker")
    combine=sub.add_parser("combine-engines",help="Combine existing engine workbooks into unfiltered and consensus-review files")
    combine.add_argument("--tesseract-dir",required=True)
    combine.add_argument("--rapidocr-dir",required=True)
    combine.add_argument("--output-dir",default="data/combined_engines")
    args=p.parse_args()
    if args.cmd=="dashboard":
        serve_dashboard(args.workbook_dir,args.host,args.port)
        return
    if args.cmd=="compare-workbooks":
        records,differences=compare_workbooks(args.baseline,args.candidate,args.output)
        print(f"Compared {records} card positions; exported {differences} field differences to {args.output}")
        return
    if args.cmd=="combine-engines":
        left_dir=Path(args.tesseract_dir); right_dir=Path(args.rapidocr_dir); root=Path(args.output_dir)
        if not left_dir.exists() or not right_dir.exists(): raise FileNotFoundError("Both engine output directories must exist")
        files=sorted(x for x in left_dir.glob("*_ocr.xlsx") if not x.name.startswith("~$"))
        if not files: raise FileNotFoundError(f"No Tesseract workbooks found in {left_dir}")
        for left in files:
            right=right_dir/left.name
            if not right.exists():
                print(f"SKIP {left.name}: matching RapidOCR workbook not found",flush=True); continue
            full_path=root/"both_engines_unfiltered"/(left.stem+"_both_engines.xlsx")
            consensus_path=root/"final_candidates"/(left.stem+"_final_candidates.xlsx")
            records,review_count=build_dual_outputs(left,right,full_path,consensus_path)
            print(f"{left.name}: {records} positions; {review_count} field items require source review",flush=True)
        return
    if args.cmd=="summarize-folder":
        outdir=Path(args.output_dir)
        files=sorted(x for x in outdir.glob("*_ocr.xlsx") if not x.name.startswith("~$"))
        if not files:
            print(f"No completed OCR workbooks found in {outdir}. process-folder saves each workbook after that PDF finishes; wait for a 'Finished …' line, then run summarize-folder.")
            return
        for path in files:
            wb=load_workbook(path,read_only=True,data_only=True)
            voters=wb["Voters"]; vh=next(voters.values); vi={name:i for i,name in enumerate(vh)}
            voter_rows=list(voters.iter_rows(min_row=2,values_only=True))
            report=list(wb["Reconciliation Report"].iter_rows(min_row=2,values_only=True))
            checks={str(row[0]):row for row in report}
            target=checks.get("Total electors",(None,None,None,None,None))
            def count(check):
                row=checks.get(check)
                return row[2] if row else "?"
            diagnostic_sheet=wb["Page Diagnostics"]
            diagnostic_header=next(diagnostic_sheet.values)
            error_index=diagnostic_header.index("Processing error")
            diagnostics=list(diagnostic_sheet.iter_rows(min_row=2,values_only=True))
            errors=sum(bool(row[error_index]) for row in diagnostics)
            review=max(0,wb["Review Required"].max_row-1)
            serials=sum(row[vi["serial_no"]] is not None for row in voter_rows)
            ids=sum(row[vi["voter_id"]] is not None for row in voter_rows)
            coverage={key:sum(row[vi[key]] is not None for row in voter_rows) for key in ("serial_no","voter_id","name","relation_name","house_no","age","gender") if key in vi}
            duration=sum(float(row[diagnostic_header.index("Duration seconds")] or 0) for row in diagnostics)
            print(f"\n{path.name}")
            print(f"  total: {target[2]}/{target[1]} [{target[4]}]")
            print(f"  gender male/female/third: {count('Male electors')}/{count('Female electors')}/{count('Third-gender electors')}")
            print(f"  records: {len(voter_rows)}; serial values: {serials}; voter IDs populated (unverified): {ids}; review rows: {review}")
            print("  field coverage: "+", ".join(f"{key}={value}/{len(voter_rows)} ({value/max(1,len(voter_rows)):.1%})" for key,value in coverage.items()))
            print(f"  pages: {len(diagnostics)}; page errors: {errors}; summed page time: {duration:.1f}s")
            wb.close()
        return
    if args.cmd=="repair-folder":
        indir=Path(args.input_dir); outdir=Path(args.output_dir)
        files=sorted(x for x in indir.glob("*_ocr.xlsx") if not x.name.startswith("~$"))
        if not files: raise FileNotFoundError(f"No OCR workbooks found in {indir}")
        outdir.mkdir(parents=True,exist_ok=True)
        for path in files:
            wb=load_workbook(path,data_only=True)
            voters=wb["Voters"]; header=next(voters.values); rows=[dict(zip(header,row)) for row in voters.iter_rows(min_row=2,values_only=True)]
            if "source_image" in header:
                image_col=header.index("source_image")+1
                for row_index,row in enumerate(rows,start=2):
                    link=voters.cell(row_index,image_col).hyperlink
                    if link and link.target:
                        target=Path(link.target)
                        row["source_image"]=str(target if target.is_absolute() else (path.parent/target).resolve())
            targets={key:value for key,value in wb["Target Evidence"].iter_rows(min_row=2,values_only=True) if key and str(key).startswith("target_")}
            diag_sheet=wb["Page Diagnostics"]; dh=next(diag_sheet.values)
            diagnostics=[dict(zip(dh,row)) for row in diag_sheet.iter_rows(min_row=2,values_only=True)]
            wb.close()
            filled={k:0 for k in ("serial_no","name","relation_type","relation_name","house_no","age","gender")}
            for row in rows:
                parsed=parse_fields(row.get("raw_text") or "")
                if not row.get("serial_no"):
                    header_serial=serial_header_candidate(row.get("raw_text") or "")
                    serial_end=targets.get("target_end_serial")
                    if header_serial is not None and (serial_end is None or header_serial<=int(serial_end)):
                        row["serial_no"]=header_serial
                        row["serial_status"]="CANDIDATE_REQUIRES_VERIFICATION"
                        serial_options=set(filter(None,(row.get("serial_candidates") or "").split("; ")))
                        serial_options.add(str(header_serial))
                        row["serial_candidates"]="; ".join(sorted(serial_options,key=lambda value:int(value) if value.isdigit() else -1))
                        filled["serial_no"]+=1
                for key in filled:
                    if not row.get(key) and parsed.get(key) is not None:
                        row[key]=parsed[key]; filled[key]+=1
                raw=row.get("raw_text") or ""; targeted=row.get("voter_id_raw") or ""
                candidates=sorted(set((row.get("voter_id_candidates") or "").split("; "))-{""}|set(epic_candidates(raw))|set(epic_candidates(targeted)))
                selected,reason=select_voter_id(raw,targeted,candidates)
                row["voter_id"]=selected
                row["voter_id_status"]="CANDIDATE_REQUIRES_VERIFICATION" if selected else ("AMBIGUOUS" if reason=="AMBIGUOUS" else "NOT_FOUND")
                row["voter_id_selection"]=reason
                row["voter_id_candidates"]="; ".join(candidates)
                gc=sorted(set((row.get("gender_candidates") or "").split("; "))-{""})
                if row.get("gender"): gc=sorted(set(gc+[row["gender"]]))
                row["gender_candidates"]="; ".join(gc)
            report,duplicates,missing,duplicate_ids,out_of_range=reconcile(rows,targets)
            converted=[{"page":d.get("Page"),"detected":d.get("Voter cards accepted",0),"grid_candidates":d.get("Inked grid slots",0),"failed_fields":d.get("Failed field count",0),"error":d.get("Processing error") or "","duration_seconds":d.get("Duration seconds",0)} for d in diagnostics]
            out=outdir/path.name
            write_workbook(out,rows,targets,report,duplicates,missing,duplicate_ids,converted,out_of_range)
            print(f"Repaired {path.name} -> {out}; filled missing fields: {filled}; selected voter IDs: {sum(bool(r.get('voter_id')) for r in rows)}")
        return
    cfg=yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    configure_tesseract(cfg["ocr"].get("tesseract_cmd"))
    if getattr(args,"ocr_engine",None): cfg["ocr"]["engine"]=args.ocr_engine
    if args.cmd in ("process-folder","process-both") and args.rapidocr_workers is not None:
        cfg["processing"]["rapidocr_workers"]=max(1,args.rapidocr_workers)
    if args.cmd in ("process-folder","process-both") and args.pdf_workers is not None:
        cfg["processing"]["pdf_workers"]=max(1,args.pdf_workers)
    if args.cmd=="process-both":
        indir=Path(args.input_dir); root=Path(args.output_dir)
        if not indir.exists(): raise FileNotFoundError(f"Input folder not found: {indir}")
        pdfs=sorted(x for x in indir.iterdir() if x.is_file() and x.suffix.lower()==".pdf")
        if not pdfs: raise FileNotFoundError(f"No PDF files found in {indir}")
        tess_dir=root/"tesseract"; rapid_dir=root/"rapidocr"
        all_dir=root/"both_engines_unfiltered"; settled_dir=root/"final_candidates"
        pdf_workers=max(1,min(int(cfg["processing"].get("pdf_workers",1)),len(pdfs)))
        job_cfg=deepcopy(cfg)
        if pdf_workers>1:
            # Keep total OCR engine instances bounded by the number of files
            # being processed instead of nesting page and PDF worker pools.
            job_cfg["processing"]["max_workers"]=1
            job_cfg["processing"]["max_workers_limit"]=1
            job_cfg["processing"]["rapidocr_workers"]=1
        if pdf_workers>1: print(f"Processing {len(pdfs)} PDFs with {pdf_workers} PDF workers and one page worker per PDF",flush=True)
        def process_both_pdf(pdf):
            outputs={}
            for engine,folder in (("tesseract",tess_dir),("rapidocr",rapid_dir)):
                engine_cfg=deepcopy(job_cfg); engine_cfg["ocr"]["engine"]=engine
                out=folder/(pdf.stem+"_ocr.xlsx")
                process_one(str(pdf),str(out),engine_cfg)
                outputs[engine]=out
            all_path=all_dir/(pdf.stem+"_both_engines.xlsx")
            consensus_path=settled_dir/(pdf.stem+"_final_candidates.xlsx")
            records,review_count=build_dual_outputs(outputs["tesseract"],outputs["rapidocr"],all_path,consensus_path)
            print(f"Combined {records} card positions; {review_count} field items require source review",flush=True)
        run_parallel_jobs(pdfs,process_both_pdf,pdf_workers)
        return
    if args.cmd=="process":
        if not Path(args.pdf).exists(): raise FileNotFoundError(args.pdf)
        process_one(args.pdf,args.output,cfg,args.start_page,args.end_page)
    else:
        indir=Path(args.input_dir); outdir=Path(args.output_dir)
        if not indir.exists(): raise FileNotFoundError(f"Input folder not found: {indir}")
        pdfs=sorted([x for x in indir.iterdir() if x.is_file() and x.suffix.lower()==".pdf"])
        if not pdfs: raise FileNotFoundError(f"No PDF files found in {indir}")
        outdir.mkdir(parents=True,exist_ok=True)
        pdf_workers=max(1,min(int(cfg["processing"].get("pdf_workers",1)),len(pdfs)))
        job_cfg=deepcopy(cfg)
        if pdf_workers>1:
            # Bound RapidOCR's ONNX sessions and avoid nested oversubscription.
            job_cfg["processing"]["max_workers"]=1
            job_cfg["processing"]["max_workers_limit"]=1
            job_cfg["processing"]["rapidocr_workers"]=1
            print(f"Processing {len(pdfs)} PDFs with {pdf_workers} PDF workers and one page worker per PDF",flush=True)
        if args.ocr_engine == "rapidocr" or cfg["ocr"].get("engine","tesseract").lower()=="rapidocr":
            if pdf_workers==1:
                print(f"RapidOCR page workers: {job_cfg['processing'].get('rapidocr_workers', job_cfg['processing'].get('max_workers', 1))}; intra/inter-op threads: {job_cfg['processing'].get('rapidocr_intra_op_threads',2)}/{job_cfg['processing'].get('rapidocr_inter_op_threads',1)}",flush=True)
        print(f"Found {len(pdfs)} PDFs. Each PDF will read its own target from page 1.",flush=True)
        def process_folder_pdf(pdf):
            out=outdir/(pdf.stem+"_ocr.xlsx")
            process_one(str(pdf),str(out),job_cfg)
        run_parallel_jobs(pdfs,process_folder_pdf,pdf_workers)
if __name__=="__main__": main()
