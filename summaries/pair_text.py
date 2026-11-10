# -*- coding: utf-8 -*-


def format_pair_text(result, tr):
    facts = result.facts
    preview_extent = facts.get("aoi_source") == "preview_extent"
    area_source_label = (
        tr("Area source", "範囲の指定")
        if preview_extent
        else tr("Area layer", "範囲レイヤー")
    )
    area_used_label = (
        tr("Area used", "使用する範囲")
        if preview_extent
        else tr("Area features", "範囲地物")
    )
    area_source = (
        tr("Full preview map extent", "プレビュー画面の全範囲")
        if preview_extent
        else facts["aoi_layer"]
    )
    if preview_extent:
        area_features = tr(
            "Full extent; central 3/5 frame is a guide only",
            "全範囲（中央3/5の枠線は目安のみ）",
        )
    elif facts["aoi_feature_mode"] == "selected":
        area_features = tr("{} selected", "選択 {} 件").format(
            facts["aoi_features"]
        )
    else:
        area_features = tr("{} total", "全 {} 件").format(
            facts["aoi_features"]
        )
    lines = [
        tr("Pair inspection", "組み合わせの確認"),
        "",
        _line(area_source_label, area_source),
        _line(area_used_label, area_features),
        _line(tr("Valid geometries", "有効な形状"), facts["aoi_valid_geometries"]),
        _line(tr("Measured area", "計測面積"), "{:.3f} km²".format(facts["aoi_area_km2"])),
        _line(tr("Area CRS", "範囲の座標系"), facts["aoi_crs"]),
        "",
        _line(tr("Data layer", "対象データ"), facts["data_layer"]),
        _line(
            tr("Data type", "データ型"),
            tr("Raster", "ラスター") if facts["data_kind"] == "raster" else tr("Vector", "ベクター"),
        ),
        _line(tr("Data CRS", "データの座標系"), facts["data_crs"]),
        _line(tr("Extent in area CRS", "範囲座標系での領域"), facts["data_extent"]),
        _line(
            tr("Bounding-box overlap", "外接矩形の重なり"),
            tr("Yes", "あり") if facts["bbox_overlap"] else tr("No", "なし"),
        ),
    ]

    if facts["data_kind"] == "raster":
        lines.extend(
            [
                _line(tr("Raster size", "ラスターサイズ"), "{} × {}".format(facts["raster_width"], facts["raster_height"])),
                _line(tr("Bands", "バンド数"), facts["raster_bands"]),
                _line(
                    tr("Pixel size", "ピクセルサイズ"),
                    "{:.6g} × {:.6g}".format(facts["pixel_size_x"], facts["pixel_size_y"]),
                ),
            ]
        )
    else:
        lines.extend(
            [
                _line(tr("Geometry", "ジオメトリ"), facts["vector_geometry"]),
                _line(tr("Features", "地物数"), facts["vector_features"]),
                _line(tr("Fields", "属性数"), facts["vector_fields"]),
            ]
        )

    if "bbox_no_overlap" in result.warnings:
        lines.extend(
            [
                "",
                tr(
                    "Warning: the two bounding boxes do not overlap. No analysis should run with this pair.",
                    "警告：2つの外接矩形が重なっていません。この組み合わせでは解析を実行できません。",
                ),
            ]
        )
    else:
        lines.extend(
            [
                "",
                tr(
                    "The pair is ready for the next analysis stage. This stage checks bounding boxes only; it does not yet clip or analyse the data.",
                    "次の解析段階へ渡せる組み合わせです。この段階では外接矩形のみを確認し、切り抜きや解析はまだ実行しません。",
                ),
            ]
        )

    return "\n".join(lines)


def _line(label, value):
    return "{}: {}".format(label, value)
