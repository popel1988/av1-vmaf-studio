# =============================================================================
#  AV1 / VMAF Compression Studio – All-in-One Image
#  Basis: CUDA-Runtime (liefert NVENC-Libs; nvidia-smi kommt zur Laufzeit vom
#  nvidia-container-toolkit). Zusätzlich Intel-QSV/VAAPI- und AMD-VAAPI-Stacks.
#  FFmpeg wird als moderner statischer GPL-Build (mit libvmaf, NVENC, VAAPI,
#  QSV/VPL) eingebunden – die Distro-Version ist zu alt für av1_nvenc/av1_qsv.
#
#  Basis Ubuntu 24.04 (noble) wegen neuem iHD-Treiber + oneVPL. WICHTIG: Der
#  BtbN-FFmpeg-Build verlangt das libva-Symbol `vaMapBuffer2` (erst ab libva
#  2.21 / VA-API 1.21). Da 24.04 nur libva 2.20 mitbringt, wird libva unten aus
#  dem Quelltext (2.24.1 / VA-API 1.24) gebaut – sonst crasht jeder Intel/AMD-HW-Encode.
#  Hinweis: CUDA-Images für ubuntu24.04 gibt es erst ab CUDA 12.6.
# =============================================================================
# Basis-Image überschreibbar – z. B. andere CUDA-Version, falls der
# Host-Treiber nicht zur Default-CUDA-Version passt:
#   docker build --build-arg CUDA_IMAGE=nvidia/cuda:12.6.3-runtime-ubuntu24.04 .
# WICHTIG: Bei einem Downgrade eine ubuntu24.04-Variante wählen (>= 12.6),
# sonst ist libva zu alt (siehe oben).
ARG CUDA_IMAGE=nvidia/cuda:12.6.3-runtime-ubuntu24.04
FROM ${CUDA_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video,graphics \
    PYTHONUNBUFFERED=1 \
    MEDIA_DIR=/media \
    VMAF_MODEL_DIR=/usr/local/share/model

# ----------------------------------------------------------------- System-Deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates wget xz-utils tar pciutils \
        python3 python3-pip python3-dev \
        # --- VAAPI / Intel QSV / AMD Userspace-Treiber ---
        # libva2 (wird unten durch Quellbau 2.24.1 ersetzt) + iHD-Treiber für
        # Intel, mesa-va für AMD. QSV läuft über oneVPL: libvpl2 (Dispatcher) +
        # libmfx-gen1.2 (Gen-Runtime, z. B. UHD 730/Gen12). libmfx1 als Fallback.
        libva2 libva-drm2 vainfo \
        intel-media-va-driver-non-free \
        mesa-va-drivers \
        ocl-icd-libopencl1 \
        intel-opencl-icd \
        mesa-opencl-icd \
        libmfx1 libmfx-gen1.2 libvpl2 \
        # --- GPU-Monitoring-Werkzeuge ---
        intel-gpu-tools radeontop \
        libdrm2 \
        # --- MKV- & Analyse-Werkzeuge (mkvtoolnix + mediainfo für DV-Labor) ---
        mkvtoolnix mediainfo \
        # --- SMB-Freigaben aus den Einstellungen (mount.cifs) ---
        cifs-utils \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------ Neueres libva bauen
# Der statische BtbN-FFmpeg-Build lädt libva dynamisch und ist gegen libva >=
# 2.21 gebaut (er verlangt das Symbol `vaMapBuffer2`, eingeführt in libva
# 2.21.0 / VA-API 1.21). KEINE gängige Distro liefert das aktuell: Ubuntu 24.04
# hat nur 2.20.0, Ubuntu 22.04 nur 2.12 → jeder Intel/AMD-Hardware-Encode
# stürzt sonst mit "undefined symbol: vaMapBuffer2" ab (BtbN-Issue #457).
# Wir bauen daher libva 2.24.1 (VA-API 1.24) aus dem Quelltext und überschreiben
# die Distro-Version am Standard-Pfad. Der per apt installierte iHD-Treiber ist
# abwärtskompatibel; fehlt ihm ein neuer Backend-Hook, fällt libva intern auf
# vaMapBuffer zurück.
ARG LIBVA_VERSION=2.24.1
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential meson ninja-build pkg-config libdrm-dev \
    && wget -q "https://github.com/intel/libva/archive/refs/tags/${LIBVA_VERSION}.tar.gz" \
        -O /tmp/libva.tar.gz \
    && tar -xf /tmp/libva.tar.gz -C /tmp \
    && cd /tmp/libva-${LIBVA_VERSION} \
    && meson setup build \
        --prefix=/usr --libdir=/usr/lib/x86_64-linux-gnu \
        -Dwith_x11=no -Dwith_glx=no -Dwith_wayland=no \
    && meson install -C build \
    && ldconfig \
    # Verifizieren, dass das neue libva (VA-API >= 1.24) + vaMapBuffer2 aktiv sind:
    && echo "== Gebaute libva / VA-API Version: $(pkg-config --modversion libva) (libva ${LIBVA_VERSION}) ==" \
    && ( pkg-config --modversion libva | grep -qE '^1\.(2[4-9]|[3-9][0-9])\.' \
         || (echo "FEHLER: libva VA-API-Version < 1.24!" && exit 1) ) \
    && ( nm -D --defined-only /usr/lib/x86_64-linux-gnu/libva.so.2 | grep -q vaMapBuffer2 \
         || (echo "FEHLER: libva ohne vaMapBuffer2 gebaut!" && exit 1) ) \
    && cd / && rm -rf /tmp/libva* \
    # Build-Werkzeuge wieder entfernen, damit das Image schlank bleibt:
    && apt-get purge -y build-essential meson ninja-build pkg-config libdrm-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------ Moderner FFmpeg (BtbN)
# Statischer GPL-Build inkl. libvmaf, NVENC, VAAPI, QSV (libvpl).
# Default ist die aktuelle Linie (Tag latest): FFmpeg 9.0, NVENC-API 13.1,
# Host-Treiber 610 oder neuer. latest-legacy übergibt
# ffmpeg-n8.1-latest-linux64-gpl-8.1 und IMAGE_CHANNEL=legacy
# (NVENC-Treiber vor 610). Beide Varianten prüfen av1_nvenc im Binary.
ARG FFMPEG_BUILD=ffmpeg-n9.0-latest-linux64-gpl-9.0
ARG IMAGE_CHANNEL=latest
RUN wget -q "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/${FFMPEG_BUILD}.tar.xz" \
        -O /tmp/ffmpeg.tar.xz \
    && tar -xf /tmp/ffmpeg.tar.xz -C /tmp \
    && cp /tmp/${FFMPEG_BUILD}/bin/ffmpeg  /usr/local/bin/ffmpeg \
    && cp /tmp/${FFMPEG_BUILD}/bin/ffprobe /usr/local/bin/ffprobe \
    && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
    && rm -rf /tmp/ffmpeg.tar.xz /tmp/${FFMPEG_BUILD} \
    # Build-Zeit-Verifikation: bricht den Build ab, falls NVENC/AV1 fehlt
    && echo "== Enthaltene NVENC/AV1-Encoder ==" \
    && /usr/local/bin/ffmpeg -hide_banner -encoders | grep -iE "nvenc|libsvtav1" \
    && /usr/local/bin/ffmpeg -hide_banner -encoders | grep -q av1_nvenc \
       || (echo "FEHLER: av1_nvenc fehlt im FFmpeg-Build!" && exit 1)

# ------------------------------------------------------- Jellyfin-FFmpeg 8.1
# Zweites Binary. BtbN bleibt ffmpeg/ffprobe. Jellyfin bringt die HDR-Filter,
# die im Upstream fehlen: tonemap_cuda, erweitertes tonemap_opencl,
# tonemap_vaapi und tonemapx. 8.1 spricht dieselbe NVENC-API wie latest-legacy
# (Treiber vor 610, inklusive 575).
ARG JELLYFIN_FFMPEG_VERSION=8.1.3-2
RUN wget -q "https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/v${JELLYFIN_FFMPEG_VERSION}/jellyfin-ffmpeg_${JELLYFIN_FFMPEG_VERSION}_portable_linux64-gpl.tar.xz" \
        -O /tmp/jf.tar.xz \
    && mkdir -p /tmp/jf \
    && tar -xf /tmp/jf.tar.xz -C /tmp/jf \
    && JF_BIN="$(find /tmp/jf -type f -name ffmpeg -print -quit)" \
    && install -m 0755 "$JF_BIN" /usr/local/bin/ffmpeg-jellyfin \
    && find "$(dirname "$JF_BIN")" -maxdepth 1 -type f -name '*.so*' -exec cp -a {} /usr/local/lib/ \; \
    && ldconfig \
    && rm -rf /tmp/jf /tmp/jf.tar.xz \
    && /usr/local/bin/ffmpeg-jellyfin -hide_banner -filters | grep -q tonemap_cuda \
    && /usr/local/bin/ffmpeg-jellyfin -hide_banner -filters | grep -q tonemap_opencl \
    && /usr/local/bin/ffmpeg-jellyfin -hide_banner -filters | grep -q tonemap_vaapi \
    && /usr/local/bin/ffmpeg-jellyfin -hide_banner -filters | grep -q " tonemapx " \
       || (echo "FEHLER: Jellyfin-FFmpeg ohne HDR-Tonemap-Filter!" && exit 1)

# ------------------------------------------------------------- dovi_tool
# Für die (experimentelle) Dolby-Vision-RPU-Erhaltung bei HEVC. Statisches
# musl-Binary aus den GitHub-Releases (quietvoid/dovi_tool).
ARG DOVI_TOOL_VERSION=2.3.4
RUN wget -q "https://github.com/quietvoid/dovi_tool/releases/download/${DOVI_TOOL_VERSION}/dovi_tool-${DOVI_TOOL_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
        -O /tmp/dovi_tool.tar.gz \
    && mkdir -p /tmp/dovi \
    && tar -xf /tmp/dovi_tool.tar.gz -C /tmp/dovi \
    # Layout des Archivs kann variieren (dovi_tool bzw. ./dovi_tool) -> per find holen
    && install -m 0755 "$(find /tmp/dovi -type f -name dovi_tool | head -n1)" /usr/local/bin/dovi_tool \
    && rm -rf /tmp/dovi /tmp/dovi_tool.tar.gz \
    && /usr/local/bin/dovi_tool --version

# --------------------------------------------------------- hdr10plus_tool
# HDR10+-Metadaten aus HEVC lesen und nach dem Encode wieder einsetzen.
# Offizielles Binary kann kein AV1 (Stand 1.7.2).
ARG HDR10PLUS_TOOL_VERSION=1.7.2
RUN wget -q "https://github.com/quietvoid/hdr10plus_tool/releases/download/${HDR10PLUS_TOOL_VERSION}/hdr10plus_tool-${HDR10PLUS_TOOL_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
        -O /tmp/hdr10plus_tool.tar.gz \
    && mkdir -p /tmp/hdr10plus \
    && tar -xf /tmp/hdr10plus_tool.tar.gz -C /tmp/hdr10plus \
    && install -m 0755 "$(find /tmp/hdr10plus -type f -name hdr10plus_tool | head -n1)" /usr/local/bin/hdr10plus_tool \
    && rm -rf /tmp/hdr10plus /tmp/hdr10plus_tool.tar.gz \
    && /usr/local/bin/hdr10plus_tool --version

# -------------------------------------------------- SSIMULACRA2 & Butteraugli
# Standbild-Vergleich Quelle gegen Encode. Statische Binaries aus dem
# libjxl-Release (devtools), kein eigener Compiler-Lauf.
# Ab v0.13.0 liefert libjxl das Linux-Static-Archiv als .tar.lz (lzip).
ARG SSIMULACRA2_VERSION=v0.13.0
RUN apt-get update && apt-get install -y --no-install-recommends lzip \
    && wget -q "https://github.com/libjxl/libjxl/releases/download/${SSIMULACRA2_VERSION}/jxl-linux-x86_64-static.tar.lz" \
        -O /tmp/jxl.tar.lz \
    && mkdir -p /tmp/jxl \
    && tar --lzip -xf /tmp/jxl.tar.lz -C /tmp/jxl \
    && S2_BIN="$(find /tmp/jxl -type f -name ssimulacra2 | head -n1)" \
    && test -n "$S2_BIN" \
    && install -m 0755 "$S2_BIN" /usr/local/bin/ssimulacra2 \
    && BA_BIN="$(find /tmp/jxl -type f -name butteraugli_main | head -n1)" \
    && test -n "$BA_BIN" \
    && install -m 0755 "$BA_BIN" /usr/local/bin/butteraugli_main \
    && rm -rf /tmp/jxl.tar.lz /tmp/jxl \
    && apt-get purge -y lzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/ffmpeg -y -hide_banner -loglevel error \
        -f lavfi -i color=c=black:s=32x32 -frames:v 1 /tmp/s2a.png \
    && cp /tmp/s2a.png /tmp/s2b.png \
    && score="$(/usr/local/bin/ssimulacra2 /tmp/s2a.png /tmp/s2b.png)" \
    && python3 -c 'import sys; v=float(sys.argv[1]); assert v > 90, v' "$score" \
    && bdist="$(/usr/local/bin/butteraugli_main /tmp/s2a.png /tmp/s2b.png | head -n1)" \
    && python3 -c 'import sys; v=float(sys.argv[1]); assert 0.0 <= v < 0.01, v' "$bdist" \
    && rm -f /tmp/s2a.png /tmp/s2b.png

# ------------------------------------------------------------- VMAF-Modelle
# libvmaf liegt im FFmpeg-Build. Die JSON-Dateien kommen extra.
# Default ist VMAF v1 (Tag latest) inkl. HDR-VMAF v1.1.8 (ICtCp PQ 16-Bit via
# zscale). latest-legacy setzt die v0.6.1-Dateinamen per --build-arg und
# deaktiviert die HDR-VMAF-v1-Modelle (leerer Wert). In v1 steckt NEG im
# Modell, daher zeigen die NEG-Variablen dort auf dieselbe Datei.
ARG VMAF_MODEL_1080P=vmaf_v1.0.16_3d0h.json
ARG VMAF_MODEL_4K=vmaf_v1.0.16_1d5h_2160.json
ARG VMAF_MODEL_1080P_NEG=vmaf_v1.0.16_3d0h.json
ARG VMAF_MODEL_4K_NEG=vmaf_v1.0.16_1d5h_2160.json
ARG VMAF_MODEL_HDR_1080P=hdr_vmaf_v1.1.8_3d0h.json
ARG VMAF_MODEL_HDR_4K=hdr_vmaf_v1.1.8_1d5h_2160.json
ENV IMAGE_CHANNEL=${IMAGE_CHANNEL} \
    LIBVA_VERSION=${LIBVA_VERSION} \
    VMAF_MODEL_1080P=${VMAF_MODEL_1080P} \
    VMAF_MODEL_4K=${VMAF_MODEL_4K} \
    VMAF_MODEL_1080P_NEG=${VMAF_MODEL_1080P_NEG} \
    VMAF_MODEL_4K_NEG=${VMAF_MODEL_4K_NEG} \
    VMAF_MODEL_HDR_1080P=${VMAF_MODEL_HDR_1080P} \
    VMAF_MODEL_HDR_4K=${VMAF_MODEL_HDR_4K}
RUN mkdir -p ${VMAF_MODEL_DIR} \
    && wget -q "https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v0.6.1.json" \
        -O ${VMAF_MODEL_DIR}/vmaf_v0.6.1.json \
    && wget -q "https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_4k_v0.6.1.json" \
        -O ${VMAF_MODEL_DIR}/vmaf_4k_v0.6.1.json \
    && wget -q "https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v0.6.1neg.json" \
        -O ${VMAF_MODEL_DIR}/vmaf_v0.6.1neg.json \
    && wget -q "https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_4k_v0.6.1neg.json" \
        -O ${VMAF_MODEL_DIR}/vmaf_4k_v0.6.1neg.json \
    && wget -q "https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v1.0.16/vmaf_v1.0.16_3d0h.json" \
        -O ${VMAF_MODEL_DIR}/vmaf_v1.0.16_3d0h.json \
    && wget -q "https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v1.0.16/vmaf_v1.0.16_1d5h_2160.json" \
        -O ${VMAF_MODEL_DIR}/vmaf_v1.0.16_1d5h_2160.json \
    && wget -q "https://raw.githubusercontent.com/Netflix/vmaf/master/model/hdr_vmaf_v1.1.8/hdr_vmaf_v1.1.8_3d0h.json" \
        -O ${VMAF_MODEL_DIR}/hdr_vmaf_v1.1.8_3d0h.json \
    && wget -q "https://raw.githubusercontent.com/Netflix/vmaf/master/model/hdr_vmaf_v1.1.8/hdr_vmaf_v1.1.8_1d5h_2160.json" \
        -O ${VMAF_MODEL_DIR}/hdr_vmaf_v1.1.8_1d5h_2160.json \
    && if [ "${IMAGE_CHANNEL}" != "legacy" ] && [ -n "${VMAF_MODEL_HDR_1080P}" ]; then \
        python3 -c 'import json; d=json.load(open("'"${VMAF_MODEL_DIR}/${VMAF_MODEL_HDR_1080P}"'")); d.get("model_dict",{}).pop("conversion_target",None); json.dump(d, open("/tmp/hdr_smoke_model.json","w"))' \
        && /usr/local/bin/ffmpeg -y -hide_banner \
            -f lavfi -i "color=c=gray:s=1920x1080:r=24:d=0.084,format=yuv420p10le,setparams=range=limited:color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc" \
            -f lavfi -i "color=c=gray:s=1920x1080:r=24:d=0.084,format=yuv420p10le,setparams=range=limited:color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc" \
            -filter_complex "[0:v]zscale=pin=bt2020:tin=smpte2084:min=bt2020nc:rin=limited:p=bt2020:t=smpte2084:m=ictcp:r=limited,format=yuv420p16le[d];[1:v]zscale=pin=bt2020:tin=smpte2084:min=bt2020nc:rin=limited:p=bt2020:t=smpte2084:m=ictcp:r=limited,format=yuv420p16le[r];[d][r]libvmaf=model='path=/tmp/hdr_smoke_model.json':log_fmt=json:log_path=/tmp/hdr_smoke_out.json:shortest=1:n_threads=2" \
            -f null - \
            > /tmp/hdr_smoke_ff.log 2>&1 \
        || { echo "FEHLER: HDR-VMAF Smoke-Test"; cat /tmp/hdr_smoke_ff.log; exit 1; }; \
        python3 -c 'import json; d=json.load(open("/tmp/hdr_smoke_out.json")); v=float(d["pooled_metrics"]["vmaf"]["mean"]); assert v == v and 0.0 <= v <= 100.0, v; print("== HDR-VMAF Smoke-Test OK:", round(v, 2), "==")' \
        && rm -f /tmp/hdr_smoke_model.json /tmp/hdr_smoke_out.json /tmp/hdr_smoke_ff.log; \
    fi

# --------------------------------------------------------------- Python-App
WORKDIR /app
COPY requirements.txt .
# Ubuntu 24.04 markiert das System-Python als "externally managed" (PEP 668)
# und blockiert systemweites pip. Im Container ist das unkritisch, daher
# --break-system-packages statt eines separaten venv.
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY . .

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
