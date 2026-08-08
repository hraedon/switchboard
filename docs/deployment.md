# Deploying switchboard

## Building the image

```bash
docker build -t switchboard:dev .
```

The build needs nothing but this repo. Plan 018 removed the private `sluice`
sibling dependency, so there is no named BuildKit context and no second tree
to supply; the runtime dependencies are `httpx` and `uvicorn`, both public.

*(Earlier revisions of this document described a `--build-context
sluice=../sluice` build. That has not been correct since Plan 018 and is
gone.)*

## The name-collision hazard, and why the wheel installs by file

`switchboard` has a public namesake on PyPI (1.6.9, an unrelated project;
ours is 0.1.0). This is not theoretical. An early version of this Dockerfile
installed switchboard **by name** with `--find-links /wheels`. That flag
*supplements* the package index rather than replacing it, so the resolver
preferred PyPI's 1.6.9 over our 0.1.0. The image built without error and
shipped a stranger's package; the only visible symptom was that the
`switchboard` console script did not exist.

The Dockerfile therefore installs the wheel by explicit file path, so the
name is never resolved against an index:

```dockerfile
RUN uv pip install --system /wheels/switchboard-*.whl
```

`--no-deps` is *not* the fix — public dependencies (httpx, uvicorn) must
still resolve normally.

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

## Path shape: paste the vendor's API root, point clients anywhere

Since Plan 021 switchboard **composes** the upstream URL rather than
concatenating the client's path onto it. The rule is one sentence:

> The base declares the version if it has one.

When `upstream` ends in a version segment (`/v1`, `/v4`, `/v1beta`), a
leading version on the client's path is redundant and is dropped. When
`upstream` carries no version, the client's is preserved.

**Configure `upstream` as the provider's complete API root, exactly as the
vendor documents it.** Quickstarts almost always give you the versioned form,
so this is usually copy-and-paste:

```toml
[provider.opencode-go]
upstream = "https://opencode.ai/zen/go/v1"        # note: /zen/go/v1, not /zen/v1

[provider.ollama-cloud]
upstream = "https://ollama.com/v1"

[provider.zai-coding-plan]
upstream = "https://api.z.ai/api/coding/paas/v4"  # /v4, no /v1 anywhere
```

**Clients need no accommodation.** Point them at switchboard the way you
would point them at any OpenAI-compatible endpoint:

```jsonc
"options": { "baseURL": "http://switchboard:8801/v1" }
```

The version-free form (`"http://switchboard:8801"`) also works and is what
existing deployments use — both compose to the same upstream URL, which is
why this change broke nothing already running.

That is the whole point of composing: three providers whose real roots are
`/zen/go/v1`, `/v1`, and `/v4` all serve one route, from clients that know
nothing about any of it. Previously clients had to omit `/v1` or every
request 404'd inside the upstream — a failure that reads as switchboard being
broken rather than as a base URL with one segment too many.

The one case worth knowing: if `upstream` is a bare host
(`https://api.example.com`) the client's `/v1` passes through unchanged. That
is deliberate — it keeps bare-host configurations working — but it means a
client sending no version reaches `https://api.example.com/chat/completions`.
If the provider needs `/v1`, put it in `upstream` where it belongs.

## Health endpoints

| path | meaning | use |
|---|---|---|
| `/healthz` | process is up | liveness probe |
| `/readyz` | every provider's first poll has completed | readiness probe |

`/readyz` stays 503 until the estate is polled. That is the correct
readiness semantic — a switchboard that has not yet heard from its providers
cannot route sensibly — so do not weaken it to make a rollout look faster.
