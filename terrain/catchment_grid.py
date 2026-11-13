# -*- coding: utf-8 -*-
"""Catchment labels on a grid with numpy only (no GRASS).

Used where GRASS r.watershed is not available (for example the QGIS disk
image for macOS), or always, as watershed.CATCHMENT_ENGINE chooses. It
follows the rules of r.watershed's MFD mode (GRASS raster/r.watershed,
do_cum.c, do_cum_mfd), written again here:

1. Routing (as r.watershed's A* search): cells are taken from the edge of
   the data inwards, the lowest queued cell first, each by its own height
   (heights rounded to mm). Every cell is reached from a neighbour, which is
   its way out, also from inside a depression.
2. Flow accumulation (MFD), upstream cells first: a cell gives its flow to
   the neighbours later on its way down that are not higher, weighted by
   (drop / distance) ^ convergence; a neighbour of the same height gets a
   tiny weight, and the routing's next cell is always among them. With only
   one such neighbour all the flow goes to the routing's next cell. A cell
   next to NoData or the grid's edge gives no flow on (as r.watershed, to
   avoid artifacts along edges).
3. Streams, upstream cells first: the drainage direction of a cell is the
   neighbour further down with the most flow, leaving out the cells next to
   NoData (the routing's next cell when nothing else is left). A stream starts where the flow
   reaches the threshold (the smallest catchment), if no higher neighbour
   already has that much flow, no neighbour is already a stream and the cell
   is not flat; it goes on along the drainage directions. Streams are cut
   into segments where they meet.
4. Each cell belongs to the segment it reaches first along the drainage
   directions; every segment is one catchment.

On test DEMs (5 m, Bavarian Alps 6 x 4 km and Palatinate Forest 8 x 6 km)
the catchments were the same as r.watershed's in number, and in the middle
third (the part a catchment map keeps: the DEM is fetched three times wider)
every cell was in the same catchment as with r.watershed. Differences were
left only along the outer edge of the grid. It is not guaranteed for every
DEM. The steps loop over the cells in Python, so they report progress and
can be stopped.
"""

import heapq
import math
from array import array

import numpy as np

METHOD_TEXT = (
    "QLAE (numpy, after the rules of GRASS r.watershed): A*-like routing, "
    "MFD accumulation (convergence 5), catchments by stream segment"
)

# Progress is reported (and cancelling checked) this many times per step.
_REPORTS = 20
# Drop given to a neighbour of the same height (m), as r.watershed's half
# unit of its scaled elevations: small, so such a neighbour gets little flow.
_FLAT_DROP = 0.0005
# Chosen by comparing with r.watershed on test DEMs (Bavaria, Rhineland-
# Palatinate, 5 m): with these three the catchments were the same in number
# and the boundaries agreed within one cell for over 99% of their cells.
# Routing key: each cell's own height (as r.watershed's A* search), not the
# height a depression spills over at.
_RAISE_PRIORITY = False
# Heights rounded to mm, as r.watershed keeps a float DEM as integers
# (more equal heights, so flats are handled alike).
_ELEVATION_STEP = 0.001
# Neighbours in r.watershed's order (S, N, W, E, then the diagonals), which
# decides between neighbours of equal flow.
_NEIGHBOUR_ORDER = "grass"


def basin_labels(z, threshold, convergence, context):
    """Catchment label per cell (0 = none) for a DEM array.

    z           : 2-D float array, NaN for NoData (cells are square)
    threshold   : smallest catchment, in cells
    convergence : MFD exponent (r.watershed's convergence factor)
    context     : has report(text) and check()
    """

    rows, cols = z.shape
    width = cols + 2
    # One cell of NoData around the grid: neighbours never fall outside.
    valid_grid = np.zeros((rows + 2, cols + 2), dtype=bool)
    valid_grid[1:-1, 1:-1] = np.isfinite(z)
    heights = np.full((rows + 2, cols + 2), np.inf)
    heights[1:-1, 1:-1] = np.where(np.isfinite(z), z, np.inf)
    size = valid_grid.size
    total = int(valid_grid.sum())
    if total == 0:
        return np.zeros((rows, cols), dtype=np.int32)

    # Cells next to NoData (including the grid's edge): the routing starts
    # there, and they pass no flow on.
    invalid = ~valid_grid
    near_invalid = np.zeros_like(valid_grid)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                near_invalid |= np.roll(np.roll(invalid, dy, axis=0), dx, axis=1)
    edge_grid = valid_grid & near_invalid
    seeds = np.flatnonzero(edge_grid)
    edge = bytearray(edge_grid.ravel().astype(np.uint8).tobytes())

    root2 = math.sqrt(2.0)
    if _NEIGHBOUR_ORDER == "grass":
        # S, N, W, E, NE, SW, SE, NW (r.watershed's nextdr / nextdc)
        steps = ((1, 0), (-1, 0), (0, -1), (0, 1), (-1, 1), (1, -1), (1, 1), (-1, -1))
    else:
        steps = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
    neighbours = tuple(
        (dy * width + dx, root2 if dy and dx else 1.0) for dy, dx in steps
    )

    valid = bytearray(valid_grid.ravel().astype(np.uint8).tobytes())
    if _ELEVATION_STEP:
        finite = np.isfinite(heights)
        heights[finite] = np.round(heights[finite] / _ELEVATION_STEP) * _ELEVATION_STEP
    raw = array("d", heights.ravel().tolist())
    parent = array("i", [-1]) * size

    # 1. Routing (priority flood): the visiting order and each cell's next
    # cell on its way out (the cell it was reached from).
    context.report("Catchments: routing the flow… 0%")
    seen = bytearray(valid)  # 1 = valid and not yet queued
    heap = []
    for count, cell in enumerate(seeds.tolist()):
        seen[cell] = 0
        heap.append((raw[cell], count, cell))
    heapq.heapify(heap)
    counter = len(heap)
    order = array("i")
    step = max(1, total // _REPORTS)
    pop, push = heapq.heappop, heapq.heappush
    while heap:
        level, _count, cell = pop(heap)
        order.append(cell)
        if len(order) % step == 0:
            context.check()
            context.report("Catchments: routing the flow… {}%".format(
                min(99, 100 * len(order) // total)))
        for offset, _distance in neighbours:
            other = cell + offset
            if seen[other]:
                seen[other] = 0
                height = raw[other]
                parent[other] = cell
                counter += 1
                if _RAISE_PRIORITY and height < level:
                    height = level
                push(heap, (height, counter, other))
    del heap, seen

    # Visiting position of each cell (-1 for NoData): a smaller position is
    # further down the way out.
    rank_grid = np.full(size, -1, dtype=np.int32)
    rank_grid[np.frombuffer(order, dtype=np.int32)] = np.arange(len(order), dtype=np.int32)
    rank = array("i")
    rank.frombytes(rank_grid.tobytes())
    del rank_grid
    context.check()

    # 2. MFD accumulation, upstream (last visited) first.
    accumulation = array("d", [1.0]) * size
    done = 0
    for cell in reversed(order):
        done += 1
        if done % step == 0:
            context.check()
            context.report("Catchments: accumulating the flow (MFD)… {}%".format(
                min(99, 100 * done // total)))
        if edge[cell]:
            continue
        height = raw[cell]
        position = rank[cell]
        nxt = parent[cell]
        targets = []
        weights = []
        biggest = 0.0
        has_next = False
        for offset, distance in neighbours:
            other = cell + offset
            if rank[other] < position and valid[other]:
                below = raw[other]
                if below <= height:
                    drop = height - below if below < height else _FLAT_DROP
                    weight = (drop / distance) ** convergence
                    targets.append(other)
                    weights.append(weight)
                    if weight > biggest:
                        biggest = weight
                    if other == nxt:
                        has_next = True
        if targets and not has_next and nxt >= 0:
            targets.append(nxt)
            weights.append(biggest)
        flow = accumulation[cell]
        if len(targets) > 1:
            share = flow / sum(weights)
            for other, weight in zip(targets, weights):
                accumulation[other] += share * weight
        elif nxt >= 0:
            accumulation[nxt] += flow
    context.check()

    # 3. Drainage directions and streams, upstream first.
    downstream = array("i", [-1]) * size
    segment = array("i", [0]) * size
    donors = array("i", [0]) * size  # stream cells draining into a cell
    donor_segment = array("i", [0]) * size
    is_stream = bytearray(size)
    segments = 0
    done = 0
    for cell in reversed(order):
        done += 1
        if done % step == 0:
            context.check()
            context.report("Catchments: finding the streams… {}%".format(
                min(99, 100 * done // total)))
        stream = is_stream[cell]
        if not edge[cell]:
            height = raw[cell]
            position = rank[cell]
            best, best_flow = parent[cell], -1.0
            higher_stream = False
            beside_stream = False
            flat = True
            for offset, _distance in neighbours:
                other = cell + offset
                if not valid[other]:
                    continue
                if is_stream[other]:
                    beside_stream = True
                below = raw[other]
                if below > height and accumulation[other] + 0.5 >= threshold:
                    higher_stream = True
                if rank[other] < position:
                    if below != height:
                        flat = False
                    if edge[other]:
                        # As r.watershed: cells that pass no flow on are not
                        # drainage targets (only the routing's fallback).
                        continue
                    if accumulation[other] > best_flow:
                        best, best_flow = other, accumulation[other]
            downstream[cell] = best
            if (not stream and accumulation[cell] + 0.5 >= threshold
                    and not higher_stream and not beside_stream and not flat):
                stream = 1
        if stream:
            is_stream[cell] = 1
            if donors[cell] == 1:
                segment[cell] = donor_segment[cell]
            else:  # a stream head, or streams meet here: a new segment
                segments += 1
                segment[cell] = segments
            below = downstream[cell]
            if below >= 0:
                is_stream[below] = 1
                donors[below] += 1
                donor_segment[below] = segment[cell]
    del accumulation, donors, donor_segment, is_stream, raw
    context.check()

    # 4. Catchments: the segment reached first downstream (downstream first).
    context.report("Catchments: labelling the catchments…")
    label = array("i", [0]) * size
    for cell in order:
        own = segment[cell]
        if own:
            label[cell] = own
        else:
            below = downstream[cell]
            if below >= 0:
                label[cell] = label[below]
    context.check()
    labels = np.frombuffer(label, dtype=np.int32).reshape(rows + 2, cols + 2)
    return labels[1:-1, 1:-1].copy()
