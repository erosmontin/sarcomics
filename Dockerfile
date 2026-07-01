FROM python:3.9-slim-bookworm

ENV PYTHON=/usr/local/bin/python \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    XDG_CACHE_HOME=/tmp/.cache \
    HOME=/tmp

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        git \
        libglib2.0-0 \
        libgl1 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-pyfe.txt /app/
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install "numpy>=1.23,<2.0" "Cython<3" \
    && python -m pip install --no-build-isolation "PyRadiomics==3.0.1" \
    && python -m pip install -r /app/requirements.txt \
    && python -m pip install --ignore-requires-python --no-deps -r /app/requirements-pyfe.txt

COPY . /app
RUN chmod +x /app/*.sh /app/*.py

CMD ["/app/run_radiomics_pipeline.sh", "--help"]
