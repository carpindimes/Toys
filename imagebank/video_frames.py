#!/usr/bin/env python3
"""Turn UNRWA YouTube videos into candidate stills for the image bank.

Runs on the owner's Mac (YouTube blocks cloud servers). Produces the same
work/candidates.json that bank.py writes, so the rest of the pipeline
(bank.py prep -> visual review -> bank.py finalize) is unchanged.

  export IMAGEBANK_STATE=state/video IMAGEBANK_WORK=work_video IMAGEBANK_NAME_TAG=v
  python3 video_frames.py [--max-videos 6] [--per-video 30]
  python3 bank.py prep
  ...review, finalize, upload as in NIGHTLY.md...

Needs: yt-dlp and ffmpeg on PATH (brew install yt-dlp ffmpeg).
"""
import argparse, json, os, re, shutil, subprocess, sys
from pathlib import Path

import cv2
import imagehash
from PIL import Image

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("IMAGEBANK_STATE", ROOT / "state" / "video"))
WORK = Path(os.environ.get("IMAGEBANK_WORK", ROOT / "work_video"))
CHANNELS = ["https://www.youtube.com/@UNRWA/videos", "https://www.youtube.com/@UNRWA/shorts"]
GAZA = re.compile(r"gaza|غزة|khan ?younis|rafah|jabalia|deir al.?balah|nuseirat", re.I)


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw).stdout


def list_videos(limit):
    vids = []
    for ch in CHANNELS:
        try:
            out = run(["yt-dlp", "--flat-playlist", "--playlist-end", str(limit),
                       "--print", "%(id)s\t%(title)s\t%(duration)s", ch])
        except subprocess.CalledProcessError as e:
            print(f"  ! could not list {ch}: {e.stderr[-300:]}", file=sys.stderr)
            continue
        for line in out.splitlines():
            vid, title, dur = (line.split("\t") + ["", ""])[:3]
            vids.append({"id": vid, "title": title, "duration": dur})
    return vids


def extract(video, outdir, per_video):
    """Scene-change frames plus a frame every 3s, keep the sharpest, drop near-duplicates."""
    outdir.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-loglevel", "error", "-i", str(video), "-vf",
         "select='gt(scene,0.2)+not(mod(n,90))',scale='min(1920,iw)':-2",
         "-vsync", "vfr", "-q:v", "2", str(outdir / "f_%05d.jpg")])
    scored = []
    for f in sorted(outdir.glob("f_*.jpg")):
        g = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
        if g is None or g.mean() < 25 or g.mean() > 235:  # black/white title cards and fades
            f.unlink(); continue
        scored.append((cv2.Laplacian(g, cv2.CV_64F).var(), f))
    scored.sort(reverse=True)
    kept, hashes = [], []
    for sharp, f in scored:
        h = imagehash.phash(Image.open(f))
        if sharp < 60 or any(h - k <= 10 for k in hashes) or len(kept) >= per_video:
            f.unlink(); continue
        hashes.append(h); kept.append(f)
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-videos", type=int, default=6)
    ap.add_argument("--per-video", type=int, default=30)
    ap.add_argument("--scan", type=int, default=40, help="recent uploads to look through per channel tab")
    a = ap.parse_args()
    for tool in ("yt-dlp", "ffmpeg"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found: brew install yt-dlp ffmpeg")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    seen_f = STATE_DIR / "videos_seen.json"
    seen = set(json.loads(seen_f.read_text())) if seen_f.exists() else set()
    if WORK.exists():
        shutil.rmtree(WORK)
    (WORK / "raw").mkdir(parents=True)
    todo = [v for v in list_videos(a.scan) if v["id"] not in seen and GAZA.search(v["title"])][:a.max_videos]
    print(f"{len(todo)} new Gaza videos to process")
    cands = []
    for v in todo:
        url = f"https://www.youtube.com/watch?v={v['id']}"
        mp4 = WORK / "videos" / f"{v['id']}.mp4"
        try:
            run(["yt-dlp", "-q", "-f", "bv*[height<=1080][ext=mp4]/bv*[height<=1080]/b",
                 "--merge-output-format", "mp4", "-o", str(mp4), url])
            frames = extract(mp4, WORK / "frames" / v["id"], a.per_video)
        except subprocess.CalledProcessError as e:
            print(f"  ! {v['id']}: {(e.stderr or '')[-300:]}", file=sys.stderr)
            continue
        for f in frames:
            dest = WORK / "raw" / f"video_{v['id']}_{f.stem}.jpg"
            shutil.move(str(f), dest)
            w, h = Image.open(dest).size
            cands.append({"id": f"video:{v['id']}:{f.stem}", "source": "UNRWA (YouTube video still)",
                          "download": url, "landing": url, "title": v["title"], "creator": "UNRWA",
                          "license": "UNRWA footage - internal reference only, credit UNRWA",
                          "description": v["title"], "date": "", "orig_w": w, "orig_h": h,
                          "query": "unrwa video", "raw": str(dest)})
        mp4.unlink(missing_ok=True)
        seen.add(v["id"])
        print(f"  {v['id']}: {len(frames)} frames  ({v['title'][:70]})")
    (WORK / "candidates.json").write_text(json.dumps(cands, indent=1))
    seen_f.write_text(json.dumps(sorted(seen), indent=1))
    print(f"{len(cands)} candidate stills -> {WORK / 'candidates.json'}; next: python3 bank.py prep")


if __name__ == "__main__":
    main()
