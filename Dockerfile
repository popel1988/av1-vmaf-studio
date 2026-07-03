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
#  dem Quelltext (2.22) gebaut – sonst crasht jeder Intel/AMD-HW-Encode.
#  Hinweis: CUDA-Images für ubuntu24.04 gibt es erst ab CUDA 12.6.
# =============================================================================
# Basis-Image überschreibbar – z. B. andere CUDA-Version, falls der (auf QNAP
# gemountete) Nvidia-Treiber nicht zur Default-CUDA-Version passt:
#   docker build --build-arg CUDA_IMAGE=nvidia/cuda:12.6.3-runtime-ubuntu24.04 .
# WICHTIG: Bei einem Downgrade eine ubuntu24.04-Variante wählen (>= 12.6),
# sonst ist libva zu alt (siehe oben).
ARG CUDA_IMAGE=nvidia/cuda:12.6.3-runtime-ubuntu24.04
FROM ${CUDA_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video \
    PYTHONUNBUFFERED=1 \
    INPUT_DIR=/media/input \
    OUTPUT_DIR=/media/output \
    VMAF_MODEL_DIR=/usr/local/share/model

# ----------------------------------------------------------------- System-Deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates wget xz-utils tar pciutils \
        python3 python3-pip python3-dev \
        # --- VAAPI / Intel QSV / AMD Userspace-Treiber ---
        # libva2 (wird unten durch Quellbau 2.22 ersetzt) + iHD-Treiber für
        # Intel, mesa-va für AMD. QSV läuft über oneVPL: libvpl2 (Dispatcher) +
        # libmfx-gen1.2 (Gen-Runtime, z. B. UHD 730/Gen12). libmfx1 als Fallback.
        libva2 libva-drm2 vainfo \
        intel-media-va-driver-non-free \
        mesa-va-drivers \
        libmfx1 libmfx-gen1.2 libvpl2 \
        # --- GPU-Monitoring-Werkzeuge ---
        intel-gpu-tools radeontop \
        libdrm2 \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------ Neueres libva bauen
# Der statische BtbN-FFmpeg-Build lädt libva dynamisch und ist gegen libva >=
# 2.21 gebaut (er verlangt das Symbol `vaMapBuffer2`, eingeführt in libva
# 2.21.0 / VA-API 1.21). KEINE gängige Distro liefert das aktuell: Ubuntu 24.04
# hat nur 2.20.0, Ubuntu 22.04 nur 2.12 → jeder Intel/AMD-Hardware-Encode
# stürzt sonst mit "undefined symbol: vaMapBuffer2" ab (BtbN-Issue #457).
# Wir bauen daher libva 2.22 aus dem Quelltext und überschreiben die
# Distro-Version am Standard-Pfad. Der per apt installierte iHD-Treiber ist
# abwärtskompatibel; fehlt ihm der neue Backend-Hook, fällt libva intern auf
# vaMapBuffer zurück.
ARG LIBVA_VERSION=2.22.0
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
    # Verifizieren, dass das neue libva das benötigte Symbol exportiert:
    && ( nm -D --defined-only /usr/lib/x86_64-linux-gnu/libva.so.2 | grep -q vaMapBuffer2 \
         || (echo "FEHLER: libva ohne vaMapBuffer2 gebaut!" && exit 1) ) \
    && cd / && rm -rf /tmp/libva* \
    # Build-Werkzeuge wieder entfernen, damit das Image schlank bleibt:
    && apt-get purge -y build-essential meson ninja-build pkg-config libdrm-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------ Moderner FFmpeg (BtbN)
# Statischer GPL-Build inkl. libvmaf, NVENC, VAAPI, QSV (libvpl).
# n8.1 enthält av1_nvenc (der ältere n7.1-Build NICHT!). Verifiziert per
# strings-Check des Binaries; nicht ohne Grund zurückstufen.
ARG FFMPEG_BUILD=ffmpeg-n8.1-latest-linux64-gpl-8.1
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

# ------------------------------------------------------------- VMAF-Modelle
# libvmaf ist im FFmpeg-Build enthalten, die Modelle werden separat bereitgestellt.
RUN mkdir -p ${VMAF_MODEL_DIR} \
    && wget -q "https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v0.6.1.json" \
        -O ${VMAF_MODEL_DIR}/vmaf_v0.6.1.json \
    && wget -q "https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_4k_v0.6.1.json" \
        -O ${VMAF_MODEL_DIR}/vmaf_4k_v0.6.1.json

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
