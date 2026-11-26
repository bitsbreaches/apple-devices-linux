# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Pointer selection rules for the lists in the application.

Clicking an item replaces the selection, Ctrl+click toggles that item, Shift+click extends
from the last clicked item, and Ctrl+Shift+click adds a range. The rules are pure functions
so they can be tested without a display, and so every list behaves the same way.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

MODE_REPLACE = "replace"
MODE_TOGGLE = "toggle"
MODE_RANGE = "range"
MODE_RANGE_ADD = "range-add"


def mode_for(*, control: bool = False, shift: bool = False) -> str:
    """Selection mode implied by the modifier keys of one click."""
    if control and shift:
        return MODE_RANGE_ADD
    if control:
        return MODE_TOGGLE
    if shift:
        return MODE_RANGE
    return MODE_REPLACE


def next_selection(
    ordered: Sequence[str],
    selected: Collection[str],
    anchor: str | None,
    clicked: str | None,
    mode: str,
) -> tuple[frozenset[str], str | None]:
    """Return ``(selection, anchor)`` after a click.

    ``ordered`` is the row order as displayed (used for ranges), ``clicked`` is ``None``
    when the click missed every row — which clears the selection, so a plain click beside
    the rows always has a way out of a multi-selection.
    """
    if clicked is None:
        return frozenset(), None
    current = set(selected)
    if mode == MODE_REPLACE:
        return frozenset({clicked}), clicked
    if mode == MODE_TOGGLE:
        if clicked in current:
            current.discard(clicked)
        else:
            current.add(clicked)
        return frozenset(current), clicked
    if anchor is None or anchor not in ordered or clicked not in ordered:
        return frozenset({clicked}), clicked
    first, last = sorted((ordered.index(anchor), ordered.index(clicked)))
    span = set(ordered[first:last + 1])
    selection = current | span if mode == MODE_RANGE_ADD else span
    # The anchor stays put so repeated Shift+clicks extend from the same origin.
    return frozenset(selection), anchor
