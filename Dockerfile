# switchboard container image.
#
# BUILD CONTEXT IS THE PARENT DIRECTORY, not this repo:
#
#   cd ~/projects/personal && docker build -f switchboard/Dockerfile -t switchboard .
#
# That is unusual enough to be worth explaining. switchboard depends on
# `sluice`, a private package declared in pyproject.toml as
# `[tool.uv.sources] sluice = { path = "../sluice" }`. It is not on PyPI —
# the public name belongs to an unrelated project and must never be
# installed — so the build needs both checkouts visible. Vendoring sluice
# into this repo would fix the build and break the thing that matters: the
# two projects are separately versioned, and a copy would drift silently.
# Widening the context is the honest trade.

FROM python:3.13-slim AS builder

# uv resolves the path dependency; pip cannot.
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

WORKDIR /build
COPY sluice/ ./sluice/
COPY switchboard/ ./switchboard/

# Build both wheels. sluice first: switchboard's own build resolves against it.
RUN uv build --wheel --out-dir /wheels ./sluice \
 && uv build --wheel --out-dir /wheels ./switchboard


FROM python:3.13-slim AS runtime

# Non-root. The image carries no credentials — provider keys arrive at
# runtime through the environment variables named by `api_key_env` in the
# config — so there is no reason for it to run privileged.
RUN useradd --create-home --uid 10001 switchboard

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
COPY --from=builder /wheels /wheels

# Install BOTH first-party wheels by FILE, never by name.
#
# This is not belt-and-braces, it is the fix for an observed failure. A
# first attempt installed switchboard by name with `--find-links`, which
# SUPPLEMENTS the index rather than replacing it — and PyPI has an unrelated
# `switchboard` at 1.6.9, which outranks our 0.1.0. The image built cleanly
# and shipped a stranger's package; the only symptom was a missing console
# script. `sluice` has the same collision waiting (public 0.3.1 vs our
# 1.3.9) and today only escapes it because the version happens not to
# satisfy our constraint.
#
# Naming the files removes name resolution from the equation entirely.
# Public dependencies still come from the index, which is what --no-deps
# would wrongly prevent.
RUN uv pip install --system /wheels/sluice-*.whl \
 && uv pip install --system /wheels/switchboard-*.whl \
 && rm -rf /wheels

USER switchboard
WORKDIR /home/switchboard

# Config path and listen address are runtime concerns; both are overridable
# by flag or environment, so the default is only a convenience for
# `docker run` with a mounted config.
ENTRYPOINT ["switchboard", "serve"]
CMD ["--config", "/etc/switchboard/switchboard.toml"]
