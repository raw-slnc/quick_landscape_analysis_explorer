# -*- coding: utf-8 -*-
# Adapted from Forestry Operations Lite (same author).
"""Virtual Shizuoka LP point cloud S3 utilities.

タイルコード体系（EPSG:6676、平面直角第8系）:
  08{folder}{XX:02d}{AA:02d}
    folder : 2文字 (例 OE, NE, MC ...)
    XX     : 4km×3km ブロック内の列(個位)・行(十位)
    AA     : タイルの列(個位)・行(十位)

座標 (EPSG:6676, メートル単位) ── 標準フォーマット (2019〜2021, 2025 確認済み):
    タイルサイズ: 400m × 300m、AA は単純行列（tens=行, units=列）
    x_center = folder_x0 + (XX%10)*4000 + (AA%10)*400 + 200
    y_center = folder_y0 - (XX//10)*3000 - (AA//10)*300 - 150
    folder_x0 = (ord(folder[1]) - ord('E')) * 40000
    folder_y0 = -(ord(folder[0]) - ord('M') + 2) * 30000

【注意】2022年データ（LD/MD フォルダで確認）はタイル仕様が異なる:
    タイルサイズ: 1000m × 750m、AA は 2 階層 Z 曲線エンコード
      tens桁 = 2×2 サブブロック (1=左上, 2=右上, 3=左下, 4=右下)
      units桁 = サブブロック内位置 (1=左上, 2=右上, 3=左下, 4=右下)
    そのため tile_bbox() / tiles_for_extent() の座標計算は 2022 年データと
    一致しない。新年度のデータを追加する際は実タイルをダウンロードして
    サイズと AA エンコードを必ず実測確認すること。
"""

import os
import sys
import time
import urllib.request
import urllib.error
import zipfile

# Bundled third-party packages live in <plugin>/vendor (see vendor/README.md).
_vendor = os.path.join(os.path.dirname(os.path.dirname(__file__)), "vendor")
if _vendor not in sys.path:
    sys.path.append(_vendor)  # vendor は末尾追加でシステム版を優先

from defusedxml.ElementTree import fromstring as _defused_fromstring  # noqa: E402

BUCKET_URL = "https://virtual-shizuoka.s3.ap-northeast-1.amazonaws.com"
TILE_W = 400   # タイル幅 X(northing) [m]
TILE_H = 300   # タイル高 Y(easting) [m]

# LP データのリターン種別（実測確認済み、2025年時点）:
#   2021, 2025: Original（全リターン）と Ground（地面フィルタ済み）が別パスで提供
#               Ground は ~53〜100 bytes/m² → 地面点のみ（DSM計算不可）
#               Original は ~1000〜4000 bytes/m² → 全リターン（DSM計算可）
#   2019, 2020, 2022: Original パスなし。Ground が全リターン結合データを提供
#               Ground は ~860〜2200 bytes/m² → 全リターン（DSM計算可）
# → DSM 生成には Original（2021/2025）または Ground（2019/2020/2022）のどちらを使っても良い
YEARS_WITH_ORIGINAL = (2021, 2025)  # LP/Original パスが存在する年度

# 年度試行順（新しい順）
_YEAR_PRIORITY = (2025, 2022, 2021, 2020, 2019)           # Ground/Original 用
_YEAR_PRIORITY_GRID = (2025, 2022, 2021, 2020, 2019)       # Grid 用


# ── 座標計算 ──────────────────────────────────────────────────────────────────

def _folder_origins(folder: str):
    """フォルダコード2文字 → (x_origin, y_origin) EPSG:6676"""
    x0 = (ord(folder[1]) - ord('E')) * 40000
    y0 = -(ord(folder[0]) - ord('M') + 2) * 30000
    return x0, y0


def tile_bbox(code: str):
    """タイルコード → (xmin, ymin, xmax, ymax) EPSG:6676"""
    folder = code[2:4]
    xx = int(code[4:6])
    aa = int(code[6:8])
    x0, y0 = _folder_origins(folder)
    xc = x0 + (xx % 10) * 4000 + (aa % 10) * TILE_W + TILE_W // 2
    yc = y0 - (xx // 10) * 3000 - (aa // 10) * TILE_H - TILE_H // 2
    return xc - TILE_W // 2, yc - TILE_H // 2, xc + TILE_W // 2, yc + TILE_H // 2


def tiles_for_extent(xmin: float, ymin: float, xmax: float, ymax: float) -> set:
    """EPSG:6676 bbox に重複するタイルコードセットを返す（未確認含む）。

    年度によりタイル仕様が異なるため、両方式の候補コードを生成して返す。
    実在確認は resolve_years() に委ねる。

    方式 A — 標準 400m×300m 単純行列 (2019〜2021, 2025 確認済み):
        AA tens=行, units=列。AA は 00〜99 の範囲。
    方式 B — 1000m×750m Z曲線 (2022/LD・MD で確認):
        4×4 グリッドを 2×2 サブブロック 2 階層で符号化。AA は 11〜44。
        今後の年度でこの方式が再登場する可能性があるため両方生成する。
    """
    codes = set()
    for c1 in "LMNOP":
        for c2 in "BCDEF":
            folder = c1 + c2
            x0, y0 = _folder_origins(folder)
            # フォルダ全体の bbox: X=[x0, x0+40000], Y=[y0-30000, y0]
            if xmax < x0 or xmin > x0 + 40000 or ymax < y0 - 30000 or ymin > y0:
                continue
            xc_min = max(0, int((xmin - x0) / 4000))
            xc_max = min(9, int((xmax - x0) / 4000))
            yr_min = max(0, int((y0 - ymax) / 3000))
            yr_max = min(9, int((y0 - ymin) / 3000))
            for yr in range(yr_min, yr_max + 1):
                for xc in range(xc_min, xc_max + 1):
                    xx_base_x = x0 + xc * 4000
                    xx_base_y = y0 - yr * 3000
                    xx = yr * 10 + xc

                    # ── 方式 A: 400m×300m 単純行列 ──────────────────────────
                    ac_min = max(0, int((xmin - xx_base_x) / TILE_W))
                    ac_max = min(9, int((xmax - xx_base_x) / TILE_W))
                    ar_min = max(0, int((xx_base_y - ymax) / TILE_H))
                    ar_max = min(9, int((xx_base_y - ymin) / TILE_H))
                    for ar in range(ar_min, ar_max + 1):
                        for ac in range(ac_min, ac_max + 1):
                            aa = ar * 10 + ac
                            codes.add(f"08{folder}{xx:02d}{aa:02d}")

                    # ── 方式 B: 1000m×750m Z曲線 (2022年形式) ───────────────
                    # 4×4 グリッド (gc=列0-3, gr=行0-3) を Z曲線で AA に変換
                    gc_min = max(0, int((xmin - xx_base_x) / 1000))
                    gc_max = min(3, int((xmax - xx_base_x) / 1000))
                    gr_min = max(0, int((xx_base_y - ymax) / 750))
                    gr_max = min(3, int((xx_base_y - ymin) / 750))
                    for gr in range(gr_min, gr_max + 1):
                        for gc in range(gc_min, gc_max + 1):
                            # サブブロック (2×2) → t (1-4)
                            t = (gr // 2) * 2 + (gc // 2) + 1
                            # サブブロック内位置 → u (1-4)
                            u = (gr % 2) * 2 + (gc % 2) + 1
                            aa = t * 10 + u
                            codes.add(f"08{folder}{xx:02d}{aa:02d}")
    # 生成した全候補（方式A・B混在）を方式A座標で実際の重複確認し絞り込む。
    # 方式Bが生成した候補が方式Aタイルとして範囲外に存在した場合に
    # 誤ってダウンロードされる問題を防ぐ。
    return {
        c for c in codes
        if (lambda b: b[2] > xmin and b[0] < xmax and b[3] > ymin and b[1] < ymax)(tile_bbox(c))
    }


# ── S3 ヘルパー ───────────────────────────────────────────────────────────────

def _s3_list_xx(year: int, folder: str, xx: str, lp_type: str = "Grid") -> set:
    """指定 XX ディレクトリ内のタイルコードセットを S3 LIST で取得。"""
    prefix = f"{year}/LP/{lp_type}/08/{folder}/{xx}/"
    url = f"{BUCKET_URL}/?list-type=2&prefix={prefix}&delimiter=/"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:  # nosec B310
            root = _defused_fromstring(r.read())
        ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
        codes = set()
        for item in root.findall("s:Contents", ns):
            key = item.find("s:Key", ns).text
            fname = key.rstrip("/").split("/")[-1]
            if fname.endswith(".zip"):
                codes.add(fname[:-4])
        return codes
    except Exception:
        return set()


def _s3_head_check(year: int, code: str, lp_type: str = "Grid") -> bool:
    """指定タイルが S3 に存在するか HEAD リクエストで確認。
    ListBucket 権限が無効な場合の _s3_list_xx フォールバック用。"""
    folder = code[2:4]
    xx = code[4:6]
    url = f"{BUCKET_URL}/{year}/LP/{lp_type}/08/{folder}/{xx}/{code}.zip"
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10):  # nosec B310
            return True
    except Exception:
        return False


# ── 年度解決 ──────────────────────────────────────────────────────────────────

def resolve_all_years(codes: set, progress_cb=None, lp_type: str = "Grid") -> dict:
    """コードセット → {code: [year, ...]}（新しい年度から順に、存在する年度すべて）。

    同じタイルコードでも年度によって計測範囲が違い、新しい年度のタイルが
    タイルの一部しか覆っていないことがある（例: 08OE8859 は 2020 年が一部のみ、
    2019 年がほぼ全面）。新しい年度の欠けを古い年度で埋められるよう、
    resolve_years() と違い最初に見つかった年度で止めない。
    """
    result: dict[str, list] = {}
    groups: dict[tuple, set] = {}
    for code in codes:
        groups.setdefault((code[2:4], code[4:6]), set()).add(code)

    priority = _YEAR_PRIORITY_GRID if lp_type == "Grid" else _YEAR_PRIORITY
    total = len(groups)
    for done, ((folder, xx), batch) in enumerate(groups.items(), start=1):
        listed_any = False
        for year in priority:
            found = _s3_list_xx(year, folder, xx, lp_type)
            listed_any = listed_any or bool(found)
            for code in sorted(batch & found):
                result.setdefault(code, []).append(year)
        if not listed_any:
            # LIST が使えない環境のフォールバック（HEAD で年度ごとに確認）
            for code in sorted(batch):
                for year in priority:
                    if _s3_head_check(year, code, lp_type):
                        result.setdefault(code, []).append(year)
        if progress_cb:
            progress_cb(done, total)
    return result


def resolve_years(codes: set, progress_cb=None, lp_type: str = "Grid") -> dict:
    """コードセット → {code: year} （S3 LISTをXX単位でバッチ処理、新年度優先）。

    S3 LIST（ListBucket）が無効化されている場合、未解決タイルに対して
    HEAD リクエストで個別確認するフォールバックを行う。

    Args:
        codes: タイルコードセット
        progress_cb: (done, total) を受け取るコールバック（任意）
        lp_type: "Grid" | "Original" | "Ortho"

    Returns:
        存在が確認できたコードの {code: year} 辞書
    """
    result: dict[str, int] = {}
    remaining = set(codes)

    # (folder, xx) でグループ化してバッチ S3 LIST
    groups: dict[tuple, set] = {}
    for code in remaining:
        key = (code[2:4], code[4:6])
        groups.setdefault(key, set()).add(code)

    priority = _YEAR_PRIORITY_GRID if lp_type == "Grid" else _YEAR_PRIORITY
    done = 0
    total = len(groups)
    for (folder, xx), batch in groups.items():
        left = set(batch)
        for year in priority:
            if not left:
                break
            found = _s3_list_xx(year, folder, xx, lp_type)
            hits = left & found
            for code in hits:
                result[code] = year
            left -= hits

        # LIST で解決できなかったタイルは HEAD で個別確認
        # （ListBucket が無効化されている環境でのフォールバック）
        if left:
            for code in list(left):
                for year in priority:
                    if _s3_head_check(year, code, lp_type):
                        result[code] = year
                        left.discard(code)
                        break

        done += 1
        if progress_cb:
            progress_cb(done, total)

    return result


# ── CRS 付与ヘルパー ──────────────────────────────────────────────────────────

def _set_tif_epsg(tif_path: str, epsg: int):
    """GeoTIFF に CRS が未設定の場合に EPSG コードを書き込む。"""
    from osgeo import gdal, osr
    ds = gdal.Open(tif_path, gdal.GA_Update)
    if ds is None:
        return
    if not ds.GetProjection():
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg)
        ds.SetProjection(srs.ExportToWkt())
    ds = None


# ── ダウンロード ──────────────────────────────────────────────────────────────

def _download(url: str, dest: str, cancel_cb=None, progress_cb=None):
    """cancel_cb を渡すと、チャンク（65536バイト）単位でキャンセルを確認できる。
    これが無いと大きなZIP取得中はQtのイベントループが回らず、Cancelボタンの
    クリック自体がダウンロード完了まで処理されない
    （terrain/remote_zip.py と同じ理由・同じ粒度）。

    progress_cb(downloaded, total) を渡すと、チャンク単位（0.15秒間隔にスロットル）で
    ダウンロード済み/合計バイト数を通知する。totalはContent-Lengthから取得できない
    場合-1（進捗率は出せないが呼び出し側でdownloadedだけ表示することは可能）。"""
    from .remote_zip import Cancelled
    with urllib.request.urlopen(url, timeout=600) as r, open(dest, "wb") as f:  # nosec B310
        total = r.headers.get("Content-Length")
        total = int(total) if total is not None else -1
        downloaded = 0
        last_report = 0.0
        while True:
            if cancel_cb and cancel_cb():
                raise Cancelled("download cancelled")
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if progress_cb:
                now = time.monotonic()
                if now - last_report >= 0.15 or downloaded == total:
                    last_report = now
                    progress_cb(downloaded, total)


def _xyz_to_tif(txt_path: str, out_dir: str, cell_size: float = 0.5) -> str:
    """VS LP/Grid の X Y Z テキストファイルを GeoTIFF（EPSG:6676）に変換。

    対応フォーマット（2025年時点）:
      旧形式 (2019〜2020): スペース区切り 3列「X Y Z」
          例: -65199.75 -130200.25 33.40
      新形式 (2021〜):    カンマ区切り 4〜5列「ID,X,Y,Z[,flag]」
          例: 1,-65199.75,-130200.25,33.40,1
    フォーマットは先頭の有効行から自動判定する。
    今後カラム構成・区切り文字が変わった場合はこの関数を修正すること。
    """
    import numpy as np
    from osgeo import gdal, osr

    xs, ys, zs = [], [], []
    fmt = None  # "old": スペース区切り X Y Z  /  "new": カンマ区切り ID,X,Y,Z[,flag]
    with open(txt_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # 先頭の有効行でフォーマットを決定
            if fmt is None:
                fmt = "new" if "," in line else "old"
            try:
                if fmt == "new":
                    parts = line.split(",")
                    if len(parts) >= 4:          # ID, X, Y, Z [, flag]
                        xs.append(float(parts[1]))
                        ys.append(float(parts[2]))
                        zs.append(float(parts[3]))
                else:
                    parts = line.split()
                    if len(parts) == 3:          # X Y Z
                        xs.append(float(parts[0]))
                        ys.append(float(parts[1]))
                        zs.append(float(parts[2]))
            except ValueError:
                continue

    x = np.asarray(xs, dtype=np.float64)
    y = np.asarray(ys, dtype=np.float64)
    z = np.asarray(zs, dtype=np.float32)

    xmin = float(x.min()) - cell_size / 2
    xmax = float(x.max()) + cell_size / 2
    ymin = float(y.min()) - cell_size / 2
    ymax = float(y.max()) + cell_size / 2
    cols = int(round((xmax - xmin) / cell_size))
    rows = int(round((ymax - ymin) / cell_size))

    grid = np.full((rows, cols), np.float32(-9999.0))
    ci = np.round((x - xmin - cell_size / 2) / cell_size).astype(np.int32)
    ri = (rows - 1) - np.round((y - ymin - cell_size / 2) / cell_size).astype(np.int32)
    valid = (ci >= 0) & (ci < cols) & (ri >= 0) & (ri < rows)
    grid[ri[valid], ci[valid]] = z[valid]

    out_path = os.path.splitext(txt_path)[0] + ".tif"
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(out_path, cols, rows, 1, gdal.GDT_Float32,
                    ["COMPRESS=LZW", "TILED=YES"])
    ds.SetGeoTransform([xmin, cell_size, 0, ymax, 0, -cell_size])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(6676)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(-9999.0)
    band.WriteArray(grid)
    ds = None

    try:
        os.remove(txt_path)
    except OSError:
        pass
    return out_path


def _extract_tif(zip_path: str, out_dir: str) -> str:
    """ZIP 内の TIF または VS LP/Grid の TXT を展開し、GeoTIFF パスを返す。
    TFW ワールドファイルが同梱されている場合は TIF と並べて展開する。"""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        tif_name = None
        txt_name = None
        for name in names:
            lower = name.lower()
            if lower.endswith((".tif", ".tiff")):
                tif_name = name
            elif lower.endswith(".txt"):
                txt_name = name

        if tif_name:
            # TIF と同名の TFW があれば先に展開
            base = os.path.splitext(tif_name)[0]
            for name in names:
                if name.lower() == base.lower() + ".tfw":
                    dest_tfw = os.path.join(out_dir, os.path.basename(name))
                    with zf.open(name) as src, open(dest_tfw, "wb") as dst:
                        dst.write(src.read())
                    break
            dest = os.path.join(out_dir, os.path.basename(tif_name))
            with zf.open(tif_name) as src, open(dest, "wb") as dst:
                dst.write(src.read())
            return dest

        if txt_name:
            dest = os.path.join(out_dir, os.path.basename(txt_name))
            with zf.open(txt_name) as src, open(dest, "wb") as dst:
                dst.write(src.read())
            return _xyz_to_tif(dest, out_dir)

    raise ValueError(f"No TIF or TXT found in {zip_path}")


def download_grid_tif(code: str, year: int, out_dir: str, lp_type: str = "Grid", cancel_cb=None, progress_cb=None) -> str:
    """LP/{lp_type} ZIP をダウンロード→展開→GeoTIFF パスを返す。
    同タイルコードの TIF が既に out_dir に存在する場合は再利用する。
    progress_cb(downloaded, total) は _download 参照。"""
    # 既存 TIF キャッシュチェック
    tif_cache = os.path.join(out_dir, f"{code}.tif")
    if os.path.isfile(tif_cache):
        return tif_cache

    folder = code[2:4]
    xx = code[4:6]
    url = f"{BUCKET_URL}/{year}/LP/{lp_type}/08/{folder}/{xx}/{code}.zip"
    zip_path = os.path.join(out_dir, f"{code}_{lp_type}.zip")
    _download(url, zip_path, cancel_cb=cancel_cb, progress_cb=progress_cb)
    tif_path = _extract_tif(zip_path, out_dir)
    _set_tif_epsg(tif_path, 6676)
    try:
        os.remove(zip_path)
    except OSError:
        pass
    return tif_path


# ── GeoTIFF マージ ────────────────────────────────────────────────────────────

def missing_share(tif_path: str, xmin: float, ymin: float, xmax: float, ymax: float) -> float:
    """タイル GeoTIFF が bbox（タイルと重なる部分）を覆えていない割合（0〜1）。

    タイルのラスタがタイル枠より小さい場合（はみ出した部分）も欠けとして数える。
    """
    import numpy as np
    from osgeo import gdal

    ds = gdal.Open(tif_path)
    if ds is None:
        return 1.0
    gt = ds.GetGeoTransform()
    width, height = ds.RasterXSize, ds.RasterYSize
    cell_x, cell_y = gt[1], -gt[5]
    total = max(1, int(round((xmax - xmin) / cell_x)) * int(round((ymax - ymin) / cell_y)))
    col0 = max(0, int(np.floor((xmin - gt[0]) / cell_x)))
    col1 = min(width, int(np.ceil((xmax - gt[0]) / cell_x)))
    row0 = max(0, int(np.floor((gt[3] - ymax) / cell_y)))
    row1 = min(height, int(np.ceil((gt[3] - ymin) / cell_y)))
    if col1 <= col0 or row1 <= row0:
        return 1.0
    band = ds.GetRasterBand(1)
    values = band.ReadAsArray(col0, row0, col1 - col0, row1 - row0)
    nodata = band.GetNoDataValue()
    valid = np.isfinite(values)
    if nodata is not None:
        valid &= values != nodata
    ds = None
    return max(0.0, 1.0 - float(valid.sum()) / total)


def merge_tifs(tif_paths: list, out_path: str):
    """複数 GeoTIFF を gdal.Warp でマージして out_path に保存。

    後ろの GeoTIFF ほど上に重なる（値なしの画素は下の値を残す）。"""
    from osgeo import gdal
    result = gdal.Warp(
        out_path, tif_paths,
        format="GTiff",
        creationOptions=["COMPRESS=LZW", "TILED=YES"],
    )
    if result is None:
        raise RuntimeError(f"gdal.Warp failed for {tif_paths}")
    result = None
