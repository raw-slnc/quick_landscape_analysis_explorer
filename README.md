# Quick Landscape Analysis Explorer

Quick Landscape Analysis Explorer (QLAE) makes terrain maps of an area in
QGIS: hillshade, slope, CS Map, contours and catchments. Each analysis is
saved as one portable ZIP beside the QGIS project. What the preview shows can
be exported as a GeoPDF, an image or a print layout, and the analysis files
as layers.

## Purpose

QLAE is for terrain reconnaissance: reading the landform of an
area quickly from a DEM, and taking the result out in the form each task
needs.

- GeoPDF: a GeoPDF for field surveys. It opens in field map apps that read
  GeoPDF, such as Avid Maps Extra (a PWA by the author of this plugin;
  overview page in English, app in Japanese), linked at the bottom right of
  the window.
- Image: images for documents outside QGIS.
- Layers: layers kept in QGIS as survey information.
- Print layout: terrain information as a temporary record.

## Window

- An independent window: it can be minimized, reached with Alt+Tab, or put
  behind QGIS.
- English user interface with a Japanese switch stored in QGIS settings.
- The left preview is an A-series sheet, portrait or landscape. Its first view
  follows the QGIS main map; it can then be panned and zoomed on its own, and
  a button copies the main map's centre, CRS and scale again.
- Below the preview: the base map and an overlay, chosen from the project's
  layers and shown with their QGIS styles.

## Analysis

- Area: a polygon layer (optionally only the selected features), or the
  preview map extent.
- DEM source: one list, grouped by region. A source stays listed but cannot
  be chosen while the preview is outside its region, or when this QGIS cannot
  read its data; its tooltip says why.

  | Source | Cell | Provider / licence |
  |---|---|---|
  | Local DEM file | the file's | — |
  | GSI elevation tiles (Japan): best available, DEM1A 1 m, DEM5A 5 m, DEM10B 10 m | 1–10 m | Geospatial Information Authority of Japan (国土地理院標高タイル) |
  | VIRTUAL SHIZUOKA LP/Grid DTM (Shizuoka) | 0.5 m | Shizuoka Prefecture, CC BY 4.0 |
  | Nagano Sabo R3-4 DEM / Nagano Rinmu DEM (Nagano) | 0.5 m | Nagano Prefecture (see the dataset pages on G空間情報センター) |
  | AHN4 DTM (Netherlands) | 0.5 m | Rijkswaterstaat / PDOK, CC0 |
  | IGN LiDAR HD MNT (France) | 0.5 m | IGN, Licence Ouverte / Open Licence (Etalab 2.0) |
  | Rhineland-Palatinate DGM1 (Germany) | 1 m | ©GeoBasis-DE / LVermGeoRP (year obtained), dl-de/by-2-0, www.lvermgeo.rlp.de |
  | Bavaria DGM1 (Germany) | 1 m | Bayerische Vermessungsverwaltung – www.geodaten.bayern.de, CC BY 4.0 |
  | LINZ DEM (New Zealand; needs GDAL with LERC) | 1 m | Toitū Te Whenua LINZ and the survey licensors, CC BY 4.0 |
  | AWS Terrain Tiles (Mapzen Terrarium), zoom 12–14 (global) | varies by region | Mapzen / AWS Open Data and their data sources |

  VIRTUAL SHIZUOKA and Rhineland-Palatinate fill gaps in a tile's newest
  survey from older surveys, and the survey years used are shown on the
  print layout. The print layout credits the German states in the form they
  ask for, marking the data as changed ("[Daten bearbeitet]", "Daten
  verändert").
- Outputs:
  - Hillshade.
  - Slope in degrees, in 5° classes from 20° to 45° (the slope colour classes
    of the original MORIZON).
  - CS Map (the logic of Forestry Operations Lite).
  - Contours: every 10 m, thicker every 20 m and 100 m, from a DEM averaged
    to 5 m and smoothed.
  - Catchment map (map-extent area only; off by default): catchments by
    valley, smallest 5 ha, with GRASS r.watershed (MFD, as in the original
    MORIZON and Morizon Next). Where QGIS has no GRASS (for example the
    macOS disk image), or GRASS is listed but does not run, the plugin
    computes them itself with numpy, after the
    rules of r.watershed: on two test areas in Germany the catchments in
    the part kept for the map were the same as r.watershed's cell for cell
    (differences only along the outer edge of the wider DEM), and it takes
    a few times longer. The print layout names the computation used. The DEM is
    fetched three times wider and higher than the frame, every analysis
    runs on that area, and the results are cut to the frame.
- Every result uses one A-series output frame around the area; DEM outside
  the frame may still be used to compute edge values correctly.
- The QGIS project must be saved first. Each analysis is saved as one ZIP
  below `<project>/quick_landscape_analysis/analyses/`; QGIS reads the rasters
  in it through `/vsizip/`. An analysis is never overwritten: a name already
  in use cannot be run. A blank name uses the run time.

## Analysis management

- After an analysis runs, or a saved one is loaded, the right panel switches
  to analysis management.
- The preview shows the view used when the analysis ran, with its
  surroundings. Analysis layers are chosen with check boxes. Two optional
  project layers (for example forest compartments or roads) can be added.
  Drawing order, top first: optional 1, optional 2, overlay, analysis layers,
  base map.
- The analysis layers are placed in the plugin's group in the project. They
  stay when the window closes, and when it opens again it asks whether to
  open the previous analysis. "Finish analysis management" removes them from
  the project; the ZIP stays and can be loaded again.
- Exports:
  - GeoPDF, image and print layout draw exactly what the preview shows.
    - GeoPDF: made in the background, with each layer kept as a GeoPDF layer.
    - Image: PNG, JPEG or TIFF on the paper size, with a world file. The
      button names its format; the button at its right switches the format.
    - Print layout: one layout named "Quick Landscape Analysis Explorer",
      replaced by each new one.
  - Layers: all the layers of the analysis, whatever the preview shows, are
    written with their styles (.qml) to
    `<project>/quick_landscape_analysis/exports/<analysis name>/` and added as
    ordinary layers in a group of their own. A record of the print layout
    (`qlae_layout.json`) is written beside them: right-clicking the group in
    the layer panel offers "Make print layout", which draws the group's
    layers in the layer panel's order, at the extent of the export, also
    without the plugin's window. For a group of files in an exports folder
    whose record is missing, the item is shown greyed out.

## Notices and limits for large processing

The plugin uses fixed settings to warn about or limit large processing. They
are the same on every PC: the plugin does not check the PC's memory,
processor or other environment. A powerful PC may handle more, and a modest
one may slow down earlier.

- Analysis: before an analysis, the plugin estimates the DEM it reads (the
  A-series frame, three times wider and higher when catchments are on). It
  asks first from about 40 km² of a 0.5 m DEM (about 1.3 GB in memory), and
  warns more strongly from about 100 km² (about 3.2 GB). Coarser DEMs are
  compared by their number of cells, so they can cover a wider area. Depending
  on the location, the download can be larger than the estimate.
- Export processing limit: for each paper size, the plugin takes the finest
  resolution (in 50 dpi steps) whose image stays within 60 million pixels as
  the guideline processing limit, marked "*" in the resolution lists. Above
  it, GeoPDF and image exports ask whether to export as they are or at the
  processing limit.
- Image size: one exported image is limited to about 2 GB (4 bytes per pixel).
  Larger images can only be exported at the processing limit.

## Code layout

- `dialog.py`, `management_panel.py`: the window and analysis management.
- `analyses/terrain.py`: the terrain analysis job (runs in a worker thread).
- `terrain/`: DEM sources and loaders, terrain derivatives, contours,
  catchments and styles.
- `core/packages.py`: the analysis ZIP packages.
- `core/session.py`: the plugin's analysis layers in the project.

## Next Japan routes

Later stages can add national forest-resource analysis without changing the
polygon-or-map-extent area contract or the portable package format.

---

## Support

If you find this plugin useful, your support is appreciated.
https://paypal.me/rawslnc

## License

GNU General Public License v2 or later. See [LICENSE](LICENSE).

The plugin bundles defusedxml 0.7.1 in `vendor/` under its own license
(Python Software Foundation License Version 2, `vendor/defusedxml/LICENSE`).
Its source and the changes made to it are listed in `vendor/README.md`.

## Author

Copyright (C) 2026 Hideharu Masai

## Disclaimer

- This plugin was not made by the providers of the DEM data (such as the
  Geospatial Information Authority of Japan, Shizuoka Prefecture or Nagano
  Prefecture), and is not endorsed or guaranteed by them.
- Use of the fetched data and of the maps made from it must follow each
  provider's terms: show the source, and note any processing. For GSI
  elevation tiles, follow the
  [GSI terms of use](https://www.gsi.go.jp/kikakuchousei/kikakuchousei40182.html)
  and the Survey Act of Japan.
- The results are computed from DEMs for reconnaissance and can differ from
  the actual site. Check them in the field.
- The plugin is provided without warranty; the author takes no
  responsibility for any result of its use.
- The slope colour classes and the catchment settings (GRASS r.watershed)
  follow the original MORIZON (もりぞん), the forest zoning support tool of
  the Forestry Agency of Japan (Copyright (C) 2021 MIERUNE Inc.), and Morizon
  Next. This plugin is not made or endorsed by their authors.
