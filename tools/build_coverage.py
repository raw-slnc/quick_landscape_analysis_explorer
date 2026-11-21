# -*- coding: utf-8 -*-
"""Make the coverage grids shipped with the plugin (terrain/data/coverage/).

For the developer, before a release: reads each source's whole list (France
about 105 requests of 5,000 tiles) and writes one JSON per source. The
plugin brings them up to date itself while QGIS runs (terrain/coverage.py);
this keeps the shipped starting point recent.

Run from the folder holding the plugin's folder:
    python3 -m quick_landscape_analysis_explorer.tools.build_coverage [key ...]
"""

import os
import sys
import time

from ..terrain import coverage


def main(keys):
    folder = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "terrain",
        "data",
        "coverage",
    )
    for key in keys or coverage.GRIDS:
        started = time.time()
        grid = coverage.build(
            key,
            progress=lambda done: print("  {} tiles".format(done), flush=True),
        )
        path = coverage.save(grid, folder)
        print(
            "{}: {} tiles, grid {} x {}, date {}, {} bytes, {:.0f} s".format(
                key,
                grid.count,
                grid.mask.shape[1],
                grid.mask.shape[0],
                grid.date,
                os.path.getsize(path),
                time.time() - started,
            )
        )


if __name__ == "__main__":
    main(sys.argv[1:])
