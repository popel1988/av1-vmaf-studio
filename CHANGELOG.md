# Changelog – AV1 / VMAF Compression Studio

All notable changes, new features, and milestones of this project in reverse chronological order, linked to the corresponding versions and commits on [GitHub (`popel1988/av1-vmaf-studio`)](https://github.com/popel1988/av1-vmaf-studio).

---

## [This Release] – 2026-11-13

### Video, VMAF & Quality Analysis
- **GPU-accelerated `libvmaf` (`vmaf_cuda`) & Subsampling:** Automatic detection and use of `vmaf_cuda` on NVIDIA systems (aware of `latest` vs. `legacy` FFmpeg builds and driver requirements), plus optional frame subsampling (`n_subsample`) for significantly faster VMAF runs.
- **Stricter Dolby Vision Detection (`core/dolby_vision.py`):** Distinguishes genuine RPU/DOVI metadata from false positives on standard Full HD / SDR files, preventing unnecessary DV handling in both the encoder and player paths.
- **2-Pass Target Size Mode & Auto-Crop Integration:** Two-pass bitrate encoding for hitting exact file size targets, alongside automatic black-bar crop detection directly in the main workflow.
- **Scene-Aware Smart Chunking (`core/chunked.py`):** Scene-cut detection for seamless parallel chunked encoding without visible GOP boundary artifacts.

### Player & A/B Comparison
- **Native HDR / HEVC Capability Check in Auto Mode:** Probes actual browser codec and HDR capabilities on load, preserves native source resolution (e.g., 2160p) in Automatic mode whenever supported, and only falls back to tonemapping when HDR cannot be played natively (including an explicit indicator when Dolby Vision falls back to HDR10).
- **Frame-by-Frame Stepping & Independent A/B Frame Offset:** Single-frame stepping in both the standalone player and A/B comparison view, plus per-side frame offset controls (A or B) to align slightly out-of-sync sources and encodes.

### Audio & Subtitles
- **True 2-Pass EBU R128 Normalization & Dialogue Boost (`core/audio_opt.py`):** Measured two-pass `loudnorm` loudness normalization instead of single-pass estimation, plus optional center/dialogue boost for multichannel downmixes.
- **Local PGS / VobSub to SRT OCR (`core/subtitle_ocr.py`):** Optional local optical character recognition to convert image-based Blu-ray/DVD subtitles into text-based `SRT` tracks via Tesseract.

### Workflow & Queue
- **Repositioned Verification Report:** Moved the output verification report to the bottom of the queue item details view for a cleaner reading flow.
- **Watchfolder Stability Check, Dry-Run & CSV Export:** File write-stability verification before auto-enqueueing watched folders, rule dry-run simulation mode, and CSV export for encode history.
- **Process Priority & Per-Job Suspend/Resume:** Ability to throttle active FFmpeg worker priority (Idle / Below Normal) and pause/resume individual running processes.

### Windows Desktop App & Hardware
- **Zero-Subprocess GPU Telemetry (`core/win_gpu.py`, `core/hardware.py`):** Direct in-process bindings to `nvml.dll` (including dedicated NVENC/NVDEC utilization and multi-NVENC engine counts for RTX 40/50 series) as well as `dxgi.dll` + `pdh.dll` for AMD Radeon and Intel Arc/UHD GPUs on Windows—eliminating periodic `nvidia-smi.exe` spawns and console window flashing.
- **Full Windows AMF & QSV Support:** Native `-quality` speed presets, CQP rate control, `d3d11va`/`qsv` hardware acceleration, and self-test diagnostics for AMD AMF and Intel QSV on Windows.
- **Taskbar Progress, Sleep Prevention & Post-Queue Power Actions (`core/desktop_win32.py`, `core/desktop_host.py`):** Native Windows taskbar progress indicator (`ITaskbarList3`: green/yellow/red), automatic sleep/standby lock (`SetThreadExecutionState`) during active encodes, post-queue power actions (*Sleep*, *Hibernate*, *Shut down* with a 30s cancellable countdown), tray toast notifications, and custom media folder/drive management.
- **Packaging & Installer (`installer.iss`, `build_exe.ps1`):** Automatic Mark-of-the-Web (`:Zone.Identifier`) unblocking to prevent `.NET` load errors (`0x80131515`), plus an Inno Setup 6 script to build a clean Windows installer (`AV1-VMAF-Studio-Setup.exe`).

---

## [v9.1.0 – v9.1.1] – 2026-11-09 to 2026-11-12
- **[v9.1.1](https://github.com/popel1988/av1-vmaf-studio/commit/5cacac3) (2026-11-12):** Fixed automatic resolution and playback path selection in the player; added persistent custom media folder selection (`core/desktop_folders.py`) and suppressed flashing console windows (`CREATE_NO_WINDOW` in `core/desktop_processes.py`) in the Windows app.
- **[v9.1.0](https://github.com/popel1988/av1-vmaf-studio/commit/944fe90) (2026-11-09):** Major Windows Desktop Host & Workspace UI overhaul:
  - Native WebView2 host bridge (`core/desktop_host.py`), single-instance IPC (`core/desktop_ipc.py`), zero-upload path grant security model (`core/desktop_paths.py`), and optional per-user *"Open with"* registration (`core/desktop_associations.py`).
  - Export preflight checks (`core/export_preflight.py`) and quick sample test exports prior to full encodes.
  - Streamlined Workspace UI (`static/js/workspace-ui.js`, `static/css/workspace.css`) with collapsible sidebar and bundled offline frontend assets (`Chart.js`, `hls.js`).

## [v9.0.0 – v9.0.4] – 2026-11-08
- **[v9.0.0](https://github.com/popel1988/av1-vmaf-studio/commit/efe2548) to [v9.0.4](https://github.com/popel1988/av1-vmaf-studio/commit/9a5942b) (2026-11-08):** Introduced the standalone **Windows Desktop Application** (`desktop_main.py`, `build_exe.ps1`), including GitHub Actions build workflows, PyInstaller packaging, local media tool bundling (`ffmpeg`, `mkvmerge`, `dovi_tool`, `hdr10plus_tool`), and initial Windows path and WebSocket/HTTP live-polling fallback fixes.

---

## [v8.2.0 – v8.3.0] – 2026-11-08
- **[v8.3.0](https://github.com/popel1988/av1-vmaf-studio/commit/8e3bede) (2026-11-08):** Expanded video editor filters: logo/watermark removal (`delogo`), text overlays (`drawtext`), color/look presets, and frame snapshot export (`/api/editor/snapshot`).
- **[v8.2.0](https://github.com/popel1988/av1-vmaf-studio/commit/a9a172a) (2026-11-08):** Enhanced timeline editor (`core/editor.py`, `static/js/editor.js`): automatic scene/cut detection, editor auto-crop detection, crossfade transition previews, overlays, and keyframe-aware cutting.

## [v8.1.0 – v8.1.11] – 2026-11-04 to 2026-11-07
- **[v8.1.11](https://github.com/popel1988/av1-vmaf-studio/commit/959f187) (2026-11-07):** Queue manager reliability and state handling improvements.
- **[v8.1.4](https://github.com/popel1988/av1-vmaf-studio/commit/7c9bd24) – [v8.1.10](https://github.com/popel1988/av1-vmaf-studio/commit/da64611) (2026-11-04 to 2026-11-06):** Retained source test clips for VMAF sessions and delivered extensive synchronization and seek fixes for the A/B comparison player.
- **[v8.1.0](https://github.com/popel1988/av1-vmaf-studio/commit/eed241b) – [v8.1.3](https://github.com/popel1988/av1-vmaf-studio/commit/bdd932c) (2026-11-04):** Added **SMB / CIFS network share mounting** (`core/shares.py`) directly from the web UI without restarting the container.

## [v8.0.0 – v8.0.1] – 2026-11-03
- **[v8.0.0](https://github.com/popel1988/av1-vmaf-studio/commit/7087935) & [v8.0.1](https://github.com/popel1988/av1-vmaf-studio/commit/1140117) (2026-11-03):** Major verification, observability, and storage update:
  - Automatic post-encode **Output Verification Report** (`core/output_report.py`) validating container integrity, streams, HDR/DV metadata, chapters, and duration.
  - **Command Provenance & Runtime Inspector** (`core/provenance.py`, `core/runtime.py`) for auditing exact FFmpeg commands and active background subprocesses.
  - **Storage & Retention Policy Engine** (`core/storage_policy.py`) to preview and clean up VMAF sessions, previews, and temporary work files.
  - **Studio Insights** (`static/js/insights.js`), targeted extra-scene VMAF re-probing (`/api/vmaf/extra-scene`), and a comprehensive automated test suite (`tests/`).

---

## [v7.6.0 – v7.10.2] – 2026-10-04 to 2026-11-03
- **[v7.10.0](https://github.com/popel1988/av1-vmaf-studio/commit/dd23e68) – [v7.10.2](https://github.com/popel1988/av1-vmaf-studio/commit/b188966) (2026-11-02 to 2026-11-03):** Overhauled the A/B Nerdplayer with tighter playback synchronization and low-VMAF scene context inspection.
- **[v7.9.1](https://github.com/popel1988/av1-vmaf-studio/commit/a945f93) – [v7.9.5](https://github.com/popel1988/av1-vmaf-studio/commit/2823e7d) (2026-10-31 to 2026-11-01):** Integrated **SSIMULACRA2** for perceptual still-frame quality comparison, plus zoom and UI refinements.
- **[v7.9.0](https://github.com/popel1988/av1-vmaf-studio/commit/28d4010) (2026-10-31):** Integrated `jellyfin-ffmpeg` with improved hardware driver and filter pipelines.
- **[v7.7.0](https://github.com/popel1988/av1-vmaf-studio/commit/501e6b0) – [v7.8.0](https://github.com/popel1988/av1-vmaf-studio/commit/e606809) (2026-10-23 to 2026-10-31):** Expanded **Encoder Speed & Quality Benchmark** (with automatic preset recommendations), configurable HDR tonemapping (`libplacebo` / OpenCL / VAAPI / CPU; Hable, Mobius, BT.2390), debanding filters, and SVT-AV1 look tuning.
- **[v7.6.0](https://github.com/popel1988/av1-vmaf-studio/commit/de728df) – [v7.6.7](https://github.com/popel1988/av1-vmaf-studio/commit/f585a36) (2026-10-04 to 2026-10-06):** Added live **Nerdstats** overlays (codec, bitrate, frame, and metric telemetry) along with multi-run logging and visual fixes.

## [v7.0.0 – v7.5.4] – 2026-09-30 to 2026-10-04 *(GitHub tags available)*
- **[v7.5.0](https://github.com/popel1988/av1-vmaf-studio/commit/49be51f) – [v7.5.4](https://github.com/popel1988/av1-vmaf-studio/commit/2b0a15f) (2026-10-03 to 2026-10-04):** Visual polish and stability fixes.
- **[v7.4.5](https://github.com/popel1988/av1-vmaf-studio/releases/tag/v7.4.5) (2026-10-02):** Interactive side-by-side / split **A/B video & frame comparison** directly from VMAF test runs and completed jobs.
- **[v7.4.0](https://github.com/popel1988/av1-vmaf-studio/releases/tag/v7.4.0) – [v7.4.4](https://github.com/popel1988/av1-vmaf-studio/releases/tag/v7.4.4) (2026-10-02):** Zoomable VMAF timeline charts, extended percentile metrics, and deeper VMAF result views.
- **[v7.3.0](https://github.com/popel1988/av1-vmaf-studio/releases/tag/v7.3.0) (2026-10-01):** Extended VMAF scene sampling and benchmark capabilities.
- **[v7.2.0](https://github.com/popel1988/av1-vmaf-studio/releases/tag/v7.2.0) – [v7.2.2](https://github.com/popel1988/av1-vmaf-studio/releases/tag/v7.2.2) (2026-10-01):** Added **XPSNR** quality metric support, **DVD / `VIDEO_TS` folder import** (`core/dvd.py`), configurable **B-frames** (`v7.2.1`), and an built-in FAQ section (`v7.2.2`).
- **[v7.1.0](https://github.com/popel1988/av1-vmaf-studio/releases/tag/v7.1.0) – [v7.1.1](https://github.com/popel1988/av1-vmaf-studio/releases/tag/v7.1.1) (2026-09-30):** Direct reading of unencrypted **Blu-ray folder structures (`BDMV`)** with playlist and chapter selection (`core/bluray.py`).
- **[v7.0.0](https://github.com/popel1988/av1-vmaf-studio/releases/tag/v7.0.0) (2026-09-30):** Major FFmpeg upgrade introducing dual container channels (`latest` for modern GPUs/drivers + `legacy` image for older NVIDIA driver branches).

---

## [v6.0 – v6.7.5] – 2026-07-23 to 2026-09-28
- **[v6.7.0](https://github.com/popel1988/av1-vmaf-studio/commit/30602eb) – [v6.7.5](https://github.com/popel1988/av1-vmaf-studio/commit/65aaa0b) (2026-09-27 to 2026-09-28):** **Bitrate distribution profiling** (`core/bitrate_profile.py`), advanced Nerd analysis graphs, and CSV export refinements.
- **[v6.6.0](https://github.com/popel1988/av1-vmaf-studio/commit/8a88e0f) – [v6.6.5](https://github.com/popel1988/av1-vmaf-studio/commit/80138e5) (2026-09-26):** **HDR10+ dynamic metadata preservation** via `hdr10plus_tool` (`core/hdr10plus.py`), **Jellyfin / Sonarr / Radarr** integration (`core/media_servers.py`) for automatic library refreshes, and improved VMAF 1%-low calculations.
- **[v6.4](https://github.com/popel1988/av1-vmaf-studio/commit/7402981) – [v6.5.0](https://github.com/popel1988/av1-vmaf-studio/commit/0b21f08) (2026-08-28 to 2026-09-26):** Native **encoder speed presets** per encoder family (SVT-AV1 `0–13`, NVENC `p1–p7`, x264/x265, QSV, VP9), dedicated **Encoder Benchmark module** (`core/encoder_bench.py`), and major media library scanner upgrades (`v6.4.4`–`v6.4.9`).
- **[v6.0](https://github.com/popel1988/av1-vmaf-studio/commit/71ec3dd) – [v6.3.1](https://github.com/popel1988/av1-vmaf-studio/commit/fbc8faa) (2026-07-23 to 2026-08-28):** Introduced the built-in **HLS Web Player** (`core/player_hls.py`, `static/js/player.js`) with audio/subtitle track switching and live transcoding, alongside the **Visual Video Editor** (`core/editor.py`, `static/js/editor.js`) for trimming, cutting, and concatenating clips.

---

## [v5.0 – v5.8.3] – 2026-07-18 to 2026-07-22
- **[v5.7](https://github.com/popel1988/av1-vmaf-studio/commit/f5edd80) – [v5.8.3](https://github.com/popel1988/av1-vmaf-studio/commit/c05736d) (2026-07-20 to 2026-07-22):** **Target file size calculator** (`core/size_target.py`), direct HTTP media streaming (`core/media_stream.py`) for previews, and smart job re-queueing with output conflict resolution.
- **[v5.5](https://github.com/popel1988/av1-vmaf-studio/commit/e0687fb) – [v5.6.1](https://github.com/popel1988/av1-vmaf-studio/commit/ca723fd) (2026-07-20):** Unified **single-mount media tree** (`/media` + multi-root `MEDIA_DIRS`), persistent application settings (`core/app_settings.py`), and pre-execution job planning (`core/job_plan.py`).
- **[v5.2](https://github.com/popel1988/av1-vmaf-studio/commit/27609e0) – [v5.4.3](https://github.com/popel1988/av1-vmaf-studio/commit/416a639) (2026-07-19 to 2026-07-20):** Added **French and Spanish** UI translations (alongside German and English) and expanded Supertool options.
- **[v5.0](https://github.com/popel1988/av1-vmaf-studio/commit/2312090) – [v5.1.1](https://github.com/popel1988/av1-vmaf-studio/commit/b56e4ec) (2026-07-18 to 2026-07-19):** Full **MKV / Container Remux Studio** (`core/remux.py`): reorder/strip tracks, edit language and default flags, extract streams, losslessly concatenate files (`concat`), and split by chapters or timestamps (`split`).

---

## [v4.0 – v4.6.1] – 2026-07-17 to 2026-07-18
- **[v4.2](https://github.com/popel1988/av1-vmaf-studio/commit/67c8027) – [v4.6.1](https://github.com/popel1988/av1-vmaf-studio/commit/4c8e609) (2026-07-17 to 2026-07-18):** Automated runtime encoder/decoder capability probing (`core/capabilities.py`) and Dolby Vision safeguard logic (preventing invalid AV1 + DV RPU combinations in favor of HEVC DV preservation or HDR10 fallback in `v4.5`).
- **[v4.1](https://github.com/popel1988/av1-vmaf-studio/commit/6beb4fd) (2026-07-17):** Initial browser preview support, `ffprobe` stream inspector, and automatic MKV track statistics tags via `mkvpropedit`.
- **[v4.0](https://github.com/popel1988/av1-vmaf-studio/commit/3d190e8) (2026-07-17):** Major feature release:
  - **Internationalization (`static/js/i18n.js`)** for German and English.
  - **Audio Optimizer (`core/audio_opt.py`)**, parallel **Chunked Encoding (`core/chunked.py`)**, time-window **Queue Scheduler (`core/scheduler.py`)**, **System Self-Diagnostics (`core/diagnostics.py`)**, and **API Key authentication (`core/apikeys.py`)**.

---

## [v3.0 – v3.2] – 2026-07-16 to 2026-07-17
- **[v3.1](https://github.com/popel1988/av1-vmaf-studio/commit/fc10f5b) – [v3.2](https://github.com/popel1988/av1-vmaf-studio/commit/419ade3) (2026-07-16 to 2026-07-17):** **Dolby Vision support** (`core/dolby_vision.py`) using `dovi_tool` for RPU extraction, Profile 7 → 8.1 conversion, and reinjection on HEVC encodes.
- **[v3.0](https://github.com/popel1988/av1-vmaf-studio/commit/cd1aef2) (2026-07-16):** Introduced the **Supertool** (`core/supertool.py`) for combined batch analysis, remuxing, and automated encoding workflows.

---

## [v2.0 – v2.6.2] – 2026-07-02 to 2026-07-03
- **[v2.5](https://github.com/popel1988/av1-vmaf-studio/commit/916228c) – [v2.6.2](https://github.com/popel1988/av1-vmaf-studio/commit/ffd43c4) (2026-07-03):** Upgraded Docker base image to **Ubuntu 24.04** and expanded multi-vendor GPU acceleration (NVIDIA NVENC, Intel QSV/VAAPI, AMD VAAPI).
- **[v2.0](https://github.com/popel1988/av1-vmaf-studio/commit/9af2465) – [v2.4.2](https://github.com/popel1988/av1-vmaf-studio/commit/530b3dc) (2026-07-02 to 2026-07-03):** Evolved into a complete media management suite:
  - **Media Library Scanner (`core/library.py`)**, persistent SQLite **Encode History (`core/history.py`)** with space-saved analytics, reusable **Encoding Profiles (`core/profiles.py`)**, automated **Folder Watcher (`core/watcher.py`)**, and **Push Notifications (`core/notify.py`)** for Discord, Telegram, and generic webhooks.

---

## [v1.0 – v1.9.1] – 2026-06-29 to 2026-07-02
- **[v1.1](https://github.com/popel1988/av1-vmaf-studio/commit/5326013) – [v1.9.1](https://github.com/popel1988/av1-vmaf-studio/commit/125bd62) (2026-06-29 to 2026-07-02):** Added the data directory browser (`core/data_browser.py`), multi-track audio and subtitle selection, HDR10 static metadata passthrough, improved VMAF multi-clip sampling, and live WebSocket progress updates.
- **[v1.0 – Initial Release](https://github.com/popel1988/av1-vmaf-studio/commit/f7fc4aa) (2026-06-29):** Initial release of **AV1 / VMAF Compression Studio** featuring a FastAPI backend, responsive web UI, Docker setup, live hardware monitoring, job queue (`core/queue_manager.py`), AV1/HEVC/H.264 encoding (`core/encoder.py`), and automated VMAF-guided CRF/CQ quality search (`core/vmaf.py`).
