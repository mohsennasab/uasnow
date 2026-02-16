"""
Step 4 — Visualize SWE data: spatial maps from GeoTIFFs + time series from CSVs.

All plots use the same --units flag that was applied during clipping and
processing, ensuring labels, axes, and color bars are consistent.

Produces:
  - Spatial maps of SWE for selected dates (PNG)
  - Basin-mean SWE time series plots (PNG)
  - Annual peak SWE comparison plots (PNG)
  - Heatmap of basin-mean SWE over day-of-water-year (x) vs water year (y) (PNG)

Usage:
    python 04_visualize_swe.py --wy_start 2020 --wy_end 2020
    python 04_visualize_swe.py --wy_start 2015 --wy_end 2024 --units inches --dpi 300
"""

import os
import glob
import argparse

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import rioxarray  # noqa: F401

from config import (
    DIR_CLIPPED, DIR_PROCESSED, DIR_FIGURES,
    DEFAULT_UNITS, NODATA_VALUE,
    unit_label,
)
from utils import setup_logging, ensure_dir, parse_filename_date, StepTimer

logger = setup_logging()

# ─────────────────────────────────────────────────────────────────────────────
# Color maps and styling
# ─────────────────────────────────────────────────────────────────────────────
SWE_CMAP = "Blues"
SWE_VMIN = 0
# Sensible defaults per unit system (can be overridden later)
# SWE_VMAX_MM = 1500
# SWE_VMAX_IN = 60  # ~1524 mm

plt.rcParams.update({
    "font.size": 10,
    "axes.titlesize": 12,
    "axes.labelsize": 10,
    "figure.dpi": 150,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.1,
})


def _vmax_for_units(units: str) -> float:
    """Return a reasonable default colorbar max for the given unit system."""
    return SWE_VMAX_IN if units == "inches" else SWE_VMAX_MM


# ─────────────────────────────────────────────────────────────────────────────
# Spatial maps
# ─────────────────────────────────────────────────────────────────────────────

def plot_spatial_map(tif_path: str, out_dir: str, variable: str = "SWE",
                     units: str = "mm", dpi: int = 200) -> str | None:
    """
    Plot a single GeoTIFF as a spatial map and save as PNG.

    The GeoTIFF values are already in the target unit system (mm or inches),
    so no conversion is needed here — only labelling.

    Returns path to the saved PNG, or None on failure.
    """
    try:
        da = rioxarray.open_rasterio(tif_path).squeeze("band", drop=True)
        da = da.where(da != NODATA_VALUE)

        d = parse_filename_date(os.path.basename(tif_path))
        date_str = d.strftime("%Y-%m-%d") if d else "unknown"

        data_max = float(da.max().values)
        vmax = np.ceil(data_max / 5) * 5 if data_max > 5 else np.ceil(data_max)  # Round up to nearest 5
        ulabel = unit_label(variable, units)

        fig, ax = plt.subplots(1, 1, figsize=(8, 6))
        da.plot(ax=ax, cmap=SWE_CMAP, vmin=SWE_VMIN, vmax=vmax,
                add_colorbar=True, cbar_kwargs={"label": ulabel})
        ax.set_title(f"UA {variable} 800m — {date_str}", fontweight="bold")
        ax.set_xlabel("Easting (ft)")
        ax.set_ylabel("Northing (ft)")
        ax.set_aspect("equal")

        png_name = os.path.splitext(os.path.basename(tif_path))[0] + ".png"
        out_path = os.path.join(out_dir, png_name)
        fig.savefig(out_path, dpi=dpi)
        plt.close(fig)
        return out_path

    except Exception as e:
        logger.error(f"  Error plotting {tif_path}: {e}")
        return None


def plot_peak_swe_maps(wy_start: int, wy_end: int, variable: str = "SWE",
                       units: str = "mm", out_dir: str = DIR_FIGURES,
                       dpi: int = 200) -> None:
    """
    For each water year, find the date with maximum basin-mean value
    and plot that day's spatial map.
    """
    val_col = f"{variable}_{units}"
    for wy in range(wy_start, wy_end + 1):
        csv_path = os.path.join(DIR_PROCESSED, f"basin_mean_{variable}_WY{wy}.csv")
        if not os.path.exists(csv_path):
            continue

        df = pd.read_csv(csv_path, parse_dates=["date"])
        if df.empty or val_col not in df.columns:
            continue

        peak_row = df.loc[df[val_col].idxmax()]
        peak_date = peak_row["date"]
        date_str = peak_date.strftime("%Y%m%d")

        tif_dir = os.path.join(DIR_CLIPPED, f"WY{wy}")
        matches = glob.glob(os.path.join(tif_dir, f"*{date_str}*.tif"))
        if matches:
            path = plot_spatial_map(matches[0], out_dir, variable, units, dpi)
            if path:
                logger.info(f"  WY{wy} peak {variable} map: "
                            f"{peak_date.date()} → {path}")


# ─────────────────────────────────────────────────────────────────────────────
# Time series plots
# ─────────────────────────────────────────────────────────────────────────────

def plot_basin_mean_timeseries(wy_start: int, wy_end: int,
                               variable: str = "SWE",
                               units: str = "mm",
                               out_dir: str = DIR_FIGURES,
                               dpi: int = 200) -> None:
    """
    Plot basin-mean time series. If multiple water years, overlay them
    on a day-of-water-year axis for comparison.
    """
    ensure_dir(out_dir)
    val_col = f"{variable}_{units}"
    ulabel = unit_label(variable, units)
    n_years = wy_end - wy_start + 1

    if n_years == 1:
        csv_path = os.path.join(DIR_PROCESSED,
                                f"basin_mean_{variable}_WY{wy_start}.csv")
        if not os.path.exists(csv_path):
            logger.warning(f"  CSV not found: {csv_path}")
            return

        df = pd.read_csv(csv_path, parse_dates=["date"])
        if val_col not in df.columns:
            logger.warning(f"  Column '{val_col}' not found in CSV")
            return

        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(df["date"], df[val_col], color="steelblue", linewidth=1.2)
        ax.fill_between(df["date"], 0, df[val_col], alpha=0.2, color="steelblue")
        ax.set_xlabel("Date")
        ax.set_ylabel(f"Basin-Mean {ulabel}")
        ax.set_title(f"Basin-Mean {variable} — WY{wy_start}", fontweight="bold")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        plt.xticks(rotation=45)
        ax.grid(True, alpha=0.3)
        ax.set_xlim(df["date"].min(), df["date"].max())
        ax.set_ylim(bottom=0)

        out_path = os.path.join(out_dir,
                                f"timeseries_{variable}_WY{wy_start}.png")
        fig.savefig(out_path, dpi=dpi)
        plt.close(fig)
        logger.info(f"  Time series plot: {out_path}")

    else:
        fig, ax = plt.subplots(figsize=(10, 6))
        cmap = plt.cm.viridis(np.linspace(0, 1, n_years))

        for i, wy in enumerate(range(wy_start, wy_end + 1)):
            csv_path = os.path.join(DIR_PROCESSED,
                                    f"basin_mean_{variable}_WY{wy}.csv")
            if not os.path.exists(csv_path):
                continue
            df = pd.read_csv(csv_path, parse_dates=["date"])
            if df.empty or val_col not in df.columns:
                continue
            wy_start_date = pd.Timestamp(wy - 1, 10, 1)
            df["dowy"] = (df["date"] - wy_start_date).dt.days + 1
            ax.plot(df["dowy"], df[val_col], color=cmap[i],
                    linewidth=0.8, alpha=0.7, label=f"WY{wy}")

        ax.set_xlabel("Day of Water Year (Oct 1 = Day 1)")
        ax.set_ylabel(f"Basin-Mean {ulabel}")
        ax.set_title(f"Basin-Mean {variable} — WY{wy_start}–{wy_end}",
                      fontweight="bold")
        ax.set_xlim(1, 366)
        ax.set_ylim(bottom=0)
        ax.grid(True, alpha=0.3)

        if n_years <= 15:
            ax.legend(fontsize=7, ncol=3, loc="upper right")

        out_path = os.path.join(out_dir,
            f"timeseries_{variable}_WY{wy_start}-{wy_end}.png")
        fig.savefig(out_path, dpi=dpi)
        plt.close(fig)
        logger.info(f"  Multi-year overlay plot: {out_path}")


def plot_annual_peak_swe(wy_start: int, wy_end: int, variable: str = "SWE",
                         units: str = "mm", out_dir: str = DIR_FIGURES,
                         dpi: int = 200) -> None:
    """Bar chart of annual peak basin-mean values across water years."""
    ensure_dir(out_dir)
    val_col = f"{variable}_{units}"
    ulabel = unit_label(variable, units)
    records = []

    for wy in range(wy_start, wy_end + 1):
        csv_path = os.path.join(DIR_PROCESSED,
                                f"basin_mean_{variable}_WY{wy}.csv")
        if not os.path.exists(csv_path):
            continue
        df = pd.read_csv(csv_path, parse_dates=["date"])
        if df.empty or val_col not in df.columns:
            continue
        peak = df[val_col].max()
        peak_date = df.loc[df[val_col].idxmax(), "date"]
        records.append({"WY": wy, "peak": peak, "peak_date": peak_date})

    if not records:
        logger.warning(f"  No data for annual peak {variable} plot")
        return

    peaks = pd.DataFrame(records)

    fig, ax = plt.subplots(figsize=(max(8, len(peaks) * 0.4), 5))
    colors = plt.cm.Blues(np.linspace(0.3, 0.9, len(peaks)))
    ax.bar(peaks["WY"], peaks["peak"], color=colors, edgecolor="navy",
           linewidth=0.5)
    ax.set_xlabel("Water Year")
    ax.set_ylabel(f"Peak Basin-Mean {ulabel}")
    ax.set_title(f"Annual Peak {variable} — WY{wy_start}–{wy_end}",
                  fontweight="bold")
    ax.grid(True, axis="y", alpha=0.3)

    out_path = os.path.join(out_dir,
        f"annual_peak_{variable}_WY{wy_start}-{wy_end}.png")
    fig.savefig(out_path, dpi=dpi)
    plt.close(fig)
    logger.info(f"  Annual peak {variable} plot: {out_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Heatmap — day-of-water-year (x) vs water year (y)
# ─────────────────────────────────────────────────────────────────────────────

def plot_swe_heatmap(wy_start: int, wy_end: int, variable: str = "SWE",
                     units: str = "mm", out_dir: str = DIR_FIGURES,
                     dpi: int = 200) -> None:
    """
    Create a heatmap of basin-mean values with:
      - x-axis: day of water year (1–366, Oct 1 = day 1)
      - y-axis: water year

    Values come directly from the CSV (already in the target unit system).
    """
    ensure_dir(out_dir)
    n_years = wy_end - wy_start + 1
    val_col = f"{variable}_{units}"
    ulabel = unit_label(variable, units)

    if n_years < 2:
        logger.info("  Heatmap requires at least 2 water years — skipping")
        return

    max_dowy = 366
    wy_labels = []
    matrix = []

    for wy in range(wy_start, wy_end + 1):
        csv_path = os.path.join(DIR_PROCESSED,
                                f"basin_mean_{variable}_WY{wy}.csv")
        if not os.path.exists(csv_path):
            continue
        df = pd.read_csv(csv_path, parse_dates=["date"])
        if df.empty or val_col not in df.columns:
            continue

        wy_start_date = pd.Timestamp(wy - 1, 10, 1)
        df["dowy"] = (df["date"] - wy_start_date).dt.days + 1
        df = df[(df["dowy"] >= 1) & (df["dowy"] <= max_dowy)]

        row = np.full(max_dowy, np.nan)
        for _, r in df.iterrows():
            idx = int(r["dowy"]) - 1
            if 0 <= idx < max_dowy:
                row[idx] = r[val_col]

        matrix.append(row)
        wy_labels.append(f"WY{wy}")

    if len(matrix) < 2:
        logger.warning("  Not enough data for heatmap")
        return

    data = np.array(matrix)

    vmax = np.nanmax(data) if not np.all(np.isnan(data)) else 1.0
    vmax = np.ceil(vmax / 50) * 50 if vmax > 50 else np.ceil(vmax)

    month_starts = {
        "Oct": 1, "Nov": 32, "Dec": 62, "Jan": 93, "Feb": 124,
        "Mar": 152, "Apr": 183, "May": 213, "Jun": 244,
        "Jul": 274, "Aug": 305, "Sep": 336,
    }

    fig, ax = plt.subplots(figsize=(14, max(4, n_years * 0.35)))

    im = ax.imshow(
        data, aspect="auto", cmap=SWE_CMAP,
        vmin=0, vmax=vmax, interpolation="nearest", origin="upper",
    )

    ax.set_yticks(range(len(wy_labels)))
    ax.set_yticklabels(wy_labels, fontsize=8)
    ax.set_xticks([v - 1 for v in month_starts.values()])
    ax.set_xticklabels(month_starts.keys(), fontsize=9)
    ax.set_xlim(-0.5, max_dowy - 0.5)

    ax.set_xlabel("Month of Water Year")
    ax.set_ylabel("Water Year")
    ax.set_title(f"Basin-Mean {variable} Heatmap — WY{wy_start}–{wy_end}",
                  fontweight="bold", fontsize=13)

    cbar = fig.colorbar(im, ax=ax, pad=0.02)
    cbar.set_label(ulabel, fontsize=10)

    out_path = os.path.join(
        out_dir, f"heatmap_{variable}_WY{wy_start}-{wy_end}.png"
    )
    fig.savefig(out_path, dpi=dpi)
    plt.close(fig)
    logger.info(f"  {variable} heatmap: {out_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Visualize processed SWE data: spatial maps + time series + heatmap."
    )
    parser.add_argument("--wy_start", type=int, required=True)
    parser.add_argument("--wy_end", type=int, required=True)
    parser.add_argument("--variable", type=str, default="SWE",
                        choices=["SWE", "DEPTH"])
    parser.add_argument("--units", type=str, default=DEFAULT_UNITS,
                        choices=["mm", "inches"],
                        help=f"Unit system (must match earlier steps; default: {DEFAULT_UNITS})")
    parser.add_argument("--dpi", type=int, default=200,
                        help="Output resolution (default: 200)")
    args = parser.parse_args()

    out_dir = ensure_dir(DIR_FIGURES)

    logger.info("=" * 70)
    logger.info(f"UA SWE Visualization — WY{args.wy_start} to WY{args.wy_end}")
    logger.info(f"  Units: {args.units}")
    logger.info("=" * 70)

    with StepTimer("Time series plots"):
        plot_basin_mean_timeseries(args.wy_start, args.wy_end,
                                   variable=args.variable, units=args.units,
                                   out_dir=out_dir, dpi=args.dpi)

    if args.wy_end > args.wy_start:
        with StepTimer("Annual peak bar chart"):
            plot_annual_peak_swe(args.wy_start, args.wy_end,
                                 variable=args.variable, units=args.units,
                                 out_dir=out_dir, dpi=args.dpi)

    with StepTimer("Peak spatial maps"):
        plot_peak_swe_maps(args.wy_start, args.wy_end,
                           variable=args.variable, units=args.units,
                           out_dir=out_dir, dpi=args.dpi)

    if args.wy_end > args.wy_start:
        with StepTimer("Heatmap"):
            plot_swe_heatmap(args.wy_start, args.wy_end,
                             variable=args.variable, units=args.units,
                             out_dir=out_dir, dpi=args.dpi)

    logger.info("Visualization complete.")


if __name__ == "__main__":
    main()
