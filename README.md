# Telegram → IPA source

Watches a Telegram channel for Instagram `.ipa` posts, publishes each one as a GitHub
Release asset and keeps an AltStore-style `source.json` up to date for FlareStore / Feather.
Runs every 30 minutes on GitHub Actions; no server.

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

- GitHub **pauses scheduled workflows after 60 days without repo activity**. Real updates
  commit to the repo and keep it alive; if the channel goes quiet, re-enable the workflow
  or push any commit.
- Cron runs can be delayed by several minutes. Release assets may be up to 2 GB.
- `python fetch.py --dry-run` (needs the `TG_*` env vars) lists IPAs it would process without
  downloading, uploading or editing anything.
- Tests: `pip install -r requirements-dev.txt && pytest`.
- If a download fails, state is not advanced past that message; the next run retries it.
