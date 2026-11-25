# Nightly run — instructions for the scheduled Claude session

Goal: add up to **100 new 9:16, overlay-free reference photos** of people in Gaza to the
Google Drive image bank, sorted into the three category folders, with a contact-sheet row
for each one. The owner reviews the bank before sending it to the media team.

Drive (owner's My Drive):
- Root `Gaza Reference Image Bank` — `1aSmV4fzPftX0-ZMVzkHnmeJOZMnmcf_x`
- `1 - Dire Need` — `1axa2DpVXtvTtiqpdG4IEWP_QjlPSt9_I`
- `2 - Life Among the Ruins` — `1BJiwh8Q7idsDHdCCWWW3jL1P2xmud0pI`
- `3 - Receiving Aid` — `1zDOGRV6CaqtzsXsO6VjfQSoyJ6jtYDhw`
- `Photographer Guides` — `1ObRgcMKkdyLOpNsB2FpRR5WjfpFGIKT1`
- `Contact Sheets` — `1y9AKHEu_FvdPttpeaxGYRcbYZIKaTb8L` (one Google Sheet per night)

## Steps

1. **Setup.** `cd` into the repo, `git fetch origin claude/relaxed-edison-y4afki && git checkout claude/relaxed-edison-y4afki && git pull`.
   If `tesseract` or the Python packages are missing, run `bash imagebank/setup.sh`.
2. **Check the Wikimedia token.** `curl -s "https://commons.wikimedia.org/w/api.php?action=query&meta=userinfo&format=json"`
   should show the owner's Wikimedia username (Beatleali). If it shows `"anon"`, the saved network secret isn't being applied.
   Carry on, but say so in the report.
2b. **Fetch.** `cd imagebank && python3 bank.py fetch`. This can take 20–40 minutes (Wikimedia rate limits); run it in the background and wait.
   If Wikimedia keeps returning 429, carry on with whatever was downloaded and mention it in the report.
3. **Prep.** `python3 bank.py prep`. This trims borders, smart-crops to 9:16, OCRs for text and dedupes against earlier nights.
4. **Visual review. This is the important part.** `prep` splits the sheets to review into `work/batch_<n>.txt`
   (40 paths each). Launch one subagent per batch **in parallel** (Agent tool), each told to read this file's
   step 4, open every sheet in its batch file, and write its decisions to `work/decisions_<n>.json`. Spot-check a few
   of their keeps yourself before finalizing. (The `cid` is the sheet's filename without `.jpg`; OCR hits are
   in `work/prepped.json` under `ocr`.) Each sheet shows the original with the
   red 9:16 crop box on the left, and the crop on the right. Judge **the crop**:
   - **REJECT** if the crop contains *any* overlay: subtitles, captions, headline bars, watermarks,
     logos or bugs added in editing, borders, collage/split layouts, stickers, emoji, screenshots,
     TV graphics, or date stamps. Text that is physically part of the scene (a shop sign, an aid-sack print,
     a banner held by people) is acceptable only if it is small and not the focus.
   - **REJECT** anything graphic or ad-unsafe: blood, wounds, injuries, bodies (shrouded or not),
     weapons, armed people, nudity. Follow `guides/04_meta_ad_policy_safe_imagery.md`.
   - **REJECT** if it is not people in Gaza: protests abroad, maps, infographics, politicians at
     podiums, press conferences, aerial shots with no people, or a building without a person.
   - **REJECT** weak reference shots: blurry, no clear subject, faces too small to read.
   - **Fix the crop** if a better 9:16 window exists in the original: set `crop_pos` (0 = left/top, 1 = right/bottom).
   - **KEEP** strong shots: one clear human subject, readable emotion or action, a good
     vertical composition. Keeping fewer good images beats hitting 100 with weak ones.
   For each kept image write: `category` (`dire-need` | `life-among-ruins` | `receiving-aid`),
   `scene` (one sentence on what is happening), `framing` (a direction for the photographer, **starting with
   action verbs**, e.g. "CROUCH to her eye level; FRAME her in the lower half with the collapsed block rising
   behind; WAIT for her to glance at the empty pot"), and `why` (why it would stop a scroll and drive donations).
   Write decisions to `work/decisions_<n>.json` as `{cid: {keep, category, scene, framing, why, crop_pos?}}`;
   rejected ones need only `{"keep": false, "reason": "..."}`. Work in batches and save as you go.
5. **Finalize.** `python3 bank.py finalize`. This writes `work/upload_manifest.json` and updates `state/`.
6. **Upload.** For each manifest entry, call Google Drive `create_file` with `title` = name,
   `parentId` = the category folder id above, `contentMimeType` = `image/jpeg`,
   `disableConversionToGoogleType` = true and `base64Content` = the entry's `ref` (a file reference;
   don't paste base64). Collect `{name: file_id}` into `work/uploads.json`, then run `python3 bank.py links work/uploads.json`.
7. **Contact sheet.** `bank.py links` prints a `ref` for tonight's CSV. Upload it with Drive `create_file`:
   `title` = `Contact Sheet - <YYYY-MM-DD>`, `parentId` = the Contact Sheets folder id, `contentMimeType` = `text/csv`,
   `base64Content` = that ref (leave conversion on, so it becomes a Google Sheet). The full history stays in
   `state/contact_sheet.csv` in the repo.
8. **Commit state.** `git add imagebank/state && git commit -m "Image bank: nightly run <date>" && git push -u origin claude/relaxed-edison-y4afki`.
9. **Report** in one short message: how many were added per category, how many were rejected and the main
   reasons, and whether sources are running dry. If the run fell short of 100, say so plainly. Don't pad.
