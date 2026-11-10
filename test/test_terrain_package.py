# -*- coding: utf-8 -*-

import os
import json
import math
import tempfile
import unittest
import zipfile

import numpy as np
from osgeo import gdal, osr

from ..analyses.terrain import create_terrain_package


class _Context:
    def report(self, message):
        self.last_message = message

    def check(self):
        return None

    def cancelled(self):
        return False


class TerrainPackageTest(unittest.TestCase):
    def test_local_dem_creates_portable_zip_and_vsi_rasters(self):
        with tempfile.TemporaryDirectory() as folder:
            dem_path = os.path.join(folder, "source.tif")
            package_path = os.path.join(folder, "analyses", "sample.zip")
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(32654)
            driver = gdal.GetDriverByName("GTiff")
            dataset = driver.Create(dem_path, 100, 100, 1, gdal.GDT_Float32)
            dataset.SetGeoTransform((500000, 5, 0, 3900500, 0, -5))
            dataset.SetProjection(srs.ExportToWkt())
            y, x = np.mgrid[0:100, 0:100]
            dataset.GetRasterBand(1).WriteArray((x + y * 0.5).astype(np.float32))
            dataset = None

            result = create_terrain_package(
                {
                    "analysis_name": "sample",
                    "package_path": package_path,
                    "overwrite": False,
                    "plugin_version": "test",
                    "source_kind": "local",
                    "source_path": dem_path,
                    "source_name": "Synthetic DEM",
                    "gsi_dataset": "best",
                    "outputs": ["hillshade", "slope", "cs_map"],
                    "area_mode": "polygon",
                    "preview_landscape": False,
                    "aoi_wkt": (
                        "POLYGON ((500100 3900400, 500400 3900400, "
                        "500400 3900100, 500100 3900100, 500100 3900400))"
                    ),
                    "aoi_crs_wkt": srs.ExportToWkt(),
                },
                _Context(),
            )
            self.assertEqual(result["package_path"], package_path)
            with zipfile.ZipFile(package_path) as archive:
                names = set(archive.namelist())
            self.assertTrue(
                {
                    "analysis.json",
                    "area/aoi.wkt",
                    "area/frame.wkt",
                    "data/dem.tif",
                    "outputs/hillshade.tif",
                    "outputs/slope_deg.tif",
                    "outputs/cs_map.tif",
                    "styles/slope.qml",
                }.issubset(names)
            )
            slope = gdal.Open("/vsizip/" + package_path + "/outputs/slope_deg.tif")
            self.assertIsNotNone(slope)
            self.assertGreater(slope.RasterXSize, 0)
            self.assertGreater(slope.RasterYSize, 0)
            expected_size = (slope.RasterXSize, slope.RasterYSize)
            for member in (
                "data/dem.tif",
                "outputs/hillshade.tif",
                "outputs/cs_map.tif",
            ):
                raster = gdal.Open("/vsizip/" + package_path + "/" + member)
                self.assertIsNotNone(raster)
                self.assertEqual((raster.RasterXSize, raster.RasterYSize), expected_size)
                raster = None
            self.assertAlmostEqual(
                slope.RasterXSize / slope.RasterYSize,
                1.0 / math.sqrt(2.0),
                delta=0.02,
            )
            with zipfile.ZipFile(package_path) as archive:
                manifest = json.loads(archive.read("analysis.json"))
            self.assertEqual(manifest["output_frame"]["orientation"], "portrait")
            self.assertEqual(
                manifest["analysis_grid"]["columns"], slope.RasterXSize
            )
            self.assertEqual(manifest["analysis_grid"]["rows"], slope.RasterYSize)
            slope = None

            landscape_path = os.path.join(folder, "analyses", "landscape.zip")
            create_terrain_package(
                {
                    "analysis_name": "landscape",
                    "package_path": landscape_path,
                    "overwrite": False,
                    "plugin_version": "test",
                    "source_kind": "local",
                    "source_path": dem_path,
                    "source_name": "Synthetic DEM",
                    "gsi_dataset": "best",
                    "outputs": ["hillshade"],
                    "area_mode": "polygon",
                    "preview_landscape": True,
                    "aoi_wkt": (
                        "POLYGON ((500100 3900400, 500400 3900400, "
                        "500400 3900100, 500100 3900100, 500100 3900400))"
                    ),
                    "aoi_crs_wkt": srs.ExportToWkt(),
                },
                _Context(),
            )
            landscape = gdal.Open(
                "/vsizip/" + landscape_path + "/outputs/hillshade.tif"
            )
            self.assertAlmostEqual(
                landscape.RasterXSize / landscape.RasterYSize,
                math.sqrt(2.0),
                delta=0.02,
            )
            with zipfile.ZipFile(landscape_path) as archive:
                landscape_manifest = json.loads(archive.read("analysis.json"))
            self.assertEqual(
                landscape_manifest["output_frame"]["orientation"], "landscape"
            )
            landscape = None


if __name__ == "__main__":
    unittest.main()
