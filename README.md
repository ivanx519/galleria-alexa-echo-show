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

## Byte budget

APL responses have an Alexa limit of 24,576 bytes. `DIMENSIONE_BLOCCO` in
`lambda_function.py` and `BATCH_MASSIMO` in `server.py` must stay aligned —
verify them with REAL data (real captions, not synthetic ones: short ones
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

## License

MIT — see [LICENSE](LICENSE).
