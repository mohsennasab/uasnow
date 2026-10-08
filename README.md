# UA SWE Toolkit

**Automated download, clipping, processing, and visualization of the University of Arizona 800 m Snow Water Equivalent (SWE) dataset.**

The [UA SWE dataset](https://climate.arizona.edu/data/UA_SWE/DailyData_800m/) provides daily gridded SWE and snow depth estimates at 800 m resolution across the contiguous United States, available from Water Year 1982 to present. This toolkit automates the full workflow from raw netCDF download to watershed-clipped GeoTIFFs, basin-mean time series, and publication-ready figures.

---

## Features

- **Automated download** — Parses the UA Apache directory index, downloads daily netCDFs by water year with retry logic and resume support
- **Clip & reproject** — Clips to any watershed boundary (Shapefile, GeoPackage, GeoJSON) and reprojects to USA Contiguous Albers Equal Area Conic (NAD83, feet)
- **Consistent unit system** — A single `--units` flag (`mm` or `inches`) controls the unit system across all outputs: GeoTIFFs, CSVs, and visualizations
- **GeoTIFF output** — Exports clipped data as LZW-compressed GeoTIFFs; raw netCDFs are automatically deleted to save storage
- **Basin-mean time series** — Computes daily spatially-averaged SWE and exports CSV
- **Visualization** — Spatial maps, time series overlays, annual peak SWE bar charts, and multi-year SWE heatmaps
- **Metadata logging** — Every run saves a timestamped `.log.txt` file in `data/` with process details and step timing
- **Single pipeline command** — `run_pipeline.py` chains steps 1–4 end-to-end
- **HEC-DSS export** — Standalone step 5 builds a period-of-record DSS file of daily SWE grids with HEC-Vortex, masked to a buffered watershed and named to the FFRD SOP (December 2025). Step 5b appends newer stable days to that file without touching the existing grids

---

## Unit System (`--units`)

The `--units` flag controls the output unit system for the **entire pipeline**. It is accepted by `run_pipeline.py` and by Steps 2–4 when run individually. The flag must be consistent across all steps.

| `--units` value | GeoTIFF pixel values | CSV column         | Plot axis labels        |
|-----------------|----------------------|--------------------|-------------------------|
| `mm` (default)  | millimeters          | `SWE_mm` or `DEPTH_mm`   | "SWE (mm)" / "DEPTH (mm)"     |
| `inches`        | inches               | `SWE_inches` or `DEPTH_inches` | "SWE (inches)" / "DEPTH (inches)" |

The source UA SWE dataset stores all values in **millimeters (mm)** natively. When `--units inches` is selected, the mm → inch conversion (÷ 25.4) is applied during Step 2 (clip & reproject) so that every downstream output — GeoTIFFs, CSVs, and visualizations — is in inches without any further conversion.

When using `run_pipeline.py`, just pass `--units inches` once and it propagates to all steps automatically.

When running steps individually, pass `--units` consistently:

```bash
# All in inches
python 02_clip_to_watershed.py --watershed basin.shp --wy_start 2020 --wy_end 2020 --units inches
python 03_process_swe.py --wy_start 2020 --wy_end 2020 --units inches
python 04_visualize_swe.py --wy_start 2020 --wy_end 2020 --units inches
```

> **Important:** If you clip GeoTIFFs in one unit system and then run Step 3 or Step 4 with a different `--units` value, the column names and labels will be wrong. Always keep the flag consistent.

---

## Available Variables

The UA SWE netCDF files contain two variables that can be requested via the `--variable` flag:

| Variable | Description                        | Native Unit | Notes                                    |
|----------|------------------------------------|-------------|------------------------------------------|
| `SWE`    | Snow Water Equivalent              | mm          | Default variable. Water content of snowpack. |
| `DEPTH`  | Snow Depth                         | mm          | Physical depth of the snowpack.            |

Both variables are stored in **millimeters (mm)** in the source netCDF files and can be output in either mm or inches via the `--units` flag.

---

## Supported Watershed Boundary Formats

The toolkit accepts any vector boundary file readable by GeoPandas / Fiona. Tested formats include:

| Format      | Extension(s)             | Notes                                           |
|-------------|--------------------------|-------------------------------------------------|
| Shapefile   | `.shp` (+ `.dbf`, `.shx`, `.prj`) | Most common; requires sidecar files in same directory |
| GeoPackage  | `.gpkg`                  | Recommended — single file, supports multiple layers |
| GeoJSON     | `.geojson`, `.json`      | Text-based, easy to inspect and version-control    |
| KML         | `.kml`                   | Requires Fiona KML driver                         |
| File GDB    | `.gdb` (directory)       | ESRI File Geodatabase (read-only via Fiona)       |

The watershed file can contain one or more polygon features. All features are dissolved into a single clip geometry. The CRS of the input file does not matter — it is automatically reprojected to match the source data (NAD83 / EPSG:4269) before clipping.

---

## How Files Are Processed

The toolkit follows a four-step pipeline. Each step can be run independently or chained via `run_pipeline.py`. Steps 5 and 5b are separate, standalone HEC-DSS tools and are not part of `run_pipeline.py`.

### Step 1 — Download (`01_download_ua_swe.py`)

1. Constructs the URL for each water year directory on the UA climate server (e.g., `https://climate.arizona.edu/data/UA_SWE/DailyData_800m/WY2020/`).
2. Parses the Apache directory listing HTML to extract all `.nc` filenames.
3. Optionally filters by status label (`early`, `provisional`, `stable`).
4. Downloads each netCDF file with retry logic (exponential backoff) and HTTP range-resume support.
5. Skips files that already exist locally (unless `--no_skip` is set).

**Output:** Raw netCDF files in `data/raw/WY{year}/` (always in native mm).

### Step 2 — Clip & Reproject (`02_clip_to_watershed.py`)

1. Loads the watershed boundary and reprojects it to the source CRS (NAD83 / EPSG:4269).
2. Opens each netCDF, extracts the requested variable (`SWE` or `DEPTH`).
3. Assigns the source CRS and spatial dimensions to the DataArray.
4. **Clips** the raster to the watershed boundary in the native geographic CRS.
5. **Reprojects** the clipped raster to the target Albers Equal Area Conic CRS using the configured resampling method.
6. **Converts units** if `--units inches` is specified (mm × 1/25.4). NoData pixels are preserved.
7. Writes the result as a LZW-compressed GeoTIFF (float32, NoData = -9999).
8. Optionally deletes the raw netCDF to save disk space.

**Output:** Clipped GeoTIFFs in `data/clipped/WY{year}/` in the selected unit system.

### Step 3 — Process (`03_process_swe.py`)

1. Reads all clipped GeoTIFFs for each water year, parses dates from filenames.
2. Stacks them into a time-dimensioned xarray DataArray.
3. Computes the spatial mean (basin-mean) for each time step.
4. Exports daily basin-mean values as CSV with columns: `date`, `{variable}_{units}` (e.g., `SWE_mm` or `SWE_inches`).
5. Optionally computes monthly-mean GeoTIFFs via temporal resampling.
6. Produces a combined multi-year CSV when processing multiple water years.

**Output:** CSV files and optional monthly GeoTIFFs in `data/processed/`.

**Parallel worker count for Steps 2 and 3.** Both scripts default to one fewer worker than the number of logical CPUs (`cpu_count() - 1`), so a 24-thread computer launches 23 processes. Many simultaneous netCDF reads and raster transfers can use substantial memory and disk bandwidth; each spawned worker can also create a separate log file. For large runs, start with `--workers 2` or `--workers 4` and increase only after checking resource use. `run_pipeline.py` does not pass `--workers` to these steps, so run Steps 2 and 3 individually when you need to limit the worker count.


### Step 4 — Visualize (`04_visualize_swe.py`)

1. **Time series plot** — Single-year line plot with fill, or multi-year overlay on a day-of-water-year axis.
2. **Annual peak bar chart** — Bar chart comparing peak basin-mean values across water years.
3. **Peak spatial maps** — For each water year, identifies the date of maximum basin-mean value and plots the corresponding GeoTIFF as a spatial map.
4. **Heatmap** — A 2-D heatmap with day-of-water-year on the x-axis and water year on the y-axis, showing basin-mean intensity. Provides a compact visual summary of snowpack timing and magnitude across all years.

All axis labels, color bar labels, and titles reflect the selected `--units` value.

**Output:** PNG files in `data/figures/`.

### Step 5 — HEC-DSS Export (`05_create_swe_dss.py`, standalone)

Builds one HEC-DSS file of daily SWE grids for a buffered watershed from the **original netCDF files downloaded in Step 1**. It does not use the GeoTIFFs from Step 2 and is not run by `run_pipeline.py`. Step 2 deletes raw netCDFs by default, so keep them with `--keep_raw` (or download them again with Step 1) before running Step 5.

The processing follows Job Aid 5, Section 1.5 of the December 2025 FFRD SOP, which calls for gridded data to be imported to DSS with HEC-Vortex and clipped to the buffered modeling domain during import. For each day:

1. **HEC-Vortex reads** the `SWE` variable from the netCDF. The NAD83 definition stored in the file's `crs` variable is applied to the grid, because Vortex would otherwise assume a sphere.
2. **HEC-Vortex clips** to the envelope of the buffered watershed, **reprojects** to the Standard Hydrologic Grid (SHG, USA Contiguous Albers Equal Area, NAD83, meters), and **resamples** to the SHG cell size (default 1000 m, nearest neighbor). The grid origin is snapped to whole SHG cells.
3. **Masks the polygon.** Vortex clips to a rectangle only. Every cell that does not touch the buffered watershed polygon is set to no-data. Cells touched by any part of the polygon are kept. Source no-data (-999) also becomes DSS no-data.
4. **HEC-Vortex writes** the grid to DSS in millimeters as instantaneous data (`INST-VAL`).

Files are processed one calendar month at a time, with several files read in parallel (`--workers`). A progress file records each completed month, so an interrupted run resumes where it stopped. When the run finishes, the script checks that the DSS contains exactly one grid per input day, that units and cell size are correct, and that every cell outside the buffered watershed is no-data. HEC-Vortex then opens the finished file and reads the last grid back to confirm that it is recognized as instantaneous data.

#### Naming (FFRD SOP December 2025, Job Aid 6)

| Item | Convention | Upper Tennessee example |
|------|------------|-------------------------|
| File name | `data-source _ basin-name _ event-name _ data-type _ data-interval` | `ua_upper-tennessee_por_swe_1day.dss` |
| DSS A part | `SHG` + resolution | `SHG1K` |
| DSS B part | Basin name, upper case | `UPPER-TENNESSEE` |
| DSS C part | Data type | `SWE` |
| DSS D part | Grid time, `ddMMMYYYY:HHMM` | `14JAN2010:2400` |
| DSS E part | Empty for instantaneous data | |
| DSS F part | Data source | `UA` |

The file name uses the data-type `swe` and data-interval `1day` from Job Aid 6, Table 1, and follows the same pattern as the AORC POR files (`aorc_<basin>_por_precip_1hr.dss`). The event-name is `por` for the full record. When `--start` or `--end` is given it becomes `<start>to<end>`, e.g. `20100101to20100131`.

**Time stamps.** Each UA file holds one SWE state for its date, with a netCDF time coordinate at 00:00. The source time units do not specify a time zone; HEC-Vortex reads it as UTC. HEC-DSS writes midnight as 2400 of the previous day. The grid from `UA_SWE_Depth_800m_v1_20100115_stable.nc` is therefore stored as `/SHG1K/UPPER-TENNESSEE/SWE/14JAN2010:2400//UA/`. That is the same instant as 15 January 2010 00:00.

**Grid.** The default 1000 m SHG grid lines up cell-for-cell with the AORC period-of-record precipitation and temperature grids made for the same buffered boundary. The UA source cells are about 800 m, so `--cell_size` and `--resampling` (`near`, `bilinear`, `average`) are available if a different grid is needed. The DSS A part follows the cell size, e.g. `SHG500M` for 500 m.

**Missing days.** Days with no input file are not filled. They are listed in `<output>.missing_days.csv` and logged as a warning.

#### Requirements

- [HEC-Vortex](https://github.com/HydrologicEngineeringCenter/Vortex) 0.14.1 for Windows. The default path is set in `DEFAULT_VORTEX` near the top of the script; use `--vortex_home` if yours differs.
- `JPype1` and `hecdss` Python packages (in `requirements.txt`).

#### Run

```bash
# Check inputs, grid, and names without writing anything
python 05_create_swe_dss.py \
    --input_dir "data/raw/All_NC_Files" \
    --boundary "watersheds/upper-tennessee_huc04_10km_buffer/upper-tennessee_huc04_10km_buffer.shp" \
    --basin upper-tennessee \
    --output_dir "data/dss" \
    --dry_run

# Full period of record (remove --dry_run)
python 05_create_swe_dss.py --input_dir ... --boundary ... --basin upper-tennessee --output_dir ...

# A shorter window, e.g. for an event
python 05_create_swe_dss.py ... --start 2010-01-01 --end 2010-01-31
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--input_dir` | `data/raw` | Folder with `UA_SWE_Depth_800m_v1_*.nc` files, searched recursively (step 1 `WY` folders work). If a date appears twice, the most final release (stable > provisional > early) is used |
| `--boundary` | required | Buffered watershed polygon with a `.prj` file. Its buffer is used as is; no extra buffer is added |
| `--basin` | boundary file name | Lowercase SOP basin name, hyphens only, ≤ 32 characters |
| `--output_dir` | `data/dss` | Output folder for the DSS, progress file, and log |
| `--start`, `--end` | all files | Inclusive dates, `YYYY-MM-DD` |
| `--event_name` | `por` | Overrides the file name's event-name field |
| `--cell_size` | `1000` | SHG cell size in meters |
| `--resampling` | `near` | `near`, `bilinear`, or `average` |
| `--source` | `UA` | Data source in the file name and DSS F part |
| `--vortex_home` | see script | HEC-Vortex installation folder |
| `--workers` | `6` | Files read by HEC-Vortex at the same time |
| `--dry_run` | off | Report only |

The repository includes the Upper Tennessee buffered boundary at `watersheds/upper-tennessee_huc04_10km_buffer/`. Step 5 uses the polygon as supplied; it does not create a new buffer. The checked-in DSS was originally built with the identical shapefile at `F:/PrismCopy/PRISM-explorer/aorc-por-generator/buffered-watershed/upper-tennessee_huc04_10km_buffer.shp`. Its local `.progress.json` records that absolute path.

To rebuild a file with different settings, use a new output folder. The script will not overwrite a DSS made with other inputs, or one that has no progress file.

**Output:** `ua_<basin>_<event>_swe_1day.dss` in `data/dss/` (or `--output_dir`), with a `.progress.json` file, a `.missing_days.csv` file when days are missing, and a log file.

### Step 5b — Extend the DSS File (`05b_extend_swe_dss.py`, standalone)

Adds new days to the end of a DSS file made by Step 5 without rebuilding it. Grids already in the file are not rewritten. Use it when newer stable UA files become available.

1. Reads the DSS catalog to find the last date and the pathname parts (A, B, F), cell size, and grid position already in use.
2. Lists the days from the next date through `--end`. With `--download`, any of those days missing locally are downloaded from the UA server into `--input_dir`. **Only `stable` files are used.** Each download is checked against the server's file size and read once before it is kept.
3. Processes the new days exactly as Step 5 does: HEC-Vortex reads, clips, reprojects, resamples, and writes them, and cells outside the buffered watershed are no-data. Before writing, it checks that the new Vortex grid lines up with the grids in the file. It also checks that the boundary leaves the existing grids' no-data cells as they are.
4. Takes a SHA-256 fingerprint of every existing grid before the append and again after it. The run fails if any existing grid changed. Fingerprinting the 1981–2025 Upper Tennessee file takes about 5 minutes each time.
5. Confirms that the catalog holds exactly the old grids plus the new ones, checks units, cell size, and the polygon mask of the new grids, and has HEC-Vortex read the file. Step 5's progress file is updated to the new last date, so Step 5 still recognizes the file as complete.

Days with no stable file are skipped and added to `<output>.missing_days.csv`. The file name keeps its `por` event-name. A later Step 5b run begins after the latest grid already in the DSS, so it will not automatically backfill a stable file that appears for an earlier skipped date.

```bash
# See what would be added
python 05b_extend_swe_dss.py \
    --dss "data/dss/ua_upper-tennessee_por_swe_1day.dss" \
    --input_dir "data/raw/All_NC_Files" \
    --boundary "F:/PrismCopy/PRISM-explorer/aorc-por-generator/buffered-watershed/upper-tennessee_huc04_10km_buffer.shp" \
    --end 2026-01-31 --download --dry_run

# Download the stable files and append them (remove --dry_run)
python 05b_extend_swe_dss.py --dss ... --input_dir ... --boundary ... --end 2026-01-31 --download
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--dss` | required | Existing Step 5 DSS file to extend |
| `--input_dir` | `data/raw` | Folder with the UA netCDF files, searched recursively; downloads are saved here |
| `--boundary` | required | The same buffered watershed used to make the file |
| `--end` | required | Last date to add, `YYYY-MM-DD` |
| `--download` | off | Download missing stable files for the new dates first |
| `--vortex_home` | as Step 5 | HEC-Vortex installation folder |
| `--workers` | `6` | Files read by HEC-Vortex at the same time |
| `--dry_run` | off | Report only; nothing is downloaded or written |

For the existing `ua_upper-tennessee_por_swe_1day.dss`, pass the original `F:/PrismCopy/PRISM-explorer/aorc-por-generator/buffered-watershed/upper-tennessee_huc04_10km_buffer.shp` path to Step 5b when its local progress file is present. Step 5b compares the supplied path with the absolute path saved at build time. The repository copy is for reproducibility and new builds; it is not interchangeable with the original path for that existing progress file.

If a run is interrupted, run the same command again. It continues after the last grid in the file.

---

## Reprojection: Clip-Then-Reproject

The toolkit clips **before** reprojecting. This is a deliberate design choice with the following rationale:

1. **Efficiency** — Clipping first in the native geographic CRS reduces the number of pixels that need to be transformed. Reprojecting the full CONUS raster (~4000 × 8000 pixels) and then clipping to a small watershed would waste significant computation.

2. **Edge quality** — When bilinear or cubic resampling is applied during reprojection, the interpolated pixel values along watershed edges are computed only from neighboring source pixels that fall within or near the basin. If reprojection were done first on the full CONUS grid, the resampled grid would be misaligned with the original data, and clipping afterward could introduce subtle edge artifacts where interpolated values were influenced by distant, irrelevant pixels.

3. **Value preservation** — The SWE and DEPTH values are in mm. Bilinear resampling produces a spatially smooth interpolation of these values onto the new grid — it does not change the measurement unit or introduce systematic bias. If exact pixel-value preservation is needed (e.g., for categorical data), use `--resampling nearest`.

4. **Resampling options** — Three methods are available:
   - `bilinear`: Smooth interpolation for continuous fields like SWE.
   - `nearest`: Preserves exact pixel values, blockier appearance.
   - `cubic`: Smoother than bilinear but can introduce slight overshoot/undershoot.

5. **Unit conversion timing** — The mm → inches conversion is applied **after** reprojection. This ensures that the resampling interpolation operates on the native mm values, and the unit conversion is a simple linear scaling that does not affect interpolation quality.

---

## Metadata Log File

Every pipeline run produces a timestamped log file in the `data/` directory:

```
data/ua_swe_toolkit_20250213_143022.log.txt
```

The log captures:
- Start/end timestamps for each pipeline step
- Per-step elapsed time (seconds, minutes, or hours)
- File counts (downloaded, skipped, failed, processed, deleted)
- Warnings and errors
- Configuration details (watershed path, variable, units, resampling method)

This file serves as a processing metadata record and can be archived alongside the output data.

---

## Directory Structure

```
ua_swe_toolkit/
├── config.py               # All configuration (URLs, CRS, paths, units, defaults)
├── utils.py                # Shared utilities (logging, timing, filename parsing)
├── 01_download_ua_swe.py   # Step 1: Download netCDFs from UA server
├── 02_clip_to_watershed.py # Step 2: Clip to watershed → GeoTIFF (with unit conversion)
├── 03_process_swe.py       # Step 3: Basin-mean CSV + monthly aggregation
├── 04_visualize_swe.py     # Step 4: Spatial maps + time series + heatmap
├── 05_create_swe_dss.py    # Step 5 (standalone): HEC-DSS export with HEC-Vortex
├── 05b_extend_swe_dss.py   # Step 5b (standalone): append new stable days to a step 5 DSS
├── run_pipeline.py         # End-to-end pipeline orchestrator (steps 1–4)
├── requirements.txt        # Python dependencies
├── .gitignore
├── watersheds/             # Buffered Upper Tennessee shapefile and sidecars
└── data/                   # Only the validated DSS is tracked (Git LFS)
    ├── raw/                # Downloaded netCDFs (temporary, always in mm)
    ├── clipped/            # Clipped GeoTIFFs organized by WY (in selected units)
    ├── processed/          # Basin-mean CSVs + monthly GeoTIFFs (in selected units)
    ├── figures/            # PNG visualizations (labeled in selected units)
    ├── dss/                # Tracked Upper Tennessee DSS (always in mm); logs/progress ignored
    └── *.log.txt           # Timestamped metadata log files
```

---

## Tracked DSS and local source files

The validated Upper Tennessee DSS (`data/dss/ua_upper-tennessee_por_swe_1day.dss`) is tracked with Git LFS. Original netCDFs remain in the ignored `data/raw/` folder and are not included in this repository. Other generated outputs, DSS progress files, and logs are also ignored. Step 1 can download the source files again. Step 5 searches `data/raw/` recursively and creates the DSS from the original netCDFs, not the GeoTIFFs made by Steps 2 through 4.

---

## Installation

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/ua_swe_toolkit.git
cd ua_swe_toolkit

# Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

# Install dependencies
pip install -r requirements.txt
```

### Dependencies

| Package              | Minimum Version | Purpose                              |
|----------------------|-----------------|--------------------------------------|
| xarray               | 2024.1          | NetCDF reading, time resampling      |
| dask                 | 2024.1          | Parallel / chunked processing        |
| netCDF4              | 1.6             | NetCDF backend + CF time decoding    |
| rioxarray            | 0.15            | Raster clipping + GeoTIFF export     |
| rasterio             | 1.3             | Raster I/O and reprojection          |
| geopandas            | 0.14            | Watershed geometry handling           |
| pyproj               | 3.6             | CRS transforms                       |
| pandas               | 2.1             | Tabular data, time series            |
| numpy                | 1.24            | Numerical operations                 |
| matplotlib           | 3.8             | Plotting                             |
| requests             | 2.31            | HTTP downloads                       |
| JPype1               | 1.5             | Step 5: runs HEC-Vortex (Java)       |
| hecdss               | 0.1.29          | Step 5: DSS catalog validation       |

Step 5 also needs a local HEC-Vortex installation (see [Step 5](#step-5--hec-dss-export-05_create_swe_dsspy-standalone)).

---

## Quick Start

### Run the full pipeline (default: mm)

```bash
python run_pipeline.py \
    --watershed path/to/watershed.shp \
    --wy_start 2020 \
    --wy_end 2020
```

### Run the full pipeline in inches

```bash
python run_pipeline.py \
    --watershed path/to/watershed.shp \
    --wy_start 2020 \
    --wy_end 2020 \
    --units inches
```

This single command will:
1. Download all daily netCDFs for WY2020 from the UA server
2. Clip each file to your watershed, reproject, and convert to inches
3. Delete the raw netCDFs to save disk space
4. Compute daily basin-mean SWE (in inches) and export as CSV
5. Generate time series plots, spatial maps, and heatmap — all labeled in inches
6. Save a metadata log file with timing for each step

### Run steps individually

```bash
# Step 1: Download WY2020 data (no --units needed; raw files are always in mm)
python 01_download_ua_swe.py --wy_start 2020 --wy_end 2020

# Step 2: Clip to watershed → GeoTIFFs in inches
python 02_clip_to_watershed.py \
    --watershed path/to/watershed.shp \
    --wy_start 2020 --wy_end 2020 \
    --units inches

# Step 3: Compute basin-mean time series (pass same --units)
python 03_process_swe.py --wy_start 2020 --wy_end 2020 --units inches

# Step 4: Generate plots (pass same --units)
python 04_visualize_swe.py --wy_start 2020 --wy_end 2020 --units inches
```

---

## Usage Examples

### Download only stable (finalized) files
```bash
python 01_download_ua_swe.py --wy_start 2015 --wy_end 2023 --status stable
```

### Keep raw netCDFs after clipping
```bash
python 02_clip_to_watershed.py \
    --watershed basin.gpkg \
    --wy_start 2020 --wy_end 2020 \
    --keep_raw
```

### Process snow depth in inches
```bash
python run_pipeline.py \
    --watershed watershed.shp \
    --wy_start 2020 --wy_end 2020 \
    --variable DEPTH \
    --units inches
```

### Generate monthly-mean GeoTIFFs
```bash
python 03_process_swe.py --wy_start 2020 --wy_end 2020 --monthly
```

### Multi-year comparison (overlays all years + heatmap)
```bash
python 04_visualize_swe.py --wy_start 2015 --wy_end 2024 --units mm --dpi 300
```

### Skip previously completed steps
```bash
python run_pipeline.py \
    --watershed watershed.shp \
    --wy_start 2020 --wy_end 2020 \
    --units inches \
    --skip_download --skip_clip
```

---

## Output CRS

All GeoTIFFs are reprojected to **USA Contiguous Albers Equal Area Conic** (NAD83):

| Parameter            | Value           |
|----------------------|-----------------|
| Projection           | Albers Conic Equal Area |
| Datum                | NAD83 (GRS 1980)|
| Central Meridian     | -96.0°          |
| Standard Parallel 1  | 29.5°           |
| Standard Parallel 2  | 45.5°           |
| Latitude of Origin   | 23.0°           |
| Linear Unit          | Foot (0.3048 m) |
| False Easting        | 0.0             |
| False Northing       | 0.0             |

This CRS matches the WKT defined in `config.py` and can be modified there if a different target projection is needed.

---

## Dataset Details

| Attribute            | Value                                                        |
|----------------------|--------------------------------------------------------------|
| Source                | University of Arizona, Climate Research Lab                  |
| URL                   | https://climate.arizona.edu/data/UA_SWE/DailyData_800m/     |
| Spatial Resolution    | 800 m                                                        |
| Temporal Resolution   | Daily                                                        |
| Coverage              | CONUS                                                        |
| Period                | WY1982 (Oct 1981) – present                                 |
| Native CRS            | NAD83 / EPSG:4269                                            |
| Variables             | `SWE` (mm), `DEPTH` (mm)                                    |
| File Naming           | `UA_SWE_Depth_800m_v1_YYYYMMDD_{status}.nc`                |
| Status Labels         | `early` (current month), `provisional` (1–6 mo), `stable` (>6 mo) |
| Water Year Definition | Oct 1 of prior year through Sep 30                          |

---

## Configuration

All settings are centralized in `config.py`:

```python
UA_SWE_BASE_URL     # Server URL
SOURCE_CRS          # Native CRS (EPSG:4269)
TARGET_CRS_WKT      # Output CRS (Albers, WKT string)
DIR_RAW             # Raw download directory
DIR_CLIPPED         # Clipped GeoTIFF directory
DIR_DSS             # HEC-DSS output directory (step 5)
DIR_LOG             # Log file output directory
DEFAULT_UNITS       # Default unit system ("mm" or "inches")
DELETE_RAW_NETCDF   # Auto-delete raw files (True/False)
DOWNLOAD_RETRIES    # Retry count for failed downloads
RESAMPLING_METHOD   # Reprojection resampling (bilinear/nearest/cubic)
NODATA_VALUE        # NoData value for GeoTIFFs (-9999.0)
MM_TO_INCH          # Conversion factor (1/25.4)
```

To change the default unit system for all runs, edit `DEFAULT_UNITS` in `config.py`. The `--units` CLI flag overrides this default.

---

## License

This toolkit is provided under the MIT License. The UA SWE dataset is produced by the University of Arizona and is subject to their data use policies.

---

## Citation

If you use this toolkit or the UA SWE data in your work, please cite the dataset:

> Broxton, P., X. Zeng, and N. Dawson, 2023: Daily 4 km Gridded SWE and Snow Depth from Assimilated In-Situ and Modeled Data over the Conterminous US, Version 1. University of Arizona.

---

## Developer

[HydroMohsen](https://hydromohsen.com) for the water resources engineering community.
