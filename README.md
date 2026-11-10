# Quick Landscape Analysis Explorer

Quick Landscape Analysis Explorer (QLAE) pairs a polygon area in QGIS with a
geospatial dataset. The data entrance selects the required layer type, analysis
handler, and result-summary handler.

## Development stages

### Stage 1 — terrain package (current)

- Independent normal window: it can be minimized, reached with Alt+Tab, or put
  behind QGIS. An analysis result brings it to the front once.
- English source UI with a Japanese switch stored in QGIS settings.
- Two-panel 1300 x 590 starting layout. The left preview itself is an A-series
  portrait: its width is derived from the available height, and the right
  control panel receives the remaining window width when the window is resized.
- Preview centre and scale initially follow the QGIS main map. The polygon used by the
  Area controls is shown with its QGIS style and the used features are
  highlighted over a selectable base map.
- The initial preview view comes from the QGIS main map. The preview can then
  be panned and zoomed independently; a button copies the current main-map
  centre, CRS, and scale again when requested.
- Base-map controls are directly below the preview map.
- Base-map candidates can be limited to network-derived layers.
- Disabled placeholders are present for PDF, footprint GeoPDF, layer, and
  image exports; no export processing is connected yet.
- Japan terrain profile using a local DEM layer or GSI elevation tiles.
- GSI defaults to the best available source in the order DEM1A 1 m, DEM5A
  5 m, then DEM10B 10 m.
- Hillshade, slope in degrees, and the Forestry Operations Lite CS Map logic.
- Every generated terrain raster uses one common A-series output frame. The
  chosen polygon remains the analysis mask; the preview orientation determines
  portrait or landscape, and DEM context outside the frame may still be used
  to calculate edge values correctly.
- Slope uses the non-scoring appearance of Morizon Next's
  `災害リスク/傾斜` layer.
- One named analysis is saved as one portable ZIP below
  `<project>/quick_landscape_analysis/analyses/`. Raster results remain
  GeoTIFF files inside the ZIP and QGIS reads them through `/vsizip/`.
- Saved packages can be loaded again and deliberately overwritten.

### Planned extension points

- `profiles/japan.py`: data entrances available for the Japan profile.
- `analyses/`: analysis implementations selected by each entrance.
- `summaries/`: presentation of analysis facts and warnings.
- `core/registry.py`: route from an entrance to its analysis and summary.

The polygon-area contract remains the same when an entrance changes from a
local QGIS layer to a downloader or another national/global data source.

## Next Japan routes

Later stages can add watershed and national forest-resource analysis without
changing the polygon-or-map-extent area contract or the portable package
format.
