# -*- coding: utf-8 -*-
"""Keyboard focus in the window.

Tab goes through the controls in the order of the layouts (round from the
last to the first): the left panel from top left to bottom right,
then the right panel box by box (a box from
top to bottom before the next, so the two columns of "2. DEM" and
"3. Analysis" are not crossed back and forth). Worked out at each press, so
controls shown, hidden, greyed or swapped (analysis management) are
followed without keeping a list.

The arrow keys do not move the focus: Qt moves it from buttons and check
boxes, but not from combo boxes, where they change the value, which made
them unreliable for moving. They are left to the control itself (a combo's
value, a list's rows, the text cursor, the map, the choice in a group of
radio buttons). See ControlKeys for Enter, the default button and links.
"""

from qgis.PyQt.QtCore import QEvent, QObject, Qt
from qgis.PyQt.QtWidgets import (
    QAbstractButton,
    QGroupBox,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QWidget,
)

_ARROWS = (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down)


def _takes_tab(widget):
    return bool(widget.focusPolicy() & Qt.FocusPolicy.TabFocus)


def _walk(widget, out, seen, after):
    if widget in seen:
        return
    if widget in after["moved"] and widget not in after["released"]:
        return  # taken later, after its anchor
    seen.add(widget)
    _walk_inside(widget, out, seen, after)
    for moved in after["follows"].get(widget, ()):
        after["released"].add(moved)
        _walk(moved, out, seen, after)


def _walk_inside(widget, out, seen, after):
    if isinstance(widget, QScrollArea) and widget.widget() is not None:
        _walk(widget.widget(), out, seen, after)
        return
    if _takes_tab(widget) and not isinstance(widget, QGroupBox):
        # A control (its inner parts, as a combo's text field, are its own).
        out.append(widget)
        return
    if isinstance(widget, QSplitter):
        for index in range(widget.count()):
            _walk(widget.widget(index), out, seen, after)
    layout = widget.layout()
    if layout is not None:
        _walk_layout(layout, out, seen, after)
    # Children outside the layouts, in the order they were made.
    for child in widget.children():
        if isinstance(child, QWidget) and not child.isWindow():
            _walk(child, out, seen, after)


def _walk_layout(layout, out, seen, after):
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item.widget() is not None:
            _walk(item.widget(), out, seen, after)
        elif item.layout() is not None:
            _walk_layout(item.layout(), out, seen, after)


def chain(window, skip=(), after=()):
    """The controls Tab can reach now, in order (without those in ``skip``).

    ``after``: [(part, anchor)]: a part of the window (a box) taken right
    after the anchor (another box) instead of where its layout puts it."""
    found = []
    follows = {}
    for moved, anchor in after:
        follows.setdefault(anchor, []).append(moved)
    state = {
        "moved": {moved for moved, _anchor in after},
        "released": set(),
        "follows": follows,
    }
    _walk(window, found, set(skip), state)
    return [
        widget
        for widget in found
        if widget is not window and widget.isVisible() and widget.isEnabled()
    ]


def move(window, forward, skip=(), after=()):
    """Move the focus to the next (or previous) control, passing over those
    in ``skip`` (``after``: see chain); False when there is none to move to
    (Qt's own order is used then)."""
    controls = chain(window, skip, after)
    if not controls:
        return False
    current = window.focusWidget()
    while (
        current is not None
        and current not in controls
        and current is not window
    ):
        current = current.parentWidget()
    if current in controls:
        index = controls.index(current) + (1 if forward else -1)
    else:
        index = 0 if forward else -1
    reason = (
        Qt.FocusReason.TabFocusReason
        if forward
        else Qt.FocusReason.BacktabFocusReason
    )
    # Round from the last to the first (and back with Shift+Tab).
    controls[index % len(controls)].setFocus(reason)
    return True


class ControlKeys(QObject):
    """Keys on the buttons and the link labels.

    The arrow keys do not move the focus off buttons and check boxes. Radio
    buttons keep them: Tab stops only at the chosen one of a group (Qt), and
    the arrows choose within the group, as inside one control.

    No button becomes the dialog's default by having had the focus (Qt's
    "auto default"): the last one clicked stayed drawn as the default, as if
    it had the focus too, while another control had it, and Enter there
    pressed it. Only a button made the default on purpose (Run) is; Enter
    presses the button that has the focus, as before.

    A label with a link is one stop: Qt took the focus first and the link at
    the next Tab, and Shift+Tab did not leave it. The link is chosen as the
    label takes the focus (Enter opens it), and Tab and Shift+Tab go on in
    the window's order.
    """

    def eventFilter(self, watched, event):
        kind = event.type()
        if isinstance(watched, QLabel):
            return self._label_event(watched, event)
        if kind != QEvent.Type.KeyPress or not isinstance(
            watched, QAbstractButton
        ):
            return False
        if event.key() in _ARROWS:
            return not isinstance(watched, QRadioButton)
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and isinstance(watched, QPushButton)
            and not event.modifiers()
        ):
            if watched.isEnabled():
                watched.animateClick()
            return True
        return False

    @staticmethod
    def _label_event(label, event):
        kind = event.type()
        if kind == QEvent.Type.KeyPress and event.key() in (
            Qt.Key.Key_Tab,
            Qt.Key.Key_Backtab,
        ):
            forward = event.key() == Qt.Key.Key_Tab and not (
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            )
            label.window().focusNextPrevChild(forward)
            return True
        if kind == QEvent.Type.FocusIn and event.reason() in (
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
        ):
            # Choose the link (drawn as chosen; Enter opens it).
            label.focusNextPrevChild(
                event.reason() == Qt.FocusReason.TabFocusReason
            )
        return False

    def watch(self, window):
        """Watch every button and link label in the window (again after new
        ones are made; one already watched is not watched twice)."""
        for button in window.findChildren(QAbstractButton):
            button.removeEventFilter(self)
            button.installEventFilter(self)
            if isinstance(button, QPushButton) and not button.isDefault():
                button.setAutoDefault(False)
        for label in window.findChildren(QLabel):
            if (
                label.textInteractionFlags()
                & Qt.TextInteractionFlag.LinksAccessibleByKeyboard
            ):
                label.removeEventFilter(self)
                label.installEventFilter(self)
