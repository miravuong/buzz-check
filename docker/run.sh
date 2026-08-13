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
# Intentionally no `set -e`: it would abort on the very exit code we need
# to inspect. `set -u` and `set -o pipefail` are fine.
set -u
set -o pipefail

# Always emit JSON so downstream consumers get a stable, machine-readable
# payload. Any argv passed to the container is appended, so callers can
# still pass a line filter or `--all` etc. `--add` should never appear in
# an automated invocation (invariant 3), but we do not enforce that here —
# the Kubernetes manifest / cron entry is the place to enforce it.
buzzcheck --json "$@"
rc=$?

case "$rc" in
    0|1|3) exit 0 ;;
    2)     exit 2 ;;
    *)     # Unknown / signal exit — surface it as an error rather than
           # silently swallowing. `exit >125` typically means killed.
           exit 2 ;;
esac
