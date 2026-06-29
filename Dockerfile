# =============================================================================
#  AV1 / VMAF Compression Studio – All-in-One Image
#  Basis: CUDA-Runtime (liefert NVENC-Libs; nvidia-smi kommt zur Laufzeit vom
#  nvidia-container-toolkit). Zusätzlich Intel-QSV/VAAPI- und AMD-VAAPI-Stacks.
#  FFmpeg wird als moderner statischer GPL-Build (mit libvmaf, NVENC, VAAPI,
#  QSV/VPL) eingebunden – die Distro-Version ist zu alt für av1_nvenc/av1_qsv.
# =============================================================================
# Basis-Image überschreibbar – z. B. ältere CUDA-Version, falls der (auf QNAP
# gemountete) Nvidia-Treiber nicht zur Default-CUDA-Version passt:
#   docker build --build-arg CUDA_IMAGE=nvidia/cuda:12.0.0-runtime-ubuntu22.04 .
ARG CUDA_IMAGE=nvidia/cuda:12.4.1-runtime-ubuntu22.04
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
        libva2 libva-drm2 vainfo \
        intel-media-va-driver-non-free \
        mesa-va-drivers \
        libmfx1 \
        # --- GPU-Monitoring-Werkzeuge ---
        intel-gpu-tools radeontop \
        libdrm2 \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------ Moderner FFmpeg (BtbN)
# Statischer GPL-Build inkl. libvmaf, NVENC, VAAPI, QSV (libvpl).
ARG FFMPEG_BUILD=ffmpeg-n7.1-latest-linux64-gpl-7.1
RUN wget -q "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/${FFMPEG_BUILD}.tar.xz" \
        -O /tmp/ffmpeg.tar.xz \
    && tar -xf /tmp/ffmpeg.tar.xz -C /tmp \
    && cp /tmp/${FFMPEG_BUILD}/bin/ffmpeg  /usr/local/bin/ffmpeg \
    && cp /tmp/${FFMPEG_BUILD}/bin/ffprobe /usr/local/bin/ffprobe \
    && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
    && rm -rf /tmp/ffmpeg.tar.xz /tmp/${FFMPEG_BUILD}

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
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
