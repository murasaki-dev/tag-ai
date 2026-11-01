"""History persistence, review actions, and asynchronous thumbnails."""

from PIL import Image, ImageOps
from io import BytesIO
from tkinter import messagebox
import customtkinter as ctk
import json
import os
import platform
import queue
import sqlite3
import subprocess
import uuid
import weakref
import webbrowser
import time
from storage import safe_move, write_image_tags
from providers import get_http_session, tracker

class HistoryMixin:
    @staticmethod
    def _history_reason(entry):
        reason = entry.get('reason', '')
        if reason.startswith('Model abstained'):
            return 'Model could not confidently choose a supported folder'
        return reason

    @staticmethod
    def _history_status(entry):
        moved = entry['original_path'] != entry['current_path']
        if entry.get('is_unmatched'):
            return 'Needs attention · moved to Unmatched' if moved else 'Needs attention · left in source'
        return 'Sorted into' if moved else 'At source'

    def get_filtered_history_items(self):
        selection = self.history_filter.get()
        scope = self.history_scope.get()
        attempts = bool(self.switch_history_attempts.get())
        key = (selection, scope, attempts, self._current_session_id, len(self.history_items), self._history_revision)
        if self._history_filter_cache and self._history_filter_cache[0] == key:
            return self._history_filter_cache[1]
        items = [entry for entry in self.history_items if scope != 'This session' or entry.get('session_id') == self._current_session_id]
        total_attempts = len(items)
        if not attempts:
            latest = {}
            for entry in items:
                latest[self._history_path_key(entry['current_path'])] = entry
            items = list(latest.values())
        counts = {'All results': len(items), 'Needs attention': sum(bool(e.get('is_unmatched')) for e in items),
                  'Moved': sum(not e.get('is_unmatched', False) and e['original_path'] != e['current_path'] for e in items)}
        for name, count in counts.items():
            self.history_filter.buttons[name].configure(text=f'{name} ({count})')
        self.lbl_history_summary.configure(text=f"{len(items)} {'attempts' if attempts else 'files'} · {total_attempts} recorded attempts · Moved shows successful sorts")
        if selection == "Needs attention":
            items = [entry for entry in items if entry.get("is_unmatched", False)]
        elif selection == "Moved":
            items = [entry for entry in items if not entry.get('is_unmatched', False) and entry["original_path"] != entry["current_path"]]
        self._history_filter_cache = (key, items)
        return items

    def change_history_page(self, delta):
        filtered = self.get_filtered_history_items()
        max_pages = max(1, (len(filtered) + self.history_per_page - 1) // self.history_per_page)

        new_page = self.history_page + delta
        if 0 <= new_page < max_pages:
            self.history_page = new_page
            self.refresh_history_view()

    def refresh_history_view(self, force=False):
        self._history_dirty = True
        if not force and self.tabview.get() != "History":
            return  # Batches do not build hundreds of hidden Tk widgets.
        filtered = self.get_filtered_history_items()
        max_pages = max(1, (len(filtered) + self.history_per_page - 1) // self.history_per_page)
        self.history_page = min(self.history_page, max_pages - 1)
        self.lbl_page_indicator.configure(text=f"Page {self.history_page + 1}/{max_pages}")
        self.btn_prev_page.configure(state="normal" if self.history_page else "disabled")
        self.btn_next_page.configure(state="normal" if self.history_page + 1 < max_pages else "disabled")
        start = self.history_page * self.history_per_page
        page_items = filtered[start:start + self.history_per_page]
        signature = (self.history_filter.get(), self.history_page, tuple(
            (entry.get("id", id(entry)), entry["current_path"], entry.get("reason", ""),
             entry.get("is_unmatched"), entry.get("match_url", ""), entry.get("match_thumb_url", "")) for entry in page_items))
        if signature == self._history_page_signature:
            self._history_dirty = False
            return
        if self._history_render_job is not None:
            self.after_cancel(self._history_render_job)
            self._history_render_job = None
        self._history_render_generation += 1
        generation = self._history_render_generation
        self._history_page_signature = None
        for entry in self._history_visible_entries:
            for key in ("card", "info_lbl", "btn_to_unmatched", "local_thumbnail", "match_thumbnail", "btn_show_folder", "btn_open_match", 'btn_remove_tags'):
                entry.pop(key, None)
        self._history_visible_entries = page_items
        for card in self._history_cards:
            card.pack_forget()
            for name in ("local_thumbnail", "match_thumbnail"):
                card._history_widgets[name]._history_thumb_token = uuid.uuid4().hex
        self._history_empty_label.pack_forget()
        if not page_items:
            self._history_empty_label.pack(fill="x", padx=12, pady=20)

        def render_from(index):
            self._history_render_job = None
            if generation != self._history_render_generation:
                return
            if index >= 2 and self.tabview.get() != "History":
                self._history_dirty = True
                return
            while index < len(page_items):
                self.render_history_card(page_items[index], slot=index)
                index += 1
                if index >= 2 and index < len(page_items):
                    self._history_render_job = self.after(12, lambda index=index: render_from(index))
                    return
            self._history_page_signature = signature
            self._history_dirty = False
            self.history_scroll.request_layout()
        render_from(0)
        self.update_metrics_display()

    def update_history_card_buttons(self):
        for entry in self._history_visible_entries:
            if entry.get('card'):
                self._layout_history_actions(entry)

    def _layout_history_actions(self, entry):
        card = entry['card']
        buttons = [entry['btn_show_folder'], entry['btn_open_match']]
        if not entry.get('is_unmatched', False) or entry['original_path'] == entry['current_path']:
            buttons.append(entry['btn_to_unmatched'])
        if entry['original_path'] != entry['current_path']:
            buttons.append(card._history_undo)
        buttons.append(entry['btn_remove_tags'])
        card._history_actions.set_buttons(buttons)

    def open_file_external(self, file_path):
        if not os.path.exists(file_path):
            messagebox.showwarning("File Not Found", "The file no longer exists at its destination path.")
            return
        try:
            sys_name = platform.system()
            if sys_name == 'Windows':
                os.startfile(file_path)
            elif sys_name == 'Darwin':
                subprocess.run(['open', file_path])
            else:
                subprocess.run(['xdg-open', file_path])
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open file: {e}")

    def open_folder_selected(self, file_path):
        if not os.path.exists(file_path):
            messagebox.showwarning("File Not Found", "The file no longer exists at its destination path.")
            return
        try:
            sys_name = platform.system()
            if sys_name == 'Windows':
                subprocess.run(['explorer', '/select,', os.path.normpath(file_path)])
            elif sys_name == 'Darwin':
                subprocess.run(['open', '-R', file_path])
            else:
                folder_path = os.path.dirname(file_path)
                subprocess.run(['xdg-open', folder_path])
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open folder: {e}")

    def move_to_unmatched_action(self, entry, card_frame):
        curr = entry["current_path"]
        if not os.path.exists(curr):
            messagebox.showwarning("File Not Found", "The file no longer exists at its current path.")
            return

        dest_unmatched = self.unmatched_path
        if not dest_unmatched or not os.path.exists(dest_unmatched):
            dest_unmatched = os.path.join(self.dest_path, "Unmatched") if self.dest_path else os.path.abspath("./Unmatched")

        os.makedirs(dest_unmatched, exist_ok=True)
        filename = entry["filename"]
        target_file_path = os.path.join(dest_unmatched, filename)

        try:
            target_file_path = safe_move(curr, target_file_path)
            self._relocate_history_entries(curr, target_file_path)

            if entry["original_path"] == curr:
                tracker.files_moved += 1
                tracker.unmatched_files_moved += 1
            elif not entry.get("is_unmatched", False):
                tracker.matched_files_moved = max(0, tracker.matched_files_moved - 1)
                tracker.unmatched_files_moved += 1

            entry["current_path"] = target_file_path
            entry["is_unmatched"] = True
            self._history_revision += 1
            entry["reason"] = "Manually moved to Unmatched"
            self._persist_history(entry)

            if "info_lbl" in entry:
                entry["info_lbl"].configure(text=f"File: {filename}\nMoved to: {os.path.dirname(target_file_path)}")

            self.log(f"Moved {filename} to Unmatched folder via History action.")

            if entry.get('card'):
                self._layout_history_actions(entry)

            self.refresh_history_view()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to move file to unmatched folder: {e}")

    def remove_metadata_action(self, entry):
        curr = entry["current_path"]
        if not os.path.exists(curr):
            messagebox.showwarning("File Not Found", "The file no longer exists at its current path.")
            return

        try:
            write_image_tags(curr, "")

            self.log(f"Removed embedded metadata from {entry['filename']}")
            messagebox.showinfo("Success", f"Successfully removed embedded tags from {entry['filename']}.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to remove metadata: {e}")

    def _persist_history(self, entry, remove=False):
        fields = ("id", "original_path", "current_path", "filename", "is_unmatched", "match_thumb_url", "match_url", "reason", "session_id", "recorded_at", "identification_method", "from_cache")
        try:
            from contextlib import closing
            with closing(sqlite3.connect(self.history_file, timeout=10)) as conn, conn:
                if remove:
                    conn.execute("DELETE FROM history WHERE id = ?", (entry["id"],))
                else:
                    payload = {key: entry.get(key, "") for key in fields}
                    conn.execute("INSERT INTO history(id, payload) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                                 (entry["id"], json.dumps(payload)))
        except (OSError, sqlite3.Error) as exc:
            self.log(f"Could not save history: {exc}")

    def _load_history(self):
        known_ids = {entry.get('id') for entry in self.history_items}
        added = False
        try:
            from contextlib import closing
            with closing(sqlite3.connect(self.history_file, timeout=10)) as conn, conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("CREATE TABLE IF NOT EXISTS history(id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                for (payload,) in conn.execute("SELECT payload FROM history ORDER BY rowid"):
                    try:
                        entry = json.loads(payload)
                        if entry.get('id') in known_ids:
                            continue
                        self.history_items.append(entry)
                        known_ids.add(entry.get('id'))
                        added = True
                    except (ValueError, TypeError):
                        continue
        except (OSError, sqlite3.Error) as exc:
            self.log(f"Could not load history: {exc}")
        if added:
            self._history_revision += 1
        self._reindex_history_paths()
        for entry in reversed(self.history_items):
            if entry.get('session_id'):
                self._current_session_id = entry['session_id']
                break
        # Repair old entries only through recorded moves, never filename guesses.
        links = {self._history_path_key(e['original_path']): e['current_path']
                 for e in self.history_items if e['original_path'] != e['current_path']}
        for source in list(self._history_path_index):
            if source not in links or os.path.isfile(source):
                continue
            target, seen = links[source], {source}
            while self._history_path_key(target) in links and not os.path.isfile(target):
                key = self._history_path_key(target)
                if key in seen:
                    break
                seen.add(key)
                target = links[key]
            if os.path.isfile(target):
                self._relocate_history_entries(source, target)

    @staticmethod
    def _history_path_key(path):
        return os.path.normcase(os.path.abspath(path))

    def _reindex_history_paths(self):
        self._history_path_index = {}
        for entry in self.history_items:
            self._history_path_index.setdefault(self._history_path_key(entry['current_path']), []).append(entry)

    def _relocate_history_entries(self, source, target):
        """Keep older reviews attached to a file after another recorded move."""
        if not hasattr(self, '_history_path_index'):
            self._reindex_history_paths()
        source_key, target_key = self._history_path_key(source), self._history_path_key(target)
        if source_key == target_key:
            return
        entries = self._history_path_index.pop(source_key, [])
        for entry in entries:
            entry['current_path'] = os.path.abspath(target)
            self._persist_history(entry)
        if entries:
            self._history_path_index.setdefault(target_key, []).extend(entries)
            self._history_revision += 1
            self._history_dirty = True

    def add_history_item(self, original_path, current_path, filename, is_unmatched=False, match_thumb_url="", match_url="", reason=""):
        entry = {
            "id": uuid.uuid4().hex,
            "original_path": os.path.abspath(original_path),
            "current_path": os.path.abspath(current_path),
            "filename": filename,
            "is_unmatched": is_unmatched,
            "match_thumb_url": match_thumb_url,
            "match_url": match_url,
            "reason": reason or ("No reliable image match" if is_unmatched else "Matched and routed")
        }
        entry.update(session_id=self._current_session_id, recorded_at=time.time(),
                     identification_method=getattr(self, '_history_identification_method', ''), from_cache=getattr(self, '_history_from_cache', False))
        self._persist_history(entry)
        def append_entry():
            if entry['original_path'] != entry['current_path']:
                self._relocate_history_entries(entry['original_path'], entry['current_path'])
            self.history_items.append(entry)
            self._history_path_index.setdefault(self._history_path_key(entry['current_path']), []).append(entry)
            self._on_history_item_added(entry)
        self.after(0, append_entry)

    def _on_history_item_added(self, entry):
        self._history_dirty = True
        if not self.is_processing and self.tabview.get() == "History":
            self.refresh_history_view()

    def render_history_card(self, entry, slot=None):
        slot = len(self._history_cards) if slot is None else slot
        if slot < len(self._history_cards):
            self._bind_history_card(self._history_cards[slot], entry)
            return
        current_path = entry["current_path"]
        match_url = entry.get("match_url", "")
        if match_url.startswith("//"):
            match_url = "https:" + match_url
        has_source_page = match_url.startswith(("https://", "http://"))
        card = ctk.CTkFrame(self.history_scroll.content, fg_color=self.history_scroll.cget('fg_color'), corner_radius=0)
        card.bind('<Configure>', lambda event: self.history_scroll._size_changed(card, event), add='+')
        entry["card"] = card
        card._history_entry = entry
        self._history_cards.append(card)
        card.pack(fill="x", pady=7, padx=5)
        card.grid_columnconfigure(2, weight=1)
        ctk.CTkFrame(card, height=1, corner_radius=0, fg_color=("#e1e5eb", "#353c46")).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        for column, title in ((0, "Your image"), (1, "Database match")):
            ctk.CTkLabel(card, text=title, font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold")).grid(row=0, column=column, padx=12, pady=(12, 5))
        local = ctk.CTkLabel(card, text="No image", width=110, height=110,
                             fg_color=("#e1e6ec", "#343d49"), corner_radius=5, cursor="hand2")
        local.grid(row=1, column=0, padx=12, pady=4)
        local.bind("<Button-1>", lambda _event: self.open_file_external(card._history_entry["current_path"]))
        entry["local_thumbnail"] = local
        matched = ctk.CTkLabel(card, text="Loading…" if entry.get("match_thumb_url") else "No preview" if has_source_page else "No match found",
                               width=110, height=110, fg_color=("#e1e6ec", "#343d49"), corner_radius=5,
                               cursor="hand2" if has_source_page else "")
        matched.grid(row=1, column=1, padx=12, pady=4)
        entry["match_thumbnail"] = matched
        matched.bind("<Button-1>", lambda _event: self._open_history_match(card))
        actions = self._ui_action_bar(card, on_reflow=self.history_scroll.request_layout)
        actions.grid(row=2, column=0, columnspan=3, padx=12, pady=(5, 6), sticky='ew')
        card._history_actions = actions
        entry["btn_show_folder"] = self._ui_button(actions, "Show in folder", lambda: self.open_folder_selected(card._history_entry["current_path"]), width=110)
        entry["btn_open_match"] = self._ui_button(actions, "Open source page", lambda: self._open_history_match(card), width=122)
        if not has_source_page:
            entry["btn_open_match"].configure(state="disabled")
        status = self._history_status(entry)
        info = ctk.CTkLabel(card, text=f"{entry['filename']}\n{status}\n{os.path.dirname(current_path)}\n{self._history_reason(entry)}",
                            justify="left", anchor="nw", wraplength=500, font=ctk.CTkFont(family="Segoe UI", size=13))
        info.grid(row=0, column=2, rowspan=2, sticky="new", padx=18, pady=12)
        entry["info_lbl"] = info
        self._watch_wrap_width(card, info, padding=315)
        entry["btn_to_unmatched"] = self._ui_button(actions, "To Unmatched", lambda: self.move_to_unmatched_action(card._history_entry, card), width=115)
        undo = self._ui_button(actions, "Undo move", lambda: self.undo_move(card._history_entry, card), width=95)
        entry['btn_remove_tags'] = self._ui_button(actions, "Remove tags…", lambda: self.remove_metadata_action(card._history_entry), width=115)

        card._history_widgets = {name: entry[name] for name in ("card", "info_lbl", "btn_to_unmatched", "local_thumbnail", "match_thumbnail", "btn_show_folder", "btn_open_match", 'btn_remove_tags')}
        card._history_undo = undo
        self._bind_history_card(card, entry)

    def _open_history_match(self, card):
        url = card._history_entry.get("match_url", "")
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith(("http://", "https://")):
            webbrowser.open(url)

    def _bind_history_card(self, card, entry):
        card._history_entry = entry
        entry.update(card._history_widgets)
        card.pack(fill="x", pady=7, padx=5)
        path = entry["current_path"]
        url = entry.get("match_url", "")
        has_source = url.startswith(("http://", "https://", "//"))
        entry["btn_open_match"].configure(state="normal" if has_source else "disabled")
        entry["match_thumbnail"].configure(cursor="hand2" if has_source else "")
        status = self._history_status(entry)
        entry["info_lbl"].configure(text=f"{entry['filename']}\n{status}\n{os.path.dirname(path)}\n{self._history_reason(entry)}")
        self._layout_history_actions(entry)
        for name in ("local_thumbnail", "match_thumbnail"):
            label = entry[name]
            label._history_thumb_token = uuid.uuid4().hex
            label.configure(image="", text="Loading…")
            label.image = None
        self.load_history_local_thumb_async(path, entry["local_thumbnail"])
        if entry.get("match_thumb_url"):
            self.load_history_match_thumb_async(entry["match_thumb_url"], entry["match_thumbnail"], url)
        else:
            entry["match_thumbnail"].configure(text="No preview" if has_source else "No match found")

    def _cache_history_thumbnail(self, key, thumbnail):
        if thumbnail is None:
            return
        with self.history_thumb_lock:
            self.history_thumb_cache[key] = thumbnail
            self.history_thumb_cache.move_to_end(key)
            while len(self.history_thumb_cache) > self.history_thumb_cache_limit:
                self.history_thumb_cache.popitem(last=False)

    def _cached_history_thumbnail(self, key):
        with self.history_thumb_lock:
            thumbnail = self.history_thumb_cache.get(key)
            if thumbnail is not None:
                self.history_thumb_cache.move_to_end(key)
            return thumbnail

    def _queue_history_thumbnail(self, reference, token, thumbnail, key):
        label = reference()
        if label is not None and getattr(label, "_history_thumb_token", None) == token:
            self._history_image_results.put((reference, token, thumbnail, key))

    def load_history_local_thumb_async(self, path, label):
        reference, token = weakref.ref(label), label._history_thumb_token
        def load():
            target = reference()
            if target is None or getattr(target, "_history_thumb_token", None) != token:
                return
            key, thumbnail = None, None
            try:
                stat = os.stat(path)
                key = ("local", os.path.normcase(os.path.abspath(path)), stat.st_mtime_ns, stat.st_size)
                thumbnail = self._cached_history_thumbnail(key)
                if thumbnail is None:
                    with Image.open(path) as image:
                        image.draft("RGB", (192, 192))
                        image = ImageOps.exif_transpose(image)
                        image.thumbnail((96, 96))
                        thumbnail = image.copy()
                    self._cache_history_thumbnail(key, thumbnail)
            except (OSError, ValueError):
                pass
            self._queue_history_thumbnail(reference, token, thumbnail, key)
        self.history_local_executor.submit(load)

    def load_history_match_thumb_async(self, url, lbl_widget, match_url=""):
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            lbl_widget.configure(text="Preview unavailable")
            return
        token = lbl_widget._history_thumb_token
        reference = weakref.ref(lbl_widget)
        key = ("remote", url)
        cached = self._cached_history_thumbnail(key)
        if cached is not None:
            self._queue_history_thumbnail(reference, token, cached, key)
            return
        with self.history_thumb_lock:
            self.history_thumb_waiters.setdefault(url, []).append((reference, token))
            if url in self.history_thumb_inflight:
                return
            self.history_thumb_inflight.add(url)

        def download():
            with self.history_thumb_lock:
                waiters = list(self.history_thumb_waiters.get(url, []))
            if not any(ref() is not None and getattr(ref(), "_history_thumb_token", None) == wanted for ref, wanted in waiters):
                return None  # The user already paged away; discard queued work.
            return self._download_history_thumbnail(url)

        def finished(future):
            try:
                thumbnail = future.result()
            except Exception:
                thumbnail = None
            self._cache_history_thumbnail(key, thumbnail)
            with self.history_thumb_lock:
                self.history_thumb_inflight.discard(url)
                labels = self.history_thumb_waiters.pop(url, [])
            for ref, wanted in labels:
                self._queue_history_thumbnail(ref, wanted, thumbnail, key)
        self.history_thumb_executor.submit(download).add_done_callback(finished)

    def _drain_history_thumbnails(self):
        if not self.winfo_exists():
            return
        if self.tabview.get() != "History":
            self.after(80, self._drain_history_thumbnails)
            return
        applied = 0
        for _ in range(100):
            try:
                reference, token, thumbnail, key = self._history_image_results.get_nowait()
            except queue.Empty:
                break
            label = reference()
            if label is not None and getattr(label, "_history_thumb_token", None) == token and label.winfo_exists():
                self._apply_history_thumbnail(label, thumbnail, key)
                applied += 1
                if applied >= 2:
                    break
        self.after(40, self._drain_history_thumbnails)

    def _download_history_thumbnail(self, url, size=(96, 96)):
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        if "zerochan" in url: headers['Referer'] = 'https://www.zerochan.net/'
        elif "gelbooru" in url: headers['Referer'] = 'https://gelbooru.com/'
        elif "danbooru" in url.lower() or "donmai" in url.lower(): headers['Referer'] = 'https://danbooru.donmai.us/'
        elif "yande" in url: headers['Referer'] = 'https://yande.re/'
        elif "pixiv" in url: headers['Referer'] = 'https://www.pixiv.net/'
        response = get_http_session().get(url, headers=headers, timeout=5)
        if response.status_code != 200:
            return None
        with Image.open(BytesIO(response.content)) as pil_img:
            pil_img.thumbnail(size)
            return pil_img.copy()

    def _apply_history_thumbnail(self, label, pil_image, cache_key=None):
        if not label.winfo_exists():
            return
        if pil_image is None:
            label.configure(text="Preview unavailable", image="")
            return
        image = self._history_ctk_images.get(cache_key) if cache_key is not None else None
        if image is None:
            image = ctk.CTkImage(light_image=pil_image, dark_image=pil_image, size=pil_image.size)
            if cache_key is not None:
                self._history_ctk_images[cache_key] = image
                while len(self._history_ctk_images) > self.history_thumb_cache_limit:
                    self._history_ctk_images.popitem(last=False)
        elif cache_key is not None:
            self._history_ctk_images.move_to_end(cache_key)
        label.configure(image=image, text="")
        label.image = image

    def undo_move(self, entry, card_frame):
        curr = entry["current_path"]
        orig = entry["original_path"]
        if os.path.exists(curr):
            try:
                target_dir = os.path.dirname(orig)
                os.makedirs(target_dir, exist_ok=True)
                back_path = os.path.join(target_dir, entry["filename"])
                back_path = safe_move(curr, back_path)
                self._relocate_history_entries(curr, back_path)

                if entry.get("is_unmatched", False):
                    tracker.unmatched_files_moved = max(0, tracker.unmatched_files_moved - 1)
                else:
                    tracker.matched_files_moved = max(0, tracker.matched_files_moved - 1)
                tracker.files_moved = max(0, tracker.files_moved - 1)
                self.update_metrics_display()

                self.log(f"Undo: Moved {entry['filename']} back to {target_dir}")
                if entry in self.history_items:
                    self.history_items.remove(entry)
                    self._persist_history(entry, remove=True)
                    self._reindex_history_paths()
                self.refresh_history_view()
            except Exception as e:
                messagebox.showerror("Undo Error", f"Failed to undo move: {e}")
        else:
            messagebox.showwarning("File Not Found", "The moved file no longer exists at its destination path.")
            if entry in self.history_items:
                self.history_items.remove(entry)
                self._persist_history(entry, remove=True)
                self._reindex_history_paths()
            if entry.get("is_unmatched", False):
                tracker.unmatched_files_moved = max(0, tracker.unmatched_files_moved - 1)
            else:
                tracker.matched_files_moved = max(0, tracker.matched_files_moved - 1)
            tracker.files_moved = max(0, tracker.files_moved - 1)
            self.update_metrics_display()
            self.refresh_history_view()

