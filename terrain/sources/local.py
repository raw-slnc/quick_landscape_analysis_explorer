# -*- coding: utf-8 -*-
"""Local DEMs: a file, or the tiles kept in the QLAE folder."""

CODE = None
NAME = None

GROUPS = [
    {
        "label": ("Local", "ローカル"),
        "region": None,
        "sources": [
            {"key": "local", "label": ("Local DEM file", "ローカルDEMファイル")},
            # The DEM tiles kept in the folder chosen in the advanced settings
            # (terrain/kept_tiles.py); whose tiles is chosen below it.
            {
                "key": "qlae_folder",
                "label": ("QLAE folder", "QLAEフォルダ"),
                "note": (
                    ("Only the kept tiles are used.", "保存したタイルだけを使います。"),
                    ("No data outside the mesh.", "網目の外はデータがありません。"),
                ),
            },
        ],
    },
]
