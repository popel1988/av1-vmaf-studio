# Changelog – AV1 / VMAF Compression Studio

All notable changes, new features, and milestones of this project in reverse chronological order, linked to the corresponding versions and commits on [GitHub (`popel1988/av1-vmaf-studio`)](https://github.com/popel1988/av1-vmaf-studio).

---

## [This Release] – 2026-11-20
- **Butteraugli per scene, still, and heatmap (`core/vmaf.py`, `app.py`, `static/js/app.js`, `static/js/i18n.js`, `static/css/styles.css`):** The quality pill now opens the same scene and still breakdown as SSIMULACRA2 (mean, worst still, 3-norm, and each sampled frame). The scene line lists those frame distances. New runs and metric retests store the still time and a Butteraugli error map beside that frame; the comparison gallery shows the map with its distance when a still is selected. Older sessions keep the numbers they already have and gain maps on the next retest.
- **VMAF retest in the queue (`core/vmaf.py`, `core/queue_manager.py`, `app.py`, `static/js/app.js`):** A running clip retest shows up in the Warteschlange and the live progress card, and Abbrechen stops it. The retest-info request is fetched once per session on the VMAF pages instead of on every queue update.
- **Butteraugli timing (`core/vmaf_calib.py`, `static/js/app.js`):** The Grundtest measures Butteraugli seconds per frame pair at 1080p and 4K. Duration estimates add that cost when the metric is enabled, and only the extra tool time when SSIMULACRA2 already extracts the same stills. Until the next Grundtest, the estimate uses a fallback pair cost.
- **CI fixes (`Dockerfile`, `tests/`):** The HDR-VMAF image smoke test now scores two 1080p frames with explicit BT.2020 PQ tags and the 1080p model, and prints the FFmpeg log if that pass fails. Windows tests no longer depend on a local `xpsnr` filter or an undefined `butteraugliPill` in the MS-SSIM frontend check.

---

## [v9.8.0](https://github.com/popel1988/av1-vmaf-studio/commit/12d6d98) – 2026-11-20
- **libva 2.24.1 build check (`Dockerfile`, `core/diagnostics.py`):** The image build checks `pkg-config` for VA-API ≥ 1.24 and requires the `vaMapBuffer2` symbol in `libva.so.2`. Diagnose reports the installed libva / VA-API version.
- **HDR-VMAF v1.1.8 on `latest` only (`Dockerfile`, `core/vmaf.py`, `core/config.py`, `core/provenance.py`, `core/diagnostics.py`, `app.py`, `templates/index.html`, `static/js/app.js`):** HDR10/HLG sources without tone-mapping are scored with Netflix `hdr_vmaf_v1.1.8` (`3d0h` at 1080p, `1d5h_2160` at 4K). FFmpeg `zscale` converts to BT.2020 PQ ICtCp `yuv420p16le` before `libvmaf`, and a working copy of the model drops `conversion_target` because the bundled libvmaf build does not apply it. The option defaults on in Encoding, VMAF-Tool, Super-Tool, and Encoder-Test. `latest-legacy` (VMAF v0.6.1 / FFmpeg n8.1) leaves the models unset, disables the checkbox, and keeps standard VMAF. A failed HDR pass falls back to the standard model. `libvmaf_cuda` is downgraded to CUDA decode plus CPU `libvmaf` for the 16-bit frames.
- **Incremental library update scan (`core/library.py`, `app.py`, `templates/index.html`, `static/js/app.js`):** Added an **Update-Scan** mode (`POST /api/library/scan` with `incremental: true`) alongside the full scan. Unchanged files matching cached size, `mtime`, and NFO sidecar `mtime` skip `ffprobe`, while new/modified files are probed, deleted files are removed, and progress displays exact delta counts (`added`, `updated`, `removed`, `reused`).
- **Encoder & driver version tracking (`core/ffmpeg_utils.py`, `core/history.py`, `core/vmaf.py`, `app.py`, `templates/index.html`, `static/js/app.js`):** Automatically records an anonymous `encoder_version` identifier (e.g. NVIDIA GPU model + driver version, SVT-AV1/x265/x264 library version, or VAAPI/iHD/Mesa driver) in `quality_samples`, `VmafResult`, and `export_quality_exchange()`. The **Statistik** quality ranking displays detected versions and adds a **Version trennen** toggle (`group_version=true`) to compare encoder/driver generations side by side.
- **Post-run VMAF metric re-testing (`core/vmaf.py`, `app.py`, `templates/index.html`, `static/js/app.js`):** Archived VMAF sessions that retained their encoded `test_*.mkv` clips (and either `source_s*.mkv` clips or the original source file) now expose a **Metriken nachtesten** panel (`/api/vmaf/session/{name}/retest-info`, `/api/vmaf/retest`) to measure missing metrics (`SSIMULACRA2`, `MS-SSIM`, `XPSNR`, `CAMBI`, `PSNR`, `SSIM`, or `VMAF`) without re-encoding, re-aggregating `analysis.json` and upserting the updated scores into `quality_samples`.
- **More reliable Diagnose calibration & explicit resolution labels (`core/vmaf_calib.py`, `static/js/app.js`):** Extended synthetic encoder benchmark clips (1080p: 8s HW / 6s CPU, 4K: 5s HW / 3s CPU) with steady-state frame timing to reduce process/GPU startup skew while keeping fast 3s/2s cuts for `libvmaf` overhead probes, and labeled the Diagnose throughput columns explicitly (`1080p Schnell`, `1080p Ausgew.`, `1080p Langs.`, `4K Ausgew.`).
- **Updated bundled Docker tool versions (`Dockerfile`):** Bumped `dovi_tool` to `2.3.4`, `jellyfin-ffmpeg` to `8.1.3-2`, `libva` to `2.24.1`, and `libjxl` (`ssimulacra2` + `butteraugli_main`) to `v0.13.0` (extracting `.tar.lz` via `lzip`).
- **dovi_tool 2.3.4 direct MKV fast-path (`core/dolby_vision.py`, `core/dv_lab.py`, `core/ffmpeg_utils.py`, `core/diagnostics.py`):** Enabled direct Matroska RPU extraction (`extract-rpu -i input.mkv`, with automatic `-t` track selection on multi-video-track MKVs) and direct single-track Profile 7 → 8.1 conversion (`-m 2 convert --discard -i input.mkv`) without writing huge intermediate `.hevc` elementary streams, falling back transparently to the existing `ffmpeg` pipeline on errors or older `dovi_tool` builds.
- **Butteraugli distance metric (`Dockerfile`, `core/config.py`, `core/vmaf.py`, `core/history.py`, `core/queue_manager.py`, `core/encoder_bench.py`, `core/diagnostics.py`, `app.py`, `templates/index.html`, `static/js/app.js`):** Bundled `butteraugli_main` from the existing `libjxl v0.13.0` archive and integrated Butteraugli distance (lower = better, `< 1.0` visually transparent, plus `3-norm` & worst-frame max) as an optional companion metric across VMAF analyses, post-run metric re-testing (sharing a single PNG frame extraction pass when run alongside SSIMULACRA2), `quality_samples` DB/ranking, CSV export, and Diagnose tool checks.

---

## [v9.7.0](https://github.com/popel1988/av1-vmaf-studio/commit/8068e68) – 2026-11-19
- **Adaptive performance DB & full-encode ETA (`core/history.py`, `core/vmaf_calib.py`, `core/vmaf.py`, `core/queue_manager.py`, `core/encoder_bench.py`):** Completed encodes (`status='fertig'`), VMAF test clips (recorded directly on session save across VMAF-Tool, Target-VMAF, Super-Tool, and Encoder-Test), and encoder benchmarks are automatically stored in a persistent `perf_samples` SQLite table (with automatic backfill from existing job history and archived VMAF sessions) and blended with the calibration baseline via `effective_profile()`. This continuously refines runtime projections for both VMAF analyses and full-length encodes, adding a live encode duration estimate before queueing (`#opt-encode-eta`, `/api/encode/estimate`), a calibration-backed fallback ETA for queued jobs, and automatic UI refresh when a job finishes.
- **Encoder & codec quality ranking DB (`core/history.py`, `app.py`, `templates/index.html`, `static/js/app.js`):** Added a persistent `quality_samples` SQLite table (with automatic backfill from completed encodes and archived VMAF sessions) plus `GET /api/stats/quality-ranking`. Under **Statistik**, a compact **Encoder- & Codec-Rangliste** ranks encoders/presets by combined quality, compression, and detail metrics (`VMAF`, `1%-Low`, `MS-SSIM`, `SSIMULACRA2`, `CAMBI`, median bitrate, savings, speed). Filters cover resolution, HDR/SDR, min-VMAF, speed grouping, scene complexity (`peak`/`high` vs typical vs quiet from the bitrate profile), source density (bits per pixel), and anime vs live action. Head-to-head comparisons stay on the same clip and the same scene class.
- **Anonymous quality exchange ids (`core/history.py`):** Film titles are removed from `quality_samples` and `perf_samples` run ids and sample keys. A random, stable alias stays mappable to the local title and path in `source_aliases` on the same device (`source_alias.key` never leaves with an export). `export_quality_exchange()` returns only the measurement rows. Preview folders keep their readable session name for local browsing.
- **Data archive preview height (`static/css/styles.css`, `static/js/app.js`):** Opening a film clip or screenshot under **Daten und Archive** scales the picture to the viewport (`max-height: calc(100vh - 190px)`) instead of a 420px scrolling box, so the whole frame stays visible.

---

## [v9.6.2](https://github.com/popel1988/av1-vmaf-studio/commit/eaf27f9) – 2026-11-18
- **Compact Diagnose calibration summary (`static/js/app.js`, `static/css/styles.css`):** Replaced the tall calibration cards under **Diagnose** with a single-row metric strip (`libvmaf`, `S2`, and learned DB sample counts) plus a collapsible `<details>` encoder throughput table (`Fast` / `Balanced` / `Slow` / `4K` in fps and learned sample count) and companion-metric overhead line.

---

## [v9.6.1](https://github.com/popel1988/av1-vmaf-studio/commit/ccb4989) – 2026-11-18
- **Selectable VMAF companion metrics:** Optional quality tests (`PSNR`, `SSIM`, `MS-SSIM`, `XPSNR`, `CAMBI`) can now be toggled individually via compact checkboxes alongside `SSIMULACRA2` in **Encoding (Target-VMAF)**, **VMAF-Tool**, **Super-Tool**, **Diagnose Encoder-Test**, and saved presets. Disabled metrics are excluded from the `libvmaf` feature string (and skip the separate `XPSNR` pass) to reduce analysis runtime, while internal guardrails (`_vmaf_compare`) run pure VMAF without unnecessary secondary metrics.
- **Granular queue step sub-timings:** VMAF test-tier steps (`analysis.candidate`) now track and display separate durations for clip encoding (`Encode`), `libvmaf` scoring (`VMAF` + active companion tests), and `SSIMULACRA2` (`S2`) both live (highlighting the currently running sub-phase) and on completed steps.
- **System calibration & pre-run VMAF duration estimate (`core/vmaf_calib.py`):** Added a local **Grundtest (VMAF & runtime calibration)** under **Diagnose** that profiles available encoders (1080p & 4K across speed presets), `libvmaf` throughput (CPU/CUDA & subsampling), per-metric overhead (`PSNR`, `SSIM`, `MS-SSIM`, `XPSNR`, `CAMBI`), `SSIMULACRA2` per-pair cost, and HDR tonemap reference prep. Once calibrated, **VMAF-Tool**, **Target-VMAF**, **Super-Tool**, and **Encoder-Test** show an immediate runtime projection (`Encode` / `VMAF` / `S2`) while configuring options. The Grundtest reads the encoder capability cache as a boolean per encoder.
- **Folder-first media recognition (`core/works.py`, `core/library.py`):** Dedicated movie folders (`Title (Year)/movie`, or noisy scene filenames inside clean work folders) and series/season directory hierarchies (`Show (Year)/Season`) now take precedence over cryptic filenames while ignoring generic bucket folders (`Filme`, `Serien`, `2160p`, `UHD`) and preserving strict NFO/ID separation.
- **MS-SSIM metric and toggleable curve:** `libvmaf` passes now enable `float_ms_ssim` alongside PSNR, SSIM, CAMBI and XPSNR, with automatic fallback for builds without `float_ms_ssim`. Pooled, per-scene, and per-frame MS-SSIM values are stored in session archives, shown in result details, Nerd statistics and CSV exports, and can be switched on as an optional right-axis curve (`0–1`) in the step/RD/multi-codec charts, scene course chart, and Nerd per-frame chart without affecting VMAF recommendations or older archives.
- **Library — Files and Media:** Files is again the default, flat encoding table. Media browses local titles, artwork, series and seasons separately. Only matching file-specific provider IDs with compatible metadata, or an explicit version choice, combine files. Filename suggestions remain individual entries. Existing collections are preserved; selecting media for a collection is independent of selecting files for the queue. Optional Wikidata lookups send only the title entered for that search and require confirmation before changing a local display title.

---

## [v9.5.0](https://github.com/popel1988/av1-vmaf-studio/commit/8253e8a) – 2026-11-15
- **Library works:** The library recognizes a file as a film, documentary, episode, or dated broadcast from its name, folder, and NFO, and groups the versions of that work. Custom collections (a series, a film, a broadcast, or a plain collection) can be created and files assigned by hand, for example several recordings of the same occasion.
- **HDR / DV Lab metadata tools:** Dolby Vision RPU can be approximated into an HDR10+ layer on the base track while the RPU stays (on a second track the enhancement-layer pixels stay too; Profile 5 is rejected because it has no HDR10 base). Profile 7 can be written as a single Profile 8.1 TV stream, with an explicit warning that the enhancement-layer pixels are dropped. Two RPUs (vault entries or files) can be compared on frame count, scene cuts, L1 brightness, L5 crop and the intro offset. The layer inspector plots an HDR10+ curve on the same zoom window as the bitrate graph, from the file’s JSON or from the RPU, and sets the scene peaks against static MaxCLL and MaxFALL.

---

## [v9.4.4](https://github.com/popel1988/av1-vmaf-studio/commit/25a87ba) – 2026-11-15
- **Dual-track Dolby Vision Profile 7:** A file whose base track is only HDR10 and whose second HEVC track carries the Enhancement Layer plus RPU (`dvhe`, Profile 7) is now recognized as Dolby Vision in the library and in the HDR / DV Lab. The measured RPU on that track sets `Dolby Vision` and `RPU` instead of leaving both as No next to the EL/RPU sizes. MediaInfo’s HDR10+ note on the base layer is shown as coexistence when ffprobe does not list that SEI. RPU extraction (`dovi_tool`) reads that enhancement track instead of the HDR10 base, which reported `No RPU was found in input file`.

---

## [v9.4.2](https://github.com/popel1988/av1-vmaf-studio/commit/ac63a65) – 2026-11-15
- **Bitrate-Graph Window (`HDR / DV Labor`):** The layer-inspector zoom uses the same dual-thumb range as the VMAF nerd view: one track, highlighted span, `full` when the full film is visible, and a reset button.
- **Bitrate-Scan Progress:** Step 2 shows a live progress bar (played time and percent) while the full-movie scan runs.

---

## [v9.4.1](https://github.com/popel1988/av1-vmaf-studio/commit/f972ea9) – 2026-11-15
- **Studio-Wide Integration & Interactive File Picker:** Replaced flat dropdowns in the HDR / DV Lab with full folder/file picker dialogs (`openFilePickerModal` / `openFolderPickerModal`) and added 1-click `✨` / `→ DV-Labor` transfer buttons across the Media Library, Super-Tool, Global Search, and the active-source context bar. The RPU vault batch extract uses the same multi-file and folder browser modals.
- **Data & Archives Overhaul (`core/data_browser.py`, `core/queue_manager.py`, `core/vmaf.py`):**
  - **Chronological Movie + Timestamp Naming (`<Title>_<YYYY-MM-DD_HH-MM>_<id6>`):** Archive folders (`VMAF-Sessions`, `Screenshots`, kept DV work dirs, and `RPU-Tresor` files) now include both the movie title and ISO date/time stamp so multiple runs of the same movie group together while remaining clearly distinguishable over time.
  - **Dedicated `RPU-Archiv` Zone & Multi-Sort Browser:** Added `rpu_vault` (`RPU-Archiv (Dolby Vision)`) to **Data & Archives** with live RPU metadata preview, direct `.rpu.bin` download, 1-click handoff to the DV Lab, automatic `index.json` cleanup when deleting RPU files, per-row timestamps, and a sort switcher (`Name`, `Newest first`, `Oldest first`, `Size`).

---

## [v9.4.0](https://github.com/popel1988/av1-vmaf-studio/commit/9ea62e9) – 2026-11-15
- **HDR & Dolby Vision Lab (`core/dv_lab.py`, `core/rpu_vault.py`, `static/js/dv_lab.js`):** Added a dedicated **HDR / DV Labor** workspace built 100% natively on open-source tools (`dovi_tool`, `hdr10plus_tool`, `ffmpeg`/`ffprobe`, `mkvmerge`, `mediainfo`):
  - **Layer Inspector & 0-Disk-Space Bitrate Scanner:** Instant inspection of Profile, Level, `CM v2.9` / `CM v4.0`, Base Layer type, **MEL vs. FEL** Enhancement Layer detection, RPU Mastering Display luminance, and RPU `MaxCLL`/`MaxFALL`, plus an in-memory HEVC Annex-B NALU stream parser (`BL`, `EL` `UNSPEC63`/Layer 1, `RPU` `UNSPEC62`) that calculates exact sizes, average/peak bitrates, EL share `%`, and a zoomable multi-layer bitrate graph **without temporary video demuxing on disk**, with integrated MediaInfo + DV report export in 5 formats (`TXT`, `Markdown`, `JSON`, `CSV`, `HTML`) and PNG graph export.
  - **Hybrid Mux & Cross-Release RPU Transfer:** Lossless Dolby Vision transfer across releases with automatic intro/cut frame-offset correlation (`scene_refresh_flag` vs. target scene cuts), automatic **L5 Active-Area** crop adjustment (also applied during cropped HEVC DV encodes), and optional work-folder retention.
  - **Lossless HDR10+ → Dolby Vision 8.1 Dual-HDR Converter:** Generates genuine Dolby Vision Profile 8.1 RPUs from `HDR10+` metadata (`hdr10plus_tool` + `dovi_tool generate`) to create Dual-HDR files (`HDR10 + HDR10+ + Dolby Vision`) both standalone in the Lab and as a direct checkbox during HEVC encoding.
  - **RPU Vault (`RPU-Tresor`):** Batch RPU extraction from multiple movies (DV or HDR10+) into `/data/rpu_vault` with a searchable metadata table, customizable column picker, `.rpu.bin` download, and 1-click injection into target releases.
- **Editor HDR10 Metadata Fix & Full Tonemapping Engine/Curve Suite (`core/editor.py`, `core/encoder.py`):** Fixed `keep_hdr` handling in `build_editor_encode_cmd` so HDR10 color tags (`smpte2084` / `bt2020nc`), `setparams` filter metadata, `-svtav1-params tune=0`, MP4 `-tag:v hvc1`, and static `master-display` + `max-cll` metadata are preserved end-to-end; added full Tonemap Engine (`CPU`, `libplacebo`, `CUDA`, `OpenCL`, `VAAPI`, `tonemapx` via Jellyfin-FFmpeg) and curve (`Hable`, `BT.2390`, `Reinhard`, `Mobius`, `Gamma`, `Clip`, `Linear`) + desaturation/peak controls to the Editor.
- **Smart Cut 2.0 (`build_editor_smart_plan`):** Added three-phase frame-accurate smart cutting (head GOP re-encode → lossless stream-copy body → tail GOP re-encode when cuts end mid-GOP), full AV1 10-bit HDR color/tuning matching to eliminate edge color flicker, and optional GPU hardware encoding (`NVENC`/`QSV`/`AMF`/`VAAPI`) for smart-cut edges.
- **Interactive Auto-Cut Preview & Speech Padding (`/api/editor/detect`):** Added configurable thresholds (`dB` / black / scene sensitivity), minimum duration, and speech safety padding (`pad_sec`, e.g. `0.15 s` buffer so word starts/ends are never clipped), with live visual markers on the source ruler and per-hit checkboxes + jump-to-timestamp preview before applying cuts.
- **Live Program-Monitor FX Preview, Frame Fine-Trimming & Quick WebP/Loop Export:** Real-time client-side preview in the Program Monitor for look presets, rotation/flip, crop, fade-in/out darkness, live text overlays, and dB audio gain (WebAudio `GainNode`), plus `±1F` In/Out frame nudging, `⇹ ±1F` rolling cut trim, and 1-click animated WebP / silent Loop-MP4 export (`/api/editor/quick-loop`).

---

## [v9.3.4](https://github.com/popel1988/av1-vmaf-studio/commit/97628de) – 2026-11-13
- **Frame-Indexed Ring Buffer against Live Motion Ghosting:** Prevented browser compositor occlusion throttling (`opacity > 0` behind opaque diff canvas) and introduced a per-side `requestVideoFrameCallback` ring buffer that strictly pairs matching film frame numbers (`#N` of A with `#N` of B/C) during live playback, plus explicit `Frame-Lock #N` / `Standbild #N` HUD indicators.
- **3-Way Comparison (`A / B / C`) & Direct Delta-Error View (`Δ(C − B)` vs. Original `A`):** Added an optional third video slot `C` in both VMAF Nerd (pick 2 or 3 clips) and standalone A/B Compare with instant pair switching (`1: A↔B`, `2: A↔C`, `3: B↔C`), a diverging delta-error heatmap (`4: Δ Mehr-Fehler C vs. B zu A` highlighting where encode `C` loses more detail than `B` in yellow/red), 3-way pixel hover inspection, and multi-target `⚡ Auto-Sync` (`B` & `C` against `A`).

## [v9.3.3](https://github.com/popel1988/av1-vmaf-studio/commit/35ffbdb) – 2026-11-13
- **Bidirectional Auto-Sync at Clip Boundaries (`⚡ Auto-Sync`):** Fixed an edge case when triggering Auto-Sync at the very start/end of a clip (`0:00`) where negative frame offsets could not be evaluated; the probe window now shifts inward automatically so both `−12` and `+12` frames are always tested, advancing to the first shared frame if one clip leads the other.
- **VSync-Gated Heatmap/Diff, Scale Legend, Pixel Inspector & `⅛×` Speed:** Eliminated false-positive motion-edge diffs caused by 1-VSync decoder presentation skew during live playback (`requestVideoFrameCallback` phase gate + exact frame snap on pause), added `⅛×` playback speed, a dynamic gain-aware color legend, and an interactive hover pixel inspector (`RGB`, `ΔRGB`, `RMS Δ`, `ΔLuma`).

## [v9.3.2](https://github.com/popel1988/av1-vmaf-studio/commit/b523fe4) – 2026-11-13
- **A/B Player Frame-Stepping & Pause/Resume Fix:** Resolved a seek/readyState deadlock where single-frame stepping (`±1` frame) got stuck on quantized container timestamps and prevented playback from resuming after pausing.
- **Range-Seekable VMAF Clips & Accurate A/B HLS Seeking:** Served VMAF test and source clips as `+faststart` MP4s with HTTP range support, disabled fast-seek overshoot (`+0.5s`), and shortened HLS GOPs in A/B mode for responsive frame stepping.
- **1-Click Pixel Auto-Sync (`⚡ Auto-Sync`):** Added luminance cross-correlation frame matching (`±12` frames, crop- and tonemap-aware) to automatically align encodes 100% with the original source.

## [v9.3.1](https://github.com/popel1988/av1-vmaf-studio/commit/34e9368) – 2026-11-13
- **Windows CI & Custom Folder Path Fix (`core/desktop_folders.py`):** Fixed custom media folder normalization and test path resolution on Windows builds.

## [v9.3.0](https://github.com/popel1988/av1-vmaf-studio/commit/0340600) – 2026-11-13

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
