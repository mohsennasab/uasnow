"""
Configuration for the UA SWE Toolkit.

University of Arizona Snow Water Equivalent (SWE) 800m Daily Dataset
Source: https://climate.arizona.edu/data/UA_SWE/DailyData_800m/
"""

# ─────────────────────────────────────────────────────────────────────────────
# UA SWE Data Server
# ─────────────────────────────────────────────────────────────────────────────
UA_SWE_BASE_URL = "https://climate.arizona.edu/data/UA_SWE/DailyData_800m"

# Available water years on the server (WY starts Oct 1 of previous calendar year)
FIRST_WATER_YEAR = 1982
LATEST_WATER_YEAR = 2026  # update as new WYs appear on the server

# ─────────────────────────────────────────────────────────────────────────────
# Coordinate Reference Systems
# ─────────────────────────────────────────────────────────────────────────────
# Source CRS of the UA SWE netCDF files (NAD83 geographic)
SOURCE_CRS = "EPSG:4269"

# Target CRS for output GeoTIFFs
# USA Contiguous Albers Equal Area Conic (NAD83), linear unit = US survey foot
TARGET_CRS_WKT = (
    'PROJCS["USA_Contiguous_Albers_Equal_Area_Conic",'
    'GEOGCS["GCS_North_American_1983",'
    'DATUM["D_North_American_1983",'
    'SPHEROID["GRS_1980",6378137.0,298.257222101]],'
    'PRIMEM["Greenwich",0.0],'
    'UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Albers_Conic_Equal_Area"],'
    'PARAMETER["False_Easting",0.0],'
    'PARAMETER["False_Northing",0.0],'
    'PARAMETER["Central_Meridian",-96.0],'
    'PARAMETER["Standard_Parallel_1",29.5],'
    'PARAMETER["Standard_Parallel_2",45.5],'
    'PARAMETER["Latitude_Of_Origin",23.0],'
    'UNIT["Foot",0.3048]]'
)

# ─────────────────────────────────────────────────────────────────────────────
# Directory Structure
# ─────────────────────────────────────────────────────────────────────────────
DIR_RAW = "data/raw"           # downloaded netCDF files (temporary)
DIR_CLIPPED = "data/clipped"   # clipped GeoTIFFs per watershed
DIR_PROCESSED = "data/processed"  # aggregated / resampled outputs
DIR_FIGURES = "data/figures"   # visualization outputs
DIR_DSS = "data/dss"           # HEC-DSS grids from step 5
DIR_LOG = "data"               # log file output directory

# ─────────────────────────────────────────────────────────────────────────────
# Processing Defaults
# ─────────────────────────────────────────────────────────────────────────────
DELETE_RAW_NETCDF = True       # remove raw netCDF after clipping to save disk
DOWNLOAD_RETRIES = 3           # number of retry attempts for failed downloads
DOWNLOAD_TIMEOUT = 60          # seconds per request timeout
RESAMPLING_METHOD = "nearest"  # resampling for reprojection: nearest, bilinear, cubic
NODATA_VALUE = -9999.0         # NoData value for output GeoTIFFs
DEFAULT_UNITS = "mm"           # default output unit system: "mm" or "inches"

# ─────────────────────────────────────────────────────────────────────────────
# Unit Conversions
# ─────────────────────────────────────────────────────────────────────────────
# The UA SWE dataset stores values in millimeters (mm) natively.
# When --units inches is selected, the conversion is applied at every output
# stage: GeoTIFFs, CSVs, and visualizations.
MM_TO_INCH = 1.0 / 25.4


def unit_label(variable: str, units: str) -> str:
    """Return a human-readable label like 'SWE (mm)' or 'DEPTH (inches)'."""
    return f"{variable} ({units})"


def unit_conversion_factor(units: str) -> float:
    """
    Return the multiplication factor to convert from native mm to the
    requested unit system. Returns 1.0 for 'mm', MM_TO_INCH for 'inches'.
    """
    if units == "inches":
        return MM_TO_INCH
    return 1.0
