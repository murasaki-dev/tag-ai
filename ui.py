"""Native window, controls, and settings interactions."""

from PIL import Image, ImageDraw
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import providers
from tkinter import Canvas, Frame, PanedWindow, filedialog, messagebox
import customtkinter as ctk
import os
import queue
import sys
import threading
import time
import uuid
from storage import BatchSessionStore, DisabledResultCache, PersistentResultCache, StdoutRedirector, StorageMixin
from routing import AUTO_MODEL, CONTEXT_METHODS, DATABASE_METHODS, FLOW_LABELS, PIPELINE_METHODS, RoutingMixin, available_model_ids, enabled_flow_methods, enabled_search_methods, legacy_database_flow, legacy_search_pipeline, normalize_named_flow
from providers import ProviderMixin, tracker
from history import HistoryMixin
from workflow import WorkflowMixin
from routing import UNMATCHED_ACTIONS, MATCHING_MODES

# Let CustomTkinter own per-monitor DPI awareness and scaling.
ctk.set_appearance_mode("System")


ctk.set_default_color_theme("green")


def invalidate_native_window(widget):
    """Ask Windows to repaint the client and descendants without blocking Tk."""
    if sys.platform != 'win32' or not widget.winfo_viewable():
        return
    import ctypes
    from ctypes import wintypes
    redraw = ctypes.windll.user32.RedrawWindow
    redraw.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    redraw.restype = wintypes.BOOL
    # Invalidate/erase all child windows. Do not use synchronous UPDATENOW or
    # composited-window styles, which can stall dragging while requests run.
    return bool(redraw(widget.winfo_id(), None, None, 0x0001 | 0x0004 | 0x0080))


class WrapActionBar(ctk.CTkFrame):
    """Lay out existing action buttons in rows without recreating controls."""
    def __init__(self, master, on_reflow=None, **kwargs):
        super().__init__(master, **kwargs)
        self._buttons = []
        self._layout_job = None
        self._placement = None
        self._last_width = None
        self.on_reflow = on_reflow
        self.bind('<Configure>', self._resized, add='+')

    def _resized(self, event):
        if event.width != self._last_width:
            self._last_width = event.width
            self._schedule_layout()

    def set_buttons(self, buttons):
        for button in self._buttons:
            if button not in buttons:
                button.grid_forget()
        self._buttons = list(buttons)
        self._schedule_layout()

    def _schedule_layout(self):
        if self._layout_job is None:
            self._layout_job = self.after(16, self._layout_buttons)

    def _layout_buttons(self):
        self._layout_job = None
        width = max(1, self._reverse_widget_scaling(self.winfo_width()))
        row = column = 0
        used = 0
        placement = []
        for button in self._buttons:
            needed = max(float(button.cget('width')), self._reverse_widget_scaling(button.winfo_reqwidth())) + 8
            if column and used + needed > width:
                row += 1; column = 0; used = 0
            placement.append((button, row, column))
            column += 1; used += needed
        if placement != self._placement:
            for button, row, column in placement:
                button.grid(row=row, column=column, sticky='w', padx=(0, 8), pady=(0, 5))
            self._placement = placement
            if self.on_reflow:
                self.on_reflow()

    def destroy(self):
        if self._layout_job is not None:
            self.after_cancel(self._layout_job)
        super().destroy()


class SearchFlowChart(ctk.CTkFrame):
    """Small native drag/drop schematic backed by the same config as execution."""
    def __init__(self, master, command, methods=PIPELINE_METHODS, kind="search", expand_command=None):
        super().__init__(master, fg_color="transparent")
        self.command = command
        self.methods, self.kind, self.expand_command = tuple(methods), kind, expand_command
        self.expanded = None
        self.steps = legacy_search_pipeline("IQDB") if kind == "search" else [{"method": key, "enabled": True} for key in self.methods]
        self.selected = self.steps[0]["method"]
        self._drag = None
        self._draw_job = None
        self._boxes = []
        self._context = ""
        self._outcome_target = "Leave in original source folder"
        self._outcome_metadata = False
        self.preflight_label = ctk.CTkLabel(self, text="Image → cached tags → search if needed", anchor="w", justify="left",
            wraplength=640, text_color=("#256b55", "#8ac9af"), font=ctk.CTkFont(family="Segoe UI", size=12))
        if kind == "search":
            self.preflight_label.pack(fill="x", pady=(0, 6))
        self.canvas = Canvas(self, height=195, highlightthickness=0, takefocus=True)
        self.canvas.pack(fill="x")
        self.canvas.bind("<Configure>", lambda event: self._schedule_draw())
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Escape>", lambda event: self._cancel_drag())
        self.canvas.bind("<Alt-Left>", lambda event: self._keyboard_move(-1))
        self.canvas.bind("<Alt-Right>", lambda event: self._keyboard_move(1))
        self.canvas.bind("<space>", lambda event: self._toggle_selected())
        self.canvas.bind("<Return>", lambda event: self.expand_selected())
        controls = ctk.CTkFrame(self, fg_color="transparent")
        self.controls = controls
        controls.pack(fill="x", pady=(0, 5))
        self.selection_label = ctk.CTkLabel(controls, text="Selected: IQDB", anchor="w", font=ctk.CTkFont(family="Segoe UI", size=12))
        self.selection_label.pack(side="left", fill="x", expand=True)
        for text, delta in (("← Earlier", -1), ("Later →", 1)):
            ctk.CTkButton(controls, text=text, width=88, height=28, corner_radius=5, border_width=1,
                fg_color=("#ffffff", "#20242b"), border_color=("#e1e5eb", "#353c46"),
                text_color=("#222b36", "#e7ebf1"), hover_color=("#edf0f5", "#2b323c"),
                command=lambda delta=delta: self._keyboard_move(delta)).pack(side="left", padx=(6, 0))
        if expand_command:
            ctk.CTkButton(controls, text="Details ▾", width=86, height=28, corner_radius=5,
                fg_color=("#edf0f5", "#2b323c"), text_color=("#222b36", "#e7ebf1"),
                command=self.expand_selected).pack(side="left", padx=(6, 0))
        self.context_label = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=640,
            text_color=("#626d7c", "#a5afbd"), font=ctk.CTkFont(family="Segoe UI", size=12))
        if kind == "search":
            self.context_label.pack(fill="x", pady=(3, 0))
        self._schedule_draw()

    def get_config(self):
        return [dict(step) for step in self.steps]

    def set_outcome(self, move_unmatched, embed_metadata):
        self._outcome_target = "Move to Unmatched folder" if move_unmatched else "Leave in original source folder"
        self._outcome_metadata = bool(embed_metadata)
        self._schedule_draw()

    def set_config(self, config, notify=False):
        self.steps = normalize_named_flow(config, self.methods)
        self._schedule_draw()
        if notify:
            self.command(self.get_config())

    def set_context(self, text, preflight=None):
        if text != self._context:
            self._context = text
            self.context_label.configure(text=text)
        if preflight is not None:
            self.preflight_label.configure(text=preflight)

    def reorder(self, method, index):
        old = next(i for i, step in enumerate(self.steps) if step["method"] == method)
        index = max(0, min(int(index), len(self.steps) - 1))
        if index != old:
            step = self.steps.pop(old)
            self.steps.insert(index, step)
            self.command(self.get_config())
        self._schedule_draw()

    def toggle(self, method):
        for step in self.steps:
            if step["method"] == method:
                step["enabled"] = not step["enabled"]
                break
        self.command(self.get_config())
        self._schedule_draw()

    def _keyboard_move(self, delta):
        index = next(i for i, step in enumerate(self.steps) if step["method"] == self.selected)
        self.reorder(self.selected, index + delta)
        return "break"

    def _toggle_selected(self):
        self.toggle(self.selected)
        return "break"

    def _press(self, event):
        self.canvas.focus_set()
        for index, (x1, y1, x2, y2) in enumerate(self._boxes):
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.selected = self.steps[index]["method"]
                if event.x >= x2 - self._apply_widget_scaling(32) and event.y <= y1 + self._apply_widget_scaling(32):
                    self.toggle(self.selected)
                elif self.expand_command and event.y >= y2 - self._apply_widget_scaling(23):
                    self.expand_selected()
                else:
                    self._drag = {"method": self.selected, "start": event.x, "start_y": event.y, "x": event.x, "y": event.y, "active": False}
                    self.canvas.grab_set()
                self._schedule_draw()
                break

    def expand_selected(self):
        if self.expand_command:
            self.expanded = None if self.expanded == self.selected else self.selected
            self.expand_command(self.expanded)
            self._schedule_draw()
        return "break"

    def _motion(self, event):
        if self._drag is not None:
            self._drag["x"] = event.x
            self._drag["y"] = event.y
            self._drag["active"] |= max(abs(event.x - self._drag["start"]), abs(event.y - self._drag["start_y"])) > 6
            self._schedule_draw()

    def _release(self, event):
        drag = self._drag
        self._cancel_drag()
        if drag and drag["active"] and self._boxes and 0 <= event.y <= self.canvas.winfo_height():
            index = self._drop_index(event.x, event.y)
            self.reorder(drag["method"], index)

    def _drop_index(self, x, y):
        return min(range(len(self._boxes)), key=lambda i:
            (x - (self._boxes[i][0] + self._boxes[i][2]) / 2) ** 2 +
            (y - (self._boxes[i][1] + self._boxes[i][3]) / 2) ** 2)

    def _cancel_drag(self):
        if self._drag is not None:
            self.canvas.grab_release()
        self._drag = None
        self._schedule_draw()

    def _schedule_draw(self):
        if self._draw_job is None:
            self._draw_job = self.after_idle(self._draw_flow)

    def _draw_flow(self):
        self._draw_job = None
        if not self.winfo_exists():
            return
        scale = self._get_widget_scaling()
        canvas = self.canvas
        color = self._apply_appearance_mode
        bg, panel, muted, ink, border, accent = map(color, (
            ("#f6f7f9", "#181b20"), ("#ffffff", "#20242b"), ("#626d7c", "#a5afbd"),
            ("#222b36", "#e7ebf1"), ("#d3dbe4", "#424c59"), ("#256b55", "#8ac9af")))
        width = max(300, canvas.winfo_width())
        columns = min(3, len(self.steps))
        rows = (len(self.steps) + columns - 1) // columns
        node_height = 96 if self.expand_command else 79
        row_stride = node_height + 31
        bottom = 40 + (rows - 1) * row_stride + node_height
        canvas.configure(bg=bg, height=int((bottom + (180 if self.kind == 'search' else 77)) * scale))
        canvas.delete("all")
        font = ("Segoe UI", -round(12 * scale))
        title_font = ("Segoe UI", -round(14 * scale), "bold")
        canvas.create_text(8 * scale, 12 * scale, text="Drag steps to reorder · checkbox enables/disables · Alt + ← / → also works", anchor="w", fill=muted, font=font)
        gap, inset = 30 * scale, 6 * scale
        cell = (width - 2 * inset - (columns - 1) * gap) / columns
        self._boxes = []
        rank = 0
        for index, step in enumerate(self.steps):
            column, row = index % columns, index // columns
            x1, x2 = inset + column * (cell + gap), inset + column * (cell + gap) + cell
            y1 = (40 + row * row_stride) * scale
            y2 = y1 + node_height * scale
            self._boxes.append((x1, y1, x2, y2))
            selected = step["method"] == self.selected
            active = step["enabled"]
            rank += int(active)
            canvas.create_rectangle(x1, y1, x2, y2, fill=panel if active else bg,
                                    outline=accent if selected else border, width=2 if selected else 1,
                                    dash=() if active else (3, 3))
            canvas.create_text(x1 + 12 * scale, y1 + 16 * scale, text=(f"{rank} · " if active else "Off · ") + FLOW_LABELS.get(step["method"], step["method"]),
                               anchor="w", fill=ink if active else muted, font=title_font, width=max(60, cell - 45 * scale))
            bx, by = x2 - 24 * scale, y1 + 10 * scale
            canvas.create_rectangle(bx, by, bx + 14 * scale, by + 14 * scale, fill=accent if active else panel, outline=accent if active else border)
            if active:
                canvas.create_text(bx + 7 * scale, by + 7 * scale, text="✓", fill=color(("#ffffff", "#152b22")), font=font)
            detail = ("Post ID → categorized tags" if self.kind == "database" else "Web hints → image model" if self.kind == "context" else
                      "Web hints + image → tags" if step["method"] == "Local Vision LLM" else "Reverse search → booru tags")
            canvas.create_text(x1 + 12 * scale, y1 + 54 * scale, text=detail, anchor="w", fill=muted, font=font, width=cell - 20 * scale)
            if self.expand_command:
                canvas.create_text(x2 - 10 * scale, y2 - 12 * scale, text="▾ Close" if self.expanded == step["method"] else "▸ Details",
                                   anchor="e", fill=accent, font=font)
            if column < columns - 1 and index + 1 < len(self.steps):
                middle = y1 + node_height * scale / 2
                canvas.create_line(x2 + 4 * scale, middle, x2 + gap - 4 * scale, middle, arrow="last", fill=muted)
            if column == columns - 1 and index + 1 < len(self.steps):
                canvas.create_text(width / 2, y2 + 16 * scale, text="↓ continue below", fill=muted, font=font)
        active_names = enabled_flow_methods(self.steps, self.methods)
        message = "Next enabled step is tried only if no usable tags were found. First success skips the rest." if active_names else "All searches are off. Images without cached/embedded tags take the unmatched outcome below."
        if self.kind == "database":
            message = ("Only linked post IDs are queried. First usable tag set wins; disabled sources are skipped." if active_names else
                       "All databases are off. No tag servers are queried; the next search method is tried.")
        elif self.kind == "context":
            message = ("First usable context wins. Empty results, exhausted keys, or errors try the next enabled API." if active_names else
                       "All context APIs are off. Local vision analyzes the image without web hints.")
        canvas.create_text(8 * scale, (bottom + 26) * scale, text=message, anchor="w", fill=muted, font=font, width=width - 16 * scale)
        tail = ("No usable tags → next search method" if self.kind == "database" else
                "Web hints → local image model; failure policy is configured below" if self.kind == "context" else
                "Tags found → exact folder rules / model shortlist → move + optional metadata")
        if self.kind == 'search':
            lane_width = (width - 30 * scale) / 2
            center_y, top = (bottom + 45) * scale, (bottom + 69) * scale
            for column, heading, detail in (
                (0, 'Tags found → folder routing', 'Supported folder → move into destination library' + ('\nThen embed selected tags' if self._outcome_metadata else '')),
                (1, 'Searches exhausted / no supported folder', self._outcome_target + '\nRecord in History · Needs attention')):
                left = 6 * scale + column * (lane_width + 18 * scale)
                middle = left + lane_width / 2
                canvas.create_line(width / 2, center_y, middle, center_y, middle, top - 3 * scale, arrow='last', fill=muted)
                canvas.create_rectangle(left, top, left + lane_width, top + 91 * scale, fill=panel, outline=border)
                canvas.create_text(left + 12 * scale, top + 18 * scale, text=heading, anchor='w', fill=accent, font=title_font, width=lane_width - 24 * scale)
                canvas.create_text(left + 12 * scale, top + 57 * scale, text=detail, anchor='w', fill=ink, font=font, width=lane_width - 24 * scale)
        else:
            canvas.create_text(8 * scale, (bottom + 60) * scale, text=tail, anchor="w", fill=accent, font=font, width=width - 16 * scale)
        if self._drag and self._drag["active"]:
            target = self._drop_index(self._drag["x"], self._drag.get("y", 80 * scale))
            x1, y1, x2, y2 = self._boxes[target]
            canvas.create_rectangle(x1 - 2, y1 - 2, x2 + 2, y2 + 2, outline=accent, width=3, dash=(5, 3))
            canvas.create_text(self._drag["x"], 30 * scale, text="Drop " + self._drag["method"], fill=accent, font=font)
        self.selection_label.configure(text="Selected: " + FLOW_LABELS.get(self.selected, self.selected))

    def _set_appearance_mode(self, mode):
        super()._set_appearance_mode(mode)
        if hasattr(self, "canvas"):
            self._schedule_draw()

    def _set_scaling(self, *args, **kwargs):
        super()._set_scaling(*args, **kwargs)
        if hasattr(self, "canvas"):
            self._schedule_draw()

    def destroy(self):
        if self._draw_job is not None:
            self.after_cancel(self._draw_job)
        super().destroy()


class SmoothScrollableFrame(ctk.CTkScrollableFrame):
    """Fit scrolling content once per frame, using the latest canvas size."""
    def __init__(self, *args, **kwargs):
        self._fit_job = None
        self._last_fitted_size = None
        super().__init__(*args, **kwargs)

    def _fit_frame_dimensions_to_canvas(self, _event=None):
        if self._fit_job is None:
            self._fit_job = self.after(16, self._apply_canvas_fit)

    def _apply_canvas_fit(self):
        self._fit_job = None
        if not self._parent_canvas.winfo_exists():
            return
        dimension = "height" if self._orientation == "horizontal" else "width"
        size = self._parent_canvas.winfo_height() if dimension == "height" else self._parent_canvas.winfo_width()
        if size > 1 and size != self._last_fitted_size:
            self._last_fitted_size = size
            self._parent_canvas.itemconfigure(self._create_window_id, **{dimension: size})

    def destroy(self):
        if self._fit_job is not None:
            self.after_cancel(self._fit_job)
            self._fit_job = None
        super().destroy()


class HistoryViewport(ctk.CTkFrame):
    """Clip and move one opaque content frame; do not scroll embedded Tk windows.

    Canvas window scrolling can leave copied child-window pixels behind on Windows.
    History uses a normal clipped frame instead, with drag updates coalesced per frame.
    """
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._fit_job = self._scroll_job = None
        self._offset = 0
        self._pending_offset = None
        self._content_height = 1
        self._sizes = {}
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        background = self._apply_appearance_mode(self.cget('fg_color'))
        self.viewport = Frame(self, bg=background, borderwidth=0, highlightthickness=0)
        self.viewport.grid(row=0, column=0, sticky='nsew')
        self.content = ctk.CTkFrame(self.viewport, fg_color=self.cget('fg_color'), corner_radius=0)
        self.content.place(x=0, y=0, relwidth=1)
        self.scrollbar = ctk.CTkScrollbar(self, command=self.yview)
        self.scrollbar.grid(row=0, column=1, sticky='ns')
        self.viewport.bind('<Configure>', lambda event: self._size_changed('viewport', event))
        self.content.bind('<Configure>', lambda event: self._size_changed('content', event))
        self._wheel_bindings = {sequence: self.winfo_toplevel().bind(sequence, self._mouse_wheel, add='+')
                                for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>')}

    def _size_changed(self, name, event):
        size = (event.width, event.height)
        if self._sizes.get(name) != size:
            self._sizes[name] = size
            self.request_layout()

    def request_layout(self):
        if self._fit_job is None:
            self._fit_job = self.after(16, self._apply_canvas_fit)

    def _apply_canvas_fit(self):
        self._fit_job = None
        self._content_height = max(1, self.content.winfo_reqheight())
        height = max(1, self.viewport.winfo_height())
        self._sizes['content'] = (self.viewport.winfo_width(), max(height, self._content_height))
        self.content.place_configure(height=self._reverse_widget_scaling(max(height, self._content_height)))
        self._apply_scroll()

    def yview(self, *args):
        height = max(1, self.viewport.winfo_height())
        total = max(height, self._content_height)
        if not args:
            return self._offset / total, min(1, (self._offset + height) / total)
        offset = self._offset if self._pending_offset is None else self._pending_offset
        if args[0] == 'moveto':
            offset = float(args[1]) * total
        elif args[0] == 'scroll':
            step = height * .9 if args[2] == 'pages' else 42 * self._get_widget_scaling()
            offset += int(args[1]) * step
        self._pending_offset = max(0, min(offset, total - height))
        if self._scroll_job is None:
            self._scroll_job = self.after(16, self._apply_scroll)

    def _apply_scroll(self):
        if self._scroll_job is not None:
            self.after_cancel(self._scroll_job)
            self._scroll_job = None
        height = max(1, self.viewport.winfo_height())
        total = max(height, self._content_height)
        offset = self._offset if self._pending_offset is None else self._pending_offset
        self._offset = max(0, min(round(offset), total - height))
        self._pending_offset = None
        self.content.place_configure(y=-self._reverse_widget_scaling(self._offset))
        self.scrollbar.set(self._offset / total, min(1, (self._offset + height) / total))

    def _mouse_wheel(self, event):
        if not self.winfo_viewable():
            return
        widget = event.widget
        while widget is not None and widget is not self:
            widget = getattr(widget, 'master', None)
        if widget is not self:
            return
        delta = getattr(event, 'delta', 0)
        direction = (-1 if delta > 0 else 1) if delta else (-1 if getattr(event, 'num', None) == 4 else 1)
        self.yview('scroll', direction * max(1, int(abs(delta) / 120)), 'units')
        return 'break'

    def _set_appearance_mode(self, mode):
        super()._set_appearance_mode(mode)
        if hasattr(self, 'viewport'):
            self.viewport.configure(bg=self._apply_appearance_mode(self.cget('fg_color')))
            self.content.configure(fg_color=self.cget('fg_color'))

    def destroy(self):
        for job in (self._fit_job, self._scroll_job):
            if job is not None:
                self.after_cancel(job)
        for sequence, binding in self._wheel_bindings.items():
            self.winfo_toplevel().unbind(sequence, binding)
        super().destroy()


class AppNavigation(ctk.CTkFrame):
    """Flat application chrome matching the approved mockup, with a small tab API."""
    def __init__(self, master):
        super().__init__(master, corner_radius=0, fg_color=("#f6f7f9", "#181b20"))
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self.header = ctk.CTkFrame(self, corner_radius=0, fg_color=("#ffffff", "#20242b"), height=68)
        self.header.grid(row=0, column=0, sticky="ew")
        self.header.grid_propagate(False)
        self.brand = ctk.CTkLabel(self.header, text="Tag-AI", image=master._ui_icon("tag", accent=True),
                                 compound="left", padx=10, font=ctk.CTkFont(family="Segoe UI", size=17, weight="bold"))
        self.brand.pack(side="left", padx=(20, 24), pady=16)
        ctk.CTkFrame(self, height=1, corner_radius=0, fg_color=("#e1e5eb", "#353c46")).grid(row=1, column=0, sticky="ew")
        self.pages, self.buttons = {}, {}
        self.selection = None

    def add(self, name):
        page = ctk.CTkFrame(self, corner_radius=0, fg_color=("#f6f7f9", "#181b20"))
        self.pages[name] = page
        button = ctk.CTkButton(self.header, text=name, width=85, height=34, corner_radius=6,
            fg_color="transparent", hover_color=("#edf0f5", "#2b323c"), text_color=("#626d7c", "#a5afbd"),
            font=ctk.CTkFont(family="Segoe UI", size=14), command=lambda: self.set(name))
        button.pack(side="left", padx=3, pady=16)
        self.buttons[name] = button
        if self.selection is None:
            self.set(name)
        return page

    def set(self, name):
        if name not in self.pages:
            raise ValueError(f"Unknown page: {name}")
        if name == self.selection:
            return
        previous_name = self.selection
        previous = self.pages.get(previous_name)
        target = self.pages[name]
        if previous is not None:
            previous.grid_forget()
        target.grid(row=2, column=0, sticky="nsew")
        if name == "History":
            self.master.refresh_history_view(force=True)
        self.master._prepare_view(target)
        target.lift()
        target.update_idletasks()
        self.selection = name
        self.master._queue_native_repaint()
        for title in (name, previous_name):
            if title is None:
                continue
            self.buttons[title].configure(fg_color=("#edf0f5", "#2b323c") if title == name else "transparent",
                text_color=("#222b36", "#e7ebf1") if title == name else ("#626d7c", "#a5afbd"))

    def get(self):
        return self.selection


class HistoryFilter(ctk.CTkFrame):
    def __init__(self, master, values, command):
        super().__init__(master, fg_color="transparent")
        self.buttons, self.selection = {}, None
        for value in values:
            button = ctk.CTkButton(self, text=value, height=34, width=110 if value != "Needs attention" else 145,
                corner_radius=6, border_width=1, border_color=("#e1e5eb", "#353c46"),
                fg_color=("#ffffff", "#20242b"), text_color=("#222b36", "#e7ebf1"),
                hover_color=("#edf0f5", "#2b323c"), font=ctk.CTkFont(family="Segoe UI", size=14),
                command=lambda value=value: (self.set(value), command(value)))
            button.pack(side="left", padx=(0, 6))
            self.buttons[value] = button

    def set(self, value):
        self.selection = value
        for label, button in self.buttons.items():
            button.configure(fg_color=("#e8f1ec", "#293e35") if label == value else ("#ffffff", "#20242b"),
                             text_color=("#256b55", "#8ac9af") if label == value else ("#222b36", "#e7ebf1"))

    def get(self):
        return self.selection


class BooruManagerApp(HistoryMixin, ProviderMixin, RoutingMixin, StorageMixin, WorkflowMixin, ctk.CTk):
    def _update_dimensions_event(self, event=None):
        # Tk includes the toplevel in each descendant's bind tags. CustomTkinter's
        # root handler otherwise queries the window dimensions again for every
        # label, canvas, and frame Configure notification during a resize.
        if event is not None and event.widget is not self:
            return
        super()._update_dimensions_event(event)
        if self.__dict__.get('_paint_ready', False):
            size = (self.winfo_width(), self.winfo_height())
            if size != self._last_paint_size:
                self._last_paint_size = size
                self._queue_native_repaint()
                if self._settled_paint_job is not None:
                    self.after_cancel(self._settled_paint_job)
                self._settled_paint_job = self.after(150, self._settled_native_repaint)

    def _queue_native_repaint(self):
        if not self.__dict__.get('_paint_ready', False):
            return
        if self._native_paint_job is None:
            self._native_paint_job = self.after(16, self._native_repaint)

    def _native_repaint(self):
        self._native_paint_job = None
        invalidate_native_window(self)

    def _settled_native_repaint(self):
        self._settled_paint_job = None
        self._queue_native_repaint()

    def _set_appearance_mode(self, mode):
        super()._set_appearance_mode(mode)
        for name in ('sort_split', 'diagnostics_split'):
            pane = self.__dict__.get(name)
            if pane is not None:
                pane.configure(bg=self._apply_appearance_mode(('#f6f7f9', '#181b20')))
        self._queue_native_repaint()

    def __init__(self):
        super().__init__()
        self._paint_ready = False
        self._native_paint_job = self._settled_paint_job = None
        self._last_paint_size = None

        # Avoid exposing the native window while the large CustomTkinter
        # widget tree is still being constructed. Showing it after idle lets
        # Tk calculate the complete layout before the first visible paint.
        self.withdraw()
        self.title("Tag-AI Booru Image Sorter")
        self.geometry("1150x960")
        self.minsize(980, 760)

        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        self._ui_thread = threading.get_ident()
        self._ui_queue = queue.SimpleQueue()
        self._widget_snapshot = {}
        self._entry_snapshot = {}
        self._metrics_pending = False
        self._log_queue = queue.Queue(maxsize=5000)
        sys.stdout = StdoutRedirector(self.log)
        sys.stderr = StdoutRedirector(self.log)

        self.is_processing = False
        self.stop_requested = False
        self.skip_requested = False
        self.file_in_progress = False
        self._preview_generation = 0
        self.target_source = ""
        self.dest_path = os.path.abspath("./organized_library")
        self.unmatched_path = ""
        self.vision_warning_shown = False
        self.folder_aliases = {}
        self.database_pipelines = {engine: legacy_database_flow() for engine in ("IQDB", "SauceNAO")}
        self.context_pipeline = [{"method": key, "enabled": False} for key in CONTEXT_METHODS]
        self._database_flow_widgets = {}
        self._context_flow_widget = None
        self._models_refresh_generation = 0

        self.history_items = []
        self._current_session_id = uuid.uuid4().hex
        self.history_page = 0
        self.history_per_page = 6
        self._history_cards = []
        self._history_visible_entries = []
        self._history_dirty = True
        self._history_render_job = None
        self._history_render_generation = 0
        self._history_page_signature = None
        self._history_revision = 0
        self._history_filter_cache = None
        self._history_image_results = queue.SimpleQueue()
        self._history_ctk_images = OrderedDict()
        self.history_thumb_cache = OrderedDict()
        self.history_thumb_cache_limit = 128
        self.history_thumb_inflight = set()
        self.history_thumb_waiters = {}
        self.history_thumb_lock = threading.Lock()
        self.history_local_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="history-local")
        self.history_thumb_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="history-thumb")

        # Robust config path supporting both compiled .exe and python script execution
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(os.path.abspath(sys.executable))
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_file = os.path.join(base_dir, "config.json")
        self.session_store = BatchSessionStore(os.path.join(base_dir, "batch_session.sqlite3"))
        self.pending_session = None
        self.history_file = os.path.join(base_dir, "operation_history.sqlite3")
        self._load_history()
        try:
            self.result_cache = PersistentResultCache(os.path.join(base_dir, "image_result_cache.sqlite3"))
        except Exception:
            self.result_cache = DisabledResultCache()

        self._build_ui()
        self._paint_ready = True
        self._queue_native_repaint()

        self.after(100, self._flush_log_queue)
        self.after(40, self._drain_history_thumbnails)
        self.on_unmatched_change(self._read_widget(self.opt_unmatched))
        self.load_settings()
        self._restore_session()
        self.update_search_options()
        self.after_idle(self.refresh_history_view)
        self.after_idle(self._show_initial_window)

    def _ui_icon(self, name, accent=False):
        """Small native line icons; no extra assets or web runtime required."""
        if not hasattr(self, "_icon_cache"):
            self._icon_cache = {}
        key = (name, accent)
        if key in self._icon_cache:
            return self._icon_cache[key]
        images = []
        for color in (("#256b55", "#8ac9af") if accent else ("#626d7c", "#a5afbd")):
            image = Image.new("RGBA", (48, 48))
            draw = ImageDraw.Draw(image)
            if name == "search":
                draw.ellipse((7, 6, 31, 30), outline=color, width=3)
                draw.line((29, 28, 41, 40), fill=color, width=3)
            elif name == "cpu":
                draw.rounded_rectangle((12, 12, 36, 36), radius=3, outline=color, width=3)
                draw.rectangle((19, 19, 29, 29), outline=color, width=2)
                for offset in (17, 25, 33):
                    for line in ((offset, 5, offset, 12), (offset, 36, offset, 43), (5, offset, 12, offset), (36, offset, 43, offset)):
                        draw.line(line, fill=color, width=2)
            elif name == "folder":
                draw.line((9, 10, 9, 37, 18, 37), fill=color, width=3)
                draw.line((9, 22, 18, 22), fill=color, width=3)
                for y in (7, 19, 33):
                    draw.rounded_rectangle((19, y, 41, y + 10), radius=2, outline=color, width=2)
            else:
                draw.line((7, 7, 26, 7, 43, 24, 24, 43, 7, 26, 7, 7), fill=color, width=3)
                draw.ellipse((13, 13, 18, 18), outline=color, width=2)
            images.append(image)
        icon = ctk.CTkImage(light_image=images[0], dark_image=images[1], size=(20, 20))
        self._icon_cache[key] = icon
        return icon

    def _prepare_view(self, view):
        """Settle an incoming view after unmapping the previous native controls.

        This runs only on navigation, not on the live-resize path. Idle work
        settles geometry without running input handlers or queued batch actions.
        """
        view.update_idletasks()
        widgets = []

        def collect(widget):
            widgets.append(widget)
            for child in widget.winfo_children():
                collect(child)

        collect(view)
        # A scrollable frame's inner viewport width normally updates on a timer.
        # Resolve it now so the new page doesn't visibly expand after being shown.
        for widget in widgets:
            if isinstance(widget, (SmoothScrollableFrame, HistoryViewport)) and widget.winfo_viewable():
                if widget._fit_job is not None:
                    widget.after_cancel(widget._fit_job)
                    widget._fit_job = None
                widget._apply_canvas_fit()
        view.update_idletasks()
        for widget in widgets:
            timer = getattr(widget, "_wrap_resize_job", None)
            if timer is not None and widget.winfo_viewable():
                self.after_cancel(timer)
                widget._wrap_resize_job = None
                width = widget._pending_wrap_width
                if abs(float(widget.cget("wraplength")) - width) >= 8:
                    widget.configure(wraplength=width)
        view.update_idletasks()

    def _ui_label(self, parent, text, heading=False):
        title = heading and not getattr(parent, "_has_title", False)
        if heading:
            if not title:
                ctk.CTkFrame(parent, height=1, corner_radius=0, fg_color=("#e1e5eb", "#353c46")).pack(fill="x", pady=(22, 18))
            parent._has_title = True
        label = ctk.CTkLabel(parent, text=text, anchor="w", justify="left",
                             font=ctk.CTkFont(family="Segoe UI", size=22 if title else 15 if heading else 14,
                                             weight="bold" if heading else "normal"),
                             text_color=("#222b36", "#e7ebf1") if heading else ("#626d7c", "#a5afbd"), wraplength=640)
        label.pack(fill="x", pady=(0, 4 if title else 6))
        return label

    def _ui_button(self, parent, text, command, **kwargs):
        return ctk.CTkButton(parent, text=text, command=command, height=36, corner_radius=6, border_width=1,
                            border_color=("#e1e5eb", "#353c46"), font=ctk.CTkFont(family="Segoe UI", size=14),
                            fg_color=("#ffffff", "#20242b"), text_color=("#222b36", "#e7ebf1"),
                            hover_color=("#edf0f5", "#2b323c"), **kwargs)

    def _ui_select(self, parent, **kwargs):
        defaults = dict(height=36, corner_radius=6, border_width=1, border_color=("#e1e5eb", "#353c46"), state="readonly", fg_color=("#ffffff", "#20242b"),
                        button_color=("#ffffff", "#20242b"), button_hover_color=("#edf0f5", "#2b323c"),
                        text_color=("#222b36", "#e7ebf1"), dropdown_fg_color=("#ffffff", "#20242b"),
                        dropdown_text_color=("#222b36", "#e7ebf1"), dropdown_hover_color=("#edf0f5", "#2b323c"),
                        font=ctk.CTkFont(family="Segoe UI", size=14), dropdown_font=ctk.CTkFont(family="Segoe UI", size=14))
        defaults.update(kwargs)
        control = ctk.CTkComboBox(parent, **defaults)
        if defaults.get("values"):
            control.set(defaults["values"][0])
        return control

    def _ui_entry(self, parent, **kwargs):
        defaults = dict(height=36, corner_radius=6, border_width=1, border_color=("#e1e5eb", "#353c46"),
                        fg_color=("#ffffff", "#20242b"), text_color=("#222b36", "#e7ebf1"),
                        font=ctk.CTkFont(family="Segoe UI", size=14))
        defaults.update(kwargs)
        return ctk.CTkEntry(parent, **defaults)

    def _ui_action_bar(self, parent, on_reflow=None):
        return WrapActionBar(parent, on_reflow=on_reflow, fg_color='transparent')

    def _ui_note(self, parent, text):
        frame = ctk.CTkFrame(parent, corner_radius=6, fg_color=("#edf0f5", "#2b323c"))
        frame.pack(fill="x", pady=(12, 5))
        ctk.CTkLabel(frame, text=text, anchor="w", justify="left", wraplength=600,
                     text_color=("#626d7c", "#a5afbd"), font=ctk.CTkFont(family="Segoe UI", size=12)).pack(fill="x", padx=15, pady=13)
        return frame

    def _schedule_wrap_update(self, label, width):
        """Reflow text after resizing settles, rather than changing row heights mid-drag."""
        label._pending_wrap_width = max(180, int(width) // 8 * 8)
        label._wrap_resize_deadline = time.monotonic() + 0.12
        if getattr(label, "_wrap_resize_job", None) is not None:
            return

        def apply():
            label._wrap_resize_job = None
            if not label.winfo_exists():
                return
            remaining = label._wrap_resize_deadline - time.monotonic()
            if remaining > 0:
                label._wrap_resize_job = self.after(max(1, int(remaining * 1000) + 1), apply)
                return
            width = label._pending_wrap_width
            if abs(float(label.cget("wraplength")) - width) >= 8:
                label.configure(wraplength=width)

        # One pending timer follows the latest width. The canvas can resize
        # normally without also triggering repeated text/row-height layout passes.
        label._wrap_resize_job = self.after(120, apply)

    def _watch_wrap_width(self, container, label, padding):
        last_width = None

        def resized(_event):
            nonlocal last_width
            width = container.winfo_width()
            if width == last_width:
                return  # Moving a window or changing row height needs no reflow.
            last_width = width
            # Tk Configure sizes are physical pixels; wraplength uses logical
            # widget units. Convert before applying the content padding.
            logical_width = container._reverse_widget_scaling(width)
            self._schedule_wrap_update(label, logical_width - padding)

        def destroyed(_event):
            timer = getattr(label, "_wrap_resize_job", None)
            if timer is not None:
                self.after_cancel(timer)
                label._wrap_resize_job = None

        container.bind("<Configure>", resized, add="+")
        container.bind("<Destroy>", destroyed, add="+")

    def _ui_switch(self, parent, name, text, command=None, selected=False, packed=True):
        widget = ctk.CTkCheckBox(parent, text=text, command=command, checkbox_width=17, checkbox_height=17,
                              corner_radius=3, border_width=1, border_color=("#85909c", "#a5afbd"),
                              fg_color=("#256b55", "#8ac9af"), hover_color=("#205e4b", "#77b89d"),
                              checkmark_color=("#ffffff", "#152b22"), text_color=("#222b36", "#e7ebf1"),
                              font=ctk.CTkFont(family="Segoe UI", size=14))
        if packed:
            widget.pack(anchor="w", pady=6)
        if selected:
            widget.select()
        setattr(self, name, widget)
        return widget

    def _disclosure(self, parent, text):
        """Keep optional credentials and maintenance actions out of the main form."""
        outer = ctk.CTkFrame(parent, fg_color="transparent")
        outer.pack(fill="x", pady=5)
        body = ctk.CTkFrame(outer, fg_color="transparent")
        def toggle():
            opened = bool(body.winfo_manager())
            if opened:
                body.pack_forget()
            else:
                body.pack(fill="x", padx=8, pady=8)
            button.configure(text=("▸ " if opened else "▾ ") + text)
        button = self._ui_button(outer, "▸ " + text, toggle, anchor="w")
        button.configure(fg_color="transparent", border_width=0, font=ctk.CTkFont(family="Segoe UI", size=12),
                         text_color=("#626d7c", "#a5afbd"), height=24)
        button.pack(anchor="w")
        body._disclosure_wrapper = outer
        return body

    def _build_ui(self):
        self.geometry("1100x820")
        self.minsize(960, 720)
        self.configure(fg_color=("#f6f7f9", "#181b20"))
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.tabview = AppNavigation(self)
        self.tabview.grid(row=0, column=0, sticky="nsew")
        self.tab_monitor = self.tabview.add("Sort")
        self.tab_history = self.tabview.add("History")
        self.tab_settings = self.tabview.add("Settings")

        # Sort: everyday controls and a quiet monitor; activity is optional.
        self.sort_layout = ctk.CTkFrame(self.tab_monitor, fg_color='transparent')
        self.sort_layout.pack(fill='both', expand=True, padx=18, pady=(10, 12))
        self.sort_layout.grid_columnconfigure(0, weight=1)
        self.sort_layout.grid_rowconfigure(1, weight=1)
        toolbar = ctk.CTkFrame(self.sort_layout, fg_color='transparent')
        toolbar.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 8))
        self._show_logs = self._show_api = False
        self.btn_api_usage = self._ui_button(toolbar, 'Show API usage', self.toggle_api_usage)
        self.btn_api_usage.pack(side='right')
        self.btn_activity = self._ui_button(toolbar, 'Show logs', self.toggle_activity)
        self.btn_activity.pack(side='right', padx=(0, 8))
        self.lbl_panel_resize = ctk.CTkLabel(toolbar, text='Drag dividers to resize panels', font=ctk.CTkFont(family='Segoe UI', size=12), text_color=('#626d7c', '#a5afbd'))
        pane_options = dict(bg=self._apply_appearance_mode(('#f6f7f9', '#181b20')), sashwidth=8, borderwidth=0, opaqueresize=True, showhandle=True, handlesize=8)
        self.sort_split = PanedWindow(self.sort_layout, orient='horizontal', sashcursor='sb_h_double_arrow', **pane_options)
        self.sort_split.grid(row=1, column=0, columnspan=2, sticky='nsew')
        self.sort_host = ctk.CTkFrame(self.sort_split, fg_color='transparent')
        self.sort_split.add(self.sort_host, minsize=320, stretch='always')
        sort = SmoothScrollableFrame(self.sort_host, fg_color="transparent")
        sort.pack(fill='both', expand=True)
        self.diagnostics_frame = ctk.CTkFrame(self.sort_split, fg_color='transparent')
        self.diagnostics_split = PanedWindow(self.diagnostics_frame, orient='vertical', sashcursor='sb_v_double_arrow', **pane_options)
        self.diagnostics_split.pack(fill='both', expand=True)
        self.api_usage_host = ctk.CTkFrame(self.diagnostics_split, fg_color='transparent')
        self._sort_sash_ratio, self._diagnostics_sash_ratio = .46, .35
        self._main_sash_job = self._diagnostics_sash_job = None
        for pane in (self.sort_split, self.diagnostics_split):
            pane.bind('<ButtonRelease-1>', lambda event: self._capture_sashes(), add='+')
        for frame in (self.sort_host, self.diagnostics_frame, self.api_usage_host):
            frame.bind('<Configure>', lambda event: self._queue_native_repaint(), add='+')
        intro = ctk.CTkFrame(sort, fg_color="transparent")
        intro.pack(fill="x", pady=(0, 12))
        intro_copy = ctk.CTkFrame(intro, fg_color="transparent")
        intro_copy.pack(side="top", fill="x")
        self._ui_label(intro_copy, "Sort your library", True)
        self._ui_label(intro_copy, "Choose your folders. Tag-AI handles the rest in the background.")
        self.opt_exec_mode = self._ui_select(intro, width=245,
            values=["Automated Full Folder Batch", "Single File Review Mode"], command=self.on_exec_mode_change)
        self.opt_exec_mode.pack(side="bottom", anchor='w', pady=(8, 0))
        paths = ctk.CTkFrame(sort, fg_color="transparent")
        paths.pack(fill="x", pady=(4, 10))
        paths.grid_columnconfigure(0, weight=1)
        for column, title, label_name, button_name, command, value in (
            (0, "Source", "lbl_source_val", "btn_select_source", self.browse_source, "No source selected"),
            (1, "Destination library", "lbl_dest_val", "btn_select_dest", self.browse_dest, self.dest_path)):
            box = ctk.CTkFrame(paths, fg_color="transparent")
            box.grid(row=column, column=0, sticky="nsew", pady=(0, 10))
            ctk.CTkLabel(box, text=title, anchor="w", font=ctk.CTkFont(family="Segoe UI", size=14)).pack(fill="x", pady=(0, 6))
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(fill="x")
            button = self._ui_button(row, "Browse…", command, width=90)
            button.pack(side="right", padx=(8, 0))
            field = ctk.CTkFrame(row, fg_color=("#ffffff", "#20242b"), border_width=1,
                                 border_color=("#e1e5eb", "#353c46"), corner_radius=6)
            field.pack(side="left", fill="x", expand=True)
            label = ctk.CTkLabel(field, text=value, anchor="w", justify="left", wraplength=280, height=36,
                                 font=ctk.CTkFont(family="Segoe UI", size=13))
            label.pack(fill="x", padx=11, pady=2)
            self._watch_wrap_width(field, label, padding=24)
            setattr(self, label_name, label)
            setattr(self, button_name, button)
        ctk.CTkFrame(sort, height=1, corner_radius=0, fg_color=("#e1e5eb", "#353c46")).pack(fill="x", pady=(8, 16))
        run_row = ctk.CTkFrame(sort, fg_color="transparent")
        run_row.pack(fill="x")
        self.lbl_run_status = ctk.CTkLabel(run_row, text="Ready to sort", anchor="w", wraplength=220, justify='left', font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"))
        self.lbl_run_status.pack(side="left")
        self.btn_start = ctk.CTkButton(run_row, text="Start sorting", height=38, corner_radius=6, command=self.start_thread,
                                      font=ctk.CTkFont(family="Segoe UI", size=14), fg_color=("#256b55", "#8ac9af"), text_color=("#ffffff", "#152b22"), hover_color=("#205e4b", "#77b89d"))
        self.btn_start.pack(side="right")
        self.session_controls = ctk.CTkFrame(sort, fg_color="transparent")
        self.lbl_session = self._ui_label(self.session_controls, "")
        self.btn_restart = self._ui_button(self.session_controls, "Start over", self.restart_session)
        self.btn_resume = self._ui_button(self.session_controls, "Resume", self.resume_session)
        self.btn_restart.pack(side="right")
        self.btn_resume.pack(side="right", padx=(0, 8))
        self.lbl_session.pack(side="top", fill="x", before=self.btn_restart)
        self.add_tooltip(self.btn_resume, "Continue the saved queue using current settings. The interrupted image is retried; completed images are skipped.")
        self.add_tooltip(self.btn_restart, "Rescan the source using current settings. Images already moved stay in the destination; cached results can be reused.")
        self.lbl_sort_progress = self._ui_label(sort, "No batch running")
        self.progress_bar = ctk.CTkProgressBar(sort, height=6, border_width=0, corner_radius=4, fg_color=("#edf0f5", "#2b323c"), progress_color=("#256b55", "#8ac9af"))
        self.progress_bar.pack(fill="x", pady=(5, 12))
        self.progress_bar.set(0)
        self.lbl_files_moved = self._ui_label(sort, "Files moved this session: 0")
        self.preview_container = ctk.CTkFrame(sort, fg_color="transparent")
        self.preview_container.pack(fill="x", pady=8)
        self.preview_container.grid_columnconfigure((0, 1), weight=1, uniform="previews")
        for column, title, name in ((0, "Your image", "lbl_source_thumb"), (1, "Database match", "lbl_match_thumb")):
            ctk.CTkLabel(self.preview_container, text=title, font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold")).grid(row=0, column=column, padx=15)
            label = ctk.CTkLabel(self.preview_container, text="No image" if column == 0 else "No match", width=130, height=155,
                                 fg_color=("#edf0f5", "#2b323c"), corner_radius=6)
            label.grid(row=1, column=column, padx=(0, 9) if column == 0 else (9, 0), pady=(4, 6), sticky="ew")
            setattr(self, name, label)
        self.lbl_details = ctk.CTkLabel(self.preview_container, text="Status: Ready", anchor="w", justify="left", wraplength=400)
        self.lbl_details.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.bottom_frame = ctk.CTkFrame(sort, fg_color="transparent")
        self.bottom_frame.pack(fill="x", pady=5)
        self.btn_skip = self._ui_button(self.bottom_frame, "Skip current image", self.skip_current_file_action)
        self.btn_skip.pack(side="left", padx=(0, 10))
        self.btn_skip.configure(state="disabled")
        self.btn_cancel = self._ui_button(self.bottom_frame, "Stop after current request", self.cancel_processing)
        self.btn_cancel.pack(side="left")
        self.btn_cancel.configure(state="disabled")
        self.api_usage_frame = SmoothScrollableFrame(self.api_usage_host, fg_color=("#ffffff", "#20242b"))
        self.api_usage_frame.pack(fill='both', expand=True)
        self._ui_label(self.api_usage_frame, 'API usage', True)
        self._ui_label(self.api_usage_frame, 'Local counts are for this app run; server balances are shown when reported.')
        self.lbl_key_usage = ctk.CTkLabel(self.api_usage_frame, text='', anchor='w', justify='left', wraplength=370, font=ctk.CTkFont(family='Segoe UI', size=12))
        self.lbl_key_usage.pack(fill='x', padx=8, pady=12)
        self.counter_frame = self._disclosure(self.api_usage_frame, 'Service totals and rate limits')
        self.counter_frame.grid_columnconfigure(0, weight=1)
        for index, (name, title) in enumerate((("iqdb", "IQDB"), ("saucenao", "SauceNAO"), ("danbooru", "Danbooru"),
            ("gelbooru", "Gelbooru"), ("yandere", "Yande.re"), ("konachan", "Konachan"), ("serpapi", "SerpApi"), ("gvision", "Google Vision"))):
            label = ctk.CTkLabel(self.counter_frame, text=title + ": 0", anchor="w", wraplength=410, font=ctk.CTkFont(family="Segoe UI", size=12))
            label.grid(row=index, column=0, sticky="w", padx=8, pady=2)
            setattr(self, "lbl_" + name + "_metric", label)
        self.activity_frame = ctk.CTkFrame(self.diagnostics_split, fg_color='transparent')
        self.activity_frame.bind('<Configure>', lambda event: self._queue_native_repaint(), add='+')
        self.console_tabview = ctk.CTkTabview(self.activity_frame, height=340)
        self.console_tabview.pack(fill="both", expand=True)
        for title, name in (("Summary", "txt_console_simp"), ("Detailed log", "txt_console_full")):
            page = self.console_tabview.add(title)
            textbox = ctk.CTkTextbox(page, wrap="word", height=145, font=ctk.CTkFont(family="Consolas", size=12))
            textbox.pack(fill="both", expand=True)
            setattr(self, name, textbox)
        self.after(1000, self._poll_usage)

        # Settings: one scrollable form at a time; Save remains visible.
        self.tab_settings.grid_columnconfigure(1, weight=1)
        self.tab_settings.grid_rowconfigure(0, weight=1)
        rail = ctk.CTkFrame(self.tab_settings, width=190, corner_radius=0, fg_color=("#ffffff", "#20242b"))
        rail.grid(row=0, column=0, rowspan=2, sticky="ns")
        rail.pack_propagate(False)
        self.settings_rail = rail
        ctk.CTkLabel(rail, text="SETTINGS", anchor="w", text_color=("#626d7c", "#a5afbd"), font=ctk.CTkFont(family="Segoe UI", size=12)).pack(fill="x", padx=26, pady=(26, 14))
        self.settings_panes, self.settings_buttons = {}, {}
        for name in ("Search", "Local model", "Folder routing", "Image tags"):
            button = self._ui_button(rail, name, lambda name=name: self.show_settings_section(name), width=160, anchor="w")
            button.configure(border_width=0, image=self._ui_icon({"Search": "search", "Local model": "cpu", "Folder routing": "folder", "Image tags": "tag"}[name]), compound="left")
            button.pack(fill="x", padx=14, pady=(0, 5))
            self.settings_buttons[name] = button
            pane = SmoothScrollableFrame(self.tab_settings, fg_color="transparent")
            pane.grid(row=0, column=1, sticky="nsew", padx=(26, 22), pady=(24, 8))
            self.settings_panes[name] = pane
        search, model, routing, tags = (self.settings_panes[name] for name in ("Search", "Local model", "Folder routing", "Image tags"))
        self._ui_label(search, "Search", True)
        self._ui_label(search, "Choose how Tag-AI identifies your images.")
        self._ui_label(search, "Search order", True)
        self.search_flow = SearchFlowChart(search, command=self._on_search_flow_changed, expand_command=self.expand_search_step)
        self.search_flow.pack(fill="x", pady=(4, 12))
        self.flow_details_host = ctk.CTkFrame(self.search_flow, fg_color="transparent")
        self.flow_details_title = ctk.CTkLabel(self.flow_details_host, text="", anchor="w", font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"))
        self.flow_details_title.pack(fill="x", pady=(8, 4))
        self.flow_details_note = ctk.CTkLabel(self.flow_details_host, text="", anchor="w", justify="left", wraplength=640,
                                            font=ctk.CTkFont(family="Segoe UI", size=12), text_color=("#626d7c", "#a5afbd"))
        self.flow_details_note.pack(fill="x", pady=(0, 8))
        self.flow_details_pages = {method: ctk.CTkFrame(self.flow_details_host, fg_color="transparent") for method in PIPELINE_METHODS}
        self.database_flow_anchors = {}
        for method in ("IQDB", "SauceNAO"):
            anchor = ctk.CTkFrame(self.flow_details_pages[method], fg_color="transparent")
            anchor.pack(fill="x")
            self.database_flow_anchors[method] = anchor
        vision_page = self.flow_details_pages["Local Vision LLM"]
        self.vision_flow_anchor = ctk.CTkFrame(vision_page, fg_color="transparent")
        self.vision_flow_anchor.pack(fill="x")
        self.primary_search_row = ctk.CTkFrame(search, fg_color="transparent")

        primary_copy = ctk.CTkFrame(self.primary_search_row, fg_color="transparent")
        primary_copy.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(primary_copy, text="Primary search engine", anchor="w", font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold")).pack(fill="x")
        ctk.CTkLabel(primary_copy, text="The first service used for each image.", anchor="w", text_color=("#626d7c", "#a5afbd"), font=ctk.CTkFont(family="Segoe UI", size=12)).pack(fill="x")
        self.opt_engine = self._ui_select(self.primary_search_row, values=["IQDB", "SauceNAO", "Local Vision LLM"], command=self.on_engine_change, width=190)
        self.opt_engine.pack(side="right", padx=(12, 0))
        # Retain legacy backing controls for old configs and integrations; the
        # flow is the visible editor and the authoritative execution plan.
        self._ui_switch(search, "switch_engine_fallback", "Try SauceNAO next", self.update_search_options, True)
        self.switch_engine_fallback.pack_forget()
        self._ui_switch(search, "switch_vision_fallback", "Try local vision after reverse search")
        self.switch_vision_fallback.pack_forget()
        self._sync_search_flow_from_legacy(force=True)
        self._ui_note(search, "Uncertain images stay in their source folder. Review them in History whenever you’re ready.")
        for name in ("serpapi_entries", "gvision_entries", "db_user_entries", "db_key_entries", "gb_user_entries", "gb_key_entries", "skey_entries"):
            setattr(self, name, [])
        self.sauce_credentials = self._disclosure(self.flow_details_pages["SauceNAO"], "SauceNAO API keys (optional)")
        self.skey_list_frame = self.sauce_credentials
        self.add_skey_field()
        self._ui_label(vision_page, "Web context API keys", True)
        self._ui_label(vision_page, "Enable providers in the inner flow. Add credentials below for enabled APIs.")
        self._ui_switch(vision_page, "switch_serpapi", "Google Lens via SerpApi", self.update_search_options)
        self.switch_serpapi.pack_forget()
        self.serpapi_options = ctk.CTkFrame(vision_page, fg_color="transparent")
        self.lbl_serpapi_info = self._ui_label(self.serpapi_options, "API keys · requests use your provider quota.")
        self.serpapi_list_frame = ctk.CTkFrame(self.serpapi_options, fg_color="transparent")
        self.serpapi_list_frame.pack(fill="x")
        self.add_serpapi_field()
        self._ui_switch(vision_page, "switch_gvision", "Google Cloud Vision", self.update_search_options)
        self.switch_gvision.pack_forget()
        self.gvision_options = ctk.CTkFrame(vision_page, fg_color="transparent")
        self.lbl_gvision_info = self._ui_label(self.gvision_options, "Cloud Vision API keys")
        self.gvision_list_frame = ctk.CTkFrame(self.gvision_options, fg_color="transparent")
        self.gvision_list_frame.pack(fill="x")
        self.add_gvision_field()
        vision_behavior = self._disclosure(vision_page, "When context is unavailable")
        self._ui_switch(vision_behavior, "switch_skip_on_web_fail", "Leave image if web context cannot be found", self._refresh_flow_context)
        self._ui_label(vision_behavior, "Applies when local vision uses one of the web context services.")
        self._ui_label(model, "Local model", True)
        self._ui_label(model, "Connect LM Studio or an OpenAI-compatible local server.")
        self._ui_label(model, "Server address", True)
        connection_row = ctk.CTkFrame(model, fg_color="transparent")
        connection_row.pack(fill="x", pady=6)
        self.ent_lm = self._ui_entry(connection_row, height=36)
        self.ent_lm.insert(0, "http://localhost:1234/v1")
        self.ent_lm.pack(side="left", fill="x", expand=True)
        self.btn_test_model = self._ui_button(connection_row, "Refresh models", self.test_model_connection)
        self.btn_test_model.pack(side="right", padx=(8, 0))
        self.lbl_model_status = self._ui_label(model, "Not checked")
        self._ui_label(model, "Model", True)
        self.opt_model = self._ui_select(model, values=[AUTO_MODEL], width=420)
        self.opt_model.pack(fill="x", pady=6)
        self._ui_label(model, "Refresh models to query the server. This selection is used for routing and vision; choose a vision-capable model when analyzing images.")
        self._ui_label(model, "How the model is used", True)
        self._ui_label(model, "Folder routing: selects an existing folder when exact series and character tags do not match.")
        self._ui_note(model, "If the model cannot confidently choose a supported folder, the image follows your unmatched setting. This is a cautious routing decision, not a connection error.")
        self._ui_label(model, "Vision tagging: describes images when local vision is enabled in Search.")
        self._ui_button(model, "Configure vision in Search →", lambda: self.show_settings_section("Search")).pack(anchor="w", pady=12)
        self._ui_label(model, "You can browse History while the model responds. Settings changes apply to the next batch.")
        self._ui_label(routing, "Folder routing", True)
        self._ui_label(routing, "Match images to your existing library structure.")
        self._ui_label(routing, 'Matching mode', True)
        self.opt_matching_mode = self._ui_select(routing, values=list(MATCHING_MODES), command=self.on_matching_mode_change, width=220)
        self.opt_matching_mode.pack(anchor='w', pady=(6, 8))
        self.lbl_matching_mode = self._ui_label(routing, '')
        self.on_matching_mode_change('Exact')
        self._ui_switch(routing, "switch_scan_subfolders", "Include character subfolders")
        self._ui_label(routing, "Looks one level inside each series folder. Source scanning is unchanged.")
        self._ui_label(routing, "How a destination is chosen", True)
        self._ui_note(routing, "Reliable exact tags and aliases are preferred in both modes. Otherwise Exact uses an evidence-scoped shortlist; Flexible lets the local model assess semantic relationships to existing folders. The unmatched setting applies when no destination is chosen.")
        self._ui_label(routing, "If searches find no match, or no folder fits", True)
        self.opt_unmatched = self._ui_select(routing, values=list(UNMATCHED_ACTIONS), command=self.on_unmatched_change, width=300)
        self.opt_unmatched.pack(anchor="w", pady=(6, 8))
        self._ui_note(routing, "Both outcomes are recorded in History under Needs attention. Processing continues without a review prompt. Changes apply to the next batch.")
        self._ui_label(routing, "Unmatched folder", True)
        self.btn_select_unmatched = self._ui_button(routing, "Choose folder", self.browse_unmatched)
        self.btn_select_unmatched.pack(anchor="w", pady=6)
        self.lbl_unmatched_val = self._ui_label(routing, "Default: Library / Unmatched")
        self._ui_note(routing, "Used by Move to Unmatched Folder and the To Unmatched button in History. The default is Unmatched inside your destination library.")
        self._ui_label(tags, "Image tags", True)
        self._ui_label(tags, "Control how tags are read and saved.")
        self._ui_label(tags, "Read existing tags", True)
        self._ui_switch(tags, "switch_use_embedded_tags", "Use embedded tags before searching", self.on_use_embedded_tags_toggle)
        self.switch_skip_no_embedded = self._ui_switch(tags, "switch_skip_no_embedded", "Process only images that already have tags", self._refresh_flow_context)
        self.switch_skip_no_embedded.pack_forget()
        self._ui_label(tags, "Save tags to images", True)
        self.meta_container = ctk.CTkFrame(tags, fg_color="transparent")
        self.meta_container.pack(fill="x")
        self._ui_switch(self.meta_container, "switch_embed_metadata", "Embed tags after sorting", self.on_embed_metadata_toggle)
        self.frame_tag_categories = ctk.CTkFrame(self.meta_container, fg_color="transparent")
        self.frame_tag_categories.grid_columnconfigure((0, 1), weight=1)
        for index, (name, text, selected) in enumerate((("artist", "Artists", True), ("series", "Series", True), ("character", "Characters", True), ("general", "General tags", True), ("meta", "Technical tags", False))):
            category = self._ui_switch(self.frame_tag_categories, "switch_meta_" + name, text, selected=selected, packed=False)
            category.pack_forget()
            category.grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 12), pady=6)
        self._ui_label(tags, "Preserves image pixels and unrelated metadata.")
        self._ui_label(tags, "Tag database", True)
        self.opt_tags = self._ui_select(tags, width=265, values=["Auto (Highest Confidence)", "Yande.re", "Danbooru", "Gelbooru", "Konachan", "Zerochan", "Pixiv"])
        self._ui_label(tags, "Database order and toggles are inside the IQDB / SauceNAO boxes in Search.")
        self._ui_button(tags, "Edit database flow →", lambda: self._open_database_flow()).pack(anchor="w", pady=6)
        for title, user_name, key_name, add_user, add_key in (
            ("Danbooru credentials (optional)", "db_user_list_frame", "db_key_list_frame", self.add_db_user_field, self.add_db_key_field),
            ("Gelbooru credentials (optional)", "gb_user_list_frame", "gb_key_list_frame", self.add_gb_user_field, self.add_gb_key_field)):
            body = self._disclosure(tags, title)
            for attr, add in ((user_name, add_user), (key_name, add_key)):
                frame = ctk.CTkFrame(body, fg_color="transparent")
                frame.pack(fill="x", pady=4)
                setattr(self, attr, frame)
                add()
        alias_editor = self._disclosure(routing, "Folder aliases (advanced)")
        self._ui_label(alias_editor, 'JSON example: {"Frieren": {"series": ["sousou_no_frieren"]}, "Frieren/Fern": {"character": ["fern_(sousou_no_frieren)"]}}')
        self.txt_folder_aliases = ctk.CTkTextbox(alias_editor, height=150, wrap="word", font=ctk.CTkFont(family="Consolas", size=12))
        self.txt_folder_aliases.pack(fill="x", pady=8)
        self.txt_folder_aliases.insert("1.0", "{}")
        self._ui_label(alias_editor, "Keys must name existing folders relative to the library, using /. Aliases never create folders.")
        maintenance = self._disclosure(routing, "Import, export and maintenance")
        self.btn_export = self._ui_button(maintenance, "Export settings", self.export_settings)
        self.btn_export.pack(anchor="w", pady=4)
        self.btn_import = self._ui_button(maintenance, "Import settings", self.import_settings)
        self.btn_import.pack(anchor="w", pady=4)
        self.btn_clear_cache = self._ui_button(maintenance, "Clear cache and history…", self.clear_app_cache)
        self.btn_clear_cache.pack(anchor="w", pady=4)
        savebar = ctk.CTkFrame(self.tab_settings, fg_color="transparent")
        savebar.grid(row=1, column=1, sticky="ew", padx=30, pady=(0, 20))
        ctk.CTkFrame(savebar, height=1, corner_radius=0, fg_color=("#e1e5eb", "#353c46")).pack(fill="x", pady=(0, 16))
        self.lbl_settings_saved = ctk.CTkLabel(savebar, text="Changes apply to the next batch", anchor="w", text_color=("#626d7c", "#a5afbd"), font=ctk.CTkFont(family="Segoe UI", size=12))
        self.lbl_settings_saved.pack(side="left")
        ctk.CTkButton(savebar, text="Save settings", command=self.save_settings_action, fg_color=("#256b55", "#8ac9af"), text_color=("#ffffff", "#152b22"), hover_color=("#205e4b", "#77b89d")).pack(side="right")

        # History retains two independent, clickable image previews per result.
        self.tab_history.grid_columnconfigure(0, weight=1)
        self.tab_history.grid_rowconfigure(2, weight=1)
        self.history_top_frame = ctk.CTkFrame(self.tab_history, fg_color="transparent")
        self.history_top_frame.grid(row=0, column=0, sticky="ew", padx=18, pady=(15, 4))
        self._ui_label(self.history_top_frame, "History", True)
        self._ui_label(self.history_top_frame, "Compare your image with the database match. Click either preview to open it.")
        history_scope_row = ctk.CTkFrame(self.history_top_frame, fg_color='transparent')
        history_scope_row.pack(fill='x', pady=(8, 2))
        self.history_scope = self._ui_select(history_scope_row, values=['Overall history', 'This session'], command=self.on_history_filter, width=180)
        self.history_scope.pack(side='left', padx=(0, 16))
        self.switch_history_attempts = self._ui_switch(history_scope_row, 'switch_history_attempts', 'Show repeated attempts', self.on_history_filter, packed=False)
        self.switch_history_attempts.pack(side='left')
        self.lbl_history_summary = self._ui_label(self.history_top_frame, 'Latest result per file; repeated attempts remain available.')
        controls = ctk.CTkFrame(self.tab_history, fg_color="transparent")
        controls.grid(row=1, column=0, sticky="ew", padx=18, pady=8)
        self.history_filter = HistoryFilter(controls, values=["All results", "Needs attention", "Moved"], command=self.on_history_filter)
        self.history_filter.set("All results")
        self.history_filter.pack(side="left")
        self.history_pagination_frame = ctk.CTkFrame(controls, fg_color="transparent")
        self.history_pagination_frame.pack(side="right")
        self.btn_prev_page = self._ui_button(self.history_pagination_frame, "‹ Prev", lambda: self.change_history_page(-1), width=65)
        self.btn_prev_page.pack(side="left", padx=3)
        self.lbl_page_indicator = ctk.CTkLabel(self.history_pagination_frame, text="Page 1/1")
        self.lbl_page_indicator.pack(side="left", padx=8)
        self.btn_next_page = self._ui_button(self.history_pagination_frame, "Next ›", lambda: self.change_history_page(1), width=65)
        self.btn_next_page.pack(side="left", padx=3)
        self.history_scroll = HistoryViewport(self.tab_history, fg_color=("#f6f7f9", "#181b20"), corner_radius=0)
        self.history_scroll.grid(row=2, column=0, sticky="nsew", padx=12, pady=5)
        self._history_empty_label = ctk.CTkLabel(self.history_scroll.content, text="No results in this view yet.", anchor="w")
        footer = ctk.CTkFrame(self, corner_radius=0, fg_color=("#ffffff", "#20242b"))
        footer.grid(row=1, column=0, sticky="ew")
        ctk.CTkFrame(footer, height=1, corner_radius=0, fg_color=("#e1e5eb", "#353c46")).pack(fill="x")
        ctk.CTkLabel(footer, text="●", text_color=("#256b55", "#8ac9af"), font=ctk.CTkFont(family="Segoe UI", size=10)).pack(side="left", padx=(23, 7), pady=8)
        self.lbl_history_counter = ctk.CTkLabel(footer, text="Ready · local library", anchor="w", font=ctk.CTkFont(family="Segoe UI", size=12))
        self.lbl_history_counter.pack(side="left")
        self.lbl_files_moved_hist = ctk.CTkLabel(footer, text="Files moved this session: 0", font=ctk.CTkFont(family="Segoe UI", size=12))
        self.lbl_files_moved_hist.pack(side="right", padx=23, pady=8)
        self.show_settings_section("Search")
        self.update_search_options()

    def show_settings_section(self, name):
        if name not in self.settings_panes:
            raise ValueError(f"Unknown settings section: {name}")
        previous_name = getattr(self, "_settings_selection", None)
        if name == previous_name:
            return
        if previous_name is not None:
            self.settings_panes[previous_name].grid_forget()
        target = self.settings_panes[name]
        target.grid(row=0, column=1, sticky="nsew", padx=(26, 22), pady=(24, 8))
        self._prepare_view(target)
        target.lift()
        target.update_idletasks()
        for title, pane in self.settings_panes.items():
            if title != name:
                # Forget the geometry so DPI updates cannot remap hidden sections.
                pane.grid_forget()
            if previous_name is None or title in (name, previous_name):
                self.settings_buttons[title].configure(fg_color=("#e8f1ec", "#293e35") if title == name else "transparent", text_color=("#256b55", "#8ac9af") if title == name else ("#626d7c", "#a5afbd"), image=self._ui_icon({"Search": "search", "Local model": "cpu", "Folder routing": "folder", "Image tags": "tag"}[title], accent=title == name))
        self._settings_selection = name
        self._queue_native_repaint()

    def toggle_activity(self):
        self._show_logs = not self._show_logs
        self._layout_diagnostics()

    def toggle_api_usage(self):
        self._show_api = not self._show_api
        self._layout_diagnostics()
        if self._show_api:
            self.update_metrics_display()

    def _layout_diagnostics(self):
        self._capture_sashes()
        shown = self._show_api or self._show_logs
        if shown:
            self.lbl_panel_resize.pack(side='left')
        else:
            self.lbl_panel_resize.pack_forget()
        main_present = str(self.diagnostics_frame) in tuple(map(str, self.sort_split.panes()))
        if shown and not main_present:
            self.sort_split.add(self.diagnostics_frame, minsize=300, stretch='always')
            self._schedule_sash_restore('main')
        elif not shown and main_present:
            self.sort_split.forget(self.diagnostics_frame)
        for visible, frame in ((self._show_api, self.api_usage_host), (self._show_logs, self.activity_frame)):
            present = str(frame) in tuple(map(str, self.diagnostics_split.panes()))
            if visible and not present:
                options = {'before': self.activity_frame} if frame is self.api_usage_host and str(self.activity_frame) in tuple(map(str, self.diagnostics_split.panes())) else {}
                self.diagnostics_split.add(frame, minsize=100, stretch='always', **options)
                self._schedule_sash_restore('diagnostics')
            elif not visible and present:
                self.diagnostics_split.forget(frame)
        self.btn_activity.configure(text='Hide logs' if self._show_logs else 'Show logs')
        self.btn_api_usage.configure(text='Hide API usage' if self._show_api else 'Show API usage')
        self._queue_native_repaint()

    def _capture_sashes(self):
        for pane, axis, attribute, job in ((self.sort_split, 0, '_sort_sash_ratio', '_main_sash_job'), (self.diagnostics_split, 1, '_diagnostics_sash_ratio', '_diagnostics_sash_job')):
            size = pane.winfo_width() if axis == 0 else pane.winfo_height()
            if getattr(self, job) is None and len(pane.panes()) == 2 and size > 1:
                setattr(self, attribute, max(.1, min(.9, pane.sash_coord(0)[axis] / size)))
        self._queue_native_repaint()

    def _schedule_sash_restore(self, name):
        attribute = '_main_sash_job' if name == 'main' else '_diagnostics_sash_job'
        callback = self._restore_main_sash if name == 'main' else self._restore_diagnostics_sash
        if getattr(self, attribute) is None:
            setattr(self, attribute, self.after(16, callback))

    def _restore_main_sash(self):
        if len(self.sort_split.panes()) == 2:
            self.sort_split.update_idletasks()
            size = self.sort_split.winfo_width()
            position = max(320, min(size - 308, int(size * self._sort_sash_ratio)))
            self.sort_split.paneconfigure(self.sort_host, width=position)
            self.sort_split.paneconfigure(self.diagnostics_frame, width=max(300, size - position - 8))
            self.sort_split.update_idletasks()
            self.sort_split.sash_place(0, position, 0)
        self._main_sash_job = None

    def _restore_diagnostics_sash(self):
        if len(self.diagnostics_split.panes()) == 2:
            self.diagnostics_split.update_idletasks()
            size = self.diagnostics_split.winfo_height()
            position = max(100, min(size - 108, int(size * self._diagnostics_sash_ratio)))
            self.diagnostics_split.paneconfigure(self.api_usage_host, height=position)
            self.diagnostics_split.paneconfigure(self.activity_frame, height=max(100, size - position - 8))
            self.diagnostics_split.update_idletasks()
            self.diagnostics_split.sash_place(0, 0, position)
        self._diagnostics_sash_job = None

    def _poll_usage(self):
        if self._show_api and self.tabview.get() == 'Sort':
            self.update_metrics_display()
        self.after(1000, self._poll_usage)

    def _update_key_usage(self):
        if not self._show_api:
            return
        lines = ['PER-KEY USAGE · requests observed this app run']
        for title, service, entry_name in (
            ('SerpApi', 'serpapi', 'serpapi_entries'), ('Cloud Vision', 'gvision', 'gvision_entries'),
            ('SauceNAO', 'saucenao', 'skey_entries'), ('Danbooru', 'danbooru', 'db_key_entries'),
            ('Gelbooru', 'gelbooru', 'gb_key_entries')):
            keys = list(dict.fromkeys(ent.get().strip() for _, ent in getattr(self, entry_name) if ent.get().strip()))
            lines.append('\n' + title)
            if not keys:
                lines.append('  No keys configured' + (' · public access may be used' if service in ('danbooru', 'gelbooru') else ''))
            for index, key in enumerate(keys, 1):
                row = tracker.key_snapshot(service, key)
                parts = [f"Key {index}: {row.get('requests', 0)} requests"]
                if service == 'serpapi':
                    parts += [f"{row.get('uploads', 0)} uploads", f"{row.get('checks', 0)} balance checks"]
                if row.get('errors'):
                    parts.append(f"{row['errors']} failed requests")
                if row.get('last_status') is not None:
                    parts.append(f"last status: {row['last_status']}")
                lines.append('  ' + ' · '.join(parts))
                quota = row.get('quota', {})
                if quota and time.time() - quota.get('observed_at', 0) < 86400:
                    if service == 'serpapi' and quota.get('total_searches_left') is not None:
                        lines.append(f"  Server balance: {quota['total_searches_left']} searches left (last reported)")
                    elif service == 'saucenao':
                        remaining = [f"{quota[name]} {label} left" for name, label in (('short_remaining', 'short-window'), ('long_remaining', 'daily')) if quota.get(name) is not None]
                        lines.append('  Server quota: ' + (', '.join(remaining) or 'not reported'))
                    else:
                        lines.append('  Server quota: not reported')
                else:
                    lines.append('  Server quota: unknown · local requests are not billed usage')
        text = '\n'.join(lines)
        if text != getattr(self, '_last_key_usage_text', None):
            self.lbl_key_usage.configure(text=text)
            self._last_key_usage_text = text

    def _open_database_flow(self):
        self.tabview.set("Settings")
        self.show_settings_section("Search")
        self.search_flow.selected = "IQDB"
        self.search_flow.expanded = "IQDB"
        self.expand_search_step("IQDB")
        self.search_flow._schedule_draw()

    def expand_search_step(self, method):
        for page in self.flow_details_pages.values():
            page.pack_forget()
        if method is None:
            self.flow_details_host.pack_forget()
            return
        if method not in PIPELINE_METHODS:
            raise ValueError("Unknown search step")
        self.flow_details_host.pack(after=self.search_flow.controls, fill="x", pady=(8, 12))
        page = self.flow_details_pages[method]
        page.pack(fill="x")
        if method in ("IQDB", "SauceNAO") and method not in self._database_flow_widgets:
            chart = SearchFlowChart(self.database_flow_anchors[method],
                command=lambda flow, engine=method: self._on_database_flow_changed(engine, flow),
                methods=DATABASE_METHODS, kind="database")
            chart.set_config(self.database_pipelines[method])
            chart.pack(fill="x")
            self._database_flow_widgets[method] = chart
        elif method == "Local Vision LLM" and self._context_flow_widget is None:
            chart = SearchFlowChart(self.vision_flow_anchor, command=self._on_context_flow_changed,
                                     methods=CONTEXT_METHODS, kind="context")
            chart.set_config(self.context_pipeline)
            chart.pack(fill="x")
            self._context_flow_widget = chart
        self._refresh_nested_flow_note(method)
        self._prepare_view(page)

    def _refresh_nested_flow_note(self, method=None):
        method = method or self.search_flow.expanded
        if not method:
            return
        self.flow_details_title.configure(text=method + (" · tag database flow" if method != "Local Vision LLM" else " · context API flow"))
        active = method in enabled_search_methods(self.search_flow.get_config())
        note = ("Only post IDs returned by this search engine can be queried. Source priority comes first; similarity breaks ties."
                if method != "Local Vision LLM" else
                "Context APIs supply hints to the image model. The first usable context stops this flow; each provider tries its backup keys first.")
        if not active:
            note += " Parent step is off; these settings are saved but will not run until it is enabled."
        self.flow_details_note.configure(text=note)

    def _on_database_flow_changed(self, engine, flow):
        self.database_pipelines[engine] = normalize_named_flow(flow, DATABASE_METHODS)
        self.lbl_settings_saved.configure(text="Database flow changed · applies to the next batch")

    def _apply_context_flags(self):
        active = enabled_flow_methods(self.context_pipeline, CONTEXT_METHODS)
        for provider, widget in (("serpapi", self.switch_serpapi), ("gvision", self.switch_gvision)):
            widget.select() if provider in active else widget.deselect()
        self._last_context_signature = (bool(self.switch_serpapi.get()), bool(self.switch_gvision.get()))

    def _on_context_flow_changed(self, flow):
        self.context_pipeline = normalize_named_flow(flow, CONTEXT_METHODS)
        self._apply_context_flags()
        self.update_search_options()
        self.lbl_settings_saved.configure(text="Context flow changed · applies to the next batch")

    def _refresh_inner_flow_widgets(self):
        for engine, chart in self._database_flow_widgets.items():
            chart.set_config(self.database_pipelines[engine])
        if self._context_flow_widget is not None:
            self._context_flow_widget.set_config(self.context_pipeline)

    def _sync_inner_flows_from_legacy(self):
        preferred = self.opt_tags.get()
        changed = False
        if preferred != getattr(self, "_last_database_preference", None):
            self.database_pipelines = {engine: legacy_database_flow(preferred) for engine in ("IQDB", "SauceNAO")}
            self._last_database_preference = preferred
            changed = True
        flags = (bool(self.switch_serpapi.get()), bool(self.switch_gvision.get()))
        if flags != getattr(self, "_last_context_signature", None):
            enabled = dict(zip(CONTEXT_METHODS, flags))
            self.context_pipeline = [{"method": step["method"], "enabled": enabled[step["method"]]} for step in self.context_pipeline]
            self._last_context_signature = flags
            changed = True
        if changed:
            self._refresh_inner_flow_widgets()

    def _legacy_search_signature(self):
        return (self.opt_engine.get(), bool(self.switch_engine_fallback.get()), bool(self.switch_vision_fallback.get()))

    def _sync_search_flow_from_legacy(self, force=False):
        signature = self._legacy_search_signature()
        if force or signature != getattr(self, "_last_legacy_search_signature", None):
            self.search_flow.set_config(legacy_search_pipeline(*signature))
            self._last_legacy_search_signature = signature

    def _on_search_flow_changed(self, pipeline):
        active = enabled_search_methods(pipeline)
        if active:
            self.opt_engine.set(active[0])
        self.switch_engine_fallback.select() if "IQDB" in active and "SauceNAO" in active else self.switch_engine_fallback.deselect()
        self.switch_vision_fallback.select() if "Local Vision LLM" in active and active[0] != "Local Vision LLM" else self.switch_vision_fallback.deselect()
        self._last_legacy_search_signature = self._legacy_search_signature()
        self.update_search_options()
        if hasattr(self, "lbl_settings_saved"):
            self.lbl_settings_saved.configure(text="Flow changed · save settings for the next batch")

    def _refresh_flow_context(self):
        if not hasattr(self, "search_flow") or not hasattr(self, "switch_use_embedded_tags"):
            return
        if self.switch_use_embedded_tags.get():
            start = ("Cached tags first → embedded tags. Missing embedded tags: leave in source; searches are skipped."
                     if self.switch_skip_no_embedded.get() else
                     "Cached tags first → embedded tags → search flow only if tags are missing.")
        else:
            start = "Cached tags first; otherwise use the enabled search steps above."
        context = [FLOW_LABELS[key] for key in enabled_flow_methods(self.context_pipeline, CONTEXT_METHODS)]
        hints = " → ".join(context) if context else "No web context service enabled"
        text = start + "\nInside local vision: " + hints + "."
        if len(context) > 1:
            text += " Next web service runs only if context is missing."
        if self.switch_skip_on_web_fail.get() and context:
            text += " If all web context fails, local vision tagging is skipped."
        move_unmatched = self.opt_unmatched.get() == UNMATCHED_ACTIONS[1]
        text += "\nNo search match or supported folder → " + ("move to Unmatched." if move_unmatched else "leave in source.") + " Results appear in History."
        text += '\nFolder matching: ' + self.opt_matching_mode.get() + '.'
        self.search_flow.set_outcome(move_unmatched, self.switch_embed_metadata.get())
        preflight = "Image → cached tags"
        if self.switch_use_embedded_tags.get():
            preflight += " → embedded tags"
            preflight += " → route, or leave if tags missing" if self.switch_skip_no_embedded.get() else " → search below if missing"
        else:
            preflight += " → search below if missing"
        self.search_flow.set_context(text, preflight=preflight)

    def update_search_options(self):
        self._sync_search_flow_from_legacy()
        self._sync_inner_flows_from_legacy()
        options = {"serpapi": self.serpapi_options, "gvision": self.gvision_options}
        for frame in options.values():
            frame.pack_forget()
        for provider in reversed(enabled_flow_methods(self.context_pipeline, CONTEXT_METHODS)):
            options[provider].pack(after=self.vision_flow_anchor, fill="x", padx=12, pady=5)
        self._refresh_nested_flow_note()
        self._refresh_flow_context()

    def on_history_filter(self, _choice=None):
        self.history_page = 0
        self.refresh_history_view()

    def save_settings_action(self):
        if self.save_settings():
            self.lbl_settings_saved.configure(text="Settings saved · applies to the next batch")

    def test_model_connection(self):
        self.refresh_local_models()

    def refresh_local_models(self):
        url = self.ent_lm.get().strip()
        self._models_refresh_generation += 1
        generation = self._models_refresh_generation
        self.btn_test_model.configure(state="disabled")
        self.lbl_model_status.configure(text="Querying available models…")
        def check():
            try:
                with providers.local_model_client(url, timeout=10, max_retries=0) as client:
                    ids = available_model_ids(client)
                error = None
            except Exception as exc:
                ids, error = [], str(exc)
            def finish():
                if generation != self._models_refresh_generation:
                    return
                self.btn_test_model.configure(state="normal")
                if self.ent_lm.get().strip() != url:
                    self.lbl_model_status.configure(text="Server address changed; refresh models again.")
                    return
                if error:
                    self.lbl_model_status.configure(text=f"Could not query models: {error}")
                    return
                selected = self.opt_model.get()
                choices = [AUTO_MODEL] + ids
                missing = selected != AUTO_MODEL and selected not in ids
                if missing:
                    choices.append(selected)  # Keep the saved choice; never silently substitute.
                self.opt_model.configure(values=choices)
                self.opt_model.set(selected)
                self.lbl_model_status.configure(text=(f"{len(ids)} model(s) available" if ids else "Server connected; no models available")
                    + (" · selected model is unavailable" if missing else ""))
            self.after(0, finish)
        threading.Thread(target=check, daemon=True).start()

    def after(self, ms, func=None, *args):
        if getattr(self, "_ui_thread", threading.get_ident()) != threading.get_ident():
            if func is not None:
                self._ui_queue.put((ms, func, args))
            return None
        return super().after(ms, func, *args)

    def _read_widget(self, widget):
        if threading.get_ident() == self._ui_thread:
            return widget.get()
        return self._widget_snapshot[id(widget)]

    def _entries(self, name):
        if threading.get_ident() == self._ui_thread:
            return getattr(self, name)
        return self._entry_snapshot[name]

    def _snapshot_widgets(self):
        def visit(widget):
            if isinstance(widget, (ctk.CTkEntry, ctk.CTkSwitch, ctk.CTkOptionMenu, ctk.CTkCheckBox, ctk.CTkComboBox)):
                self._widget_snapshot[id(widget)] = widget.get()
            for child in widget.winfo_children():
                visit(child)
        self._widget_snapshot = {}
        visit(self)
        self._entry_snapshot = {
            name: list(getattr(self, name)) for name in (
                "skey_entries", "serpapi_entries", "gvision_entries", "db_user_entries",
                "db_key_entries", "gb_user_entries", "gb_key_entries")}

    def add_tooltip(self, widget, text):
        state = {"after_id": None, "window": None}

        def hide_tooltip(_event=None):
            if state["after_id"] is not None:
                try:
                    self.after_cancel(state["after_id"])
                except Exception:
                    pass
                state["after_id"] = None
            if state["window"] is not None:
                try:
                    state["window"].destroy()
                except Exception:
                    pass
                state["window"] = None

        def show_tooltip():
            state["after_id"] = None
            if not widget.winfo_exists():
                return
            tip = ctk.CTkToplevel(self)
            tip.withdraw()
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            label = ctk.CTkLabel(
                tip, text=text, justify="left", wraplength=280,
                corner_radius=6,
                fg_color=("gray15", "gray90"), text_color=("white", "black")
            )
            label.pack(padx=9, pady=6)
            tip.update_idletasks()
            x = widget.winfo_rootx() + 12
            y = widget.winfo_rooty() + widget.winfo_height() + 5
            screen_w = tip.winfo_screenwidth()
            screen_h = tip.winfo_screenheight()
            x = min(x, max(0, screen_w - tip.winfo_width() - 8))
            y = min(y, max(0, screen_h - tip.winfo_height() - 8))
            tip.geometry(f"+{x}+{y}")
            tip.deiconify()
            state["window"] = tip

        widget.bind("<Enter>", lambda _event: state.update(after_id=self.after(500, show_tooltip)), add="+")
        widget.bind("<Leave>", hide_tooltip, add="+")
        widget.bind("<ButtonPress>", hide_tooltip, add="+")

    def _show_initial_window(self):
        if not self.winfo_exists():
            return
        self.update_idletasks()
        self.deiconify()

    def add_serpapi_field(self, val=""):
        row = ctk.CTkFrame(self.serpapi_list_frame, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ent = self._ui_entry(row, show="•", placeholder_text="SerpApi Key (Optional)")
        ent.pack(side="left", fill="x", expand=True, padx=(0, 2))
        if val: ent.insert(0, val)

        btn_add = ctk.CTkButton(row, text="+", width=28, height=28, fg_color=("#ffffff", "#20242b"), text_color=("#26313e", "#e7ebf1"), hover_color=("#dce3eb", "#3b4654"), command=lambda: self.add_serpapi_field())
        btn_add.pack(side="left", padx=2)
        btn_rem = ctk.CTkButton(row, text="-", width=28, height=28, fg_color=("#ffffff", "#20242b"), text_color=("#26313e", "#e7ebf1"), hover_color=("#dce3eb", "#3b4654"), command=lambda: self.remove_key_row(row, self.serpapi_entries, ent))
        btn_rem.pack(side="left")
        self.serpapi_entries.append((row, ent))

    def add_gvision_field(self, val=""):
        row = ctk.CTkFrame(self.gvision_list_frame, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ent = self._ui_entry(row, show="•", placeholder_text="Google Cloud Vision Key (Optional)")
        ent.pack(side="left", fill="x", expand=True, padx=(0, 2))
        if val: ent.insert(0, val)

        btn_add = ctk.CTkButton(row, text="+", width=28, height=28, fg_color=("#ffffff", "#20242b"), text_color=("#26313e", "#e7ebf1"), hover_color=("#dce3eb", "#3b4654"), command=lambda: self.add_gvision_field())
        btn_add.pack(side="left", padx=2)
        btn_rem = ctk.CTkButton(row, text="-", width=28, height=28, fg_color=("#ffffff", "#20242b"), text_color=("#26313e", "#e7ebf1"), hover_color=("#dce3eb", "#3b4654"), command=lambda: self.remove_key_row(row, self.gvision_entries, ent))
        btn_rem.pack(side="left")
        self.gvision_entries.append((row, ent))

    def add_db_user_field(self, val=""):
        row = ctk.CTkFrame(self.db_user_list_frame, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ent = self._ui_entry(row, placeholder_text="Danbooru User ID (Optional)")
        ent.pack(side="left", fill="x", expand=True, padx=(0, 2))
        if val: ent.insert(0, val)

        btn_add = ctk.CTkButton(row, text="+", width=28, height=28, fg_color=("#ffffff", "#20242b"), text_color=("#26313e", "#e7ebf1"), hover_color=("#dce3eb", "#3b4654"), command=lambda: self.add_db_user_field())
        btn_add.pack(side="left", padx=2)
        btn_rem = ctk.CTkButton(row, text="-", width=28, height=28, fg_color=("#ffffff", "#20242b"), text_color=("#26313e", "#e7ebf1"), hover_color=("#dce3eb", "#3b4654"), command=lambda: self.remove_key_row(row, self.db_user_entries, ent))
        btn_rem.pack(side="left")
        self.db_user_entries.append((row, ent))

    def add_db_key_field(self, val=""):
        row = ctk.CTkFrame(self.db_key_list_frame, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ent = self._ui_entry(row, show="•", placeholder_text="Danbooru API Key (Optional)")
        ent.pack(side="left", fill="x", expand=True, padx=(0, 2))
        if val: ent.insert(0, val)

        btn_add = ctk.CTkButton(row, text="+", width=28, height=28, fg_color=("#ffffff", "#20242b"), text_color=("#26313e", "#e7ebf1"), hover_color=("#dce3eb", "#3b4654"), command=lambda: self.add_db_key_field())
        btn_add.pack(side="left", padx=2)
        btn_rem = ctk.CTkButton(row, text="-", width=28, height=28, fg_color=("#ffffff", "#20242b"), text_color=("#26313e", "#e7ebf1"), hover_color=("#dce3eb", "#3b4654"), command=lambda: self.remove_key_row(row, self.db_key_entries, ent))
        btn_rem.pack(side="left")
        self.db_key_entries.append((row, ent))

    def add_gb_user_field(self, val=""):
        row = ctk.CTkFrame(self.gb_user_list_frame, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ent = self._ui_entry(row, placeholder_text="Gelbooru User ID (Optional)")
        ent.pack(side="left", fill="x", expand=True, padx=(0, 2))
        if val: ent.insert(0, val)

        btn_add = ctk.CTkButton(row, text="+", width=28, height=28, fg_color=("#ffffff", "#20242b"), text_color=("#26313e", "#e7ebf1"), hover_color=("#dce3eb", "#3b4654"), command=lambda: self.add_gb_user_field())
        btn_add.pack(side="left", padx=2)
        btn_rem = ctk.CTkButton(row, text="-", width=28, height=28, fg_color=("#ffffff", "#20242b"), text_color=("#26313e", "#e7ebf1"), hover_color=("#dce3eb", "#3b4654"), command=lambda: self.remove_key_row(row, self.gb_user_entries, ent))
        btn_rem.pack(side="left")
        self.gb_user_entries.append((row, ent))

    def add_gb_key_field(self, val=""):
        row = ctk.CTkFrame(self.gb_key_list_frame, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ent = self._ui_entry(row, show="•", placeholder_text="Gelbooru API Key (Optional)")
        ent.pack(side="left", fill="x", expand=True, padx=(0, 2))
        if val: ent.insert(0, val)

        btn_add = ctk.CTkButton(row, text="+", width=28, height=28, fg_color=("#ffffff", "#20242b"), text_color=("#26313e", "#e7ebf1"), hover_color=("#dce3eb", "#3b4654"), command=lambda: self.add_gb_key_field())
        btn_add.pack(side="left", padx=2)
        btn_rem = ctk.CTkButton(row, text="-", width=28, height=28, fg_color=("#ffffff", "#20242b"), text_color=("#26313e", "#e7ebf1"), hover_color=("#dce3eb", "#3b4654"), command=lambda: self.remove_key_row(row, self.gb_key_entries, ent))
        btn_rem.pack(side="left")
        self.gb_key_entries.append((row, ent))

    def add_skey_field(self, val=""):
        row = ctk.CTkFrame(self.skey_list_frame, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ent = self._ui_entry(row, show="•", placeholder_text="SauceNAO API Key (Optional)")
        ent.pack(side="left", fill="x", expand=True, padx=(0, 2))
        if val: ent.insert(0, val)

        btn_add = ctk.CTkButton(row, text="+", width=28, height=28, fg_color=("#ffffff", "#20242b"), text_color=("#26313e", "#e7ebf1"), hover_color=("#dce3eb", "#3b4654"), command=lambda: self.add_skey_field())
        btn_add.pack(side="left", padx=2)
        btn_rem = ctk.CTkButton(row, text="-", width=28, height=28, fg_color=("#ffffff", "#20242b"), text_color=("#26313e", "#e7ebf1"), hover_color=("#dce3eb", "#3b4654"), command=lambda: self.remove_key_row(row, self.skey_entries, ent))
        btn_rem.pack(side="left")
        self.skey_entries.append((row, ent))

    def remove_key_row(self, row_frame, entry_list, ent_widget):
        if len(entry_list) > 1:
            entry_list[:] = [item for item in entry_list if item[1] != ent_widget]
            row_frame.destroy()
        else:
            ent_widget.delete(0, 'end')

    def on_engine_change(self, choice):
        self.update_search_options()
        if choice == "Local Vision LLM":
            self.vision_warning_shown = True
            self.lbl_model_status.configure(text="Vision tagging needs a vision-capable model. Configure your local server here.")

    def on_embed_metadata_toggle(self):
        if self._read_widget(self.switch_embed_metadata) == 1:
            self.frame_tag_categories.pack(pady=(5, 2), padx=0, fill="x")
        else:
            self.frame_tag_categories.pack_forget()
        self._refresh_flow_context()

    def on_use_embedded_tags_toggle(self):
        if self._read_widget(self.switch_use_embedded_tags) == 1:
            self.switch_skip_no_embedded.pack(after=self.switch_use_embedded_tags, pady=(2, 6), padx=20, anchor="w")
        else:
            self.switch_skip_no_embedded.pack_forget()
        self._refresh_flow_context()

    def update_progress_display(self, current, total):
        def _update():
            self.lbl_history_counter.configure(text=f"Processing {current}/{total} images")
            self.lbl_sort_progress.configure(text=f"Processing image {current} of {total}")
            self.progress_bar.set(current / total if total else 0)
            self.title(f"Tag-AI {current}/{total}")
        self.after(0, _update)

    def clear_app_cache(self):
        if messagebox.askyesno("Confirm Clear Cache", "Are you sure you want to clear the hash cache, API counters, console logs, and history?\n\nYour folders, toggles, and API keys will be preserved."):
            tracker.hash_cache.clear()
            self.result_cache.clear()
            tracker.serpapi_key_cooldowns.clear()
            tracker.serpapi_account_cache.clear()
            tracker.serpapi_account_info = None
            with tracker.key_usage_lock:
                tracker.key_usage.clear()
            tracker.files_moved = 0
            tracker.matched_files_moved = 0
            tracker.unmatched_files_moved = 0

            tracker.iqdb_limiter.window_start = time.time()
            tracker.iqdb_limiter.window_calls = 0
            tracker.danbooru_limiter.window_start = time.time()
            tracker.danbooru_limiter.window_calls = 0
            for l in tracker.saucenao_limiter.limiters:
                l["start"] = time.time()
                l["calls"] = 0
            tracker.gelbooru_limiter.last_call_time = 0
            tracker.yandere_limiter.last_call_time = 0
            tracker.konachan_limiter.last_call_time = 0
            tracker.zerochan_limiter.last_call_time = 0
            tracker.pixiv_limiter.last_call_time = 0

            for entry in self.history_items:
                self._persist_history(entry, remove=True)
            self.history_items.clear()
            self._reindex_history_paths()
            self.history_page = 0
            self.refresh_history_view()
            self.update_progress_display(0, 0)

            self.txt_console_simp.delete("0.0", "end")
            self.txt_console_simp.insert("0.0", "Cache, history, and counters cleared. System ready.\n")
            self.txt_console_full.delete("0.0", "end")
            self.txt_console_full.insert("0.0", "Cache, history, and counters cleared. System ready.\n")

            self.update_metrics_display()
            self.save_settings()
            self.log("App cache, history, logs, and session counters cleared successfully.")

    def on_closing(self):
        if self.is_processing:
            self.stop_requested = True
            self.btn_cancel.configure(text="Closing after active request...")
            self.after(200, self.on_closing)
            return
        self.save_settings()
        self.history_local_executor.shutdown(wait=False, cancel_futures=True)
        self.history_thumb_executor.shutdown(wait=False, cancel_futures=True)
        try:
            self.result_cache.close()
        except Exception:
            pass
        self.destroy()

    def on_exec_mode_change(self, choice):
        if "Folder" in choice:
            self.btn_select_source.configure(text="Browse…")
        else:
            self.btn_select_source.configure(text="Browse…")

    def on_unmatched_change(self, choice):
        self.btn_select_unmatched.configure(state="normal")
        self._refresh_flow_context()
        self.update_history_card_buttons()

    def on_matching_mode_change(self, choice):
        self.lbl_matching_mode.configure(text=(
            'Flexible: model knowledge can connect tags to broader categories without matching folder names. More interpretive; review results in History.'
            if choice == 'Flexible' else 'Exact: use matching series/character names or your configured aliases; unrelated folders are excluded.'))
        self._refresh_flow_context()
        if hasattr(self, 'lbl_settings_saved'):
            self.lbl_settings_saved.configure(text='Matching mode changed · applies to the next batch')

    def update_metrics_display(self):
        if self._metrics_pending:
            return
        self._metrics_pending = True
        def _update():
            self._metrics_pending = False
            self.lbl_iqdb_metric.configure(text=f"IQDB: {tracker.iqdb_calls} (Limit: {tracker.IQDB_LIMIT})")
            sauce_text = f"SauceNAO: {tracker.saucenao_status} (local throttle)"
            self.lbl_saucenao_metric.configure(text=sauce_text)
            self.lbl_danbooru_metric.configure(text=f"Danbooru: {tracker.danbooru_calls} (Limit: {tracker.DANBOORU_LIMIT})")
            self.lbl_gelbooru_metric.configure(text=f"Gelbooru: {tracker.gelbooru_calls} (Limit: {tracker.GELBOORU_LIMIT})")
            self.lbl_yandere_metric.configure(text=f"Yande.re: {tracker.yandere_calls} (Limit: {tracker.YANDERE_LIMIT})")
            self.lbl_konachan_metric.configure(text=f"Konachan: {tracker.konachan_calls} (Limit: {tracker.KONACHAN_LIMIT})")
            serpapi_text = f"SerpApi: {tracker.serpapi_calls} completed searches (saved total)"
            self.lbl_serpapi_metric.configure(text=serpapi_text)
            self.lbl_gvision_metric.configure(text=f"Cloud Vision: {tracker.gvision_calls} context hits (saved total)")
            self.lbl_files_moved.configure(text=f"Files Moved This Session: {tracker.files_moved}")

            self.lbl_files_moved_hist.configure(text=f"Files moved this session: {tracker.files_moved}")
            self._update_key_usage()
        self.after(0, _update)

    def browse_source(self):
        mode = self._read_widget(self.opt_exec_mode)
        if "Folder" in mode:
            folder = filedialog.askdirectory(title="Select Folder Containing Images")
            if folder:
                self.target_source = folder
                self.lbl_source_val.configure(text=folder)
                self.log(f"Source folder selected: {folder}")
        else:
            file_path = filedialog.askopenfilename(title="Select Image File", filetypes=[("Image Files", "*.jpg *.jpeg *.png *.webp")])
            if file_path:
                self.target_source = file_path
                self.lbl_source_val.configure(text=file_path)
                self.log(f"Individual file selected: {file_path}")

    def browse_dest(self):
        folder = filedialog.askdirectory(title="Select Organized Destination Library")
        if folder:
            self.dest_path = folder
            self.lbl_dest_val.configure(text=folder)
            self.log(f"Destination library updated to: {folder}")

    def browse_unmatched(self):
        folder = filedialog.askdirectory(title="Select Target Folder for Unmatched Images")
        if folder:
            self.unmatched_path = folder
            self.lbl_unmatched_val.configure(text=folder)
            self.log(f"Unmatched destination updated to: {folder}")

    def log(self, message, full_only=False):
        try:
            self._log_queue.put_nowait((str(message), bool(full_only)))
        except queue.Full:
            pass  # Bound memory when a provider produces excessive diagnostic output.

    def _flush_log_queue(self):
        if not self.winfo_exists():
            return

        for _ in range(50):
            try:
                delay, callback, args = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            super().after(delay, callback, *args)

        simplified_lines = []
        full_lines = []
        while len(full_lines) < 100:
            try:
                message, full_only = self._log_queue.get_nowait()
            except queue.Empty:
                break
            full_lines.append(message)
            if not full_only:
                simplified_lines.append(message)

        def append_batch(widget, lines):
            if not lines:
                return
            widget.insert("end", "\n".join(lines) + "\n")
            line_count = int(widget.index("end-1c").split(".")[0])
            max_lines = 250
            if line_count > max_lines:
                widget.delete("1.0", f"{line_count - max_lines}.0")
            widget.see("end")

        append_batch(self.txt_console_simp, simplified_lines)
        append_batch(self.txt_console_full, full_lines)
        self.after(200, self._flush_log_queue)

    def load_local_thumbnail(self, file_path):
        try:
            with Image.open(file_path) as pil_img:
                pil_img = pil_img.resize((110, 110))
                thumbnail = pil_img.copy()
                def _apply():
                    ctk_img = ctk.CTkImage(light_image=thumbnail, dark_image=thumbnail, size=(110, 110))
                    self.lbl_source_thumb.configure(image=ctk_img, text="")
                    self.lbl_source_thumb.image = ctk_img
                self.after(0, _apply)
        except Exception:
            self.after(0, lambda: self.lbl_source_thumb.configure(text="[Load Error]", image=""))

    def update_match_thumbnail(self, url):
        if not url:
            return
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            return
        generation = self._preview_generation
        def download():
            if generation != self._preview_generation:
                return
            try:
                thumbnail = self._download_history_thumbnail(url, (110, 110))
            except Exception:
                thumbnail = None
            def apply():
                if generation != self._preview_generation:
                    return
                if thumbnail is None:
                    self.lbl_match_thumb.configure(text="[Fetch Error]", image="")
                    return
                image = ctk.CTkImage(light_image=thumbnail, dark_image=thumbnail, size=thumbnail.size)
                self.lbl_match_thumb.configure(image=image, text="")
                self.lbl_match_thumb.image = image
            self.after(0, apply)
        self.history_thumb_executor.submit(download)

