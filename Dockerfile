# switchboard container image.
#
#   docker build -t switchboard .
#
# Plan 018 removed the private `sluice` sibling dependency, and with it the
# named BuildKit context this build used to need. The repository is now the
# whole build context.

FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

WORKDIR /build
COPY . ./switchboard/

RUN uv build --wheel --out-dir /wheels ./switchboard


FROM python:3.13-slim AS runtime

# Non-root. The image carries no credentials — provider keys arrive at
# runtime through the environment variables named by `api_key_env` in the
# config — so there is no reason for it to run privileged.
RUN useradd --create-home --uid 10001 switchboard

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
COPY --from=builder /wheels /wheels

# Install the first-party wheel by FILE, never by name.
#
# This is not belt-and-braces, it is the fix for an observed failure. A
# first attempt installed switchboard by name with `--find-links`, which
# SUPPLEMENTS the index rather than replacing it — and PyPI has an unrelated
# `switchboard` at 1.6.9, which outranks our 0.1.0. The image built cleanly
# and shipped a stranger's package; the only symptom was a missing console
# script.
#
# Naming the file removes name resolution from the equation entirely.
# Public dependencies still come from the index, which is what --no-deps
# would wrongly prevent.
RUN uv pip install --system /wheels/switchboard-*.whl \
 && rm -rf /wheels

USER switchboard
WORKDIR /home/switchboard

# Config path and listen address are runtime concerns; both are overridable
# by flag or environment, so the default is only a convenience for
# `docker run` with a mounted config.
ENTRYPOINT ["switchboard", "serve"]
CMD ["--config", "/etc/switchboard/switchboard.toml"]
