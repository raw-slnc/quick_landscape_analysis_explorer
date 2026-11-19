"""Network scheduling without calling the public elevation services."""

import os
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

from ..core.dem_store import TileStore
from ..terrain import remote_dem


class _Context:
    def __init__(self):
        self.messages = []

    def check(self):
        pass

    def report(self, message):
        self.messages.append(message)

    def cancelled(self):
        return False


class RemoteDemDownloadTest(unittest.TestCase):
    def test_transient_502_is_retried_before_keeping_chunk(self):
        context = _Context()
        calls = []

        def download(_ctx, url, dest):
            calls.append(url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(url, 502, "Bad Gateway", {}, None)
            with open(dest, "wb") as output:
                output.write(b"TIFF")

        with tempfile.TemporaryDirectory() as root:
            store = TileStore(os.path.join(root, "kept"), "folder")
            with patch.object(remote_dem, "_wait_for_retry") as waiting:
                path = remote_dem._download_chunk(
                    context, type("Connection", (), {"download": staticmethod(download)})(),
                    "https://example.test/tile.tif", os.path.join(root, "tile.tif"),
                    store, "ign", None, "1_2",
                )
            with open(path, "rb") as kept:
                self.assertEqual(kept.read(), b"TIFF")
        self.assertEqual(len(calls), 2)
        waiting.assert_called_once_with(context, 1)
        self.assertTrue(any("HTTP 502" in message for message in context.messages))

    def test_missing_chunk_404_is_not_retried(self):
        context = _Context()
        calls = []

        def download(_ctx, url, _dest):
            calls.append(url)
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        with tempfile.TemporaryDirectory() as root:
            with patch.object(remote_dem, "_wait_for_retry") as waiting:
                with self.assertRaises(urllib.error.HTTPError):
                    remote_dem._download_with_retries(
                        context, download, "https://example.test/missing.tif",
                        os.path.join(root, "missing.tif"),
                    )
            self.assertEqual(len(calls), 1)
            waiting.assert_not_called()

    def test_https_connection_is_reused_for_sequential_chunks(self):
        created = []

        class Response:
            status = 200

            def __init__(self):
                self.remaining = [b"TIFF", b""]

            def getheader(self, _name, _default=""):
                return "image/tiff"

            def read(self, _size):
                return self.remaining.pop(0)

        class Connection:
            def __init__(self, *args, **kwargs):
                created.append(self)
                self.requests = []

            def request(self, method, path, headers):
                self.requests.append((method, path, headers))

            def getresponse(self):
                return Response()

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as root:
            with patch.object(remote_dem.http.client, "HTTPSConnection", Connection), \
                    patch.object(remote_dem.urllib.request, "getproxies", return_value={}):
                pool = remote_dem._DownloadConnections()
                try:
                    for code in ("a", "b"):
                        pool.download(_Context(), "https://example.test/" + code,
                                      os.path.join(root, code + ".tif"))
                finally:
                    pool.close()
        self.assertEqual(len(created), 1)
        self.assertEqual([request[1] for request in created[0].requests], ["/a", "/b"])

    def test_ogc_overlaps_two_requests_and_preserves_mosaic_order(self):
        context = _Context()
        spec = dict(label="test", prefix="test", cell=1, chunk_px=10,
                    url="https://example.test/?x0={x0}&y0={y0}&x1={x1}&y1={y1}",
                    layer="test", epsg=2154)
        two_started = threading.Barrier(2, timeout=5)
        calls = []

        def download(_self, _ctx, url, dest, timeout=180, _retry=True,
                     on_headers=None, on_bytes=None):
            calls.append(url)
            if len(calls) <= 2:
                two_started.wait()
            if on_headers is not None:
                on_headers(4)
            with open(dest, "wb") as output:
                output.write(b"TIFF")
            if on_bytes is not None:
                on_bytes(4)

        with tempfile.TemporaryDirectory() as root:
            store = TileStore(os.path.join(root, "kept"), "folder")
            with patch.object(remote_dem._DownloadConnections, "download", download), \
                    patch.object(remote_dem, "_finish_mosaic", side_effect=lambda _ctx, paths, *_a, **_k: paths):
                paths = remote_dem._fetch_ogc(context, spec, (0, 0, 20, 20), root, store)
                reused_paths = remote_dem._fetch_ogc(context, spec, (0, 0, 20, 20), root, store)
            self.assertEqual([os.path.basename(path) for path in paths],
                             ["0_0.tif", "1_0.tif", "0_1.tif", "1_1.tif"])
            self.assertEqual(store.downloaded, 4)
            self.assertEqual(store.reused, 4)
            self.assertEqual(len(calls), 4)
            self.assertEqual(reused_paths, paths)
            self.assertTrue(any("DEM transfer: 0.0 MB /" in message for message in context.messages))
            self.assertTrue(any("4/4 downloaded, 0 reused, 0 active" in message
                                for message in context.messages))

    def test_transfer_progress_estimates_then_confirms_total(self):
        context = _Context()
        progress = remote_dem._OgcTransferProgress(context, "AHN", 3, 1)
        progress.started()
        progress.headers(0, 2_000_000)
        progress.bytes(500_000)
        progress._report(force=True)
        self.assertIn("0.5 MB / about 4.0 MB", context.messages[-1])
        progress.headers(1, 3_000_000)
        self.assertIn("0.5 MB / 5.0 MB", context.messages[-1])

    def test_german_newest_404_falls_back_without_extra_years(self):
        context = _Context()
        spec = dict(label="test", prefix="test", cell=1, state="rlp",
                    epsg=25832, credit="credit {year}")
        calls = []

        def download(_self, _ctx, url, dest, timeout=180):
            calls.append(url)
            if "100_200_1_rp_2026" in url:
                raise urllib.error.HTTPError(url, 404, "missing", {}, None)
            with open(dest, "wb") as output:
                output.write(b"TIFF")

        years = {(100, 200): [2026, 2022], (101, 200): [2026, 2022]}
        with tempfile.TemporaryDirectory() as root:
            store = TileStore(os.path.join(root, "kept"), "folder")
            with patch.object(remote_dem, "_rlp_years", return_value=years), \
                    patch.object(remote_dem._DownloadConnections, "download", download), \
                    patch.object(remote_dem, "_tile_coverage", return_value=1.0), \
                    patch.object(remote_dem, "_normalised_tile", side_effect=lambda path, *_a: path), \
                    patch.object(remote_dem, "_finish_mosaic", side_effect=lambda _ctx, paths, *_a, **_k: paths):
                paths, info = remote_dem._fetch_de_tiles(
                    context, spec, (100000, 200000, 102000, 201000), root, store)
            self.assertEqual([os.path.basename(path) for path in paths],
                             ["100_200.tif", "101_200.tif"])
            self.assertEqual(info["survey_years"], [2022, 2026])
            self.assertEqual(store.downloaded, 2)
            self.assertEqual(store.reused, 0)
            self.assertEqual(len(calls), 3)
            self.assertFalse(any("101_200_1_rp_2022" in url for url in calls))


if __name__ == "__main__":
    unittest.main()
