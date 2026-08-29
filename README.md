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
- [FAQ](#faq)
- [Naming, duplicates & dry-run](#naming-duplicates--dry-run)
- [Remux & editing (no re-encode)](#remux--editing-no-re-encode)
- [Video editor](#video-editor)
- [Media tree & output](#media-tree--output)
- [Quality assurance](#quality-assurance)
- [Automation & integration](#automation--integration)
- [REST API](#rest-api)
- [Persistence & data layout](#persistence--data-layout)
- [Deployment](#deployment)
- [Local development](#local-development-without-docker)
- [Project structure](#project-structure)
- [Configuration (environment variables)](#configuration-environment-variables)

---

## Feature overview

The dashboard (FastAPI + HTML/CSS/JS, no frontend build step) is split into
several pages — switchable via the sidebar, with multiple themes, **compact
mode**, and language support for **DE / EN / ES / FR**:

| Page | Purpose |
|------|---------|
| **Encoding** | Direct encoding with CQ/bitrate/ABR **or target-VMAF** (test encodes, then automatic encode), size target, naming templates, audio/HDR options, dry-run preview. Source browser and settings sit side by side. |
| **VMAF Tool** | Pure comparison of multiple encoders/codecs & quality levels with charts, screenshots, and “→ Encoding” transfer. Same two-column layout as Encoding. |
| **Super Tool** | Guided batch processing: target VMAF, representative VMAF, or fixed quality for entire folders (incl. remux-only profiles). |
| **Audio optimization** | Audio-only remux: transcode bloated audio tracks, copy video 1:1. |
| **Remux & edit** | Lossless container editing (no video re-encode): add/remove/reorder tracks, edit flags/language/title, external tracks, attachments, chapters, trim, extract — plus merge & split. |
| **Editor** | Timeline editor: In/Out cuts, keep/cut ranges, reorder, multi-file concat, **direct upload**, per-source audio/subtitle selection, remux (keyframe copy) or encode export (CQ/CBR/ABR) to the queue. |
| **Player** | Full media player: Direct-Play when possible, else HLS; quality profiles (NVENC/CPU from diagnostics), chapters, text subs + optional PGS burn-in. |
| **A/B compare** | Side-by-side original vs. encode playback in the browser. |
| **Queue** | Live progress (bar, FPS, bitrate, ETA), pause/resume, reorder, cancel, **requeue** finished jobs. |
| **Stats** | Historical job analytics (SQLite): savings, VMAF, runtimes; requeue from history. |
| **Library** | Recursive scan with live filters, savings estimates, **sub-libraries** (each scan cached per root), codec/dynamic filters, CSV export. |
| **Data & archives** | Browse saved VMAF sessions and encode directly from them. |
| **Settings** | Parallel encodes, watch folder, notifications, API keys, profiles, default output folder. |
| **Diagnostics** | System health self-test including functional encoder tests. |
| **FAQ** | In-app explanations of CQ/CBR/ABR, HDR / Dolby Vision profiles, VMAF, and containers. |

Other highlights:

- **Sidebar with live hardware rings**: CPU, RAM, and GPU(s) via `psutil`,
  `nvidia-smi`, sysfs (AMD `gpu_busy_percent`), and `intel_gpu_top`.
- **Global search** across the media tree (sidebar).
- **Structured file/folder browser** for `/media` (recursive, with filter/sort).
- **Multiple named input roots** via `MEDIA_DIRS` (optional extra mounts).
- **Per-job output mode**: standard output · next to source · custom folder
  (browser in the media tree).
- **In-browser media player** (sidebar **Player**): HLS sessions with probe
  duration / seek-restart, audio remux to AAC, WebVTT text subs; plus a lighter
  modal preview (`/api/media/stream`).
- **Video editor** (sidebar **Editor**): visual timeline with source ruler and
  clip list, library sources or uploads (`/data/uploads` or a chosen media
  folder), multi-track audio/subtitles, remux (concat copy) or encode into the
  queue. See [Video editor](#video-editor).
- **Functional encoder detection**: mini test encodes verify what the hardware
  can actually do; unavailable options are hidden in the UI.
- **Dynamic GPU capacity**: configurable number of concurrent encodes per GPU.
- **Persistent queue**: the queue list (open + recent finished/failed) survives
  restarts/rebuilds via `/data/queue.json` (keep a stable `DATA_PATH` volume);
  “awaiting selection” jobs keep their already computed VMAF analysis.
- **Requeue**: finished jobs can be run again (overwrite warning or auto-suffix
  like `_remux2`) or opened with their exact settings (“Again with …”).

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

**Image toolchain (current defaults):**

| Component | Version / notes |
|-----------|-----------------|
| Base image | CUDA **12.6.3** runtime (Ubuntu 24.04) |
| FFmpeg | BtbN **n8.1** (GPL, with NVENC / libvmaf / …) |
| `dovi_tool` | **2.3.3** |
| libva | **2.22** (Intel VAAPI) |

Older Nvidia host drivers may refuse to start the container (CUDA version check).
See `docker-compose.yml` for `NVIDIA_DISABLE_REQUIRE` and related notes.

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

- **Rate modes**: CQ/QP/CRF (quality number), fixed bitrate (CBR), average
  bitrate (VBR / ABR target), or **target VMAF** (test encodes, then the smallest
  file with VMAF ≥ target is encoded automatically — same idea as Super Tool).
  Folder batches still run the VMAF test only on the first file (representative).
  Multi-encoder comparison stays in the **VMAF Tool**.
- **Size target (MB)**: optional total output budget including audio — the app
  derives an ABR video bitrate before encode (distinct from the post-encode
  size cap below).
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
- **Post-encode caps**: optional max output size (MB) and max video bitrate
  (kbit/s); failed caps are reported after the job.

---

## FAQ

The same topics live in the dashboard under **FAQ** (sidebar). Below is the
technical version.

### CQ / CRF / QP

CQ does **not** set a file size. It sets how hard the encoder should avoid
visible errors; size follows. **Lower number = sharper and larger**, higher =
coarser and smaller. The Encoding slider is **10–51**, default **28**.

Same idea, different flag: CPU **CRF**, Nvidia **CQ**, Intel QSV
`global_quality`, Intel/AMD VAAPI **QP**. The same number is **not** equally
“sharp” across codecs: AV1 CQ 28 is often much smaller than HEVC CRF 28, which
is smaller than H.264 CRF 28.

There is **no reference clip or standard** that defines “CQ 28 = N dB”. Each
encoder maps the number onto its own quantization curve (H.264 QP 0–51, AV1
qindex 0–255, plus a λ for rate-distortion search). Video Studio passes the
slider **unchanged**: CPU `-crf`, NVENC `-cq` (VBR), QSV `-global_quality`,
VAAPI constant QP. Same number ≠ same look, and NVENC CQ 28 is not SVT CRF 28.
Compare encoders with **VMAF**, not the integer. The preset (SVT 6, x264
medium, NVENC p5) does not change what the number *means*; it changes how
well the encoder compresses at that setpoint.

CQ is not a bit budget per frame. The encoder minimises roughly
*distortion + λ×bits* per block. Higher CQ → higher λ → bits are “more
expensive”. Hard frames still get more bits (more residual energy); CRF/CQ
may also move QP per frame/block (adaptive quantization) so *perceived*
quality stays even. True CQP (VAAPI here) keeps QP fixed, so quality swings
more with the scene.

**Higher CQ = more error allowed**, so bitrate usually drops. Bitrate is the
consequence, not the target. Each encoder has its own yardstick: CQ 28 is
step 28 on *that* scale. Software (x264/x265/SVT) typically spends bits more
efficiently; GPU encoders are faster. The **speed preset** often matters more
than CPU vs GPU — a very fast SVT preset can lose to a good NVENC.

Speed presets are selectable under **Settings → Encoder speed** (default
**balanced**, matching the former hardcoded values: SVT-AV1 **6**, x264/x265
**medium**, NVENC **p5**, QSV **slower**). Encoding, VMAF Tool, Super Tool and
the editor can override per job. Faster = larger files / worse bit use;
slower = much more time, especially CPU AV1. VAAPI has no extra speed preset.
Diagnostics and the player transcode stay on fast presets on purpose.
The Film/Series/Anime chips are job templates (CQ, codec, anime mode), not
encoder speed.

Under **Settings → Encoder speed** you can run an **encoder test**: download
free reference clips of different picture types (4K excerpts from Blender’s
Big Buck Bunny and Sintel, Tears of Steel for live action, plus a Jellyfin
UHD ~40 Mbps test file, CC-BY-SA — allowlisted URLs only) and/or pick up to
four files from your library. The test uses the
**native presets of the selected encoder** (SVT 0–13, x264/x265
ultrafast–placebo, NVENC p1–p7, QSV veryfast–veryslow), not only the five
aliases. Default comparison is fast/balanced/slow; **All tiers** tests every
preset. VMAF uses 1–5 scene samples (encoder test default: 3). The
recommendation scores **55% mean + 35% 1% low + 10% harmonic mean**, so a
mean of 95 with 1% low 79 is not treated as “good enough”. Keep the matrix
small; CPU AV1 × all presets × 5 scenes is slow on purpose.

VMAF Tool, Encoding (target VMAF) and Super Tool can use up to **5 scene
samples** (evenly spaced; short files get shorter excerpts instead of
collapsing to one middle clip).

The encoder may spend as many bits as a scene needs to hold that quality.
Action and grain cost more; still scenes cost less. Bitrate therefore varies —
there is no fixed MB target.

| CQ | Typical use |
|----|-------------|
| 18–22 | Very high, close to the source, file stays large |
| **24–28** | Film (preset Film = 28) |
| **28–32** | Series, noticeably smaller (preset Serie = 30) |
| 34–40 | Much smaller; 1080p often already soft |
| 40+ | Clearly visible; size-first only |

Anime often uses a **lower** CQ (preset 26) because flat areas band easily.
**Target VMAF** tries several values and keeps the smallest file still ≥ target.

**Why a file can grow:** CQ means “hold quality X”. If the source is already
smaller/more efficient than what CQ X produces, output grows — e.g. already
good AV1/HEVC with CQ 20, switching to H.264, 8-bit → 10-bit (anime mode), or
keeping bulky TrueHD audio. A fat remux at CQ 28 almost always shrinks; a
small AV1 file at CQ 24 often grows. For a size cap use ABR, size target, or
target VMAF — not an extremely low CQ.

### CBR vs ABR vs CQ (one-pass)

Variable is **not** always better. It depends whether you care about quality,
approximate size, or a hard rate cap.

- **CQ** — archives when size is secondary.
- **ABR** — “about X kbit/s on average”; hard scenes may go higher (here
  typically up to **1.5×**, buffer **2×**).
- **CBR** — rate should stay at X throughout. Size is very predictable;
  quality is less even.

One-pass cannot see the future. ABR still distributes bits better than CBR
because it may vary locally. Two-pass (bitrate mode) mainly helps ABR. CBR is
worth it only when the rate must not exceed X (streaming, strict decoder
cap).

On **CPU/SVT-AV1**, CBR and ABR are technically the same (`-b:v` only, no real
CBR). The CBR vs ABR split mainly applies to **NVENC** and other hardware
encoders.

### Dynamic range (SDR / HDR / Dolby Vision)

| Kind | What it is |
|------|------------|
| **SDR** | Standard contrast (typical PC/TV SDR). |
| **HDR10** | Static metadata for the whole file (MaxCLL/MaxFALL), 10-bit PQ. |
| **HLG** | Broadcast HDR; often watchable on SDR sets. |
| **HDR10+** | Can carry per-scene metadata; this app treats it as HDR (no separate HDR10+ encode path). |
| **Dolby Vision** | Dynamic RPU (per scene/frame). Profile describes how it is stored. |

**HDR without DV:** *Keep HDR* = 10-bit + metadata. *Tone-map* = SDR for any
display (VMAF tests are tonemapped the same way so scores stay fair).

**Dolby Vision profiles (as used here):**

| Profile | Typical source | Notes |
|---------|----------------|-------|
| **5** | Streaming | IPTPQc2, **no HDR10 fallback**. Wrong colours without a DV player. Auto → **SDR**. |
| **7** | Blu-ray (dual layer) | Enhancement layer is dropped on re-encode. Auto → HEVC **8.1** (GPU ok) or AV1 **10.1** (CPU/SVT only). |
| **8 / 8.1** | Single layer + HDR10 base | Watchable as HDR10 without DV. Auto keeps the RPU when the encoder can. |
| **10.1** | AV1 DV | **CPU/SVT only** (`libsvtav1 -dolbyvision`). Nvidia/Intel/AMD cannot embed the RPU → HDR10 fallback. |

*Preserve RPU:* HEVC 8.1 via `dovi_tool` (GPU ok); AV1 10.1 CPU only. *HDR10
base only:* drop RPU, keep static HDR. *Tone-map:* safest for any display;
recommended for profile 5 without a DV player. If a DV step fails, the
HDR10-compatible base is kept and the job does not fail.

### Which tool?

| Place | Behaviour |
|-------|-----------|
| **Encoding** (fixed CQ/CBR/ABR) | Encodes immediately with your value. |
| **Encoding** (target VMAF) | Test encodes, then auto-encode the smallest value ≥ target. Folders: first file only (representative). |
| **VMAF Tool** | Compare several encoders/codecs; no encode. Pick a winner → Encoding. |
| **Super Tool** | Folder batches: per-file target VMAF, one representative test, or fixed quality. |
| **Quality guardrail** | Measures VMAF **after** encode (optional re-encode). Not a pre-comparison. |

VMAF estimates perceptual similarity (roughly 0–100). **93–95** is the
sweet-spot used here. Anime mode uses **VMAF-NEG** + 10-bit against banding.
Recommendations also require 1% low within the gap from **Settings → VMAF
recommendation** (default 6 points below the target mean). A **minimum
savings** filter (default 0% = file must not grow) skips auto-encode when
every matching tier would be larger — the source is kept, not treated as a
failure. After the coarse grid, each encoder may get **one midpoint** between
the last hit and the first miss. VMAF is not a
guarantee — screenshots and A/B compare still help.

**MKV** is recommended (all codecs and subtitle types). **MP4** converts text
subs to `mov_text` and drops image subs (PGS/VobSub).

**Anime mode** = VMAF-NEG + 10-bit. **Auto-crop** = `cropdetect` letterbox
(same crop for VMAF/guardrail). **Film grain** = AV1/CPU/SVT only; 0 = off.

---

## Naming, duplicates & dry-run

- **Name pattern**: placeholders `{stem}`, `{suffix}`, `{codec}`, `{height}`,
  `{height_suffix}`, `{vmaf}`, `{date}`.
- **On duplicate**: `ask` (preview modal) · `skip` · `overwrite`.
- **Dry-run preview**: planned output paths, existing-file / history flags, and
  estimated savings before enqueue.
- **Requeue**: if the planned output already exists, choose overwrite or a free
  suffix (`_remux` → `_remux2`, …). Job settings (including remux `edit_spec`)
  are stored in history for “Again with …”.

---

## Remux & editing (no re-encode)

The **Remux & edit** page manipulates the container without touching the video
stream (`-c:v copy`), so it is near-instant and lossless. It shares the same
safety features as encoding (integrity check, safe post-processing) and runs
through the normal queue.

| Capability | Details |
|------------|---------|
| **Track selection** | Keep/remove individual audio & subtitle tracks. |
| **Reorder** | Move tracks up/down; the order defines the output order (internal and external tracks share the same tables). |
| **Track metadata** | Edit `default`/`forced` disposition, language, and title per track. |
| **Smart disposition** | One-click intelligent Default/Forced suggestions. |
| **External tracks** | Add audio/subtitle files from the media tree or upload from the PC; duration is compared to the source (warning if they differ). Optional delay, language, title, per-track audio transcode. |
| **Attachments** | Keep existing and add new fonts/covers (MKV only); optional sidecar fonts/covers next to the source. |
| **Chapters** | Keep, remove, rename, or import chapters (FFmetadata / NFO). |
| **Trim** | Lossless cut by start/end time. |
| **Extract** | Export selected tracks to standalone files. |
| **Container compatibility** | MP4 limitations are checked up front (e.g. image subtitles), with warnings and optional per-track transcode of incompatible audio. |
| **Merge (concat)** | Join multiple files losslessly (same codecs/parameters) or with optional re-encode. |
| **Split** | Split at chapter boundaries, fixed-length segments, or custom ranges. |

---

## Video editor

The **Editor** page builds a new file from one or more sources (cuts, concat,
optional fades/tempo) and sends it to the same queue as Encoding/Remux.

**Preview & timeline**

- Direct file playback in the browser when the codec is playable; otherwise a
  light FFmpeg/HLS fallback. Jump **±0.2 s** and frame-step; **Timeline follow**
  skips cut-away parts and continues at the next clip.
- Source ruler for the whole loaded file (In/Out, keep/cut range, snap to
  chapters or keyframes). The output timeline supports trim-by-drag, fade
  corners, reorder, split, duplicate, black clips, intro/outro, undo/redo.
- Keyboard: **I/O** In/Out · **J/K/L** −5 s / pause / play · arrows for frames
  and clip change · **S** split · **Del** delete clip · **Ctrl+Z** undo.
- Unload a source from the preview without dropping clips already on the
  timeline. Projects can be saved/loaded as JSON.

**Audio & subtitles**

- Per source: tick which **audio** and **subtitle** tracks to keep (applies to
  all clips from that file). No audio selected → silent. Burn-in of the first
  ticked subtitle forces an encode.
- Clip list shows **video bitrate**, selected **audio bitrates**, and subtitle
  count so you can see what will go into the export.

**Export**

| Mode | Details |
|------|---------|
| **Remux (copy)** | Concat demuxer, cuts on keyframes. Fast; needs compatible clips (same codecs, matching audio/sub selection). |
| **Encode** | Frame-accurate. Video: CQ/CRF, CBR, or ABR (same rate modes as Encoding). Audio: copy, FLAC, AAC, Opus, AC-3, E-AC-3; optional channel mix. |

- **Audio copy on encode** is used when possible (no fades, tempo change,
  crossfade, black clips, or channel remix). Otherwise audio is re-encoded.
  Subtitles stay copy when the selection is consistent across clips.
- **Filename**: source name + suffix (e.g. `_edit`) or a custom stem.
- Output folder: standard output · next to source · custom folder (same as
  Encoding). Optional **chapters from clips**. A non-zero crossfade forces encode.

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
- **Sub-libraries** — named subsets of the media tree for Library scans
  (managed in the Library UI / `settings.json`).

---

## Quality assurance

- **VMAF analysis**: sample clips (1–5, evenly across the movie), 4 test encodes,
  interactive line chart, screenshots (original vs. encode), “sweet spot”
  recommendation (VMAF 93–95). Model choice is automatic: `vmaf_4k_v0.6.1.json`
  for 4K, otherwise `vmaf_v0.6.1.json` (NEG variants in anime mode).
- **Extra metrics**: besides mean VMAF, **1%-low** (mean of the worst 1% of frames),
  **harmonic mean**, plus **PSNR** and **SSIM** are reported. Recommendations
  (VMAF Tool, target VMAF, Super Tool, encoder test) keep 1% low within a
  **gap set under Settings → VMAF recommendation** (default 6, so target 94
  requires 1% low ≥ 88). 0 disables the floor (mean only). **Minimum savings**
  (default 0%) only recommends a tier if the predicted file is at least that
  much smaller than the source (−1 disables). If the target holds but every
  matching tier would be too large, the source is kept and auto workflows skip
  the encode. After the four-value grid, each encoder may test **one midpoint**
  between the last hit and the first miss (not a full binary search).
- **Size prediction**: `(test clip size / clip length) × total duration` including
  savings in %.
- **Quality guardrail**: after encoding, the real VMAF of the output is measured
  on sample clips. If it is below target, it can optionally re-encode at higher
  quality — otherwise it is flagged as a warning.
- **Integrity / playability check**: **sampled** decode of the output (start /
  middle / end, ~9–12 s each) plus duration match against the source — not a
  full-file decode (avoids huge RAM/time cost on large files). Toggleable; forced
  on for inplace/archive post-processing.
- **Safe original post-processing**: “replace in place” or “move to `.archiv/`”
  deletes/moves the original **only** if the integrity check and (if enabled) the
  guardrail passed — protects against data loss.

---

## Automation & integration

- **Watch folder**: automatically enqueue new files in the media tree (with a time
  window, configurable in the UI).
- **Notifications**: generic webhook, **Discord**, and **Telegram**
  (via env or UI).
- **REST API + API keys**: see [REST API](#rest-api); Sonarr/Radarr webhooks
  supported.
- **Profiles**: save/load reusable settings sets (including remux-only profiles
  that open **Remux & edit** with the current selection).
- **Post-processing**: keep original (+suffix), replace in place, or move to
  `.archiv/` (each safeguarded as above).

---

## REST API

Authenticated with API keys from **Settings**. Base path: `/api/v1`.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/health` | Liveness + FFmpeg version |
| `GET` | `/api/v1/queue` | Current queue state |
| `GET` | `/api/v1/stats` | Aggregated history stats |
| `POST` | `/api/v1/enqueue` | Enqueue a file/folder (`path`, optional `profile` / `settings`, `is_batch`) |
| `POST` | `/api/v1/webhook/arr` | Sonarr/Radarr “On Import” / “On Upgrade” webhook (`?profile=…`) |

Optional path remapping for *arr hosts: env `ARR_PATH_MAP=from:to,from2:to2`.

The dashboard itself uses many additional `/api/*` endpoints (probe, remux,
library, media stream, …) — those are session/UI oriented, not the stable
external integration surface.

---

## Persistence & data layout

Everything persistent lives under **`/data`** (mount as a Docker volume). Contents
survive rebuilds/restarts as long as the volume is kept:

```
/data/queue.json            Queue list (open + recent finished; restored on start)
/data/history.db            Job history/stats (SQLite; includes settings_json)
/data/settings.json         App settings (default output, sub-libraries, …)
/data/profiles.json         Saved encode/remux profiles
/data/apikeys.json          REST API keys
/data/notify.json           Notification config
/data/watch*.json           Watch-folder state
/data/scheduler.json        Schedule / time windows
/data/capabilities.json     Cached encoder capability tests
/data/library_scan.json     Library scan cache (per root / sub-library)
/data/previews/<session>/   VMAF screenshots + analysis.json
/data/vmaf/                 optionally retained VMAF session artifacts
/data/uploads/              User-uploaded files (editor sources, remux external tracks)
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

- **Nvidia**: the [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/) must be installed (`runtime: nvidia`). Image is based on **CUDA 12.6**; older host drivers may need `NVIDIA_DISABLE_REQUIRE=true` (see compose comments).
- **Intel/AMD**: `/dev/dri` is passed through. On pure Intel/AMD hosts without
  Nvidia, remove the `runtime: nvidia` and `deploy:` blocks. Set
  `LIBVA_DRIVER_NAME=iHD` (Intel) and `VAAPI_DEVICE` as needed.
- With multiple GPUs (e.g. Nvidia + Intel iGPU), `renderD128` may not be the
  iGPU — set `VAAPI_DEVICE` to `renderD129` or similar.
- `privileged: true` ensures reliable hardware metric readout. A finer-grained
  device/cap mapping is prepared as an alternative.
- Optional CPU/RAM limits are commented in `docker-compose.yml` (`deploy.resources.limits`).
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

Compose uses host vars `MEDIA_PATH` / `DATA_PATH`; inside the app the equivalents
are `MEDIA_DIR` / `DATA_DIR`.

---

## Project structure

```
app.py                  FastAPI app, routes, file-browser API, WebSocket, REST API
core/
  config.py             Paths, VMAF parameters, env configuration
  app_settings.py       Persistent settings.json (default output, sub-libraries)
  hardware.py           CPU/RAM/GPU monitoring (Nvidia/Intel/AMD)
  capabilities.py       Functional encoder capability tests (mini encodes)
  ffmpeg_utils.py       ffprobe, encoder mapping, sampled integrity check, auto-crop
  encoder.py            FFmpeg command builder, filter chains, progress parser
  vmaf.py               VMAF pipeline (VMAF/PSNR/SSIM/percentiles, sessions)
  dolby_vision.py       Dolby Vision RPU preservation via dovi_tool (HEVC 8.1)
  chunked.py            Chunked adaptive encoding
  size_target.py        Size-target → ABR bitrate preview
  job_plan.py           Naming templates, dry-run, duplicate / requeue paths
  audio_opt.py          Audio-only remux/optimization
  remux.py              Lossless remux/edit: tracks, attachments, chapters, trim, concat/split
  editor.py             Timeline editor: remux/encode (CQ/CBR/ABR), audio/sub copy
  media_stream.py       Light modal player: fMP4 stream + VTT
  player_hls.py         Full Player: HLS sessions (audio remux, seek)
  queue_manager.py      Async queue, guardrail, post-processing, persistence
  supertool.py          Guided batch processing (target/representative VMAF)
  library.py            Library scan + savings estimate
  data_browser.py       Data/archive browser
  history.py            Job history (SQLite, settings_json)
  watcher.py            Watch-folder automation
  scheduler.py          Scheduling/time windows
  notify.py             Notifications (webhook/Discord/Telegram)
  apikeys.py            API key management (REST/integration)
  profiles.py           Settings profiles
  diagnostics.py        Self-test/system diagnostics
templates/index.html    Dashboard markup
static/css/styles.css   Theme system
static/js/app.js        Dashboard logic (WebSocket, charts, browser)
static/js/editor.js     Timeline video editor UI
static/js/player.js     Full Player UI (HLS)
static/js/i18n.js       DE / EN / ES / FR translations
Dockerfile              All-in-one image (CUDA 12.6 + FFmpeg n8.1 + dovi_tool + models)
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
| `WORK_DIR` | `$DATA_DIR/work` | Encode scratch directory |
| `PREVIEW_DIR` | `$DATA_DIR/previews` | VMAF preview / screenshot sessions |
| `UPLOAD_DIR` | `$DATA_DIR/uploads` | Uploaded external tracks |
| `VMAF_SESSIONS_DIR` | `$DATA_DIR/vmaf` | Retained VMAF artifacts |
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
| `LIBVA_DRIVER_NAME` | – | e.g. `iHD` for Intel iGPU (set in compose) |
| `NVENC_FULL_GPU` | `false` | Force full GPU pipeline (faster, riskier) |
| `CQ_SWEETSPOT` | – | CQ fine-tuning, e.g. `cpu:hevc=22,nvidia:av1=33` |
| `FFMPEG_BIN` / `FFPROBE_BIN` / `DOVI_TOOL_BIN` | – | Override tool paths |
| `ARR_PATH_MAP` | – | *arr path remap: `from:to,from2:to2` |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FFMPEG_CMD` | `true` | Log the full FFmpeg command line |
| `APP_PASSWORD` | – | Optional login protection (empty = open) |
| `NOTIFY_WEBHOOK_URL` | – | Generic webhook for notifications |
| `DISCORD_WEBHOOK_URL` | – | Discord webhook |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | – | Telegram notifications |

> Nvidia-specific variables (`NVIDIA_VISIBLE_DEVICES`, `NVIDIA_DRIVER_CAPABILITIES`,
> optionally `NVIDIA_DISABLE_REQUIRE`) are documented in `docker-compose.yml`.
