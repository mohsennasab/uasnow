"""
Run the full UA SWE pipeline: Download → Clip → Process → Visualize.

This is the single entry point for automated end-to-end processing.
The --units flag is passed to all downstream steps so that GeoTIFFs, CSVs,
and visualizations use a consistent unit system throughout.

A timestamped log file (.log.txt) is saved in the data/ directory with
process details and step-by-step timing.

Usage:
    python run_pipeline.py --watershed watershed.shp --wy_start 2020 --wy_end 2020
    python run_pipeline.py --watershed watershed.shp --wy_start 2020 --wy_end 2020 --units inches
    python run_pipeline.py --watershed basin.gpkg --wy_start 2015 --wy_end 2024 --monthly
"""

import argparse
import subprocess
import sys
from config import DEFAULT_UNITS
from utils import setup_logging, StepTimer

logger = setup_logging()


def run_step(script: str, args: list[str], step_name: str) -> bool:
    """Run a pipeline step as a subprocess."""
    cmd = [sys.executable, script] + args
    logger.info(f"{'─' * 60}")
    logger.info(f"STEP: {step_name}")
    logger.info(f"  Command: {' '.join(cmd)}")
    logger.info(f"{'─' * 60}")

    result = subprocess.run(cmd)
    if result.returncode != 0:
        logger.error(f"  {step_name} FAILED (exit code {result.returncode})")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Run the full UA SWE pipeline end-to-end."
    )
    parser.add_argument("--watershed", type=str, required=True,
                        help="Path to watershed boundary (.shp, .gpkg, .geojson)")
    parser.add_argument("--wy_start", type=int, required=True,
                        help="First water year")
    parser.add_argument("--wy_end", type=int, required=True,
                        help="Last water year")
    parser.add_argument("--variable", type=str, default="SWE",
                        choices=["SWE", "DEPTH"])
    parser.add_argument("--units", type=str, default=DEFAULT_UNITS,
                        choices=["mm", "inches"],
                        help=f"Unit system for all outputs (default: {DEFAULT_UNITS})")
    parser.add_argument("--status", type=str, default=None,
                        choices=["early", "provisional", "stable"],
                        help="Only download files with this status")
    parser.add_argument("--monthly", action="store_true",
                        help="Also compute monthly-mean GeoTIFFs")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--keep_raw", action="store_true",
                        help="Keep raw netCDF files after clipping")
    parser.add_argument("--skip_download", action="store_true",
                        help="Skip download step (use existing files)")
    parser.add_argument("--skip_clip", action="store_true",
                        help="Skip clip step (use existing GeoTIFFs)")
    parser.add_argument("--skip_process", action="store_true",
                        help="Skip processing step")
    parser.add_argument("--skip_visualize", action="store_true",
                        help="Skip visualization step")
    args = parser.parse_args()

    logger.info("=" * 70)
    logger.info("UA SWE PIPELINE")
    logger.info(f"  Water Years : {args.wy_start} – {args.wy_end}")
    logger.info(f"  Watershed   : {args.watershed}")
    logger.info(f"  Variable    : {args.variable}")
    logger.info(f"  Units       : {args.units}")
    logger.info("=" * 70)

    # Step 1: Download (units not relevant here — raw netCDFs are always in mm)
    if not args.skip_download:
        dl_args = ["--wy_start", str(args.wy_start),
                    "--wy_end", str(args.wy_end)]
        if args.status:
            dl_args += ["--status", args.status]
        with StepTimer("Step 1 — Download"):
            if not run_step("01_download_ua_swe.py", dl_args, "Download"):
                logger.error("Pipeline aborted at download step.")
                sys.exit(1)

    # Step 2: Clip & Reproject → GeoTIFF (applies unit conversion)
    if not args.skip_clip:
        clip_args = ["--watershed", args.watershed,
                     "--wy_start", str(args.wy_start),
                     "--wy_end", str(args.wy_end),
                     "--variable", args.variable,
                     "--units", args.units]
        if args.keep_raw:
            clip_args.append("--keep_raw")
        with StepTimer("Step 2 — Clip & Reproject"):
            if not run_step("02_clip_to_watershed.py", clip_args,
                            "Clip & Reproject"):
                logger.error("Pipeline aborted at clip step.")
                sys.exit(1)

    # Step 3: Process (reads GeoTIFFs in target units, writes CSV accordingly)
    if not args.skip_process:
        proc_args = ["--wy_start", str(args.wy_start),
                     "--wy_end", str(args.wy_end),
                     "--variable", args.variable,
                     "--units", args.units]
        if args.monthly:
            proc_args.append("--monthly")
        with StepTimer("Step 3 — Process"):
            if not run_step("03_process_swe.py", proc_args, "Process"):
                logger.error("Pipeline aborted at processing step.")
                sys.exit(1)

    # Step 4: Visualize (labels all plots in target units)
    if not args.skip_visualize:
        viz_args = ["--wy_start", str(args.wy_start),
                    "--wy_end", str(args.wy_end),
                    "--variable", args.variable,
                    "--units", args.units,
                    "--dpi", str(args.dpi)]
        with StepTimer("Step 4 — Visualize"):
            if not run_step("04_visualize_swe.py", viz_args, "Visualize"):
                logger.warning("Visualization step failed (non-critical).")

    logger.info("=" * 70)
    logger.info("PIPELINE COMPLETE")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
