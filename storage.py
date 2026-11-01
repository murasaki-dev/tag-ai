"""Settings, image metadata, file moves, and result cache."""

from stage_timing import profile, timed_stage, cache_stage, cache_hit
from PIL import Image
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import zlib
from contextlib import closing
from routing import AUTO_MODEL, CONTEXT_METHODS, DATABASE_METHODS, legacy_database_flow, normalize_named_flow, normalize_search_pipeline, validate_folder_aliases
from providers import tracker
from routing import UNMATCHED_ACTIONS, MATCHING_MODES

def stream_file_md5(file_path, chunk_size=1024 * 1024):
    """Hash a file without loading its entire contents into memory."""
    digest = hashlib.md5()
    with open(file_path, "rb") as image_file:
        for chunk in iter(lambda: image_file.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


@profile("move")
def safe_move(source, destination):
    """Reserve a unique destination; never overwrite an existing image."""
    if os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(destination)):
        return source
    stem, ext = os.path.splitext(destination)
    index = 0
    while True:
        candidate = destination if index == 0 else f"{stem} ({index}){ext}"
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            index += 1
    try:
        try:
            os.replace(source, candidate)  # Same-volume moves do not copy image data.
        except OSError as exc:
            if exc.errno != errno.EXDEV and getattr(exc, "winerror", None) != 17:
                raise
            shutil.copy2(source, candidate)
            os.unlink(source)
    except BaseException:
        os.unlink(candidate)
        raise
    return candidate


@profile("metadata.write")
def write_image_tags(path, text):
    """Edit tag metadata atomically without re-encoding image pixels."""
    ext = os.path.splitext(path)[1].lower()
    fd, temporary = tempfile.mkstemp(suffix=ext, dir=os.path.dirname(os.path.abspath(path)))
    os.close(fd)
    try:
        if ext in (".jpg", ".jpeg"):
            import piexif
            from piexif.helper import UserComment
            metadata = piexif.load(path)
            if text:
                metadata["Exif"][piexif.ExifIFD.UserComment] = UserComment.dump(text, encoding="unicode")
            else:
                metadata["Exif"].pop(piexif.ExifIFD.UserComment, None)
            piexif.insert(piexif.dump(metadata), path, temporary)
        elif ext == ".png":
            with open(path, "rb") as source, open(temporary, "wb") as output:
                signature = source.read(8)
                if signature != b"\x89PNG\r\n\x1a\n":
                    raise ValueError("Invalid PNG")
                output.write(signature)
                while True:
                    header = source.read(8)
                    if len(header) != 8:
                        raise ValueError("Truncated PNG")
                    length, kind = struct.unpack(">I4s", header)
                    data = source.read(length)
                    crc = source.read(4)
                    if len(data) != length or len(crc) != 4:
                        raise ValueError("Truncated PNG chunk")
                    ours = kind in (b"tEXt", b"iTXt", b"zTXt") and data.split(b"\0", 1)[0] in (b"parameters", b"booru_tags")
                    if kind == b"IEND" and text:
                        for key in ("parameters", "booru_tags"):
                            payload = key.encode() + b"\0\0\0\0\0" + text.encode("utf-8")
                            output.write(struct.pack(">I", len(payload)) + b"iTXt" + payload + struct.pack(">I", zlib.crc32(b"iTXt" + payload) & 0xffffffff))
                    if not ours:
                        output.write(header + data + crc)
                    if kind == b"IEND":
                        break
        else:
            return
        shutil.copystat(path, temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class PersistentResultCache:
    """SQLite cache for completed image results and short-lived negative results."""
    NEGATIVE_TTL_SECONDS = 6 * 60 * 60

    def __init__(self, path):
        from contextlib import closing
        self.path = path
        self.lock = threading.Lock()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""CREATE TABLE IF NOT EXISTS image_results (
                    image_hash TEXT PRIMARY KEY,
                    settings_signature TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    saved_at REAL NOT NULL
                )""")
                conn.execute("CREATE TABLE IF NOT EXISTS image_refresh (image_hash TEXT PRIMARY KEY)")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    @profile(lambda owner, image_hash, settings_signature: cache_stage(image_hash) + ".read")
    def get(self, image_hash, settings_signature):
        try:
            result = self._get_unchecked(image_hash, settings_signature)
            if result is not None: cache_hit(self, cache_stage(image_hash).replace(".cache", ""))
            return result
        except (sqlite3.Error, OSError):
            return None

    def _get_unchecked(self, image_hash, settings_signature):
        from contextlib import closing
        with self.lock, closing(self._connect()) as conn:
            with conn:
                row = conn.execute(
                    "SELECT settings_signature, status, payload, saved_at FROM image_results WHERE image_hash = ?",
                    (image_hash,),
                ).fetchone()
                if row is None:
                    return None
                signature, status, payload, saved_at = row
                expired_negative = status == "miss" and time.time() - saved_at > self.NEGATIVE_TTL_SECONDS
                expired_alias = image_hash.startswith('context:booru_alias:') and time.time() - saved_at > 30 * 24 * 60 * 60
                if signature != settings_signature or expired_negative or expired_alias:
                    conn.execute("DELETE FROM image_results WHERE image_hash = ?", (image_hash,))
                    return None
                try:
                    return {"status": status, **json.loads(payload)}
                except (TypeError, ValueError, json.JSONDecodeError):
                    conn.execute("DELETE FROM image_results WHERE image_hash = ?", (image_hash,))
                    return None

    @profile(lambda owner, image_hash, *args, **kwargs: cache_stage(image_hash) + ".write")
    def put(self, image_hash, settings_signature, status, payload=None):
        try:
            self._put_unchecked(image_hash, settings_signature, status, payload)
        except (sqlite3.Error, OSError):
            return None

    def _put_unchecked(self, image_hash, settings_signature, status, payload=None):
        from contextlib import closing
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        with self.lock, closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO image_results (image_hash, settings_signature, status, payload, saved_at) VALUES (?, ?, ?, ?, ?)",
                    (image_hash, settings_signature, status, payload_json, time.time()),
                )
                if status in ("resolved", "miss"):
                    conn.execute("DELETE FROM image_refresh WHERE image_hash=?", (image_hash,))

    def forget_image(self, hashes, current_hash):
        """Invalidate only known content identities; request a fresh search next run."""
        hashes = {h for h in hashes if isinstance(h, str) and len(h) == 32 and all(c in '0123456789abcdef' for c in h)}
        if current_hash not in hashes: raise ValueError('No valid current image hash')
        keys = [key for h in hashes for key in (h, h + ':route', 'context:serpapi:' + h, 'context:tagger:' + h)]
        with self.lock, closing(self._connect()) as conn, conn:
            deleted = conn.executemany('DELETE FROM image_results WHERE image_hash=?', [(key,) for key in keys]).rowcount
            for digest in hashes:
                deleted += conn.execute('DELETE FROM image_results WHERE image_hash LIKE ?', ('context:serpapi:' + digest + ':guided:%',)).rowcount
            conn.execute('INSERT OR IGNORE INTO image_refresh VALUES (?)', (current_hash,))
        return deleted

    @profile("source.refresh")
    def needs_refresh(self, image_hash):
        with self.lock, closing(self._connect()) as conn:
            return conn.execute('SELECT 1 FROM image_refresh WHERE image_hash=?', (image_hash,)).fetchone() is not None

    def clear(self):
        try:
            self._clear_unchecked()
        except (sqlite3.Error, OSError):
            return None

    def _clear_unchecked(self):
        from contextlib import closing
        with self.lock, closing(self._connect()) as conn:
            with conn:
                conn.execute("DELETE FROM image_results")
                conn.execute("DELETE FROM image_refresh")

    def close(self):
        return None


class DisabledResultCache:
    """Fallback that preserves application operation if the cache file is unavailable."""
    def get(self, image_hash, settings_signature):
        return None
    def put(self, image_hash, settings_signature, status, payload=None):
        return None
    def clear(self):
        return None
    def close(self):
        return None


class StdoutRedirector:
    """Redirects stdout and stderr to the GUI application log window."""
    def __init__(self, log_callback):
        self.log_callback = log_callback
        self.terminal_out = sys.stdout

    def write(self, message):
        self.terminal_out.write(message)
        cleaned = message.strip()
        if cleaned:
            self.log_callback(cleaned)

    def flush(self):
        self.terminal_out.flush()


def extract_embedded_tags(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    tag_text = ""
    try:
        with Image.open(file_path) as img:
            if ext == '.png':
                # The lossless writer places text after IDAT. Pillow's text property
                # reads those trailing chunks; info at open() can still be empty.
                png_text = img.text
                tag_text = png_text.get("booru_tags") or png_text.get("parameters", "")
            elif ext in ['.jpg', '.jpeg']:
                import piexif
                exif_data = img.info.get('exif')
                if exif_data:
                    exif_dict = piexif.load(exif_data)
                    user_comment = exif_dict.get("Exif", {}).get(piexif.ExifIFD.UserComment, b'')
                    if user_comment:
                        from piexif.helper import UserComment
                        tag_text = UserComment.load(user_comment)
    except Exception:
        pass
    return tag_text.strip()


class BatchSessionStore:
    """Persist the file list once; checkpoint progress without rewriting large lists."""
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS batch_session (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL, completed INTEGER NOT NULL)")

    def load(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            row = connection.execute("SELECT payload, completed FROM batch_session WHERE id=1").fetchone()
        if row is None:
            return None
        record = json.loads(row[0])
        if (not isinstance(record, dict) or record.get('version') != 1
                or not all(isinstance(record.get(key), str) and record[key] for key in ('source', 'destination', 'mode'))
                or not isinstance(record.get('files'), list)
                or not all(isinstance(path, str) and os.path.isabs(path) for path in record['files'])
                or not 0 <= row[1] <= len(record['files'])):
            raise ValueError('Invalid batch checkpoint')
        record['completed'] = row[1]
        return record if row[1] < len(record['files']) else None

    @profile("checkpoint.begin")
    def begin(self, source, destination, mode, files, session_id=None):
        record = {'version': 1, 'source': os.path.abspath(source),
                  'destination': os.path.abspath(destination), 'mode': mode,
                  'files': list(files), 'completed': 0, 'session_id': session_id}
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT OR REPLACE INTO batch_session VALUES (1, ?, 0)",
                               (json.dumps(record),))
        return record

    def set_session_id(self, session_id):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            row = connection.execute('SELECT payload FROM batch_session WHERE id=1').fetchone()
            if row:
                record = json.loads(row[0]); record['session_id'] = session_id
                connection.execute('UPDATE batch_session SET payload=? WHERE id=1', (json.dumps(record),))

    @profile("checkpoint")
    @profile("checkpoint.advance")
    def advance(self, completed):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE batch_session SET completed=? WHERE id=1", (completed,))

    def clear(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DELETE FROM batch_session WHERE id=1")


class StorageMixin:
    def save_settings(self):
        self._sync_search_flow_from_legacy()
        self._sync_inner_flows_from_legacy()
        try:
            aliases = validate_folder_aliases(json.loads(self.txt_folder_aliases.get("1.0", "end").strip()))
        except (ValueError, TypeError) as exc:
            self.lbl_settings_saved.configure(text=f"Aliases not saved: {exc}")
            self.log(f"Invalid folder aliases: {exc}")
            return False
        self.folder_aliases = aliases
        config = {
            "exec_mode": self._read_widget(self.opt_exec_mode),
            "source_folder": self.target_source,
            "dest_folder": self.dest_path,
            "matching_mode": self._read_widget(self.opt_matching_mode),
            "unmatched_action": self._read_widget(self.opt_unmatched),
            "unmatched_folder": self.unmatched_path,
            "engine": self._read_widget(self.opt_engine),
            "search_pipeline": self.search_flow.get_config(),
            "database_pipelines": self.database_pipelines,
            "context_pipeline": self.context_pipeline,
            "tags": self._read_widget(self.opt_tags),
            "fallback": int(self._read_widget(self.switch_engine_fallback)),
            "vision_fallback": int(self._read_widget(self.switch_vision_fallback)),
            "serpapi_enabled": int(self._read_widget(self.switch_serpapi)),
            "gvision_enabled": int(self._read_widget(self.switch_gvision)),
            "skip_on_web_fail": int(self._read_widget(self.switch_skip_on_web_fail)),
            "vision_warning_shown": self.vision_warning_shown,
            "scan_subfolders": int(self._read_widget(self.switch_scan_subfolders)),
            "use_embedded_tags": int(self._read_widget(self.switch_use_embedded_tags)),
            "skip_no_embedded": int(self._read_widget(self.switch_skip_no_embedded)),
            "embed_metadata": int(self._read_widget(self.switch_embed_metadata)),
            "meta_artist": int(self._read_widget(self.switch_meta_artist)),
            "meta_series": int(self._read_widget(self.switch_meta_series)),
            "meta_character": int(self._read_widget(self.switch_meta_character)),
            "meta_general": int(self._read_widget(self.switch_meta_general)),
            "meta_meta": int(self._read_widget(self.switch_meta_meta)),
            "serpapi_keys": [self._read_widget(ent).strip() for _, ent in self._entries("serpapi_entries") if self._read_widget(ent).strip()],
            "gvision_keys": [self._read_widget(ent).strip() for _, ent in self._entries("gvision_entries") if self._read_widget(ent).strip()],
            "db_users": [self._read_widget(ent).strip() for _, ent in self._entries("db_user_entries") if self._read_widget(ent).strip()],
            "db_keys": [self._read_widget(ent).strip() for _, ent in self._entries("db_key_entries") if self._read_widget(ent).strip()],
            "gb_users": [self._read_widget(ent).strip() for _, ent in self._entries("gb_user_entries") if self._read_widget(ent).strip()],
            "gb_keys": [self._read_widget(ent).strip() for _, ent in self._entries("gb_key_entries") if self._read_widget(ent).strip()],
            "skeys": [self._read_widget(ent).strip() for _, ent in self._entries("skey_entries") if self._read_widget(ent).strip()],
            "lm_url": self._read_widget(self.ent_lm),
            "selected_model": self._read_widget(self.opt_model),
            "folder_aliases": aliases,
            "serpapi_counter": tracker.serpapi_calls,
            "gvision_counter": tracker.gvision_calls
        }
        try:
            fd, temporary = tempfile.mkstemp(dir=os.path.dirname(self.config_file), suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=4)
                os.replace(temporary, self.config_file)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except Exception as e:
            self.log(f"Warning: Failed to save config to disk. ({e})")
            return False
        return True

    def load_settings(self, filepath=None):
        path = filepath or self.config_file
        if not os.path.exists(path): return
        try:
            with open(path, 'r') as f:
                config = json.load(f)

            if "exec_mode" in config: self.opt_exec_mode.set(config["exec_mode"])
            if "source_folder" in config:
                self.target_source = config["source_folder"]
                if self.target_source: self.lbl_source_val.configure(text=self.target_source)
            if "dest_folder" in config:
                self.dest_path = config["dest_folder"]
                if self.dest_path: self.lbl_dest_val.configure(text=self.dest_path)
            mode = config.get('matching_mode', 'Exact')
            self.opt_matching_mode.set(mode if mode in MATCHING_MODES else 'Exact')
            self.on_matching_mode_change(self.opt_matching_mode.get())
            action = config.get('unmatched_action', UNMATCHED_ACTIONS[0])
            self.opt_unmatched.set(action if action in UNMATCHED_ACTIONS else UNMATCHED_ACTIONS[0])
            if "unmatched_folder" in config:
                self.unmatched_path = config["unmatched_folder"]
                if self.unmatched_path: self.lbl_unmatched_val.configure(text=self.unmatched_path)

            if "engine" in config:
                engine_value = "IQDB" if config["engine"] == "Self-hosted IQDB" else config["engine"]
                if engine_value in ["IQDB", "SauceNAO", "Local Vision LLM"]:
                    self.opt_engine.set(engine_value)
            if "tags" in config: self.opt_tags.set(config["tags"])

            if config.get("fallback", 1): self.switch_engine_fallback.select()
            else: self.switch_engine_fallback.deselect()

            if config.get("vision_fallback", 1): self.switch_vision_fallback.select()
            else: self.switch_vision_fallback.deselect()

            if config.get("serpapi_enabled", 0): self.switch_serpapi.select()
            else: self.switch_serpapi.deselect()

            if config.get("gvision_enabled", 0): self.switch_gvision.select()
            else: self.switch_gvision.deselect()

            if config.get("skip_on_web_fail", 0): self.switch_skip_on_web_fail.select()
            else: self.switch_skip_on_web_fail.deselect()

            self.vision_warning_shown = config.get("vision_warning_shown", False)

            if config.get("scan_subfolders", 0): self.switch_scan_subfolders.select()
            else: self.switch_scan_subfolders.deselect()

            if config.get("use_embedded_tags", 0): self.switch_use_embedded_tags.select()
            else: self.switch_use_embedded_tags.deselect()
            self.on_use_embedded_tags_toggle()

            if config.get("skip_no_embedded", 0): self.switch_skip_no_embedded.select()
            else: self.switch_skip_no_embedded.deselect()

            if config.get("embed_metadata", 0): self.switch_embed_metadata.select()
            else: self.switch_embed_metadata.deselect()
            self.on_embed_metadata_toggle()

            if config.get("meta_artist", 1): self.switch_meta_artist.select()
            else: self.switch_meta_artist.deselect()

            if config.get("meta_series", 1): self.switch_meta_series.select()
            else: self.switch_meta_series.deselect()

            if config.get("meta_character", 1): self.switch_meta_character.select()
            else: self.switch_meta_character.deselect()

            if config.get("meta_general", 1): self.switch_meta_general.select()
            else: self.switch_meta_general.deselect()

            if config.get("meta_meta", 0): self.switch_meta_meta.select()
            else: self.switch_meta_meta.deselect()

            # Load multi-key fields safely supporting legacy config keys as fallback
            serp_vals = config.get("serpapi_keys", [])
            if not serp_vals and config.get("serpapi_key"): serp_vals = [config.get("serpapi_key")]

            gvis_vals = config.get("gvision_keys", [])
            if not gvis_vals and config.get("gvision_key"): gvis_vals = [config.get("gvision_key")]

            db_u_vals = config.get("db_users", [])
            if not db_u_vals and config.get("db_user"): db_u_vals = [config.get("db_user")]

            db_k_vals = config.get("db_keys", [])
            if not db_k_vals and config.get("db_key"): db_k_vals = [config.get("db_key")]

            gb_u_vals = config.get("gb_users", [])
            if not gb_u_vals and config.get("gb_user"): gb_u_vals = [config.get("gb_user")]

            gb_k_vals = config.get("gb_keys", [])
            if not gb_k_vals and config.get("gb_key"): gb_k_vals = [config.get("gb_key")]

            s_vals = config.get("skeys", [])
            if not s_vals and config.get("skey"): s_vals = [config.get("skey")]

            for list_frame, entries, vals, add_func in [
                (self.serpapi_list_frame, self.serpapi_entries, serp_vals, self.add_serpapi_field),
                (self.gvision_list_frame, self.gvision_entries, gvis_vals, self.add_gvision_field),
                (self.db_user_list_frame, self.db_user_entries, db_u_vals, self.add_db_user_field),
                (self.db_key_list_frame, self.db_key_entries, db_k_vals, self.add_db_key_field),
                (self.gb_user_list_frame, self.gb_user_entries, gb_u_vals, self.add_gb_user_field),
                (self.gb_key_list_frame, self.gb_key_entries, gb_k_vals, self.add_gb_key_field),
                (self.skey_list_frame, self.skey_entries, s_vals, self.add_skey_field),
            ]:
                if vals:
                    for row, ent in list(entries):
                        row.destroy()
                    entries.clear()
                    for v in vals:
                        if v.strip():
                            add_func(v.strip())
                    if not entries:
                        add_func()

            self.ent_lm.delete(0, 'end')
            self.ent_lm.insert(0, config.get("lm_url", "http://localhost:1234/v1"))

            self.folder_aliases = validate_folder_aliases(config.get("folder_aliases", {}))
            self.txt_folder_aliases.delete("1.0", "end")
            self.txt_folder_aliases.insert("1.0", json.dumps(self.folder_aliases, indent=2, ensure_ascii=False))
            selected_model = config.get("selected_model") or AUTO_MODEL
            self.opt_model.configure(values=[AUTO_MODEL] if selected_model == AUTO_MODEL else [AUTO_MODEL, selected_model])
            self.opt_model.set(selected_model)
            tracker.serpapi_calls = config.get("serpapi_counter", 0)
            tracker.gvision_calls = config.get("gvision_counter", 0)
            self.update_metrics_display()

            self.on_unmatched_change(self._read_widget(self.opt_unmatched))
            self.on_exec_mode_change(self._read_widget(self.opt_exec_mode))
            self.database_pipelines = {engine: normalize_named_flow(config.get("database_pipelines", {}).get(engine, legacy_database_flow(self.opt_tags.get())), DATABASE_METHODS)
                                       for engine in ("IQDB", "SauceNAO")}
            legacy_context = [{"method": "serpapi", "enabled": bool(self.switch_serpapi.get())},
                              {"method": "gvision", "enabled": bool(self.switch_gvision.get())}]
            self.context_pipeline = normalize_named_flow(config.get("context_pipeline", legacy_context), CONTEXT_METHODS)
            self._last_database_preference = self.opt_tags.get()
            self._apply_context_flags()
            self._refresh_inner_flow_widgets()
            if "search_pipeline" in config:
                self.search_flow.set_config(normalize_search_pipeline(config["search_pipeline"]))
                self._on_search_flow_changed(self.search_flow.get_config())
            else:
                self._sync_search_flow_from_legacy(force=True)
            self.update_search_options()
            self.log("Settings and usage data loaded successfully.")
        except Exception as e:
            self.log(f"Error loading config: {e}")

    def export_settings(self):
        from tkinter import filedialog
        self.save_settings()
        file_path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON Files", "*.json")], title="Export Settings Data")
        if file_path:
            try:
                shutil.copy(self.config_file, file_path)
                self.log(f"Successfully exported data to: {os.path.basename(file_path)}")
            except Exception as e:
                self.log(f"Export failed: {e}")

    def import_settings(self):
        from tkinter import filedialog
        file_path = filedialog.askopenfilename(filetypes=[("JSON Files", "*.json")], title="Import Settings Data")
        if file_path:
            try:
                shutil.copy(file_path, self.config_file)
                self.load_settings()
                self.log(f"Successfully imported data from: {os.path.basename(file_path)}")
            except Exception as e:
                self.log(f"Import failed: {e}")

    def embed_metadata(self, file_path, tag_dict):
        if not self._read_widget(self.switch_embed_metadata) == 1:
            return

        if os.path.splitext(file_path)[1].lower() not in ('.png', '.jpg', '.jpeg'):
            self.log(f"  [Metadata] Skipped unsupported format: {os.path.basename(file_path)}", full_only=True)
            return
        with timed_stage(self, "metadata"):
            parts = []
            if self._read_widget(self.switch_meta_artist) and tag_dict.get('artist'):
                parts.append(tag_dict['artist'])
            if self._read_widget(self.switch_meta_series) and tag_dict.get('copyright'):
                parts.append(tag_dict['copyright'])
            if self._read_widget(self.switch_meta_character) and tag_dict.get('character'):
                parts.append(tag_dict['character'])
            if self._read_widget(self.switch_meta_general) and tag_dict.get('general'):
                parts.append(tag_dict['general'])
            if self._read_widget(self.switch_meta_meta) and tag_dict.get('meta'):
                parts.append(tag_dict['meta'])

            final_string = " ".join([p for p in parts if p]).strip()
            if not final_string:
                return

            try:
                write_image_tags(file_path, final_string)
                self.log(f"  [Metadata] Embedded {len(final_string.split())} selected tags in {os.path.basename(file_path)}")
            except Exception as exc:
                self.log(f"Metadata update failed for {file_path}: {exc}")

