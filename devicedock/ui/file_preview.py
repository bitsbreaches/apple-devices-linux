# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""GTK view for prepared file previews; all I/O and decoding is prepared off-thread."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, Gtk  # noqa: E402

from ..previews import PreviewDocument
from .widgets import Space, caption, scrolled


class FilePreviewPane(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        self.title = Gtk.Label(label="Preview", xalign=0, ellipsize=3)
        self.title.add_css_class("title-5")
        self.metadata = caption("")
        self.metadata.set_wrap(True)
        self.metadata.set_selectable(True)
        self.message = caption("Select a file to preview it.")
        self.message.set_wrap(True)
        self.message.set_margin_top(Space.L)
        self.message.set_margin_bottom(Space.L)
        self.message.set_margin_start(Space.L)
        self.message.set_margin_end(Space.L)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.message, "message")
        self.stack.set_visible_child_name("message")
        self.technical = Gtk.Expander(label="Technical file details")
        self.technical_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.technical_label.add_css_class("monospace")
        self.technical_label.add_css_class("caption")
        self.technical.set_child(self.technical_label)
        self.technical.set_visible(False)
        self.append(self.title)
        self.append(self.metadata)
        self.append(scrolled(self.stack))
        self.append(self.technical)
        self.media_stream: Gtk.MediaStream | None = None
        self.picture: Gtk.Picture | None = None
        self.texture = None

    def show_loading(self, title: str) -> None:
        self.title.set_text(title)
        self.metadata.set_text("")
        self.technical.set_visible(False)
        self.message.set_text(f"Preparing preview for {title}…")
        self.stack.set_visible_child_name("message")

    def clear(self, message: str) -> None:
        self.title.set_text("Preview")
        self.metadata.set_text("")
        self.technical.set_visible(False)
        self.message.set_text(message)
        self.stack.set_visible_child_name("message")
        self._remove_dynamic()

    def show_document(self, title: str, document: PreviewDocument, technical: str = "") -> None:
        self.title.set_text(title)
        self.metadata.set_text("  ·  ".join(f"{key}: {value}" for key, value in document.metadata))
        self.technical_label.set_text(technical)
        self.technical.set_visible(bool(technical))
        self._remove_dynamic()
        if document.kind in {"image", "pdf"}:
            image_path = document.thumbnail or document.path
            if image_path is None:
                self._show_message(document.message or "Image preview is unavailable.")
                return
            try:
                self.texture = Gdk.Texture.new_from_file(Gio.File.new_for_path(str(image_path)))
            except Exception as exc:  # Gdk may report format errors with GLib.Error
                self._show_message(f"This image format cannot be previewed here. Export the original file. ({exc})")
                return
            picture = Gtk.Picture.new_for_paintable(self.texture)
            picture.set_can_shrink(True)
            picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            self.picture = picture
            self._show_dynamic(picture)
        elif document.kind == "text":
            view = Gtk.TextView(editable=False, monospace=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
            view.set_cursor_visible(False)
            view.get_buffer().set_text(document.text)
            self._show_dynamic(view)
        elif document.kind in {"audio", "video"} and document.path is not None:
            video = Gtk.Video.new_for_file(Gio.File.new_for_path(str(document.path)))
            video.set_autoplay(False)
            if document.kind == "audio":
                video.set_size_request(-1, 120)
            self.media_stream = video.get_media_stream()
            media_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
            if document.kind == "video" and document.thumbnail is not None:
                # Gtk.Video has no poster or controls setters. Use a real GTK picture
                # as the initial poster and Gtk.MediaControls for play/seek controls.
                overlay = Gtk.Overlay()
                overlay.set_child(video)
                try:
                    poster_texture = Gdk.Texture.new_from_file(
                        Gio.File.new_for_path(str(document.thumbnail)))
                except Exception:
                    poster_texture = None
                if poster_texture is not None:
                    poster = Gtk.Picture.new_for_paintable(poster_texture)
                    poster.set_can_shrink(True)
                    poster.set_content_fit(Gtk.ContentFit.CONTAIN)
                    poster.set_halign(Gtk.Align.FILL)
                    poster.set_valign(Gtk.Align.FILL)
                    overlay.add_overlay(poster)
                    self.media_stream.connect(
                        "notify::playing",
                        lambda stream, _param, image=poster: image.set_visible(
                            not stream.get_playing()),
                    )
                media_box.append(overlay)
            else:
                media_box.append(video)
            media_box.append(Gtk.MediaControls.new(self.media_stream))
            self._show_dynamic(media_box)
        else:
            self._show_message(document.message or "No preview is available for this file. Export the original to open it elsewhere.")

    def _show_message(self, text: str) -> None:
        self.message.set_text(text)
        self.stack.set_visible_child_name("message")

    def _show_dynamic(self, widget: Gtk.Widget) -> None:
        self.stack.add_named(widget, "content")
        self.stack.set_visible_child_name("content")

    def _remove_dynamic(self) -> None:
        child = self.stack.get_child_by_name("content")
        if child is not None:
            if self.media_stream is not None:
                self.media_stream.pause()
            self.stack.remove(child)
        self.picture = None
        self.texture = None
        self.media_stream = None

