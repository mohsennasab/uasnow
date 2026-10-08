"""
Step 5 — Build a period-of-record HEC-DSS file of daily UA SWE grids.

Standalone step. It is not called by run_pipeline.py.

Reads the original UA SWE netCDF files downloaded by step 1 and writes one
HEC-DSS file of daily SWE grids for a buffered watershed. HEC-Vortex does the
reading, clipping, reprojection to the Standard Hydrologic Grid (SHG),
resampling, and DSS writing, as Job Aid 5, Section 1.5 of the December 2025
FFRD SOP describes. Vortex clips to the boundary's envelope only, so every
cell that does not touch the buffered polygon is set to no-data before the
grid is written.

File and pathname conventions follow Job Aid 6 of the December 2025 FFRD SOP:

    ua_<basin>_por_swe_1day.dss
    /SHG1K/<BASIN>/SWE/<date>:2400//UA/

Usage:
    python 05_create_swe_dss.py --input_dir "G:/Python Package/uasnow/data/raw/All_NC_Files" \\
        --boundary "F:/PrismCopy/PRISM-explorer/aorc-por-generator/buffered-watershed/upper-tennessee_huc04_10km_buffer.shp" \\
        --basin upper-tennessee --output_dir "G:/Python Package/uasnow/data/dss"
    python 05_create_swe_dss.py ... --start 2010-01-01 --end 2010-01-31
    python 05_create_swe_dss.py ... --dry_run
"""

import argparse
import csv
import glob
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import geopandas as gpd
import netCDF4
import numpy as np
from pyproj import CRS
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from shapely.geometry import mapping

from config import DIR_DSS, DIR_RAW
from utils import StepTimer, parse_filename_date, parse_filename_status, setup_logging

logger = None  # set in main() so the log file is written next to the DSS

# ─────────────────────────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_VORTEX = Path(r"F:\PrismCopy\PRISM-explorer\vortex-0.14.1-win-x64\vortex-0.14.1")

# Standard Hydrologic Grid: USA Contiguous Albers Equal Area (NAD83), meters
SHG_WKT = CRS.from_proj4(
    "+proj=aea +lat_1=29.5 +lat_2=45.5 +lat_0=23 +lon_0=-96 "
    "+x_0=0 +y_0=0 +datum=NAD83 +units=m +no_defs"
).to_wkt(version="WKT1_ESRI")

VARIABLE = "SWE"
DATA_TYPE = "swe"            # SOP Job Aid 6, Table 1 data-type
DATA_INTERVAL = "1day"       # SOP Job Aid 6, Table 1 data-interval
DSS_UNITS = "MM"
DSS_DATA_TYPE = "INST-VAL"   # SWE is a state, not an accumulation
RESAMPLING = {"near": "Nearest Neighbor", "bilinear": "Bilinear", "average": "Average"}
UA_FILE_PATTERN = "UA_SWE_Depth_800m_v1_*.nc"


@dataclass(frozen=True)
class Options:
    input_dir: Path
    boundary: Path
    basin: str
    output_dir: Path
    start: date | None
    end: date | None
    event_name: str
    cell_size: int
    resampling: str
    source: str
    vortex_home: Path
    workers: int
    dry_run: bool


# ─────────────────────────────────────────────────────────────────────────────
# Naming (FFRD SOP Dec 2025, Job Aid 6)
# ─────────────────────────────────────────────────────────────────────────────

def shg_part_a(cell_size: int) -> str:
    """DSS A part for an SHG grid, e.g. 1000 -> SHG1K, 500 -> SHG500M."""
    if cell_size % 1000 == 0:
        return f"SHG{cell_size // 1000}K"
    return f"SHG{cell_size}M"


def dss_filename(options: Options) -> str:
    """ua _ basin-name _ event-name _ data-type _ data-interval .dss"""
    return f"{options.source.lower()}_{options.basin}_{options.event_name}_{DATA_TYPE}_{DATA_INTERVAL}.dss"


def hec_time(value: datetime) -> str:
    """HEC-DSS D part. Midnight is written as 2400 of the previous day."""
    if value.hour == 0 and value.minute == 0:
        return (value - timedelta(days=1)).strftime("%d%b%Y:2400").upper()
    return value.strftime("%d%b%Y:%H%M").upper()


def pathname(options: Options, day: date) -> str:
    stamp = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return (f"/{shg_part_a(options.cell_size)}/{options.basin.upper()}/{VARIABLE}/"
            f"{hec_time(stamp)}//{options.source.upper()}/")


# ─────────────────────────────────────────────────────────────────────────────
# Arguments
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> Options:
    parser = argparse.ArgumentParser(
        description="Create a period-of-record HEC-DSS file of daily UA SWE grids "
                    "for a buffered watershed using HEC-Vortex."
    )
    parser.add_argument("--input_dir", type=Path, default=Path(DIR_RAW),
                        help="Folder with the UA SWE netCDF files from step 1 (searched recursively)")
    parser.add_argument("--boundary", type=Path, required=True,
                        help="Buffered watershed polygon (shapefile with a .prj)")
    parser.add_argument("--basin", type=str,
                        help="Lowercase SOP basin name, e.g. upper-tennessee "
                             "(default: boundary file name)")
    parser.add_argument("--output_dir", type=Path, default=Path(DIR_DSS))
    parser.add_argument("--start", type=date.fromisoformat,
                        help="First date to include, YYYY-MM-DD (default: first file)")
    parser.add_argument("--end", type=date.fromisoformat,
                        help="Last date to include, YYYY-MM-DD (default: last file)")
    parser.add_argument("--event_name", type=str,
                        help="SOP event-name field of the file name "
                             "(default: 'por', or <start>to<end> when dates are given)")
    parser.add_argument("--cell_size", type=int, default=1000,
                        help="Output SHG cell size in meters (default: 1000)")
    parser.add_argument("--resampling", choices=sorted(RESAMPLING), default="near",
                        help="HEC-Vortex resampling method (default: near)")
    parser.add_argument("--source", type=str, default="UA",
                        help="Data source used in the file name and DSS F part")
    parser.add_argument("--vortex_home", type=Path, default=DEFAULT_VORTEX)
    parser.add_argument("--workers", type=int, default=6,
                        help="Files read and processed by HEC-Vortex at the same time")
    parser.add_argument("--dry_run", action="store_true",
                        help="Report the inputs, grid, and output names without writing")
    args = parser.parse_args()

    basin = args.basin or args.boundary.stem.lower().replace("_", "-")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", basin):
        parser.error("--basin must use lowercase letters, digits, and hyphens")
    if len(basin) > 32:
        parser.error("--basin must be at most 32 characters for the DSS B part")
    if not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", args.source):
        parser.error("--source must use letters, digits, and hyphens")
    if not args.boundary.is_file():
        parser.error(f"Boundary not found: {args.boundary}")
    if not args.input_dir.is_dir():
        parser.error(f"Input folder not found: {args.input_dir}")
    if args.start and args.end and args.end < args.start:
        parser.error("--end must not be before --start")
    if args.cell_size <= 0 or args.workers < 1:
        parser.error("--cell_size and --workers must be positive")

    if args.event_name:
        event_name = args.event_name
    elif args.start or args.end:
        first = args.start.strftime("%Y%m%d") if args.start else "first"
        last = args.end.strftime("%Y%m%d") if args.end else "last"
        event_name = f"{first}to{last}"
    else:
        event_name = "por"
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", event_name):
        parser.error("--event_name must use lowercase letters, digits, and hyphens")

    return Options(
        args.input_dir.resolve(), args.boundary.resolve(), basin,
        args.output_dir.resolve(), args.start, args.end, event_name,
        args.cell_size, args.resampling, args.source,
        args.vortex_home.resolve(), args.workers, args.dry_run,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Inputs
# ─────────────────────────────────────────────────────────────────────────────

def list_inputs(options: Options) -> dict[date, Path]:
    """Return {date: netCDF path} for the requested period, one file per day."""
    files: dict[date, Path] = {}
    rank = {"stable": 3, "provisional": 2, "early": 1, None: 0}
    for path in sorted(options.input_dir.rglob(UA_FILE_PATTERN)):
        day = parse_filename_date(path.name)
        if day is None:
            continue
        if (options.start and day < options.start) or (options.end and day > options.end):
            continue
        # Prefer the most final release when more than one status exists for a day
        if day in files and rank[parse_filename_status(files[day].name)] >= rank[parse_filename_status(path.name)]:
            continue
        files[day] = path
    if not files:
        raise FileNotFoundError(f"No {UA_FILE_PATTERN} files for the period in {options.input_dir}")
    return dict(sorted(files.items()))


def missing_days(files: dict[date, Path], options: Options) -> list[date]:
    first = options.start or min(files)
    last = options.end or max(files)
    expected = (first + timedelta(days=n) for n in range((last - first).days + 1))
    return [day for day in expected if day not in files]


def source_wkt(path: Path) -> str:
    """The UA files store NAD83 in the crs variable. Vortex would assume a sphere."""
    with netCDF4.Dataset(path) as ds:
        return str(ds.variables["crs"].spatial_ref)


def boundary_in_shg(path: Path):
    frame = gpd.read_file(path)
    if frame.empty or frame.crs is None:
        raise ValueError("The boundary needs geometry and a .prj file")
    shapes = frame.to_crs(SHG_WKT).geometry
    if (~shapes.is_valid).any():
        shapes = shapes.copy()
        shapes.loc[~shapes.is_valid] = shapes.loc[~shapes.is_valid].buffer(0)
    geometry = shapes.union_all()
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError("The boundary geometry must be valid and nonempty")
    return geometry


# ─────────────────────────────────────────────────────────────────────────────
# HEC-Vortex
# ─────────────────────────────────────────────────────────────────────────────

def start_vortex(vortex_home: Path) -> None:
    """Start the JVM bundled with HEC-Vortex and put its native libraries on PATH."""
    import jpype
    import jpype.imports  # noqa: F401

    if jpype.isJVMStarted():
        return
    if not vortex_home.is_dir():
        raise FileNotFoundError(f"HEC-Vortex not found at {vortex_home}")
    native_dirs = [
        vortex_home / "bin", vortex_home / "bin" / "gdal",
        vortex_home / "bin" / "netcdf", vortex_home / "bin" / "hdf",
        vortex_home / "jre" / "bin",
    ]
    native_dirs = [item for item in native_dirs if item.is_dir()]
    os.environ["PATH"] = os.pathsep.join(
        [str(item) for item in native_dirs] + [os.environ.get("PATH", "")]
    )
    for item in native_dirs:
        os.add_dll_directory(str(item))
    os.environ["GDAL_DATA"] = str(vortex_home / "bin" / "gdal" / "gdal-data")
    os.environ["PROJ_LIB"] = str(vortex_home / "bin" / "gdal" / "projlib")
    jpype.startJVM(
        str(vortex_home / "jre" / "bin" / "server" / "jvm.dll"),
        "-Xmx16g", "-Djava.awt.headless=true",
        "-Djava.library.path=" + os.pathsep.join(str(item) for item in native_dirs),
        classpath=glob.glob(str(vortex_home / "lib" / "*.jar")),
    )


def java_map(values: dict):
    from java.util import HashMap
    result = HashMap()
    for key, value in values.items():
        result.put(key, value)
    return result


def geo_options(options: Options):
    """Vortex clip (boundary envelope), reprojection, and resampling options."""
    return java_map({
        "pathToShp": str(options.boundary),
        "targetCellSize": str(options.cell_size),
        "targetCellSizeUnits": "Meters",
        "targetWkt": SHG_WKT,
        "resamplingMethod": RESAMPLING[options.resampling],
    })


def write_options(options: Options):
    return java_map({
        "partA": shg_part_a(options.cell_size),
        "partB": options.basin.upper(),
        "partC": VARIABLE,
        "partF": options.source.upper(),
        "dataType": DSS_DATA_TYPE,
        "units": DSS_UNITS,
    })


def read_and_project(path: Path, wkt: str, geo):
    """Read one UA file with Vortex, then clip, reproject, and resample it."""
    from mil.army.usace.hec.vortex import VortexGrid
    from mil.army.usace.hec.vortex.geo import GeographicProcessor
    from mil.army.usace.hec.vortex.io import DataReader

    grids = DataReader.builder().path(str(path)).variable(VARIABLE).build().getDtos()
    if grids.size() != 1:
        raise RuntimeError(f"Expected one {VARIABLE} grid in {path.name}, found {grids.size()}")
    grid = grids.get(0)
    if "millimeter" not in str(grid.units()).lower():
        raise RuntimeError(f"Unexpected {VARIABLE} units {grid.units()!r} in {path.name}")
    grid = VortexGrid.toBuilder(grid).wkt(wkt).build()
    return GeographicProcessor(geo).process(grid)


def masked_grid(grid, mask: np.ndarray):
    """Return a Vortex grid in mm with no-data outside the buffered watershed."""
    from jpype import JArray, JFloat
    from mil.army.usace.hec.vortex import VortexGrid

    values = np.array(grid.data(), dtype=np.float32).reshape(grid.ny(), grid.nx())
    values[values == np.float32(grid.noDataValue())] = np.nan
    values[~np.isfinite(values)] = np.nan
    values[~mask] = np.nan
    return VortexGrid.toBuilder(grid).data(JArray(JFloat)(values.ravel())).units(DSS_UNITS).build()


def grid_signature(grid) -> tuple:
    return (round(grid.originX(), 3), round(grid.originY(), 3), round(grid.dx(), 3),
            round(grid.dy(), 3), grid.nx(), grid.ny())


def write_batch(grids: list, destination: Path, options: Options) -> None:
    from java.util import ArrayList
    from mil.army.usace.hec.vortex.io import DataWriter

    batch = ArrayList()
    for grid in grids:
        batch.add(grid)
    DataWriter.builder().data(batch).destination(str(destination)) \
        .options(write_options(options)).build().write()


# ─────────────────────────────────────────────────────────────────────────────
# Progress and validation
# ─────────────────────────────────────────────────────────────────────────────

def progress_path(output: Path) -> Path:
    return output.with_suffix(".progress.json")


def read_progress(output: Path, identity: dict) -> date | None:
    marker = progress_path(output)
    if not output.is_file() or not marker.is_file():
        return None
    saved = json.loads(marker.read_text(encoding="utf-8"))
    if saved.get("identity") != identity:
        raise RuntimeError(f"{output} was made with different inputs. Choose another output folder.")
    return date.fromisoformat(saved["next_date"])


def write_progress(output: Path, identity: dict, next_date: date, count: int) -> None:
    payload = {"identity": identity, "next_date": next_date.isoformat(), "written_records": count}
    marker = progress_path(output)
    temporary = marker.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(marker)


def write_missing_days(output: Path, days: list[date]) -> Path:
    path = output.with_suffix(".missing_days.csv")
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date", "notes"])
        for day in days:
            writer.writerow([day.isoformat(), "No UA SWE file in the input folder; no grid written"])
    return path


def validate_dss(output: Path, options: Options, days: list[date], mask: np.ndarray) -> None:
    """Check the record count, end records, units, cell size, and polygon mask."""
    import hecdss

    with hecdss.HecDss(str(output)) as dss:
        paths = {str(value) for value in dss.get_catalog().uncondensed_paths}
        expected = {pathname(options, day) for day in days}
        if paths != expected:
            raise RuntimeError(
                f"{output} has {len(paths)} grids; expected {len(expected)}. "
                f"Missing {len(expected - paths)}, unexpected {len(paths - expected)}."
            )
        for day in (days[0], days[-1]):
            record = dss.get(pathname(options, day))
            if record is None or record.dataUnits.upper() != DSS_UNITS:
                raise RuntimeError(f"DSS units or grid data are invalid on {day}")
            if float(record.cellSize) != float(options.cell_size):
                raise RuntimeError(f"DSS cell size is not {options.cell_size} m on {day}")
            stored = np.flipud(np.asarray(record.data))  # DSS stores rows south to north
            if stored.shape != mask.shape:
                raise RuntimeError(f"DSS grid shape {stored.shape} differs from the mask on {day}")
            if np.any(stored[~mask] > -1e30):
                raise RuntimeError(f"Cells outside the buffered watershed hold data on {day}")


def validate_vortex(output: Path, expected_path: str) -> None:
    """Read the finished file back with HEC-Vortex."""
    from mil.army.usace.hec.vortex.io import DataReader

    variables = DataReader.getVariables(str(output))
    if variables.isEmpty() or not any(expected_path.upper() == str(item).upper() for item in variables):
        raise RuntimeError(f"HEC-Vortex cannot find {expected_path} in {output}")
    grid = DataReader.builder().path(str(output)).variable(expected_path).build().getDtos().get(0)
    if str(grid.dataType()) != "INSTANTANEOUS":
        raise RuntimeError(f"HEC-Vortex reads {expected_path} as {grid.dataType()}, not instantaneous")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def month_batches(days: list[date]):
    batch: list[date] = []
    for day in days:
        if batch and (day.year, day.month) != (batch[0].year, batch[0].month):
            yield batch
            batch = []
        batch.append(day)
    if batch:
        yield batch


def build_dss(options: Options) -> Path:
    files = list_inputs(options)
    days = list(files)
    gaps = missing_days(files, options)
    output = options.output_dir / dss_filename(options)

    logger.info(f"Input folder:  {options.input_dir}")
    logger.info(f"Boundary:      {options.boundary}")
    logger.info(f"Period:        {days[0]} to {days[-1]} ({len(days)} daily files)")
    logger.info(f"Output:        {output}")
    logger.info(f"First path:    {pathname(options, days[0])}")
    logger.info(f"Grid:          {shg_part_a(options.cell_size)} ({options.cell_size} m), "
                f"{RESAMPLING[options.resampling]} resampling, no-data outside the buffered watershed")
    if gaps:
        logger.warning(f"{len(gaps)} days in the period have no input file and will have no grid: "
                       f"{gaps[0]} ... {gaps[-1]}")

    wkt = source_wkt(files[days[0]])
    polygon = boundary_in_shg(options.boundary)
    start_vortex(options.vortex_home)
    geo = geo_options(options)

    # Build the watershed mask from the grid that Vortex produces for the first day
    first = read_and_project(files[days[0]], wkt, geo)
    signature = grid_signature(first)
    mask = geometry_mask(
        [mapping(polygon)], out_shape=(first.ny(), first.nx()),
        transform=from_origin(first.originX(), first.originY(), first.dx(), abs(first.dy())),
        all_touched=True, invert=True,
    )
    if not mask.any():
        raise ValueError("The buffered watershed does not touch the Vortex grid")
    logger.info(f"Masked grid is {first.nx()} by {first.ny()} cells, with {int(mask.sum())} "
                f"watershed cells and {int((~mask).sum())} no-data cells")

    if options.dry_run:
        return output

    options.output_dir.mkdir(parents=True, exist_ok=True)
    identity = {
        "input_dir": str(options.input_dir),
        "boundary": str(options.boundary),
        "boundary_mtime_ns": options.boundary.stat().st_mtime_ns,
        "basin": options.basin, "first_date": days[0].isoformat(), "last_date": days[-1].isoformat(),
        "file_count": len(days), "cell_size_m": options.cell_size,
        "resampling": options.resampling, "source": options.source,
        "polygon_mask": "all_touched",
    }
    next_day = read_progress(output, identity)
    if output.is_file() and next_day is None:
        raise FileExistsError(f"{output} exists without a matching progress file. Choose a new output folder.")
    remaining = [day for day in days if next_day is None or day >= next_day]
    written = len(days) - len(remaining)
    if remaining:
        logger.info(f"Writing {len(remaining)} grids" + (f", resuming at {remaining[0]}" if written else ""))

    with ThreadPoolExecutor(max_workers=options.workers) as pool:
        for batch in month_batches(remaining):
            with StepTimer(f"{batch[0]:%Y-%m}"):
                projected = list(pool.map(lambda day: read_and_project(files[day], wkt, geo), batch))
                grids = []
                for day, grid in zip(batch, projected):
                    if grid_signature(grid) != signature:
                        raise RuntimeError(f"The Vortex grid geometry changed on {day}")
                    grids.append(masked_grid(grid, mask))
                write_batch(grids, output, options)
                written += len(batch)
                write_progress(output, identity, batch[-1] + timedelta(days=1), written)
                logger.info(f"  Completed through {batch[-1]} ({written}/{len(days)} grids)")

    if gaps:
        logger.info(f"Missing days listed in {write_missing_days(output, gaps)}")
    validate_dss(output, options, days, mask)
    validate_vortex(output, pathname(options, days[-1]))
    logger.info(f"Validated {len(days)} grids; HEC-Vortex recognized {output}")
    return output


def main():
    global logger
    options = parse_args()
    logger = setup_logging(log_dir=str(options.output_dir))
    with StepTimer("Create UA SWE DSS"):
        build_dss(options)


if __name__ == "__main__":
    main()
