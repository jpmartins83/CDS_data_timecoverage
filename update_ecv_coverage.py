#!/usr/bin/env python3
"""
Weekly refresh of the C3S ECV temporal-coverage figures.

Converted from coverage_per_lot_from_constraints_v2.ipynb for unattended
(cron) use on Atos.  The run does, in order:

  1. update the cads-forms-json checkout and put it on the `prod` branch;
  2. derive the time coverage of every ECV from the CDS constraints files
     (plus a handful of manifest/NetCDF lookups for the datasets whose
     coverage is not described in the form);
  3. write the per-ECV and per-product spreadsheets and figures;
  4. diff the per-product coverage against the previous run and append the
     differences to a change log (see "Change tracking" below);
  5. upload the per-product PNG, the two change-tracking CSVs and an HTML
     rendering of the change log to https://sites.ecmwf.int/cxjo/ecv-info/ .

What ends up on ECMWF Sites, all under names that do not change from week to
week, so a Confluence page can link to them once:

  temporal_coverage_by_Product_latest.png  the figure (also uploaded dated)
  coverage_changelog.html                  the change log as a table; this is
                                           what a Confluence HTML macro should
                                           point an <iframe> at, since a .csv
                                           is downloaded rather than displayed
  coverage_changelog.csv                   the same data, for download
  coverage_state.csv                       the current coverage, for download
  coverage_state_versions.csv              the same, per sub-product and version

Change tracking
---------------
The notebook compared two dated .xlsx files whose names had to be edited by
hand.  Instead this script keeps two small CSV files in the output directory:

  coverage_state.csv      the coverage as of the last successful run; it is
                          the baseline for the next diff and is rewritten at
                          the end of every run.
  coverage_state_versions.csv
                          the same per (Product, Subproduct, Version); this
                          is what the diff is actually done on, so that each
                          change names the individual product (sensor,
                          algorithm, ...) and version it concerns.
  coverage_changelog.csv  append-only history, one row per change, carrying
                          the run date, the kind of change, the sub-product,
                          the version and the old/new dates.  The whole history of the products therefore
                          lives in a single file that can be opened, filtered
                          or plotted directly.

The changes of the current run are also written to coverage_changes_latest.md
and printed to stdout, so the weekly cron mail carries the delta in its body.

Environment
-----------
  ECMWF_SITES_TOKEN   ECMWF Sites token, needed for the upload
  ENVEO_PASSWORD      password of the ENVEO manifest server (ice sheets)
  EODC_PASSWORD       password of the EODC manifest server (TWSA, lake water
                      level, gravimetric mass balance)
  CDS_MANIFEST_USER   user for both of the above (defaults to "cds")

The two manifest passwords used to be hard-coded in the notebook; they are
read from the environment here so that they stay out of the repository.

Usage
-----
    python update_ecv_coverage.py [--config config-athos.yml] [--output-dir .]
                                  [--no-git] [--no-upload] [--skip-ecv]
"""

from __future__ import annotations

import os

# Must be set before matplotlib/plotly are imported: the batch nodes have no
# usable system fontconfig, and kaleido needs a writable TMPDIR.
_USER = os.environ.get("USER", "cxjo")
os.environ.setdefault("FONTCONFIG_FILE", f"/home/{_USER}/myfonts/fonts.conf")
os.environ.setdefault("FONTCONFIG_PATH", f"/home/{_USER}/myfonts")
os.environ.setdefault("TMPDIR", f"/home/{_USER}/tmp")
os.makedirs(os.environ["TMPDIR"], exist_ok=True)

import argparse
import itertools
import json
import logging
import re
import shutil
import subprocess
import sys
from calendar import monthrange
from html import escape
from functools import lru_cache
from glob import glob
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import xarray as xr
import yaml

LOG = logging.getLogger("ecv_coverage")

MANIFEST_USER = os.environ.get("CDS_MANIFEST_USER", "cds")

SITES_SPACE = "cxjo"
SITES_NAME = "ecv-info"
SITES_REMOTE_PATH = "."

# Gravimetric mass balance: the manifest listed in generate.yaml is stale, so
# the EODC one is fetched directly (temporary, as in the notebook).
GMB_MANIFEST_URL = (
    "https://cds.c3s.eodc.eu/manifest/"
    "manifest_c3s2_312a_eodc_ice_sheets_gravimetry_latest.txt"
)

HUB_COLORS = {
    "Atmospheric Composition": "#f4a261",
    "Atmospheric Physics": "#ff6f00",
    "Cryosphere": "#1f9ebc",
    "Land Biosphere": "#a6b84c",
    "Land Hydrology": "#4caf0b",
    "Ocean": "#1f4e8c",
}

# Hand-tuned display order of the 24 ECVs of the per-ECV figure; it is applied
# only when the config still holds exactly those 24 entries (see order_ecvs).
ECV_FIGURE_ORDER = [0, 2, 14, 3, 21, 4, 15, 8, 6, 7, 1, 9, 10, 5,
                    12, 19, 11, 18, 17, 22, 20, 13, 16, 23]

CONF: dict = {}
DATASETS_DIR = ""
CDS_FORM_DIR = ""


def manifest_password(server: str) -> str:
    """Password of a manifest server, read from the environment."""
    var = {"enveo": "ENVEO_PASSWORD", "eodc": "EODC_PASSWORD"}[server]
    try:
        return os.environ[var]
    except KeyError:
        raise SystemExit(
            f"{var} is not set; export it before running (the {server} "
            "manifest server needs it)")


# ---------------------------------------------------------------------------
# repository update
# ---------------------------------------------------------------------------
def update_forms_repo(repo_dir: str, branch: str = "prod") -> None:
    """Put `repo_dir` on `branch` and fast-forward it to origin."""
    repo = Path(repo_dir)
    if not (repo / ".git").exists():
        raise SystemExit(f"{repo} is not a git checkout")

    dirty = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if dirty:
        raise SystemExit(
            f"{repo} has local modifications; refusing to update:\n{dirty}"
        )

    for cmd in (
        ["git", "-C", str(repo), "fetch", "--prune", "origin", branch],
        ["git", "-C", str(repo), "checkout", branch],
        ["git", "-C", str(repo), "merge", "--ff-only", f"origin/{branch}"],
    ):
        LOG.info("running %s", " ".join(cmd))
        subprocess.run(cmd, check=True)

    head = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%h %ci"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    LOG.info("%s is on %s at %s", repo, branch, head)


# ---------------------------------------------------------------------------
# manifest / NetCDF helpers
# ---------------------------------------------------------------------------
def rm_manifests(datasets_dir: str) -> None:
    """Delete previously downloaded manifest files."""
    for fname in glob(f"{datasets_dir}/*manifest*"):
        os.remove(fname)


def download(url: str, dest_dir: str, user=None, password=None) -> None:
    """Fetch `url` into `dest_dir` with wget, no shell involved."""
    cmd = ["wget", "--no-verbose"]
    if user is not None:
        cmd += [f"--user={user}", f"--password={password}"]
    cmd += [url, "-P", dest_dir]
    subprocess.run(cmd, check=True)


def generate_yaml(entry: str) -> dict:
    """Read the gecko generate.yaml of a catalogue entry."""
    fname = f"{CONF['cads_forms_yml_dir']}/{entry}/gecko-config/generate.yaml"
    with open(fname) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# product versions and sub-products
# ---------------------------------------------------------------------------
# Every product is tracked per (Subproduct, Version).  The version comes, in
# order of preference, from the product's config:
#
#   version: 'A3'                 hardcoded
#   version:                      looked up from a constraint key
#     key: sensor_on_satellite
#     values: {atsr2_on_ers2: 'v3.0', ...}
#   version_manifest:             read off the files listed in the manifests
#     source: manifest            of generate.yaml (default); "url" to match
#                                 the manifest URLs themselves, or the name of
#                                 a file in gecko-config (e.g. list-study.txt)
#     select: hoaps               only the manifests whose URL contains this
#     pattern: '/(v[0-9.]+)/'     regex, group 1 is the version
#     by: climate_data_record_type
#                                 optional: a record only gets the versions of
#                                 the manifests whose URL contains its value
#
# or else from the "version"/"product_version" key of the constraint records.
# The datasets whose dates are read from manifests or data files get their
# version from the file names (see the extract_dates_* functions).
#
#   subproduct: [sensor_on_satellite, algorithm]
#
# names the constraint keys that tell the individual products of an entry
# apart (sensor, algorithm, ...).  They only split the change tracking; the
# figure still shows one line per product.

# constraint keys that hold the product version, in order of preference
VERSION_KEYS = ("version", "product_version")
UNKNOWN_VERSION = "unknown"

_warned: set = set()


def _warn_once(msg: str, *args) -> None:
    if (msg, args) not in _warned:
        _warned.add((msg, args))
        LOG.warning(msg, *args)


def _redact(url: str) -> str:
    """Drop the user:password@ part of a URL, for logging."""
    return re.sub(r"//[^/@]+@", "//", url)


@lru_cache(maxsize=None)
def read_url(url: str) -> tuple[str, ...]:
    """Non-empty lines of a text file on the web; () if it cannot be fetched."""
    res = subprocess.run(["wget", "--quiet", "-O", "-", url],
                         capture_output=True, text=True)
    if res.returncode != 0:
        LOG.warning("could not fetch %s (wget status %d)",
                    _redact(url), res.returncode)
        return ()
    return tuple(line.strip() for line in res.stdout.splitlines() if line.strip())


def dotted(version: str) -> str:
    """v1_1 -> v1.1: CDS form values use "_" where the versions use "."."""
    return re.sub(r"(?<=[0-9])_(?=[0-9])", ".", version)


def file_version(name: str, pattern: str) -> str:
    """The version captured by group 1 of `pattern` in a file name or URL."""
    m = re.search(pattern, name)
    if m is None:
        _warn_once("no version matching %r in %s", pattern, _redact(name))
        return UNKNOWN_VERSION
    return m.group(1)


@lru_cache(maxsize=None)
def manifest_versions(entry: str, source: str, select: str,
                      pattern: str) -> dict[str, frozenset]:
    """Versions of the files listed in the manifests of `entry`, per manifest."""
    if source in ("manifest", "url"):
        urls = [u for u in generate_yaml(entry)["manifest"] if select in u]
        if source == "url":
            return {u: frozenset([file_version(u, pattern)]) for u in urls}
        return {u: frozenset(file_version(line, pattern) for line in read_url(u))
                for u in urls}
    fname = f"{CONF['cads_forms_yml_dir']}/{entry}/gecko-config/{source}"
    with open(fname) as f:
        lines = [line.strip() for line in f if line.strip()]
    return {source: frozenset(file_version(line, pattern) for line in lines)}


def row_versions(row, cfg=None) -> list[str]:
    """The versions a constraint record applies to."""
    cfg = cfg or {}
    row = row.dropna()

    fixed = cfg.get("version")
    if isinstance(fixed, str):
        return [fixed]
    if isinstance(fixed, dict):
        versions = []
        for value in row[fixed["key"]]:
            if value not in fixed["values"]:
                _warn_once("%s: no version configured for %s=%s",
                           cfg.get("Product"), fixed["key"], value)
            versions.append(fixed["values"].get(value, UNKNOWN_VERSION))
        return versions

    vm = cfg.get("version_manifest")
    if vm:
        found = manifest_versions(cfg["entry"][0].rstrip("/"),
                                  vm.get("source", "manifest"),
                                  vm.get("select", ""), vm["pattern"])
        by = vm.get("by")
        versions = set()
        for url, vs in found.items():
            if by is None or any(value in url for value in row[by]):
                versions |= vs
        return sorted(versions) or [UNKNOWN_VERSION]

    for key in VERSION_KEYS:
        if key in row.keys():
            return [str(v) for v in row[key]]
    _warn_once("%s: no version in the constraints nor in the config",
               cfg.get("Product"))
    return [UNKNOWN_VERSION]


def row_labels(row, cfg=None) -> list[tuple[str, str]]:
    """The (Subproduct, Version) pairs a constraint record applies to.

    The sub-product is named after the values of the config's `subproduct`
    keys (e.g. "slstr_on_sentinel_3a / swansea").  A record may list several
    values of those keys; the versions are worked out for each combination on
    its own, so that a version looked up from one of the keys stays paired
    with its own sensor.
    """
    row = row.dropna()
    keys = [k for k in (cfg or {}).get("subproduct", []) if k in row.keys()]
    if not keys:
        return [("", v) for v in row_versions(row, cfg)]

    labels = []
    for combo in itertools.product(*(row[k] for k in keys)):
        single = row.copy()
        for key, value in zip(keys, combo):
            single[key] = [value]
        labels += [(" / ".join(combo), v) for v in row_versions(single, cfg)]
    return labels


def coverage_per_version(df: pd.DataFrame, cfg=None,
                         fmin=None, fmax=None) -> pd.DataFrame:
    """Earliest/latest date of each (Subproduct, Version) of the records.

    A record lists the versions and sub-products it applies to, so its dates
    count towards each of them.
    """
    dated = pd.DataFrame({
        "label": df.apply(row_labels, axis=1, args=(cfg,)),
        "DateBeg": df.apply(fmin or compute_datemin, axis=1),
        "DateEnd": df.apply(fmax or compute_datemax, axis=1),
    }).explode("label")
    dated["Subproduct"] = dated["label"].str[0]
    dated["Version"] = dated["label"].str[1].map(dotted)
    return dated.groupby(["Subproduct", "Version"]).agg(
        DateBeg=("DateBeg", "min"), DateEnd=("DateEnd", "max"))


def per_file_coverage(files: list[tuple[str, pd.Timestamp, pd.Timestamp]]) -> pd.DataFrame:
    """Coverage per version from (version, first date, last date) of data files."""
    dated = pd.DataFrame(files, columns=["Version", "DateBeg", "DateEnd"])
    dated["Version"] = dated["Version"].map(dotted)
    return dated.assign(Subproduct="").groupby(["Subproduct", "Version"]).agg(
        DateBeg=("DateBeg", "min"), DateEnd=("DateEnd", "max"))


def span(versions: pd.DataFrame):
    """Overall (earliest, latest) date over all versions."""
    return versions["DateBeg"].min(), versions["DateEnd"].max()


@lru_cache(maxsize=None)
def extract_dates_from_TSI(datasets_dir: str, entry: str):
    """Total solar irradiance: coverage comes from the ICDR data file itself."""
    generate = generate_yaml(entry)

    rm_manifests(datasets_dir)
    for url in generate["manifest"]:
        download(url, datasets_dir)
    LOG.info("manifests: %s", glob(f"{datasets_dir}/*manifest*"))

    # the TSI ICDR manifest is the most complete one
    fname_tsi = next(
        f for f in glob(f"{datasets_dir}/*manifest*")
        if f.endswith("TSI_ICDR_latest.txt")
    )
    with open(fname_tsi) as f:
        tsi_url = f.readlines()[0].strip()

    fname_tsi_data = os.path.join(datasets_dir, os.path.basename(tsi_url))
    if os.path.exists(fname_tsi_data):
        os.remove(fname_tsi_data)  # else wget saves a .1 duplicate
    download(tsi_url, datasets_dir)

    LOG.info("reading TSI info from %s", fname_tsi_data)
    c = pd.read_csv(fname_tsi_data, skiprows=130, sep=r"\s+", header=None)
    # C3S_RMIB_daily_TSI_composite_ICDR_v3.10.txt
    return per_file_coverage([(file_version(tsi_url, r"_(v[0-9.]+)\.txt$"),
                               pd.Timestamp(str(c[3].iloc[0])),
                               pd.Timestamp(str(c[3].iloc[-1])))])


@lru_cache(maxsize=None)
def extract_dates_icesheets(datasets_dir: str, entry: str, region: str):
    """Ice sheet surface elevation change; region is 'AIS' or 'GrIS'."""
    generate = generate_yaml(entry)

    rm_manifests(datasets_dir)
    for url in generate["manifest"]:
        download(url, datasets_dir)

    files = []
    for fname_manif in glob(f"{datasets_dir}/*manifest*{region}*txt"):
        with open(fname_manif) as f:
            # ENVEO manifests sometimes contain blank lines
            flist = [line.strip() for line in f.readlines() if line.strip()]

        for url in flist:
            fname_path = os.path.join(datasets_dir, url.split("/")[-1])

            if os.path.exists(fname_path):
                LOG.debug("%s already present", fname_path)
            else:
                LOG.info("downloading %s", url)
                download(url, datasets_dir, user=MANIFEST_USER,
                         password=manifest_password("enveo"))

            nc = xr.open_dataset(fname_path)
            # C3S_GrIS_RA_SEC_25km_Vers6_..., version "6_0" in the CDS form
            version = file_version(url, r"_[Vv]ers([0-9]+)_")
            if version != UNKNOWN_VERSION:
                version += "_0"
            files.append((version, pd.Timestamp(nc["time"].values[0]),
                          pd.Timestamp(nc["time"].values[-1])))
    return per_file_coverage(files)


@lru_cache(maxsize=None)
def extract_dates_massbalance(datasets_dir: str):
    """Ice sheet gravimetric mass balance, read from the latest GMB file."""
    download(GMB_MANIFEST_URL, datasets_dir, user=MANIFEST_USER,
             password=manifest_password("eodc"))
    fname_manif = glob(f"{datasets_dir}/*gravimetry_latest.txt")[0]

    with open(fname_manif) as f:
        flist = [line.strip() for line in f.readlines() if line.strip()]
    url = flist[-1]
    fname_gmb = os.path.join(datasets_dir, url.split("/")[-1])

    if os.path.exists(fname_gmb):
        LOG.debug("%s already present", fname_gmb)
    else:
        LOG.info("downloading %s", url)
        download(url, datasets_dir)

    nc = xr.open_dataset(fname_gmb)
    # C3S_GMB_GRACE_vers5.nc
    return per_file_coverage([(file_version(url, r"_vers([0-9]+)\.nc$"),
                               pd.Timestamp(nc["time"].values[0]),
                               pd.Timestamp(nc["time"].values[-1]))])


def extract_dates_derived_glaciers(jfile: str, cfg=None) -> pd.DataFrame:
    """Glacier mass change: hydrological years, 1 Apr to 30 Sep."""
    with open(jfile) as f:
        df = pd.DataFrame(json.load(f))

    def first(row):
        return pd.Timestamp(f"{int(min(row['hydrological_year'])[0:4])}-4-1")

    def last(row):
        return pd.Timestamp(f"{int(max(row['hydrological_year'])[0:4]) + 1}-9-30")

    return coverage_per_version(df, cfg, first, last)


def extract_dates_rgi(jfile: str, cfg=None) -> pd.DataFrame:
    """Randolph Glacier Inventory: every version is an inventory of ~2000.

    http://www.glims.org/rgi_user_guide/06_dataset_summary.html
    """
    with open(jfile) as f:
        df = pd.DataFrame(json.load(f))
    return coverage_per_version(df, cfg, lambda row: pd.Timestamp("1990-01-01"),
                                lambda row: pd.Timestamp("2010-12-31"))


def parse_manifest_date(token: str, end_of_month: bool = False) -> pd.Timestamp:
    """Dates in manifest filenames are YYYYMMDD (velocity, lakes) or YYYYMM (TWSA)."""
    if len(token) == 8:
        return pd.to_datetime(token, format="%Y%m%d")
    ts = pd.to_datetime(token, format="%Y%m")
    # monthly data: cover the whole month
    return ts + pd.offsets.MonthEnd(0) if end_of_month else ts


# version in the file names listed in the manifests, as group 1
MANIFEST_NAME_VERSION = {
    # .../C3S_AIS_IV_200m_S1_20210401_20220331_v1_5.nc
    "satellite-greenland-ice-sheet-velocity": r"_v([0-9]+_[0-9]+)\.nc$",
    # .../C3S_TWSA_GLOBAL_MONTHLY_200204_202509_v1.0.nc
    "satellite-terrestrial-water-storage": r"_(v[0-9]+\.[0-9]+)\.nc$",
    # .../water_level/ICDR/V5.0/C3S_LWL_..._19950605_20241225_R20250228.nc
    "satellite-lake-water-level": r"/V([0-9]+\.[0-9]+)/",
}


@lru_cache(maxsize=None)
def extract_dates_from_manifest_name(datasets_dir: str, entry: str, region=None):
    """Coverage read off the filenames listed in a manifest.

    Works for ice sheet velocity, terrestrial water storage and lake water level.
    """
    manifest_list = generate_yaml(entry)["manifest"]
    if region is not None:
        manifest = next((url for url in manifest_list if region in url), None)
    else:
        manifest = manifest_list[-1]
    LOG.info("%s: manifest %s", entry, manifest)

    rm_manifests(datasets_dir)
    if entry in ("satellite-terrestrial-water-storage", "satellite-lake-water-level"):
        download(manifest, datasets_dir, user=MANIFEST_USER,
                 password=manifest_password("eodc"))
        # C3S_TWSA_GLOBAL_MONTHLY_200204_202509_v1.0.nc
        # C3S_LWL_..._19950605_20241225_R20250228.nc
        ibeg, iend = -3, -2
    else:
        download(manifest, datasets_dir)
        # C3S_AIS_IV_200m_S1_20210401_20220331_v1_5.nc
        ibeg, iend = -4, -3

    fname_manif = glob(f"{datasets_dir}/*manifest_*.txt")[0]
    with open(fname_manif) as f:
        flist = [line.strip() for line in f.readlines() if line.strip()]

    files = []
    for line in flist:
        t = line.split("/")[-1].split("_")
        files.append((file_version(line, MANIFEST_NAME_VERSION[entry]),
                      parse_manifest_date(t[ibeg]),
                      parse_manifest_date(t[iend], end_of_month=True)))
    return per_file_coverage(files)


# ---------------------------------------------------------------------------
# coverage derived from the CDS constraints files
# ---------------------------------------------------------------------------
def datemax2(row):
    row = row.dropna()
    ymax = int(max(row["year"]))
    mmax = int(max(row["month"])) if "month" in row.keys() else 12
    _, dmax = monthrange(ymax, mmax)
    return pd.Timestamp(f"{ymax}-{mmax}-{dmax}")


def datemin2(row):
    row = row.dropna()
    ymin = int(min(row["year"]))
    mmin = int(min(row["month"])) if "month" in row.keys() else 1
    return pd.Timestamp(f"{ymin}-{mmin}-1")


def extract_dates_wv(jfilepath: str, cfg=None) -> pd.DataFrame:
    """Total column water vapour over ocean: the aggregation labels are shifted
    (monthly means yearly, 6-hourly means monthly) so year/month are used raw."""
    with open(jfilepath) as f:
        df = pd.DataFrame(json.load(f))
    return coverage_per_version(df, cfg, datemin2, datemax2)


def check_time_agg(row) -> str:
    row = row.dropna()
    if "time_aggregation" in row.keys():
        time_aggregation = row["time_aggregation"][0]
        if time_aggregation in ("daily_average", "daily_mean", "day", "day_average"):
            return "day"
        if time_aggregation == "daily":
            return "daily"
        # note interim solution for 5_daily_composite and 10_day_average
        if time_aggregation in (
            "monthly_average", "5_daily_composite", "monthly_mean", "27_days",
            "month", "monthly", "month_average", "10_day_average",
        ):
            return "monthly"
        raise SystemExit(f"could not determine time aggregation: {time_aggregation}")
    if "period" in row.keys():  # ice sheets
        return "period"
    if "temporal_aggregation" in row.keys():
        time_aggregation = row["temporal_aggregation"][0]
        if time_aggregation in ("monthly", "6-hourly"):
            return "monthly"
        if time_aggregation == "daily":
            return "daily"
        raise SystemExit(f"error in temporal aggregation: {time_aggregation}")
    if "day" in row.keys():
        return "day"
    if "nominal_day" in row.keys():
        return "nominal_day"
    return "monthly"


def compute_datemax(row):
    time_agg = check_time_agg(row)
    if time_agg == "period":
        per_str = max(row["period"])
        return pd.Timestamp(f"{int(per_str[5:])}-9-30")

    ymax = int(max(row["year"]))
    mmax = int(max(row["month"])) if "month" in row.keys() else 12
    _, ndays = monthrange(ymax, mmax)
    if time_agg in ("day", "nominal_day"):
        dmax = int(max(row[time_agg]))
    elif time_agg == "daily":
        dmax = int(max(row["day"]))
    else:
        dmax = ndays  # last day of month
    if dmax > ndays:
        LOG.warning("allowed dates give day %d in %d-%02d, clipping", dmax, ymax, mmax)
        dmax = ndays
    return pd.Timestamp(f"{ymax}-{mmax}-{dmax}")


def compute_datemin(row):
    time_agg = check_time_agg(row)
    if time_agg == "period":
        per_str = max(row["period"])
        return pd.Timestamp(f"{int(per_str[0:4])}-10-1")

    ymin = int(min(row["year"]))
    mmin = int(min(row["month"])) if "month" in row.keys() else 1
    if time_agg in ("day", "nominal_day"):
        dmin = int(min(row[time_agg]))
    elif time_agg == "daily":
        dmin = int(min(row["day"]))
    else:
        dmin = 1  # first day of month
    _, ndays = monthrange(ymin, mmin)
    if dmin > ndays:
        LOG.warning("allowed dates give day %d in %d-%02d, clipping", dmin, ymin, mmin)
        dmin = 1
    return pd.Timestamp(f"{ymin}-{mmin}-{dmin}")


def _drop_undatable_rows(df: pd.DataFrame, ecv: str) -> pd.DataFrame:
    """Remove constraint records whose dates cannot be derived from the form."""
    if "sensor_and_algorithm" in df.keys():
        drop = [i for i in df.index
                if df["sensor_and_algorithm"][i][0] == "merged_obs4mips"]
        df = df.drop(drop)
    if ecv == "Earth Radiation Budget":
        # total solar irradiance is read from the dataset itself
        drop = [i for i in df.index
                if df["variable"][i][0] == "total_solar_irradiance"]
        df = df.drop(drop)
    return df


def calc_dateminmax_from_cds_form(jfilepath: str, ecv: str, cfg=None) -> pd.DataFrame:
    with open(jfilepath) as f:
        df = pd.DataFrame(json.load(f))
    df = _drop_undatable_rows(df, ecv)
    return coverage_per_version(df, cfg)


def extract_dates_dwd_products(jfilepath: str, product_family: str,
                               cfg=None) -> pd.DataFrame:
    """Clouds / ERB / SRB: one CDS entry holds several product families."""
    with open(jfilepath) as f:
        df = pd.DataFrame(json.load(f))
    families = df["product_family"].apply(lambda v: v[0])
    df2 = df[families == product_family].copy()
    return coverage_per_version(df2, cfg)


# ---------------------------------------------------------------------------
# coverage tables
# ---------------------------------------------------------------------------
def coverage_by_ecv() -> pd.DataFrame:
    """Earliest/latest date of each ECV, over all of its catalogue entries."""
    ecv_dic = {}
    for k, ecv in enumerate(CONF["ECV"]):
        entries = CONF["ECV"][ecv]["entry"]
        datemin_list, datemax_list = [], []

        if ecv == "Earth Radiation Budget":
            tsi_min, tsi_max = span(extract_dates_from_TSI(DATASETS_DIR, entries[0]))
            datemin_list.append(tsi_min)
            datemax_list.append(tsi_max)

        for entry in entries:
            jfilepath = f"{CDS_FORM_DIR}{entry}/constraints.json"
            if entry == "satellite-ice-sheet-elevation-change":
                frames = [extract_dates_icesheets(DATASETS_DIR, entry, "AIS"),
                          extract_dates_icesheets(DATASETS_DIR, entry, "GrIS")]
            elif entry == "satellite-ice-sheet-mass-balance":
                frames = [extract_dates_massbalance(DATASETS_DIR)]
            elif entry == "derived-gridded-glacier-mass-change":
                frames = [extract_dates_derived_glaciers(jfilepath)]
            elif entry == "satellite-greenland-ice-sheet-velocity":
                frames = [extract_dates_from_manifest_name(DATASETS_DIR, entry, "AIS"),
                          extract_dates_from_manifest_name(DATASETS_DIR, entry, "GrIS")]
            elif entry in ("satellite-terrestrial-water-storage",
                           "satellite-lake-water-level"):
                frames = [extract_dates_from_manifest_name(DATASETS_DIR, entry)]
            elif entry == "insitu-glaciers-extent":
                frames = [extract_dates_rgi(jfilepath)]
            elif entry == "satellite-total-column-water-vapour-ocean":
                frames = [extract_dates_wv(jfilepath)]
            else:
                frames = [calc_dateminmax_from_cds_form(jfilepath, ecv)]

            datemin_list += [span(f)[0] for f in frames]
            datemax_list += [span(f)[1] for f in frames]

        ecv_dic[k] = {
            "ECV": ecv,
            "DateBeg": np.min(np.array(datemin_list)),
            "DateEnd": np.max(np.array(datemax_list)),
            "Thematic Hub": CONF["ECV"][ecv]["Thematic_hub"],
        }

    df = pd.DataFrame.from_dict(ecv_dic, orient="index").sort_values(["Thematic Hub"])
    return _format_dates(df)


def coverage_by_product() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Earliest/latest date of every individual product.

    Returns the per-product table, which is what the figure shows, and the
    same broken down into one row per (Product, Subproduct, Version), which is
    what the change tracking compares (see "product versions" above).
    """
    prod_dic = {}
    version_frames = []
    for k_prod, prod in enumerate(CONF["PRODUCT"]):
        cfg = CONF["PRODUCT"][prod]
        ecv = cfg["ECV"]
        entry = cfg["entry"][0]
        jfilepath = f"{CDS_FORM_DIR}{entry}/constraints.json"

        if prod == "ERB_RMIB_TSI":
            versions = extract_dates_from_TSI(DATASETS_DIR, entry)
        elif prod in (
            "CLOUDS_CLARA-A2", "CLOUDS_CLARA-A3", "CLOUDS_CCI_C3S",
            "ERB_NASA_CERES", "ERB_NOAA_HIRS", "ERB_CCI_C3S", "ERB_CLARA-A3",
            "SRB_CLARA-A2", "SRB_CLARA-A3", "SRB_CCI_C3S",
        ):
            versions = extract_dates_dwd_products(
                jfilepath, product_family=cfg["product_family"], cfg=cfg)
        elif prod == "Ice Sheet Gravimetric Mass Balance":
            versions = extract_dates_massbalance(DATASETS_DIR)
        elif prod == "Ice Sheet Surface Elevation Change (Antarctica)":
            versions = extract_dates_icesheets(DATASETS_DIR, entry, "AIS")
        elif prod == "Ice Sheet Surface Elevation Change (Greenland)":
            versions = extract_dates_icesheets(DATASETS_DIR, entry, "GrIS")
        elif prod == "Ice Sheet Velocity (Antarctica)":
            versions = extract_dates_from_manifest_name(DATASETS_DIR, entry, "AIS")
        elif prod == "Ice Sheet Velocity (Greenland)":
            versions = extract_dates_from_manifest_name(DATASETS_DIR, entry, "GrIS")
        elif entry in ("satellite-terrestrial-water-storage",
                       "satellite-lake-water-level"):
            versions = extract_dates_from_manifest_name(DATASETS_DIR, entry)
        elif prod == "Glaciers elevation and mass change data":
            versions = extract_dates_derived_glaciers(jfilepath, cfg)
        elif entry == "insitu-glaciers-extent":
            versions = extract_dates_rgi(jfilepath, cfg)
        elif entry == "satellite-total-column-water-vapour-ocean":
            versions = extract_dates_wv(jfilepath, cfg)
        else:
            versions = calc_dateminmax_from_cds_form(jfilepath, ecv, cfg)

        datemin, datemax = span(versions)
        prod_dic[k_prod] = {
            "Product": cfg["Product"],
            "ECV": ecv,
            "DateBeg": datemin,
            "DateEnd": datemax,
            "Thematic Hub": cfg["Thematic_hub"],
        }
        LOG.info("%s: %s to %s", cfg["Product"], datemin.date(), datemax.date())
        version_frames.append(versions.reset_index().assign(
            Product=cfg["Product"], ECV=ecv, **{"Thematic Hub": cfg["Thematic_hub"]}))

    df = pd.DataFrame.from_dict(prod_dic, orient="index").sort_values(
        ["Thematic Hub", "ECV", "Product"])
    vdf = pd.concat(version_frames, ignore_index=True).sort_values(
        ["Thematic Hub", "ECV", "Product", "Subproduct", "Version"])
    return _format_dates(df), _format_dates(vdf)[VERSION_STATE_COLS]


def _format_dates(df: pd.DataFrame) -> pd.DataFrame:
    for col in ("DateBeg", "DateEnd"):
        df[col] = df[col].dt.ceil(freq="s").dt.strftime("%Y-%m-%d")
    return df


def order_ecvs(df: pd.DataFrame) -> pd.DataFrame:
    """Apply the hand-tuned ECV order, if the config still matches it."""
    if sorted(df.index) == sorted(ECV_FIGURE_ORDER):
        return df.reindex(ECV_FIGURE_ORDER)
    LOG.warning(
        "the ECV list changed (%d entries); keeping the thematic-hub order "
        "and leaving ECV_FIGURE_ORDER untouched", len(df))
    return df


# ---------------------------------------------------------------------------
# figures
# ---------------------------------------------------------------------------
def write_image(fig, path: Path, **kwargs) -> None:
    """fig.write_image, downloading the kaleido browser once if it is missing."""
    try:
        fig.write_image(str(path), **kwargs)
    except Exception:
        LOG.warning("write_image failed, fetching the kaleido browser and retrying")
        import kaleido
        kaleido.get_chrome_sync()
        fig.write_image(str(path), **kwargs)
    LOG.info("wrote %s", path)


def figure_by_ecv(ecv_pd: pd.DataFrame, outdir: Path, today: str) -> None:
    thisyear = pd.Timestamp.today().year + 1

    fig = px.timeline(ecv_pd, x_start="DateBeg", x_end="DateEnd",
                      y="ECV", color="Thematic Hub")
    fig.update_yaxes(autorange="reversed")
    fig.update_layout(autosize=False, width=1200, height=800)

    xlab = np.arange(1970, thisyear + 1).astype("int")
    fig.update_xaxes(minor=dict(ticks="inside", showgrid=True))
    fig.update_layout(xaxis=dict(tickmode="array", tickvals=xlab,
                                 ticktext=[f"{i}" for i in xlab]))
    fig.update_xaxes(tickangle=-45)
    fig.update_layout(xaxis=dict(tickfont=dict(size=10)))
    fig.update_xaxes(range=["1970-01-01", f"{thisyear - 1}-12-31"])

    write_image(fig, outdir / f"temporal_coverage_by_ECV_{today}.pdf")
    write_image(fig, outdir / f"temporal_coverage_by_ECV_{today}.png")


def figure_by_product(prod_pd: pd.DataFrame, outdir: Path, today: str) -> Path:
    df = prod_pd.copy(deep=True)
    df["DateBeg"] = pd.to_datetime(df["DateBeg"])
    df["DateEnd"] = pd.to_datetime(df["DateEnd"])

    fig = go.Figure()
    y_positions, y_labels, hub_first_y = [], [], {}
    current_y = 0

    for hub in df["Thematic Hub"].unique():
        hub_df = df[df["Thematic Hub"] == hub]
        hub_first_y[hub] = current_y  # first row of the hub

        for _, row in hub_df.iterrows():
            fig.add_trace(go.Scatter(
                x=[row["DateBeg"], row["DateEnd"]],
                y=[current_y, current_y],
                mode="lines+markers",
                line=dict(color=HUB_COLORS[hub], width=3),
                marker=dict(size=6, color=HUB_COLORS[hub],
                            line=dict(color="black", width=1)),
                showlegend=False,
            ))
            for date, anchor, shift in ((row["DateEnd"], "left", 6),
                                        (row["DateBeg"], "right", -6)):
                fig.add_annotation(
                    x=date, y=current_y, text=date.strftime("%b %d %Y"),
                    showarrow=False, xanchor=anchor, xshift=shift,
                    font=dict(size=9, color="rgba(0,0,0,0.65)"),
                )
            y_labels.append(row["Product"])
            y_positions.append(current_y)
            current_y -= 1

        current_y -= 1.8  # extra spacing between hubs

    fig.update_yaxes(showticklabels=False, showgrid=False)

    for y_val, label in zip(y_positions, y_labels):
        fig.add_annotation(
            x=-0.30, y=y_val, xref="paper", yref="y", text=label,
            showarrow=False, xanchor="left", align="left",
            font=dict(size=13, color="#2c3e50"),
        )
    for hub, y_val in hub_first_y.items():
        fig.add_annotation(
            x=-0.30, y=y_val + 0.9, xref="paper", yref="y",
            text=f"<b>{hub}</b>", showarrow=False, xanchor="left", align="left",
            font=dict(size=16, color=HUB_COLORS[hub]),
        )

    # below the x tick labels, in the bottom margin
    updated = pd.to_datetime(today, format="%Y%m%d").strftime("%d %b %Y")
    fig.add_annotation(
        x=1, y=0, xref="paper", yref="paper", xanchor="right", yanchor="top",
        yshift=-32, text=f"Last updated {updated}", showarrow=False,
        font=dict(size=10, color="rgba(0,0,0,0.5)"),
    )

    fig.update_xaxes(showgrid=True, gridcolor="rgba(0,0,0,0.08)",
                     griddash="dash", zeroline=False)
    fig.update_layout(
        title=dict(text="<b>ECV time coverage, by product</b>", x=0.06, y=0.925,
                   xanchor="left", font=dict(size=20, family="Roboto")),
        font=dict(family="Roboto", size=13),
        plot_bgcolor="white", paper_bgcolor="white",
        margin=dict(l=260, r=60, t=90, b=60),
        width=1000, height=1200, autosize=False,
    )

    png = outdir / f"temporal_coverage_by_Product_{today}.png"
    write_image(fig, png, scale=4)
    return png


# ---------------------------------------------------------------------------
# change tracking
# ---------------------------------------------------------------------------
STATE_COLS = ["Product", "ECV", "Thematic Hub", "DateBeg", "DateEnd"]
VERSION_KEY_COLS = ["Product", "Subproduct", "Version"]
VERSION_STATE_COLS = VERSION_KEY_COLS + ["ECV", "Thematic Hub", "DateBeg", "DateEnd"]
CHANGE_COLS = ["run_date", "change", "Product", "Subproduct", "Version", "ECV",
               "Thematic Hub", "DateBeg_old", "DateEnd_old", "DateBeg_new",
               "DateEnd_new"]


def _change_row(today, change, key, ref, old, new) -> dict:
    product, subproduct, version = key
    return {
        "run_date": today, "change": change, "Product": product,
        "Subproduct": subproduct, "Version": version,
        "ECV": ref["ECV"], "Thematic Hub": ref["Thematic Hub"],
        "DateBeg_old": old["DateBeg"] if old is not None else "",
        "DateEnd_old": old["DateEnd"] if old is not None else "",
        "DateBeg_new": new["DateBeg"] if new is not None else "",
        "DateEnd_new": new["DateEnd"] if new is not None else "",
    }


def _read_csv(path: Path) -> pd.DataFrame:
    # str throughout, so that versions such as "3_0" or "001" survive as written
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _diff(new: pd.DataFrame, old: pd.DataFrame, today: str) -> list[dict]:
    """Change rows between two coverage tables indexed by VERSION_KEY_COLS."""
    rows = []

    for key in new.index.difference(old.index):
        r = new.loc[key]
        rows.append(_change_row(today, "added", key, r, None, r))

    for key in old.index.difference(new.index):
        r = old.loc[key]
        rows.append(_change_row(today, "removed", key, r, r, None))

    for key in new.index.intersection(old.index):
        n, o = new.loc[key], old.loc[key]
        if n["DateBeg"] == o["DateBeg"] and n["DateEnd"] == o["DateEnd"]:
            if n["ECV"] != o["ECV"] or n["Thematic Hub"] != o["Thematic Hub"]:
                rows.append(_change_row(today, "reclassified", key, n, o, n))
            continue
        if n["DateBeg"] <= o["DateBeg"] and n["DateEnd"] >= o["DateEnd"]:
            change = "extended"
        elif n["DateBeg"] >= o["DateBeg"] and n["DateEnd"] <= o["DateEnd"]:
            change = "shortened"
        else:
            change = "modified"  # one end moved forward, the other backward
        rows.append(_change_row(today, change, key, n, o, n))
    return rows


def _append_changelog(changes: pd.DataFrame, changelog_file: Path) -> None:
    """Append to the change log, first adding any column it predates."""
    if changelog_file.exists():
        log = _read_csv(changelog_file)
        if list(log.columns) != CHANGE_COLS:
            LOG.info("adding the columns %s to %s",
                     [c for c in CHANGE_COLS if c not in log.columns], changelog_file)
            log.reindex(columns=CHANGE_COLS, fill_value="").to_csv(
                changelog_file, index=False)
    changes.to_csv(changelog_file, mode="a", index=False,
                   header=not changelog_file.exists())


def _grouped_markdown(changes: pd.DataFrame) -> str:
    """The changes as a table with one entry per product: the product, ECV and
    hub are only given on the first line of each product's versions."""
    cols = ["Product", "ECV", "Thematic Hub", "change", "Subproduct", "Version",
            "DateBeg_old", "DateEnd_old", "DateBeg_new", "DateEnd_new"]
    table = changes[cols].reset_index(drop=True)
    repeated = table["Product"].eq(table["Product"].shift())
    table.loc[repeated, ["Product", "ECV", "Thematic Hub"]] = ""
    return table.to_markdown(index=False)


def track_changes(prod_pd: pd.DataFrame, versions_pd: pd.DataFrame,
                  outdir: Path, today: str) -> pd.DataFrame:
    """Diff the new coverage against the previous run and log the differences.

    The diff is done per (Product, Subproduct, Version), so every change names
    the individual product (sensor, algorithm, ...) and the version whose
    coverage moved; a new version shows up as "added" even when it does not
    change the overall coverage of its product.

    Returns the changes of this run (empty on the very first run, which only
    establishes the baseline).
    """
    state_file = outdir / "coverage_state.csv"
    versions_state_file = outdir / "coverage_state_versions.csv"
    changelog_file = outdir / "coverage_changelog.csv"
    report_file = outdir / "coverage_changes_latest.md"

    keys = VERSION_KEY_COLS
    new = versions_pd[VERSION_STATE_COLS].set_index(keys)

    if versions_state_file.exists():
        old = _read_csv(versions_state_file).set_index(keys)
    elif state_file.exists():
        # first run with version tracking: compare per product this once
        LOG.info("no %s yet: comparing per product, without versions",
                 versions_state_file)
        old = _read_csv(state_file).assign(Subproduct="", Version="").set_index(keys)
        new = prod_pd[STATE_COLS].assign(Subproduct="", Version="").set_index(keys)
    else:
        LOG.info("no %s yet: this run only establishes the baseline", state_file)
        prod_pd[STATE_COLS].to_csv(state_file, index=False)
        versions_pd[VERSION_STATE_COLS].to_csv(versions_state_file, index=False)
        report_file.write_text(
            f"# ECV coverage changes, {today}\n\nBaseline established, "
            f"{len(prod_pd)} products recorded. No comparison possible yet.\n")
        return pd.DataFrame(columns=CHANGE_COLS)

    changes = pd.DataFrame(_diff(new, old, today), columns=CHANGE_COLS)
    changes = changes.sort_values(["Thematic Hub"] + keys)

    if not changes.empty:
        _append_changelog(changes, changelog_file)

    report = [f"# ECV coverage changes, {today}", ""]
    if changes.empty:
        report.append(f"No change since the previous run ({len(prod_pd)} products).")
    else:
        n_products = changes["Product"].nunique()
        report.append(f"{len(changes)} change(s) to {n_products} product(s) "
                      "since the previous run.")
        report.append("")
        report.append(_grouped_markdown(changes))
    report_file.write_text("\n".join(report) + "\n")
    print("\n".join(report))

    # the new coverage becomes the baseline of the next run
    prod_pd[STATE_COLS].to_csv(state_file, index=False)
    versions_pd[VERSION_STATE_COLS].to_csv(versions_state_file, index=False)
    return changes


# ---------------------------------------------------------------------------
# HTML rendering of the change log, for the Confluence HTML macro
# ---------------------------------------------------------------------------
# text colour, background, for the "change" badge
CHANGE_STYLES = {
    "added":        ("#14406e", "#e4ecf6"),
    "removed":      ("#4a4a4a", "#e9e9e9"),
    "extended":     ("#1f6b27", "#e5f3e7"),
    "shortened":    ("#a32116", "#fbe9e7"),
    "modified":     ("#8a5300", "#fdf1df"),
    "reclassified": ("#5c1a86", "#f2e6f8"),
}

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>C3S ECV time coverage &mdash; change log</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin: 0; padding: 18px;
         font: 14px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         color: #24292f; background: #fff; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  p.meta {{ margin: 0 0 16px; color: #57606a; font-size: 13px; }}
  p.meta a {{ color: #0b5cad; }}
  .wrap {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; min-width: 760px; }}
  th, td {{ padding: 6px 10px; text-align: left; vertical-align: top;
           border-bottom: 1px solid #eaecef; white-space: nowrap; }}
  th {{ position: sticky; top: 0; background: #f6f8fa; font-weight: 600;
       border-bottom: 2px solid #d8dee4; z-index: 1; }}
  tr.newrun td {{ border-top: 2px solid #d8dee4; }}
  tr.cont td:not([rowspan]) {{ border-bottom: 1px dotted #d8dee4; }}
  td.group {{ background: #fbfcfd; }}
  td.product {{ white-space: normal; font-weight: 600; }}
  td.hub, td.ecv {{ color: #57606a; }}
  td.subproduct {{ white-space: normal; }}
  td.version {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
               font-size: 13px; }}
  td.dates {{ font-variant-numeric: tabular-nums; }}
  .badge {{ display: inline-block; padding: 1px 8px; border-radius: 10px;
           font-size: 12px; font-weight: 600; }}
  .none {{ color: #57606a; }}
  .arrow {{ color: #8c959f; padding: 0 2px; }}
  @media (max-width: 600px) {{ body {{ padding: 10px; }} }}
</style>
</head>
<body>
<h1>C3S ECV time coverage &mdash; change log</h1>
<p class="meta">{summary}<br>
Generated {generated} &middot;
<a href="temporal_coverage_by_Product_latest.png">current figure</a> &middot;
<a href="coverage_changelog.csv">changelog CSV</a> &middot;
<a href="coverage_state.csv">current coverage CSV</a> &middot;
<a href="coverage_state_versions.csv">per version</a></p>
{body}
</body>
</html>
"""


def _badge(change: str) -> str:
    fg, bg = CHANGE_STYLES.get(change, ("#24292f", "#eaecef"))
    return (f'<span class="badge" style="color:{fg};background:{bg}">'
            f'{escape(change)}</span>')


def _range(beg: str, end: str) -> str:
    if not beg and not end:
        return '<span class="none">&mdash;</span>'
    return (f'{escape(beg) or "?"}<span class="arrow">&rarr;</span>'
            f'{escape(end) or "?"}')


def _cell(text: str) -> str:
    return escape(text) if text else '<span class="none">&mdash;</span>'


def write_changelog_html(outdir: Path) -> Path:
    """Render coverage_changelog.csv as a standalone HTML page.

    A .csv is downloaded rather than displayed by the browser, so this page is
    what a Confluence HTML macro should point at:

        <iframe src="https://sites.ecmwf.int/cxjo/ecv-info/coverage_changelog.html"
                width="100%" height="800" style="border:0"></iframe>

    Everything is inlined, so the page needs no external request to render.
    """
    changelog_file = outdir / "coverage_changelog.csv"
    state_file = outdir / "coverage_state.csv"
    html_file = outdir / "coverage_changelog.html"

    generated = pd.Timestamp.now().strftime("%d %b %Y %H:%M UTC")

    n_products = None
    if state_file.exists():
        n_products = len(pd.read_csv(state_file))

    if not changelog_file.exists():
        summary = "No changes recorded yet."
        if n_products is not None:
            summary = (f"{n_products} products tracked. "
                       "No changes recorded yet.")
        body = '<p class="none">The change log is still empty.</p>'
        html_file.write_text(HTML_PAGE.format(
            summary=summary, generated=generated, body=body), encoding="utf-8")
        LOG.info("wrote %s", html_file)
        return html_file

    log = _read_csv(changelog_file).reindex(columns=CHANGE_COLS, fill_value="")
    log = log.sort_values(["run_date", "Thematic Hub"] + VERSION_KEY_COLS,
                          ascending=[False, True, True, True, True])

    last_run = log["run_date"].max()
    n_last = int((log["run_date"] == last_run).sum())
    last_run_fmt = pd.to_datetime(last_run, format="%Y%m%d").strftime("%d %b %Y")

    n_last_products = log.loc[log["run_date"] == last_run, "Product"].nunique()
    summary = (f"{n_last} change(s) to {n_last_products} product(s) in the run "
               f"of {last_run_fmt}; {len(log)} change(s) recorded in total")
    if n_products is not None:
        summary += f", across {n_products} tracked products"
    summary += "."

    # one entry per product and run; the run, product, ECV and hub cells span
    # the lines of the individual versions that changed
    rows = []
    previous_run = None
    for (run, _, product), group in log.groupby(
            ["run_date", "Thematic Hub", "Product"], sort=False):
        first = group.iloc[0]
        cls = "newrun" if previous_run is not None and run != previous_run else ""
        previous_run = run
        run_fmt = pd.to_datetime(run, format="%Y%m%d").strftime("%Y-%m-%d")
        span_attr = f' rowspan="{len(group)}"' if len(group) > 1 else ""
        shared = (
            f'<td class="dates group"{span_attr}>{escape(run_fmt)}</td>'
            f'<td class="product group"{span_attr}>{escape(product)}</td>'
            f'<td class="ecv group"{span_attr}>{escape(first["ECV"])}</td>'
            f'<td class="hub group"{span_attr}>{escape(first["Thematic Hub"])}</td>'
        )
        for i, (_, r) in enumerate(group.iterrows()):
            # "cont": another version of the same product follows
            row_cls = " ".join(c for c in (cls if i == 0 else "",
                                           "cont" if i < len(group) - 1 else "") if c)
            rows.append(
                (f'<tr class="{row_cls}">' if row_cls else "<tr>")
                + (shared if i == 0 else "")
                + f"<td>{_badge(r['change'])}</td>"
                f'<td class="subproduct">{_cell(r["Subproduct"])}</td>'
                f'<td class="version">{_cell(r["Version"])}</td>'
                f'<td class="dates">{_range(r["DateBeg_old"], r["DateEnd_old"])}</td>'
                f'<td class="dates">{_range(r["DateBeg_new"], r["DateEnd_new"])}</td>'
                "</tr>"
            )

    body = (
        '<div class="wrap"><table>\n'
        "<thead><tr><th>Run</th><th>Product</th><th>ECV</th><th>Thematic hub</th>"
        "<th>Change</th><th>Sub-product</th><th>Version</th>"
        "<th>Coverage before</th><th>Coverage after</th>"
        "</tr></thead>\n<tbody>\n" + "\n".join(rows) + "\n</tbody></table></div>"
    )

    html_file.write_text(HTML_PAGE.format(
        summary=summary, generated=generated, body=body), encoding="utf-8")
    LOG.info("wrote %s", html_file)
    return html_file


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------
def upload_to_sites(paths) -> list[str]:
    """Upload files to https://sites.ecmwf.int/cxjo/ecv-info/ and return the URLs."""
    from sites.sdk.sites import Authenticator, Site

    try:
        token = os.environ["ECMWF_SITES_TOKEN"]
    except KeyError:
        raise SystemExit(
            "ECMWF_SITES_TOKEN is not set; export it before running, or use "
            "--no-upload")

    site = Site(space=SITES_SPACE, name=SITES_NAME)
    content = site.get_content_manager(
        authenticator=Authenticator.from_token(token=token))

    urls = []
    for path in paths:
        if not Path(path).exists():
            LOG.warning("not uploading %s: it does not exist", path)
            continue
        content.upload(local_path=str(path), remote_path=SITES_REMOTE_PATH)
        url = (f"https://sites.ecmwf.int/{SITES_SPACE}/{SITES_NAME}/"
               f"{Path(path).name}")
        LOG.info("uploaded %s", url)
        urls.append(url)
    return urls


def site_uploads(png: Path, outdir: Path) -> list[Path]:
    """The files published to ECMWF Sites on every run.

    The figure goes up twice: under its dated name, and as
    temporal_coverage_by_Product_latest.png so that a page can link to a URL
    that does not change every week.  The two CSVs and the HTML rendering of
    the change log go up under fixed names for the same reason.
    """
    latest = outdir / "temporal_coverage_by_Product_latest.png"
    shutil.copyfile(png, latest)
    return [
        png,
        latest,
        outdir / "coverage_changelog.html",
        outdir / "coverage_changelog.csv",
        outdir / "coverage_state.csv",
        outdir / "coverage_state_versions.csv",
    ]


# ---------------------------------------------------------------------------
def parse_args(argv=None):
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(here / "config-athos.yml"),
                   help="YAML config (default: %(default)s)")
    p.add_argument("--output-dir", default=str(here),
                   help="where tables, figures and the change log go "
                        "(default: %(default)s)")
    p.add_argument("--no-git", action="store_true",
                   help="do not update the cads-forms-json checkout")
    p.add_argument("--no-upload", action="store_true",
                   help="do not upload the figure to ECMWF Sites")
    p.add_argument("--skip-ecv", action="store_true",
                   help="only produce the per-product table and figure")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p.parse_args(argv)


def main(argv=None) -> int:
    global CONF, DATASETS_DIR, CDS_FORM_DIR

    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    with open(args.config) as f:
        CONF = yaml.safe_load(f)
    CDS_FORM_DIR = CONF["cds_form_dir"]
    DATASETS_DIR = CONF["datasets_dir"].rstrip("/")
    os.makedirs(DATASETS_DIR, exist_ok=True)

    outdir = Path(args.output_dir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    today = pd.Timestamp.today().strftime("%Y%m%d")
    LOG.info("run of %s, output in %s", today, outdir)

    if args.no_git:
        LOG.info("skipping the cads-forms-json update (--no-git)")
    else:
        update_forms_repo(CDS_FORM_DIR, branch="prod")

    # uncomment if needed

    # if not args.skip_ecv:
    #     ecv_pd = coverage_by_ecv()
    #     print(ecv_pd.to_markdown())
    #     # ecv_pd.to_excel(outdir / f"ECV_time_coverage_perECV_{today}.xlsx")
    #     figure_by_ecv(order_ecvs(ecv_pd), outdir, today)

    prod_pd, versions_pd = coverage_by_product()
    print(prod_pd.to_markdown())
    # prod_pd.to_excel(outdir / f"ECV_time_coverage_perProduct_{today}.xlsx")
    png = figure_by_product(prod_pd, outdir, today)

    track_changes(prod_pd, versions_pd, outdir, today)
    write_changelog_html(outdir)

    if args.no_upload:
        LOG.info("skipping the upload (--no-upload)")
    else:
        upload_to_sites(site_uploads(png, outdir))

    LOG.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
