# -*- coding: utf-8 -*-
"""Lift the scale limits of the preview's project layers for a while.

The preview draws the project layers themselves. While the plugin window is
in front, their scale-based visibility and the scale ranges of rule-based
styles are switched off so nothing disappears at the paper's scale; the
original settings are put back when the window goes behind, closes or stops
showing the layer.

QGIS sends no signal for these changes, so the project is not marked as
modified by them.
"""

from qgis.PyQt import sip
from qgis.core import QgsRuleBasedRenderer


class ScaleLimitSuspender:
    def __init__(self):
        # layer id -> (layer, had scale visibility, renderer address,
        #              [(rule, minimum, maximum)])
        self._saved = {}

    def suspendedIds(self):
        return set(self._saved)

    def suspend(self, layers):
        """Lift the limits of these layers and restore all others."""

        wanted = {layer.id(): layer for layer in layers if _alive(layer)}
        self.restore(keep=wanted)
        for layer_id, layer in wanted.items():
            if layer_id in self._saved:
                continue
            renderer = layer.renderer() if hasattr(layer, "renderer") else None
            rules = []
            if isinstance(renderer, QgsRuleBasedRenderer):
                for rule in _all_rules(renderer.rootRule()):
                    rules.append((rule, rule.minimumScale(), rule.maximumScale()))
            had_limits = layer.hasScaleBasedVisibility()
            if not had_limits and not any(lo or hi for _r, lo, hi in rules):
                continue
            self._saved[layer_id] = (
                layer,
                had_limits,
                _address(renderer),
                rules,
            )
            layer.setScaleBasedVisibility(False)
            for rule, _lo, _hi in rules:
                rule.setMinimumScale(0)
                rule.setMaximumScale(0)
            layer.triggerRepaint()

    def restore(self, keep=None):
        """Put back the saved settings, except for the layer ids in keep."""

        keep = keep or {}
        for layer_id in list(self._saved):
            if layer_id in keep:
                continue
            layer, had_limits, renderer_address, rules = self._saved.pop(layer_id)
            if not _alive(layer):
                continue
            layer.setScaleBasedVisibility(had_limits)
            renderer = layer.renderer() if hasattr(layer, "renderer") else None
            # A style replaced in the meantime has its own rules; leave it.
            if rules and _address(renderer) == renderer_address:
                for rule, minimum, maximum in rules:
                    rule.setMinimumScale(minimum)
                    rule.setMaximumScale(maximum)
            layer.triggerRepaint()


def _all_rules(rule):
    for child in rule.children():
        yield child
        yield from _all_rules(child)


def _alive(obj):
    return obj is not None and not sip.isdeleted(obj)


def _address(obj):
    return sip.unwrapinstance(obj) if obj is not None else None
