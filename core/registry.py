# -*- coding: utf-8 -*-

from ..analyses.pair_inspection import inspect_pair
from ..profiles.japan import ROUTES
from ..summaries.pair_text import format_pair_text


_ROUTES = {route.route_id: route for route in ROUTES}
_ANALYSES = {"pair_inspection": inspect_pair}
_SUMMARIES = {"pair_text": format_pair_text}


def routes():
    return ROUTES


def route(route_id):
    return _ROUTES[route_id]


def execute(route_id, context, tr):
    selected_route = route(route_id)
    result = _ANALYSES[selected_route.analysis_id](selected_route, context)
    return _SUMMARIES[selected_route.summary_id](result, tr)
