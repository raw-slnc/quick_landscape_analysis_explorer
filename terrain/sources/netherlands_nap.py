# -*- coding: utf-8 -*-
"""The Netherlands' regional analysis: the DEM coloured as PDOK shows the
AHN DTM (the default style of the dtm_05m layer of PDOK's AHN WMS): fixed
classes from -8 m to 350 m (NAP), finest near NAP (0.5 m from -3 m to 5 m),
from dark blue through green and yellow to orange and red-brown.

No new data: a style for the basic analysis's DEM. The classes and colours
were read from PDOK's legend (GetLegendGraphic, dtm_05m, style "default").
"""

import os

from ..styles import SLOPE_COLORS, write_discrete_qml

# Upper value of each class (the lowest class starts at -8 m) and its colour.
CLASSES = (
    (-7, "#082d7b"),
    (-6, "#08397b"),
    (-5, "#084684"),
    (-4, "#105284"),
    (-3, "#105e84"),
    (-2.5, "#106a8c"),
    (-2, "#18768c"),
    (-1.5, "#18838c"),
    (-1, "#189294"),
    (-0.5, "#219693"),
    (0, "#209e8b"),
    (0.5, "#18a283"),
    (1, "#18aa79"),
    (1.5, "#17b26a"),
    (2, "#10ba61"),
    (2.5, "#0fbe50"),
    (3, "#08c740"),
    (3.5, "#08cf31"),
    (4, "#07d02b"),
    (4.5, "#01db00"),
    (5, "#12df00"),
    (6, "#2be300"),
    (7, "#45e700"),
    (8, "#65eb00"),
    (9, "#7def00"),
    (10, "#97f300"),
    (12, "#b7f700"),
    (14, "#d1f700"),
    (16, "#f0fe00"),
    (18, "#fffa00"),
    (20, "#fff100"),
    (25, "#fee500"),
    (30, "#f7d600"),
    (35, "#f7ce08"),
    (40, "#f7c608"),
    (45, "#f7bd08"),
    (50, "#f6b410"),
    (60, "#efa510"),
    (70, "#efa110"),
    (80, "#ee9810"),
    (90, "#e78d18"),
    (100, "#e68421"),
    (125, "#dd7721"),
    (150, "#d66c29"),
    (175, "#d56829"),
    (200, "#ce6031"),
    (250, "#cd5831"),
    (300, "#c65439"),
    (350, "#c95942"),
)
LOWEST = -8


def _number(value):
    return "{:g}".format(value)


# Aerial photos for the base map: PDOK's newest photos (25 cm), over the
# preview (the GeoPDF's area). Fine enough to print (the long side at most
# ORTHO_MAX_PX, about A3 at 300 dpi) and never finer than the photo.
PHOTO_WMS = (
    "https://service.pdok.nl/hwh/{service}/wms/v1_0?SERVICE=WMS&VERSION=1.3.0"
    "&REQUEST=GetMap&LAYERS={layer}&STYLES=&CRS=EPSG:28992"
    "&BBOX={x0},{y0},{x1},{y1}&WIDTH={w}&HEIGHT={h}&FORMAT=image/jpeg"
)
PHOTOS = {
    # key: (service, layer of the newest year, year layers' suffix, file,
    # progress label). The year layers (2025_ortho25, ...) are used, the
    # newest one, so its year is known ("Actueel" is the newest year too,
    # but does not name it).
    "orthophoto": (
        "luchtfotorgb",
        "Actueel_ortho25",
        "_ortho25",
        "orthophoto.tif",
        "Orthophoto",
    ),
    # Colour infrared: living plants red; taken in early spring in recent
    # years, so evergreen trees stand out from bare broadleaves.
    "cir": (
        "luchtfotocir",
        "Actueel_ortho25IR",
        "_ortho25IR",
        "orthophoto_cir.tif",
        "Orthophoto CIR",
    ),
}
PHOTO_CAPABILITIES = (
    "https://service.pdok.nl/hwh/{service}/wms/v1_0?SERVICE=WMS"
    "&REQUEST=GetCapabilities&VERSION=1.3.0"
)
ORTHO_SERVICE = "https://service.pdok.nl/hwh/luchtfotorgb/wms/v1_0"
CIR_SERVICE = "https://service.pdok.nl/hwh/luchtfotocir/wms/v1_0"
ORTHO_CELL_M = 0.25
ORTHO_MAX_PX = 5000
ORTHO_REQUEST_PX = 2500  # the most the service sends at once
_USER_AGENT = "Quick Landscape Analysis Explorer (QGIS plugin)"

# The geomorphological map (BRO, 1:50,000, CC0): offered only as one file
# for the whole country (about 213 MB, 516 MB unpacked), fetched once and
# kept (the QLAE folder when one is chosen, else the QGIS profile); each
# analysis keeps the landforms over its preview.
GMM_URL = ('https://service.pdok.nl/tno/bro-geomorfologische-kaart/atom/do'
           'wnloads/bro-geomorfologischekaart.zip')
GMM_FILE = "bro-geomorfologischekaart.gpkg"
GMM_LAYER = "geomorphological_area"
GMM_STYLE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "data",
    "styles",
    "nl_geomorphology.qml",
)


def _amount(size):
    return (
        "{:.2f} GB".format(size / 1e9)
        if size >= 1e9
        else "{:.1f} MB".format(size / 1e6)
    )


def _ortho_box(view):
    """The preview's area in RD New (EPSG:28992), or None."""
    from osgeo import ogr, osr

    extent = view.get("extent")
    if not extent or not view.get("crs_wkt"):
        return None
    source = osr.SpatialReference()
    source.ImportFromWkt(view["crs_wkt"])
    source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    target = osr.SpatialReference()
    target.ImportFromEPSG(28992)
    target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    xmin, ymin, xmax, ymax = (float(value) for value in extent)
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in (
        (xmin, ymin),
        (xmax, ymin),
        (xmax, ymax),
        (xmin, ymax),
        (xmin, ymin),
    ):
        ring.AddPoint_2D(x, y)
    area = ogr.Geometry(ogr.wkbPolygon)
    area.AddGeometry(ring)
    area.Segmentize(max(xmax - xmin, ymax - ymin) / 20.0)
    area.AssignSpatialReference(source)
    if area.TransformTo(target) != 0:
        return None
    x0, x1, y0, y1 = area.GetEnvelope()
    return x0, y0, x1, y1


def _download(url, path, cancelled, on_bytes, timeout=120):
    """Stream ``url`` to ``path``; on_bytes(received, total or None) as it
    goes. False when cancelled."""
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    got = 0
    with (
        urllib.request.urlopen(  # nosec B310
            request, timeout=timeout
        ) as response,
        open(path, "wb") as out,
    ):
        total = response.headers.get("Content-Length")
        total = int(total) if total else None
        kind = response.headers.get("Content-Type") or ""
        while True:
            if cancelled():
                return False, kind
            chunk = response.read(65536)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            on_bytes(got, total)
    return True, kind


def _fetch_photo(key, folder, box, progress, cancelled):
    """One of PHOTOS over ``box``, in requests of at most ORTHO_REQUEST_PX;
    the progress line gives the tiles done and the data received over the
    data expected. Its record, or None when cancelled."""
    import math

    from osgeo import gdal

    service, newest, suffix, file_name, label = PHOTOS[key]
    layer, year = _newest_year_layer(service, newest, suffix)
    x0, y0, x1, y1 = box
    long_m = max(x1 - x0, y1 - y0)
    pixels = min(ORTHO_MAX_PX, int(math.ceil(long_m / ORTHO_CELL_M)))
    cell = long_m / pixels
    width = max(1, int(round((x1 - x0) / cell)))
    height = max(1, int(round((y1 - y0) / cell)))
    columns = int(math.ceil(width / float(ORTHO_REQUEST_PX)))
    rows = int(math.ceil(height / float(ORTHO_REQUEST_PX)))
    total = columns * rows
    parts = []
    state = {"received": 0, "done": 0}

    def show(current):
        done = state["done"]
        expected = (
            "?" if done == 0 else _amount(state["received"] / done * total)
        )
        progress(
            "{}: {}/{} tiles, {} / {}".format(
                label,
                done,
                total,
                _amount(state["received"] + current),
                expected,
            )
        )

    show(0)
    for row in range(rows):
        for column in range(columns):
            if cancelled():
                return None
            px0, px1 = (
                column * ORTHO_REQUEST_PX,
                min(width, (column + 1) * ORTHO_REQUEST_PX),
            )
            py0, py1 = (
                row * ORTHO_REQUEST_PX,
                min(height, (row + 1) * ORTHO_REQUEST_PX),
            )
            bx0, bx1 = x0 + px0 * cell, x0 + px1 * cell
            by1, by0 = y1 - py0 * cell, y1 - py1 * cell
            url = PHOTO_WMS.format(
                service=service,
                layer=layer,
                x0=bx0,
                y0=by0,
                x1=bx1,
                y1=by1,
                w=px1 - px0,
                h=py1 - py0,
            )
            path = os.path.join(
                folder, "_{}_{}_{}.jpg".format(key, row, column)
            )
            finished, kind = _download(
                url, path, cancelled, lambda got, _total: show(got)
            )
            if not finished:
                return None
            if "image" not in kind:
                raise RuntimeError(
                    "PDOK did not send an image ({}).".format(layer)
                )
            state["received"] += os.path.getsize(path)
            state["done"] += 1
            show(0)
            parts.append(
                gdal.Translate(
                    path + ".vrt",
                    path,
                    format="VRT",
                    outputSRS="EPSG:28992",
                    outputBounds=[bx0, by1, bx1, by0],
                )
            )
    mosaic = gdal.BuildVRT(os.path.join(folder, "_{}.vrt".format(key)), parts)
    parts = None
    gdal.Translate(
        os.path.join(folder, file_name),
        mosaic,
        format="GTiff",
        creationOptions=[
            "COMPRESS=JPEG",
            "JPEG_QUALITY=85",
            "PHOTOMETRIC=YCBCR",
            "TILED=YES",
        ],
    )
    mosaic = None
    for name in os.listdir(folder):
        if name.startswith("_" + key):
            os.remove(os.path.join(folder, name))
    return {
        "member": "{}/{}".format(info_member(folder), file_name),
        "tiles": total,
        "bytes": state["received"],
        "cell_m": round(cell, 3),
        "layer": layer,
        "year": year,
    }


def _newest_year_layer(service, newest, suffix):
    """(layer, year) of the newest year's photos in the service's list (as
    2025_ortho25); ("Actueel_...", None) when the list cannot be read."""
    import re
    import urllib.request

    request = urllib.request.Request(
        PHOTO_CAPABILITIES.format(service=service),
        headers={"User-Agent": _USER_AGENT},
    )
    try:
        # This service uses a fixed HTTPS URL.
        with urllib.request.urlopen(  # nosec B310
            request, timeout=60
        ) as response:
            text = response.read().decode("utf-8", "replace")
    except OSError:
        return newest, None
    years = sorted(
        {
            int(year)
            for year in re.findall(
                r"<Name>(\d{4})" + re.escape(suffix) + r"</Name>", text
            )
        }
    )
    if not years:
        return newest, None
    return "{}{}".format(years[-1], suffix), years[-1]


def info_member(folder):
    """regional/<key> of a folder inside the analysis's work folder."""
    parts = os.path.normpath(folder).split(os.sep)
    return "/".join(parts[-2:])


def _fetch_dsm(folder, box, progress, cancelled, kept=None):
    """AHN's DSM (buildings and trees on top) over ``box``, through the
    plugin's AHN fetching; its chunks kept for reuse in ``kept`` ((root,
    kind) of the plugin's tile store) like the DTM's. (Its path, chunks
    reused, chunks fetched), or None when cancelled."""
    from ...core import dem_store
    from .. import remote_dem

    spec = dict(
        remote_dem.REMOTE_SOURCES["ahn_dtm"],
        layer="dsm_05m",
        prefix="ahn_dsm",
        label="AHN4 DSM 0.5m",
    )

    class _Side:
        """The fetch's steps on the side line; a cancel stops it."""

        def check(self):
            if cancelled():
                raise remote_dem.RemoteDemError("cancelled")

        def report(self, text):
            # Named: the same fetching as the DEM's, whose line is above.
            progress("DSM: " + text)

        def cancelled(self):
            return cancelled()

    work = os.path.join(folder, "_dsm")
    os.makedirs(work, exist_ok=True)
    store = dem_store.TileStore(*kept) if kept else None
    try:
        path = remote_dem._fetch_ogc(_Side(), spec, box, work, store=store)
        return {
            "path": path,
            "reused": store.reused if store else 0,
            "downloaded": store.downloaded if store else 0,
        }
    except remote_dem.RemoteDemError:
        if cancelled():
            return None
        raise


def first_fetch_note(kept_folder):
    """What the first regional analysis fetches more than the later ones:
    the whole country's geomorphological map, until it is kept ((en, ja),
    or None once kept). ``kept_folder``: the QLAE folder, or ""."""
    target = os.path.join(
        _keep_folder({"kept_folder": kept_folder}),
        "nl_geomorphology",
        GMM_FILE,
    )
    if os.path.isfile(target):
        return None
    return (
        "The first time only, the geomorphological map of the whole country "
        "(about 213 MB) is fetched.",
        "初回のみ、地形分類図（全国、約213 MB）を取得します。",
    )


def _national_gmm(keep_folder, progress, cancelled):
    """The whole country's geomorphological map, fetched once (progress:
    the data received over its size) and kept; its path, or None."""
    import zipfile

    folder = os.path.join(keep_folder, "nl_geomorphology")
    target = os.path.join(folder, GMM_FILE)
    if os.path.isfile(target):
        return target
    os.makedirs(folder, exist_ok=True)
    archive = target + ".zip.part"

    def show(got, total):
        progress(
            ('Geomorphological map (whole country, first time '
             'only): {} / {}').format(
                _amount(got), _amount(total) if total else "?"
            )
        )

    finished, _kind = _download(GMM_URL, archive, cancelled, show, timeout=600)
    if not finished:
        os.remove(archive)
        return None
    progress("Geomorphological map: unpacking...")
    with zipfile.ZipFile(archive) as zipped:
        with (
            zipped.open(GMM_FILE) as source,
            open(target + ".part", "wb") as out,
        ):
            while True:
                chunk = source.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
    os.replace(target + ".part", target)
    os.remove(archive)
    return target


def _clip_gmm(national, folder, box):
    """The landforms touching ``box`` (the preview), whole, into the analysis;
    run() cuts them to the analysis area."""
    from osgeo import gdal

    target = os.path.join(folder, "geomorphology.gpkg")
    result = gdal.VectorTranslate(
        target,
        national,
        format="GPKG",
        layers=[GMM_LAYER],
        spatFilter=list(box),
    )
    count = result.GetLayer(0).GetFeatureCount() if result is not None else 0
    result = None
    return target, count


def _cut_to_area(path, info):
    """Cut the geomorphological map at ``path`` (fetched for the preview) to
    the analysis area, like the basic analysis's results: the analysis has
    nothing outside it. The landforms left, or None when the area is not
    known (left as it is)."""
    import shutil

    from osgeo import gdal, ogr, osr

    area_path = os.path.join(info.get("root") or "", "area", "aoi.wkt")
    if not info.get("area_crs_wkt") or not os.path.isfile(area_path):
        return None
    with open(area_path, encoding="utf-8") as stream:
        area = ogr.CreateGeometryFromWkt(stream.read())
    source = gdal.OpenEx(path, gdal.OF_VECTOR)
    layer_srs = source.GetLayer(0).GetSpatialRef()
    area_srs = osr.SpatialReference()
    area_srs.ImportFromWkt(info["area_crs_wkt"])
    for srs in (area_srs, layer_srs):
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    area.Transform(osr.CoordinateTransformation(area_srs, layer_srs))
    source = None
    cut = path + ".cut.gpkg"
    result = gdal.VectorTranslate(
        cut,
        path,
        format="GPKG",
        layers=[GMM_LAYER],
        clipSrc=area.ExportToWkt(),
    )
    count = result.GetLayer(0).GetFeatureCount() if result is not None else 0
    result = None
    shutil.move(cut, path)
    return count


def _keep_folder(info):
    """Where the whole-country file is kept: the QLAE folder when chosen,
    else the QGIS profile."""
    if info.get("kept_folder"):
        return info["kept_folder"]
    from qgis.core import QgsApplication

    return os.path.join(
        QgsApplication.qgisSettingsDirPath(),
        "quick_landscape_analysis_explorer",
        "data",
    )


def prefetch(folder, info, progress, cancelled):
    """Fetch, beside the analysis and one after another, what the regional
    analysis adds from the network: the orthophoto, the CIR photo, AHN's
    DSM and the geomorphological map. Each is recorded with its error and
    the others go on. The progress line names the step and gives tiles and
    data amounts."""
    box = _ortho_box(info.get("preview_view") or {})
    if box is None:
        return None
    os.makedirs(folder, exist_ok=True)
    fetched = {}
    steps = (
        (
            "orthophoto",
            lambda: _fetch_photo(
                "orthophoto", folder, box, progress, cancelled
            ),
        ),
        ("cir", lambda: _fetch_photo("cir", folder, box, progress, cancelled)),
        (
            "dsm",
            lambda: _fetch_dsm(
                folder, box, progress, cancelled, info.get("tile_store")
            ),
        ),
        (
            "gmm",
            lambda: _national_gmm(_keep_folder(info), progress, cancelled),
        ),
    )
    for key, step in steps:
        if cancelled():
            return fetched
        try:
            fetched[key] = step()
        except Exception as error:  # noqa: BLE001 - the others go on
            fetched[key + "_error"] = str(error)
    if fetched.get("gmm"):
        try:
            fetched["gmm_clip"] = _clip_gmm(fetched["gmm"], folder, box)
        except Exception as error:  # noqa: BLE001 - recorded
            fetched["gmm_error"] = str(error)
    fetched["box"] = box
    return fetched


# The slope coloured in classes fitted to the analysis area (a prototype):
# the same seven classes and colours as the basic slope, their breaks from
# the area's own slopes. The breaks change from place to place; the legend
# (and the PDF's) shows them.
FITTED_SLOPE_PERCENTILE = 98  # the top slopes left out of the range
_NICE_STEPS = (0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10)


def _fitted_slope_items(slopes):
    """Seven classes over the area's slopes: six equal steps, rounded up to
    a plain value, up to the given percentile; the last class above."""
    import numpy as np

    values = slopes[np.isfinite(slopes)]
    top = (
        float(np.percentile(values, FITTED_SLOPE_PERCENTILE))
        if values.size
        else 6.0
    )
    step = next(
        (nice for nice in _NICE_STEPS if nice * 6 >= top), _NICE_STEPS[-1]
    )
    breaks = [round(step * index, 3) for index in range(1, 7)]
    items = [(breaks[0], "<= {}".format(_number(breaks[0])), SLOPE_COLORS[0])]
    for index in range(1, 6):
        items.append(
            (
                breaks[index],
                "{} - {}".format(
                    _number(breaks[index - 1]), _number(breaks[index])
                ),
                SLOPE_COLORS[index],
            )
        )
    items.append(("inf", "> {}".format(_number(breaks[-1])), SLOPE_COLORS[6]))
    return items, step


def _fitted_slope(folder, info, dem_path):
    """The slope layer with fitted classes, made from the regional analysis's
    own DEM (``dem_path``: the DEM with its holes filled, so the slope is the
    ground's, under the trees too), not from the basic slope. Its record."""
    import numpy as np
    from osgeo import gdal

    from ..analysis import compute_slope_deg

    dataset = gdal.Open(dem_path)
    band = dataset.GetRasterBand(1)
    dem = band.ReadAsArray().astype(float)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        dem[dem == nodata] = np.nan
    slopes = compute_slope_deg(dem, abs(dataset.GetGeoTransform()[1]))
    member = info["folder_member"] + "/slope_deg.tif"
    target = gdal.GetDriverByName("GTiff").Create(
        os.path.join(folder, "slope_deg.tif"),
        dataset.RasterXSize,
        dataset.RasterYSize,
        1,
        gdal.GDT_Float32,
        ["COMPRESS=LZW", "TILED=YES"],
    )
    target.SetGeoTransform(dataset.GetGeoTransform())
    target.SetProjection(dataset.GetProjection())
    out = target.GetRasterBand(1)
    out.SetNoDataValue(-9999.0)
    written = slopes.astype(np.float32)
    written[~np.isfinite(written)] = -9999.0
    out.WriteArray(written)
    target = None
    dataset = None
    items, step = _fitted_slope_items(slopes)
    write_discrete_qml(
        os.path.join(folder, "slope_fitted.qml"),
        items,
        opacity=0.65,
        minimum=0,
        maximum=90,
    )
    return {
        "layer": {
            "name": (
                "Slope (local scale, prototype)",
                "傾斜（範囲階調・試作）",
            ),
            "member": member,
            "style": info["folder_member"] + "/slope_fitted.qml",
            "visible": False,
            # Shown instead of the basic slope: one of the two at a time.
            "excludes": "slope",
        },
        "classes": [value for value, _label, _color in items],
        "step_deg": step,
    }


# Canopy height: AHN's DSM less the ground (the DEM with its holes filled
# below: the DTM has no values under trees and buildings, just where the
# canopy is).
CANOPY_CLASSES = (
    (1, "<= 1", "#ffffff"),
    (3, "1 - 3", "#e5f5e0"),
    (6, "3 - 6", "#c7e9c0"),
    (10, "6 - 10", "#a1d99b"),
    (15, "10 - 15", "#74c476"),
    (20, "15 - 20", "#41ab5d"),
    (25, "20 - 25", "#238b45"),
    (30, "25 - 30", "#006d2c"),
    ("inf", "> 30", "#00441b"),
)


def _on_dtm_grid(path, dtm):
    """The raster at ``path`` on the DTM's grid and area (MEM; no value where
    it has none)."""
    from osgeo import gdal

    gt = dtm.GetGeoTransform()
    bounds = [
        gt[0],
        gt[3] + gt[5] * dtm.RasterYSize,
        gt[0] + gt[1] * dtm.RasterXSize,
        gt[3],
    ]
    return gdal.Warp(
        "",
        path,
        format="MEM",
        outputBounds=bounds,
        xRes=gt[1],
        yRes=abs(gt[5]),
        dstSRS=dtm.GetProjection(),
        resampleAlg="bilinear",
        dstNodata=-9999.0,
        outputType=gdal.GDT_Float32,
    )


def _longest_run(mask):
    """The longest run of True cells along a row or a column."""
    import numpy as np

    longest = 0
    for lines in (mask, mask.T):
        padded = np.zeros((lines.shape[0], lines.shape[1] + 2), dtype=np.int8)
        padded[:, 1:-1] = lines
        steps = np.diff(padded, axis=1)
        starts = np.argwhere(steps == 1)
        ends = np.argwhere(steps == -1)
        if len(starts):
            longest = max(longest, int((ends[:, 1] - starts[:, 1]).max()))
    return longest


def _ground_dem(folder, info, dsm_path):
    """The DEM of the ground: AHN's DTM with its holes under trees and
    buildings filled from the ground around them, at the DTM's resolution.
    Such a hole is told by the DSM: it has a value there (the top of what
    stands on the ground). Where the DSM has none either, there is no ground
    to fill (water: the laser does not come back from it; outside the
    Netherlands): the hole stays. Holes at the DEM's edge are filled too,
    from the ground on their inner side. Its member and the cells filled
    and left."""
    import numpy as np
    from osgeo import gdal

    dtm_path = os.path.join(info["root"], *info["outputs"]["dem"].split("/"))
    dtm = gdal.Open(dtm_path)
    band = dtm.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    ground = band.ReadAsArray().astype(np.float32)
    holes = ~np.isfinite(ground)
    if nodata is not None:
        holes |= ground == nodata
    surface_set = _on_dtm_grid(dsm_path, dtm)  # kept while its band is read
    surface = surface_set.GetRasterBand(1).ReadAsArray()
    surface_set = None
    under_something = holes & (surface != -9999.0) & np.isfinite(surface)
    copy = gdal.GetDriverByName("MEM").CreateCopy("", dtm)
    copy_band = copy.GetRasterBand(1)
    if under_something.any():
        reach = min(_longest_run(under_something) + 2, max(ground.shape))
        gdal.FillNodata(copy_band, None, reach, 0)
        interpolated = copy_band.ReadAsArray()
        usable = under_something & np.isfinite(interpolated)
        if nodata is not None:
            usable &= interpolated != nodata
        ground[usable] = interpolated[usable]
    else:
        usable = under_something
    target = gdal.GetDriverByName("GTiff").CreateCopy(
        os.path.join(folder, "dem_filled.tif"),
        dtm,
        options=["COMPRESS=LZW", "TILED=YES", "PREDICTOR=3"],
    )
    target.GetRasterBand(1).WriteArray(ground)
    target = copy = copy_band = dtm = None
    return (
        info["folder_member"] + "/dem_filled.tif",
        int(usable.sum()),
        int((holes & ~usable).sum()),
    )


def _canopy(folder, info, dsm_path, ground_path):
    """The canopy height (DSM less the ground DEM) over the DSM's area, on
    the DTM's grid; its member."""
    import numpy as np
    from osgeo import gdal

    ground_set = gdal.Open(ground_path)
    gt = ground_set.GetGeoTransform()
    dsm = gdal.Open(dsm_path)
    dgt = dsm.GetGeoTransform()
    # The DSM's area on the DTM's grid.
    xmin = max(gt[0], dgt[0])
    xmax = min(
        gt[0] + gt[1] * ground_set.RasterXSize,
        dgt[0] + dgt[1] * dsm.RasterXSize,
    )
    ymax = min(gt[3], dgt[3])
    ymin = max(
        gt[3] + gt[5] * ground_set.RasterYSize,
        dgt[3] + dgt[5] * dsm.RasterYSize,
    )
    if xmin >= xmax or ymin >= ymax:
        return None
    options = dict(
        format="MEM",
        outputBounds=[xmin, ymin, xmax, ymax],
        xRes=gt[1],
        yRes=abs(gt[5]),
        dstSRS=ground_set.GetProjection(),
        resampleAlg="bilinear",
        dstNodata=-9999.0,
        outputType=gdal.GDT_Float32,
    )
    ground = gdal.Warp("", ground_set, **options)
    surface = gdal.Warp("", dsm, **options)
    ground_set = dsm = None
    ground_values = ground.GetRasterBand(1).ReadAsArray().astype(np.float32)
    surface_values = surface.GetRasterBand(1).ReadAsArray().astype(np.float32)
    height = surface_values - ground_values
    height[height < 0] = 0
    height[(surface_values == -9999.0) | (ground_values == -9999.0)] = -9999.0
    target = gdal.GetDriverByName("GTiff").Create(
        os.path.join(folder, "canopy_height.tif"),
        surface.RasterXSize,
        surface.RasterYSize,
        1,
        gdal.GDT_Float32,
        ["COMPRESS=LZW", "TILED=YES"],
    )
    target.SetGeoTransform(surface.GetGeoTransform())
    target.SetProjection(surface.GetProjection())
    out = target.GetRasterBand(1)
    out.SetNoDataValue(-9999.0)
    out.WriteArray(height)
    target = ground = None
    # The DSM itself, kept too: the same grid and area as the canopy height.
    gdal.Translate(
        os.path.join(folder, "dsm.tif"),
        surface,
        format="GTiff",
        creationOptions=["COMPRESS=LZW", "TILED=YES", "PREDICTOR=3"],
    )
    surface = None
    write_discrete_qml(
        os.path.join(folder, "canopy_height.qml"),
        CANOPY_CLASSES,
        opacity=1.0,
        minimum=0,
        maximum=40,
        multiply=True,
    )
    return info["folder_member"] + "/canopy_height.tif"


# The credits of the layers (a print layout lists those of the layers on its
# map; the analysis's record lists them all).
NAP_CREDIT = (
    "Colours after PDOK's AHN DTM map (dtm_05m)",
    "配色は PDOK の AHN DTM の地図（dtm_05m）に合わせています",
)
# The DSM less the ground: trees and buildings alike (no building mask).
CANOPY_CREDIT = (
    ('Object height (trees and buildings): AHN4 DSM − DTM, surveyed '
     '2020-2022 (PDOK), CC0'),
    "地物の高さ（樹木・建物）：AHN4 の DSM − DTM、2020〜2022年測量（PDOK）、CC0",
)
DSM_CREDIT = (
    "DSM: AHN4 DSM 0.5m, surveyed 2020-2022 (PDOK), CC0",
    "DSM：AHN4 DSM 0.5m、2020〜2022年測量（PDOK）、CC0",
)
GMM_CREDIT = (
    "Geomorphological map: BRO Geomorfologische kaart 1:50,000 (PDOK), CC0",
    "地形分類：BRO 地形分類図 1:5万（PDOK）、CC0",
)
PHOTO_CREDIT = (
    "Orthophotos: Beeldmateriaal.nl (PDOK), CC BY 4.0",
    "オルソフォト：Beeldmateriaal.nl（PDOK）、CC BY 4.0",
)


def _photo_credit(year, infrared):
    """The credit of one photo, with its year when known."""
    if year is None:
        return PHOTO_CREDIT
    if infrared:
        return (
            "Orthophoto CIR {}: Beeldmateriaal.nl (PDOK), CC BY 4.0".format(
                year
            ),
            "赤外オルソフォト {}年：Beeldmateriaal.nl（PDOK）、CC BY 4.0".format(
                year
            ),
        )
    return (
        "Orthophoto {}: Beeldmateriaal.nl (PDOK), CC BY 4.0".format(year),
        "オルソフォト {}年：Beeldmateriaal.nl（PDOK）、CC BY 4.0".format(year),
    )


def run(folder, info, context=None):
    """Write the styles into ``folder`` (regional/<key>/ in the analysis
    ZIP) and make the canopy height; the layers: the DEM in PDOK's colours,
    the slope in classes fitted to the area, the canopy height and the
    geomorphological map (the regional analysis's maps), and the orthophoto
    and the CIR photo for the base map."""
    import shutil

    del context
    items = []
    lower = LOWEST
    for upper, color in CLASSES:
        items.append(
            (upper, "{} - {}".format(_number(lower), _number(upper)), color)
        )
        lower = upper
    # Above the last class (higher than any land in the Netherlands): its
    # colour.
    items[-1] = ("inf", "> {}".format(_number(CLASSES[-2][0])), CLASSES[-1][1])
    os.makedirs(folder, exist_ok=True)
    # Drawn multiplied, so the relief maps below show through its colours.
    write_discrete_qml(
        os.path.join(folder, "nap_colouring.qml"),
        items,
        opacity=1.0,
        minimum=LOWEST,
        maximum=CLASSES[-1][0],
        multiply=True,
    )
    member = info["folder_member"]
    layers = [
        {
            "name": ("NAP colouring", "段彩（NAP基準）"),
            "member": info["outputs"]["dem"],
            "style": member + "/nap_colouring.qml",
            "credit": NAP_CREDIT,
            # Off at first, like all the regional analysis's layers: in the
            # layer
            # panel and in analysis management.
            "visible": False,
        }
    ]
    result = {
        "layers": layers,
        "credit": NAP_CREDIT,
        "credits": [],
        "errors": {},
    }
    fetched = info.get("prefetched") or {}
    if info.get("prefetch_error"):
        result["errors"]["prefetch"] = info["prefetch_error"]
    for key, error in fetched.items():
        if key.endswith("_error"):
            result["errors"][key[:-6]] = error
    # The ground DEM needs the DSM (to tell the holes under trees and
    # buildings from water); the slope is made from it, and the canopy
    # height on it.
    dem_member = canopy = None
    if fetched.get("dsm"):
        dsm_path = fetched["dsm"]["path"]
        result["dsm_tiles"] = {
            "reused": fetched["dsm"]["reused"],
            "downloaded": fetched["dsm"]["downloaded"],
        }
        try:
            dem_member, filled, left = _ground_dem(folder, info, dsm_path)
            result["filled_dem"] = {"cells_filled": filled, "cells_left": left}
        except Exception as error:  # noqa: BLE001 - recorded; the others stay
            result["errors"]["filled_dem"] = str(error)
        if dem_member:
            try:
                canopy = _canopy(
                    folder,
                    info,
                    dsm_path,
                    os.path.join(folder, "dem_filled.tif"),
                )
            # recorded; the others stay
            except Exception as error:  # noqa: BLE001
                result["errors"]["canopy"] = str(error)
        shutil.rmtree(os.path.join(folder, "_dsm"), ignore_errors=True)
    elif "dsm" not in result["errors"]:
        result["errors"]["filled_dem"] = "no DSM"
    if dem_member:
        layers.append(
            {
                "name": ("DEM (holes filled)", "DEM（穴埋め）"),
                "member": dem_member,
                "credit": (
                    ('DEM (holes filled): AHN4 DTM 0.5m, surveyed '
                     '2020-2022 (PDOK), CC0'),
                    "DEM（穴埋め）：AHN4 DTM 0.5m、2020〜2022年測量（PDOK）、CC0",
                ),
                # Data like the DEM: in the analysis's Data group.
                "role": "data",
                "visible": False,
                # Shown instead of the basic DEM: one of the two at a time.
                "excludes": "dem",
            }
        )
        try:
            fitted = _fitted_slope(
                folder, info, os.path.join(folder, "dem_filled.tif")
            )
            layers.append(fitted["layer"])
            result["fitted_slope"] = {
                "classes": fitted["classes"],
                "step_deg": fitted["step_deg"],
                "percentile": FITTED_SLOPE_PERCENTILE,
                "from": dem_member,
            }
        except Exception as error:  # noqa: BLE001 - recorded; the others stay
            result["errors"]["fitted_slope"] = str(error)
    if canopy:
        layers.append(
            {
                "name": (
                    "Object height (trees and buildings)",
                    "地物の高さ（樹木・建物）",
                ),
                "member": canopy,
                "style": member + "/canopy_height.qml",
                "visible": False,
                "credit": CANOPY_CREDIT,
            }
        )
        # Data like the DEM: in the analysis's Data group, ticked like
        # the other maps in analysis management.
        layers.append(
            {
                "name": ("DSM", "DSM"),
                "member": member + "/dsm.tif",
                "role": "data",
                "visible": False,
                "credit": DSM_CREDIT,
            }
        )
        result["credits"].extend([CANOPY_CREDIT, DSM_CREDIT])
    if fetched.get("gmm_clip"):
        path, count = fetched["gmm_clip"]
        try:
            count = _cut_to_area(path, info) or count
        except Exception as error:  # noqa: BLE001 - kept uncut; recorded
            result["errors"]["geomorphology_cut"] = str(error)
        shutil.copy(GMM_STYLE, os.path.join(folder, "geomorphology.qml"))
        layers.append(
            {
                "name": ("Geomorphology", "地形分類"),
                "member": member + "/geomorphology.gpkg",
                "kind": "vector",
                "layer": GMM_LAYER,
                "style": member + "/geomorphology.qml",
                "visible": False,
                "credit": GMM_CREDIT,
                # About 160 landforms: a print layout gives its legend a page
                # of
                # its own, not the legend next to the map.
                "legend_page": True,
            }
        )
        result["geomorphology"] = {"landforms": count, "source": GMM_URL}
        result["credits"].append(GMM_CREDIT)
    for key, (english, japanese) in (
        ("orthophoto", ("Orthophoto", "オルソフォト")),
        ("cir", ("Orthophoto CIR", "赤外オルソフォト")),
    ):
        photo = fetched.get(key)
        if photo:
            year = photo.get("year")
            if year is not None:
                english, japanese = (
                    "{} {}".format(english, year),
                    "{} {}".format(japanese, year),
                )
            credit = _photo_credit(year, key == "cir")
            # Not one of the maps: there for the base map.
            layers.append(
                {
                    "name": (
                        english + " for Base map",
                        japanese + " for Base map",
                    ),
                    "member": photo["member"],
                    "role": "base_map",
                    "visible": False,
                    "credit": credit,
                }
            )
            result[key] = {
                k: photo.get(k)
                for k in ("tiles", "bytes", "cell_m", "layer", "year")
            }
            result["credits"].append(credit)
    return result
