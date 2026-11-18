# -*- coding: utf-8 -*-
"""Germany: each state publishes its own DGM1; one group per state."""

CODE = "DE"
NAME = ("Germany", "ドイツ")

RHINELAND_PALATINATE_BBOX = (6.05, 48.9, 8.6, 51.0)
BAVARIA_BBOX = (8.9, 47.2, 13.9, 50.6)

_SLOW = ("Fetching a large area takes time.", "広い範囲の取得は時間がかかります。")
# 1 km tiles in ETRS89 / UTM 32N, named by their lower-left corner in km.
_KM_TILES = {"epsg": 25832, "cell_w": 1000.0, "cell_h": 1000.0, "origin": (0.0, 7000000.0)}

GROUPS = [
    {
        "label": ("Germany: Rhineland-Palatinate open data", "ドイツ ラインラント＝プファルツ州 オープンデータ"),
        "region": RHINELAND_PALATINATE_BBOX,
        "sources": [
            {
                "key": "rlp_dgm1",
                "label": ("Rhineland-Palatinate DGM1 1 m", "ラインラント＝プファルツ州 DGM1 1 m"),
                "place": ("Rhineland-Palatinate", "ラインラント＝プファルツ州"),
                "note": (("1 m tiles", "1 mタイル"), _SLOW,
                         ("The newest survey of each tile is used.", "タイルごとに最新の測量を使います。")),
                "facts": {
                    "provider": "GeoBasis-RLP (Landesamt für Vermessung und Geobasisinformation Rheinland-Pfalz)",
                    "dataset": "Rhineland-Palatinate DGM1 1m",
                    "licence": "dl-de/by-2-0",
                    # The form LVermGeo RLP asks for (lvermgeo.rlp.de, Open
                    # Data); the year is the year the data was obtained. The
                    # plugin changes the data (clipped, regridded, derived
                    # maps), so "[Daten bearbeitet]".
                    "credit": "©GeoBasis-DE / LVermGeoRP {year}, dl-de/by-2-0, www.lvermgeo.rlp.de "
                              "[Daten bearbeitet]",
                    "service": "https://www.geobasis-rlp.de/data/dgm1/current/tif/",
                },
                "coverage": dict(_KM_TILES, check_days=30, ask_first=False),
                "outline": ("file",),
            },
        ],
    },
    {
        "label": ("Germany: Bavaria open data", "ドイツ バイエルン州 オープンデータ"),
        "region": BAVARIA_BBOX,
        "sources": [
            {
                "key": "by_dgm1",
                "label": ("Bavaria DGM1 1 m", "バイエルン州 DGM1 1 m"),
                "place": ("Bavaria", "バイエルン州"),
                "note": (("1 m tiles", "1 mタイル"), _SLOW),
                "facts": {
                    "provider": "Bayerische Vermessungsverwaltung (geodaten.bayern.de)",
                    "dataset": "Bavaria DGM1 1m",
                    "licence": "CC BY 4.0",
                    # The form of the Bavarian terms of use, with the licence
                    # and the statement that the data was changed, which the
                    # terms ask for.
                    "credit": "Bayerische Vermessungsverwaltung – www.geodaten.bayern.de, CC BY 4.0 "
                              "(Daten verändert)",
                    "service": "https://download1.bayernwolke.de/a/dgm/dgm1/",
                },
                # Bavaria's list is about 4 MB: asked first.
                "coverage": dict(_KM_TILES, check_days=30, ask_first=True),
                "outline": ("file",),
                "ask_update": (
                    "<p>Bavaria has published a newer list of its DGM1 tiles since the "
                    "coverage mesh on the preview was made.</p>"
                    "<p>The tiles cover the whole state, so the mesh seldom changes; "
                    "fetching works without updating it. Updating reads the list "
                    "(about {mb} MB, in the background).</p>"
                    '<p><a href="qlae:coverage-update">Update the mesh</a>'
                    '&nbsp;&nbsp;&nbsp;<a href="qlae:coverage-later">Not now</a> '
                    "(asked again in a month)</p>",
                    "<p>プレビューの網目を作ったあとに、バイエルン州が DGM1 のタイルの"
                    "一覧を新しくしています。</p>"
                    "<p>タイルは州全体にあるので、網目が変わることはまれです。更新しなくても"
                    "取得はできます。更新すると、一覧を読みます（約{mb} MB、裏で行います）。</p>"
                    '<p><a href="qlae:coverage-update">網目を更新する</a>'
                    '&nbsp;&nbsp;&nbsp;<a href="qlae:coverage-later">今はしない</a>'
                    "（1か月後にまた確認します）</p>",
                ),
                "updated": ("The Bavaria coverage mesh is up to date.",
                            "バイエルン州の範囲の網目を更新しました。"),
            },
        ],
    },
]
