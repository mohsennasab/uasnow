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
- **Single pipeline command** — `run_pipeline.py` chains all steps end-to-end

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

The toolkit follows a four-step pipeline. Each step can be run independently or chained via `run_pipeline.py`.

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
5. **Reprojects** the clipped raster to the target Albers Equal Area Conic CRS using the configured resampling method (default: bilinear).
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

### Step 4 — Visualize (`04_visualize_swe.py`)

1. **Time series plot** — Single-year line plot with fill, or multi-year overlay on a day-of-water-year axis.
2. **Annual peak bar chart** — Bar chart comparing peak basin-mean values across water years.
3. **Peak spatial maps** — For each water year, identifies the date of maximum basin-mean value and plots the corresponding GeoTIFF as a spatial map.
4. **Heatmap** — A 2-D heatmap with day-of-water-year on the x-axis and water year on the y-axis, showing basin-mean intensity. Provides a compact visual summary of snowpack timing and magnitude across all years.

All axis labels, color bar labels, and titles reflect the selected `--units` value.

**Output:** PNG files in `data/figures/`.

---

## Reprojection: Clip-Then-Reproject

The toolkit clips **before** reprojecting. This is a deliberate design choice with the following rationale:

1. **Efficiency** — Clipping first in the native geographic CRS reduces the number of pixels that need to be transformed. Reprojecting the full CONUS raster (~4000 × 8000 pixels) and then clipping to a small watershed would waste significant computation.

2. **Edge quality** — When bilinear or cubic resampling is applied during reprojection, the interpolated pixel values along watershed edges are computed only from neighboring source pixels that fall within or near the basin. If reprojection were done first on the full CONUS grid, the resampled grid would be misaligned with the original data, and clipping afterward could introduce subtle edge artifacts where interpolated values were influenced by distant, irrelevant pixels.

3. **Value preservation** — The SWE and DEPTH values are in mm. Bilinear resampling produces a spatially smooth interpolation of these values onto the new grid — it does not change the measurement unit or introduce systematic bias. If exact pixel-value preservation is needed (e.g., for categorical data), use `--resampling nearest`.

4. **Resampling options** — Three methods are available:
   - `bilinear` (default): Smooth interpolation, best for continuous fields like SWE.
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
├── run_pipeline.py         # End-to-end pipeline orchestrator
├── requirements.txt        # Python dependencies
├── .gitignore
└── data/                   # Created at runtime (not tracked in Git)
    ├── raw/                # Downloaded netCDFs (temporary, always in mm)
    ├── clipped/            # Clipped GeoTIFFs organized by WY (in selected units)
    ├── processed/          # Basin-mean CSVs + monthly GeoTIFFs (in selected units)
    ├── figures/            # PNG visualizations (labeled in selected units)
    └── *.log.txt           # Timestamped metadata log files
```

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
