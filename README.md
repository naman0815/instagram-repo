# Telegram → IPA source

Watches a Telegram channel for Instagram `.ipa` posts, publishes each one as a GitHub
Release asset and keeps an AltStore-style `source.json` up to date for FlareStore / Feather.
Runs on the 1st and 16th of each month (03:17 UTC) on GitHub Actions; no server. Run it by hand
from the Actions tab for an immediate check.

## Setup

1. Go to <https://my.telegram.org> → API development tools → create an app. Note `api_id` and `api_hash`.
2. Locally create `.env` (gitignored):
   ```
   TG_API_ID=123456
   TG_API_HASH=abcdef...
   TG_CHANNEL=mychannel        # username, or numeric id
   ```
   then `pip install -r requirements.txt && python make_session.py`, log in with your phone + code,
   and copy the printed session string. **It is a login to your Telegram account** — only ever
   store it as a GitHub secret.
3. Create a **public** GitHub repo and push this project.
4. Repo → Settings → Secrets and variables → Actions → add `TG_API_ID`, `TG_API_HASH`, `TG_SESSION`, `TG_CHANNEL`.
5. Repo → Settings → Actions → General → Workflow permissions → **Read and write permissions**.
6. Actions tab → *Update source* → Run workflow.
7. In FlareStore / Feather add: `https://raw.githubusercontent.com/<user>/<repo>/main/source.json`

Optional: add `icon.png` (source icon) and `instagram.png` (app icon) to the repo root; the
generated `iconURL`s point at them.

## Notes

- GitHub **pauses scheduled workflows after 60 days without repo activity**. Real updates commit
  to the repo and keep it alive. If a run has nothing to publish and the latest commit is more
  than 30 days old, the workflow adds one empty "Keep scheduled workflow alive" commit.
- Cron runs can be delayed by several minutes. Release assets may be up to 2 GB.
- `python fetch.py --dry-run` (needs the `TG_*` env vars) lists IPAs it would process without
  downloading, uploading or editing anything.
- Only filenames matching `IPA_FILENAME_REGEX` (set in `.github/workflows/update.yml`) are tracked;
  currently `Sparkle_v<x>_IG_v<y>.ipa`. Other posts (no-ext IPA, `.deb`, `.dylib`) are ignored.
- If the regex has a named group `version`, that text (Sparkle's version) is used as the release
  version/tag instead of the Info.plist version (Instagram's), so a new Sparkle build on the same
  Instagram version is still published. The Instagram version goes in the description.
- Every run looks at the **newest** post in the channel that has a matching IPA. If that version
  is already published it is skipped without downloading; otherwise it is published. `.ipa` files
  that don't match the filter are listed in the log (`Ignoring #<id>: <name>`). If two releases
  appear between runs, only the newest is published. Set `BACKFILL=1` in the workflow env to
  process every message after `state.json` instead (the whole history if it is 0).
- The changelog is the post's caption. Telegram albums keep it on just one file, so the script
  looks through the same album for it. Lines matching `CAPTION_DROP_REGEX` (workflow env) are
  removed. An already-published version only gets its changelog filled in, with no re-download.
- The source name (`name` in `source.json`) is shown in FlareStore/Feather; edit it there.
- `icon.png` / `instagram.png` are original placeholder artwork; replace them with any image.
- **Optional Cloudflare R2 hosting** (faster downloads than GitHub release assets). Add the repo
  secrets `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` and
  `R2_PUBLIC_URL` (the bucket's public `https://pub-....r2.dev` or custom-domain URL). With all five
  set, new IPAs are uploaded to R2 and `downloadURL` points there; the GitHub release is still
  created. The newest `R2_KEEP` (default 5) versions live on R2; older ones are deleted from the
  bucket and fall back to their GitHub release URL. Existing versions are copied to R2 on the next
  run. If R2 fails or is not configured, GitHub URLs are used.
- Tests: `pip install -r requirements-dev.txt && pytest`.
- If a download fails, state is not advanced past that message; the next run retries it.
