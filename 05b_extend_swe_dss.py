"""
Step 5b — Extend an existing UA SWE HEC-DSS file to a later end date.

Standalone step. It is not called by run_pipeline.py.

Appends daily grids after the last date already in a DSS file made by
05_create_swe_dss.py. Existing records are never rewritten. Only stable UA
files are used. The new grids are made exactly as in step 5: HEC-Vortex reads,
clips, reprojects, resamples, and writes them, and cells outside the buffered
watershed are no-data. The grid geometry, DSS pathname parts, and watershed
mask are taken from the existing file and checked before anything is written.

Every existing grid is fingerprinted before and after the append to prove it
was not changed.

Usage:
    python 05b_extend_swe_dss.py \\
        --dss "G:/Python Package/uasnow/data/dss/ua_upper-tennessee_por_swe_1day.dss" \\
        --input_dir "G:/Python Package/uasnow/data/raw/All_NC_Files" \\
        --boundary "F:/PrismCopy/PRISM-explorer/aorc-por-generator/buffered-watershed/upper-tennessee_huc04_10km_buffer.shp" \\
        --end 2025-12-31 --download
"""

import argparse
import csv
import hashlib
import importlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import netCDF4
import numpy as np
import requests
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from shapely.geometry import mapping

from config import DIR_RAW, DOWNLOAD_TIMEOUT, UA_SWE_BASE_URL
from utils import StepTimer, parse_filename_status, setup_logging, water_year

step5 = importlib.import_module("05_create_swe_dss")
logger = None  # set in main() so the log file is written next to the DSS


# ─────────────────────────────────────────────────────────────────────────────
# Arguments
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append daily UA SWE grids to an existing step 5 DSS file, "
                    "without changing the grids already in it."
    )
    parser.add_argument("--dss", type=Path, required=True,
                        help="Existing DSS file made by 05_create_swe_dss.py")
    parser.add_argument("--input_dir", type=Path, default=Path(DIR_RAW),
                        help="Folder with the UA SWE netCDF files (searched recursively)")
    parser.add_argument("--boundary", type=Path, required=True,
                        help="The buffered watershed polygon used to make the DSS file")
    parser.add_argument("--end", type=date.fromisoformat, required=True,
                        help="Last date to add, YYYY-MM-DD")
    parser.add_argument("--download", action="store_true",
                        help="First download missing stable files for the new dates into --input_dir")
    parser.add_argument("--vortex_home", type=Path, default=step5.DEFAULT_VORTEX)
    parser.add_argument("--workers", type=int, default=6,
                        help="Files read and processed by HEC-Vortex at the same time")
    parser.add_argument("--dry_run", action="store_true",
                        help="Report what would be added without downloading or writing")
    args = parser.parse_args()
    if not args.dss.is_file():
        parser.error(f"DSS file not found: {args.dss}")
    if not args.boundary.is_file():
        parser.error(f"Boundary not found: {args.boundary}")
    if args.workers < 1:
        parser.error("--workers must be positive")
    args.input_dir.mkdir(parents=True, exist_ok=True)
    return args


# ─────────────────────────────────────────────────────────────────────────────
# Existing DSS file
# ─────────────────────────────────────────────────────────────────────────────

def path_date(path: str) -> date:
    """Date of the UA file behind a pathname. 03AUG2025:2400 is 04 Aug 2025."""
    d_part = path.split("/")[4]
    day = datetime.strptime(d_part[:9], "%d%b%Y").date()
    return day + timedelta(days=1) if d_part.endswith(":2400") else day


def read_existing(dss_path: Path) -> dict:
    """Catalog the DSS file and confirm it holds one daily SWE series."""
    import hecdss

    with hecdss.HecDss(str(dss_path)) as dss:
        paths = sorted(str(value) for value in dss.get_catalog().uncondensed_paths)
        if not paths:
            raise RuntimeError(f"{dss_path} has no records")
        parts = {tuple(p.split("/")[i] for i in (1, 2, 3, 5, 6)) for p in paths}
        if len(parts) != 1:
            raise RuntimeError(f"{dss_path} holds more than one series: {sorted(parts)[:3]}")
        part_a, part_b, part_c, part_e, part_f = parts.pop()
        if part_c != step5.VARIABLE or part_e:
            raise RuntimeError(f"{dss_path} is not an instantaneous {step5.VARIABLE} series")
        dates = {path_date(p): p for p in paths}
        last = max(dates)
        record = dss.get(dates[last])
        stored = np.flipud(np.asarray(record.data, dtype=np.float64))  # DSS rows run south to north
        return {
            "paths": set(paths), "dates": dates, "last": last,
            "part_a": part_a, "part_b": part_b, "part_f": part_f,
            "units": str(record.dataUnits),
            "cell_size": float(record.cellSize),
            "lower_left": (int(record.lowerLeftCellX), int(record.lowerLeftCellY)),
            "shape": stored.shape,
            "valid": stored > -1e30,
        }


def fingerprint(dss_path: Path, paths: set[str]) -> dict[str, str]:
    """SHA-256 of each record's grid values, units, and geometry."""
    import hecdss

    prints = {}
    with hecdss.HecDss(str(dss_path)) as dss:
        for path in sorted(paths):
            record = dss.get(path)
            if record is None:
                raise RuntimeError(f"Cannot read {path}")
            digest = hashlib.sha256(np.ascontiguousarray(record.data).tobytes())
            digest.update(f"{record.dataUnits}|{record.cellSize}|{record.lowerLeftCellX}|"
                          f"{record.lowerLeftCellY}".encode())
            prints[path] = digest.hexdigest()
    return prints


# ─────────────────────────────────────────────────────────────────────────────
# Stable input files
# ─────────────────────────────────────────────────────────────────────────────

def stable_name(day: date) -> str:
    return f"UA_SWE_Depth_800m_v1_{day:%Y%m%d}_stable.nc"


def local_stable_files(input_dir: Path, days: list[date]) -> dict[date, Path]:
    wanted = {stable_name(day): day for day in days}
    found = {}
    for path in input_dir.rglob(step5.UA_FILE_PATTERN):
        if path.name in wanted and parse_filename_status(path.name) == "stable":
            found[wanted[path.name]] = path
    return found


def server_stable_names(days: list[date]) -> set[str]:
    names = set()
    for wy in sorted({water_year(day) for day in days}):
        url = f"{UA_SWE_BASE_URL}/WY{wy}/"
        response = requests.get(url, timeout=DOWNLOAD_TIMEOUT)
        response.raise_for_status()
        names.update(re.findall(r'href="(UA_SWE_Depth_800m_v1_\d{8}_stable\.nc)"', response.text))
    return names


def download_stable(day: date, input_dir: Path) -> Path:
    """Download one stable file, check its size and that it reads, then keep it."""
    name = stable_name(day)
    target = input_dir / name
    partial = target.with_suffix(".nc.part")
    url = f"{UA_SWE_BASE_URL}/WY{water_year(day)}/{name}"
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
        response.raise_for_status()
        expected = int(response.headers.get("Content-Length", -1))
        with partial.open("wb") as stream:
            for chunk in response.iter_content(chunk_size=1 << 20):
                stream.write(chunk)
    if expected >= 0 and partial.stat().st_size != expected:
        partial.unlink()
        raise RuntimeError(f"Incomplete download of {name}")
    with netCDF4.Dataset(partial) as ds:
        ds.variables[step5.VARIABLE][:]
    partial.replace(target)
    return target


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def update_progress(dss_path: Path, last: date, written: int, added_days: int) -> None:
    """Keep step 5's progress file in step with the extended file, if it exists."""
    marker = step5.progress_path(dss_path)
    if not marker.is_file():
        return
    saved = json.loads(marker.read_text(encoding="utf-8"))
    identity = saved["identity"]
    identity["last_date"] = last.isoformat()
    identity["file_count"] = int(identity.get("file_count", 0)) + added_days
    step5.write_progress(dss_path, identity, last + timedelta(days=1), written)


def record_missing_days(dss_path: Path, days: list[date]) -> Path:
    """Add rows to the step 5 missing-days CSV, keeping any rows already there."""
    path = dss_path.with_suffix(".missing_days.csv")
    rows = {}
    if path.is_file():
        with path.open(newline="", encoding="utf-8") as stream:
            rows = {row["date"]: row["notes"] for row in csv.DictReader(stream)}
    for day in days:
        rows[day.isoformat()] = "No stable UA SWE file; no grid written"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date", "notes"])
        writer.writerows(sorted(rows.items()))
    return path


def extend(args: argparse.Namespace) -> None:
    dss_path = args.dss.resolve()
    existing = read_existing(dss_path)
    first_new = existing["last"] + timedelta(days=1)
    logger.info(f"DSS file:      {dss_path}")
    logger.info(f"Existing:      {len(existing['paths'])} grids, last date {existing['last']}")
    if args.end < first_new:
        logger.info(f"Nothing to add; the file already reaches {existing['last']}")
        return
    new_days = [first_new + timedelta(days=n) for n in range((args.end - first_new).days + 1)]
    logger.info(f"To add:        {first_new} to {args.end} ({len(new_days)} days)")

    files = local_stable_files(args.input_dir, new_days)
    absent = [day for day in new_days if day not in files]
    if absent and args.download:
        on_server = server_stable_names(absent)
        to_get = [day for day in absent if stable_name(day) in on_server]
        logger.info(f"Stable files:  {len(files)} local, {len(to_get)} to download, "
                    f"{len(absent) - len(to_get)} not stable on the server")
        if not args.dry_run:
            with StepTimer(f"Download {len(to_get)} stable files"):
                for day in to_get:
                    files[day] = download_stable(day, args.input_dir)
        else:
            files.update({day: args.input_dir / stable_name(day) for day in to_get})
    gaps = [day for day in new_days if day not in files]
    if gaps:
        logger.warning(f"{len(gaps)} days have no stable file and will have no grid: {gaps[0]} ... {gaps[-1]}")
    days = sorted(files)
    if not days:
        logger.info("No stable files to add")
        return

    # Settings from the existing file, so the new grids match the old ones
    cell_size = int(existing["cell_size"])
    options = step5.Options(
        args.input_dir.resolve(), args.boundary.resolve(), existing["part_b"].lower(),
        dss_path.parent, days[0], days[-1], "extension", cell_size,
        "near", existing["part_f"], args.vortex_home.resolve(), args.workers, args.dry_run,
    )
    progress = step5.progress_path(dss_path)
    if progress.is_file():
        identity = json.loads(progress.read_text(encoding="utf-8"))["identity"]
        options = replace(options, resampling=identity.get("resampling", "near"))
        if Path(identity.get("boundary", options.boundary)) != options.boundary:
            raise RuntimeError(f"The DSS file was made with {identity['boundary']}, not {options.boundary}")
    if step5.shg_part_a(cell_size) != existing["part_a"] or existing["units"].upper() != step5.DSS_UNITS:
        raise RuntimeError("The DSS A part or units do not match a step 5 file")
    if args.dry_run:
        logger.info(f"First new path: {step5.pathname(options, days[0])}")
        logger.info(f"Last new path:  {step5.pathname(options, days[-1])}")
        return

    # Check the Vortex grid and watershed mask against the last existing grid
    wkt = step5.source_wkt(files[days[0]])
    polygon = step5.boundary_in_shg(options.boundary)
    step5.start_vortex(options.vortex_home)
    geo = step5.geo_options(options)
    first = step5.read_and_project(files[days[0]], wkt, geo)
    signature = step5.grid_signature(first)
    lower_left = (round(first.originX() / cell_size), round((first.originY() + first.ny() * first.dy()) / cell_size))
    if (first.ny(), first.nx()) != existing["shape"] or lower_left != existing["lower_left"] \
            or first.dx() != existing["cell_size"]:
        raise RuntimeError("The new Vortex grid does not line up with the grids in the DSS file")
    mask = geometry_mask(
        [mapping(polygon)], out_shape=(first.ny(), first.nx()),
        transform=from_origin(first.originX(), first.originY(), first.dx(), abs(first.dy())),
        all_touched=True, invert=True,
    )
    if np.any(existing["valid"] & ~mask):
        raise RuntimeError("The boundary mask does not match the no-data cells of the existing grids")
    logger.info(f"Grid matches the DSS file: {first.nx()} by {first.ny()} cells, {int(mask.sum())} watershed cells")

    with StepTimer(f"Fingerprint {len(existing['paths'])} existing grids"):
        before = fingerprint(dss_path, existing["paths"])

    written = len(existing["paths"])
    with ThreadPoolExecutor(max_workers=options.workers) as pool:
        for batch in step5.month_batches(days):
            with StepTimer(f"{batch[0]:%Y-%m}"):
                projected = list(pool.map(lambda day: step5.read_and_project(files[day], wkt, geo), batch))
                grids = []
                for day, grid in zip(batch, projected):
                    if step5.grid_signature(grid) != signature:
                        raise RuntimeError(f"The Vortex grid geometry changed on {day}")
                    grids.append(step5.masked_grid(grid, mask))
                step5.write_batch(grids, dss_path, options)
                written += len(batch)
                logger.info(f"  Added through {batch[-1]} ({written} grids in file)")

    # Validate: old grids unchanged, new grids present and masked, Vortex reads the file
    import hecdss
    new_paths = {step5.pathname(options, day) for day in days}
    with hecdss.HecDss(str(dss_path)) as dss:
        paths = {str(value) for value in dss.get_catalog().uncondensed_paths}
        if paths != existing["paths"] | new_paths:
            raise RuntimeError(f"Unexpected catalog after the append: {len(paths)} grids, "
                               f"expected {len(existing['paths']) + len(new_paths)}")
        for day in (days[0], days[-1]):
            record = dss.get(step5.pathname(options, day))
            stored = np.flipud(np.asarray(record.data))
            if record.dataUnits.upper() != step5.DSS_UNITS or float(record.cellSize) != existing["cell_size"]:
                raise RuntimeError(f"New grid on {day} has the wrong units or cell size")
            if np.any(stored[~mask] > -1e30):
                raise RuntimeError(f"Cells outside the buffered watershed hold data on {day}")
    with StepTimer(f"Re-check {len(before)} existing grids"):
        after = fingerprint(dss_path, existing["paths"])
    changed = [path for path in before if before[path] != after[path]]
    if changed:
        raise RuntimeError(f"{len(changed)} existing grids changed, e.g. {changed[0]}")
    step5.validate_vortex(dss_path, step5.pathname(options, days[-1]))

    update_progress(dss_path, days[-1], written, len(days))
    if gaps:
        logger.info(f"Missing days listed in {record_missing_days(dss_path, gaps)}")
    logger.info(f"Added {len(days)} grids ({days[0]} to {days[-1]}); all {len(before)} existing grids "
                f"are unchanged; HEC-Vortex recognized {dss_path}")


def main():
    global logger
    args = parse_args()
    logger = setup_logging(log_dir=str(args.dss.resolve().parent))
    step5.logger = logger
    with StepTimer("Extend UA SWE DSS"):
        extend(args)


if __name__ == "__main__":
    main()
