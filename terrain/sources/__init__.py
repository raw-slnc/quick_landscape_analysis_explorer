# -*- coding: utf-8 -*-
"""The DEM sources, one file for each country (or kind), holding what is
particular to each source: its name and region, the note under the source
list, the facts recorded with an analysis, its coverage mesh and region
outline, and the words the window uses about it. The work is done by the
rest of the plugin, which looks them up here.

To add a source: add it to its country's file (or a new file, listed in
COUNTRIES in the order of the source list), and its fetching to the job.

A country file has:
- CODE, NAME: the ISO code and (English, Japanese) name, for the region
  shown at the top of the window (None for local and global sources);
- GROUPS: the groups of the source list, each a dict with ``label``
  (English, Japanese), ``region`` (WGS84 lon_min, lat_min, lon_max, lat_max;
  None = anywhere) and ``sources``, a list of dicts:
    key        stored in analyses and window state: keep it stable
    label      (English, Japanese)
    place      None (the whole country) or the region's (English, Japanese)
    note       lines (English, Japanese) under the source list, at most
               three, each short enough not to wrap
    facts      recorded with an analysis (provider, dataset, licence,
               attribution, credit, service), where fixed
    coverage   the coverage mesh (see terrain/coverage.py), or absent
    outline    the region outline: ("country", ISO code, WGS84 box of the
               parts kept) or ("file",) for terrain/data/regions/<key>.json
    ask_update (English, Japanese) HTML asking before a coverage update,
    updated    (English, Japanese) when the update is done
- OPTION (optional): the country's regional analysis, offered as one check
  under the analysis checks when the preview is in the country:
    key        recorded with the analysis
    label      (English, Japanese), short (the check's column is narrow)
    tooltip    (English, Japanese)
    route      the module in this folder that does it (its own fetching,
               processing and outputs); the check is shown only when the
               module is there. Its run(folder, info, context) writes its
               files into ``folder`` (regional/<key>/ in the analysis ZIP;
               info: "outputs", the basic analysis's members, "root",
               the folder they are in, and "folder_member") and returns
               {"layers": [{"name": (English,
               Japanese), "member", "style", "visible", optionally "role":
               "base_map" or "excludes": a basic output it is shown
               instead of, or a role of its own made exclusive in
               management_panel.EXCLUSIVE_OUTPUTS}], "credit": (English,
               Japanese)}; the plugin puts the layers in a group of their own
    sources    optional: the DEM source keys it is made for; with another
               source the check is greyed (absent: any source)
    geopdf_fill_holes  True: the DEM for the GeoPDF starts from the DEM
               with its holes filled from around them (the region's stage;
               the later stages are the same everywhere)
  The analysis runs it together with the basic analyses; what to show (for
  example its colouring or the basic one) is chosen in analysis management.
"""

from . import france, germany, japan, local, netherlands, new_zealand, world

# In the order of the source list.
COUNTRIES = (local, japan, netherlands, france, germany, new_zealand, world)

SOURCES = {
    source["key"]: dict(source, country=country.CODE)
    for country in COUNTRIES
    for group in country.GROUPS
    for source in group["sources"]
}

COUNTRY_NAMES = {
    country.CODE: country.NAME for country in COUNTRIES if country.CODE
}


def get(key):
    """The settings of a source key ({} when unknown)."""
    return SOURCES.get(key or "", {})


def groups():
    """The groups of the source list, in order."""
    return [group for country in COUNTRIES for group in country.GROUPS]


def regional_option(code):
    """The regional analysis of a country (ISO code) whose route is in this
    folder, or None."""
    import importlib.util

    for country in COUNTRIES:
        option = getattr(country, "OPTION", None)
        if country.CODE != code or not option:
            continue
        if (
            importlib.util.find_spec("{}.{}".format(__name__, option["route"]))
            is None
        ):
            return None
        return option
    return None


def option_by_key(key):
    """(the regional analysis of that key, its route module), or (None, None).

    The route is imported with the import statement's own function, not
    importlib: QGIS records the modules a plugin imports that way and drops
    them when the plugin is reloaded; one imported by importlib stayed, old,
    until QGIS was restarted."""
    for country in COUNTRIES:
        option = getattr(country, "OPTION", None)
        if option and option["key"] == key:
            try:
                return option, __import__(
                    "{}.{}".format(__name__, option["route"]), fromlist=["run"]
                )
            except ImportError:
                return option, None
    return None, None
