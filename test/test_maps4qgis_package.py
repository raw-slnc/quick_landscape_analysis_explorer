# -*- coding: utf-8 -*-
"""The guided GeoPDF must not damage the portable analysis ZIP."""

import json
import os
import tempfile
import unittest
import zipfile
from unittest import mock

from ..core import packages


class Maps4QGISPackageTest(unittest.TestCase):
    def test_staged_geopdf_does_not_change_loaded_zip_until_swap(self):
        with tempfile.TemporaryDirectory() as folder:
            package_path = os.path.join(folder, "analysis.zip")
            pdf_path = os.path.join(folder, "map.pdf")
            with zipfile.ZipFile(package_path, "w") as archive:
                archive.writestr(
                    "analysis.json",
                    json.dumps(
                        {
                            "analysis_name": "test",
                            "outputs": {},
                        }
                    ),
                )
                archive.writestr("outputs/hillshade.tif", b"analysis data")
            with open(pdf_path, "wb") as stream:
                stream.write(b"PDF")

            _manifest, staged = packages.stage_geopdf(package_path, pdf_path)
            try:
                with zipfile.ZipFile(package_path) as archive:
                    self.assertNotIn(
                        packages.GEOPDF_MEMBER, archive.namelist()
                    )
                with zipfile.ZipFile(staged) as archive:
                    self.assertEqual(
                        archive.read(packages.GEOPDF_MEMBER), b"PDF"
                    )
                    self.assertEqual(
                        archive.read("outputs/hillshade.tif"), b"analysis data"
                    )
                os.replace(staged, package_path)
                with zipfile.ZipFile(package_path) as archive:
                    self.assertEqual(
                        archive.read(packages.GEOPDF_MEMBER), b"PDF"
                    )
            finally:
                if os.path.exists(staged):
                    os.remove(staged)

    def test_denied_zip_swap_keeps_original_and_removes_staged_file(self):
        with tempfile.TemporaryDirectory() as folder:
            package_path = os.path.join(folder, "analysis.zip")
            pdf_path = os.path.join(folder, "map.pdf")
            with zipfile.ZipFile(package_path, "w") as archive:
                archive.writestr(
                    "analysis.json",
                    json.dumps(
                        {
                            "analysis_name": "test",
                            "outputs": {},
                        }
                    ),
                )
                archive.writestr("outputs/hillshade.tif", b"analysis data")
            with open(pdf_path, "wb") as stream:
                stream.write(b"PDF")
            with open(package_path, "rb") as stream:
                original = stream.read()

            with mock.patch.object(
                packages.os,
                "replace",
                side_effect=PermissionError(5, "denied"),
            ):
                with self.assertRaises(PermissionError):
                    packages.replace_geopdf(package_path, pdf_path)

            with open(package_path, "rb") as stream:
                self.assertEqual(stream.read(), original)
            self.assertEqual(
                sorted(os.listdir(folder)), ["analysis.zip", "map.pdf"]
            )

    def test_replacing_pdf_preserves_analysis_and_has_one_pdf(self):
        with tempfile.TemporaryDirectory() as folder:
            package_path = os.path.join(folder, "analysis.zip")
            pdf_path = os.path.join(folder, "map.pdf")
            with zipfile.ZipFile(package_path, "w") as archive:
                archive.writestr(
                    "analysis.json",
                    json.dumps(
                        {
                            "analysis_name": "試験",
                            "outputs": {
                                "dem_geopdf": "data/dem_for_geopdf.tif",
                            },
                        }
                    ),
                )
                archive.writestr("data/dem_for_geopdf.tif", b"original DEM")
                archive.writestr("outputs/hillshade.tif", b"original analysis")

            for payload in (b"first PDF", b"replacement PDF"):
                with open(pdf_path, "wb") as stream:
                    stream.write(payload)
                packages.replace_geopdf(package_path, pdf_path)

            with zipfile.ZipFile(package_path) as archive:
                self.assertEqual(
                    archive.namelist().count(packages.GEOPDF_MEMBER), 1
                )
                self.assertEqual(
                    archive.read(packages.GEOPDF_MEMBER), b"replacement PDF"
                )
                self.assertEqual(
                    archive.read("data/dem_for_geopdf.tif"), b"original DEM"
                )
                self.assertEqual(
                    archive.read("outputs/hillshade.tif"), b"original analysis"
                )
            self.assertEqual(
                packages.read_manifest(package_path)["outputs"]["geopdf"],
                packages.GEOPDF_MEMBER,
            )

            with self.assertRaises(OSError):
                packages.replace_geopdf(
                    package_path, os.path.join(folder, "missing.pdf")
                )
            with zipfile.ZipFile(package_path) as archive:
                self.assertEqual(
                    archive.read(packages.GEOPDF_MEMBER), b"replacement PDF"
                )


if __name__ == "__main__":
    unittest.main()
