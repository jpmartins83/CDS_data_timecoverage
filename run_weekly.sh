#!/bin/bash
#
# Cron wrapper for update_ecv_coverage.py.
#
# Cron starts with an almost empty environment, so everything the run needs is
# set up here: the conda interpreter, the secrets and a log file.
#
# Install with `crontab -e`, e.g. every Monday at 06:30:
#
#   30 6 * * 1 /home/cxjo/C3S_stuff/CDS_data_timecoverage/run_weekly.sh
#
# Cron mails whatever the job prints, so keep MAILTO set in the crontab to get
# the weekly list of coverage changes by mail:
#
#   MAILTO=joao.martins@ecmwf.int
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export HOME="${HOME:-/home/cxjo}"

# Secrets, kept out of the repository: ECMWF_SITES_TOKEN, ENVEO_PASSWORD and
# EODC_PASSWORD.  Create it from ecv_coverage_env.template and chmod 600 it.
ENVFILE="$HOME/.ecv_coverage_env"
if [[ -r "$ENVFILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENVFILE"
else
    echo "$ENVFILE is missing; copy $HERE/ecv_coverage_env.template to it" >&2
    exit 1
fi

PYTHON="${ECV_COVERAGE_PYTHON:-/perm/cxjo/conda/envs/work/bin/python}"
OUTDIR="${ECV_COVERAGE_OUTDIR:-$HERE}"

LOGDIR="$HERE/logs"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/update_ecv_coverage_$(date +%Y%m%d).log"

cd "$HERE"
echo "=== $(date -Is) starting ===" >> "$LOGFILE"

# The full run is echoed to the log; only the change report and any error go to
# stdout/stderr, so the cron mail stays short.
set +e
"$PYTHON" update_ecv_coverage.py --output-dir "$OUTDIR" "$@" >> "$LOGFILE" 2>&1
status=$?
set -e

if [[ $status -ne 0 ]]; then
    echo "update_ecv_coverage.py failed with status $status; last 40 log lines:" >&2
    tail -40 "$LOGFILE" >&2
    exit $status
fi

cat "$OUTDIR/coverage_changes_latest.md"
echo
echo "Figure: https://sites.ecmwf.int/cxjo/ecv-info/temporal_coverage_by_Product_latest.png"
echo "Full log: $LOGFILE"

# keep three months of logs
find "$LOGDIR" -name 'update_ecv_coverage_*.log' -mtime +90 -delete
