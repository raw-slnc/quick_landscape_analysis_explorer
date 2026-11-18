# -*- coding: utf-8 -*-
"""Catalogue of DEM sources offered in the DEM section, made from the
country files in terrain/sources/.

Keys are stored in analysis manifests and window state, so keep them stable.
Labels are (English, Japanese); the dialog picks one with ``tr``.
Regions are WGS84 (lon_min, lat_min, lon_max, lat_max); ``None`` = anywhere.
"""

from . import sources

# (group label, region, [(key, label)]), from the country files.
GROUPS = [
    (group["label"], group["region"], [(source["key"], source["label"]) for source in group["sources"]])
    for group in sources.groups()
]

DEFAULT_KEY = "gsi:best"

# Countries with a regional source, by ISO code: their names here are used
# in place of the English names of QGIS's world map.
COUNTRY_NAMES = dict(sources.COUNTRY_NAMES)

# Country (ISO code) and region within it shown in the window for a regional
# network source (by key kind); None = the whole country.
SOURCE_REGIONS = {
    key.partition(":")[0]: (source["country"], source.get("place"))
    for key, source in sources.SOURCES.items() if source.get("country")
}


def country_for(key):
    """ISO code of the country of a regional network source, or None."""
    found = SOURCE_REGIONS.get((key or "").partition(":")[0])
    return found[0] if found else None


def source_region(key):
    """(country (English, Japanese), region (English, Japanese) or None) of a
    regional network source, or None for other sources."""
    found = SOURCE_REGIONS.get((key or "").partition(":")[0])
    if found is None:
        return None
    code, region = found
    return COUNTRY_NAMES[code], region


def region_for(key):
    for _group, region, items in GROUPS:
        if any(item_key == key for item_key, _label in items):
            return region
    return None


def overlaps(region, bbox84):
    """Whether a WGS84 bbox touches a region (None region = everywhere)."""
    if region is None or bbox84 is None:
        return True
    return not (
        bbox84[2] < region[0] or bbox84[0] > region[2]
        or bbox84[3] < region[1] or bbox84[1] > region[3]
    )


def key_from_legacy(kind, dataset=None):
    """Map older state/manifest values (kind + GSI dataset) to a key."""
    if kind == "gsi":
        return "gsi:" + (dataset or "best")
    if kind == "terrarium":
        return "terrarium:" + (dataset or "5m")
    if kind == "local":
        return "local"
    return kind or DEFAULT_KEY
