"""
Step 1 — Download UA SWE 800 m daily netCDF files.

Automatically scrapes the Apache directory listing at the UA climate server
and downloads all .nc files for the requested water years.

Usage:
    python 01_download_ua_swe.py --wy_start 2020 --wy_end 2020
    python 01_download_ua_swe.py --wy_start 1982 --wy_end 2026
    python 01_download_ua_swe.py --wy_start 2024 --wy_end 2024 --status stable
"""

import os
import re
import argparse
import time
import requests
from config import (
    UA_SWE_BASE_URL,
    FIRST_WATER_YEAR,
    LATEST_WATER_YEAR,
    DIR_RAW,
    DOWNLOAD_RETRIES,
    DOWNLOAD_TIMEOUT,
)
from utils import setup_logging, ensure_dir, StepTimer

logger = setup_logging()

# ─────────────────────────────────────────────────────────────────────────────
# Apache index parser
# ─────────────────────────────────────────────────────────────────────────────

def list_nc_files(wy: int, status_filter: str | None = None) -> list[str]:
    """
    Fetch the Apache directory listing for a water year and return a list of
    .nc filenames. Optionally filter by status (early, provisional, stable).
    """
    url = f"{UA_SWE_BASE_URL}/WY{wy}/"
    logger.info(f"Listing files at {url}")
    resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT)
    resp.raise_for_status()

    # Parse <a href="..."> links ending in .nc
    pattern = re.compile(r'href="([^"]+\.nc)"', re.IGNORECASE)
    filenames = pattern.findall(resp.text)

    if status_filter:
        filenames = [f for f in filenames if f"_{status_filter}.nc" in f]

    logger.info(f"  Found {len(filenames)} netCDF files for WY{wy}"
                + (f" (status={status_filter})" if status_filter else ""))
    return filenames


def download_file(url: str, out_path: str, retries: int = DOWNLOAD_RETRIES) -> bool:
    """
    Download a single file with retry logic and resume support.
    Returns True on success, False on failure.
    """
    for attempt in range(1, retries + 1):
        try:
            # Check for partial download → resume
            headers = {}
            existing_size = 0
            if os.path.exists(out_path):
                existing_size = os.path.getsize(out_path)
                headers["Range"] = f"bytes={existing_size}-"

            resp = requests.get(url, headers=headers, stream=True,
                                timeout=DOWNLOAD_TIMEOUT)

            # If server returns 416 (range not satisfiable), file is complete
            if resp.status_code == 416:
                logger.debug(f"  Already complete: {os.path.basename(out_path)}")
                return True

            resp.raise_for_status()

            mode = "ab" if existing_size and resp.status_code == 206 else "wb"
            with open(out_path, mode) as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
            return True

        except (requests.RequestException, IOError) as e:
            logger.warning(f"  Attempt {attempt}/{retries} failed for "
                           f"{os.path.basename(out_path)}: {e}")
            if attempt < retries:
                time.sleep(2 ** attempt)  # exponential backoff
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Main download logic
# ─────────────────────────────────────────────────────────────────────────────

def download_water_year(wy: int, out_dir: str,
                        status_filter: str | None = None,
                        skip_existing: bool = True) -> dict:
    """
    Download all daily netCDF files for a single water year.

    Parameters
    ----------
    wy : int
        Water year (e.g. 2020).
    out_dir : str
        Output directory for this water year.
    status_filter : str, optional
        Only download files matching this status ('early', 'provisional', 'stable').
    skip_existing : bool
        Skip files that already exist locally with size > 0.

    Returns
    -------
    dict with keys: 'downloaded', 'skipped', 'failed' (counts).
    """
    ensure_dir(out_dir)
    filenames = list_nc_files(wy, status_filter)

    stats = {"downloaded": 0, "skipped": 0, "failed": 0}

    for i, fname in enumerate(filenames, 1):
        out_path = os.path.join(out_dir, fname)

        if skip_existing and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            stats["skipped"] += 1
            continue

        url = f"{UA_SWE_BASE_URL}/WY{wy}/{fname}"
        logger.info(f"  [{i}/{len(filenames)}] Downloading {fname}")

        if download_file(url, out_path):
            stats["downloaded"] += 1
        else:
            stats["failed"] += 1
            logger.error(f"  FAILED: {fname}")

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Download UA SWE 800m daily netCDF files by water year."
    )
    parser.add_argument("--wy_start", type=int, default=FIRST_WATER_YEAR,
                        help=f"First water year to download (default: {FIRST_WATER_YEAR})")
    parser.add_argument("--wy_end", type=int, default=LATEST_WATER_YEAR,
                        help=f"Last water year to download (default: {LATEST_WATER_YEAR})")
    parser.add_argument("--status", type=str, default=None,
                        choices=["early", "provisional", "stable"],
                        help="Only download files with this status label")
    parser.add_argument("--no_skip", action="store_true",
                        help="Re-download files even if they exist locally")
    args = parser.parse_args()

    logger.info("=" * 70)
    logger.info("UA SWE 800m Download — Water Years "
                f"{args.wy_start} to {args.wy_end}")
    logger.info("=" * 70)

    total = {"downloaded": 0, "skipped": 0, "failed": 0}

    for wy in range(args.wy_start, args.wy_end + 1):
        out_dir = os.path.join(DIR_RAW, f"WY{wy}")
        with StepTimer(f"Download WY{wy}"):
            result = download_water_year(wy, out_dir,
                                         status_filter=args.status,
                                         skip_existing=not args.no_skip)
        for k in total:
            total[k] += result[k]
        logger.info(f"  WY{wy} complete — {result}")

    logger.info("=" * 70)
    logger.info(f"Download complete — {total}")
    if total["failed"] > 0:
        logger.warning(f"  {total['failed']} files failed. Re-run to retry.")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
