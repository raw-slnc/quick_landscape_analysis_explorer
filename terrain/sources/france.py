# -*- coding: utf-8 -*-
"""France: IGN LiDAR HD."""

CODE = "FR"
NAME = ("France", "フランス")

FRANCE_BBOX = (-5.5, 41.2, 9.8, 51.2)

GROUPS = [
    {
        "label": ("France open data", "フランス オープンデータ"),
        "region": FRANCE_BBOX,
        "sources": [
            {
                "key": "ign_mnt",
                "label": ("IGN LiDAR HD MNT 0.5 m", "IGN LiDAR HD MNT 0.5 m"),
                "note": (("0.5 m", "0.5 m"),
                         ("Not all of France is covered yet.", "フランス全土はまだ覆われていません。")),
                "facts": {
                    "provider": "IGN (Institut national de l'information géographique et forestière)",
                    "dataset": "IGN LiDAR HD MNT 0.5m",
                    "licence": "Licence Ouverte / Open Licence (Etalab 2.0)",
                    "service": "https://data.geopf.fr/wms-r/wms",
                },
                # 1 km tiles in Lambert-93, from IGN's list of LiDAR HD
                # tiles; new tiles are published every few weeks.
                "coverage": {"epsg": 2154, "cell_w": 1000.0, "cell_h": 1000.0,
                             "origin": (0.0, 8000000.0), "check_days": 7, "ask_first": True},
                "outline": ("country", "FR", (-5.5, 41.0, 10.0, 51.5)),
                "ask_update": (
                    "<p>IGN has published about {count:,} new tiles of LiDAR HD since the "
                    "coverage mesh on the preview was made.</p>"
                    "<p>The mesh only shows where data can be fetched; fetching itself "
                    "works without updating it. Updating reads the list of the new tiles "
                    "(about {seconds} s, in the background).</p>"
                    '<p><a href="qlae:coverage-update">Update the mesh</a>'
                    '&nbsp;&nbsp;&nbsp;<a href="qlae:coverage-later">Not now</a> '
                    "(asked again in a week)</p>",
                    "<p>プレビューの網目を作ったあとに、IGN が LiDAR HD のタイルを"
                    "約{count:,}枚公開しています。</p>"
                    "<p>網目は取得できる範囲の目安で、更新しなくても取得はできます。"
                    "更新すると、新しいタイルの一覧を読みます（約{seconds}秒、裏で行います）。</p>"
                    '<p><a href="qlae:coverage-update">網目を更新する</a>'
                    '&nbsp;&nbsp;&nbsp;<a href="qlae:coverage-later">今はしない</a>'
                    "（1週間後にまた確認します）</p>",
                ),
                "updated": ("The France coverage mesh is up to date.",
                            "フランスの範囲の網目を更新しました。"),
            },
        ],
    },
]
