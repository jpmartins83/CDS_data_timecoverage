#!/bin/bash
#SBATCH --job-name=ecv-coverage
#SBATCH --qos=nf
#SBATCH --time=01:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --chdir=/home/cxjo/C3S_stuff/CDS_data_timecoverage
#SBATCH --output=/home/cxjo/C3S_stuff/CDS_data_timecoverage/logs/ecv-coverage.%j.out
#SBATCH --mail-type=FAIL
#SBATCH --mail-user=joao.martins@ecmwf.int
#
# Weekly run of update_ecv_coverage.py, as a SLURM batch job.
#
# HPC2020 cron runs on hpc-cron and should not do the work itself; it only
# submits this job (see the HPC2020 Cron service page, UDOC-288336510):
#
#   ssh hpc-cron
#   crontab -e
#
#   MAILTO=joao.martins@ecmwf.int
#   37 12 * * 4 /usr/local/bin/sbatch -Q /home/cxjo/C3S_stuff/CDS_data_timecoverage/run_weekly.sh
#
# -Q suppresses the "Submitted batch job ..." banner, so cron only mails when
# the submission itself fails.  The cron environment sources no profile and has
# no $SCRATCH, hence the absolute paths above and below.
#
# The #SBATCH lines are ordinary comments when the script is run directly, so
#   ./run_weekly.sh [--no-upload] [--skip-ecv]
# still works on a login node for a manual run or a test.
#
set -uo pipefail

# SLURM copies the batch script to /var/spool/slurmd/job<id>/, so the location
# cannot be derived from $BASH_SOURCE here.  Keep this in step with the
# --chdir and --output paths above, which cannot use variables either.
HERE="${ECV_COVERAGE_DIR:-/home/cxjo/C3S_stuff/CDS_data_timecoverage}"
export HOME="${HOME:-/home/cxjo}"

# Secrets, kept out of the repository: ECMWF_SITES_TOKEN, ENVEO_PASSWORD and
# EODC_PASSWORD.  Create it from ecv_coverage_env.template and chmod 600 it.
# The batch job starts with SBATCH_EXPORT=NONE, i.e. it inherits nothing from
# the submitting shell, so this file is the only source of the credentials.
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
# no colon, so ECV_COVERAGE_MAILTO="" switches the report mail off
MAILTO="${ECV_COVERAGE_MAILTO-joao.martins@ecmwf.int}"

LOGDIR="$HERE/logs"
mkdir -p "$LOGDIR"

# Under SLURM stdout is already the --output file; a direct run gets its own.
if [[ -n "${SLURM_JOB_ID:-}" ]]; then
    LOGFILE="$LOGDIR/ecv-coverage.$SLURM_JOB_ID.out"
    echo "SLURM job $SLURM_JOB_ID on $(hostname), $(date -Is)"
else
    LOGFILE="$LOGDIR/ecv-coverage.$(date +%Y%m%d-%H%M%S).out"
    exec > >(tee -a "$LOGFILE") 2>&1
    echo "direct run on $(hostname), $(date -Is)"
fi

# kaleido writes its Chrome profile here; the node-local $TMPDIR set by the
# ECMWF job prologue disappears between steps of some setups, and this path is
# the one the figures are known to render under.
export TMPDIR="$HOME/tmp"
mkdir -p "$TMPDIR"

cd "$HERE"

"$PYTHON" update_ecv_coverage.py --output-dir "$OUTDIR" "$@"
status=$?

REPORT="$OUTDIR/coverage_changes_latest.md"

if [[ $status -ne 0 ]]; then
    echo "update_ecv_coverage.py failed with status $status" >&2
    # --mail-type=FAIL notifies of the job failure; the log holds the traceback
    exit $status
fi

# Mail the week's delta.  The verbose run stays in $LOGFILE.
SITE="https://sites.ecmwf.int/cxjo/ecv-info"
if [[ -n "$MAILTO" ]] && [[ -r "$REPORT" ]] && command -v mail >/dev/null 2>&1; then
    {
        cat "$REPORT"
        echo
        echo "Figure:    $SITE/temporal_coverage_by_Product_latest.png"
        echo "Change log: $SITE/coverage_changelog.html"
        echo "  as CSV:   $SITE/coverage_changelog.csv"
        echo "Current coverage: $SITE/coverage_state.csv"
        echo "  per version:    $SITE/coverage_state_versions.csv"
        echo
        echo "Log: $LOGFILE"
    } | mail -s "ECV time coverage, $(date +%Y-%m-%d)" "$MAILTO"
    echo "report mailed to $MAILTO"
else
    echo "not mailing the report (MAILTO='$MAILTO', report readable: ${REPORT})" >&2
    cat "$REPORT" 2>/dev/null
fi

# keep three months of logs
find "$LOGDIR" -name 'ecv-coverage.*.out' -mtime +90 -delete

echo "finished $(date -Is)"
