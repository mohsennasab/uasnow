"""
Step 6 — Build one HEC-DSS file of daily UA SWE grids per calibration event.

Standalone step. It is not called by run_pipeline.py.

Reads a list of calibration events (start dates), adds a time buffer around
each one, and writes a separate DSS file for every event that has snow in the
buffered watershed. The grids are made exactly as in step 5: HEC-Vortex reads
the original UA SWE netCDF files, clips to the buffered watershed envelope,
reprojects to the Standard Hydrologic Grid (SHG), resamples, and writes DSS;
every cell that does not touch the buffered polygon is set to no-data. The
step 5 functions are imported and reused unchanged.

Buffers follow the calibration events explorer:
    time:  event start date - 10 days to event start date + 10 days (inclusive)
    space: the 10 km buffered watershed passed with --boundary

File name and pathnames:

    <source>_swe_<mmm-yyyy>.dss          e.g. ua_swe_feb-2020.dss
    /SHG1K/<BASIN>/SWE/<date>:2400//UA/  (same as step 5)

Usage:
    python 06_create_event_swe_dss.py --events "F:/Met_Tools/calibration_events_explorer/Events.csv" \\
        --input_dir "G:/Python Package/uasnow/data/raw/All_NC_Files" \\
        --boundary "watersheds/upper-tennessee_huc04_10km_buffer/upper-tennessee_huc04_10km_buffer.shp" \\
        --basin upper-tennessee --output_dir "data/dss/events"
    python 06_create_event_swe_dss.py ... --dry_run
    python 06_create_event_swe_dss.py ... --include_no_snow
"""

import argparse
import csv
import importlib.util
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from shapely.geometry import mapping

from config import DIR_DSS, DIR_RAW
from utils import StepTimer, setup_logging

logger = None  # set in main() so the log file is written next to the DSS files

DATE_COLUMNS = ["start date", "date", "storm date", "start_date"]
NAME_COLUMNS = ["name", "event name", "storm name", "event"]
SUMMARY_NAME = "event_swe_dss_summary.csv"


def load_step5():
    """Import 05_create_swe_dss.py (its file name is not a valid module name)."""
    path = Path(__file__).with_name("05_create_swe_dss.py")
    spec = importlib.util.spec_from_file_location("create_swe_dss", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


step5 = load_step5()


@dataclass(frozen=True)
class Event:
    label: str
    start: date
    first_day: date
    last_day: date
    event_name: str  # file name field, e.g. feb-2020

    @property
    def days(self) -> list[date]:
        return [self.first_day + timedelta(days=n) for n in range((self.last_day - self.first_day).days + 1)]


# ─────────────────────────────────────────────────────────────────────────────
# Arguments and events
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create one HEC-DSS file of daily UA SWE grids for each calibration event "
                    "that has snow in the buffered watershed."
    )
    parser.add_argument("--events", type=Path, required=True,
                        help="CSV or Excel file with a start date column (Start Date, Date, ...) "
                             "and an optional Name column")
    parser.add_argument("--input_dir", type=Path, default=Path(DIR_RAW),
                        help="Folder with the UA SWE netCDF files from step 1 (searched recursively)")
    parser.add_argument("--boundary", type=Path, required=True,
                        help="Buffered watershed polygon (shapefile with a .prj)")
    parser.add_argument("--basin", type=str,
                        help="Lowercase SOP basin name for the DSS B part, e.g. upper-tennessee "
                             "(default: boundary file name)")
    parser.add_argument("--output_dir", type=Path, default=Path(DIR_DSS) / "events")
    parser.add_argument("--buffer_days_before", type=int, default=10,
                        help="Days before each event start date (default: 10)")
    parser.add_argument("--buffer_days_after", type=int, default=10,
                        help="Days after each event start date (default: 10)")
    parser.add_argument("--include_no_snow", action="store_true",
                        help="Also write events with no SWE in the buffered watershed")
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace event DSS files that already exist")
    parser.add_argument("--cell_size", type=int, default=1000,
                        help="Output SHG cell size in meters (default: 1000)")
    parser.add_argument("--resampling", choices=sorted(step5.RESAMPLING), default="near",
                        help="HEC-Vortex resampling method (default: near)")
    parser.add_argument("--source", type=str, default="UA",
                        help="Data source used in the file name and DSS F part")
    parser.add_argument("--vortex_home", type=Path, default=step5.DEFAULT_VORTEX)
    parser.add_argument("--workers", type=int, default=6,
                        help="Files read and processed by HEC-Vortex at the same time")
    parser.add_argument("--dry_run", action="store_true",
                        help="Report the events, windows, inputs, and file names without writing")
    args = parser.parse_args()

    args.basin = args.basin or args.boundary.stem.lower().replace("_", "-")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", args.basin) or len(args.basin) > 32:
        parser.error("--basin must use lowercase letters, digits, and hyphens (at most 32 characters)")
    if not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", args.source):
        parser.error("--source must use letters, digits, and hyphens")
    for path, label in ((args.events, "Events file"), (args.boundary, "Boundary")):
        if not path.is_file():
            parser.error(f"{label} not found: {path}")
    if not args.input_dir.is_dir():
        parser.error(f"Input folder not found: {args.input_dir}")
    if args.buffer_days_before < 0 or args.buffer_days_after < 0:
        parser.error("Buffer days must be zero or positive")
    if args.cell_size <= 0 or args.workers < 1:
        parser.error("--cell_size and --workers must be positive")
    return args


def find_column(columns, candidates):
    lowered = {str(c).lower().strip(): c for c in columns}
    return next((lowered[c] for c in candidates if c in lowered), None)


def load_events(path: Path, before: int, after: int) -> list[Event]:
    """Read the events file and build the buffered window and file name for each event."""
    frame = pd.read_excel(path) if path.suffix.lower() in (".xlsx", ".xls") else pd.read_csv(path)
    date_column = find_column(frame.columns, DATE_COLUMNS)
    if date_column is None:
        raise ValueError(f"No start date column in {path}. Expected one of {DATE_COLUMNS}; "
                         f"found {list(frame.columns)}")
    name_column = find_column(frame.columns, NAME_COLUMNS)

    events, used, skipped = [], {}, []
    for line, row in enumerate(frame.to_dict("records"), start=2):
        start = pd.to_datetime(row[date_column], errors="coerce")
        if pd.isna(start):
            skipped.append(f"line {line}: {row[date_column]!r}")
            continue
        start = start.date()
        label = str(row[name_column]).strip() if name_column and not pd.isna(row.get(name_column)) \
            else f"{start:%b %Y} event"
        base = f"{start:%b}-{start:%Y}".lower()
        used[base] = used.get(base, 0) + 1
        event_name = base if used[base] == 1 else f"{base}-{used[base]}"
        events.append(Event(label, start, start - timedelta(days=before),
                            start + timedelta(days=after), event_name))
    if skipped:
        logger.warning(f"Skipped {len(skipped)} event row(s) with no readable date: {'; '.join(skipped)}")
    if not events:
        raise ValueError(f"No events with a readable start date in {path}")
    return events


def event_filename(source: str, event: Event) -> str:
    """<source>_swe_<mmm-yyyy>.dss"""
    return f"{source.lower()}_{step5.DATA_TYPE}_{event.event_name}.dss"


def step5_options(args, event: Event | None):
    """Step 5 Options for an event window (None: the whole input folder)."""
    return step5.Options(
        args.input_dir.resolve(), args.boundary.resolve(), args.basin, args.output_dir.resolve(),
        event.first_day if event else None, event.last_day if event else None,
        event.event_name if event else "events", args.cell_size, args.resampling, args.source,
        args.vortex_home.resolve(), args.workers, args.dry_run,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Grids
# ─────────────────────────────────────────────────────────────────────────────

def grid_values(grid, mask: np.ndarray) -> np.ndarray:
    """SWE values in mm inside the buffered watershed; NaN elsewhere."""
    values = np.array(grid.data(), dtype=np.float32).reshape(grid.ny(), grid.nx())
    values[values == np.float32(grid.noDataValue())] = np.nan
    values[~np.isfinite(values)] = np.nan
    values[~mask] = np.nan
    return values


def watershed_mask(grid, polygon) -> np.ndarray:
    return geometry_mask(
        [mapping(polygon)], out_shape=(grid.ny(), grid.nx()),
        transform=from_origin(grid.originX(), grid.originY(), grid.dx(), abs(grid.dy())),
        all_touched=True, invert=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def build_event(event, args, files, wkt, geo, grid_state, pool) -> dict:
    """Write one event DSS file, or report why it was skipped."""
    options = step5_options(args, event)
    output = options.output_dir / event_filename(args.source, event)
    days = [day for day in event.days if day in files]
    gaps = [day for day in event.days if day not in files]
    row = {
        "event": event.label, "event_name": event.event_name, "start_date": event.start.isoformat(),
        "window_start": event.first_day.isoformat(), "window_end": event.last_day.isoformat(),
        "days_in_window": len(event.days), "days_with_data": len(days), "missing_days": len(gaps),
        "max_swe_mm": "", "max_swe_date": "", "max_watershed_mean_swe_mm": "", "days_with_snow": "",
        "dss_file": "", "status": "",
    }
    logger.info(f"{event.label}: {event.first_day} to {event.last_day} -> {output.name}")
    if not days:
        row["status"] = "skipped: no UA SWE files in the window"
        logger.warning(f"  {row['status']}")
        return row
    if gaps:
        logger.warning(f"  {len(gaps)} day(s) in the window have no input file: {gaps[0]} ... {gaps[-1]}")
    if args.dry_run:
        row["status"] = "dry run"
        return row

    projected = list(pool.map(lambda day: step5.read_and_project(files[day], wkt, geo), days))
    if grid_state["mask"] is None:
        grid_state["mask"] = watershed_mask(projected[0], grid_state["polygon"])
        grid_state["signature"] = step5.grid_signature(projected[0])
        if not grid_state["mask"].any():
            raise ValueError("The buffered watershed does not touch the Vortex grid")
        logger.info(f"Masked grid is {projected[0].nx()} by {projected[0].ny()} cells, "
                    f"with {int(grid_state['mask'].sum())} watershed cells")
    mask = grid_state["mask"]
    for day, grid in zip(days, projected):
        if step5.grid_signature(grid) != grid_state["signature"]:
            raise RuntimeError(f"The Vortex grid geometry changed on {day}")

    values = [grid_values(grid, mask) for grid in projected]
    daily_max = [float(np.nanmax(v)) if np.isfinite(v).any() else 0.0 for v in values]
    daily_mean = [float(np.nanmean(v)) if np.isfinite(v).any() else 0.0 for v in values]
    peak = int(np.argmax(daily_max))
    row.update({
        "max_swe_mm": f"{daily_max[peak]:.1f}", "max_swe_date": days[peak].isoformat(),
        "max_watershed_mean_swe_mm": f"{max(daily_mean):.3f}",
        "days_with_snow": sum(1 for m in daily_max if m > 0),
    })
    logger.info(f"  Max SWE {daily_max[peak]:.1f} mm on {days[peak]}; "
                f"{row['days_with_snow']} of {len(days)} days with snow in the buffered watershed")

    if daily_max[peak] <= 0 and not args.include_no_snow:
        row["status"] = "skipped: no SWE in the buffered watershed"
        logger.info(f"  {row['status']}")
        return row

    if output.exists():
        if not args.overwrite:
            raise FileExistsError(f"{output} exists. Use --overwrite to replace it.")
        output.unlink()
        for sidecar in (output.with_suffix(".missing_days.csv"),):
            sidecar.unlink(missing_ok=True)

    options.output_dir.mkdir(parents=True, exist_ok=True)
    step5.write_batch([step5.masked_grid(grid, mask) for grid in projected], output, options)
    if gaps:
        logger.info(f"  Missing days listed in {step5.write_missing_days(output, gaps)}")
    step5.validate_dss(output, options, days, mask)
    step5.validate_vortex(output, step5.pathname(options, days[-1]))
    logger.info(f"  Validated {len(days)} grids; HEC-Vortex recognized {output.name}")
    row.update({"dss_file": output.name, "status": "written"})
    return row


def write_summary(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    global logger
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(log_dir=str(args.output_dir))
    step5.logger = logger

    with StepTimer("Create event UA SWE DSS files"):
        events = load_events(args.events, args.buffer_days_before, args.buffer_days_after)
        logger.info(f"Events file:   {args.events} ({len(events)} events)")
        logger.info(f"Input folder:  {args.input_dir}")
        logger.info(f"Boundary:      {args.boundary}")
        logger.info(f"Window:        start date -{args.buffer_days_before} to +{args.buffer_days_after} days")
        logger.info(f"Output folder: {args.output_dir}")

        files = step5.list_inputs(step5_options(args, None))
        logger.info(f"UA SWE files:  {len(files)}, {min(files)} to {max(files)}")

        wkt = geo = None
        grid_state = {"polygon": step5.boundary_in_shg(args.boundary), "mask": None, "signature": None}
        if not args.dry_run:
            wkt = step5.source_wkt(next(iter(files.values())))
            step5.start_vortex(args.vortex_home.resolve())
            geo = step5.geo_options(step5_options(args, None))

        rows = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for event in sorted(events, key=lambda item: item.start):
                with StepTimer(event.event_name):
                    rows.append(build_event(event, args, files, wkt, geo, grid_state, pool))

        summary = args.output_dir / SUMMARY_NAME
        if not args.dry_run:
            write_summary(summary, rows)
        written = [row for row in rows if row["status"] == "written"]
        logger.info(f"{len(written)} of {len(rows)} events written"
                    + ("" if args.dry_run else f"; summary in {summary}"))
        for row in rows:
            logger.info(f"  {row['event_name']:10s} {row['status']}"
                        + (f" (max SWE {row['max_swe_mm']} mm)" if row["max_swe_mm"] else ""))


if __name__ == "__main__":
    main()
