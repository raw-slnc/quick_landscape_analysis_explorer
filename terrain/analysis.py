# -*- coding: utf-8 -*-
"""Terrain derivatives.

地形解析アルゴリズム群
  compute_slope_deg  : Horn法で傾斜角[度]を計算
  compute_hillshade  : 陰影起伏（0-255）を計算
  compute_curvature  : 平均曲率（ラプラシアン）を計算

流れの計算は terrain/catchment_grid.py（流域）と terrain/valley_flow.py
（沢の成立）にある。
"""

import numpy as np


def compute_slope_deg(dem, cell_size):
    """Horn法で傾斜角（度）を計算"""
    pad = np.pad(dem, 1, mode="edge")
    dzdx = (pad[1:-1, 2:] - pad[1:-1, :-2]) / (2.0 * cell_size)
    dzdy = (pad[2:, 1:-1] - pad[:-2, 1:-1]) / (2.0 * cell_size)
    slope_rad = np.arctan(np.sqrt(dzdx**2 + dzdy**2))
    result = np.degrees(slope_rad)
    result[np.isnan(dem)] = np.nan
    return result


def compute_hillshade(dem, cell_size, azimuth=315.0, altitude=45.0):
    """
    陰影起伏（Hillshade）を計算する。0-255の輝度値を返す。

    背景地形の表示用（terrain背景レイヤ）。既定は光源方位315°・高度45°
    （地図表現で一般的な左上光源）。行方向（軸0）は南向きに増加する
    ラスタ配列を前提とする（QGIS/GDALの標準的な読み込み順）。
    """
    x, y = np.gradient(dem, cell_size)
    slope = np.pi / 2.0 - np.arctan(np.sqrt(x**2 + y**2))
    aspect = np.arctan2(-x, y)
    az_rad = np.radians(360.0 - azimuth)
    alt_rad = np.radians(altitude)
    shaded = np.sin(alt_rad) * np.sin(slope) + np.cos(alt_rad) * np.cos(
        slope
    ) * np.cos((az_rad - np.pi / 2.0) - aspect)
    result = np.clip((shaded + 1.0) / 2.0, 0.0, 1.0) * 255.0
    result[np.isnan(dem)] = np.nan
    return result.astype(np.float32)


def compute_curvature(dem, cell_size):
    """
    平均曲率（ラプラシアン近似）を計算する。
    正値=凸地形（尾根）、負値=凹地形（谷）。
    """
    pad = np.pad(dem, 1, mode="edge")
    r = (pad[1:-1, 2:] - 2.0 * dem + pad[1:-1, :-2]) / (
        cell_size**2
    )  # d²z/dx²
    t = (pad[2:, 1:-1] - 2.0 * dem + pad[:-2, 1:-1]) / (
        cell_size**2
    )  # d²z/dy²
    curv = -(r + t)
    curv[np.isnan(dem)] = np.nan
    return curv
