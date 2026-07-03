FROM debian:bookworm-slim

ENV CONDA_DIR=/opt/conda \
    ENV_NAME=able \
    PYTHON_VERSION=3.9 \
    PYTHON=/opt/conda/envs/able/bin/python \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    XDG_CACHE_HOME=/tmp/.cache \
    HOME=/tmp \
    PATH=/opt/conda/envs/able/bin:/opt/conda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        bzip2 \
        ca-certificates \
        cmake \
        curl \
        git \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        libsm6 \
        libxext6 \
        libxrender1 \
        pkg-config \
        wget \
    && case "$(uname -m)" in \
        x86_64|amd64) miniforge_arch="x86_64" ;; \
        aarch64|arm64) miniforge_arch="aarch64" ;; \
        *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac \
    && curl -L \
        "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-${miniforge_arch}.sh" \
        -o /tmp/miniforge.sh \
    && bash /tmp/miniforge.sh -b -p "$CONDA_DIR" \
    && rm -f /tmp/miniforge.sh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY installer.sh installer.py requirements.txt requirements-pyfe.txt /app/
RUN chmod +x /app/installer.sh /app/installer.py \
    && bash /app/installer.sh --env-name "$ENV_NAME"

COPY . /app
RUN chmod +x /app/*.sh /app/*.py

CMD ["/app/run_radiomics_pipeline.sh", "--help"]
