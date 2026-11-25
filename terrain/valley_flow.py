# -*- coding: utf-8 -*-
"""Valley flow: the area draining through each cell by multiple flow
direction (MFD), for the valley flow of Japan's regional analysis
(terrain/sources/japan_regional.py).

1. Depressions are filled (priority flood with a tiny rise across flats,
   Barnes et al. 2014), so the flow goes on through pits and flats.
2. MFD (Holmgren 1994; as GRASS r.watershed's MFD): a cell gives its flow
   to all its lower neighbours, in shares of (drop / distance) ^
   convergence.
3. Accumulation, upstream first (in waves: a cell is done when all the
   cells giving it flow are): the area draining through each cell (m2).

numpy only; the fill loops in Python (heapq), the rest is vectorized.
"""

import heapq
import math

import numpy as np

# Rise across flats and filled depressions (m), so they drain.
FLAT_RISE = 1e-4


def fill_depressions(dem, check=None):
    """The DEM with its depressions filled (NaN stays NaN). Cells are taken
    from the edge of the data inwards, the lowest first."""
    rows, cols = dem.shape
    z = np.where(np.isfinite(dem), dem, np.nan).astype(np.float64)
    filled = z.ravel().copy()
    valid = np.isfinite(filled)
    done = ~valid
    # The edge of the data: the grid's edge and cells next to NaN.
    pad = np.pad(~valid.reshape(rows, cols), 1, constant_values=True)
    edge = valid.reshape(rows, cols) & (
        pad[:-2, 1:-1] | pad[2:, 1:-1] | pad[1:-1, :-2] | pad[1:-1, 2:]
        | pad[:-2, :-2] | pad[:-2, 2:] | pad[2:, :-2] | pad[2:, 2:]
    )
    start = np.flatnonzero(edge.ravel())
    heap = list(zip(filled[start].tolist(), start.tolist()))
    heapq.heapify(heap)
    done[start] = True
    offsets = (-cols - 1, -cols, -cols + 1, -1, 1, cols - 1, cols, cols + 1)
    col_steps = (-1, 0, 1, -1, 1, -1, 0, 1)
    pop, push = heapq.heappop, heapq.heappush
    values = filled.tolist()
    done_list = done.tolist()
    total = len(values)
    count = 0
    while heap:
        level, cell = pop(heap)
        count += 1
        if check is not None and count % 500000 == 0:
            check(count / float(total))
        column = cell % cols
        for offset, step in zip(offsets, col_steps):
            other = cell + offset
            if other < 0 or other >= total:
                continue
            if not 0 <= column + step < cols:
                continue
            if done_list[other]:
                continue
            done_list[other] = True
            height = values[other]
            if height <= level:
                height = level + FLAT_RISE
                values[other] = height
            push(heap, (height, other))
    out = np.array(values, dtype=np.float64).reshape(rows, cols)
    out[~valid.reshape(rows, cols)] = np.nan
    return out


# MFD: the eight neighbours as (row, col) steps and their distances (cells).
_NEIGHBOURS = (
    (-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1),
)


def mfd_area(dem, cell_size, convergence=5.0, check=None):
    """The area (m2) draining through each cell by multiple flow direction
    (Holmgren 1994; as GRASS r.watershed's MFD): a cell gives its flow to
    all its lower neighbours, in shares of (drop / distance) ^ convergence
    (lower convergence: the flow spreads wider). On the DEM with its
    depressions filled. NaN outside the data."""
    z = fill_depressions(dem, check)
    rows, cols = z.shape
    size = z.size
    pad = np.pad(z, 1, constant_values=np.nan)
    r_idx, c_idx = np.indices(z.shape)
    weights = np.zeros((8, size), dtype=np.float32)
    receivers = np.zeros((8, size), dtype=np.int64)
    for k, (dr, dc) in enumerate(_NEIGHBOURS):
        other = pad[1 + dr: 1 + dr + rows, 1 + dc: 1 + dc + cols]
        distance = cell_size * math.hypot(dr, dc)
        drop = np.nan_to_num((z - other) / distance, nan=0.0)
        weights[k] = np.where(drop > 0, drop, 0.0).ravel() ** convergence
        receivers[k] = ((r_idx + dr) * cols + (c_idx + dc)).ravel()
    total = weights.sum(axis=0)
    gives = total > 0
    weights[:, gives] /= total[gives]
    receivers[weights <= 0] = 0
    donors = np.zeros(size, dtype=np.int64)
    for k in range(8):
        donors += np.bincount(
            receivers[k], weights=weights[k] > 0, minlength=size
        ).astype(np.int64)
    valid = np.isfinite(z.ravel())
    area = np.where(valid, cell_size * cell_size, 0.0)
    frontier = np.flatnonzero(donors == 0)
    waves = 0
    while frontier.size:
        waves += 1
        if check is not None and waves % 200 == 0:
            check(None)
        flow = area[frontier]
        targets = []
        for k in range(8):
            w = weights[k, frontier]
            give = w > 0
            if give.any():
                t = receivers[k, frontier][give]
                np.add.at(area, t, flow[give] * w[give])
                np.subtract.at(donors, t, 1)
                targets.append(t)
        if not targets:
            break
        t = np.unique(np.concatenate(targets))
        frontier = t[donors[t] == 0]
    area = area.reshape(z.shape)
    area[~np.isfinite(z)] = np.nan
    return area
