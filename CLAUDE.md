# CLAUDE.md

Project context for Claude Code. Read this before making changes.

## What this is

`buzzcheck` is a Python CLI that checks whether BuzzBallz are on sale at a
Kroger-family store and can stage them in the user's cart. It is single-shot: it
runs, prints, exits. It does not poll, watch, or check out.

Active work: containerizing it (Docker) and running it as a scheduled job
(Kubernetes CronJob) with real secret handling and metrics. **The CLI is the
product. The platform layer wraps it, it does not rewrite it.** If a Kubernetes
requirement conflicts with CLI behavior, solve it in a wrapper script or
manifest, not by changing the CLI's contract.

## Hard invariants

These are not preferences. Violating any of them is a bug, even if tests pass.

1. **Cart adds are not idempotent.** Kroger's `/cart/add` is additive, not a set
   operation. Two calls at quantity 1 leave quantity 2 in the cart. Never add
   retry logic, backoff, or automatic re-invocation anywhere on the cart add
   path. Never set `backoffLimit > 0` or `restartPolicy: OnFailure` on a Job that
   can reach `--add`.

2. **Exit codes are a public contract.** For `buzzcheck` (the check command):
   `0` = something on sale, `1` = nothing on sale, `2` = error, `3` = no
   matches. Exit `1` is a *success* state, not a failure. Sibling subcommands
   (`setup`, `stores`, `auth`, `selftest`) use `0` for OK and `2` for error and
   do not emit `1` or `3`. Do not collapse these to 0/1 to make a tool happy.
   Callers that need different semantics translate in a wrapper. Constants are
   defined at `buzzcheck/cli.py:34-37`.

3. **Automation is notify-only.** Scheduled runs never pass `--add`. Cart
   mutation is always a deliberate human invocation. This boundary is
   intentional and predates the Kubernetes work.

4. **`--yes` refuses to guess.** When two or more variants are on sale it must
   error and tell the user to pass `--upc`. Do not add a "pick the cheapest"
   heuristic. A wrong order is harder to notice than an error message.

5. **The tool stops at the cart.** No checkout, no payment, no order submission.
   Not behind a flag, not behind a config option.

6. **Prices require a location ID.** Kroger's products endpoint returns HTTP 200
   with no pricing data when no location is attached, so a missing location ID
   looks identical to "nothing is on sale." Any code path that fetches prices
   must assert a location ID is present and fail loudly if it is not.

7. **Never commit or bake in secrets.** `.env` stays gitignored and out of
   Docker layers. `~/.buzzcheck/token.json` is mode `0600` and never leaves the
   user's machine or the cluster's Secret store.

8. **Refresh tokens rotate.** Kroger issues a new refresh token on use. Anything
   that seeds a token into a container must not overwrite a live `token.json`
   with a stale seed copy. Seed only when absent.

9. **Tests never touch the live cart.** No test suite exists today. When one is
   added, mock the Kroger API. A test that calls the real `/cart/add` mutates a
   real person's shopping cart.

## Repository layout

```
buzzcheck/          CLI package (the product)
  cli.py            argparse dispatch, subcommands, add-to-cart flow
  kroger.py         httpx-based Kroger API client (app + user OAuth)
  config.py         ~/.buzzcheck resolution, store + refresh-token I/O
Dockerfile          multi-stage image
docker/run.sh       entrypoint wrapper (exit-code remap for schedulers)
compose.yaml        local dev harness
.dockerignore       keeps .env / .venv / .git out of build context
deploy/base/        Kustomize base manifests           [planned]
deploy/overlays/    per-environment overlays           [planned]
.github/workflows/  build, scan, sign, deploy         [planned]
.env.example        credential template
pyproject.toml
```

Items marked `[planned]` do not exist yet. Do not assume their contents; create
them when the task calls for it.

## Commands

Local development:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env          # then fill in Kroger credentials
buzzcheck setup               # interactive store picker, writes ~/.buzzcheck/config.json
buzzcheck auth                # OAuth browser flow, only needed for --add
buzzcheck selftest            # verifies connectivity vs "store does not stock them"
```

CLI surface:

| Command | Purpose |
|---|---|
| `buzzcheck` | Check all lines at the configured store |
| `buzzcheck {chillers,cocktails,biggies,uncategorized}` | Filter to one line |
| `buzzcheck --all` | Include not-on-sale variants |
| `buzzcheck --json` | Machine-readable output, this is the automation hook |
| `buzzcheck --add [--qty N] [--upc UPC] [--yes]` | Stage in cart, human-invoked only |
| `buzzcheck selftest` | Diagnose connection vs stock |

The positional argument is a line filter, not a search box. `buzzcheck milk` is
a usage error, not a search.

## State locations

| Path | Contains | Committed |
|---|---|---|
| `.env` | Client ID and secret | Never |
| `~/.buzzcheck/config.json` | Store location ID, default term | No, lives outside repo |
| `~/.buzzcheck/token.json` | OAuth refresh token, mode 0600 | No, lives outside repo |

Config and tokens deliberately live in the home directory, not the project, so
cloning never inherits someone else's store and `git add -A` cannot publish
credentials.

## Kroger API notes

- The developer app must use the **Production** environment. Kroger user
  accounts do not exist in Certification, so the Cart API cannot work there.
- The Redirect URI is marked optional on Kroger's registration form. It is not.
  Cart authorization fails without an exact match, port included.
- `401` means an expired token (refresh it). `403` means the wrong scope
  (`product.compact` for search, `cart.basic:write` for cart). Different fixes,
  do not conflate them in error handling.
- Rate limits are per-endpoint, not per-operation. Operations sharing an
  endpoint draw from the same budget. This is why the workload cannot be scaled
  horizontally (see below).

## Docker conventions

- Runtime deps are pure Python (`httpx`, `python-dotenv`), so the build stage
  needs no compiler — plain `pip install` on a slim image is enough. Nothing in
  `pyproject.toml` pins a specific Python patch; the floor is `>=3.10`. Current
  image uses `python:3.12-slim` (latest stable that satisfies the floor).
- Multi-stage build, venv copied from the build stage to keep the final image
  free of build artifacts and pip cache.
- Runs as UID 10001 created via `useradd --create-home` so `$HOME` resolves and
  `~/.buzzcheck` lands somewhere writable. A bare numeric `USER` without a
  passwd entry can make `Path.home()` fall back to `/`. `config.py:9` reads
  `Path.home() / ".buzzcheck"` unconditionally — there is no env-var override.
- `readOnlyRootFilesystem` compatible: anything written at runtime goes to a
  mounted volume (`~/.buzzcheck`) or `/tmp`.
- Distroless is desirable but incompatible with a shell entrypoint. If moving to
  distroless, port `docker/run.sh` to a small Python entrypoint module.
- `.dockerignore` must exclude `.env`, `.venv`, `.git`, `__pycache__`,
  `*.egg-info`, and any local state.
- **OAuth callback bind:** the CLI parses the host from `KROGER_REDIRECT_URI`
  and passes it directly to `http.server.HTTPServer` (`cli.py:672-691`). The
  default URI is `http://localhost:8000/callback`, which binds `127.0.0.1` and
  is unreachable from a host browser when the server runs in a container. To
  run `buzzcheck auth` in a container, set `KROGER_REDIRECT_URI` to a URI
  whose host resolves to `0.0.0.0` (a `BUZZCHECK_BIND_ANY=1` env var is
  supported as a container-friendly override that binds `0.0.0.0` regardless
  of the URI). The Kroger app registration must still list the URI the
  *browser* hits (e.g. `http://localhost:8000/callback`) exactly.

## Kubernetes conventions

Shape: a CronJob, not a Deployment. There is no traffic to serve.

Required settings and the reason each one exists:

| Setting | Value | Why |
|---|---|---|
| `backoffLimit` | `0` | Invariant 1: retries can duplicate cart adds |
| `restartPolicy` | `Never` | Same reason |
| `concurrencyPolicy` | `Forbid` | ReadWriteOnce PVC, and overlapping runs burn rate limit |
| `timeZone` | `America/Los_Angeles` | Schedules are UTC by default (needs k8s 1.27+) |
| `activeDeadlineSeconds` | `120` | A hung run should not hold the PVC |
| `replicas` | n/a | Do not convert this to a Deployment, see below |

The entrypoint wrapper `docker/run.sh` translates exit codes so that `1` and `3`
read as success to the Job controller while `2` still fails the Job. Keep the
translation in the wrapper, never in the CLI (invariant 2).

An init container seeds `config.json` and, **only if absent**, `token.json` into
the state volume (invariant 8).

Metrics go to a Prometheus Pushgateway, which is the correct pattern for batch
jobs since there is no long-lived process to scrape. Always emit
`buzzcheck_last_success_timestamp_seconds`. An alert on that going stale is the
only thing that catches a dead refresh token, which is the primary silent
failure mode.

## Do not suggest

- **HorizontalPodAutoscaler or replicas > 1.** The bottleneck is a per-endpoint
  Kroger rate limit tied to one account. More replicas produce more 429s, not
  more throughput. If an HTTP interface is ever added, it is one replica plus a
  short-TTL cache plus a PodDisruptionBudget.
- **A service mesh, Ingress, or Gateway API resources.** Nothing serves traffic.
- **Retry, backoff, or circuit-breaker libraries on the cart path.** Invariant 1.
- **Checkout, payment, or order submission.** Invariant 5.
- **Watch or daemon mode inside the CLI.** Scheduling belongs to cron or the
  CronJob. The exit codes exist precisely so external schedulers can compose it.
- **Extending the search to other products or retailers.** Fixed product, Kroger
  banners only.

## Style

- Error messages name the fix, not just the symptom. The README's troubleshooting
  table is the tone to match.
- Prefer failing loudly over degrading silently, especially around missing
  location IDs and missing scopes, since both fail as "no results."
- Keep `--json` output stable. It is a consumed interface.
