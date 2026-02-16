"""
Shared utility functions for the UA SWE Toolkit.
"""

import os
import re
import time
import logging
from datetime import date, datetime

logger = logging.getLogger("ua_swe_toolkit")


def setup_logging(level=logging.INFO, log_dir: str = "data"):
    """
    Configure consistent logging across all scripts.

    Logs to both console and a timestamped log file in the specified directory.
    The log file captures process details and timing metadata.

    Parameters
    ----------
    level : int
        Logging level (default: logging.INFO).
    log_dir : str
        Directory where log files are saved (default: 'data').

    Returns
    -------
    logging.Logger
    """
    log = logging.getLogger("ua_swe_toolkit")

    # Avoid adding duplicate handlers on repeated calls
    if log.handlers:
        return log

    log.setLevel(level)

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(level)
    console_fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console.setFormatter(console_fmt)
    log.addHandler(console)

    # File handler — save metadata log
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"ua_swe_toolkit_{timestamp}.log.txt")
    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setLevel(level)
    file_fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_fmt)
    log.addHandler(file_handler)

    log.info(f"Log file: {log_path}")
    return log


class StepTimer:
    """
    Context manager for timing pipeline steps. Logs elapsed time on exit.

    Usage:
        with StepTimer("Download WY2020"):
            download_water_year(2020, ...)
    """

    def __init__(self, step_name: str):
        self.step_name = step_name
        self.start = None

    def __enter__(self):
        self.start = time.time()
        logger.info(f"[TIMER] {self.step_name} — started")
        return self

    def __exit__(self, *exc):
        elapsed = time.time() - self.start
        if elapsed < 60:
            time_str = f"{elapsed:.1f}s"
        elif elapsed < 3600:
            time_str = f"{elapsed / 60:.1f}min"
        else:
            time_str = f"{elapsed / 3600:.1f}hr"
        logger.info(f"[TIMER] {self.step_name} — finished in {time_str}")
        return False


def ensure_dir(path: str) -> str:
    """Create directory (and parents) if it does not exist. Returns the path."""
    os.makedirs(path, exist_ok=True)
    return path


def water_year(d: date) -> int:
    """Return the water year for a given date (WY starts Oct 1)."""
    return d.year + 1 if d.month >= 10 else d.year


def wy_date_range(wy: int) -> tuple:
    """Return (start_date, end_date) for a water year."""
    return date(wy - 1, 10, 1), date(wy, 9, 30)


def parse_filename_date(filename: str) -> date | None:
    """
    Extract the date from a UA SWE filename.
    Example: UA_SWE_Depth_800m_v1_19811001_stable.nc → 1981-10-01
    """
    match = re.search(r"_(\d{8})_", filename)
    if match:
        ds = match.group(1)
        return date(int(ds[:4]), int(ds[4:6]), int(ds[6:8]))
    return None


def parse_filename_status(filename: str) -> str | None:
    """
    Extract the status label from a UA SWE filename.
    Returns one of: 'early', 'provisional', 'stable', or None.
    """
    match = re.search(r"_(early|provisional|stable)\.nc$", filename)
    return match.group(1) if match else None


def build_geotiff_name(nc_filename: str, variable: str = "SWE") -> str:
    """
    Convert a netCDF filename to a GeoTIFF filename.
    Example: UA_SWE_Depth_800m_v1_19811001_stable.nc
           → UA_SWE_800m_19811001_stable.tif
    """
    base = os.path.splitext(nc_filename)[0]
    base = base.replace("_Depth_800m_v1_", f"_800m_{variable}_")
    return base + ".tif"
