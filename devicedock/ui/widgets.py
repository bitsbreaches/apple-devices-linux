# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Shared GTK widgets: page scaffolding, key/value rows and a simple table."""

from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GObject, Gtk  # noqa: E402


class RowItem(GObject.Object):
    """One display row: string cells plus the object it represents."""

    def __init__(self, values: Sequence[str], payload: Any = None) -> None:
        super().__init__()
        self.values = list(values)
        self.payload = payload

    def __getitem__(self, index: int) -> str:
        return self.values[index]


def key_value_grid(pairs: Iterable[tuple[str, str]], columns: int = 2) -> Gtk.Grid:
    grid = Gtk.Grid(column_spacing=18, row_spacing=6)
    grid.add_css_class("kv-grid")
    for index, (key, value) in enumerate(pairs):
        key_label = Gtk.Label(label=key, xalign=0)
        key_label.add_css_class("dim-label")
        value_label = Gtk.Label(label=value or "—", xalign=0, wrap=True, selectable=True)
        row, column = divmod(index, columns)
        grid.attach(key_label, column * 2, row, 1, 1)
        grid.attach(value_label, column * 2 + 1, row, 1, 1)
    return grid


def card(child: Gtk.Widget, title: str | None = None, subtitle: str | None = None) -> Gtk.Widget:
    """A rounded card with an optional heading, matching the GNOME boxed-list look."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.add_css_class("card")
    box.set_margin_top(12)
    box.set_margin_bottom(12)
    box.set_margin_start(12)
    box.set_margin_end(12)
    if title:
        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class("heading")
        box.append(label)
    if subtitle:
        sub = Gtk.Label(label=subtitle, xalign=0, wrap=True)
        sub.add_css_class("dim-label")
        box.append(sub)
    box.append(child)
    return box


def scrolled(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroller.set_child(child)
    scroller.set_vexpand(True)
    scroller.set_hexpand(True)
    return scroller


class DataTable(Gtk.Box):
    """Read-only table built on ``Gtk.ColumnView`` with a selection model."""

    def __init__(self, columns: Sequence[str], *, on_activate: Callable[[RowItem], None] | None = None,
                 on_selection: Callable[[RowItem | None], None] | None = None,
                 multi_select: bool = False) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._columns = list(columns)
        self._store = Gio.ListStore(item_type=RowItem)
        self._multi_select = multi_select
        if multi_select:
            self._selection = Gtk.MultiSelection(model=self._store)
        else:
            self._selection = Gtk.SingleSelection(model=self._store)
            self._selection.set_autoselect(False)
            self._selection.set_can_unselect(True)
        self.view = Gtk.ColumnView(model=self._selection)
        self.view.set_show_column_separators(False)
        self.view.set_show_row_separators(True)
        self.view.add_css_class("data-table")
        self._add_columns()

        if on_activate is not None:
            self.view.connect("activate", lambda _view, position: on_activate(self._store.get_item(position)))
        if on_selection is not None:
            if multi_select:
                def changed(selection, _position, _count):
                    items = self.selected_items()
                    on_selection(items[-1] if items else None)

                self._selection.connect("selection-changed", changed)
            else:
                def changed(selection, _pspec):
                    item = selection.get_selected_item()
                    on_selection(item if isinstance(item, RowItem) else None)

                self._selection.connect("notify::selected-item", changed)

        self.append(scrolled(self.view))

    def _add_columns(self) -> None:
        for index, name in enumerate(self._columns):
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", _setup_cell)
            factory.connect("bind", _bind_cell, index)
            column = Gtk.ColumnViewColumn(title=name, factory=factory)
            column.set_resizable(True)
            column.set_expand(index == 0)
            self.view.append_column(column)

    def set_columns(self, columns: Sequence[str]) -> None:
        """Replace the visible columns (used when a category's schema changes)."""
        existing = self.view.get_columns()
        while existing.get_n_items():
            self.view.remove_column(existing.get_item(0))
        self._columns = list(columns)
        self._add_columns()

    def set_rows(self, rows: Iterable[RowItem]) -> None:
        self._store.remove_all()
        for row in rows:
            self._store.append(row)

    def clear(self) -> None:
        self._store.remove_all()

    def selected(self) -> RowItem | None:
        items = self.selected_items()
        return items[0] if items else None

    def selected_items(self) -> list[RowItem]:
        if self._multi_select:
            bitset = self._selection.get_selection()
            items = []
            for index in range(bitset.get_size()):
                item = self._store.get_item(bitset.get_nth(index))
                if isinstance(item, RowItem):
                    items.append(item)
            return items
        item = self._selection.get_selected_item()
        return [item] if isinstance(item, RowItem) else []

    def select_row(self, index: int) -> None:
        if 0 <= index < self._store.get_n_items():
            self._selection.set_selected(index)

    @property
    def row_count(self) -> int:
        return self._store.get_n_items()


def _setup_cell(_factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
    label = Gtk.Label(xalign=0, ellipsize=3)
    label.set_margin_top(4)
    label.set_margin_bottom(4)
    label.set_margin_start(4)
    label.set_margin_end(4)
    list_item.set_child(label)


def _bind_cell(_factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem, index: int) -> None:
    item = list_item.get_item()
    label = list_item.get_child()
    values = item.values
    label.set_text(values[index] if index < len(values) else "")


def banner(message: str = "", *, kind: str = "info") -> Adw.Banner:
    widget = Adw.Banner(title=message)
    widget.set_revealed(bool(message))
    apply_banner_kind(widget, kind)
    return widget


def apply_banner_kind(widget: Adw.Banner, kind: str) -> None:
    for name in ("error", "warning"):
        widget.remove_css_class(name)
    if kind in ("error", "warning"):
        widget.add_css_class(name)


def set_banner(widget: Adw.Banner, message: str, kind: str = "info") -> None:
    widget.set_title(message)
    widget.set_revealed(bool(message))
    apply_banner_kind(widget, kind)


def icon_text_button(label: str, icon_name: str) -> Gtk.Button:
    """A button showing both an icon and a label.

    ``Gtk.Button(icon_name=...)`` replaces the label child, so the content is built here.
    """
    button = Gtk.Button()
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    box.append(Gtk.Image.new_from_icon_name(icon_name))
    box.append(Gtk.Label(label=label))
    button.set_child(box)
    return button


# --------------------------------------------------------------------------- design system
class Space:
    """Spacing scale on a 4 px grid; every margin in the UI comes from here."""

    XS = 4
    S = 8
    M = 12
    L = 16
    XL = 24
    XXL = 32


PAGE_MARGIN_X = Space.XL
PAGE_MARGIN_Y = Space.L
CONTENT_MAX_WIDTH = 1000
SIDEBAR_WIDTH = 248
ROW_PADDING_Y = 6


def section_heading(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0)
    label.add_css_class("heading")
    return label


def caption(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0, wrap=True)
    label.add_css_class("dim-label")
    label.add_css_class("caption")
    return label


class EmptyState(Gtk.Box):
    """Consistent empty placeholder: icon, title, explanation and an optional action."""

    def __init__(self, icon_name: str, title: str, description: str,
                 action: Gtk.Widget | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_margin_top(Space.XXL)
        self.set_margin_bottom(Space.XXL)
        self.icon = Gtk.Image.new_from_icon_name(icon_name)
        self.icon.set_pixel_size(48)
        self.icon.add_css_class("dim-label")
        self.append(self.icon)
        self.heading = Gtk.Label(label=title, justify=Gtk.Justification.CENTER, wrap=True)
        self.heading.add_css_class("title-4")
        self.append(self.heading)
        self.body = Gtk.Label(label=description, justify=Gtk.Justification.CENTER, wrap=True,
                              max_width_chars=52)
        self.body.add_css_class("dim-label")
        self.append(self.body)
        if action is not None:
            action.set_halign(Gtk.Align.CENTER)
            action.set_margin_top(Space.S)
            self.append(action)

    def set_title(self, text: str) -> None:
        self.heading.set_text(text)

    def set_description(self, text: str) -> None:
        self.body.set_text(text)


class PageScaffold(Gtk.Box):
    """Common page skeleton: header block, scrolling body, optional footer.

    All seven pages use this so titles, margins, spacing and action placement match.
    """

    def __init__(self, title: str, subtitle: str = "", *, actions: list[Gtk.Widget] | None = None,
                 max_width: int = CONTENT_MAX_WIDTH, scroll: bool = True) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
        header.set_margin_top(PAGE_MARGIN_Y)
        header.set_margin_start(PAGE_MARGIN_X)
        header.set_margin_end(PAGE_MARGIN_X)

        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        titles.set_hexpand(True)
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("title-1")
        titles.append(title_label)
        if subtitle:
            subtitle_label = Gtk.Label(label=subtitle, xalign=0, wrap=True)
            subtitle_label.add_css_class("dim-label")
            titles.append(subtitle_label)
        header.append(titles)

        self.actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.actions.set_valign(Gtk.Align.CENTER)
        for widget in actions or []:
            self.actions.append(widget)
        header.append(self.actions)

        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.L)
        self.body.set_margin_top(Space.L)
        self.body.set_margin_bottom(PAGE_MARGIN_Y)
        self.body.set_margin_start(PAGE_MARGIN_X)
        self.body.set_margin_end(PAGE_MARGIN_X)

        self.footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.footer.set_margin_start(PAGE_MARGIN_X)
        self.footer.set_margin_end(PAGE_MARGIN_X)
        self.footer.set_margin_bottom(PAGE_MARGIN_Y)
        self.footer.set_visible(False)

        self.append(header)
        clamp = Adw.Clamp(maximum_size=max_width, child=self.body)
        if scroll:
            self.append(scrolled(clamp))
        else:
            self.append(clamp)
        self.append(self.footer)

    def add(self, widget: Gtk.Widget) -> Gtk.Widget:
        widget.set_margin_top(0)
        widget.set_margin_bottom(0)
        self.body.append(widget)
        return widget

    def add_section(self, title: str, widget: Gtk.Widget, description: str = "") -> Gtk.Widget:
        block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        block.append(section_heading(title))
        if description:
            block.append(caption(description))
        block.append(widget)
        return self.add(block)

    def set_footer(self, widget: Gtk.Widget) -> None:
        child = self.footer.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.footer.remove(child)
            child = nxt
        self.footer.append(widget)
        self.footer.set_visible(True)


def panel(child: Gtk.Widget, *, padding: int = Space.L) -> Gtk.Widget:
    """A card with the shared radius/padding, replacing ad-hoc framed boxes."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)
    box.add_css_class("card")
    box.set_margin_top(0)
    box.set_margin_bottom(0)
    box.set_margin_start(0)
    box.set_margin_end(0)
    box.append(child)
    return box


def metadata_list(pairs: Iterable[tuple[str, str]], *, title: str | None = None) -> Gtk.Widget:
    """A boxed list of read-only metadata rows.

    Preferred over :func:`key_value_grid` inside a narrow pane: each row owns its width, so a
    long firmware string or path ellipsizes instead of clipping the key next to it.
    """
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
    if title:
        box.append(section_heading(title))
    listing = Gtk.ListBox()
    listing.add_css_class("boxed-list")
    listing.set_selection_mode(Gtk.SelectionMode.NONE)
    for key, value in pairs:
        row = Adw.ActionRow(title=key)
        text_value = str(value) if value else "—"
        row.set_subtitle(text_value)
        # A narrow detail pane must wrap instead of clipping the value out of reach.
        row.set_subtitle_lines(3)
        row.set_title_lines(1)
        row.set_tooltip_text(text_value)
        listing.append(row)
    box.append(listing)
    return box


def count_pill(text: str) -> Gtk.Label:
    """Small rounded label used for record counts and states."""
    label = Gtk.Label(label=text)
    label.add_css_class("caption")
    label.add_css_class("dim-label")
    label.set_valign(Gtk.Align.CENTER)
    return label


def status_strip(*labels: Gtk.Widget) -> Gtk.Widget:
    """A horizontal, low-emphasis line of facts shown above a table."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
    box.add_css_class("status-strip")
    for index, label in enumerate(labels):
        if index:
            separator = Gtk.Label(label="·")
            separator.add_css_class("dim-label")
            separator.add_css_class("caption")
            box.append(separator)
        box.append(label)
    return box
