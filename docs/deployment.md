# Deploying switchboard

## Building the image

```bash
docker build --build-context sluice=../sluice -t switchboard:dev .
```

switchboard depends on `sluice`, a private package declared as a path
dependency on a sibling checkout (`[tool.uv.sources]` in `pyproject.toml`).
It is not on PyPI, so the build needs both trees.

sluice arrives through a **named BuildKit context**, not by widening the
main context to the parent directory. The difference matters: the parent
directory on a working checkout holds every other personal repository —
here roughly 20 of them and 8.8 GB — and a parent-directory build ships all
of it to the daemon, leaving nothing but `.dockerignore` patterns between
another project's contents and this image. A named context takes exactly
the one tree required.

Vendoring sluice into this repo would also make the build work, and would be
worse: the two projects are separately versioned and a copy drifts silently.

The build fails if the sluice context is missing, which is correct — the
alternative is resolving the name from an index.

## The name-collision hazard, and why both wheels install by file

Both first-party packages have a public namesake:

| package | ours | on PyPI |
|---|---|---|
| `switchboard` | 0.1.0 | 1.6.9 (unrelated project) |
| `sluice` | 1.3.9 | 0.3.1 (unrelated project) |

This is not theoretical. An early version of this Dockerfile installed
switchboard **by name** with `--find-links /wheels`. That flag *supplements*
the package index rather than replacing it, so the resolver preferred PyPI's
1.6.9 over our 0.1.0. The image built without error and shipped a stranger's
package; the only visible symptom was that the `switchboard` console script
did not exist.

`sluice` escapes the same fate today only by coincidence — the public
versions happen not to satisfy `>=1.3.9,<2.0`. One upstream release would
end that.

The Dockerfile therefore installs **both** wheels by explicit file path, so
neither name is ever resolved against an index:

```dockerfile
RUN uv pip install --system /wheels/sluice-*.whl \
 && uv pip install --system /wheels/switchboard-*.whl
```

Note that `--no-deps` is *not* the fix — public dependencies (httpx,
uvicorn) must still resolve normally.

**If you change the install step, verify the result rather than the build
exit code:**

```bash
docker run --rm --entrypoint sh switchboard:dev -c \
  'python -c "import importlib.metadata as m; print(m.distribution(\"switchboard\").version)"'
# must print 0.1.0
```

## Running

```bash
docker run -d --name switchboard \
  -p 8801:8801 \
  -v /path/to/switchboard.toml:/etc/switchboard/switchboard.toml:ro \
  -e SWITCHBOARD_OPENCODE_GO_KEY=... \
  -e SWITCHBOARD_OLLAMA_CLOUD_KEY=... \
  switchboard:dev \
  --config /etc/switchboard/switchboard.toml --listen 0.0.0.0:8801
```

- **Config** is mounted read-only. `examples/agent-delegation.toml` is a
  working starting point.
- **Provider credentials** arrive only through the environment, under the
  variable names the config's `api_key_env` fields declare. Nothing is baked
  into the image, which is what makes it publishable.
- A configured `api_key_env` whose variable is unset **fails startup by
  design** — falling back would forward the client's own credential to that
  upstream. If the container exits immediately, check that every declared
  variable is present before suspecting anything else.
- The process runs as the unprivileged `switchboard` user (uid 10001).
- `--listen 0.0.0.0:<port>` is required in a container; the default binds
  loopback, which is unreachable from outside the namespace.

## Health endpoints

| path | meaning | use |
|---|---|---|
| `/healthz` | process is up | liveness probe |
| `/readyz` | every provider's first poll has completed | readiness probe |

`/readyz` stays 503 until the estate is polled. That is the correct
readiness semantic — a switchboard that has not yet heard from its providers
cannot route sensibly — so do not weaken it to make a rollout look faster.
