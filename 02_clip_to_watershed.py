"""
Step 2 — Clip UA SWE netCDF files to a watershed boundary and export GeoTIFFs.

For each downloaded netCDF, this script:
  1. Opens the netCDF and attaches the source CRS (NAD83 / EPSG:4269).
  2. Clips the raster to the watershed polygon geometry in the **native CRS**.
  3. Reprojects the *clipped* raster to the target Albers Equal Area Conic CRS.
  4. Optionally converts pixel values from mm to inches (--units inches).
  5. Writes a compressed GeoTIFF with LZW compression.
  6. Deletes the raw netCDF file to save disk space (configurable).

Processing order — Clip-then-Reproject:
  The toolkit deliberately clips first in the native geographic CRS and then
  reprojects the smaller clipped raster. This approach is both more efficient
  (fewer pixels to transform) and avoids the edge artifacts that can appear
  when reprojecting a large CONUS raster and then clipping. Because the
  bilinear (default) resampling operates on the clipped extent, the
  interpolated pixel values along watershed edges are computed only from
  neighboring source pixels that are relevant to the basin — not from distant
  CONUS pixels that would be discarded anyway. For SWE/DEPTH data stored in
  mm, the resampling produces a smooth spatial interpolation; it does not
  alter the measurement unit or introduce systematic bias.

  If nearest-neighbor resampling is selected, pixel values are preserved
  exactly (no interpolation), at the cost of a blockier output grid.

Supported watershed boundary formats:
  Shapefile (.shp), GeoPackage (.gpkg), GeoJSON (.geojson / .json),
  and any other vector format readable by geopandas / Fiona.

Usage:
    python 02_clip_to_watershed.py --watershed watershed.shp --wy_start 2020 --wy_end 2020
    python 02_clip_to_watershed.py --watershed basin.gpkg --wy_start 2020 --wy_end 2020 --units inches
    python 02_clip_to_watershed.py --watershed boundary.shp --variable DEPTH --keep_raw
"""

import os
import glob
import argparse
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed
import numpy as np
import xarray as xr
import geopandas as gpd
import rioxarray  # noqa: F401 — registers .rio accessor
from rasterio.enums import Resampling
from pyproj import CRS

from config import (
    SOURCE_CRS,
    TARGET_CRS_WKT,
    DIR_RAW,
    DIR_CLIPPED,
    DELETE_RAW_NETCDF,
    NODATA_VALUE,
    RESAMPLING_METHOD,
    DEFAULT_UNITS,
    unit_conversion_factor,
)
from utils import setup_logging, ensure_dir, build_geotiff_name, StepTimer

logger = setup_logging()

# Map string names → rasterio Resampling enums
RESAMPLING_MAP = {
    "nearest": Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "cubic": Resampling.cubic,
}


def load_watershed(shp_path: str) -> gpd.GeoDataFrame:
    """
    Load a watershed boundary and ensure it is in the source CRS.

    Supported formats: Shapefile (.shp), GeoPackage (.gpkg),
    GeoJSON (.geojson/.json), and any Fiona-readable vector format.
    """
    gdf = gpd.read_file(shp_path)
    gdf = gdf.to_crs(SOURCE_CRS)
    logger.info(f"Watershed loaded: {shp_path} ({len(gdf)} feature(s))")
    return gdf


def clip_and_reproject(nc_path: str, gdf: gpd.GeoDataFrame,
                       out_path: str, variable: str = "SWE",
                       resampling: str = RESAMPLING_METHOD,
                       units: str = DEFAULT_UNITS) -> bool:
    """
    Clip a single UA SWE netCDF to the watershed and write a reprojected GeoTIFF.

    Processing order: clip in native CRS → reproject clipped raster → convert units.

    Parameters
    ----------
    nc_path : str
        Path to the input netCDF file.
    gdf : GeoDataFrame
        Watershed boundary in the source CRS.
    out_path : str
        Path for the output GeoTIFF.
    variable : str
        Variable name to extract ('SWE' or 'DEPTH'). Both are natively in mm.
    resampling : str
        Resampling method for reprojection ('nearest', 'bilinear', 'cubic').
    units : str
        Output unit system ('mm' or 'inches'). Conversion applied after reproject.

    Returns
    -------
    bool : True if successful.
    """
    try:
        ds = xr.open_dataset(nc_path)

        if variable not in ds:
            available = list(ds.data_vars)
            logger.warning(f"  Variable '{variable}' not found in {nc_path}. "
                           f"Available: {available}. Skipping.")
            ds.close()
            return False

        da = ds[variable]

        # Squeeze out single time dimension if present
        if "time" in da.dims and da.sizes["time"] == 1:
            da = da.squeeze("time", drop=True)

        # Attach source CRS and set spatial dimensions
        da = da.rio.write_crs(SOURCE_CRS)
        da = da.rio.set_spatial_dims(x_dim="lon", y_dim="lat")

        # Set NoData
        if da.rio.nodata is None:
            da = da.rio.write_nodata(np.nan)

        # --- Step A: Clip to watershed (in native geographic CRS) ---
        clipped = da.rio.clip(gdf.geometry, gdf.crs, drop=True, all_touched=True)

        # --- Step B: Reproject the clipped raster to target CRS ---
        target_crs = CRS.from_wkt(TARGET_CRS_WKT)
        reprojected = clipped.rio.reproject(
            target_crs,
            resampling=RESAMPLING_MAP.get(resampling, Resampling.bilinear),
            nodata=NODATA_VALUE,
        )

        # --- Step C: Convert units (mm → inches if requested) ---
        factor = unit_conversion_factor(units)
        if factor != 1.0:
            # Only convert valid pixels; keep NoData as-is
            mask = reprojected != NODATA_VALUE
            reprojected = xr.where(mask, reprojected * factor, NODATA_VALUE)
            # Preserve spatial metadata after xr.where
            reprojected.rio.write_crs(target_crs, inplace=True)
            reprojected.rio.write_nodata(NODATA_VALUE, inplace=True)

        # Write GeoTIFF with LZW compression
        reprojected.rio.to_raster(out_path, compress="LZW", dtype="float32")

        ds.close()
        return True

    except Exception as e:
        logger.error(f"  Error processing {nc_path}: {e}")
        return False


def _clip_worker(nc_path: str, shp_path: str, out_path: str,
                 variable: str, resampling: str, units: str,
                 delete_raw: bool) -> str:
    """
    Worker function for parallel clip-and-reproject.

    Accepts the watershed path (not GeoDataFrame) so the function is
    picklable across processes. Each worker reloads the shapefile.

    Returns
    -------
    str : 'processed', 'failed', or 'deleted' (processed + raw deleted).
    """
    gdf = load_watershed(shp_path)
    success = clip_and_reproject(nc_path, gdf, out_path, variable, resampling, units)
    if success:
        if delete_raw and os.path.exists(nc_path):
            os.remove(nc_path)
            return "deleted"
        return "processed"
    return "failed"


def process_water_year(wy: int, gdf: gpd.GeoDataFrame,
                       variable: str = "SWE",
                       delete_raw: bool = DELETE_RAW_NETCDF,
                       resampling: str = RESAMPLING_METHOD,
                       units: str = DEFAULT_UNITS,
                       shp_path: str | None = None,
                       max_workers: int | None = None) -> dict:
    """
    Clip and reproject all netCDF files for a single water year.

    Returns dict with counts: 'processed', 'skipped', 'failed', 'deleted'.
    """
    raw_dir = os.path.join(DIR_RAW, f"WY{wy}")
    out_dir = ensure_dir(os.path.join(DIR_CLIPPED, f"WY{wy}"))

    nc_files = sorted(glob.glob(os.path.join(raw_dir, "*.nc")))
    if not nc_files:
        logger.warning(f"  No netCDF files found in {raw_dir}")
        return {"processed": 0, "skipped": 0, "failed": 0, "deleted": 0}

    stats = {"processed": 0, "skipped": 0, "failed": 0, "deleted": 0}

    # Build list of files that need processing (skip existing)
    to_process = []
    for nc_path in nc_files:
        nc_name = os.path.basename(nc_path)
        tif_name = build_geotiff_name(nc_name, variable)
        out_path = os.path.join(out_dir, tif_name)

        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            stats["skipped"] += 1
            if delete_raw and os.path.exists(nc_path):
                os.remove(nc_path)
                stats["deleted"] += 1
            continue

        to_process.append((nc_path, out_path, nc_name, tif_name))

    if not to_process:
        return stats

    # Parallel processing if watershed path is available and workers > 1
    n_workers = max_workers or max(1, multiprocessing.cpu_count() - 1)

    if shp_path and n_workers > 1 and len(to_process) > 1:
        logger.info(f"  Processing {len(to_process)} files with {n_workers} workers")
        futures = {}
        with ProcessPoolExecutor(max_workers=n_workers) as executor:
            for nc_path, out_path, nc_name, tif_name in to_process:
                future = executor.submit(
                    _clip_worker, nc_path, shp_path, out_path,
                    variable, resampling, units, delete_raw,
                )
                futures[future] = (nc_name, tif_name)

            for future in as_completed(futures):
                nc_name, tif_name = futures[future]
                try:
                    result = future.result()
                    if result == "deleted":
                        stats["processed"] += 1
                        stats["deleted"] += 1
                        logger.debug(f"  Deleted raw: {nc_name}")
                    elif result == "processed":
                        stats["processed"] += 1
                    else:
                        stats["failed"] += 1
                except Exception as e:
                    logger.error(f"  Worker error for {nc_name}: {e}")
                    stats["failed"] += 1
    else:
        # Sequential fallback
        for i, (nc_path, out_path, nc_name, tif_name) in enumerate(to_process, 1):
            logger.info(f"  [{i}/{len(nc_files)}] {nc_name} → {tif_name}")

            if clip_and_reproject(nc_path, gdf, out_path, variable, resampling, units):
                stats["processed"] += 1
                if delete_raw:
                    os.remove(nc_path)
                    stats["deleted"] += 1
                    logger.debug(f"  Deleted raw: {nc_name}")
            else:
                stats["failed"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Clip UA SWE netCDFs to a watershed and export GeoTIFFs."
    )
    parser.add_argument("--watershed", type=str, required=True,
                        help="Path to watershed boundary file (.shp, .gpkg, .geojson)")
    parser.add_argument("--wy_start", type=int, required=True,
                        help="First water year to process")
    parser.add_argument("--wy_end", type=int, required=True,
                        help="Last water year to process")
    parser.add_argument("--variable", type=str, default="SWE",
                        choices=["SWE", "DEPTH"],
                        help="Variable to extract (default: SWE)")
    parser.add_argument("--units", type=str, default=DEFAULT_UNITS,
                        choices=["mm", "inches"],
                        help=f"Output unit system for GeoTIFFs (default: {DEFAULT_UNITS})")
    parser.add_argument("--keep_raw", action="store_true",
                        help="Do NOT delete raw netCDF files after clipping")
    parser.add_argument("--resampling", type=str, default=RESAMPLING_METHOD,
                        choices=["nearest", "bilinear", "cubic"],
                        help=f"Resampling method for reprojection (default: {RESAMPLING_METHOD})")
    parser.add_argument("--workers", type=int, default=None,
                        help="Number of parallel workers (default: CPU count - 1)")
    args = parser.parse_args()

    logger.info("=" * 70)
    logger.info("UA SWE Clip & Reproject — Water Years "
                f"{args.wy_start} to {args.wy_end}")
    logger.info(f"  Watershed : {args.watershed}")
    logger.info(f"  Variable  : {args.variable}")
    logger.info(f"  Units     : {args.units}")
    logger.info(f"  Delete raw: {not args.keep_raw}")
    logger.info(f"  Resampling: {args.resampling}")
    logger.info(f"  Workers   : {args.workers or 'auto'}")
    logger.info("=" * 70)

    gdf = load_watershed(args.watershed)

    total = {"processed": 0, "skipped": 0, "failed": 0, "deleted": 0}

    for wy in range(args.wy_start, args.wy_end + 1):
        with StepTimer(f"Clip WY{wy}"):
            logger.info(f"Processing WY{wy}...")
            result = process_water_year(wy, gdf,
                                        variable=args.variable,
                                        delete_raw=not args.keep_raw,
                                        resampling=args.resampling,
                                        units=args.units,
                                        shp_path=args.watershed,
                                        max_workers=args.workers)
            for k in total:
                total[k] += result[k]
            logger.info(f"  WY{wy} — {result}")

    logger.info("=" * 70)
    logger.info(f"Clipping complete — {total}")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
