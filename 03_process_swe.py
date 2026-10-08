"""
Step 3 — Process clipped SWE GeoTIFFs: aggregate, compute basin-mean time series.

This script reads the clipped GeoTIFFs (one per day), builds a time series
of basin-mean SWE, computes unit conversions, and exports:
  - A CSV of daily basin-mean SWE in the selected unit system
  - Optional monthly / seasonal aggregated GeoTIFFs

Important: The --units flag here must match what was used in Step 2
(clip_to_watershed). If GeoTIFFs were written in inches, pass --units inches
so the CSV columns and labels are correct. If GeoTIFFs are in mm (default),
pass --units mm.

Usage:
    python 03_process_swe.py --wy_start 2020 --wy_end 2020
    python 03_process_swe.py --wy_start 2020 --wy_end 2020 --units inches
    python 03_process_swe.py --wy_start 2020 --wy_end 2020 --monthly
"""

import os
import glob
import argparse
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd
import rioxarray  # noqa: F401
import xarray as xr

from config import (
    DIR_CLIPPED,
    DIR_PROCESSED,
    NODATA_VALUE,
    DEFAULT_UNITS,
    unit_label,
)
from utils import setup_logging, ensure_dir, parse_filename_date, StepTimer

logger = setup_logging()


def _read_single_tif(f: str, nodata: float) -> tuple | None:
    """
    Worker function to read a single GeoTIFF and return (date, DataArray).
    Returns None if the date cannot be parsed.
    """
    d = parse_filename_date(os.path.basename(f))
    if d is None:
        return None
    da = rioxarray.open_rasterio(f).squeeze("band", drop=True)
    da = da.where(da != nodata)
    return (np.datetime64(d), da)


def read_geotiffs_as_timeseries(tif_dir: str, variable: str = "SWE",
                                max_workers: int | None = None) -> xr.DataArray:
    """
    Read all GeoTIFFs in a directory, extract dates from filenames, and
    stack into a time-dimensioned DataArray.

    The returned values are in whatever unit the GeoTIFFs were written in
    (mm by default, or inches if --units inches was used in Step 2).

    Parameters
    ----------
    tif_dir : str
        Directory containing GeoTIFF files.
    variable : str
        Variable name (used for filtering filenames).
    max_workers : int, optional
        Number of parallel workers for reading files.

    Returns
    -------
    xr.DataArray with dims (time, y, x), or None.
    """
    tif_files = sorted(glob.glob(os.path.join(tif_dir, f"*{variable}*.tif")))
    if not tif_files:
        return None

    n_workers = max_workers or max(1, multiprocessing.cpu_count() - 1)

    arrays = []
    dates = []

    if n_workers > 1 and len(tif_files) > 1:
        with ProcessPoolExecutor(max_workers=n_workers) as executor:
            results = list(executor.map(
                _read_single_tif, tif_files,
                [NODATA_VALUE] * len(tif_files),
            ))
        for r in results:
            if r is None:
                continue
            dates.append(r[0])
            arrays.append(r[1])
    else:
        for f in tif_files:
            r = _read_single_tif(f, NODATA_VALUE)
            if r is None:
                logger.warning(f"  Cannot parse date from {os.path.basename(f)}, skipping")
                continue
            dates.append(r[0])
            arrays.append(r[1])

    if not arrays:
        return None

    stacked = xr.concat(arrays, dim="time")
    stacked["time"] = dates
    stacked.name = variable
    return stacked


def compute_basin_mean(da: xr.DataArray, units: str = "mm") -> pd.DataFrame:
    """
    Compute spatially-averaged (basin-mean) values for each time step.

    Parameters
    ----------
    da : xr.DataArray
        Time-stacked raster (values already in the target unit system).
    units : str
        Unit system ('mm' or 'inches') — used for column naming.

    Returns
    -------
    DataFrame with columns: date, {variable}_{units}
    """
    variable = da.name or "SWE"
    mean_vals = da.mean(dim=["y", "x"]).values
    times = pd.to_datetime(da["time"].values)

    col = f"{variable}_{units}"
    rounding = 2 if units == "mm" else 3

    df = pd.DataFrame({
        "date": times,
        col: np.round(mean_vals, rounding),
    })
    df = df.sort_values("date").reset_index(drop=True)
    return df


def compute_monthly_mean(da: xr.DataArray) -> xr.DataArray:
    """Resample daily data to monthly mean."""
    monthly = da.resample(time="MS").mean()
    monthly.name = da.name
    monthly.attrs = da.attrs.copy()
    monthly.attrs["temporal_resolution"] = "monthly_mean"
    return monthly


def process_water_years(wy_start: int, wy_end: int,
                        variable: str = "SWE",
                        units: str = DEFAULT_UNITS,
                        compute_monthly: bool = False,
                        max_workers: int | None = None) -> None:
    """
    Process all clipped GeoTIFFs across the requested water years.

    Parameters
    ----------
    wy_start, wy_end : int
        Range of water years.
    variable : str
        Variable name to process.
    units : str
        Unit system of the GeoTIFFs ('mm' or 'inches').
    compute_monthly : bool
        If True, also export monthly-mean GeoTIFFs.
    max_workers : int, optional
        Number of parallel workers for reading GeoTIFFs.
    """
    out_dir = ensure_dir(DIR_PROCESSED)
    all_dfs = []

    for wy in range(wy_start, wy_end + 1):
        tif_dir = os.path.join(DIR_CLIPPED, f"WY{wy}")
        if not os.path.isdir(tif_dir):
            logger.warning(f"  No clipped directory for WY{wy}, skipping")
            continue

        with StepTimer(f"Process WY{wy}"):
            logger.info(f"Processing WY{wy}...")
            da = read_geotiffs_as_timeseries(tif_dir, variable, max_workers)
            if da is None:
                logger.warning(f"  No valid GeoTIFFs in WY{wy}")
                continue

            # Basin-mean time series
            df = compute_basin_mean(da, units)
            csv_path = os.path.join(out_dir, f"basin_mean_{variable}_WY{wy}.csv")
            df.to_csv(csv_path, index=False)
            logger.info(f"  Saved basin-mean CSV: {csv_path} "
                        f"({len(df)} days, units={units})")
            all_dfs.append(df)

            # Monthly aggregation
            if compute_monthly:
                monthly = compute_monthly_mean(da)
                for t in monthly.time.values:
                    ts = pd.Timestamp(t)
                    month_str = ts.strftime("%Y%m")
                    tif_path = os.path.join(out_dir,
                        f"UA_SWE_800m_{variable}_monthly_{month_str}.tif")
                    monthly.sel(time=t).rio.to_raster(tif_path, compress="LZW",
                                                       dtype="float32")
                logger.info(f"  Saved {len(monthly.time)} monthly GeoTIFFs")

    # Combined multi-year CSV
    if all_dfs:
        combined = pd.concat(all_dfs, ignore_index=True).sort_values("date")
        combined_path = os.path.join(out_dir,
            f"basin_mean_{variable}_WY{wy_start}-{wy_end}.csv")
        combined.to_csv(combined_path, index=False)
        logger.info(f"Saved combined CSV: {combined_path} ({len(combined)} records)")


def main():
    parser = argparse.ArgumentParser(
        description="Process clipped SWE GeoTIFFs: basin-mean CSV + optional monthly."
    )
    parser.add_argument("--wy_start", type=int, required=True,
                        help="First water year")
    parser.add_argument("--wy_end", type=int, required=True,
                        help="Last water year")
    parser.add_argument("--variable", type=str, default="SWE",
                        choices=["SWE", "DEPTH"],
                        help="Variable to process (default: SWE)")
    parser.add_argument("--units", type=str, default=DEFAULT_UNITS,
                        choices=["mm", "inches"],
                        help=f"Unit system of the GeoTIFFs (default: {DEFAULT_UNITS})")
    parser.add_argument("--monthly", action="store_true",
                        help="Also export monthly-mean GeoTIFFs")
    parser.add_argument("--workers", type=int, default=None,
                        help="Number of parallel workers (default: CPU count - 1)")
    args = parser.parse_args()

    logger.info("=" * 70)
    logger.info(f"UA SWE Processing — WY{args.wy_start} to WY{args.wy_end}")
    logger.info(f"  Units  : {args.units}")
    logger.info(f"  Workers: {args.workers or 'auto'}")
    logger.info("=" * 70)

    process_water_years(args.wy_start, args.wy_end,
                        variable=args.variable,
                        units=args.units,
                        compute_monthly=args.monthly,
                        max_workers=args.workers)

    logger.info("Processing complete.")


if __name__ == "__main__":
    main()
