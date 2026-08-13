# syntax=docker/dockerfile:1.7

# ---- build stage: install into a venv we can copy wholesale ----
FROM python:3.12-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /src

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY pyproject.toml ./
COPY buzzcheck ./buzzcheck

RUN pip install .

# ---- runtime stage ----
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

# Non-root user with a real passwd entry so Path.home() resolves to /home/app,
# not "/". UID 10001 is arbitrary but above the systemd reserved range.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app

COPY --from=build /opt/venv /opt/venv
COPY docker/run.sh /usr/local/bin/buzzcheck-run
RUN chmod 0755 /usr/local/bin/buzzcheck-run

USER app
WORKDIR /home/app

# ~/.buzzcheck is where config.json and token.json live. Mount a volume here
# to persist them across runs; the directory itself is created lazily by the
# CLI on first write (mode 0700).
VOLUME ["/home/app/.buzzcheck"]

# Default: emit JSON, remap exit codes for batch schedulers. Override the
# entrypoint (or CMD) for interactive use (e.g. `buzzcheck auth`).
ENTRYPOINT ["/usr/local/bin/buzzcheck-run"]
