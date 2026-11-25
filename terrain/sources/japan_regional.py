# -*- coding: utf-8 -*-
"""Japan's regional analysis.

CS Valley (a prototype): the CS Map with only its valleys kept. The valleys
are picked by the CS Map's own colours, as one reads them in it: where its
valley blue outweighs its ridge red and slope brown (not from the
catchments, and not only the channels: the valley heads and sides the CS
Map colours blue are kept too).

How the valleys are picked (the constants below):
1. The blueness of each cell of the CS Map: (blue - red) / 255, smoothed
   over COLOR_SMOOTH_M.
2. Its weight goes smoothly from 0 at the low to 1 at the high end of a
   gate (GATES: named like a noise gate's threshold; the higher the gate,
   the stronger a valley must be to pass). One layer for each gate, ticked
   one at a time (and not with the CS Map) in analysis management.
3. Cut off sharply the rest looks unnatural, so the rest of the CS Map
   stays faint (BACKGROUND_ALPHA), drawn through to the layers below; the
   smoothing of 1. feathers the edges.

Valley flow (a prototype, drawn above the CS Valley): how much water
gathers along the valleys, to read against the hollows of the CS Valley
which have become streams. The area draining through each cell by MFD
flow (terrain/valley_flow.py: mfd_area), in stages (FLOW_STAGES: valley
head, channel start, stream, main stream); below the first stage nothing
is drawn. Light blue, so it reads apart from the CS Valley's blue.
Computed on the analysis frame only: the area above the frame is not
counted.

Made for fine DEMs (the sources in the OPTION of japan.py). No new data:
from the basic analysis's DEM.
"""

import os

# 1.-2. The colour judgement: the smoothing and the gates, (key, name,
# (low, high) blueness), in the order listed.
COLOR_SMOOTH_M = 2.0
GATES = (
    ("high", ("CS Valley - Gate High", "CS Valley（ゲート高）"), (-0.10, 0.02)),
    ("low", ("CS Valley - Gate Low", "CS Valley（ゲート低）"), (-0.15, 0.0)),
)
# 3. The opacity of the rest of the CS Map (0 = cut off, 1 = as it is).
BACKGROUND_ALPHA = 0.15

# Valley flow: the MFD convergence (as the catchments: GRASS r.watershed's)
# and the stages of the area draining through each cell (m2): (upper value,
# (English, Japanese) stage, colour), light blue. Below the first: not
# drawn. Provisional, to be set against the field.
FLOW_CONVERGENCE = 5.0
FLOW_SLOPE_M2 = 1000
FLOW_STAGES = (
    (5000, ("valley head / hollow", "谷頭・0次谷"), "#d4f4fb"),
    (20000, ("channel start", "沢の始まり"), "#9de6f5"),
    (100000, ("stream", "沢"), "#5fd3ec"),
    ("inf", ("main stream", "本流"), "#1fb5dc"),
)
FLOW_OPACITY = 0.85


def _read_dem(path):
    import numpy as np
    from osgeo import gdal

    dataset = gdal.Open(path)
    band = dataset.GetRasterBand(1)
    dem = band.ReadAsArray().astype(np.float64)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        dem[dem == nodata] = np.nan
    gt = dataset.GetGeoTransform()
    wkt = dataset.GetProjection()
    dataset = None
    return dem, gt, wkt


def blueness(rgba, cell_size):
    """The smoothed blueness of each cell of the CS Map (uint8 RGBA)."""
    import numpy as np

    from ..cs_map import _smooth_nan

    blue = (rgba[..., 2].astype(np.float64) - rgba[..., 0]) / 255.0
    blue[rgba[..., 3] == 0] = np.nan
    return np.nan_to_num(_smooth_nan(blue, COLOR_SMOOTH_M / cell_size))


def valley_weight(blue, gate):
    """How much each cell passes the gate ((low, high) blueness), 0-1."""
    import numpy as np

    low, high = gate
    t = np.clip((blue - low) / (high - low), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def cs_valleys(dem, cell_size):
    """The CS Valley images, one for each gate: [(key, name, uint8 RGBA,
    record)]."""
    import numpy as np

    from ..cs_map import compute_cs_map

    rgba = compute_cs_map(dem, cell_size)
    blue = blueness(rgba, cell_size)
    valid = rgba[..., 3] > 0
    cells = int(valid.sum())
    images = []
    for key, name, gate in GATES:
        weight = valley_weight(blue, gate)
        alpha = BACKGROUND_ALPHA + (1.0 - BACKGROUND_ALPHA) * weight
        image = rgba.copy()
        image[..., 3] = np.where(
            valid, np.clip(alpha * 255.0, 0, 255), 0
        ).astype(np.uint8)
        record = {
            "gate": list(gate),
            "kept_share": round(float((weight[valid] > 0.5).sum()) / cells, 4)
            if cells
            else 0.0,
        }
        images.append((key, name, image, record))
    return images


def _stage_label(lower, upper, name):
    """A stage's legend label: its area in hectares and its name."""
    from ...i18n import tr

    def ha(value):
        return "{:g}".format(value / 10000.0)

    if upper == "inf":
        size = "> {} ha".format(ha(lower))
    else:
        size = "{} - {} ha".format(ha(lower), ha(upper))
    return "{} {}".format(size, tr(*name))


def _valley_flow(folder, info, dem, gt, wkt, context):
    """Write the valley flow (the area draining through each cell, in
    stages) and its style; its layer and record."""
    import numpy as np

    from ..valley_flow import mfd_area
    from ..result_writer import save_raster
    from ..styles import write_discrete_qml

    check = (lambda _done: context.check()) if context is not None else None
    cell = abs(gt[1])
    area = mfd_area(dem, cell, FLOW_CONVERGENCE, check)
    valid = np.isfinite(area)
    area = np.nan_to_num(area)
    # The stage of each cell (1 = the first), NaN (not drawn) below it.
    bounds = [FLOW_SLOPE_M2] + [
        upper for upper, _name, _color in FLOW_STAGES[:-1]
    ]
    stage = np.searchsorted(np.array(bounds, float), area, side="left")
    stage = np.where(area > FLOW_SLOPE_M2, stage, np.nan)
    save_raster(stage, gt, wkt, folder, "valley_flow", overwrite=True)
    items = []
    lower = FLOW_SLOPE_M2
    for index, (upper, name, color) in enumerate(FLOW_STAGES, start=1):
        value = index if upper != "inf" else "inf"
        items.append((value, _stage_label(lower, upper, name), color))
        lower = upper
    write_discrete_qml(
        os.path.join(folder, "valley_flow.qml"),
        items,
        opacity=FLOW_OPACITY,
        minimum=1,
        maximum=len(FLOW_STAGES),
    )
    count = int(valid.sum())
    shares = {"below": round(
        float((area[valid] <= FLOW_SLOPE_M2).sum()) / count, 4
    ) if count else 0.0}
    for index, (_upper, name, _color) in enumerate(FLOW_STAGES, start=1):
        shares[name[0]] = (
            round(float((stage[valid] == index).sum()) / count, 4)
            if count
            else 0.0
        )
    record = {
        "method": "MFD (convergence {:g}), depressions filled".format(
            FLOW_CONVERGENCE
        ),
        "stages_m2": [FLOW_SLOPE_M2]
        + [upper for upper, _name, _color in FLOW_STAGES],
        "shares": shares,
    }
    layer = {
        "name": ("Valley flow (MFD)", "沢の成立（MFD）"),
        "member": info["folder_member"] + "/valley_flow.tif",
        "style": info["folder_member"] + "/valley_flow.qml",
        "visible": False,
    }
    return layer, record


def _recorded(error, context):
    """Whether a failure is recorded (the rest kept): not a stop or a job
    error, which end the analysis."""
    return not (
        context is not None
        and type(error).__name__ in ("TerrainJobCancelled", "TerrainJobError")
    )


def run(folder, info, context=None):
    """Write the CS Valley and the valley flow into ``folder``
    (regional/<key>/ in the analysis ZIP), from the basic analysis's DEM;
    their layers are off at first, like all the regional analysis's
    layers. A failure of one is recorded; the other is kept."""
    from ..result_writer import save_rgba_raster

    os.makedirs(folder, exist_ok=True)
    result = {"layers": [], "credit": None, "credits": [], "errors": {}}
    try:
        dem, gt, wkt = _read_dem(
            os.path.join(info["root"], *info["outputs"]["dem"].split("/"))
        )
    except Exception as error:  # noqa: BLE001 - recorded; the basic stays
        result["errors"]["cs_valley"] = str(error)
        return result

    if context is not None:
        context.report("Creating CS Valley...")
        context.check()
    try:
        images = cs_valleys(dem, abs(gt[1]))
        for key, _name, image, _record in images:
            if context is not None:
                context.check()
            save_rgba_raster(
                image, gt, wkt, folder, "cs_valley_" + key, overwrite=True
            )
    except Exception as error:  # noqa: BLE001 - recorded; the rest stays
        if not _recorded(error, context):
            raise
        result["errors"]["cs_valley"] = str(error)
        images = []
    for key, name, _image, _record in images:
        result["layers"].append(
            {
                "name": name,
                "member": info["folder_member"]
                + "/cs_valley_{}.tif".format(key),
                "visible": False,
                # One gate at a time, and not with the CS Map
                # (management_panel.EXCLUSIVE_OUTPUTS).
                "excludes": "cs_valley",
            }
        )
    if images:
        result["cs_valley"] = {
            "color_smooth_m": COLOR_SMOOTH_M,
            "background_alpha": BACKGROUND_ALPHA,
            "gates": {key: record for key, _n, _i, record in images},
        }
    del images

    if context is not None:
        context.report("Creating the valley flow...")
    try:
        layer, record = _valley_flow(folder, info, dem, gt, wkt, context)
    except Exception as error:  # noqa: BLE001 - recorded; the rest stays
        if not _recorded(error, context):
            raise
        result["errors"]["valley_flow"] = str(error)
        return result
    # First: drawn above the CS Valley.
    result["layers"].insert(0, layer)
    result["valley_flow"] = record
    return result
