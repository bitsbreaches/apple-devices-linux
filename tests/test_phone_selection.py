# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Pointer selection rules for the phone file list."""

from __future__ import annotations

from devicedock.ui import selection

ORDER = ["a", "b", "c", "d", "e"]


def _apply(selected, anchor, clicked, *, control=False, shift=False):
    return selection.next_selection(ORDER, selected, anchor, clicked,
                                    selection.mode_for(control=control, shift=shift))


def test_modifier_mapping() -> None:
    assert selection.mode_for() == selection.MODE_REPLACE
    assert selection.mode_for(control=True) == selection.MODE_TOGGLE
    assert selection.mode_for(shift=True) == selection.MODE_RANGE
    assert selection.mode_for(control=True, shift=True) == selection.MODE_RANGE_ADD


def test_a_plain_click_replaces_the_selection() -> None:
    selected, anchor = _apply({"a", "b", "c"}, "c", "d")
    assert selected == frozenset({"d"})
    assert anchor == "d"


def test_ctrl_click_adds_without_losing_the_rest() -> None:
    selected, anchor = _apply({"b"}, "b", "d", control=True)
    assert selected == frozenset({"b", "d"})
    assert anchor == "d"


def test_ctrl_click_on_a_selected_item_removes_it() -> None:
    """The reported defect: a multi-selection could not be undone by clicking again."""
    selected, anchor = _apply({"b", "d"}, "d", "d", control=True)
    assert selected == frozenset({"b"})
    assert anchor == "d"


def test_ctrl_click_can_empty_the_selection() -> None:
    selected, _anchor = _apply({"b"}, "b", "b", control=True)
    assert selected == frozenset()


def test_shift_click_selects_the_range_between_anchor_and_click() -> None:
    selected, anchor = _apply({"b"}, "b", "d", shift=True)
    assert selected == frozenset({"b", "c", "d"})
    assert anchor == "b", "the anchor must stay so further Shift-clicks extend"


def test_shift_click_works_backwards() -> None:
    selected, anchor = _apply({"d"}, "d", "b", shift=True)
    assert selected == frozenset({"b", "c", "d"})
    assert anchor == "d"


def test_shift_click_replaces_rather_than_accumulates() -> None:
    selected, _anchor = _apply({"a", "e"}, "b", "c", shift=True)
    assert selected == frozenset({"b", "c"})


def test_ctrl_shift_click_adds_a_range() -> None:
    selected, _anchor = _apply({"a"}, "c", "e", control=True, shift=True)
    assert selected == frozenset({"a", "c", "d", "e"})


def test_shift_click_without_an_anchor_selects_only_the_item() -> None:
    selected, anchor = _apply(set(), None, "c", shift=True)
    assert selected == frozenset({"c"})
    assert anchor == "c"


def test_shift_click_with_a_hidden_anchor_falls_back_to_the_item() -> None:
    selected, anchor = _apply({"zz"}, "zz", "c", shift=True)
    assert selected == frozenset({"c"})
    assert anchor == "c"


def test_clicking_empty_space_clears_the_selection() -> None:
    selected, anchor = _apply({"a", "b"}, "b", None)
    assert selected == frozenset()
    assert anchor is None


def test_ranges_follow_the_displayed_order_not_the_selection_order() -> None:
    selected, _anchor = _apply({"c"}, "c", "a", shift=True)
    assert selected == frozenset({"a", "b", "c"})
