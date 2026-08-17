# buzzcheck

`buzzcheck` is a single-shot Python CLI that queries a pinned Kroger-family
store for BuzzBallz product pricing and optionally stages an on-sale item in
the user's Kroger cart. The tool executes one check per invocation and exits;
it does not poll, watch, or complete a purchase.

Supported Kroger banners: Ralphs, Fred Meyer, King Soopers, Smith's, Fry's,
QFC, Dillons, Harris Teeter, Food4Less.

## Requirements

- Python 3.10 or newer.
- A Kroger account with a shopping profile.
- A Kroger developer application in the **Production** environment, enabled
  for the Cart, Locations, and Products APIs, with the redirect URI
  `http://localhost:8000/callback` registered exactly.

The Certification environment does not expose Kroger user accounts, so the
Cart API cannot be exercised there.

## Installation

```bash
git clone https://github.com/miravuong/buzz-check
cd buzz-check
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Runtime dependencies (`httpx`, `python-dotenv`) are pure Python; no compiler
is required.

## Configuration

### Credentials

Credentials are read from environment variables, with `.env` autoloaded from
the working directory via `python-dotenv`.

```bash
cp .env.example .env
```

| Variable | Purpose | Required |
|---|---|---|
| `KROGER_CLIENT_ID` | Kroger developer app client ID | Yes |
| `KROGER_CLIENT_SECRET` | Kroger developer app client secret | Yes |
| `KROGER_REDIRECT_URI` | OAuth redirect URI, must match the app registration exactly | Defaults to `http://localhost:8000/callback` |
| `BUZZCHECK_BIND_ANY` | When set to `1`, `true`, or `yes`, the OAuth callback server binds `0.0.0.0` regardless of the URI. For containerized `buzzcheck auth`; see [Container deployment](#container-deployment). | No |

`.env` is `.gitignore`d and `.dockerignore`d.

### Store selection

Kroger location IDs are internal identifiers and must be resolved through the
API. `buzzcheck setup` performs the lookup and pins one store to
`~/.buzzcheck/config.json`.

```bash
buzzcheck setup                              # interactive ZIP prompt
buzzcheck setup --zip 92649                  # interactive picker for one ZIP
buzzcheck setup --zip 92649 --pick 1         # non-interactive
buzzcheck setup --location-id 70100123       # skip search
buzzcheck setup --force                      # overwrite an existing pin
buzzcheck stores --zip 92649                 # browse without saving
buzzcheck stores --zip 92649 --chain Ralphs  # filter by banner
```

### Cart authorization

Required only for `--add`. Runs a browser-based OAuth flow, exchanges the
authorization code, and writes the resulting refresh token to
`~/.buzzcheck/token.json` at mode `0600`.

```bash
buzzcheck auth              # no-op if a token is already present
buzzcheck auth --force      # re-authorize
```

The refresh token rotates on every use. `buzzcheck` writes the new value back
to `~/.buzzcheck/token.json` after each refresh.

## Usage

```
buzzcheck [LINE] [OPTIONS]
```

`LINE` is a fixed-set filter, not a search term. Valid values: `chillers`,
`cocktails`, `biggies`, `uncategorized`. Any other value is a usage error.

| Command | Behavior |
|---|---|
| `buzzcheck` | Check every BuzzBallz line at the pinned store |
| `buzzcheck chillers` | Restrict output to the Chillers line |
| `buzzcheck --all` | Include variants that are not on sale |
| `buzzcheck --add` | Stage an on-sale variant in the cart |
| `buzzcheck --add --qty N` | Stage `N` units |
| `buzzcheck --add --upc UPC` | Stage a specific variant by UPC |
| `buzzcheck --add --yes` | Skip confirmation when exactly one variant is on sale |
| `buzzcheck --json` | Emit machine-readable output on stdout |
| `buzzcheck --modality DELIVERY` | Override fulfillment; default is `PICKUP` |
| `buzzcheck --store NAME` | Select a non-default named store from config |
| `buzzcheck setup` | Configure the pinned store |
| `buzzcheck stores --zip ZIP` | List Kroger-family stores near a ZIP |
| `buzzcheck auth` | Complete the OAuth flow for cart writes |
| `buzzcheck selftest` | Diagnose store connectivity independent of BuzzBallz stock |

`--add` requires exactly one selection. If multiple variants are on sale,
either `--upc` or an interactive selection is required. `--yes` refuses to
proceed against ambiguity and exits with a usage error.

The tool does not check out. It stops at the cart; the user reviews and pays
on Kroger's website.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Something on sale (check), or subcommand succeeded |
| `1` | Nothing on sale (check command only) |
| `2` | Error: missing config, auth failure, network, or invalid input |
| `3` | No matches for BuzzBallz at the pinned store (check command only) |

`1` and `3` are success states; only `2` indicates a failure. Non-check
subcommands (`setup`, `stores`, `auth`, `selftest`) emit `0` on success and
`2` on error, and do not emit `1` or `3`. Exit-code constants are defined at
`buzzcheck/cli.py:34-37`.

## Behavior

**Cart adds are not idempotent.** Kroger's `/cart/add` endpoint is additive.
Two calls at quantity 1 leave quantity 2. The tool contains no retry or
backoff logic on the cart path. Ambiguous outcomes (e.g., timeout) are
reported as exit `2` with an instruction to check the cart before retrying.

**Prices require a location ID.** Kroger's `/products` endpoint returns
HTTP 200 with empty price fields when no location is attached. `buzzcheck`
always passes `filter.locationId`; a store must be pinned before any priced
query.

**`--add` requires an unambiguous selection.** When two or more variants are
on sale, `--yes` returns a usage error rather than picking one heuristically.
`--upc` or interactive selection resolves the ambiguity.

**Rate limits are per endpoint.** Concurrent invocations share the same
per-account budget. Parallelizing does not increase throughput; it produces
`429` responses.

## State locations

| Path | Contents | Mode |
|---|---|---|
| `.env` | Kroger client ID and secret | Repo-relative, `.gitignore`d |
| `~/.buzzcheck/config.json` | Pinned store metadata | `0600` |
| `~/.buzzcheck/token.json` | OAuth refresh token | `0600` |

`~/.buzzcheck` is resolved via `Path.home()`; there is no override
environment variable. Config and tokens live outside the repository so
cloning does not inherit another user's state.

## Container deployment

### Docker

The provided `Dockerfile` produces a multi-stage image (`python:3.12-slim`)
with the CLI installed into a venv copied from the build stage. The image
runs as UID 10001 (`app`, created via `useradd --create-home`), is
compatible with `readOnlyRootFilesystem`, and declares `VOLUME
/home/app/.buzzcheck` for persistent state.

The entrypoint is `/usr/local/bin/buzzcheck-run`, a wrapper (`docker/run.sh`)
that invokes `buzzcheck --json "$@"` and remaps exit codes for batch
schedulers: `0`, `1`, and `3` become `0`; `2` remains `2`; any other code
becomes `2`.

```bash
docker build -t buzzcheck:dev .
docker run --rm --env-file .env \
    -v buzzcheck-state:/home/app/.buzzcheck \
    buzzcheck:dev
```

To invoke the CLI directly (bypassing the wrapper), override the entrypoint:

```bash
docker run --rm --env-file .env \
    -v buzzcheck-state:/home/app/.buzzcheck \
    --entrypoint buzzcheck buzzcheck:dev setup --zip 92649 --pick 1
```

To run `buzzcheck auth` inside a container, set `BUZZCHECK_BIND_ANY=1` so
the callback server binds `0.0.0.0` (reachable from the host through the
published port) while the advertised `KROGER_REDIRECT_URI` remains the value
registered with Kroger:

```bash
docker run --rm -p 8000:8000 \
    --env-file .env -e BUZZCHECK_BIND_ANY=1 \
    -v buzzcheck-state:/home/app/.buzzcheck \
    --entrypoint buzzcheck buzzcheck:dev auth
```

### Docker Compose

`compose.yaml` provides a local development harness with the named volume,
port publishing, and `BUZZCHECK_BIND_ANY=1` already configured:

```bash
docker compose build
docker compose run --rm buzzcheck                # wrapped JSON check
docker compose run --rm buzzcheck chillers       # with line filter
docker compose run --rm --entrypoint buzzcheck buzzcheck setup --zip 92649
```

The compose service enables `read_only: true` and mounts a `tmpfs` at
`/tmp`, mirroring the intended Kubernetes shape.

## Kubernetes deployment

The Kubernetes layer runs the CLI on a schedule as a batch job — its
purpose is to invoke `buzzcheck --json` at a fixed cadence without a
human present, persist the rotating OAuth refresh token across runs on
a mounted volume, and source credentials from Secrets rather than image
layers or environment files. The container image and CLI are unchanged;
the manifests only supply scheduling, storage, and credential wiring.
Scheduled runs are notify-only. Cart mutation (`--add`) remains a
human-initiated action, either through a manually-triggered Job or a
direct CLI invocation.

Manifests live under `deploy/` and are managed with Kustomize. The base
targets Kubernetes 1.27 or newer (required for `CronJob.spec.timeZone`).

```
deploy/
  base/                     Kustomize base
    kustomization.yaml
    namespace.yaml          `buzzcheck` namespace
    serviceaccount.yaml     SA with automountServiceAccountToken: false
    pvc.yaml                ReadWriteOnce 10Mi PVC (`buzzcheck-state`)
    cronjob.yaml            CronJob with seed-state init container
  overlays/
    dev/                    Example overlay (namespace: `buzzcheck-dev`)
      kustomization.yaml
      .env.secret.example   Template for the credentials Secret
      config.json.example   Template for the store-metadata ConfigMap
      .gitignore            Blocks real .env.secret / config.json / token.json
```

### CronJob configuration

The base CronJob encodes the following operational constraints:

| Field | Value | Purpose |
|---|---|---|
| `spec.schedule` | `0 9 * * *` | Default cadence (override in an overlay) |
| `spec.timeZone` | `America/Los_Angeles` | Schedules are interpreted in local time |
| `spec.concurrencyPolicy` | `Forbid` | Prevents overlapping runs (RWO PVC, per-account rate limit) |
| `spec.startingDeadlineSeconds` | `300` | Discards missed runs after 5 minutes |
| `spec.successfulJobsHistoryLimit` | `3` | Bounded Job history |
| `spec.failedJobsHistoryLimit` | `3` | Bounded Job history |
| `jobTemplate.spec.backoffLimit` | `0` | No retries on failure |
| `jobTemplate.spec.activeDeadlineSeconds` | `120` | Bounds hung-run duration |
| `jobTemplate.spec.ttlSecondsAfterFinished` | `86400` | Auto-cleanup after 24h |
| `template.spec.restartPolicy` | `Never` | No pod-level retries |
| `template.spec.automountServiceAccountToken` | `false` | The Job does not call the Kubernetes API |

Pod and container security contexts run non-root (`runAsUser: 10001`,
`fsGroup: 10001`), enable `readOnlyRootFilesystem`, drop all capabilities,
disallow privilege escalation, and use the `RuntimeDefault` seccomp profile.

Scheduled runs never pass `--add`. Cart mutation must be initiated by a
human through a separately-triggered Job or a direct CLI invocation.

### State provisioning

An init container (`seed-state`) populates the state volume from an
optional ConfigMap and Secret before the main container starts:

- `config.json` (from the `buzzcheck-config` ConfigMap) is written on every
  run. Updating the ConfigMap re-pins the store on the next execution.
- `token.json` (from the `buzzcheck-token` Secret) is written **only if the
  file does not already exist** on the volume. The Kroger refresh token
  rotates on each use, and the CLI writes the new value back to the volume;
  seeding a stale copy would invalidate the live token.

Both sources are declared `optional: true`. When absent, the CLI produces
its standard exit-`2` "no store pinned" error rather than a `FailedMount`
event.

### Credential provisioning (dev overlay)

The dev overlay demonstrates the credentialing pattern using Kustomize
generators. Neither real secrets nor real store data are checked in.

```bash
cd deploy/overlays/dev
cp .env.secret.example .env.secret            # fill in real values
cp config.json.example config.json            # fill in real store data
kubectl kustomize .                           # render locally
kubectl apply -k .                            # apply to cluster
```

`disableNameSuffixHash: true` on both generators keeps the resource names
(`buzzcheck-credentials`, `buzzcheck-config`) stable so the base's `envFrom`
and volume references resolve without patching.

The `buzzcheck-token` Secret generator is commented out by default. Enable
it only when running `--add` via a manually-triggered in-cluster Job is
desired.

### Phone notifications (ntfy)

The container's entrypoint wrapper (`docker/run.sh`) optionally sends an
[ntfy.sh](https://ntfy.sh) push notification whenever a run reports
`on_sale_count > 0`. Set `BUZZCHECK_NTFY_TOPIC` in the credentials
Secret; the existing `envFrom` wiring picks it up.

| Variable | Purpose |
|---|---|
| `BUZZCHECK_NTFY_TOPIC` | ntfy topic name. Acts as a public-URL password — anyone who knows it can push to your phone. Pick something un-guessable (e.g. `openssl rand -hex 8`). Leave unset to disable notifications. |
| `BUZZCHECK_NTFY_URL` | ntfy server base URL. Defaults to `https://ntfy.sh`. Override only when self-hosting. |

Subscribe your phone by installing the ntfy app
([iOS](https://apps.apple.com/us/app/ntfy/id1625396347) /
[Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy))
and subscribing to the same topic string.

Notification failures never affect the run's exit code. The check
succeeding is the primary contract; ntfy is a side channel. Notifications
fire only on `on_sale_count > 0` — no heartbeat, no error-path pings.
For dead-token / silent-failure alerting, see the planned Prometheus
Pushgateway integration in `CLAUDE.md`.

### Validation

Manifests are validated with `kubectl kustomize` and
[kubeconform](https://github.com/yannh/kubeconform):

```bash
kubectl kustomize deploy/base > /tmp/base.yaml
kubectl kustomize deploy/overlays/dev > /tmp/overlay.yaml
kubeconform -strict -kubernetes-version 1.29.0 -summary /tmp/base.yaml
kubeconform -strict -kubernetes-version 1.29.0 -summary /tmp/overlay.yaml
```

## JSON output schema

`buzzcheck --json` writes a JSON object to stdout:

```json
{
  "store": {
    "location_id": "70100123",
    "chain": "Ralphs",
    "name": "Ralphs Fresh Fare",
    "address": "5241 Warner Ave, Huntington Beach, CA 92649"
  },
  "line_filter": null,
  "checked_at": "2026-01-15T17:00:00Z",
  "match_count": 3,
  "on_sale_count": 1,
  "total_matches_all_lines": 3,
  "variants": [
    {
      "description": "BuzzBallz Chillers Watermelon Smash",
      "upc": "0008532100013",
      "size": "200 ml",
      "regular": 3.49,
      "promo": 2.79,
      "on_sale": true,
      "percent_off": 20,
      "price_available": true,
      "line": "chillers"
    }
  ]
}
```

Errors caught before or during the check are reported as
`{"error": "..."}` on stdout in addition to the human-readable message on
stderr, and the process still exits with the appropriate code.

## Troubleshooting

| Symptom | Cause | Resolution |
|---|---|---|
| `command not found: buzzcheck` | Package not installed or venv inactive | `source .venv/bin/activate && pip install -e .` |
| Exit `2`, "no store pinned" | `~/.buzzcheck/config.json` missing | `buzzcheck setup` |
| Zero stores returned by `setup` | ZIP is out of range, or Kroger absent from the area | Widen `--radius` |
| All results have `price_available: false` | Location ID not attached to the request | Verify `~/.buzzcheck/config.json` contains a `location_id` |
| "No matches for BuzzBallz" | Store does not stock the product, or setup issue | `buzzcheck selftest` distinguishes the two |
| OAuth callback returns an error page | `KROGER_REDIRECT_URI` differs from the app registration | Match exactly, including port |
| `403 missing required scopes` | Wrong scope for the endpoint | Search uses `product.compact`; cart uses `cart.basic:write` |
| `401 unauthorized` on cart write | Refresh token expired or revoked | `buzzcheck auth --force` |
| Container `auth` flow times out on host browser | Callback listener bound to `127.0.0.1` inside the container | Set `BUZZCHECK_BIND_ANY=1` |

## Notes

Alcohol availability varies by state and store. `--modality` defaults to
`PICKUP` because delivery is unavailable for most alcohol SKUs. Stores
verify ID at handoff; staging an item in the cart does not complete a
purchase.

This is an unofficial client. It is not affiliated with, endorsed by, or
sponsored by The Kroger Co.

MIT licensed.
