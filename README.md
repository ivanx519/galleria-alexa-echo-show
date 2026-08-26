# Galleria Alexa — Skill Foto/Slideshow

Cornice digitale su Echo Show: slideshow delle foto/video di famiglia, con
ricerca vocale per mese o luogo ("mostra dicembre", "mostra Villaspeciosa").

> **Perché "Limone"?** Il nome di invocazione della skill (quello che dici ad
> Alexa per aprirla, es. "Alexa, apri Limone") è volutamente una parola
> insolita e senza nessun legame ovvio con foto/gallerie. È una scelta fatta
> apposta: nomi generici tipo "galleria" o "foto" rischiano di far confondere
> Alexa con altre skill che usano parole simili, aprendo la cosa sbagliata.
> Una parola corta, riconoscibile e improbabile come "limone" evita
> ambiguità — puoi usare qualsiasi parola funzioni allo stesso modo per la
> tua, basta cambiarla in `interaction_model.json`.

## Componenti

| File | Dove gira | Deploy |
|---|---|---|
| `lambda_function.py` | AWS Lambda `galleria-alexa-skill` | `aws lambda update-function-code` (GitHub Actions) |
| `server.py` | VPS, servizio systemd `foto-slideshow-api` (porta 8770) | SSH + `systemctl restart` (GitHub Actions) |
| `ridimensiona.py` | VPS, batch manuale | ridimensiona le foto originali + geocoding (Nominatim) |
| `ridimensiona_video.py` | VPS, batch manuale | comprime i video originali (ffmpeg, 720p H.264) |
| `organizza_per_anno.py` | VPS, batch manuale | crea `Foto/<anno>/` e `Video/<anno>/` via hardlink per l'accesso SFTP |
| `interaction_model.json` | Alexa Developer Console | caricato via `ask smapi set-interaction-model` (manuale, non in CI) |

Skill ID: il tuo, assegnato quando crei la skill nella Alexa Developer Console (vedi sezione Setup)

## Deploy automatico

Push su `master` → GitHub Actions:
- **deploy-lambda**: installa `requirements.txt` in `package/`, copia `lambda_function.py`, zippa, aggiorna la Lambda su AWS (credenziali scoped, permessi limitati a `freezer-alexa-skill`/`galleria-alexa-skill`)
- **deploy-vps**: `git pull` di questo repo in `/opt/foto_slideshow_repo` sulla VPS, copia `server.py` e i tre script in `/opt/foto_slideshow/`, riavvia `foto-slideshow-api`

`interaction_model.json` NON viene applicato automaticamente — un cambio ai
comandi vocali richiede un giro manuale con `ask smapi set-interaction-model`
(async, verificare `ask smapi get-skill-status` per la conferma "SUCCEEDED").

## Byte budget

Le risposte APL hanno un limite Alexa di 24.576 byte. `DIMENSIONE_BLOCCO` in
`lambda_function.py` e `BATCH_MASSIMO` in `server.py` devono restare
allineati — vanno verificati con dati REALI (didascalie vere, non
sintetiche: quelle corte sottostimano il costo) prima di alzarli.

## Setup da zero

Serve un tuo server (VPS o macchina sempre accesa, raggiungibile via HTTPS)
e un account Amazon Developer per creare la skill.

### 1. Server (`server.py`)

```bash
pip install aiohttp pillow
```

Cambia in `server.py` (e in `lambda_function.py`, devono coincidere):
- `API_TOKEN` — una stringa segreta a tua scelta
- Le cartelle base (`originali/`, `ridimensionate/`, `video_originali/`, ecc.)
  se vuoi percorsi diversi da quelli di default

Avvialo (systemd/supervisor consigliato per tenerlo sempre su):
```bash
python3 server.py  # ascolta di default sulla porta 8770
```

Mettilo dietro un reverse proxy HTTPS (nginx/Caddy) — la skill Alexa e i
componenti APL richiedono HTTPS, non HTTP semplice. Se non hai già un
dominio, un servizio gratuito come [nip.io](https://nip.io) (DNS wildcard
che risolve `IP-con-trattini.nip.io` al tuo IP) evita di doverne comprare uno
solo per questo.

### 2. Skill Alexa

1. Crea una skill custom su [developer.amazon.com](https://developer.amazon.com/alexa/console/ask) — tipo "Custom", modello "Provision your own"
2. Carica `interaction_model.json` come modello di interazione (tab JSON Editor, oppure `ask smapi set-interaction-model` da riga di comando)
3. Crea una funzione Lambda su AWS (Python 3.12), incolla `lambda_function.py`, installa le dipendenze di `requirements.txt` nello stesso pacchetto
4. Nella Lambda, aggiorna `BOT_URL` e `BOT_TOKEN` con l'URL del tuo server e il token scelto sopra
5. Collega l'ARN della Lambda come endpoint della skill

### 3. Deploy automatico (opzionale)

Il workflow `.github/workflows/deploy.yml` automatizza i passi 3-4 (build +
upload Lambda) e il deploy di `server.py` su una VPS via SSH. Richiede questi
Secrets nel repo GitHub: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `GH_PAT` (un Personal Access Token con
permesso di leggere questo repo, per il clone sulla VPS). Adatta i nomi di
funzione/percorsi nel workflow ai tuoi.

`interaction_model.json` resta sempre manuale (vedi sopra) anche con la CI
attiva.

## Endpoint principali del server

| Endpoint | Uso |
|---|---|
| `GET /api/foto/lista`, `/api/video/lista` | Elenco paginato per l'app companion |
| `GET /api/foto/batch`, `/api/video/batch` | Blocco di foto/video per lo slideshow APL |
| `GET /api/foto/cerca?q=...` | Ricerca vocale per mese/luogo |
| `GET /api/foto/accadde-oggi` | "Accadde oggi" — stesso giorno/mese negli anni passati |
| `POST /api/foto/preferita`, `/api/video/preferita` | Toggle preferito |
| `POST /api/foto/upload`, `/api/video/upload` | Caricamento da app companion |
| `GET /api/impostazioni`, `POST /api/impostazioni` | Durata dello slideshow |

Tutti richiedono `Authorization: Bearer <API_TOKEN>`, tranne gli endpoint
binari (`/api/foto/binario/...`, `/api/foto/miniatura/...`) che accettano
anche `?token=<API_TOKEN>` in query string — necessario perché i componenti
Image/Video di APL caricano l'URL direttamente, senza poter impostare header
custom.

## Licenza

MIT — vedi [LICENSE](LICENSE).
