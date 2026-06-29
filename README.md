# AV1 / VMAF Compression Studio

Ein produktionsbereites All-in-One-Tool zur platzsparenden Video-Komprimierung
mit **VMAF-gesteuerter Qualitätsfindung**, modernem Dashboard, Live-Hardware-
Metriken und Hardware-Encoding für **Nvidia (NVENC)**, **Intel (QSV/VAAPI)**,
**AMD (VAAPI)** sowie CPU-Fallback (SVT-AV1 / x265 / x264).

![Dashboard](https://img.shields.io/badge/UI-FastAPI%20Dashboard-22d3ee) ![VMAF](https://img.shields.io/badge/VMAF-libvmaf-38bdf8)

## Features

- **Modernes Dashboard** (FastAPI + HTML/CSS, kein Build-Step nötig) mit 4 Themes:
  Server Anthrazit, Dark, Light, Cyberpunk/Neon – alles per Dropdown umschaltbar.
- **Sidebar mit Echtzeit-Hardware-Ringen**: CPU, RAM und GPU(s) via `psutil`,
  `nvidia-smi`, sysfs (AMD `gpu_busy_percent`) und `intel_gpu_top`.
- **Strukturierter Datei-/Ordner-Browser** für `/media/input` (rekursiv).
- **Automatischer VMAF-Check**: 30-Sek-Ausschnitt aus der Mitte, 4 Test-Encodes
  (CQ/QP 20/24/28/32), interaktives Liniendiagramm und „Sweet Spot"-Empfehlung
  (VMAF 93–95).
  - **Downscaling**: das herunterskalierte Test-Encode wird für den Vergleich in
    der FFmpeg-Filter-Pipeline wieder auf die Originalauflösung hochskaliert.
  - **HDR→SDR**: die Referenz wird ebenfalls getonemappt (gleiche Farbdomäne),
    um Score-Verfälschungen zu vermeiden.
  - **Modellwahl**: `vmaf_4k_v0.6.1.json` bei 4K-Quellen, sonst `vmaf_v0.6.1.json`.
- **Größenprognose**: `(Größe_Testclip / 30) * Gesamtlänge` inkl. Ersparnis in %.
- **Live-Fortschritt**: Balken, FPS, Bitrate, ETA, aktuelle Größe, eingesparter
  Speicher – plus Warteschlangen-Tabelle mit farbigen Status-Badges.
- **Post-Processing**: Original behalten (+Suffix), Inplace-Ersetzung oder
  Verschieben nach `.archiv/`.
- **Batch-Modus**: VMAF-Test repräsentativ für die erste Datei, Wert wird auf den
  ganzen Ordner angewendet.

## Deployment

### A) Portainer-Stack direkt aus dem Git-Repository (ohne lokale Dateien)

Portainer klont das Repo selbst auf den Docker-Host und baut das Image dort.
Du musst also **nichts** manuell auf den Server kopieren.

1. Projekt in ein Git-Repo pushen (GitHub/GitLab, kann privat sein).
2. In Portainer: **Stacks → Add stack → Build method: _Repository_**.
3. Felder ausfüllen:
   - **Repository URL**: z. B. `https://github.com/<user>/av1convert-vmaf`
   - **Repository reference**: `refs/heads/main`
   - **Compose path**: `docker-compose.yml`
   - (privates Repo: Authentifizierung/Token aktivieren)
4. Unter **Environment variables** die Pfade setzen (kein Datei-Editieren nötig):
   - `INPUT_PATH = /mnt/videos`
   - `OUTPUT_PATH = /mnt/output`
   - optional `WEB_PORT = 8080`
5. **Deploy the stack** – Portainer baut das Image und startet den Container.

> Kein Nvidia im Host? Entferne in `docker-compose.yml` die Zeilen `runtime: nvidia`
> und den kompletten `deploy:`-Block, sonst schlägt der Start fehl.

### B) Lokal bauen & über Compose starten

1. Pfade als Env-Variablen setzen (oder in der Compose-Datei eintragen) und starten:

```bash
INPUT_PATH=/mnt/videos OUTPUT_PATH=/mnt/output docker compose up -d --build
```

2. Dashboard öffnen: <http://SERVER-IP:8080>

### C) Image in eine Registry pushen (für mehrere Hosts)

```bash
docker build -t ghcr.io/<user>/av1-vmaf-studio:latest .
docker push ghcr.io/<user>/av1-vmaf-studio:latest
```

Danach in `docker-compose.yml` `build: .` durch `image: ghcr.io/<user>/av1-vmaf-studio:latest`
ersetzen – Portainer zieht dann nur noch das fertige Image.

### Hinweise zur Hardware

- **Nvidia**: Es muss das [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/) installiert sein (`runtime: nvidia`).
- **Intel/AMD**: `/dev/dri` wird durchgereicht. Auf reinen Intel/AMD-Systemen ohne
  Nvidia kann der `runtime: nvidia`- und `deploy:`-Block in der Compose-Datei
  entfernt werden.
- `privileged: true` garantiert die fehlerfreie Auslesung der Hardware-Metriken.
  Alternativ ist in der Compose-Datei ein feingranulares Device-/Cap-Mapping
  vorbereitet.

## Lokale Entwicklung (ohne Docker)

```bash
pip install -r requirements.txt
# FFmpeg/ffprobe mit libvmaf müssen im PATH liegen
export INPUT_DIR=/pfad/videos OUTPUT_DIR=/pfad/output VMAF_MODEL_DIR=/pfad/model
python app.py
```

## Projektstruktur

```
app.py                  FastAPI-App, Routen, Datei-Browser-API, WebSocket
core/
  config.py             Pfade, VMAF-Parameter, Konstanten
  hardware.py           CPU/RAM/GPU-Monitoring (Nvidia/Intel/AMD)
  ffmpeg_utils.py       ffprobe + Encoder-/Qualitäts-Flag-Mapping
  encoder.py            FFmpeg-Kommandobau + Progress-Parser
  vmaf.py               VMAF-Pipeline (Referenz, Test-Encodes, Vergleich)
  queue_manager.py      Asynchrone Hintergrund-Warteschlange (Worker-Thread)
templates/index.html    Dashboard-Markup
static/css/styles.css   Theme-System
static/js/app.js        Dashboard-Logik (WebSocket, Charts, Browser)
Dockerfile              All-in-One Image
docker-compose.yml      Portainer-Stack
```

## Konfiguration (Umgebungsvariablen)

| Variable           | Default                  | Beschreibung                         |
|--------------------|--------------------------|--------------------------------------|
| `INPUT_DIR`        | `/media/input`           | Quellverzeichnis (read)              |
| `OUTPUT_DIR`       | `/media/output`          | Zielverzeichnis                      |
| `VMAF_MODEL_DIR`   | `/usr/local/share/model` | Pfad zu den VMAF-Modellen            |
| `METRICS_INTERVAL` | `1.5`                    | Refresh-Intervall der Live-Metriken  |
| `VMAF_CLIP_SECONDS`| `30`                     | Länge des VMAF-Testausschnitts       |
