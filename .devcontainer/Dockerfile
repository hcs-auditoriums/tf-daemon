# Development image: tf-daemon with every piece of hardware simulated.
# The repo is bind-mounted at /workspace (see compose.yaml), so the code that
# runs is always the code on your disk; only the dependencies live in the image.
FROM python:3.13-slim-trixie

# libportaudio2: sounddevice imports it even when the audio source is synthetic.
# git / procps: for working inside the container (VS Code, `ps`).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libportaudio2 git procps curl \
 && rm -rf /var/lib/apt/lists/*

# Dependencies come from pyproject.toml (core + dev + rf), so the image stays in
# step with the project. Only rebuilt when pyproject.toml changes.
COPY pyproject.toml /tmp/pyproject.toml
RUN python - <<'EOF' > /tmp/requirements.txt
import tomllib
p = tomllib.load(open("/tmp/pyproject.toml", "rb"))["project"]
print("\n".join(p["dependencies"] + p["optional-dependencies"]["dev"] + p["optional-dependencies"]["rf"]))
EOF
RUN pip install --no-cache-dir -r /tmp/requirements.txt watchfiles

ARG USERNAME=dev
ARG USER_UID=1000
RUN useradd --create-home --uid ${USER_UID} --shell /bin/bash ${USERNAME}
USER ${USERNAME}

# The package is imported straight from the mount; no install step, nothing
# written into the repo.
# OPENBLAS_NUM_THREADS=1: numpy's BLAS otherwise starts a thread per core that
# spin-waits between the small matrix products the analysis does: the idle demo
# server used ~260% CPU with it and ~16% without, and parallel test workers
# starved each other.
ENV PYTHONPATH=/workspace \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OPENBLAS_NUM_THREADS=1
WORKDIR /workspace
EXPOSE 8080
