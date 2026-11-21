# -*- coding: utf-8 -*-
"""Serve the bundled Maps4QGIS page and one analysis on loopback."""

import json
import mimetypes
import os
import secrets
import threading
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import packages


APP_DIRECTORY = os.path.join(os.path.dirname(__file__), "maps4qgis")
# Keep the browser origin stable so its desktop Map Library survives QGIS
# restarts.
PREFERRED_PORT = 48591
_server = None
STATIC_FILES = {
    "index.html",
    "app-en.html",
    "app-ja.html",
    "manifest.json",
    "sw.js",
    "leaflet.min.css",
    "leaflet.min.js",
    "pdf.mjs",
    "pdf.worker.mjs",
    "proj4.js",
    "datums.json",
    "wmmhr.js",
    "WMMHR.COF",
    "icon-192.png",
    "icon-512.png",
    "screenshot-mobile.png",
    "LICENSE-apache-2.0.txt",
    "LICENSE-leaflet.txt",
    "LICENSE-proj4js.txt",
}


class Maps4QGISServer:
    def __init__(self):
        self._sessions = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                # A loopback listener must not answer a rebinding host name.
                host = self.headers.get("Host", "").split(":", 1)[0]
                if host not in ("127.0.0.1", "localhost"):
                    self.send_error(403)
                    return
                url = urllib.parse.urlsplit(self.path)
                if url.path == "/":
                    self.send_response(302)
                    self.send_header("Location", "/maps4qgis/")
                    self.end_headers()
                    return
                if url.path.startswith("/api/"):
                    outer._serve_api(self, url)
                    return
                name = url.path.removeprefix("/maps4qgis/")
                if url.path == "/maps4qgis/":
                    name = "index.html"
                if name not in STATIC_FILES:
                    self.send_error(404)
                    return
                path = os.path.join(APP_DIRECTORY, name)
                try:
                    with open(path, "rb") as stream:
                        payload = stream.read()
                except OSError:
                    self.send_error(404)
                    return
                content_type = (
                    mimetypes.guess_type(name)[0] or "application/octet-stream"
                )
                if name.endswith(".mjs"):
                    content_type = "text/javascript"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        try:
            self._http = ThreadingHTTPServer(
                ("127.0.0.1", PREFERRED_PORT), Handler
            )
        except OSError:
            # Another process may use the port; the current session still
            # works.
            self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._http.serve_forever, daemon=True
        )
        self._thread.start()

    def guided_url(self, package_path):
        manifest = packages.read_manifest(package_path)
        outputs = manifest.get("outputs") or {}
        if not outputs.get("geopdf"):
            raise packages.PackageError("geopdf_missing")
        token = secrets.token_urlsafe(24)
        self._sessions[token] = os.path.abspath(package_path)
        return "{}?session={}".format(self.desktop_url(), token)

    def desktop_url(self):
        return "http://127.0.0.1:{}/maps4qgis/".format(self._http.server_port)

    def _serve_api(self, handler, url):
        query = urllib.parse.parse_qs(url.query)
        token = query.get("session", [""])[0]
        path = self._sessions.get(token)
        if not path:
            handler.send_error(404)
            return
        headers_sent = False
        try:
            manifest = packages.read_manifest(path)
            outputs = manifest.get("outputs") or {}
            members = {
                "pdf": outputs.get("geopdf"),
                "dem": outputs.get("dem_geopdf"),
            }
            if url.path == "/api/session":
                items = [
                    {"kind": kind, "name": os.path.basename(member)}
                    for kind, member in members.items()
                    if member
                ]
                payload = json.dumps(
                    {
                        "analysis": manifest["analysis_name"],
                        "files": items,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                handler.send_response(200)
                handler.send_header(
                    "Content-Type", "application/json; charset=utf-8"
                )
                handler.send_header("Content-Length", str(len(payload)))
                handler.send_header("Cache-Control", "no-store")
                handler.send_header("Referrer-Policy", "no-referrer")
                handler.end_headers()
                headers_sent = True
                handler.wfile.write(payload)
                return
            kind = url.path.removeprefix("/api/file/")
            member = (
                members.get(kind)
                if url.path.startswith("/api/file/")
                else None
            )
            if not member:
                handler.send_error(404)
                return
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo(member)
                handler.send_response(200)
                handler.send_header(
                    "Content-Type",
                    "application/pdf" if kind == "pdf" else "image/tiff",
                )
                handler.send_header("Content-Length", str(info.file_size))
                handler.send_header("Cache-Control", "no-store")
                handler.send_header("Referrer-Policy", "no-referrer")
                handler.send_header("X-Content-Type-Options", "nosniff")
                handler.end_headers()
                headers_sent = True
                with archive.open(info) as stream:
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        handler.wfile.write(chunk)
        except (
            OSError,
            KeyError,
            ValueError,
            zipfile.BadZipFile,
            packages.PackageError,
        ):
            if not headers_sent:
                try:
                    handler.send_error(404)
                except OSError:
                    pass

    def close(self):
        self._http.shutdown()
        self._http.server_close()
        self._thread.join(timeout=2)


def get_server():
    global _server
    if _server is None:
        _server = Maps4QGISServer()
    return _server


def stop_server():
    global _server
    if _server is not None:
        _server.close()
        _server = None
