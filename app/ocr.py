import re
from itertools import product
from pathlib import Path
from PIL import Image, ImageOps, ImageEnhance, ImageFilter
import pytesseract
from threading import local

_rapid_engines = local()

def rapidocr_image(img, intra_op_threads=2, inter_op_threads=1):
    """Run RapidOCR on an RGB PIL image and return text in reading order."""
    thread_key=(int(intra_op_threads),int(inter_op_threads))
    if not hasattr(_rapid_engines, "engine") or getattr(_rapid_engines,"thread_key",None)!=thread_key:
        from rapidocr import RapidOCR
        _rapid_engines.engine = RapidOCR(params={
            "EngineConfig.onnxruntime.intra_op_num_threads": thread_key[0],
            "EngineConfig.onnxruntime.inter_op_num_threads": thread_key[1],
        })
        _rapid_engines.thread_key=thread_key
    import numpy as np
    result = _rapid_engines.engine(np.asarray(img.convert("RGB")))
    return "\n".join(str(value) for value in (result.txts or []))

def configure_tesseract(path=None):
    if path:
        p=Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Tesseract executable not found: {p}")
        pytesseract.pytesseract.tesseract_cmd=str(p)
    else:
        for p in [Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"), Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")]:
            if p.exists():
                pytesseract.pytesseract.tesseract_cmd=str(p)
                break

def prep(img, scale=2, threshold=False):
    img=ImageOps.grayscale(img)
    img=ImageEnhance.Contrast(img).enhance(1.8)
    img=img.resize((max(1,img.width*scale),max(1,img.height*scale)))
    img=img.filter(ImageFilter.SHARPEN)
    if threshold:
        img=img.point(lambda p:255 if p>160 else 0)
    return img

def ocr_image(img, lang="eng", psm=6, scale=1, whitelist=None, threshold=False):
    im=prep(img,scale,threshold)
    cfg=f"--oem 3 --psm {psm}"
    if whitelist: cfg += f" -c tessedit_char_whitelist={whitelist}"
    try:
        return pytesseract.image_to_string(im,lang=lang,config=cfg).strip()
    except pytesseract.TesseractError:
        return pytesseract.image_to_string(im,config=cfg).strip()

def clean(s):
    return re.sub(r"\s+"," ",(s or "")).strip()

def _label_value(text,label,stop_pattern):
    match=re.search(label+r"\s*:\s*(.*?)"+stop_pattern,text or "",re.I|re.S)
    if not match:return None
    value=clean(match.group(1)).strip(" |:;,.-")
    return value or None

def select_voter_id(raw_text,targeted_text,candidates):
    """Prefer the explicit card-header EPIC read over less contextual crops."""
    header=raw_text or ""
    name_start=re.search(r"Name\s*[^\w\s]{0,4}\s*",header,re.I)
    if name_start: header=header[:name_start.start()]
    header_ids=sorted(set(epic_matches(header)))
    if len(header_ids)==1:return header_ids[0],"EXACT_CARD_HEADER"
    targeted=sorted(set(epic_matches(targeted_text)))
    if len(targeted)==1:return targeted[0],"EXACT_TARGETED_CROP"
    raw=sorted(set(epic_matches(raw_text)))
    if len(raw)==1:return raw[0],"EXACT_CARD_RAW_TEXT"
    options=sorted({x for x in (candidates or []) if x})
    if len(options)==1:return options[0],"SINGLE_PATTERN_CANDIDATE"
    return None,"AMBIGUOUS" if options or len(targeted)>1 or len(raw)>1 else "NOT_FOUND"

def epic_matches(text):
    # Preserve original OCR text; normalize only candidate string for pattern matching.
    out=[]
    for token in re.findall(r"[A-Za-z0-9|IlOoSsBb]{8,14}", text or ""):
        compact=re.sub(r"[^A-Z0-9]","",token.upper())
        for m in re.finditer(r"[A-Z]{3}[0-9]{7}",compact):
            out.append(m.group(0))
    return out

def epic_candidates(text):
    """Return exact EPIC reads and explicit OCR-confusion alternatives.

    Alternatives are never treated as verified: callers retain the raw OCR
    string and mark every candidate for visual review.
    """
    out=set(epic_matches(text))
    # Confusable glyphs are expanded only in the position where the EPIC
    # format permits them: letters in the three-character prefix, digits in
    # the seven-character suffix.
    to_letter={'0':'O','1':'I','5':'S','6':'G','8':'B','2':'Z'}
    to_digit={'O':'0','Q':'0','D':'0','I':'1','L':'1','|':'1','S':'5','B':'8','Z':'2','G':'6'}
    for token in re.findall(r'[A-Za-z0-9|IlOoQqSsBbZzGgDd]{10}',text or ''):
        token=token.upper()
        prefix_options=[]
        for ch in token[:3]:
            opts={ch} if ch.isalpha() else ({to_letter[ch]} if ch in to_letter else set())
            prefix_options.append(sorted(x for x in opts if x.isalpha()))
        digit_options=[]
        for ch in token[3:]:
            opts={ch} if ch.isdigit() else ({to_digit[ch]} if ch in to_digit else set())
            digit_options.append(sorted(x for x in opts if x.isdigit()))
        if any(not x for x in prefix_options+digit_options):
            continue
        for letters in product(*prefix_options):
            for digits in product(*digit_options):
                out.add(''.join(letters)+''.join(digits))
    return sorted(out)

def parse_fields(text):
    flat=clean(text)
    f={"serial_no":None,"voter_id":None,"name":None,"relation_type":None,
       "relation_name":None,"house_no":None,"age":None,"gender":None}
    # OCR often substitutes punctuation for ':' and may concatenate fields.
    # Capture each value up to the next recognized label to prevent shifting.
    # Consume OCR punctuation (including Unicode replacement/quote glyphs)
    # between a label and value without consuming letters or digits.
    separator=r"[\W]*"
    relation_label=r"(?:Father'?s?|Husband'?s?|Mother'?s?|Others?)\s*[.]?\s+Name"
    label_re=re.compile(
        r"(?P<relation>"+relation_label+r")"+separator+
        r"|(?P<name>\bName)"+separator+
        r"|(?P<house>House\s+(?:Number|No\.?))"+separator+
        r"|(?P<age>Age)"+separator+r"|(?P<gender>Gender)"+separator,
        re.I)
    hits=list(label_re.finditer(flat))
    for i,hit in enumerate(hits):
        end=hits[i+1].start() if i+1<len(hits) else len(flat)
        stop=re.search(r"\b(?:Photo|Available)\b",flat[hit.end():end],re.I)
        if stop:end=hit.end()+stop.start()
        value=clean(flat[hit.end():end]).strip(" |:;,") or None
        kind=next((k for k in ("relation","name","house","age","gender") if hit.group(k)),None)
        if kind=="name" and f["name"] is None:
            f["name"]=value
        elif kind=="relation" and f["relation_name"] is None:
            label=hit.group("relation").lower()
            f["relation_type"]="Father" if label.startswith("father") else "Husband" if label.startswith("husband") else "Mother" if label.startswith("mother") else "Others"
            f["relation_name"]=value
        elif kind=="house" and f["house_no"] is None:
            f["house_no"]=value
        elif kind=="age" and f["age"] is None:
            m=re.match(r"(\d{1,3})\b",value or "")
            if m and 18<=int(m.group(1))<=120:f["age"]=int(m.group(1))
        elif kind=="gender" and f["gender"] is None:
            m=re.match(r"(Male|Female|Third\s+Gender)\b",value or "",re.I)
            if m:
                v=clean(m.group(1)).title()
                f["gender"]="Third Gender" if v.lower()=="third gender" else v
    # Serial often printed in a small box at card top-left. Keep separate crop extraction;
    # don't invent a serial from grid position in this field.
    return f

def serial_header_candidate(text):
    """Read a boxed serial from the first two OCR lines near the EPIC."""
    first=" ".join((text or "").splitlines()[:2])
    match=re.search(r"^\D{0,12}(\d{1,3})\D+([A-Za-z0-9|]{10})\b",first)
    if not match:
        match=re.search(r"^\s*(\d{1,3})\s+(?:\|\s*)?([A-Za-z0-9|]{10})\b",first)
    if not match or not epic_candidates(match.group(2)):
        return None
    value=int(match.group(1))
    return value if 1<=value<=999 else None
