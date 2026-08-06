# switchboard container image.
#
#   docker build --build-context sluice=../sluice -t switchboard .
#
# switchboard depends on `sluice`, a private package declared in
# pyproject.toml as `[tool.uv.sources] sluice = { path = "../sluice" }`. It
# is not on PyPI — the public name belongs to an unrelated project — so the
# build needs both checkouts.
#
# It gets them through a NAMED BuildKit context rather than by widening the
# main context to the parent directory. That distinction is not cosmetic:
# the parent here holds ~20 unrelated repositories and 8.8 GB, all of which
# a parent-directory build would ship to the daemon, with nothing but
# .dockerignore patterns between another project's contents and this image.
# A named context takes exactly the one sibling tree that is needed.
#
# Vendoring sluice into this repo would also make the build work, and would
# be worse: the projects are separately versioned and a copy drifts
# silently.

FROM python:3.13-slim AS builder

# uv resolves the path dependency; pip cannot.
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

WORKDIR /build
COPY --from=sluice . ./sluice/
COPY . ./switchboard/

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
