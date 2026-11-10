# -*- coding: utf-8 -*-
"""Terrain derivatives adapted from Forestry Operations Lite (GPL-3.0-or-later)."""

"""
地形解析アルゴリズム群
  compute_slope_deg  : Horn法で傾斜角[度]を計算
  compute_hillshade  : 陰影起伏（0-255）を計算
  compute_curvature  : 平均曲率（ラプラシアン）を計算
  compute_shc        : SHC = 曲率の局所標準偏差（FOP用）
  d8_flow_direction  : D8流向コードを返す
  flow_accumulation  : 上流集水セル数を累積（D8）
  mfd_proportions    : MFD（多方向）の分配率 (rows,cols,8) を返す
  dinf_proportions   : D∞（Tarboton）の分配率 (rows,cols,8) を返す
  multi_flow_accumulation : 分配率から上流集水を累積（MFD/D∞ 共通）
  compute_twi        : TWI = ln(A / tan(β))
  stability_fs       : 無限斜面安定解析（FS）
  rational_flow      : 合理式による流量推測 [m³/s]
  cum_travel_time_to_outlet : 各セル→出口の累積移動時間（時間‐面積法の基準）
  triangular_hyetograph     : 三角形の設計ハイエトグラフ
  time_area_flow_metrics    : 時間‐面積法/Clark 単位図法による負荷3指標
"""
import numpy as np

# セルごとの Python ループで progress_cb を呼ぶ間隔（セル数）。
# 一番重い MFD の累積（約8µs/セル）でも 0.15秒ほどに1回になる。
_PROGRESS_EVERY = 16384

FLOW_MODEL_VERSION = "time-area-clark-d8-canopy-2"
FLOW_ROUTING_OPTIONS = {"clark_k": 0.75, "n_time": 24}
FLOW_CANOPY_COEFFICIENTS = {
    "c_forest": 0.15, "c_bare": 0.55,
    "v_forest": 0.30, "v_bare": 0.60,
    "cs_forest": 10.0, "cs_bare": 3.0,
}


def _sub_progress(progress_cb, lo, hi):
    """progress_cb の 0..1 を lo..hi に割り当てた callback を返す（None は None）。"""
    if progress_cb is None:
        return None
    return lambda frac: progress_cb(lo + (hi - lo) * frac)


def compute_slope_deg(dem, cell_size):
    """Horn法で傾斜角（度）を計算"""
    pad = np.pad(dem, 1, mode="edge")
    dzdx = (pad[1:-1, 2:] - pad[1:-1, :-2]) / (2.0 * cell_size)
    dzdy = (pad[2:, 1:-1] - pad[:-2, 1:-1]) / (2.0 * cell_size)
    slope_rad = np.arctan(np.sqrt(dzdx ** 2 + dzdy ** 2))
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
    slope = np.pi / 2.0 - np.arctan(np.sqrt(x ** 2 + y ** 2))
    aspect = np.arctan2(-x, y)
    az_rad = np.radians(360.0 - azimuth)
    alt_rad = np.radians(altitude)
    shaded = (
        np.sin(alt_rad) * np.sin(slope)
        + np.cos(alt_rad) * np.cos(slope) * np.cos((az_rad - np.pi / 2.0) - aspect)
    )
    result = np.clip((shaded + 1.0) / 2.0, 0.0, 1.0) * 255.0
    result[np.isnan(dem)] = np.nan
    return result.astype(np.float32)


def compute_curvature(dem, cell_size):
    """
    平均曲率（ラプラシアン近似）を計算する。
    正値=凸地形（尾根）、負値=凹地形（谷）。
    """
    pad = np.pad(dem, 1, mode="edge")
    r = (pad[1:-1, 2:] - 2.0 * dem + pad[1:-1, :-2]) / (cell_size ** 2)  # d²z/dx²
    t = (pad[2:, 1:-1] - 2.0 * dem + pad[:-2, 1:-1]) / (cell_size ** 2)  # d²z/dy²
    curv = -(r + t)
    curv[np.isnan(dem)] = np.nan
    return curv


