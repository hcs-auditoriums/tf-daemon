# Development image: tf-daemon with every piece of hardware simulated.
# The repo is bind-mounted at /workspace (see compose.yaml), so the code that
# runs is always the code on your disk; only the dependencies live in the image.
FROM python:3.13-slim-trixie
LABEL org.opencontainers.image.source=https://github.com/hcs-auditoriums/tf-daemon

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

# Headless Chromium for tests/test_browser.py (the pages in a real browser). Only the
# libraries chrome-headless-shell links against (ldd, on amd64 and arm64) and one font for canvas
# text: playwright's --with-deps would add ~330 MB, mostly fonts for other scripts.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libasound2t64 libatk1.0-0t64 libatk-bridge2.0-0t64 libatspi2.0-0t64 libdbus-1-3 libexpat1 libgbm1 libglib2.0-0t64 \
      libnspr4 libnss3 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 \
      libxrandr2 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 && python -m playwright install --only-shell chromium \
 && chmod -R a+rX /opt/playwright

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
