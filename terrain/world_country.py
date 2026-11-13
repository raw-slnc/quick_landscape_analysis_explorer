# -*- coding: utf-8 -*-
"""Country at a point, from the world map that ships with QGIS.

QGIS keeps ``resources/data/world_map.gpkg`` (used by its own CRS preview).
Its ``countries`` layer gives an ISO code (``iso_a2``) and an English name
(``NAME``). When the file or the fields are missing, nothing is returned and
the caller shows only the coordinates.
"""

import os
import warnings

from qgis.core import QgsApplication

_cache = {}


def _countries():
    """(data source, layer, code field index, name field index), or None."""

    if "countries" not in _cache:
        found = None
        try:
            from osgeo import ogr

            path = os.path.join(
                QgsApplication.pkgDataPath(), "resources", "data", "world_map.gpkg"
            )
            source = None
            if os.path.exists(path):
                # GDAL warns when no exception mode was chosen; the global
                # mode is shared with QGIS and other plugins, so it is left
                # as it is and only this warning is silenced.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", FutureWarning)
                    source = ogr.Open(path)
            layer = source.GetLayer("countries") if source is not None else None
            if layer is not None:
                definition = layer.GetLayerDefn()
                code = definition.GetFieldIndex("iso_a2")
                name = definition.GetFieldIndex("NAME")
                if code >= 0 and name >= 0:
                    found = (source, layer, code, name)
        except Exception:  # noqa: BLE001 - any failure: no country names
            found = None
        _cache["countries"] = found
    return _cache["countries"]


def country_at(lon, lat):
    """Return (ISO code, English name) of the country at a WGS84 point."""

    countries = _countries()
    if countries is None:
        return None
    _source, layer, code_index, name_index = countries
    try:
        from osgeo import ogr

        point = ogr.Geometry(ogr.wkbPoint)
        point.AddPoint_2D(float(lon), float(lat))
        layer.SetSpatialFilter(point)
        try:
            layer.ResetReading()
            for feature in layer:
                geometry = feature.GetGeometryRef()
                if geometry is not None and geometry.Intersects(point):
                    name = feature.GetField(name_index)
                    if name:
                        return feature.GetField(code_index), name
        finally:
            layer.SetSpatialFilter(None)
    except Exception:  # noqa: BLE001 - any failure: no country name
        return None
    return None


# The world map's borders are simplified: a country counts as touched within
# about 3 km of its border, so a source is not hidden near a border.
COUNTRY_MARGIN_DEG = 0.03


def touches_country(code, bbox84, margin=COUNTRY_MARGIN_DEG):
    """Whether a WGS84 bbox comes near a country (ISO code).

    None when it cannot be told (no world map); the caller then keeps the
    source available.
    """

    countries = _countries()
    if countries is None or not code or not code.isalpha():
        return None
    _source, layer, _code_index, _name_index = countries
    try:
        from osgeo import ogr

        x0, y0, x1, y1 = (
            bbox84[0] - margin, bbox84[1] - margin, bbox84[2] + margin, bbox84[3] + margin
        )
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)):
            ring.AddPoint_2D(x, y)
        box = ogr.Geometry(ogr.wkbPolygon)
        box.AddGeometry(ring)
        layer.SetAttributeFilter("iso_a2 = '{}'".format(code))
        layer.SetSpatialFilterRect(x0, y0, x1, y1)
        try:
            layer.ResetReading()
            for feature in layer:
                geometry = feature.GetGeometryRef()
                if geometry is not None and geometry.Intersects(box):
                    return True
        finally:
            layer.SetAttributeFilter(None)
            layer.SetSpatialFilter(None)
    except Exception:  # noqa: BLE001 - any failure: cannot tell
        return None
    return False


def lat_lon_text(lon, lat):
    """Decimal degrees, latitude first, with N/S and E/W (5 decimals, ~1 m)."""

    return "{:.5f}°{}, {:.5f}°{}".format(
        abs(lat), "N" if lat >= 0 else "S", abs(lon), "E" if lon >= 0 else "W"
    )
