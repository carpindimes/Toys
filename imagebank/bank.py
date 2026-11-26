#!/usr/bin/env python3
"""Gaza reference image bank: fetch -> prep -> (visual review) -> finalize.

Usage:
  python3 bank.py fetch      # collect candidate photos from open sources into work/raw
  python3 bank.py prep       # trim borders, smart-crop to 9:16, OCR for overlay text, dedupe
  # ...visual review writes work/decisions.json (see NIGHTLY.md)...
  python3 bank.py finalize   # name/save kept images, update contact sheet + seen state
  python3 bank.py links work/uploads.json   # record Drive links in the contact sheet

Everything runtime lives in imagebank/work (scratch, not committed) and
imagebank/state (committed: what has been seen + the master contact sheet).
"""
import csv, datetime as dt, hashlib, html, json, os, re, sys, time, random
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from urllib.parse import quote

os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # tesseract threads fight the process pool otherwise
import cv2
import numpy as np
import requests
import imagehash
import pytesseract
from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text())
STATE_DIR = Path(os.environ.get("IMAGEBANK_STATE", ROOT / "state"))  # the Mac video run uses state/video
WORK = Path(os.environ.get("IMAGEBANK_WORK", ROOT / "work"))
RAW, CROPS, REVIEW, OUT = WORK / "raw", WORK / "crops", WORK / "review", WORK / "out"
SEEN_F = STATE_DIR / "seen.json"
SHEET_F = STATE_DIR / "contact_sheet.csv"
UA = "RefImageBank/1.0 (https://commons.wikimedia.org/wiki/User:Beatleali; reference-photo tool)"
RATIO = 9 / 16
SHEET_COLS = ["filename", "category", "date_added", "scene", "framing_direction", "why_it_works",
              "cropped_from", "creator", "license", "source", "source_link", "drive_link"]

S = requests.Session()
S.headers["User-Agent"] = os.environ.get("IMAGEBANK_USER_AGENT", UA)


def load_state():
    if SEEN_F.exists():
        st = json.loads(SEEN_F.read_text())
    else:
        st = {}
    st.setdefault("ids", [])
    st.setdefault("phashes", [])
    st.setdefault("cursor", {})
    st.setdefault("query_start", 0)
    st.setdefault("counters", {k: 0 for k in CFG["categories"]})
    return st


def save_state(st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_F.write_text(json.dumps(st, indent=1))


BLOCKED_HOSTS = {}  # host -> reason; a source that is out of quota is skipped for the rest of the run


def get_json(url, params, tries=4):
    host = url.split('/')[2]
    if host in BLOCKED_HOSTS:
        return None
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=40)
            if r.status_code == 429:
                try:
                    wait = int(r.headers.get("retry-after", "0"))
                except ValueError:
                    wait = 0
                if wait > 300 or i == tries - 1:  # daily quota gone: stop asking this host tonight
                    BLOCKED_HOSTS[host] = f"rate limit, retry after {wait // 60} min" if wait else "rate limit"
                    print(f"  ! {host}: {BLOCKED_HOSTS[host]}; skipping it for the rest of this run", file=sys.stderr)
                    return None
                print(f"  rate-limited by {host}, waiting...", file=sys.stderr)
                time.sleep(max(wait, 30 * (i + 1))); continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa
            if i == tries - 1:
                print(f"  ! {url} failed: {e}", file=sys.stderr)
                return None
            time.sleep(3 * (i + 1))


def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def crop_size(w, h):
    return (h * RATIO, h) if w / h > RATIO else (w, w / RATIO)


# ---------------------------------------------------------------- sources
def commons_batch(q, offset):
    """Wikimedia Commons file search. Returns (candidates, next_offset)."""
    d = get_json("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{q} filetype:bitmap -protest -encampment -solidarity -rally -demonstration -march -vigil -flotilla", "gsrnamespace": 6, "gsrlimit": 40, "gsroffset": offset,
        "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1920,
        "iiextmetadatafilter": "Artist|LicenseShortName|DateTimeOriginal|ImageDescription|Credit"})
    if not d:
        return [], offset
    nxt = d.get("continue", {}).get("gsroffset")
    out = []
    for p in (d.get("query", {}).get("pages") or {}).values():
        ii = (p.get("imageinfo") or [{}])[0]
        w, h, mime = ii.get("width") or 0, ii.get("height") or 0, ii.get("mime", "")
        if mime not in ("image/jpeg", "image/png") or not w or not h:
            continue
        cw, ch = crop_size(w, h)
        if cw < CFG["min_crop_width"] or ch < CFG["min_crop_height"]:
            continue
        # Commons only serves standard thumbnail widths; ask the API for the 1920px one
        url = (ii.get("thumburl") or ii["url"]).split("?")[0]
        orig = ii["url"].split("?")[0]
        if url == orig and w > 960:  # narrower than 1920: use a standard thumb (originals are heavily rate-limited)
            std = 1280 if w > 1280 else 960
            a, b = orig.split("/wikipedia/commons/", 1)
            url = f"{a}/wikipedia/commons/thumb/{b}/{std}px-{b.split('/')[-1]}"
        url = url.replace("//upload.wikimedia.org/wikipedia/commons/thumb/", "//thumb.wikimedia.org/wikipedia/commons/thumb/")
        md = ii.get("extmetadata", {})
        out.append({
            "id": f"commons:{p['pageid']}", "source": "Wikimedia Commons",
            "download": url, "landing": ii.get("descriptionurl"),
            "title": p.get("title", "").replace("File:", ""),
            "creator": strip_html(md.get("Artist", {}).get("value"))[:120],
            "license": strip_html(md.get("LicenseShortName", {}).get("value")),
            "description": strip_html(md.get("ImageDescription", {}).get("value"))[:400],
            "date": strip_html(md.get("DateTimeOriginal", {}).get("value"))[:20],
            "orig_w": w, "orig_h": h, "query": q})
    return out, (nxt if nxt is not None else -1)


def openverse_batch(q, page):
    """Openverse (Flickr etc., openly licensed). Tall images only: Flickr URLs we can
    reach are capped at 1024px, so only portrait shots survive a 9:16 crop at usable size."""
    d = get_json("https://api.openverse.org/v1/images/", {
        "q": q, "page": page, "page_size": 20, "aspect_ratio": "tall", "mature": "false"})
    if not d:
        return [], page
    out = []
    for r in d.get("results", []):
        if r.get("source") == "wikimedia":
            continue  # covered directly
        url = r.get("url") or ""
        m = re.match(r"(https://live\.staticflickr\.com/\d+/\d+_[0-9a-f]+)(_[a-z])?\.jpg$", url)
        if not m:
            continue
        url = m.group(1) + "_b.jpg"
        out.append({
            "id": f"openverse:{r['id']}", "source": f"Openverse/{r.get('source')}",
            "download": url, "landing": r.get("foreign_landing_url"),
            "title": r.get("title") or "", "creator": (r.get("creator") or "")[:120],
            "license": f"CC {r.get('license', '').upper()} {r.get('license_version') or ''}".strip(),
            "description": ", ".join(t["name"] for t in (r.get("tags") or [])[:12]),
            "date": "", "orig_w": r.get("width"), "orig_h": r.get("height"), "query": q})
    nxt = page + 1 if page < (d.get("page_count") or 0) else -1
    return out, nxt


OFF_TOPIC = re.compile(r"protest|vigil|rally|march|demonstrat|flotilla|solidarity|encampment|boycott|"
                       r"conference|press|parliament|embassy|ship to gaza|exhibition|map of|infographic|"
                       r"puja|pandal|kolkata|india|teach-in|trident|pier|jlots|aircraft|c-17|c-130|airdrop|"
                       r"\bidf\b|soldier|navy|army|marines|military|painting|watercolou?r|engraving|botticelli|"
                       r"\b1[89]\d\d\b|cyprus|jerash|jordan|chicago|new york|london|paris|berlin", re.I)


def on_topic(c):
    return not OFF_TOPIC.search(" ".join([c.get("title", ""), c.get("description", "")]))


SOURCES = {"commons": (commons_batch, 0), "openverse": (openverse_batch, 1)}


def cmd_fetch():
    import shutil
    if WORK.exists():  # every night starts clean; state/ holds what must persist
        shutil.rmtree(WORK)
    st = load_state()
    seen = set(st["ids"])
    RAW.mkdir(parents=True, exist_ok=True)
    pool_target = int(os.environ.get("IMAGEBANK_POOL", CFG["candidate_pool_per_night"]))
    queries = CFG["queries"]
    start = st["query_start"] % len(queries)
    order = queries[start:] + queries[:start]
    cands, got_ids = [], set()
    exhausted_rounds = 0
    while len(cands) < pool_target and exhausted_rounds < 2:
        progress = False
        for q in order:
            for sname, (fn, first) in SOURCES.items():
                key = f"{sname}:{q}"
                cur = st["cursor"].get(key, first)
                if cur == -1:
                    continue
                batch, nxt = fn(q, cur)
                st["cursor"][key] = nxt
                new = [c for c in batch if c["id"] not in seen and c["id"] not in got_ids and on_topic(c)]
                for c in new:
                    got_ids.add(c["id"])
                cands.extend(new)
                progress = progress or bool(batch)
                time.sleep(0.5)
            if len(cands) >= pool_target:
                break
        exhausted_rounds = 0 if progress else exhausted_rounds + 1
        if not progress:
            break
    st["query_start"] = (start + 7) % len(queries)
    random.shuffle(cands)
    cands = cands[:pool_target]
    print(f"{len(cands)} new candidates; downloading...")
    ok = []
    for c in cands:
        fn = RAW / (c["id"].replace(":", "_") + ".jpg")
        if not fn.exists():
            try:
                for attempt in range(2):
                    r = S.get(c["download"], timeout=60)
                    if r.status_code != 429:
                        break
                    time.sleep(8)
                r.raise_for_status()
                fn.write_bytes(r.content)
            except Exception as e:
                print(f"  ! download {c['id']}: {e}", file=sys.stderr)
                continue
            time.sleep(1.0 if "wikimedia" in c["download"] else 0.3)
        c["raw"] = str(fn)
        ok.append(c)
    (WORK / "candidates.json").write_text(json.dumps(ok, indent=1))
    save_state(st)
    by_src = {}
    for c in ok:
        by_src[c["source"]] = by_src.get(c["source"], 0) + 1
    print(f"downloaded {len(ok)}: {by_src}")
    for host, why in BLOCKED_HOSTS.items():
        print(f"source unavailable tonight: {host} ({why})")


# ---------------------------------------------------------------- prep
def trim_borders(im):
    """Remove flat-colour borders/letterboxing (common on reposted/broadcast frames)."""
    a = np.asarray(im.convert("L"), dtype=np.float32)
    h, w = a.shape

    def flat(v):
        return v.std() < 6

    t, b, l, r = 0, h, 0, w
    while t < h * 0.3 and flat(a[t]): t += 1
    while b > h * 0.7 and flat(a[b - 1]): b -= 1
    while l < w * 0.3 and flat(a[:, l]): l += 1
    while r > w * 0.7 and flat(a[:, r - 1]): r -= 1
    trimmed = (t, h - b, l, w - r)
    if any(x > 4 for x in trimmed):
        return im.crop((l, t, r, b)), trimmed
    return im, (0, 0, 0, 0)


_FACE = None


def interest_point(im):
    """(x,y) fractions of where the subject is: faces first, else spectral-residual saliency."""
    global _FACE
    if _FACE is None:
        _FACE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    small = im.copy(); small.thumbnail((900, 900))
    g = cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2GRAY)
    sh, sw = g.shape
    faces = _FACE.detectMultiScale(g, 1.1, 6, minSize=(max(20, sw // 40),) * 2)
    if len(faces):
        wts = np.array([fw * fh for (_, _, fw, fh) in faces], dtype=float)
        cx = np.array([x + fw / 2 for (x, _, fw, _) in faces])
        cy = np.array([y + fh / 2 for (_, y, _, fh) in faces])
        return float((cx * wts).sum() / wts.sum() / sw), float((cy * wts).sum() / wts.sum() / sh), len(faces)
    # spectral residual saliency
    s = cv2.resize(g, (128, int(128 * sh / sw) or 1)).astype(np.float32)
    f = np.fft.fft2(s)
    la = np.log(np.abs(f) + 1e-8)
    sr = la - cv2.blur(la, (3, 3))
    sal = np.abs(np.fft.ifft2(np.exp(sr + 1j * np.angle(f)))) ** 2
    sal = cv2.GaussianBlur(sal, (9, 9), 2.5)
    thr = np.percentile(sal, 95)
    ys, xs = np.nonzero(sal >= thr)
    return float(xs.mean() / s.shape[1]), float(ys.mean() / s.shape[0]), 0


def do_crop(im, pos):
    """pos in [0,1]: where along the slack axis the 9:16 window sits."""
    w, h = im.size
    if w / h > RATIO:
        cw = int(round(h * RATIO)); x = int(round((w - cw) * pos))
        return im.crop((x, 0, x + cw, h)), (x, 0, x + cw, h)
    ch = int(round(w / RATIO)); y = int(round((h - ch) * pos))
    return im.crop((0, y, w, y + ch)), (0, y, w, y + ch)


def auto_pos(im, ix, iy):
    w, h = im.size
    if w / h > RATIO:
        cw = h * RATIO
        return float(np.clip((ix * w - cw / 2) / max(1, w - cw), 0, 1))
    ch = w / RATIO
    # keep the subject in the upper-middle (faces read best around 40% down)
    return float(np.clip((iy * h - ch * 0.4) / max(1, h - ch), 0, 1))


def ocr_words(crop):
    c = crop.copy(); c.thumbnail((1000, 1600))
    try:
        d = pytesseract.image_to_data(c, lang="eng+ara", config="--psm 11",
                                      output_type=pytesseract.Output.DICT, timeout=60)
    except Exception:
        return []
    words = []
    for t, conf, y, hh in zip(d["text"], d["conf"], d["top"], d["height"]):
        t = t.strip()
        if float(conf) >= 75 and len(re.sub(r"\W", "", t)) >= 3:
            words.append({"t": t, "y": round(y / c.size[1], 2), "h": round(hh / c.size[1], 3)})
    return words


def review_sheet(cid, im, box, crop, info):
    H = 760
    a = im.copy(); a.thumbnail((1400, H))
    sc = a.size[0] / im.size[0]
    dr = ImageDraw.Draw(a)
    bx = [int(v * sc) for v in box]
    for k in range(4):
        dr.rectangle([bx[0] + k, bx[1] + k, bx[2] - k, bx[3] - k], outline=(255, 40, 40))
    b = crop.copy(); b.thumbnail((H, H))
    canvas = Image.new("RGB", (a.size[0] + b.size[0] + 30, H + 40), (30, 30, 30))
    canvas.paste(a, (10, 30)); canvas.paste(b, (a.size[0] + 20, 30))
    ImageDraw.Draw(canvas).text((10, 8), f"{cid}   {info}", fill=(255, 255, 0))
    p = REVIEW / f"{cid}.jpg"
    canvas.save(p, quality=80)
    return str(p)


def prep_one(c):
    cid = c["id"].replace(":", "_")
    try:
        im = Image.open(c["raw"]); im = ImageOps.exif_transpose(im).convert("RGB")
    except Exception as e:
        return {**c, "cid": cid, "status": "reject", "reason": f"unreadable: {e}"}
    im, trimmed = trim_borders(im)
    w, h = im.size
    cw, ch = crop_size(w, h)
    if cw < CFG["min_crop_width"] or ch < CFG["min_crop_height"]:
        return {**c, "cid": cid, "status": "reject", "reason": f"too small after trim ({int(cw)}x{int(ch)})"}
    ix, iy, nfaces = interest_point(im)
    pos = auto_pos(im, ix, iy)
    crop, box = do_crop(im, pos)
    words = ocr_words(crop)
    ph = str(imagehash.phash(crop))
    ph_full = str(imagehash.phash(im))
    status, reason = "ok", ""
    if len(words) >= 10:
        status, reason = "reject", f"overlay text ({len(words)} words)"
    elif words:
        status = "flag"
    crop_p = CROPS / f"{cid}.jpg"
    crop.save(crop_p, quality=92)
    info = f"{w}x{h} -> {crop.size[0]}x{crop.size[1]}  faces={nfaces}  ocr={[x['t'] for x in words][:6]}"
    rv = review_sheet(cid, im, box, crop, info) if status != "reject" else None
    return {**c, "cid": cid, "status": status, "reason": reason, "ocr": words, "phash": ph,
            "phash_full": ph_full, "faces": nfaces, "trimmed": trimmed, "crop_pos": round(pos, 3), "crop": str(crop_p),
            "review": rv, "work_size": [w, h], "crop_size": list(crop.size)}


def cmd_prep():
    for d in (CROPS, REVIEW):
        d.mkdir(parents=True, exist_ok=True)
    cands = json.loads((WORK / "candidates.json").read_text())
    with ProcessPoolExecutor(max_workers=max(2, (os.cpu_count() or 2))) as ex:
        res = list(ex.map(prep_one, cands, chunksize=4))
    st = load_state()
    known = [imagehash.hex_to_hash(x) for x in st["phashes"]]
    batch = []
    for r in res:
        if r["status"] == "reject" or "phash" not in r:
            continue
        hsh = imagehash.hex_to_hash(r["phash"])
        full = imagehash.hex_to_hash(r["phash_full"])
        if any(hsh - k <= 8 or full - k <= 8 for k in known + batch):
            r["status"], r["reason"] = "reject", "duplicate of an earlier image"
            continue
        batch.extend([hsh, full])
    (WORK / "prepped.json").write_text(json.dumps(res, indent=1))
    n = {}
    for r in res:
        n[r["status"]] = n.get(r["status"], 0) + 1
    print("prep:", n)
    reasons = {}
    for r in res:
        if r["status"] == "reject":
            k = r["reason"].split(" (")[0]
            reasons[k] = reasons.get(k, 0) + 1
    print("rejected because:", reasons)
    todo = [r["review"] for r in res if r["status"] != "reject"]
    for i in range(0, len(todo), 40):
        (WORK / f"batch_{i // 40 + 1}.txt").write_text("\n".join(todo[i:i + 40]))
    print(f"{len(todo)} review sheets in {REVIEW}, split into {(len(todo) + 39) // 40} batch files work/batch_<n>.txt")


# ---------------------------------------------------------------- finalize
def cmd_finalize():
    prepped = {r["cid"]: r for r in json.loads((WORK / "prepped.json").read_text())}
    decisions = {}
    for f in sorted(WORK.glob("decisions*.json")):  # decisions.json and/or per-batch decisions_<n>.json
        decisions.update(json.loads(f.read_text()))
    st = load_state()
    today = dt.date.today().isoformat()
    rows, manifest = [], []
    kept = [(cid, d) for cid, d in decisions.items() if d.get("keep")]
    kept = kept[:CFG["target_per_night"]]
    for cid, d in kept:
        r = prepped[cid]
        cat = d["category"]
        assert cat in CFG["categories"], cat
        crop_path = r["crop"]
        if d.get("crop_pos") is not None and abs(float(d["crop_pos"]) - r["crop_pos"]) > 0.02:
            im = ImageOps.exif_transpose(Image.open(r["raw"])).convert("RGB")
            im, _ = trim_borders(im)
            crop, _ = do_crop(im, float(d["crop_pos"]))
            crop.save(crop_path, quality=92)
        st["counters"][cat] = st["counters"].get(cat, 0) + 1
        tag = os.environ.get("IMAGEBANK_NAME_TAG", "")  # "v" for video frames, so names never collide
        name = f"{today}_{cat}_{tag}{st['counters'][cat]:04d}.jpg"
        dest = OUT / CFG["categories"][cat] / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        Image.open(crop_path).save(dest, quality=92)
        sha = hashlib.sha256(dest.read_bytes()).hexdigest()
        ow, oh = r["work_size"]
        rows.append({
            "filename": name, "category": CFG["categories"][cat], "date_added": today,
            "scene": d.get("scene", ""), "framing_direction": d.get("framing", ""),
            "why_it_works": d.get("why", ""),
            "cropped_from": "native vertical" if abs(ow / oh - RATIO) < 0.02 else f"cropped from {ow}x{oh}",
            "creator": r.get("creator", ""), "license": r.get("license", ""), "source": r["source"],
            "source_link": r.get("landing", ""), "drive_link": ""})
        manifest.append({"file": str(dest), "name": name, "folder": CFG["categories"][cat],
                         "sha256": sha, "ref": f"sha256:{sha}:{dest}"})
    # mark everything reviewed tonight (kept or not) as seen so it never comes back
    for cid, r in prepped.items():
        st["ids"].append(r["id"])
        if r.get("phash") and (decisions.get(cid, {}).get("keep") or r["status"] != "reject"):
            st["phashes"].append(r["phash"])
            if r.get("phash_full"):
                st["phashes"].append(r["phash_full"])
    st["ids"] = sorted(set(st["ids"]))
    st["phashes"] = sorted(set(st["phashes"]))
    new_file = not SHEET_F.exists()
    with SHEET_F.open("a", newline="") as f:
        wr = csv.DictWriter(f, SHEET_COLS)
        if new_file:
            wr.writeheader()
        wr.writerows(rows)
    save_state(st)
    (WORK / "upload_manifest.json").write_text(json.dumps(manifest, indent=1))
    per = {}
    for m in manifest:
        per[m["folder"]] = per.get(m["folder"], 0) + 1
    print(f"finalized {len(manifest)} images: {per}")
    print(f"manifest: {WORK / 'upload_manifest.json'}")


def cmd_links(path):
    links = json.loads(Path(path).read_text())  # {filename: drive_file_id}
    rows = list(csv.DictReader(SHEET_F.open()))
    for r in rows:
        if r["filename"] in links and not r["drive_link"]:
            r["drive_link"] = f"https://drive.google.com/file/d/{links[r['filename']]}/view"
    with SHEET_F.open("w", newline="") as f:
        wr = csv.DictWriter(f, SHEET_COLS); wr.writeheader(); wr.writerows(rows)
    tonight = [r for r in rows if r["filename"] in links]
    nightly = WORK / f"contact_sheet_{dt.date.today().isoformat()}.csv"
    with nightly.open("w", newline="") as f:
        wr = csv.DictWriter(f, SHEET_COLS); wr.writeheader(); wr.writerows(tonight)
    sha = hashlib.sha256(nightly.read_bytes()).hexdigest()
    print(f"updated {len(tonight)} links")
    print(f"tonight's contact sheet ref: sha256:{sha}:{nightly}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"fetch": cmd_fetch, "prep": cmd_prep, "finalize": cmd_finalize,
     "links": lambda: cmd_links(sys.argv[2])}.get(cmd, lambda: print(__doc__))()
