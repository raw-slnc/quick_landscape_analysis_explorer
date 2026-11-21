#!/usr/bin/env python3
"""fieldmap の datums.json（測地系 → WGS84 の変換値の表）を PROJ のデータベースから作る。

使い方:  python3 make_datums.py /usr/share/proj/proj.db ../datums.json \
           /home/user/datums_generation_report.json
レポートは配布するプラグインの外に保存する。失敗がなくても件数0を記録する。

- WGS84 (EPSG:4326) へ直接変換する Helmert 系の変換（廃止扱いを除く）を、元の測地系ごとにまとめる。
  値は Position Vector の TOWGS84（m・秒・ppm）にそろえる。Molodensky-Badekas は同じ結果になる
  普通の Helmert に直す（回転中心の分を並進に含める）。
- 各変換には適用範囲（緯度経度の四角）と精度を付ける。アプリは地図の中心を含むもののうち精度の良いものを使う。
- 楕円体が GRS80/WGS84 で、WGS84 への変換が無い測地系（CGCS2000 など）は「WGS84と同じ」として載せる。
  それ以外で変換の無い測地系は載せない（アプリは「変換できない」として注意を出す）。
- 測地系の名前は EPSG の名前・別名・ESRI の名前（GDAL で変換）を、記号と先頭の D_ を除いた小文字で引けるようにする。
サーバーには置かない（アップロード時に tools/ は除外する）。
"""

import json
import math
import os
import re
import sqlite3
import stat
import sys
import tempfile

from osgeo import gdal, osr

osr.UseExceptions()

if len(sys.argv) != 4:
    raise SystemExit(
        "usage: make_datums.py PROJ_DB OUTPUT_JSON REPORT_JSON"
    )
db_path, out_path, report_path = sys.argv[1:]
if os.path.abspath(out_path) == os.path.abspath(report_path):
    raise SystemExit("OUTPUT_JSON and REPORT_JSON must be different files")

# Reviewed GDAL alias failures only: (datum EPSG, CRS EPSG, stage, error type)
# -> reason. New failures must be reviewed before adding an entry here.
ALLOWED_ALIAS_FAILURES = {}


def write_json_atomic(path, value, new_mode=0o600):
    """Replace a JSON file only after the complete new file is written."""
    directory = os.path.dirname(os.path.abspath(path))
    mode = (
        stat.S_IMODE(os.stat(path).st_mode)
        if os.path.exists(path)
        else new_mode
    )
    fd, temporary = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.chmod(temporary, mode)
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


db = sqlite3.connect(db_path)
uom = {
    (a, c): f
    for a, c, f in db.execute(
        "select auth_name, code, conv_factor from unit_of_measure"
    )
}
ARCSEC = math.pi / 648000


def norm(name):
    # アプリの normalizeDatumKey と同じ。CH1903+ と CH1903 を分けるため + は plus にする
    name = name.lower().replace("+", "plus")
    name = re.sub(r"^d_", "", name)
    return re.sub(r"[^a-z0-9]", "", name)


def to_si(value, auth, code, default):
    if value is None:
        return 0.0
    factor = uom.get((auth, code)) if code else None
    return value * (factor if factor is not None else default)


def round_sig(value, digits=10):
    if value == 0:
        return 0
    rounded = round(
        value, digits - int(math.floor(math.log10(abs(value)))) - 1
    )
    return (
        int(rounded)
        if rounded == int(rounded) and abs(rounded) < 1e15
        else rounded
    )


# 測地系ごとの情報（地理2D・グリニッジ子午線の CRS を通して変換を集める）
crs_rows = db.execute("""
  select g.code, g.datum_code, d.name, e.semi_major_axis
  from geodetic_crs g
  join geodetic_datum d on d.auth_name = g.datum_auth_name
    and d.code = g.datum_code
  join ellipsoid e on e.auth_name = d.ellipsoid_auth_name
    and e.code = d.ellipsoid_code
  join prime_meridian p on p.auth_name = d.prime_meridian_auth_name
    and p.code = d.prime_meridian_code
  where g.auth_name = 'EPSG' and d.auth_name = 'EPSG'
    and g.type = 'geographic 2D' and p.longitude = 0
""").fetchall()
datum_of_crs = {code: datum for code, datum, _, _ in crs_rows}
datum_info = {}
for code, datum, name, a in crs_rows:
    info = datum_info.setdefault(
        datum,
        {"name": name, "grs80": abs(a - 6378137) < 0.5, "crs": [], "ops": {}},
    )
    info["crs"].append(code)

ops = db.execute("""
  select code, method_code, source_crs_code, accuracy,
         tx, ty, tz, translation_uom_auth_name, translation_uom_code,
         rx, ry, rz, rotation_uom_auth_name, rotation_uom_code,
         scale_difference, scale_difference_uom_auth_name,
         scale_difference_uom_code,
         px, py, pz, pivot_uom_auth_name, pivot_uom_code
  from helmert_transformation
  where auth_name = 'EPSG' and target_crs_auth_name = 'EPSG'
    and target_crs_code = '4326' and deprecated = 0
    and source_crs_auth_name = 'EPSG' and rate_tx is null
""").fetchall()

# 置き換えられた変換（同じ元・先の測地系で新しい変換がある）。置き換え先が表に入るときだけ除く
helmert_codes = {row[0] for row in ops}
replaced_by = {}
for old_code, new_code in db.execute("""
  select superseded_code, replacement_code from supersession
  where superseded_table_name = 'helmert_transformation'
    and replacement_table_name = 'helmert_transformation'
    and superseded_auth_name = 'EPSG' and replacement_auth_name = 'EPSG'
    and same_source_target_crs = 1
"""):
    replaced_by[old_code] = new_code


def superseded(code):
    seen = set()
    while code in replaced_by and code not in seen:
        seen.add(code)
        code = replaced_by[code]
        if code in helmert_codes:
            return True
    return False


skipped = []
for (
    code,
    method,
    src,
    acc,
    tx,
    ty,
    tz,
    tua,
    tuc,
    rx,
    ry,
    rz,
    rua,
    ruc,
    ds,
    sua,
    suc,
    px,
    py,
    pz,
    pua,
    puc,
) in ops:
    if superseded(code):
        skipped.append((code, "superseded"))
        continue
    datum = datum_of_crs.get(src)
    if datum is None:
        skipped.append((code, "source CRS not geographic 2D Greenwich"))
        continue
    t = [to_si(v, tua, tuc, 1.0) for v in (tx, ty, tz)]  # m
    r = [to_si(v, rua, ruc, ARCSEC) for v in (rx, ry, rz)]  # rad
    s = to_si(ds, sua, suc, 1e-6)  # 単位なし
    if method in (9607, 9636):  # Coordinate Frame → Position Vector
        r = [-v for v in r]
    elif method not in (9603, 9606, 1063):
        skipped.append((code, f"method {method}"))
        continue
    if px is not None:  # Molodensky-Badekas：回転中心の分を並進へ
        p = [to_si(v, pua, puc, 1.0) for v in (px, py, pz)]
        m = 1 + s
        rx_, ry_, rz_ = r
        rp = [
            p[0] - rz_ * p[1] + ry_ * p[2],
            rz_ * p[0] + p[1] - rx_ * p[2],
            -ry_ * p[0] + rx_ * p[1] + p[2],
        ]
        t = [t[i] + p[i] - m * rp[i] for i in range(3)]
    params = (
        [round_sig(v) for v in t]
        + [round_sig(v / ARCSEC) for v in r]
        + [round_sig(s * 1e6)]
    )
    extents = db.execute(
        """
      select x.south_lat, x.north_lat, x.west_lon, x.east_lon
      from usage u join extent x on x.auth_name = u.extent_auth_name
        and x.code = u.extent_code
      where u.object_table_name = 'helmert_transformation'
        and u.object_auth_name = 'EPSG' and u.object_code = ?
    """,
        (code,),
    ).fetchall()
    for south, north, west, east in extents:
        key = (code, west, south, east, north)
        datum_info[datum]["ops"][key] = [
            round(west, 4),
            round(south, 4),
            round(east, 4),
            round(north, 4),
            acc if acc is not None else None,
            *params,
            int(code),
        ]

# 名前の引き方：EPSG名・別名・ESRI名
aliases = {}
for code, alt in db.execute(
    ('select code, alt_name from alias_name where table_name = '
     "'geodetic_datum' and auth_name = 'EPSG'")
):
    aliases.setdefault(int(code), []).append(alt)

datums_out = []
alias_failures = []
# 名前 → (優先度, 測地系の番号の小さい順の代わりに主なCRSの番号, 出力の位置)
# 優先度：3 = EPSGの正式名、2 = GDALが書くOGC/ESRIの名前、1 = 別名。同じ名前を複数の測地系が持つときは高い方
candidates = {}
gdal.PushErrorHandler("CPLQuietErrorHandler")
for datum, info in sorted(datum_info.items(), key=lambda item: int(item[0])):
    entry_ops = sorted(info["ops"].values(), key=lambda op: op[-1])
    if not entry_ops and not info["grs80"]:
        continue
    index = len(datums_out)
    datums_out.append(
        {"epsg": int(datum), "ops": entry_ops}
        if entry_ops
        else {"epsg": int(datum), "same": 1}
    )
    main_crs = min(int(c) for c in info["crs"])
    keyed = {norm(info["name"]): 3}
    for alt in aliases.get(int(datum), []):
        keyed.setdefault(norm(alt), 1)
    for crs in sorted(info["crs"], key=int)[:3]:
        stage = "create_srs"
        try:
            srs = osr.SpatialReference()
            stage = "import_epsg"
            srs.ImportFromEPSG(int(crs))
            stage = "ogc_name"
            for name in (srs.GetAttrValue("DATUM") or "",):
                keyed[norm(name)] = max(keyed.get(norm(name), 0), 2)
            stage = "morph_to_esri"
            srs.MorphToESRI()
            stage = "esri_name"
            name = srs.GetAttrValue("DATUM") or ""
            keyed[norm(name)] = max(keyed.get(norm(name), 0), 2)
        except Exception as error:
            key = (int(datum), int(crs), stage, type(error).__name__)
            reason = ALLOWED_ALIAS_FAILURES.get(key)
            alias_failures.append(
                {
                    "datum_epsg": key[0],
                    "crs_epsg": key[1],
                    "stage": stage,
                    "error_type": key[3],
                    "message": str(error),
                    "accepted": bool(reason),
                    "reason": reason,
                }
            )
    keyed.pop("", None)
    # 変換の適用範囲の広さ（度²の合計）。同じ別名を持つ全国版と地域版では全国版を選ぶため
    area = sum(
        abs(((op[2] - op[0]) % 360 or 360) * (op[3] - op[1]))
        for op in entry_ops
    )
    classic = 4000 <= main_crs < 5000
    for key, priority in keyed.items():
        candidates.setdefault(key, []).append(
            (priority, classic, area, main_crs, index)
        )
gdal.PopErrorHandler()

names = {}
collisions = []
for key, options in candidates.items():
    # 優先度が高いもの → EPSGの古くからの番号帯(4000番台)のCRSを持つもの → 範囲が広いもの → 番号が小さいもの
    options.sort(key=lambda o: (-o[0], not o[1], -o[2], o[3]))
    names[key] = options[0][4]
    if len(options) > 1 and options[0][0] == options[1][0]:
        collisions.append((key, [datums_out[o[4]]["epsg"] for o in options]))

# 一般的な略称で、上の決め方では地域版を選んでしまうものは代表の測地系に固定する
OVERRIDES = {"nad83": 6269, "etrs89": 6258, "wgs84": 6326, "wgs1984": 6326}
for key, epsg in OVERRIDES.items():
    for index, entry in enumerate(datums_out):
        if entry["epsg"] == epsg:
            names[key] = index

out = {
    "source": ('EPSG Geodetic Parameter Dataset {} ({}) via PROJ '
               '{}.{}.{} (GDAL {})').format(
        *[
            dict(db.execute("select key, value from metadata")).get(k, "?")
            for k in ("EPSG.VERSION", "EPSG.DATE")
        ],
        osr.GetPROJVersionMajor(),
        osr.GetPROJVersionMinor(),
        osr.GetPROJVersionMicro(),
        gdal.__version__,
    ),
    "format": ('ops: [west, south, east, north, accuracy_m, tx, ty, tz '
               '(m), rx, ry, rz (arc-seconds, Position Vector), ds (ppm), '
               'epsg_code]'),
    "names": names,
    "datums": datums_out,
}
unreviewed = [failure for failure in alias_failures if not failure["accepted"]]
write_json_atomic(
    report_path,
    {
        "source": out["source"],
        "alias_failure_count": len(alias_failures),
        "unreviewed_failure_count": len(unreviewed),
        "alias_failures": alias_failures,
    },
)
if unreviewed:
    raise SystemExit(
        f"{len(unreviewed)} unreviewed alias failures; "
        f"output unchanged; see {report_path}"
    )
write_json_atomic(out_path, out, new_mode=0o644)
print(
    f"datums: {len(datums_out)}, names: {len(names)}, "
    f"ops: {sum(len(d.get('ops', [])) for d in datums_out)}"
)
print(f"skipped ops: {len(skipped)}", skipped[:10])
print(f"name collisions (first wins): {len(collisions)}", collisions[:20])
print(f"alias failures: {len(alias_failures)}; report: {report_path}")
