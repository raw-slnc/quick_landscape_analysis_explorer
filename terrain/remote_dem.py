# -*- coding: utf-8 -*-
"""Open overseas DEMs fetched for an extent (no login required).

Netherlands AHN4 DTM (PDOK WCS), France IGN LiDAR HD MNT (Géoplateforme WMS-R),
New Zealand LINZ 1 m DEM (Cloud Optimised GeoTIFFs on AWS) and the 1 m DGM1 of
two German states, Rhineland-Palatinate and Bavaria (1 km GeoTIFF tiles).

``ctx`` provides ``report(text)``, ``check()`` (raises when cancelled) and
``cancelled()``. Files are written below ``out_dir``, which the caller owns.
"""

import datetime
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import gzip
import http.client
import json
import math
import os
import re
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np

from . import sources

_USER_AGENT = "Quick Landscape Analysis Explorer (QGIS plugin)"

_AHN_WCS = (
    "https://service.pdok.nl/rws/ahn/wcs/v1_0?service=WCS&request=GetCoverage"
    "&version=2.0.1&coverageId={layer}&subset=x({x0},{x1})&subset=y({y0},{y1})"
    "&format=image/tiff"
)
_IGN_WMSR = (
    "https://data.geopf.fr/wms-r/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
    "&LAYERS={layer}&STYLES=&CRS=EPSG:2154&BBOX={x0},{y0},{x1},{y1}"
    "&WIDTH={w}&HEIGHT={h}&FORMAT=image/geotiff"
)
_NZ_BASE = ('https://nz-elevation.s3.ap-southeast-2.amazonaws.com/new-zeal'
            'and/new-zealand/dem_1m/2193/')
# German state DGM1: 1 km tiles in ETRS89 / UTM 32N named by their lower-left
# corner in km. Rhineland-Palatinate puts the survey year in the file name, so
# its file list is read first; Bavaria's names have no year.
_RLP_DGM1 = "https://www.geobasis-rlp.de/data/dgm1/current/tif/"
_BY_DGM1 = "https://download1.bayernwolke.de/a/dgm/dgm1/"

# Source key -> how to fetch it. The words (dataset name, provider, licence,
# credit, service) come from the country files (terrain/sources/).
REMOTE_SOURCES = {
    "ahn_dtm": dict(
        kind="ogc",
        url=_AHN_WCS,
        layer="dtm_05m",
        epsg=28992,
        cell=0.5,
        chunk_px=1000,
        prefix="ahn_dtm",
        # The coverage's extent (DescribeCoverage of dtm_05m), in RD New.
        extent=(10000.0, 306250.0, 280000.0, 618750.0),
    ),
    "ign_mnt": dict(
        kind="ogc",
        url=_IGN_WMSR,
        layer="IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93",
        epsg=2154,
        cell=0.5,
        chunk_px=2000,
        # WMS-R serves any image size: one coarse look first (see _probe_ogc).
        probe_px=512,
        prefix="ign_lidarhd_mnt",
    ),
    "linz_dem": dict(kind="nzcog", epsg=2193, cell=1.0, prefix="linz_dem"),
    "rlp_dgm1": dict(
        kind="de_tiles", state="rlp", epsg=25832, cell=1.0, prefix="rlp_dgm1"
    ),
    "by_dgm1": dict(
        kind="de_tiles", state="by", epsg=25832, cell=1.0, prefix="by_dgm1"
    ),
}
for _key, _spec in REMOTE_SOURCES.items():
    _facts = sources.get(_key)["facts"]
    _spec.update(
        label=_facts["dataset"],
        provider=_facts["provider"],
        licence=_facts["licence"],
        service=_facts["service"],
    )
    if "credit" in _facts:
        _spec["credit"] = _facts["credit"]
    if "survey_years" in _facts:
        _spec["survey_years"] = list(_facts["survey_years"])

LERC_MISSING_MESSAGE = (
    "This QGIS's GDAL cannot read LERC-compressed GeoTIFFs, which the "
    'LINZ data uses. '
    "Use a QGIS build whose GDAL supports LERC."
)


class RemoteDemError(Exception):
    """A message for the user."""


def _http_download(
    ctx,
    url,
    dest,
    timeout=180,
    accept=("tif",),
    on_headers=None,
    on_bytes=None,
):
    """Save url to dest, checking for cancellation every 64 KB."""
    if not url.startswith("https://"):
        raise RemoteDemError(f"Invalid URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with (
        urllib.request.urlopen(req, timeout=timeout) as r,  # nosec B310
        open(dest, "wb") as f,
    ):
        ctype = r.headers.get("Content-Type", "")
        if on_headers is not None:
            on_headers(_content_length(r.headers.get("Content-Length")))
        while True:
            ctx.check()
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
    if not any(a in ctype.lower() for a in accept):
        try:
            with open(dest, "rb") as f:
                head = f.read(300).decode("utf-8", "replace")
        except OSError:
            head = ""
        raise RemoteDemError(
            f"The service returned an error instead of elevation data: {head}"
        )


def _content_length(value):
    try:
        size = int(value)
        return size if size >= 0 else None
    except (TypeError, ValueError):
        return None


_RETRYABLE_HTTP_CODES = frozenset((429, 502, 503, 504))
_HTTP_RETRIES = 3


def _wait_for_retry(ctx, seconds):
    """Let cancellation interrupt the short wait between HTTP attempts."""
    deadline = time.monotonic() + seconds
    while True:
        ctx.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def _download_with_retries(ctx, download, url, dest, **kwargs):
    """Retry temporary service failures; keep other HTTP errors unchanged."""
    for attempt in range(_HTTP_RETRIES + 1):
        ctx.check()
        try:
            return download(ctx, url, dest, **kwargs)
        except urllib.error.HTTPError as error:
            if (
                error.code not in _RETRYABLE_HTTP_CODES
                or attempt == _HTTP_RETRIES
            ):
                raise
            retry_after = _content_length(
                error.headers.get("Retry-After") if error.headers else None
            )
            error.close()
            delay = min(30, max(2**attempt, retry_after or 0))
            ctx.report(
                f"DEM service returned HTTP {error.code}; "
                f"retrying in {delay} s "
                f"({attempt + 1}/{_HTTP_RETRIES})…"
            )
            _wait_for_retry(ctx, delay)


class _DownloadConnections:
    """One reusable HTTPS connection per worker, for the fixed DEM hosts.

    urllib remains the path when an environment proxy is configured. Its
    proxy and redirect handling is preferable to bypassing those settings.
    """

    def __init__(self):
        self._local = threading.local()
        self._connections = []
        self._lock = threading.Lock()
        proxies = urllib.request.getproxies()
        self._use_urllib = any(
            proxies.get(key) for key in ("http", "https", "all")
        )

    def download(
        self,
        ctx,
        url,
        dest,
        timeout=180,
        _retry=True,
        on_headers=None,
        on_bytes=None,
    ):
        if self._use_urllib:
            return _http_download(
                ctx,
                url,
                dest,
                timeout=timeout,
                on_headers=on_headers,
                on_bytes=on_bytes,
            )
        parts = urllib.parse.urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
        ):
            raise RemoteDemError(f"Invalid URL: {url}")
        host = (parts.hostname, parts.port)
        connection = getattr(self._local, "connection", None)
        if connection is None or getattr(self._local, "host", None) != host:
            if connection is not None:
                connection.close()
            connection = http.client.HTTPSConnection(
                parts.hostname, parts.port, timeout=timeout
            )
            self._local.connection = connection
            self._local.host = host
            with self._lock:
                self._connections.append(connection)
        path = urllib.parse.urlunsplit(
            ("", "", parts.path or "/", parts.query, "")
        )
        try:
            connection.request(
                "GET", path, headers={"User-Agent": _USER_AGENT}
            )
            response = connection.getresponse()
            if 300 <= response.status < 400:
                response.read()
                # Keep urllib's redirect behaviour for services that move.
                return _http_download(
                    ctx,
                    url,
                    dest,
                    timeout=timeout,
                    on_headers=on_headers,
                    on_bytes=on_bytes,
                )
            if response.status >= 400:
                raise urllib.error.HTTPError(
                    url,
                    response.status,
                    response.reason,
                    response.headers,
                    None,
                )
            ctype = response.getheader("Content-Type", "")
            if on_headers is not None:
                on_headers(
                    _content_length(response.getheader("Content-Length"))
                )
            with open(dest, "wb") as output:
                while True:
                    ctx.check()
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    output.write(chunk)
                    if on_bytes is not None:
                        on_bytes(len(chunk))
        except (
            http.client.RemoteDisconnected,
            http.client.CannotSendRequest,
            ConnectionResetError,
            BrokenPipeError,
        ):
            connection.close()
            self._local.connection = None
            if _retry:
                # A server may close an idle keep-alive connection. GET can
                # safely be sent once more on a fresh connection.
                return self.download(
                    ctx,
                    url,
                    dest,
                    timeout=timeout,
                    _retry=False,
                    on_headers=on_headers,
                    on_bytes=on_bytes,
                )
            raise
        except BaseException:
            connection.close()
            self._local.connection = None
            raise
        if "tif" not in ctype.lower():
            with open(dest, "rb") as source:
                head = source.read(300).decode("utf-8", "replace")
            raise RemoteDemError(
                "The service returned an error instead of elevation data: "
                f"{head}"
            )

    def close(self):
        with self._lock:
            connections = self._connections
            self._connections = []
        for connection in connections:
            connection.close()


def _download_chunk(
    ctx,
    connections,
    url,
    dest,
    store=None,
    prefix=None,
    year=None,
    code=None,
    on_headers=None,
    on_bytes=None,
):
    """Download one independent chunk, publishing it atomically if kept."""
    incoming = store.incoming() if store is not None else None
    try:
        part = (
            os.path.join(incoming, os.path.basename(dest))
            if incoming
            else dest
        )
        if on_headers is None and on_bytes is None:
            _download_with_retries(ctx, connections.download, url, part)
        else:
            _download_with_retries(
                ctx,
                connections.download,
                url,
                part,
                on_headers=on_headers,
                on_bytes=on_bytes,
            )
        if store is not None:
            return store.keep(prefix, year, code, part)
        return part
    finally:
        if incoming is not None:
            store.drop_incoming(incoming)


def snap_bbox(bbox, cell):
    """Grow bbox outwards onto the cell grid."""
    x0, y0, x1, y1 = bbox
    return (
        math.floor(x0 / cell) * cell,
        math.floor(y0 / cell) * cell,
        math.ceil(x1 / cell) * cell,
        math.ceil(y1 / cell) * cell,
    )


def check_size(bbox, cell):
    """Refuse empty extents; return the grid size at this cell size.

    There is no upper limit: the dialog asks before large analyses
    (dialog._confirm_dem_size).
    """
    x0, y0, x1, y1 = snap_bbox(bbox, cell)
    w_px = int(round((x1 - x0) / cell))
    h_px = int(round((y1 - y0) / cell))
    if w_px <= 0 or h_px <= 0:
        raise RemoteDemError("The analysis area is empty.")
    return w_px, h_px


def _finish_mosaic(ctx, sources, out_dir, prefix, epsg, bbox=None):
    """Combine sources into one GeoTIFF; all NoData means outside coverage.

    The CRS is set from epsg: IGN's WMS-R GeoTIFFs carry an incomplete
    definition without an EPSG code.
    """
    from osgeo import gdal

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    tif_path = os.path.join(out_dir, f"{prefix}_{ts}.tif")
    vrt = gdal.BuildVRT("", sources)
    if vrt is None:
        raise RemoteDemError("Could not combine the downloaded tiles.")

    def _cb(complete, _msg, _data):
        ctx.report(f"Reading elevation data… {int(complete * 100)}%")
        return 0 if ctx.cancelled() else 1

    opts = dict(
        format="GTiff",
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"],
        outputSRS=f"EPSG:{epsg}",
        callback=_cb,
    )
    if bbox is not None:
        x0, y0, x1, y1 = bbox
        opts.update(projWin=[x0, y1, x1, y0])
    out = gdal.Translate(tif_path, vrt, **opts)
    vrt = None
    ctx.check()
    if out is None:
        raise RemoteDemError("Could not write the elevation GeoTIFF.")
    band = out.GetRasterBand(1)
    valid = _valid_cells(band.ReadAsArray(), band.GetNoDataValue())
    out = None
    if not valid.any():
        raise RemoteDemError(
            "No elevation data for this area (outside the source coverage)."
        )
    return tif_path


def _valid_cells(arr, nodata):
    valid = np.isfinite(arr)
    if nodata is not None:
        valid &= arr != nodata
    # AHN uses the float32 maximum as NoData on its 0.5 m grid.
    valid &= np.abs(arr) < 1e30
    return valid


def _probe_ogc(ctx, spec, bbox, parts_dir):
    """One coarse request over the whole area before the
    full-resolution chunks.

    Outside its coverage IGN still sends every chunk in full (16 MB of
    NoData each), so an area without data took as long as one with data.
    Raises when the coarse look holds no values. Otherwise returns a test
    telling whether a chunk lies wholly in the empty part, with one coarse
    cell of margin so thin strips of data are kept; None when the look
    itself fails (then every chunk is fetched, as before).
    """
    from osgeo import gdal

    x0, y0, x1, y1 = bbox
    scale = max(x1 - x0, y1 - y0) / spec["probe_px"]
    w = max(1, int(math.ceil((x1 - x0) / scale)))
    h = max(1, int(math.ceil((y1 - y0) / scale)))
    ctx.report(f"Checking where {spec['label']} has data…")
    url = spec["url"].format(
        layer=spec["layer"], x0=x0, y0=y0, x1=x1, y1=y1, w=w, h=h
    )
    dest = os.path.join(parts_dir, "probe.tif")
    try:
        _download_with_retries(ctx, _http_download, url, dest)
        dataset = gdal.Open(dest)
        band = dataset.GetRasterBand(1)
        arr = band.ReadAsArray()
        # Scaled down, IGN blends its NoData (-9999) into values such as
        # -9999.001 and, at the data's edge, into mixed values; no land is
        # below -1000 m.
        valid = _valid_cells(arr, band.GetNoDataValue()) & (arr > -1000)
        dataset = None
    except Exception:  # noqa: BLE001 - the look is only a shortcut
        ctx.check()  # but a cancel during it still stops the job
        return None
    if not valid.any():
        raise RemoteDemError(
            "No elevation data for this area (outside the source coverage)."
        )
    cell_x = (x1 - x0) / w
    cell_y = (y1 - y0) / h

    def empty(cx0, cy0, cx1, cy1):
        c0 = max(int((cx0 - x0) / cell_x) - 1, 0)
        c1 = min(int(math.ceil((cx1 - x0) / cell_x)) + 1, w)
        r0 = max(int((y1 - cy1) / cell_y) - 1, 0)
        r1 = min(int(math.ceil((y1 - cy0) / cell_y)) + 1, h)
        return not valid[r0:r1, c0:c1].any()

    return empty


class _OgcTransferProgress:
    """Report actual received bytes and a clearly marked size estimate.

    WCS/WMS gives each chunk's Content-Length only when its GET starts. The
    estimate uses the mean of the chunks whose lengths are known; it becomes
    exact once every response header has arrived. No extra requests are made.
    """

    def __init__(self, ctx, label, total, reused):
        self.ctx = ctx
        self.label = label
        self.total = total
        self.reused = reused
        self.to_download = total - reused
        self.downloaded = 0
        self.active = 0
        self.received = 0
        self.sizes = {}
        self.lock = threading.Lock()
        self.last_report = 0.0
        self._report(force=True)

    def started(self):
        with self.lock:
            self.active += 1
            self._report_locked(force=True)

    def headers(self, index, size):
        with self.lock:
            if size is not None:
                self.sizes[index] = size
            self._report_locked(force=True)

    def bytes(self, count):
        with self.lock:
            self.received += count
            self._report_locked()

    def completed(self, index, path):
        with self.lock:
            self.sizes.setdefault(index, os.path.getsize(path))
            self.downloaded += 1
            self.active -= 1
            self._report_locked(force=True)

    def _report(self, force=False):
        with self.lock:
            self._report_locked(force)

    def _report_locked(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_report < 0.5:
            return
        self.last_report = now
        if self.to_download == 0:
            total_text = "0.0 MB"
        elif not self.sizes:
            total_text = "calculating total"
        else:
            known = sum(self.sizes.values())
            estimated = known * self.to_download / len(self.sizes)
            estimated = max(estimated, self.received)
            exact = (
                len(self.sizes) == self.to_download and self.received <= known
            )
            total_text = (
                "" if exact else "about "
            ) + f"{estimated / 1e6:.1f} MB"
        self.ctx.report(
            f"Fetching {self.label}… "
            f"{self.reused + self.downloaded}/{self.total}\n"
            f"DEM transfer: {self.received / 1e6:.1f} MB / {total_text}; "
            f"{self.downloaded}/{self.to_download} downloaded, "
            f"{self.reused} reused, {self.active} active."
        )


def _fetch_ogc(ctx, spec, bbox, out_dir, store=None, kept=None):
    """Query WCS / WMS in chunks and combine them into one GeoTIFF.

    The chunks lie on a fixed grid in the source's CRS (chunk_px cells
    square, counted from 0), so the same place gives the same chunks and
    ``store`` (core/dem_store.TileStore) can keep them for later analyses.
    ``kept`` ({chunk code: [None]}): only these kept chunks are used and
    nothing is fetched (the QLAE folder).
    """
    cell = spec["cell"]
    check_size(bbox, cell)
    x0, y0, x1, y1 = snap_bbox(bbox, cell)
    step = spec["chunk_px"] * cell
    columns = range(int(math.floor(x0 / step)), int(math.ceil(x1 / step)))
    rows = range(int(math.floor(y0 / step)), int(math.ceil(y1 / step)))
    parts_dir = os.path.join(out_dir, f"parts_{spec['prefix']}")
    os.makedirs(parts_dir, exist_ok=True)
    empty = None
    if spec.get("probe_px") and kept is None:
        empty = _probe_ogc(ctx, spec, (x0, y0, x1, y1), parts_dir)
    entries = []  # (cached path, url, destination, code), in mosaic order
    k = 0
    for row in rows:
        for column in columns:
            ctx.check()
            k += 1
            cx0, cx1 = column * step, (column + 1) * step
            cy0, cy1 = row * step, (row + 1) * step
            code = "{}_{}".format(column, row)
            kept_path = (
                store.find(spec["prefix"], None, code)
                if store is not None
                else None
            )
            if kept_path is not None:
                store.reused += 1
                entries.append((kept_path, None, None, code))
                continue
            if kept is not None:
                continue
            if empty is not None and empty(cx0, cy0, cx1, cy1):
                continue
            limits = spec.get("extent")
            if limits:
                # Only within the coverage's own extent: AHN's WCS refuses a
                # request reaching past it (400, ExtentError), as at the
                # southern tip of the Netherlands. The rest is left NoData.
                cx0, cy0 = max(cx0, limits[0]), max(cy0, limits[1])
                cx1, cy1 = min(cx1, limits[2]), min(cy1, limits[3])
                if cx0 >= cx1 or cy0 >= cy1:
                    continue
            url = spec["url"].format(
                layer=spec["layer"],
                x0=cx0,
                y0=cy0,
                x1=cx1,
                y1=cy1,
                w=int(round((cx1 - cx0) / cell)),
                h=int(round((cy1 - cy0) / cell)),
            )
            dest = os.path.join(parts_dir, f"part_{k:04d}.tif")
            entries.append((None, url, dest, code))
    if not entries:
        raise RemoteDemError(
            "No elevation data for this area (outside the source coverage)."
        )
    paths = [None] * len(entries)
    tasks = []
    for index, (cached, url, dest, code) in enumerate(entries):
        if cached is None:
            tasks.append((index, url, dest, code))
        else:
            paths[index] = cached
    progress = _OgcTransferProgress(
        ctx, spec["label"], len(entries), len(entries) - len(tasks)
    )
    connections = _DownloadConnections()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = {}
            remaining = iter(tasks)

            def submit_available():
                while len(pending) < 2:
                    try:
                        index, url, dest, code = next(remaining)
                    except StopIteration:
                        break
                    progress.started()
                    future = executor.submit(
                        _download_chunk,
                        ctx,
                        connections,
                        url,
                        dest,
                        store,
                        spec["prefix"],
                        None,
                        code,
                        lambda size, index=index: progress.headers(
                            index, size
                        ),
                        progress.bytes,
                    )
                    pending[future] = index

            submit_available()
            while pending:
                ctx.check()
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    index = pending.pop(future)
                    paths[index] = future.result()
                    if store is not None:
                        store.downloaded += 1
                    progress.completed(index, paths[index])
                submit_available()
    finally:
        connections.close()
    ctx.report("Combining…")
    # Cut to the area asked for (the chunks reach past it); the gaps NoData.
    return _finish_mosaic(
        ctx,
        paths,
        out_dir,
        spec["prefix"],
        spec["epsg"],
        bbox=(x0, y0, x1, y1),
    )


_TOPO50_LETTERS = "".join(c for c in string.ascii_uppercase if c not in "IO")


def _nz_tile_names(bbox):
    """Topo50 sheet names (e.g. BP35) touching an NZTM2000 (EPSG:2193) bbox.

    Sheets are 24 km × 36 km. Column = (xmin - 988000) / 24000; the row is the
    two-letter order without I and O, counted from AS, with
    ymax = 6234000 - row × 36000.
    """
    x0, y0, x1, y1 = bbox
    c0 = int(math.floor((x0 - 988000) / 24000))
    c1 = int(math.floor((x1 - 988000 - 1e-6) / 24000))
    r0 = int(math.floor((6234000 - y1) / 36000))
    r1 = int(math.floor((6234000 - y0 - 1e-6) / 36000))
    base = _TOPO50_LETTERS.index("A") * 24 + _TOPO50_LETTERS.index("S")
    names = []
    for r in range(r0, r1 + 1):
        code = base + r
        if code < 0:
            continue
        a, b = divmod(code, 24)
        if a >= len(_TOPO50_LETTERS):
            continue
        for c in range(c0, c1 + 1):
            if 0 <= c <= 99:
                names.append(
                    f"{_TOPO50_LETTERS[a]}{_TOPO50_LETTERS[b]}{c:02d}"
                )
    return names


def gdal_has_lerc():
    """Whether this GDAL reads LERC-compressed GeoTIFFs (LINZ COGs use LERC).

    Some builds lack LERC (for example the Flatpak QGIS).
    """
    from osgeo import gdal

    drv = gdal.GetDriverByName("GTiff")
    opts = (
        drv.GetMetadataItem("DMD_CREATIONOPTIONLIST")
        if drv is not None
        else None
    )
    return bool(opts) and "<Value>LERC</Value>" in opts


def _nz_available_tiles(ctx):
    """Tile names that exist, from the nationwide 1 m DEM collection.json."""
    ctx.report("Checking LINZ tile index…")
    req = urllib.request.Request(
        _NZ_BASE + "collection.json", headers={"User-Agent": _USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=60) as r:  # nosec B310
        col = json.loads(r.read().decode("utf-8"))
    return {
        link["href"][2:-5]
        for link in col.get("links", [])
        if link.get("rel") == "item" and link.get("href", "").startswith("./")
    }


def _fetch_nzcog(ctx, spec, bbox, out_dir):
    """Read only the needed parts of the LINZ COG tiles and combine them."""
    from osgeo import gdal

    cell = spec["cell"]
    check_size(bbox, cell)
    bbox = snap_bbox(bbox, cell)
    if not gdal_has_lerc():
        raise RemoteDemError(LERC_MISSING_MESSAGE)
    wanted = _nz_tile_names(bbox)
    available = _nz_available_tiles(ctx)
    tiles = [n for n in wanted if n in available]
    ctx.coverage_names = sorted(
        available
    )  # the whole list, for the coverage mesh
    if not tiles:
        raise RemoteDemError(
            "No elevation data for this area (outside the source coverage)."
        )
    ctx.check()
    # Partial reads of COGs on S3 (settings for this thread only).
    gdal.SetThreadLocalConfigOption(
        "GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR"
    )
    gdal.SetThreadLocalConfigOption(
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tiff,.tif"
    )
    try:
        sources = [f"/vsicurl/{_NZ_BASE}{n}.tiff" for n in tiles]
        ctx.report(f"Reading {spec['label']} ({len(tiles)} tile(s))…")
        return _finish_mosaic(
            ctx, sources, out_dir, spec["prefix"], spec["epsg"], bbox=bbox
        )
    finally:
        gdal.SetThreadLocalConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", None)
        gdal.SetThreadLocalConfigOption(
            "CPL_VSIL_CURL_ALLOWED_EXTENSIONS", None
        )


def _de_tile_keys(bbox):
    """(east km, north km) of the 1 km tiles touching an EPSG:25832 bbox."""
    x0, y0, x1, y1 = bbox
    return [
        (east, north)
        for north in range(
            int(math.floor(y0 / 1000)), int(math.floor((y1 - 1e-6) / 1000)) + 1
        )
        for east in range(
            int(math.floor(x0 / 1000)), int(math.floor((x1 - 1e-6) / 1000)) + 1
        )
    ]


def _rlp_years(ctx):
    """{(east km, north km): [year, ...] newest first} from the RLP file list.

    The list is an HTML page of about 11 MB (about 0.5 MB sent compressed).
    """
    ctx.report("Checking the Rhineland-Palatinate tile list…")
    req = urllib.request.Request(
        _RLP_DGM1,
        headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:  # nosec B310
        data = r.read()
        if r.headers.get("Content-Encoding", "").lower() == "gzip":
            data = gzip.decompress(data)
    ctx.check()
    years = {}
    for east, north, year in set(
        re.findall(
            r"dgm1_32_(\d+)_(\d+)_1_rp_(\d{4})\.tif",
            data.decode("utf-8", "replace"),
        )
    ):
        years.setdefault((int(east), int(north)), []).append(int(year))
    for tile_years in years.values():
        tile_years.sort(reverse=True)
    return years


def _tile_coverage(path):
    """Share of cells holding values in a downloaded tile."""
    from osgeo import gdal

    dataset = gdal.Open(path)
    if dataset is None:
        return 0.0
    band = dataset.GetRasterBand(1)
    arr = band.ReadAsArray()
    nodata = band.GetNoDataValue()
    dataset = None
    valid = np.isfinite(arr) & (np.abs(arr) < 1e30)
    if nodata is not None:
        valid &= arr != nodata
    return float(valid.mean()) if arr.size else 0.0


def _normalised_tile(path, epsg, cell, vrt_path):
    """The tile as a VRT on one grid: CRS, cell edges and NoData made the same.

    The tiles of one state differ by year: RLP's 2022 tiles are 1001 × 1001
    cells shifted by half a cell with the CRS set, its 2026 tiles 1000 × 1000
    without a CRS, and the NoData values differ. Unlike tiles left as they are
    would be dropped or misplaced when combined.
    """
    from osgeo import gdal

    source = gdal.Open(path)
    nodata = (
        source.GetRasterBand(1).GetNoDataValue()
        if source is not None
        else None
    )
    source = None
    out = gdal.Warp(
        vrt_path,
        path,
        format="VRT",
        srcSRS=f"EPSG:{epsg}",
        dstSRS=f"EPSG:{epsg}",
        srcNodata=nodata,
        dstNodata=-9999.0,
        xRes=cell,
        yRes=cell,
        targetAlignedPixels=True,
        resampleAlg="bilinear",
        outputType=gdal.GDT_Float32,
    )
    if out is None:
        raise RemoteDemError("Could not combine the downloaded tiles.")
    out = None
    return vrt_path


def _prefetch_de_first(ctx, spec, wanted, urls, parts_dir, store):
    ('Fetch the newest tile of each square, without fetching unneeded '
     'years.')
    keys = sorted(wanted)
    results = {}
    connections = _DownloadConnections()
    pending = deque()
    next_key = 0
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            while next_key < len(keys) or pending:
                ctx.check()
                while next_key < len(keys) and len(pending) < 2:
                    key = keys[next_key]
                    next_key += 1
                    year = wanted[key][0]
                    code = "{}_{}".format(*key)
                    if store is not None and store.find(
                        spec["prefix"], year, code
                    ):
                        continue
                    url = urls(key)[0]
                    dest = os.path.join(parts_dir, url.rsplit("/", 1)[1])
                    future = executor.submit(
                        _download_chunk,
                        ctx,
                        connections,
                        url,
                        dest,
                        store,
                        spec["prefix"],
                        year,
                        code,
                    )
                    pending.append((key, future))
                if pending:
                    key, future = pending.popleft()
                    try:
                        results[key] = future.result()
                    except urllib.error.HTTPError as error:
                        if error.code != 404:
                            raise
                        results[key] = None
                    ctx.report(
                        f"Fetching {spec['label']}… {len(results)}/{len(keys)}"
                    )
    finally:
        connections.close()
    return results


def _fetch_de_tiles(ctx, spec, bbox, out_dir, store=None, kept=None):
    """Download the German state's 1 km DGM1 tiles and combine them.

    With ``store``, a tile kept from an earlier analysis is used instead of
    downloading it: for Rhineland-Palatinate by its survey year (the file
    list is still read each time, so a newer survey is fetched); Bavaria's
    tiles have no year."""
    cell = spec["cell"]
    check_size(bbox, cell)
    bbox = snap_bbox(bbox, cell)
    keys = _de_tile_keys(bbox)
    listed = None
    if kept is not None:
        # The QLAE folder: the kept tiles and their years only.
        wanted = {
            key: kept["{}_{}".format(*key)]
            for key in keys
            if "{}_{}".format(*key) in kept
        }

        def urls(key):
            # Not fetched: only names for the work files, one per year.
            return [
                "kept/{}_{}_{}.tif".format(key[0], key[1], year)
                for year in wanted[key]
            ]
    elif spec["state"] == "rlp":
        years = _rlp_years(ctx)
        listed = list(years)  # the whole state's tiles, for the coverage mesh
        wanted = {key: years[key] for key in keys if key in years}

        def urls(key):
            east, north = key
            return [
                f"{_RLP_DGM1}dgm1_32_{east}_{north}_1_rp_{year}.tif"
                for year in wanted[key]
            ]
    else:
        wanted = {key: [None] for key in keys}

        def urls(key):
            east, north = key
            return [f"{_BY_DGM1}{east}_{north}.tif"]

    if not wanted:
        raise RemoteDemError(
            "No elevation data for this area (outside the source coverage)."
        )
    parts_dir = os.path.join(out_dir, f"parts_{spec['prefix']}")
    os.makedirs(parts_dir, exist_ok=True)
    prefetched = (
        _prefetch_de_first(ctx, spec, wanted, urls, parts_dir, store)
        if kept is None
        else {}
    )
    layered = []  # (priority, vrt path): the newest survey drawn last, on top
    survey_years = set()
    found = set()  # tiles that exist (fetched or kept), for the coverage mesh
    total = len(wanted)
    fallback_connections = _DownloadConnections()
    try:
        for index, key in enumerate(sorted(wanted), start=1):
            ctx.check()
            ctx.report(f"Fetching {spec['label']}… {index}/{total}")
            # The newest survey first; older ones only fill what it leaves
            # empty.
            for rank, url in enumerate(urls(key)):
                name = url.rsplit("/", 1)[1]
                code = "{}_{}".format(*key)
                year = wanted[key][rank]
                if rank == 0 and key in prefetched:
                    dest = prefetched[key]
                    if (
                        dest is None
                    ):  # The newest listed tile was absent (404).
                        continue
                    if store is not None:
                        store.downloaded += 1
                else:
                    dest = (
                        store.find(spec["prefix"], year, code)
                        if store is not None
                        else None
                    )
                    if dest is not None:
                        store.reused += 1
                    elif kept is not None:
                        continue
                    else:
                        # Downloaded on the store's disk, then moved in whole.
                        incoming = (
                            store.incoming()
                            if store is not None
                            else parts_dir
                        )
                        try:
                            dest = os.path.join(incoming, name)
                            try:
                                fallback_connections.download(ctx, url, dest)
                            except urllib.error.HTTPError as error:
                                if error.code == 404:
                                    # A missing newest survey may have an older
                                    # one.
                                    continue
                                raise
                            if store is not None:
                                dest = store.keep(
                                    spec["prefix"], year, code, dest
                                )
                                store.downloaded += 1
                        finally:
                            if store is not None:
                                store.drop_incoming(incoming)
                found.add(key)
                vrt = os.path.join(
                    parts_dir, os.path.splitext(name)[0] + "_grid.vrt"
                )
                layered.append(
                    (rank, _normalised_tile(dest, spec["epsg"], cell, vrt))
                )
                if wanted[key][rank] is not None:
                    survey_years.add(wanted[key][rank])
                if _tile_coverage(dest) >= 0.999:
                    break
    finally:
        fallback_connections.close()
    if not layered:
        raise RemoteDemError(
            "No elevation data for this area (outside the source coverage)."
        )
    # Later sources of a VRT are drawn over earlier ones: oldest first.
    sources = [
        path for _rank, path in sorted(layered, key=lambda item: -item[0])
    ]
    ctx.report("Combining…")
    path = _finish_mosaic(
        ctx, sources, out_dir, spec["prefix"], spec["epsg"], bbox=bbox
    )
    info = {"credit": spec["credit"].format(year=datetime.date.today().year)}
    if (
        survey_years
    ):  # Rhineland-Palatinate: the survey years in the file names
        info["survey_years"] = sorted(survey_years)
    # For terrain/coverage.py (taken out before the facts are recorded).
    if listed is not None:
        info["_coverage_list"] = listed
    info["_coverage_found"] = sorted(found)
    return path, info


def fetch_remote_dem(ctx, key, bbox, out_dir, store=None, kept=None):
    """Fetch an overseas DEM for bbox (in the source CRS).

    Returns (GeoTIFF path, extra source facts for the analysis record): the
    German states give their required credit line and, for
    Rhineland-Palatinate, the survey years used; the others give none.
    ``store`` (core/dem_store.TileStore) keeps the German tiles for reuse.
    ``kept`` ({tile code: [year, ...]}, terrain/kept_tiles.codes): only
    these kept tiles are used and nothing is fetched (the QLAE folder).
    """
    spec = REMOTE_SOURCES[key]
    os.makedirs(out_dir, exist_ok=True)
    if spec["kind"] == "nzcog":
        path = _fetch_nzcog(ctx, spec, bbox, out_dir)
        return path, {
            "_coverage_names": getattr(ctx, "coverage_names", None) or []
        }
    if spec["kind"] == "de_tiles":
        return _fetch_de_tiles(ctx, spec, bbox, out_dir, store, kept)
    return _fetch_ogc(ctx, spec, bbox, out_dir, store, kept), {}
