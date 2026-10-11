"""Traceable electoral-roll extraction. OCR values remain candidates until reviewed."""
import re, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import pymupdf
from PIL import Image
from .ocr import ocr_image, rapidocr_image, parse_fields, epic_candidates, select_voter_id, serial_header_candidate

def render_page(pdf_path, page_no, dpi):
    with pymupdf.open(pdf_path) as doc:
        pix=doc[page_no-1].get_pixmap(matrix=pymupdf.Matrix(dpi/72,dpi/72),colorspace=pymupdf.csRGB,alpha=False)
    # Avoid encoding every rendered page as PNG only to decode it back into PIL.
    return Image.frombytes('RGB',(pix.width,pix.height),pix.samples)

def extract_targets(pdf_path,cfg):
    im=render_page(pdf_path,1,int(cfg['ocr']['page1_dpi']))
    txt=ocr_image(im,cfg['ocr'].get('languages','eng'),psm=6,scale=1)
    table_cfg=cfg.get('validation',{}).get('target_table',{})
    table_im=render_page(pdf_path,1,int(table_cfg.get('dpi',120))); w,h=table_im.size
    # Read the labeled elector table and its cells only, excluding dates and page numbers.
    table_crop=table_im.crop((int(w*.03),int(h*.70),int(w*.98),int(h*.86)))
    table_txt=ocr_image(table_crop,cfg['ocr'].get('languages','eng'),psm=11,scale=2)
    y0,y1=table_cfg.get('values_y',[.80,.84]); col_cfg=table_cfg.get('columns',{})
    order={'starting_serial':'target_start_serial','ending_serial':'target_end_serial','male':'target_male',
           'female':'target_female','third_gender':'target_third_gender','total':'target_total'}
    values={}; value_evidence={}
    for key,outkey in order.items():
        x0,x1=col_cfg.get(key,[0,0]); cell=table_im.crop((int(w*x0),int(h*y0),int(w*x1),int(h*y1)))
        inset=int(cell.width*.08); cell=cell.crop((inset,0,cell.width-inset,cell.height))
        reads=[]
        for psm,threshold in ((7,False),(7,True),(10,False)):
            raw=ocr_image(cell,'eng',psm=psm,scale=4,whitelist='0123456789',threshold=threshold)
            nums=[int(x) for x in re.findall(r'\d{1,5}',raw)]
            if len(nums)==1: reads.append(nums[0])
        if key=='third_gender':
            for scale in (2,3,4):
                raw=ocr_image(cell,'eng',psm=6,scale=scale)
                if raw.strip()=='0': reads.append(0)
        elif key=='starting_serial':
            for scale in (2,3):
                raw=ocr_image(cell,'eng',psm=6,scale=scale)
                if raw.strip()=='1': reads.append(1)
        counts=Counter(reads); candidate,count=counts.most_common(1)[0] if counts else (None,0)
        values[outkey]=candidate if count>=2 else None
        value_evidence[key]={'candidates':reads,'accepted':values[outkey]}
    male=values['target_male']; female=values['target_female']; third=values['target_third_gender']; total=values['target_total']
    start=values['target_start_serial']; end=values['target_end_serial']
    # Use the labeled gender columns as an internal checksum. Preserve the
    # conflicting OCR read in evidence, while exposing the consistent value.
    if all(v is not None and v >= 0 for v in (male,female,third)):
        gender_sum=male+female+third
        if gender_sum > 0 and total != gender_sum:
            value_evidence['total']['selected_from']='male+female+third_gender'
            value_evidence['total']['ocr_accepted']=total
            value_evidence['total']['accepted']=gender_sum
            value_evidence['total']['consistency_correction']=True
            total=gender_sum
        if start == 1 and total is not None and end != total:
            value_evidence['ending_serial']['selected_from']='starting_serial_plus_total_minus_one'
            value_evidence['ending_serial']['ocr_accepted']=end
            value_evidence['ending_serial']['accepted']=total
            value_evidence['ending_serial']['consistency_correction']=True
            end=total
    values['target_total']=total
    values['target_end_serial']=end
    return {'target_total':total,'target_male':male,'target_female':female,'target_third_gender':third,
            'target_start_serial':start,'target_end_serial':end,'page1_ocr_text':txt,
            'target_table_ocr_text':table_txt,'target_value_evidence':value_evidence,
            'target_status':'READ' if total is not None else 'TARGET_EXTRACTION_FAILED',
            'target_evidence':{k:v for k,v in [('male',male),('female',female),('third_gender',third),('total',total),('start_serial',start),('end_serial',end)]}}

def parse_target_text(txt):
    flat=re.sub(r'\s+',' ',txt or '')
    def val(label):
        m=re.search(r'\b'+label+r'\b\s*[:|]?\s*(\d{1,5})',flat,re.I)
        return int(m.group(1)) if m else None
    # Explicitly adjacent labels only: no fallback to unrelated numbers on page 1.
    total=val('Total'); male=val('Male'); female=val('Female'); third=val(r'Third\s+Gender')
    start=val(r'Starting\s+Serial\s+No'); end=val(r'Ending\s+Serial\s+No')
    return {'target_total':total,'target_male':male,'target_female':female,'target_third_gender':third,'target_start_serial':start,'target_end_serial':end}

def boxes(im,cfg):
    g=cfg['grid']; cols=int(g['columns']); rows=int(g['rows'])
    x0=int(im.width*float(g['left_margin'])); x1=int(im.width*(1-float(g['right_margin'])))
    y0=int(im.height*float(g['cards_top'])); y1=int(im.height*(1-float(g['cards_bottom'])))
    cw=(x1-x0)/cols; ch=(y1-y0)/rows; px=float(g.get('card_inner_padding_x',0)); py=float(g.get('card_inner_padding_y',0))
    for r in range(rows):
        for c in range(cols):
            l=x0+c*cw; t=y0+r*ch
            yield r+1,c+1,tuple(map(int,(l+cw*px,t+ch*py,l+cw*(1-px),t+ch*(1-py))))

def looks_like_card(card):
    # Ignore border and require both sufficient ink and OCR-like text presence.
    w,h=card.size
    if w<30 or h<30:return False
    inner=card.crop((int(w*.04),int(h*.06),int(w*.96),int(h*.9))).convert('L')
    dark=sum(inner.histogram()[:190])/(inner.width*inner.height)
    return dark>.025

def has_voter_card_evidence(raw, candidates):
    """Reject inked summary/table cells unless OCR has voter-card structure."""
    s=(raw or '').lower()
    signals=sum(bool(x) for x in (re.search(r'\bname\s*:',s),re.search(r'\bage\s*:',s),re.search(r'\b(?:father|husband|mother)\s+name\s*:',s),re.search(r'\bhouse\s+(?:number|no)\s*:',s),candidates))
    return signals>=2

def serial_from_card(card,full_text,crop_ratio=None,crop_variants=(),extended_bottom=.28,bottom_row=False,vertical_windows=()):
    # Use explicitly labeled full-card text only as one source. The printed
    # serial itself is in a box at the card's upper left; the old crop
    # (.22-.32 of the card) clipped the digits against the box edge.
    m=re.search(r'(?:Serial\s*(?:No\.?|Number)?\s*[:#]?\s*)(\d{1,4})',full_text,re.I)
    reads=[]
    if m: reads.append(int(m.group(1)))
    w,h=card.size; x0,y0,x1,y1=crop_ratio or (.20,.02,.31,.15)
    # Most rows place the serial near the top of the grid cell. The last row
    # can be shifted down by the footer; adjust only the crop location there.
    # Read a tight and a wider horizontal crop. Any disagreement remains a
    # review candidate rather than being resolved by majority guessing.
    regions=[(x0,y0,x1,y1),*crop_variants]
    for rx0,ry0,rx1,ry1 in regions:
        if bottom_row:
            vertical_regions=((max(ry0,.12),max(ry1,extended_bottom)),)
        else:
            vertical_regions=((ry0,ry1),)
        for top,bottom in vertical_regions:
            crop=card.crop((int(w*rx0),int(h*top),int(w*rx1),int(h*bottom)))
            for psm,scale in ((6,5),(7,5)):
                txt=ocr_image(crop,'eng',psm=psm,scale=scale,whitelist='0123456789')
                reads += [int(x) for x in re.findall(r'\d{1,4}',txt)]
    # The fixed grid cell can start a little above or below the actual card
    # border. Sweep the serial-box band vertically so a crop does not clip the
    # number on later rows. These are OCR reads from the card image, never grid
    # position substitutions.
    for top in vertical_windows:
        bottom=min(1.0,float(top)+.13)
        crop=card.crop((int(w*.02),int(h*float(top)),int(w*.33),int(h*bottom)))
        txt=ocr_image(crop,'eng',psm=7,scale=5,whitelist='0123456789')
        reads += [int(x) for x in re.findall(r'\d{1,4}',txt)]
    valid=[x for x in reads if 1<=x<=9999]
    header_serial=serial_header_candidate(full_text)
    if header_serial is not None:
        evidence=sorted(set(valid+[header_serial]))
        return header_serial,'CANDIDATE_REQUIRES_VERIFICATION','; '.join(map(str,evidence))
    counts=Counter(valid)
    distinct=sorted(set(valid))
    consensus=distinct[0] if len(distinct)==1 else None
    status='NOT_FOUND' if not valid else ('CANDIDATE_REQUIRES_VERIFICATION' if consensus is not None else 'AMBIGUOUS')
    return consensus,status,'; '.join(map(str,sorted(set(valid))))

def gender_candidates(text):
    """Extract only explicit gender words, retaining all distinct readings."""
    values=[]
    for match in re.finditer(r'\b(Male|Female|Third\s+Gender)\b',text or '',re.I):
        value=re.sub(r'\s+',' ',match.group(1)).title()
        values.append('Third Gender' if value.lower()=='third gender' else value)
    return sorted(set(values))

def serial_candidates(text):
    """Return only serials explicitly labeled in OCR text; no grid inference."""
    return [int(m.group(1)) for m in re.finditer(r'(?:Serial\s*(?:No\.?|Number)?\s*[:#]?\s*)(\d{1,4})',text or '',re.I)]

def process_page(args):
    pdf_path,page_no,cfg=args; started=time.perf_counter(); im=render_page(pdf_path,page_no,int(cfg['ocr']['card_dpi']))
    out=[]; grid_candidates=0; debug=cfg.get('diagnostics',{}).get('debug_dir')
    for row,col,box in boxes(im,cfg):
        card=im.crop(box)
        if not looks_like_card(card):continue
        grid_candidates+=1
        engine=cfg['ocr'].get('engine','tesseract').lower()
        raw=rapidocr_image(card,
                           int(cfg['processing'].get('rapidocr_intra_op_threads',2)),
                           int(cfg['processing'].get('rapidocr_inter_op_threads',1))) if engine=='rapidocr' else ocr_image(card,cfg['ocr'].get('languages','eng'),int(cfg['ocr'].get('psm_card',6)),scale=1)
        fields=parse_fields(raw); id_candidates=[]
        # Independent top-right targeted crop; preserve raw candidate evidence and ambiguity.
        w,h=card.size
        ix0,iy0,ix1,iy1=cfg.get('validation',{}).get('voter_id_crop',[.68,0,.995,.20])
        idtxt='' if engine=='rapidocr' else ocr_image(card.crop((int(w*ix0),int(h*iy0),int(w*ix1),int(h*iy1))),'eng',psm=7,scale=4,whitelist='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789')
        id_candidates=sorted(set(epic_candidates(raw)+epic_candidates(idtxt)))
        # Retry with a little more left-side context only when the primary crop
        # produced no valid candidate. Keep alternatives; never repair O/0 or I/1.
        if engine!='rapidocr' and not id_candidates:
            fallback=card.crop((int(w*.64),0,int(w*.995),int(h*.22)))
            alt=ocr_image(fallback,'eng',psm=7,scale=5,whitelist='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789')
            idtxt=' | '.join(x for x in (idtxt,alt) if x)
            id_candidates=sorted(set(epic_candidates(raw)+epic_candidates(alt)))
        # Gender is a small, fixed line at the lower left. Reading it separately
        # recovers values lost when full-card OCR joins or distorts the line.
        gx0,gy0,gx1,gy1=cfg.get('validation',{}).get('gender_crop',[.02,.49,.58,.66])
        gcrop=card.crop((int(w*gx0),int(h*gy0),int(w*gx1),int(h*gy1)))
        gender_text='' if engine=='rapidocr' else ocr_image(gcrop,'eng',psm=7,scale=4)
        gender_values=sorted(set(gender_candidates(raw)+gender_candidates(gender_text)))
        if fields.get('gender') is None and len(gender_values)==1:
            fields['gender']=gender_values[0]
        fields['gender_raw']=gender_text
        fields['gender_candidates']='; '.join(gender_values)
        if not has_voter_card_evidence(raw,id_candidates):
            continue
        voter_id,id_selection=select_voter_id(raw,idtxt,id_candidates)
        id_status='CANDIDATE_REQUIRES_VERIFICATION' if voter_id else ('AMBIGUOUS' if id_selection=='AMBIGUOUS' else 'NOT_FOUND')
        if engine=='rapidocr':
            serial=serial_header_candidate(raw)
            serial_status='CANDIDATE_REQUIRES_VERIFICATION' if serial is not None else 'NOT_FOUND'
            serial_candidates=str(serial) if serial is not None else ''
        else:
            serial,serial_status,serial_candidates=serial_from_card(
            card,raw,cfg.get('validation',{}).get('serial_crop'),
            cfg.get('validation',{}).get('serial_crop_variants',()),
            float(cfg.get('validation',{}).get('serial_extended_bottom',.28)),
            bottom_row=(row==int(cfg['grid']['rows'])),
            vertical_windows=cfg.get('validation',{}).get('serial_vertical_windows',()))
        rec={'pdf_page':page_no,'grid_row':row,'grid_col':col,'voter_id_raw':idtxt,'raw_text':raw,'card_box_px':str(box),**fields,
             'voter_id':voter_id,'voter_id_status':id_status,'voter_id_selection':id_selection,'voter_id_candidates':'; '.join(id_candidates),
             'serial_no':serial,'serial_status':serial_status,'serial_candidates':serial_candidates}
        out.append(rec)
        if debug:
            from pathlib import Path
            p=Path(debug);p.mkdir(parents=True,exist_ok=True)
            card.save(p/f'{Path(pdf_path).stem}_p{page_no:03d}_r{row:02d}_c{col:02d}.png')
    return {'page':page_no,'rows':out,'detected':len(out),'grid_candidates':grid_candidates,'errors':'','duration':time.perf_counter()-started}

def extract_all(pdf_path,cfg,start_page=None,end_page=None):
    with pymupdf.open(pdf_path) as doc: total=len(doc)
    start=max(2,int(start_page or cfg['processing'].get('first_voter_page',2))); end=min(total,int(end_page or total))
    pages=list(range(start,end+1))
    configured_workers=max(1,min(int(cfg['processing'].get('max_workers',4)),int(cfg['processing'].get('max_workers_limit',6)),len(pages) or 1))
    # Each RapidOCR worker creates three ONNX sessions. Cap concurrency
    # separately so it can be tuned without exceeding the memory budget.
    rapid_workers=max(1,int(cfg['processing'].get('rapidocr_workers',1)))
    workers=min(configured_workers,rapid_workers) if cfg['ocr'].get('engine','tesseract').lower()=='rapidocr' else configured_workers
    result={}; print(f'Processing pages {start}-{end} with {workers} workers',flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures={pool.submit(process_page,(pdf_path,n,cfg)):n for n in pages}
        for fut in as_completed(futures):
            n=futures[fut]
            try: result[n]=fut.result()
            except Exception as e: result[n]={'page':n,'rows':[],'detected':0,'grid_candidates':0,'errors':repr(e),'duration':0}
            print(f'Page {n}: {result[n]["detected"]} voter cards from {result[n]["grid_candidates"]} inked slots'+(f' ERROR {result[n]["errors"]}' if result[n]['errors'] else ''),flush=True)
    rows=[]
    for n in pages: rows.extend(result[n]['rows'])
    return rows,[{'page':n,'detected':result[n]['detected'],'grid_candidates':result[n]['grid_candidates'],'failed_fields':sum(not r.get(k) for r in result[n]['rows'] for k in ('serial_no','voter_id','name','age','gender')),'error':result[n]['errors'],'duration_seconds':round(result[n]['duration'],2)} for n in pages]
