# -*- coding: utf-8 -*-
"""A QGIS CRS as WKT for GDAL/OGR."""

from qgis.core import Qgis


def gdal_wkt(crs):
    """The WKT of a QGIS CRS for GDAL/OGR, with the axis order in it.

    QGIS's default WKT (WKT1) leaves out the axes of some CRSs, and GDAL then
    reads the coordinates in another order: for S-JTSK / Krovak (EPSG:5513,
    X to the south, Y to the west) a point in Czechia was read as one in
    northern Russia. WKT2 carries the axes, so GDAL reads the coordinates as
    QGIS gives them.
    """
    try:
        return crs.toWkt(Qgis.CrsWktVariant.Wkt2_2019)
    except (AttributeError, TypeError):
        return crs.toWkt()
