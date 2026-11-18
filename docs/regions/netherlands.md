# The Netherlands

What Quick Landscape Analysis Explorer does particularly for the
Netherlands. The settings are in `terrain/sources/netherlands.py`; the
regional analysis is done by `terrain/sources/netherlands_nap.py`.

## DEM source

- AHN4 DTM 0.5 m, read by area from PDOK's WCS (`dtm_05m`). Rijkswaterstaat /
  PDOK, CC0. AHN4 was surveyed over 2020, 2021 and 2022 (the service's own
  description; the year of a place is not given): the analysis's
  description, the DEM's metadata and the credits name these years. The
  DSM (below) is AHN4 too.
- The DTM is the ground surface only: buildings, water and some places
  under dense trees have no values. In the Vaals test area, 0.3% of the
  open ground, 12% of the woodland (canopy over 10 m) and 17% of the land
  with 2 to 10 m on it (buildings, shrubs, young trees) had no value: AHN
  is flown when trees are bare, so the laser mostly reaches the ground.
- It is read in 500 m square chunks on a fixed grid in RD New (counted
  from 0), kept like the other sources' tiles (QGIS run, or the QLAE folder)
  and used again for the same place. The DSM for the object height below is
  read and kept the same way.
- Requests are kept within the coverage's own extent (RD New, x 10,000 to
  280,000 m, y 306,250 to 618,750 m): PDOK refuses requests reaching past
  it, as at the southern tip of Limburg. The part outside is left without
  data.

## Coverage mesh and outline

- The whole country has AHN data, so its mesh is the country's outline.
- The outline is simplified (50 m) from PDOK's Bestuurlijke Gebieden
  (Landgebied), Kadaster (from the BRK), CC BY 4.0
  (`terrain/data/regions/ahn_dtm.json`, made by `tools/build_regions.py`).
  QGIS's world map is too coarse at the Dutch borders: it left out the
  Vaalserberg area.
- The same outline tells whether the preview is in the Netherlands, for the
  regional analysis below.

## Regional analysis

One check, "Regional", under the analysis checks,
shown while the preview is in the Netherlands and greyed until a DEM source
is chosen; on by default. It runs with the basic analysis and is kept in
the same analysis ZIP (`regional/nap_colouring/`). Until the whole
country's geomorphological map is kept (below), its tooltip and the Status
section say that the first time fetches it (about 213 MB).
Its layers are off at first, in the layer panel and in analysis
management, and come out inactive when the layers are exported.

The DEM for the GeoPDF (`dem_for_geopdf.tif`) is made in stages. With the
regional analysis on, the first stage is the Netherlands' own: the DEM it
starts from has its holes that do not reach its edge filled from the heights
around them, water too, for a surface without holes in 3D
(`geopdf_fill_holes` in `terrain/sources/netherlands.py`; not the DEM (holes
filled) below, which keeps the water open). The later stages
are the same everywhere: cut to the preview and warped to its CRS, small
holes filled from around them, and what is left from AWS Terrain Tiles.

Maps (listed under Regional, Analysis, in analysis management):

- **NAP colouring**: the DEM coloured as PDOK shows the AHN DTM (the
  default style of the `dtm_05m` layer of PDOK's AHN WMS): 49 fixed classes
  from −8 m to 350 m above NAP, finest near NAP (0.5 m from −3 m to 5 m),
  from dark blue through green and yellow to orange and red-brown, drawn
  multiplied (the relief maps below show through). No new data. The classes and colours were read from PDOK's legend. Above about
  100 m (South Limburg) the classes are coarse and the colours close, as on
  PDOK's own map.
- **Slope (local scale, prototype)**: the slope of the ground, made from
  the DEM (holes filled) below, so it is there under the trees too, where
  the basic slope (from AHN's DTM) has none. The basic slope's seven
  classes and colours, their breaks fitted to the area's slopes (the 98th
  percentile in six plain steps). The breaks change from place to place, so
  its colours cannot be compared between areas; the legend shows them. At
  0.5 m it brings out small landforms and noise alike. It is shown instead of the basic slope:
  analysis management ticks one of the two at a time.
- **Object height (trees and buildings)**: AHN's DSM less the ground (the
  DEM (holes filled) below), both 0.5 m. Not a canopy height alone:
  buildings are not masked and keep their height too. Nine classes from
  white (up to 1 m) to dark green (over 30 m), drawn multiplied. AHN
  (PDOK), CC0.
- **Geomorphology**: BRO Geomorfologische kaart 1:50,000 (PDOK), CC0. The
  landforms of the analysis area, cut to it like the other results, with
  their attributes (genesis, relief, landform subgroup and their Dutch
  descriptions), in the map's
  official style (from its SLD, `terrain/data/styles/nl_geomorphology.qml`,
  made by `tools/build_nl_styles.py`), drawn multiplied. The map is offered
  only as one file for the whole country (about 213 MB, 516 MB unpacked):
  it is fetched the first time and kept, in the QLAE folder when one is
  chosen, else in the QGIS profile; each analysis keeps only its area.
  In a print layout its legend is on a page of its own, with only the
  landforms on the map; left out when other colours are drawn with it
  (rasters such as the slope or a base map, filled areas), as its colours
  then no longer match the map. The hillshade, lines and outlines do not
  count.

Data (listed under Regional, Data; in the layer panel in the analysis's
Data group above the DEM, not drawn; exported with the layers):

- **DEM (holes filled)**: the DEM of the ground, made for the regional
  analysis (the slope and the object height above are made from it). AHN's
  DTM has no values where something stands on the ground (trees, buildings)
  and where there is no ground (water). The two are told apart by the DSM:
  where it has a value, something stands there and the ground under it is
  filled from the ground around, at AHN's resolution, at the analysis
  area's edge too (from its inner side). Where the DSM has no value either
  (water: the laser does not come back from it; outside the Netherlands),
  the hole stays. At Rhenen (the Nederrijn and the wooded Grebbeberg) all
  the holes left had no DSM value. Made only when the DSM could be fetched
  (without it, neither are the slope and the object height). Shown
  instead of the basic DEM: analysis management ticks one of the two at a
  time (both marked ⇄).
- **DSM**: AHN's DSM (buildings and trees on top), on the same grid and
  area as the object height. Ticked like the maps.

The photos are listed without a tick box: they are there for the base map,
chosen in the preview's base map list.

- **Orthophoto {year} for Base map**: PDOK's newest aerial photo over the
  preview: the newest of the yearly layers (`2025_ortho25` in 2026; the
  same photo as `Actueel_ortho25`, which does not name its year).
- **Orthophoto CIR {year} for Base map**: the newest colour-infrared photo
  (`2025_ortho25IR`). Living plants show red; in an early-spring photo
  evergreen trees stand out from bare broadleaves.

The year is in the layer's name, its metadata and the credits. When the
service's list of layers cannot be read, the "Actueel" layer is used and
the year is left out. The photos are often of a later year than AHN4.

Both photos are fetched at most 5,000 pixels on their longer side and never
finer than 25 cm (about A3 at 300 dpi), in requests of at most 2,500
pixels, and kept as JPEG-compressed GeoTIFFs. Orthophotos:
Beeldmateriaal.nl (PDOK), CC BY 4.0 (as data.overheid.nl lists the yearly
sets; the newest layer's own terms should be checked).

The photos, the DSM and the geomorphological map are fetched while the
analysis runs, one after another; the Status section shows a second line
with what is being fetched, the tiles done and the data received over the
data expected. If one of them fails, the others and the basic analysis are
kept, and the finish message says which could not be made.
