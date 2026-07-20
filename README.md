# Video Studio — VMAF-guided compression & editing

**Repository:** [github.com/popel1988/av1-vmaf-studio](https://github.com/popel1988/av1-vmaf-studio)  
**Container image (GHCR):** `ghcr.io/popel1988/av1-vmaf-studio:latest`

A production-ready all-in-one tool for space-saving video compression with
**VMAF-guided quality selection**, a modern dashboard, live hardware metrics, and
hardware encoding for **Nvidia (NVENC)**, **Intel (QSV/VAAPI)**, **AMD (VAAPI)**,
plus a CPU fallback (**SVT-AV1 / x265 / x264**).

![Dashboard](https://img.shields.io/badge/UI-FastAPI%20Dashboard-22d3ee) ![VMAF](https://img.shields.io/badge/VMAF-libvmaf-38bdf8) ![Codecs](https://img.shields.io/badge/Codecs-AV1%20%7C%20HEVC%20%7C%20H.264-6366f1) ![HDR](https://img.shields.io/badge/HDR-HDR10%20%7C%20HLG%20%7C%20Dolby%20Vision-f59e0b)

---

## Contents

- [Feature overview](#feature-overview)
- [Codecs & encoders](#codecs--encoders)
- [Containers](#containers)
- [HDR & Dolby Vision](#hdr--dolby-vision)
- [Rate control & quality](#rate-control--quality)
- [Remux & editing (no re-encode)](#remux--editing-no-re-encode)
- [Multiple input & output locations](#multiple-input--output-locations)
- [Quality assurance](#quality-assurance)
- [Automation & integration](#automation--integration)
- [Persistence & data layout](#persistence--data-layout)
- [Deployment](#deployment)
- [Local development](#local-development-without-docker)
- [Project structure](#project-structure)
- [Configuration (environment variables)](#configuration-environment-variables)

---

## Feature overview

The dashboard (FastAPI + HTML/CSS/JS, no frontend build step) is split into
several pages — switchable via the sidebar, including multiple themes and a
DE/EN language toggle:

| Page | Purpose |
|------|---------|
| **Encoding** | Direct encoding with a manual quality value and all video/audio/HDR options. |
| **VMAF Tool** | Pure comparison of multiple encoders/codecs & quality levels with charts, screenshots, and “→ Encoding” transfer. |
| **Super Tool** | Guided batch processing: target VMAF, representative VMAF, or fixed quality for entire folders. |
| **Audio optimization** | Audio-only remux: transcode bloated audio tracks, copy video 1:1. |
| **Remux & edit** | Lossless container editing (no video re-encode): add/remove/reorder tracks, edit flags/language/title, external tracks, attachments, chapters, trim, extract — plus merge & split. |
| **A/B compare** | Side-by-side original vs. encode playback in the browser. |
| **Queue** | Live progress (bar, FPS, bitrate, ETA), pause/resume, reorder, cancel. |
| **Stats** | Historical job analytics (SQLite): savings, VMAF, runtimes. |
| **Library** | Recursive scan of the media tree with filters and savings estimates. |
| **Data & archives** | Browse saved VMAF sessions and encode directly from them. |
| **Settings** | Parallel encodes, watch folder, notifications, API keys, profiles. |
| **Diagnostics** | System health self-test including functional encoder tests. |

Other highlights:

- **Sidebar with live hardware rings**: CPU, RAM, and GPU(s) via `psutil`,
  `nvidia-smi`, sysfs (AMD `gpu_busy_percent`), and `intel_gpu_top`.
- **Structured file/folder browser** for `/media` (recursive, with filter/sort).
- **Multiple named input roots** and **multiple output volumes** (see below).
- **Per-job target folder**: pick the output volume and/or a subfolder for every
  encode, remux, merge, split, and extract job (with an output folder browser).
- **Functional encoder detection**: mini test encodes verify what the hardware
  can actually do; unavailable options are hidden in the UI.
- **Dynamic GPU capacity**: configurable number of concurrent encodes per GPU.
- **Persistent queue**: open jobs survive restarts/rebuilds; “awaiting selection”
  jobs keep their already computed VMAF analysis.

---

## Codecs & encoders

The combination of **platform** (CPU/GPU) and **codec** automatically selects the
matching FFmpeg encoder:

| Platform | AV1 | HEVC | H.264 |
|----------|-----|------|-------|
| **CPU** | `libsvtav1` (SVT-AV1) | `libx265` (x265) | `libx264` (x264) |
| **Nvidia (NVENC)** | `av1_nvenc` | `hevc_nvenc` | `h264_nvenc` |
| **Intel (QSV)** | `av1_qsv` | `hevc_qsv` | `h264_qsv` |
| **Intel (VAAPI, default)** | `av1_vaapi` | `hevc_vaapi` | `h264_vaapi` |
| **AMD (VAAPI)** | `av1_vaapi` | `hevc_vaapi` | `h264_vaapi` |

- The Intel backend is switchable via `INTEL_ENCODER` (`vaapi` = default, `qsv`).
- If an encoder is missing from the FFmpeg build, the job is rejected with a clear
  error (instead of failing silently) and available encoders are listed.
- In the **VMAF Tool**, multiple encoders/codecs can be compared at once;
  CQ test values are shifted per codec into a comparable quality range.

---

## Containers

| UI value | Result |
|----------|--------|
| **Automatic** | AV1 → `.mkv`, HEVC → `.mkv`, H.264 → `.mp4` |
| **MKV** | force Matroska |
| **MP4** | force MP4 |

**MKV is recommended** — it supports AV1/HEVC/H.264 and all subtitle formats.
**MP4** automatically converts text subtitles to `mov_text`; image-based subtitles
(PGS/VobSub) are not possible in MP4 and are dropped.

---

## HDR & Dolby Vision

HDR handling depends on the source and is chosen per job.

**HDR10 / HLG sources:**

| Mode | Effect |
|------|--------|
| **Keep HDR (10-bit)** | preserves HDR10/HLG metadata unchanged (no DV layer). |
| **HDR → SDR (tone mapping)** | converts to SDR; the VMAF reference is tonemapped identically to avoid skewed scores. |

**Dolby Vision sources** (additional choice):

| Mode | Effect |
|------|--------|
| **Preserve Dolby Vision (RPU)** | keeps the dynamic DV layer (see table below). |
| **Keep HDR10 base only** | discards the DV RPU, keeps the HDR10 base layer. |
| **HDR → SDR (tone mapping)** | converts to SDR. |

**How “preserve DV” works per target:**

| Target codec | Platform | Result |
|--------------|----------|--------|
| **HEVC** | any (HW or CPU) | **Profile 8.1** — HDR10 base + RPU via `dovi_tool` reinjection after the encode. |
| **AV1** | **CPU (SVT-AV1)** | **Profile 10.1** — RPU embedded natively during encode via `libsvtav1 -dolbyvision`. |
| **AV1** | Nvidia / Intel / AMD | DV not possible → **automatic HDR10 fallback** (with a log note). |

Source profiles 5, 7 (dual-layer → converted to 8.1), and 8 are supported for HEVC
targets. `dovi_tool` does **not** process AV1 on the CLI — so AV1 DV is created only
when encoding on the CPU. If a DV step fails, the HDR10-compatible base layer is kept
(best-effort; the job does not fail).

---

## Rate control & quality

- **Rate modes**: CQ/QP/CRF (quality number), fixed bitrate (CBR), or average
  bitrate (VBR target).
- **Two-pass** (CPU encoders in bitrate mode) for more consistent quality;
  NVENC uses `-multipass` instead.
- **Chunked adaptive encoding** (CQ mode): segments with complexity-based CQ —
  demanding scenes get more bits, calm scenes fewer.
- **Auto-crop** (`cropdetect`): black letterbox/pillarbox bars are detected and
  removed before encoding. VMAF analysis and the guardrail automatically use the
  same cropped area so scores stay correct.
- **Film-grain synthesis** (AV1/SVT) and **denoise** (light/medium/strong).
- **Anime mode**: VMAF-NEG model + 10-bit encode against banding.
- **Scaling**: downscale to a target height; the test encode is scaled back up to
  the original resolution for the VMAF comparison.
- **Per-track audio**: copy / re-encode (AAC/Opus/AC3/E-AC3/FLAC) / remove,
  channel downmix, and loudness normalization (EBU R128).
- **Per-track subtitles & chapters**, metadata preservation, automatic
  `mov_text`/`tx3g`→SRT conversion.

---

## Remux & editing (no re-encode)

The **Remux & edit** page manipulates the container without touching the video
stream (`-c:v copy`), so it is near-instant and lossless. It shares the same
safety features as encoding (integrity check, safe post-processing) and runs
through the normal queue.

| Capability | Details |
|------------|---------|
| **Track selection** | Keep/remove individual audio & subtitle tracks. |
| **Reorder** | Move tracks up/down; the order defines the output order. |
| **Track metadata** | Edit `default`/`forced` disposition, language, and title per track. |
| **External tracks** | Add audio/subtitle files (also several streams from one file), with optional delay, language, and title. |
| **Attachments** | Keep existing and add new fonts/covers (MKV only). |
| **Chapters** | Keep, remove, rename, or import chapters (FFmetadata). |
| **Trim** | Lossless cut by start/end time. |
| **Extract** | Export selected tracks to standalone files. |
| **Container compatibility** | MP4 limitations are checked up front (e.g. image subtitles), with warnings and optional per-track transcode of incompatible audio. |
| **Merge (concat)** | Join multiple files losslessly (same codecs/parameters). |
| **Split** | Split one file at chapter boundaries or into fixed-length segments. |

---

## Media tree & output

One media mount is enough — sources and encodes live in the same tree:

- **`MEDIA_PATH` → `/media`** — host folder mounted as the media tree (read + write).
- **Standard output** — set in **Settings → Media & output** (default: `output` →
  `/media/output`). The source folder structure is mirrored underneath.
- **Per-job output mode** — Standard output · Next to source · Custom folder
  (browser in the media tree).
- **Optional extra roots** — mount more folders and list them via `MEDIA_DIRS`
  (`Name=/path`, `;`/newline separated). The browser shows each root as a named
  virtual folder.

---

## Quality assurance

- **VMAF analysis**: sample clips (1–5, evenly across the movie), 4 test encodes,
  interactive line chart, screenshots (original vs. encode), “sweet spot”
  recommendation (VMAF 93–95). Model choice is automatic: `vmaf_4k_v0.6.1.json`
  for 4K, otherwise `vmaf_v0.6.1.json` (NEG variants in anime mode).
- **Extra metrics**: besides mean VMAF, **1%-low** (mean of the worst 1% of frames),
  **harmonic mean**, plus **PSNR** and **SSIM** are reported.
- **Size prediction**: `(test clip size / clip length) × total duration` including
  savings in %.
- **Quality guardrail**: after encoding, the real VMAF of the output is measured
  on sample clips. If it is below target, it can optionally re-encode at higher
  quality — otherwise it is flagged as a warning.
- **Integrity / playability check**: full decode of the output (error detection)
  plus duration match against the source.
- **Safe original post-processing**: “replace in place” or “move to `.archiv/`”
  deletes/moves the original **only** if the integrity check and (if enabled) the
  guardrail passed — protects against data loss.

---

## Automation & integration

- **Watch folder**: automatically enqueue new files in the media tree (with a time
  window, configurable in the UI).
- **Notifications**: generic webhook, **Discord**, and **Telegram**
  (via env or UI).
- **REST API + API keys**: integrate with `*arr`/Jellyfin & scripts; jobs can be
  enqueued programmatically.
- **Profiles**: save/load reusable settings sets.
- **Post-processing**: keep original (+suffix), replace in place, or move to
  `.archiv/` (each safeguarded as above).

---

## Persistence & data layout

Everything persistent lives under **`/data`** (mount as a Docker volume). Contents
survive rebuilds/restarts as long as the volume is kept:

```
/data/queue.json            Queue (open jobs, restored on start)
/data/history.db            Job history/stats (SQLite)
/data/previews/<session>/   VMAF screenshots + analysis.json (results, source, params)
/data/vmaf/                 optionally retained VMAF session artifacts
/data/work/                 short-lived encode scratch files
```

Archived VMAF comparisons therefore survive a rebuild and can be applied directly
to encoding via **Data & archives** (as long as the source file still exists).

---

## Deployment

### A) Portainer stack directly from the Git repository (no local files)

Portainer clones the repo onto the Docker host and builds the image there.
You do **not** need to copy anything onto the server manually.

1. In Portainer: **Stacks → Add stack → Build method: _Repository_**.
2. Fill in:
   - **Repository URL**: `https://github.com/popel1988/av1-vmaf-studio`
   - **Repository reference**: `refs/heads/main`
   - **Compose path**: `docker-compose.yml`
   - (private repo: enable authentication/token)
3. Under **Environment variables**, set the paths (no file editing needed):
   - `MEDIA_PATH = /mnt/videos` (sources + encodes)
   - `DATA_PATH = /mnt/appdata/av1-studio` (persistent app folder)
   - optionally `WEB_PORT = 8080`
4. **Deploy the stack** — Portainer builds the image and starts the container.
5. In the UI under **Settings → Media & output**, set the standard output folder
   (e.g. `output` → `/media/output`).

> No Nvidia on the host? Remove the `runtime: nvidia` lines and the entire
> `deploy:` block from `docker-compose.yml`, otherwise startup will fail.

### B) Build locally & start with Compose

```bash
MEDIA_PATH=/mnt/videos DATA_PATH=./data docker compose up -d --build
```

Open the dashboard: <http://SERVER-IP:8080>

### C) Push the image to a registry (for multiple hosts)

```bash
git clone https://github.com/popel1988/av1-vmaf-studio.git
cd av1-vmaf-studio
docker build -t ghcr.io/popel1988/av1-vmaf-studio:latest .
docker push ghcr.io/popel1988/av1-vmaf-studio:latest
```

Then replace `build: .` in `docker-compose.yml` with
`image: ghcr.io/popel1988/av1-vmaf-studio:latest`. On push to `main`, GitHub Actions
builds the image automatically and publishes it under
`ghcr.io/popel1988/av1-vmaf-studio` (see `.github/workflows/docker-build.yml`).

### Hardware notes

- **Nvidia**: the [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/) must be installed (`runtime: nvidia`).
- **Intel/AMD**: `/dev/dri` is passed through. On pure Intel/AMD hosts without
  Nvidia, remove the `runtime: nvidia` and `deploy:` blocks.
- With multiple GPUs (e.g. Nvidia + Intel iGPU), `renderD128` may not be the
  iGPU — set `VAAPI_DEVICE` to `renderD129` or similar.
- `privileged: true` ensures reliable hardware metric readout. A finer-grained
  device/cap mapping is prepared as an alternative.
- **Full NVENC GPU pipeline** (`NVENC_FULL_GPU=1`) is faster but can produce green
  output depending on driver/source; the default is the robust decode-to-RAM path.

---

## Local development (without Docker)

```bash
pip install -r requirements.txt
# FFmpeg/ffprobe with libvmaf, dovi_tool, and VMAF models must be available
export MEDIA_DIR=/path/videos VMAF_MODEL_DIR=/path/model
python app.py
```

---

## Project structure

```
app.py                  FastAPI app, routes, file-browser API, WebSocket, REST API
core/
  config.py             Paths, VMAF parameters, env configuration
  hardware.py           CPU/RAM/GPU monitoring (Nvidia/Intel/AMD)
  capabilities.py       Functional encoder capability tests (mini encodes)
  ffmpeg_utils.py       ffprobe, encoder mapping, integrity check, auto-crop
  encoder.py            FFmpeg command builder, filter chains, progress parser
  vmaf.py               VMAF pipeline (VMAF/PSNR/SSIM/percentiles, sessions)
  dolby_vision.py       Dolby Vision RPU preservation via dovi_tool (HEVC 8.1)
  chunked.py            Chunked adaptive encoding
  audio_opt.py          Audio-only remux/optimization
  remux.py              Lossless remux/edit: tracks, attachments, chapters, trim, concat/split
  queue_manager.py      Async queue, guardrail, post-processing, persistence
  supertool.py          Guided batch processing (target/representative VMAF)
  library.py            Library scan + savings estimate
  data_browser.py       Data/archive browser
  history.py            Job history (SQLite)
  watcher.py            Watch-folder automation
  scheduler.py          Scheduling/time windows
  notify.py             Notifications (webhook/Discord/Telegram)
  apikeys.py            API key management (REST/integration)
  profiles.py           Settings profiles
  diagnostics.py        Self-test/system diagnostics
templates/index.html    Dashboard markup
static/css/styles.css   Theme system
static/js/app.js        Dashboard logic (WebSocket, charts, browser)
static/js/i18n.js       DE/EN translation
Dockerfile              All-in-one image (FFmpeg + libvmaf + dovi_tool + models)
docker-compose.yml      Portainer stack
```

---

## Configuration (environment variables)

**Compose level** (host paths/port, see `docker-compose.yml`):

| Variable | Default | Description |
|----------|---------|-------------|
| `MEDIA_PATH` | `/media` | Host media tree → `/media` (read + write) |
| `DATA_PATH` | `./data` | Persistent app folder → `/data` |
| `WEB_PORT` | `8080` | Host port of the dashboard |

**App level** (inside the container):

| Variable | Default | Description |
|----------|---------|-------------|
| `MEDIA_DIR` | `/media` | Media tree inside the container |
| `MEDIA_DIRS` | – | Extra named roots, e.g. `Movies=/media/movies;Series=/media/series` (`;`/newline). |
| `DATA_DIR` | `/data` | Root for queue, history, sessions, cache |
| `VMAF_MODEL_DIR` | `/usr/local/share/model` | Path to VMAF models |
| `RETAIN_VMAF_SESSIONS` | `true` | Keep VMAF artifacts after analysis |
| `METRICS_INTERVAL` | `1.5` | Live metrics refresh interval (s) |
| `VMAF_CLIP_SECONDS` | `30` | Length of the VMAF test clip (s) |
| `VERIFY_MAX_RETRIES` | `2` | Guardrail: max encode retries |
| `VERIFY_CQ_STEP` | `3` | Guardrail: CQ reduction per retry |
| `VERIFY_BITRATE_FACTOR` | `1.25` | Guardrail: bitrate factor per retry |
| `VERIFY_CLIP_SECONDS` | `15` | Guardrail: measurement clip length (s) |
| `MAX_PARALLEL_ENCODES` | `0` | Parallel encodes (0 = derive from hardware) |
| `PARALLEL_ENCODES_LIMIT` | `6` | Max parallelism selectable in the UI |
| `INTEL_ENCODER` | `vaapi` | Intel backend: `vaapi` or `qsv` |
| `VAAPI_DEVICE` | `/dev/dri/renderD128` | DRM render node for QSV/VAAPI |
| `NVENC_FULL_GPU` | `false` | Force full GPU pipeline (faster, riskier) |
| `CQ_SWEETSPOT` | – | CQ fine-tuning, e.g. `cpu:hevc=22,nvidia:av1=33` |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FFMPEG_CMD` | `true` | Log the full FFmpeg command line |
| `APP_PASSWORD` | – | Optional login protection (empty = open) |
| `NOTIFY_WEBHOOK_URL` | – | Generic webhook for notifications |
| `DISCORD_WEBHOOK_URL` | – | Discord webhook |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | – | Telegram notifications |

> Nvidia-specific variables (`NVIDIA_VISIBLE_DEVICES`, `NVIDIA_DRIVER_CAPABILITIES`,
> optionally `NVIDIA_DISABLE_REQUIRE`) are documented in `docker-compose.yml`.
