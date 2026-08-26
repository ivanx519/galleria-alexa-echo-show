# Galleria Alexa — Photo/Slideshow Skill

Digital photo frame on Echo Show: slideshow of family photos/videos, with
voice search by month or place ("show December", "show Villaspeciosa").

> **Why "Limone" (Italian for "lemon")?** The skill's invocation name (what
> you say to Alexa to open it, e.g. "Alexa, open Limone") is deliberately an
> unusual word with no obvious connection to photos/galleries. This is a
> deliberate choice: generic names like "gallery" or "photos" risk making
> Alexa confuse the invocation with other skills using similar words, opening
> the wrong thing. A short, memorable, unlikely word like "lemon" avoids
> ambiguity — you can use any word that works the same way for yours, just
> change it in `interaction_model.json`.

## Components

| File | Runs on | Deploy |
|---|---|---|
| `lambda_function.py` | AWS Lambda `galleria-alexa-skill` | `aws lambda update-function-code` (GitHub Actions) |
| `server.py` | VPS, systemd service `foto-slideshow-api` (port 8770) | SSH + `systemctl restart` (GitHub Actions) |
| `ridimensiona.py` | VPS, manual batch | resizes original photos + geocoding (Nominatim) |
| `ridimensiona_video.py` | VPS, manual batch | compresses original videos (ffmpeg, 720p H.264) |
| `organizza_per_anno.py` | VPS, manual batch | creates `Foto/<year>/` and `Video/<year>/` via hardlinks for SFTP access |
| `interaction_model.json` | Alexa Developer Console | uploaded via `ask smapi set-interaction-model` (manual, not in CI) |

Skill ID: yours, assigned when you create the skill in the Alexa Developer
Console (see Setup section)

## Automatic deploy

Push to `master` → GitHub Actions:
- **deploy-lambda**: installs `requirements.txt` into `package/`, copies `lambda_function.py`, zips it, updates the Lambda on AWS (scoped credentials, permissions limited to `freezer-alexa-skill`/`galleria-alexa-skill`)
- **deploy-vps**: `git pull` of this repo into `/opt/foto_slideshow_repo` on the VPS, copies `server.py` and the three scripts into `/opt/foto_slideshow/`, restarts `foto-slideshow-api`

`interaction_model.json` is NOT applied automatically — a change to the
voice commands requires a manual round with `ask smapi
set-interaction-model` (async, check `ask smapi get-skill-status` for the
"SUCCEEDED" confirmation).

## Scale — running on a real 11k+ photo library

Not a toy demo: the deployment this was built for currently manages
**11,436 photos and 656 videos**, and the slideshow rotates through the
whole library smoothly. Three viewing modes (`MostraGalleriaIntent` for
photos, `MostraVideoIntent` for videos, `MostraFotoVideoIntent` for both
mixed together), plus favorites as an overlay filter on top of any of them,
and free-text voice search by month or place (`MostraArgomentoIntent`).

## Why the visuals are deliberately basic

The APL layout is intentionally plain (no fancy transitions, minimal
overlay chrome) — this is a direct consequence of the byte budget above,
not a lack of polish. Every extra visual element in the APL document (more
layout nodes, richer styling, animations) is more bytes spent on rendering
instructions instead of on the batch of actual photo/video data — at 11k+
items, that trade-off matters. Simpler UI = more headroom to keep batches
large enough that rotating through a big library stays smooth.

## Voice interaction challenges actually solved

A few real ones worth mentioning, in case you're building something
similar:

- **Invocation collision**: covered above (the "Limone" naming choice) — a
  generic name increases the odds Alexa opens a *different* skill entirely
  on a similar-sounding request.
- **Free-text place/month matching**: `MostraArgomentoIntent` uses
  `AMAZON.SearchQuery`, which is intentionally unconstrained by Alexa (no
  fixed slot values) — matching what the user actually said against
  hundreds of real place names/dates in the library needs fuzzy,
  forgiving matching server-side, not exact string equality.
- **Double-speed regression on "resume"**: `AMAZON.ResumeIntent` for photos
  was sending an extra `SendEvent` left over from an earlier version, which
  made the slideshow visibly speed up every time you said "riprendi"
  ("resume") — the touch/screen equivalent didn't have this because it went
  through a different code path, which is exactly what made it easy to miss
  at first.

## Byte budget

APL responses have an Alexa limit of 24,576 bytes — this is the real
constraint that shapes the whole architecture, not just a footnote. With a
library in the thousands, you can't hand Alexa the full list in one
response; `DIMENSIONE_BLOCCO` in `lambda_function.py` and `BATCH_MASSIMO` in
`server.py` (both set to **65** in the deployment above) fetch and rotate
through the library in fixed-size batches instead, so the response size
stays constant no matter whether the library has 100 photos or 11,000. The
two constants must stay aligned between the two files, and should be
verified with REAL data (real captions, not synthetic ones: short ones
underestimate the cost) before raising them.

## Setup from scratch

You need your own server (VPS or an always-on machine, reachable via HTTPS)
and an Amazon Developer account to create the skill.

### 1. Server (`server.py`)

```bash
pip install aiohttp pillow
```

Change in `server.py` (and in `lambda_function.py`, they must match):
- `API_TOKEN` — a secret string of your choice
- The base folders (`originali/`, `ridimensionate/`, `video_originali/`,
  etc.) if you want different paths than the defaults

Start it (systemd/supervisor recommended to keep it always on):
```bash
python3 server.py  # listens on port 8770 by default
```

Put it behind an HTTPS reverse proxy (nginx/Caddy) — the Alexa skill and the
APL components require HTTPS, not plain HTTP. If you don't already have a
domain, a free service like [nip.io](https://nip.io) (wildcard DNS that
resolves `IP-with-dashes.nip.io` to your IP) saves you from buying one just
for this.

### 2. Alexa skill

1. Create a custom skill on [developer.amazon.com](https://developer.amazon.com/alexa/console/ask) — type "Custom", model "Provision your own"
2. Upload `interaction_model.json` as the interaction model (JSON Editor tab, or `ask smapi set-interaction-model` from the command line)
3. Create a Lambda function on AWS (Python 3.12), paste in `lambda_function.py`, install the dependencies from `requirements.txt` into the same package
4. In the Lambda, update `BOT_URL` and `BOT_TOKEN` with your server's URL and the token you chose above
5. Link the Lambda's ARN as the skill's endpoint

### 3. Automatic deploy (optional)

The `.github/workflows/deploy.yml` workflow automates steps 3-4 (build +
upload to Lambda) and deploying `server.py` to a VPS over SSH. It requires
these Secrets in the GitHub repo: `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `GH_PAT` (a
Personal Access Token with read permission on this repo, for cloning it on
the VPS). Adjust the function/path names in the workflow to your own.

`interaction_model.json` always stays manual (see above) even with CI
enabled.

## Main server endpoints

| Endpoint | Use |
|---|---|
| `GET /api/foto/lista`, `/api/video/lista` | Paginated listing for the companion app |
| `GET /api/foto/batch`, `/api/video/batch` | Batch of photos/videos for the APL slideshow |
| `GET /api/foto/cerca?q=...` | Voice search by month/place |
| `GET /api/foto/accadde-oggi` | "On this day" — same day/month in past years |
| `POST /api/foto/preferita`, `/api/video/preferita` | Toggle favorite |
| `POST /api/foto/upload`, `/api/video/upload` | Upload from the companion app |
| `GET /api/impostazioni`, `POST /api/impostazioni` | Slideshow duration |

All of them require `Authorization: Bearer <API_TOKEN>`, except the binary
endpoints (`/api/foto/binario/...`, `/api/foto/miniatura/...`) which also
accept `?token=<API_TOKEN>` as a query string — necessary because APL's
Image/Video components load the URL directly, without being able to set
custom headers.

## Known limitations (not fixed yet)

Being upfront about what still doesn't work perfectly, instead of hiding it:

- **The display can go idle after a few minutes with no interaction**, even
  though the skill session itself is kept open on purpose
  (`should_end_session = False`, exactly so this is meant to behave like an
  always-on picture frame). In practice, saying the explicit voice command
  again ("show photos" / "show videos" / "show photos and videos") is the
  reliable way to bring it back — this reopens/re-renders the APL document
  fresh rather than resuming the one that went idle.
- **Mixed mode (photos + videos together) has been observed to freeze on a
  single photo after roughly 16 minutes** of continuous playback, requiring
  the same fix: say the voice command again to restart it. This looks like
  a reliability limit of long-running APL autoplay sessions on Echo Show
  rather than a simple logic bug, but it hasn't been root-caused yet.

If you dig into this and figure out the actual cause, a PR or an issue with
findings would be very welcome.

## License

MIT — see [LICENSE](LICENSE).
