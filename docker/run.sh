#!/usr/bin/env bash
# Entrypoint wrapper for batch schedulers (cron, Kubernetes Job).
#
# buzzcheck's exit codes are a public contract:
#   0 = something on sale
#   1 = nothing on sale         <-- success, not failure
#   2 = error
#   3 = no matches              <-- success, not failure
#
# The CLI does not collapse these. We remap here so a Job controller sees
# 0/1/3 as "the job ran fine" and only 2 as "the job failed."
#
# Optional side effect: when BUZZCHECK_NTFY_TOPIC is set, an ntfy
# notification fires whenever on_sale_count > 0. Notification is a side
# channel, never a contract — failures here MUST NOT affect the run's
# exit code, or the "did the check succeed" signal becomes a lie.
#
# Intentionally no `set -e`: it would abort on the very exit code we need
# to inspect. `set -u` and `set -o pipefail` are fine.
set -u
set -o pipefail

# Capture stdout so we can both re-emit it (pod logs stay unchanged) and
# inspect it (drive the notification). Stderr passes through untouched.
output=$(buzzcheck --json "$@")
rc=$?

printf '%s\n' "$output"

if [ -n "${BUZZCHECK_NTFY_TOPIC:-}" ]; then
    python3 - "$output" <<'PY' || true
import json, os, sys, urllib.request, urllib.error
try:
    data = json.loads(sys.argv[1])
except (IndexError, json.JSONDecodeError):
    sys.exit(0)
if data.get("on_sale_count", 0) <= 0:
    sys.exit(0)
topic = os.environ["BUZZCHECK_NTFY_TOPIC"]
base = os.environ.get("BUZZCHECK_NTFY_URL", "https://ntfy.sh").rstrip("/")
store = (data.get("store") or {}).get("name") or "your store"
count = data["on_sale_count"]
on_sale = [v for v in data.get("variants", []) if v.get("on_sale")]
cheapest = min(
    (v for v in on_sale if v.get("promo") is not None),
    key=lambda v: v["promo"],
    default=None,
)
if cheapest:
    body = (
        f"{count} BuzzBallz on sale at {store} — cheapest: "
        f"{cheapest['description']} ${cheapest['promo']:.2f} "
        f"(was ${cheapest.get('regular', 0):.2f})"
    )
else:
    body = f"{count} BuzzBallz on sale at {store}"
req = urllib.request.Request(
    f"{base}/{topic}",
    data=body.encode("utf-8"),
    method="POST",
    headers={"Title": "BuzzBallz alert", "Tags": "beers"},
)
try:
    urllib.request.urlopen(req, timeout=10).read()
except (urllib.error.URLError, TimeoutError, OSError):
    pass
PY
fi

case "$rc" in
    0|1|3) exit 0 ;;
    2)     exit 2 ;;
    *)     # Unknown / signal exit — surface it as an error rather than
           # silently swallowing. `exit >125` typically means killed.
           exit 2 ;;
esac
