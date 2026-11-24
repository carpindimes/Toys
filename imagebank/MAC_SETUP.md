# Mac setup — UNRWA video stills

YouTube blocks cloud servers, so turning UNRWA videos into reference stills runs on the owner's Mac.
Everything after frame extraction is the same pipeline as the nightly cloud run (`NIGHTLY.md`).

## One-time install

```bash
# Homebrew (skip if `brew -v` works)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install yt-dlp ffmpeg tesseract tesseract-lang git python

# the repo (any folder you like)
git clone https://github.com/carpindimes/Toys.git ~/GazaImageBank
cd ~/GazaImageBank && git checkout claude/relaxed-edison-y4afki
python3 -m venv .venv && source .venv/bin/activate
pip install "opencv-python-headless<5" pytesseract imagehash requests pillow numpy
```

## A run

```bash
cd ~/GazaImageBank/imagebank && source ../.venv/bin/activate
export IMAGEBANK_STATE=state/video IMAGEBANK_WORK=work_video IMAGEBANK_NAME_TAG=v
python3 video_frames.py --max-videos 6 --per-video 30   # new Gaza videos from UNRWA's channel -> stills
python3 bank.py prep                                     # trim, 9:16 crop, OCR (catches burned-in subtitles), dedupe
```

Then review `work_video/batch_<n>.txt` exactly as in `NIGHTLY.md` step 4, and run `python3 bank.py finalize`.
Upload the images and contact sheet to the same Drive folders, then commit `imagebank/state/video`.
Video stills are named `<date>_<category>_v0001.jpg`, so they never clash with photo names.

Notes:
- UNRWA videos often have burned-in subtitles and lower-third captions. OCR flags these and the visual review rejects
  them, or moves the 9:16 crop clear of the caption when possible.
- Video stills are credited "UNRWA" in the contact sheet and are for internal reference only.
