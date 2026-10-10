import os
import time
import json
import re
import threading
import shutil
import requests
import hashlib
import platform
import subprocess
import base64
from io import BytesIO
from PIL import Image, PngImagePlugin
from openai import OpenAI
import customtkinter as ctk
from tkinter import filedialog, messagebox
from bs4 import BeautifulSoup

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

class WindowRateLimiter:
    def __init__(self, limit, period_seconds):
        self.limit = limit
        self.period_seconds = period_seconds
        self.window_start = 0
        self.window_calls = 0
        self.total_calls = 0

    def record(self, log_callback=None, block=True):
        now = time.time()
        if self.window_calls == 0 or now - self.window_start >= self.period_seconds:
            self.window_start = now
            self.window_calls = 0
            
        if self.window_calls >= self.limit:
            sleep_time = self.period_seconds - (now - self.window_start)
            if sleep_time > 0:
                if not block:
                    return False
                if log_callback: log_callback(f"  [Rate Limit] Cooldown triggered. Sleeping for {sleep_time:.1f}s...")
                time.sleep(sleep_time)
            self.window_start = time.time()
            self.window_calls = 0

        self.window_calls += 1
        self.total_calls += 1
        return self.total_calls if block else True


class MultiWindowRateLimiter:
    def __init__(self, limits_and_periods):
        self.limiters = [{"limit": l, "period": p, "start": 0, "calls": 0} for l, p in limits_and_periods]
        self.total_calls = 0

    def record(self, log_callback=None, block=True):
        now = time.time()
        max_sleep = 0
        
        for limiter in self.limiters:
            if limiter["calls"] == 0 or now - limiter["start"] >= limiter["period"]:
                limiter["start"] = now
                limiter["calls"] = 0
                
            if limiter["calls"] >= limiter["limit"]:
                sleep_time = limiter["period"] - (now - limiter["start"])
                if sleep_time > max_sleep:
                    max_sleep = sleep_time
        
        if max_sleep > 0:
            if not block:
                return False
            if log_callback: log_callback(f"  [Rate Limit] Multi-tier cooldown triggered. Sleeping for {max_sleep:.1f}s...")
            time.sleep(max_sleep)
            now = time.time()
            for limiter in self.limiters:
                if now - limiter["start"] >= limiter["period"]:
                    limiter["start"] = now
                    limiter["calls"] = 0

        for limiter in self.limiters:
            limiter["calls"] += 1
            
        self.total_calls += 1
        return self.total_calls if block else True


class GentleRateLimiter:
    def __init__(self, delay_seconds):
        self.delay_seconds = delay_seconds
        self.last_call_time = 0
        self.total_calls = 0

    def record(self, log_callback=None, block=True):
        now = time.time()
        elapsed = now - self.last_call_time
        if elapsed < self.delay_seconds:
            sleep_time = self.delay_seconds - elapsed
            if not block:
                return False
            if log_callback: log_callback(f"  [Rate Limit] Pacing requests. Sleeping for {sleep_time:.1f}s...")
            time.sleep(sleep_time)
        
        self.last_call_time = time.time()
        self.total_calls += 1
        return self.total_calls if block else True


class ApiTracker:
    def __init__(self):
        self.iqdb_limiter = WindowRateLimiter(30, 60)
        self.saucenao_limiter = MultiWindowRateLimiter([(4, 30), (100, 86400)])
        self.danbooru_limiter = WindowRateLimiter(300, 60)
        self.gelbooru_limiter = GentleRateLimiter(1.5)
        self.yandere_limiter = GentleRateLimiter(1.5)
        self.konachan_limiter = GentleRateLimiter(1.5)
        self.zerochan_limiter = GentleRateLimiter(1.5)
        
        self.serpapi_counter = 0
        self.gvision_counter = 0
        self.files_moved = 0
        self.matched_files_moved = 0
        self.unmatched_files_moved = 0
        self.hash_cache = {}

        self.IQDB_LIMIT = "30/min"
        self.SAUCENAO_LIMIT = "4/30s, 100/day"
        self.DANBOORU_LIMIT = "300/min"
        self.GELBOORU_LIMIT = "Gentle (~1-2s)"
        self.YANDERE_LIMIT = "Gentle (~1-2s)"
        self.KONACHAN_LIMIT = "Gentle (~1-2s)"
        self.SERPAPI_LIMIT = "250/month"
        self.GVISION_LIMIT = "1000/month"

    @property
    def iqdb_calls(self): return self.iqdb_limiter.total_calls
    @iqdb_calls.setter
    def iqdb_calls(self, value): self.iqdb_limiter.total_calls = value

    @property
    def saucenao_calls(self): return self.saucenao_limiter.total_calls
    @saucenao_calls.setter
    def saucenao_calls(self, value): self.saucenao_limiter.total_calls = value

    @property
    def danbooru_calls(self): return self.danbooru_limiter.total_calls
    @danbooru_calls.setter
    def danbooru_calls(self, value): self.danbooru_limiter.total_calls = value

    @property
    def gelbooru_calls(self): return self.gelbooru_limiter.total_calls
    @gelbooru_calls.setter
    def gelbooru_calls(self, value): self.gelbooru_limiter.total_calls = value

    @property
    def yandere_calls(self): return self.yandere_limiter.total_calls
    @yandere_calls.setter
    def yandere_calls(self, value): self.yandere_limiter.total_calls = value

    @property
    def konachan_calls(self): return self.konachan_limiter.total_calls
    @konachan_calls.setter
    def konachan_calls(self, value): self.konachan_limiter.total_calls = value

    @property
    def zerochan_calls(self): return self.zerochan_limiter.total_calls
    @zerochan_calls.setter
    def zerochan_calls(self, value): self.zerochan_limiter.total_calls = value

    @property
    def serpapi_calls(self): return self.serpapi_counter
    @serpapi_calls.setter
    def serpapi_calls(self, value): self.serpapi_counter = value

    @property
    def gvision_calls(self): return self.gvision_counter
    @gvision_calls.setter
    def gvision_calls(self, value): self.gvision_counter = value

tracker = ApiTracker()

def normalize_tag(tag_str):
    """Replaces spaces within individual multi-word tags with underscores."""
    if not tag_str:
        return ""
    tags = [t.strip().replace(" ", "_") for t in tag_str.split() if t.strip()]
    return " ".join(tags)

def sanitize_foldername(name):
    """Removes Windows illegal characters: \ / : * ? " < > |"""
    if not name:
        return "Unsorted"
    return re.sub(r'[\\/:*?"<>|]', '', name).strip()

def fetch_google_lens_context(file_path, api_key, log_callback=None):
    """Uploads local image to SerpApi Image API (auto-compressing if >500KB), gets an image_id, then queries Google Lens."""
    if not api_key:
        return ""
    
    upload_path = file_path
    temp_compressed_path = None
    try:
        if os.path.getsize(file_path) > 500 * 1024:
            if log_callback: log_callback("  [SerpApi] Image size exceeds 500KB limit. Optimizing/compressing for upload...")
            img = Image.open(file_path)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            temp_compressed_path = file_path + "_serpapi_temp.jpg"
            
            quality = 90
            while quality >= 20:
                img.save(temp_compressed_path, "JPEG", quality=quality)
                if os.path.getsize(temp_compressed_path) <= 500 * 1024:
                    break
                quality -= 15
            upload_path = temp_compressed_path

        if log_callback: log_callback("  [SerpApi] Uploading image to SerpApi Image API...")
        
        upload_url = "https://serpapi.com/image"
        with open(upload_path, "rb") as img_file:
            upload_response = requests.post(
                upload_url,
                data={"api_key": api_key},
                files={"image": img_file},
                timeout=15
            )
            
        if upload_response.status_code != 200:
            if log_callback: log_callback(f"  [SerpApi Warning] Image upload failed (HTTP {upload_response.status_code}): {upload_response.text}")
            return ""
            
        upload_data = upload_response.json()
        image_id = upload_data.get("image_id")
        if not image_id:
            if log_callback: log_callback(f"  [SerpApi Warning] No image_id returned: {upload_data}")
            return ""
            
        if log_callback: log_callback("  [SerpApi] Querying Google Lens web index using image_id...")
        
        search_url = "https://serpapi.com/search"
        params = {
            "engine": "google_lens",
            "api_key": api_key,
            "image_id": image_id,
            "hl": "en"
        }
        
        response = requests.get(search_url, params=params, timeout=15)
        
        if response.status_code == 200:
            data = response.json()
            snippets = []
            
            kg = data.get("knowledge_graph", [])
            if kg and isinstance(kg, list):
                title = kg[0].get("title", "")
                subtitle = kg[0].get("subtitle", "")
                if title: snippets.append(f"Identified Entity: {title} ({subtitle})")

            visual_matches = data.get("visual_matches", [])
            for match in visual_matches[:5]:
                m_title = match.get("title", "")
                m_source = match.get("source", "")
                if m_title: snippets.append(f"Match: {m_title} (Source: {m_source})")
                
            if snippets:
                context_text = "\n".join(snippets)
                if log_callback: log_callback(f"  [SerpApi SUCCESS] Found {len(snippets)} search context references!")
                return context_text
        else:
            if log_callback: log_callback(f"  [SerpApi Warning] Search failed (HTTP {response.status_code}): {response.text}")
    except Exception as e:
        if log_callback: log_callback(f"  [SerpApi Warning] Search query failed: {e}")
    finally:
        if temp_compressed_path and os.path.exists(temp_compressed_path):
            try: os.remove(temp_compressed_path)
            except: pass
    return ""


def fetch_google_vision_web_detection(file_path, api_key, log_callback=None):
    """Sends local image to Google Cloud Vision Web Detection API and returns entity/matching web snippets."""
    if not api_key:
        return ""
    
    url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
    try:
        if log_callback: log_callback("  [Google Vision] Querying Google Cloud Vision Web Detection API...")
        with open(file_path, "rb") as image_file:
            encoded_image = base64.b64encode(image_file.read()).decode("utf-8")
            
        payload = {
            "requests": [
                {
                    "image": {"content": encoded_image},
                    "features": [{"type": "WEB_DETECTION", "maxResults": 10}]
                }
            ]
        }
        
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code == 200:
            data = response.json()
            responses = data.get("responses", [])
            if not responses:
                return ""
            
            web_det = responses[0].get("webDetection", {})
            snippets = []
            
            best_guess = web_det.get("bestGuessLabels", [])
            if best_guess:
                snippets.append(f"Best Guess: {best_guess[0].get('label')}")
                
            for entity in web_det.get("webEntities", [])[:5]:
                desc = entity.get("description")
                if desc:
                    snippets.append(f"Entity: {desc}")
                    
            if snippets:
                context_text = "\n".join(snippets)
                if log_callback: log_callback(f"  [Google Vision SUCCESS] Found {len(snippets)} search context references!")
                return context_text
        else:
            if log_callback: log_callback(f"  [Google Vision Warning] Request failed (HTTP {response.status_code}): {response.text}")
    except Exception as e:
        if log_callback: log_callback(f"  [Google Vision Warning] Request failed: {e}")
    return ""


class BooruManagerApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Tag-AI Booru Image Sorter")
        self.geometry("1150x960")
        self.minsize(980, 760)

        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.is_processing = False
        self.stop_requested = False
        self.skip_requested = False
        self.file_in_progress = False
        self.target_source = ""
        self.dest_path = os.path.abspath("./organized_library")
        self.unmatched_path = ""
        self.vision_warning_shown = False
        
        self.config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar_container = ctk.CTkFrame(self, width=320, corner_radius=0)
        self.sidebar_container.grid(row=0, column=0, sticky="nsew")
        self.sidebar_container.grid_rowconfigure(1, weight=1)

        self.logo_label = ctk.CTkLabel(self.sidebar_container, text="🏷️ Tag-AI Image Sorter", font=ctk.CTkFont(size=18, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=20, pady=(20, 10))

        self.scroll_frame = ctk.CTkScrollableFrame(self.sidebar_container, width=290, fg_color="transparent")
        self.scroll_frame.grid(row=1, column=0, sticky="nsew", padx=5)

        self.lbl_exec_mode = ctk.CTkLabel(self.scroll_frame, text="Execution Mode:", anchor="w")
        self.lbl_exec_mode.pack(pady=(5, 0), padx=10, fill="x")
        self.opt_exec_mode = ctk.CTkOptionMenu(self.scroll_frame, values=["Automated Full Folder Batch", "Single File Review Mode"], command=self.on_exec_mode_change)
        self.opt_exec_mode.pack(pady=(0, 10), padx=10, fill="x")

        self.btn_select_source = ctk.CTkButton(self.scroll_frame, text="Select Source Folder", command=self.browse_source, fg_color="#3b5bdb", hover_color="#364fc7")
        self.btn_select_source.pack(pady=(5, 2), padx=10, fill="x")
        self.lbl_source_val = ctk.CTkLabel(self.scroll_frame, text="No source selected", font=ctk.CTkFont(size=11), text_color="gray")
        self.lbl_source_val.pack(pady=(0, 10), padx=10, fill="x")

        self.btn_select_dest = ctk.CTkButton(self.scroll_frame, text="Select Destination Library", command=self.browse_dest, fg_color="#3b5bdb", hover_color="#364fc7")
        self.btn_select_dest.pack(pady=(5, 2), padx=10, fill="x")
        self.lbl_dest_val = ctk.CTkLabel(self.scroll_frame, text=f"...{os.path.basename(self.dest_path)}", font=ctk.CTkFont(size=11), text_color="gray")
        self.lbl_dest_val.pack(pady=(0, 10), padx=10, fill="x")

        self.lbl_unmatched = ctk.CTkLabel(self.scroll_frame, text="Action for Unmatched Images:", anchor="w")
        self.lbl_unmatched.pack(pady=(10, 0), padx=10, fill="x")
        self.opt_unmatched = ctk.CTkOptionMenu(self.scroll_frame, values=["Leave in Source Directory", "Move to Designated Folder"], command=self.on_unmatched_change)
        self.opt_unmatched.pack(pady=(0, 5), padx=10, fill="x")

        self.btn_select_unmatched = ctk.CTkButton(self.scroll_frame, text="Select Unmatched Folder", command=self.browse_unmatched, fg_color="#636363", hover_color="#525252")
        self.btn_select_unmatched.pack(pady=(0, 2), padx=10, fill="x")
        self.lbl_unmatched_val = ctk.CTkLabel(self.scroll_frame, text="Default: .../Unmatched", font=ctk.CTkFont(size=11), text_color="gray")
        self.lbl_unmatched_val.pack(pady=(0, 10), padx=10, fill="x")

        self.lbl_engine = ctk.CTkLabel(self.scroll_frame, text="Reverse Search Engine:", anchor="w")
        self.lbl_engine.pack(pady=(10, 0), padx=10, fill="x")
        self.opt_engine = ctk.CTkOptionMenu(self.scroll_frame, values=["IQDB", "SauceNAO", "Local Vision LLM"], command=self.on_engine_change)
        self.opt_engine.pack(pady=(0, 10), padx=10, fill="x")

        self.lbl_tags = ctk.CTkLabel(self.scroll_frame, text="Target Tag Database:", anchor="w")
        self.lbl_tags.pack(pady=(5, 0), padx=10, fill="x")
        self.opt_tags = ctk.CTkOptionMenu(self.scroll_frame, values=["Auto (Highest Confidence)", "Yande.re", "Danbooru", "Gelbooru", "Konachan", "Zerochan"])
        self.opt_tags.pack(pady=(0, 10), padx=10, fill="x")

        self.switch_engine_fallback = ctk.CTkSwitch(self.scroll_frame, text="Enable Search Engine Fallback")
        self.switch_engine_fallback.pack(pady=(5, 5), padx=10, anchor="w")
        self.switch_engine_fallback.select()

        self.switch_vision_fallback = ctk.CTkSwitch(self.scroll_frame, text="Allow Local Vision LLM as Fallback")
        self.switch_vision_fallback.pack(pady=(0, 10), padx=10, anchor="w")
        self.switch_vision_fallback.select()

        # SerpApi Controls
        self.switch_serpapi = ctk.CTkSwitch(self.scroll_frame, text="Enable SerpApi Google Lens")
        self.switch_serpapi.pack(pady=(5, 2), padx=10, anchor="w")

        self.lbl_serpapi_info = ctk.CTkLabel(
            self.scroll_frame, 
            text="Max filesize: 500KB (auto-compressed if larger). Limit: 250 searches/month.", 
            anchor="w", justify="left", font=ctk.CTkFont(size=10), text_color="gray", wraplength=260
        )
        self.lbl_serpapi_info.pack(pady=(0, 4), padx=10, fill="x")

        self.ent_serpapi_key = ctk.CTkEntry(self.scroll_frame, placeholder_text="SerpApi Key (Optional)")
        self.ent_serpapi_key.pack(pady=(0, 10), padx=10, fill="x")

        # Google Cloud Vision API Controls
        self.switch_gvision = ctk.CTkSwitch(self.scroll_frame, text="Enable Google Cloud Vision API")
        self.switch_gvision.pack(pady=(5, 2), padx=10, anchor="w")

        self.lbl_gvision_info = ctk.CTkLabel(
            self.scroll_frame, 
            text="Web Detection. Free limit: 1,000 requests/month.", 
            anchor="w", justify="left", font=ctk.CTkFont(size=10), text_color="gray", wraplength=260
        )
        self.lbl_gvision_info.pack(pady=(0, 4), padx=10, fill="x")

        self.ent_gvision_key = ctk.CTkEntry(self.scroll_frame, placeholder_text="Google Cloud Vision Key (Optional)")
        self.ent_gvision_key.pack(pady=(0, 10), padx=10, fill="x")

        self.switch_skip_on_web_fail = ctk.CTkSwitch(self.scroll_frame, text="Treat as Unmatched if Web Search Fails")
        self.switch_skip_on_web_fail.pack(pady=(0, 15), padx=10, anchor="w")

        self.lbl_subfolder_warning = ctk.CTkLabel(
            self.scroll_frame, 
            text="Note: Scanning subfolders increases context and will need a more powerful LLM.", 
            anchor="w", justify="left", font=ctk.CTkFont(size=11), text_color="gray", wraplength=260
        )
        self.lbl_subfolder_warning.pack(pady=(5, 2), padx=10, fill="x")

        self.switch_scan_subfolders = ctk.CTkSwitch(self.scroll_frame, text="Scan Immediate Subfolders")
        self.switch_scan_subfolders.pack(pady=(0, 10), padx=10, anchor="w")

        self.meta_container = ctk.CTkFrame(self.scroll_frame, fg_color="transparent")
        self.meta_container.pack(pady=(5, 10), padx=10, fill="x")

        self.switch_embed_metadata = ctk.CTkSwitch(self.meta_container, text="Embed Tags in Image Metadata", command=self.on_embed_metadata_toggle)
        self.switch_embed_metadata.pack(anchor="w", pady=(0, 0))

        self.frame_tag_categories = ctk.CTkFrame(self.meta_container, fg_color=("gray90", "gray20"))
        
        self.lbl_cat_title = ctk.CTkLabel(self.frame_tag_categories, text="Include Tag Categories:", font=ctk.CTkFont(size=11, weight="bold"))
        self.lbl_cat_title.pack(pady=(5, 2), padx=10, anchor="w")

        self.switch_meta_artist = ctk.CTkSwitch(self.frame_tag_categories, text="Artist", font=ctk.CTkFont(size=11))
        self.switch_meta_artist.pack(pady=2, padx=15, anchor="w")
        self.switch_meta_artist.select()

        self.switch_meta_series = ctk.CTkSwitch(self.frame_tag_categories, text="Series / Copyright", font=ctk.CTkFont(size=11))
        self.switch_meta_series.pack(pady=2, padx=15, anchor="w")
        self.switch_meta_series.select()

        self.switch_meta_character = ctk.CTkSwitch(self.frame_tag_categories, text="Character", font=ctk.CTkFont(size=11))
        self.switch_meta_character.pack(pady=2, padx=15, anchor="w")
        self.switch_meta_character.select()

        self.switch_meta_general = ctk.CTkSwitch(self.frame_tag_categories, text="General", font=ctk.CTkFont(size=11))
        self.switch_meta_general.pack(pady=2, padx=15, anchor="w")
        self.switch_meta_general.select()

        self.switch_meta_meta = ctk.CTkSwitch(self.frame_tag_categories, text="Meta", font=ctk.CTkFont(size=11))
        self.switch_meta_meta.pack(pady=(2, 8), padx=15, anchor="w")
        self.switch_meta_meta.deselect()

        self.ent_db_user = ctk.CTkEntry(self.scroll_frame, placeholder_text="Danbooru User ID (Optional)")
        self.ent_db_user.pack(pady=5, padx=10, fill="x")
        self.ent_db_key = ctk.CTkEntry(self.scroll_frame, placeholder_text="Danbooru API Key (Optional)")
        self.ent_db_key.pack(pady=(0, 10), padx=10, fill="x")

        self.ent_gb_user = ctk.CTkEntry(self.scroll_frame, placeholder_text="Gelbooru User ID (Optional)")
        self.ent_gb_user.pack(pady=5, padx=10, fill="x")
        self.ent_gb_key = ctk.CTkEntry(self.scroll_frame, placeholder_text="Gelbooru API Key (Optional)")
        self.ent_gb_key.pack(pady=(0, 10), padx=10, fill="x")

        self.ent_skey = ctk.CTkEntry(self.scroll_frame, placeholder_text="SauceNAO API Key (Optional)")
        self.ent_skey.pack(pady=(0, 15), padx=10, fill="x")

        self.lbl_lm = ctk.CTkLabel(self.scroll_frame, text="Local LLM API Base URL:\n(LM Studio / Ollama OpenAI Endpoint)", anchor="w", justify="left", font=ctk.CTkFont(size=11, weight="bold"))
        self.lbl_lm.pack(pady=(5, 2), padx=10, fill="x")
        self.ent_lm = ctk.CTkEntry(self.scroll_frame)
        self.ent_lm.insert(0, "http://localhost:1234/v1")
        self.ent_lm.pack(pady=(0, 10), padx=10, fill="x")

        self.bottom_frame = ctk.CTkFrame(self.sidebar_container, fg_color="transparent")
        self.bottom_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=(10, 20))
        self.bottom_frame.grid_columnconfigure((0, 1), weight=1)

        self.btn_start = ctk.CTkButton(self.bottom_frame, text="Start Processing", command=self.start_thread, fg_color="#2b8a3e", hover_color="#237032")
        self.btn_start.grid(row=0, column=0, padx=5, pady=5, sticky="ew", columnspan=2)

        self.btn_skip = ctk.CTkButton(self.bottom_frame, text="Skip Current File", command=self.skip_current_file_action, fg_color="#e67700", hover_color="#d9480f", state="disabled")
        self.btn_skip.grid(row=1, column=0, padx=5, pady=5, sticky="ew", columnspan=2)

        self.btn_cancel = ctk.CTkButton(self.bottom_frame, text="Cancel Execution", command=self.cancel_processing, fg_color="#c92a2a", hover_color="#a61e1e", state="disabled")
        self.btn_cancel.grid(row=2, column=0, padx=5, pady=5, sticky="ew", columnspan=2)

        self.btn_export = ctk.CTkButton(self.bottom_frame, text="Export Data", command=self.export_settings, fg_color="#495057", hover_color="#343a40")
        self.btn_export.grid(row=3, column=0, padx=5, pady=(10, 0), sticky="ew")

        self.btn_import = ctk.CTkButton(self.bottom_frame, text="Import Data", command=self.import_settings, fg_color="#495057", hover_color="#343a40")
        self.btn_export.grid(row=3, column=0, padx=5, pady=(10, 0), sticky="ew")
        self.btn_import.grid(row=3, column=1, padx=5, pady=(10, 0), sticky="ew")

        self.btn_clear_cache = ctk.CTkButton(self.bottom_frame, text="Clear App Cache & History", command=self.clear_app_cache, fg_color="#d9480f", hover_color="#c92a2a")
        self.btn_clear_cache.grid(row=4, column=0, padx=5, pady=(8, 0), sticky="ew", columnspan=2)

        self.main_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.main_frame.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        self.main_frame.grid_rowconfigure(0, weight=1)
        self.main_frame.grid_columnconfigure(0, weight=1)

        self.tabview = ctk.CTkTabview(self.main_frame)
        self.tabview.grid(row=0, column=0, sticky="nsew")

        self.tab_monitor = self.tabview.add("Console & Monitor")
        self.tab_history = self.tabview.add("History")

        self.tab_monitor.grid_rowconfigure(2, weight=1)
        self.tab_monitor.grid_columnconfigure(0, weight=1)

        self.preview_container = ctk.CTkFrame(self.tab_monitor, height=180)
        self.preview_container.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.preview_container.grid_columnconfigure(2, weight=1)

        self.lbl_source_thumb_title = ctk.CTkLabel(self.preview_container, text="Source Image", font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_source_thumb_title.grid(row=0, column=0, padx=15, pady=(10, 0))
        self.lbl_source_thumb = ctk.CTkLabel(self.preview_container, text="[No Source]", width=120, height=120, fg_color=("gray85", "gray25"))
        self.lbl_source_thumb.grid(row=1, column=0, padx=15, pady=(5, 15))

        self.lbl_match_thumb_title = ctk.CTkLabel(self.preview_container, text="Matched Artwork", font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_match_thumb_title.grid(row=0, column=1, padx=15, pady=(10, 0))
        self.lbl_match_thumb = ctk.CTkLabel(self.preview_container, text="[No Match]", width=120, height=120, fg_color=("gray85", "gray25"))
        self.lbl_match_thumb.grid(row=1, column=1, padx=15, pady=(5, 15))

        self.lbl_details = ctk.CTkLabel(self.preview_container, text="Status: Ready.\nRobust search routing active.", justify="left", font=ctk.CTkFont(size=12), wraplength=250)
        self.lbl_details.grid(row=1, column=2, padx=10, pady=15, sticky="nw")

        self.counter_frame = ctk.CTkFrame(self.tab_monitor, fg_color=("gray90", "gray20"))
        self.counter_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.counter_frame.grid_columnconfigure((0, 1, 2), weight=1)

        self.lbl_iqdb_metric = ctk.CTkLabel(self.counter_frame, text=f"IQDB: 0 (Limit: {tracker.IQDB_LIMIT})", font=ctk.CTkFont(size=11))
        self.lbl_iqdb_metric.grid(row=0, column=0, padx=10, pady=4, sticky="w")

        self.lbl_saucenao_metric = ctk.CTkLabel(self.counter_frame, text=f"SauceNAO: 0 (Limit: {tracker.SAUCENAO_LIMIT})", font=ctk.CTkFont(size=11))
        self.lbl_saucenao_metric.grid(row=0, column=1, padx=10, pady=4, sticky="w")

        self.lbl_danbooru_metric = ctk.CTkLabel(self.counter_frame, text=f"Danbooru: 0 (Limit: {tracker.DANBOORU_LIMIT})", font=ctk.CTkFont(size=11))
        self.lbl_danbooru_metric.grid(row=0, column=2, padx=10, pady=4, sticky="w")

        self.lbl_gelbooru_metric = ctk.CTkLabel(self.counter_frame, text=f"Gelbooru: 0 (Limit: {tracker.GELBOORU_LIMIT})", font=ctk.CTkFont(size=11))
        self.lbl_gelbooru_metric.grid(row=1, column=0, padx=10, pady=4, sticky="w")

        self.lbl_yandere_metric = ctk.CTkLabel(self.counter_frame, text=f"Yande.re: 0 (Limit: {tracker.YANDERE_LIMIT})", font=ctk.CTkFont(size=11))
        self.lbl_yandere_metric.grid(row=1, column=1, padx=10, pady=4, sticky="w")

        self.lbl_konachan_metric = ctk.CTkLabel(self.counter_frame, text=f"Konachan: 0 (Limit: {tracker.KONACHAN_LIMIT})", font=ctk.CTkFont(size=11))
        self.lbl_konachan_metric.grid(row=1, column=2, padx=10, pady=4, sticky="w")

        self.lbl_serpapi_metric = ctk.CTkLabel(self.counter_frame, text=f"SerpApi: 0 (Limit: {tracker.SERPAPI_LIMIT})", font=ctk.CTkFont(size=11))
        self.lbl_serpapi_metric.grid(row=2, column=0, padx=10, pady=4, sticky="w")

        self.lbl_gvision_metric = ctk.CTkLabel(self.counter_frame, text=f"Google Vision: 0 (Limit: {tracker.GVISION_LIMIT})", font=ctk.CTkFont(size=11))
        self.lbl_gvision_metric.grid(row=2, column=1, padx=10, pady=4, sticky="w")

        self.lbl_files_moved = ctk.CTkLabel(self.counter_frame, text="Files Moved This Session: 0", font=ctk.CTkFont(size=11, weight="bold"))
        self.lbl_files_moved.grid(row=3, column=0, columnspan=3, padx=10, pady=4, sticky="w")

        self.txt_console = ctk.CTkTextbox(self.tab_monitor, wrap="word", font=ctk.CTkFont(family="Courier", size=12))
        self.txt_console.grid(row=2, column=0, sticky="nsew")
        self.txt_console.insert("0.0", "System initialized. Universal search fallbacks active.\n")

        self.tab_history.grid_rowconfigure(1, weight=1)
        self.tab_history.grid_columnconfigure(0, weight=1)

        self.history_top_frame = ctk.CTkFrame(self.tab_history, fg_color="transparent")
        self.history_top_frame.grid(row=0, column=0, sticky="ew", padx=5, pady=(5, 5))
        self.history_top_frame.grid_columnconfigure(2, weight=1)

        self.lbl_history_counter = ctk.CTkLabel(self.history_top_frame, text="Processed: 0/0 images", font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_history_counter.grid(row=0, column=0, padx=5, pady=5, sticky="w")

        self.lbl_files_moved_hist = ctk.CTkLabel(self.history_top_frame, text="Files Moved This Session: 0", font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_files_moved_hist.grid(row=0, column=1, padx=15, pady=5, sticky="w")

        self.switch_hide_unmatched = ctk.CTkSwitch(self.history_top_frame, text="Hide Unmatched Files", command=self.toggle_history_filter)

        self.history_scroll = ctk.CTkScrollableFrame(self.tab_history, fg_color="transparent")
        self.history_scroll.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)
        self.history_scroll.grid_columnconfigure(0, weight=1)

        self.history_items = []

        self.on_unmatched_change(self.opt_unmatched.get())
        self.load_settings()

    def on_engine_change(self, choice):
        if choice == "Local Vision LLM" and not self.vision_warning_shown:
            messagebox.showwarning(
                "Experimental Feature Warning",
                "Local Vision LLM tagging is experimental and should only be used with advanced local vision models."
            )
            self.vision_warning_shown = True
            self.save_settings()

    def on_embed_metadata_toggle(self):
        if self.switch_embed_metadata.get() == 1:
            self.frame_tag_categories.pack(pady=(5, 2), padx=0, fill="x")
        else:
            self.frame_tag_categories.pack_forget()

    def update_progress_display(self, current, total):
        def _update():
            self.lbl_history_counter.configure(text=f"Processed: {current}/{total} images")
            self.title(f"Tag-AI {current}/{total}")
        self.after(0, _update)

    def toggle_history_filter(self):
        hide = self.switch_hide_unmatched.get() == 1
        for entry in self.history_items:
            card = entry.get("card")
            if card:
                if hide and entry.get("is_unmatched", False):
                    card.pack_forget()
                else:
                    card.pack(fill="x", pady=5, padx=5)
        self.update_metrics_display()

    def update_history_card_buttons(self):
        show_unmatched_btn = ("Move" in self.opt_unmatched.get())
        for entry in self.history_items:
            btn = entry.get("btn_to_unmatched")
            if btn:
                if show_unmatched_btn and not entry.get("is_unmatched", False):
                    btn.pack(side="left", padx=(0, 5))
                else:
                    btn.pack_forget()

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
            shutil.move(curr, target_file_path)
            
            if not entry.get("is_unmatched", False):
                tracker.matched_files_moved = max(0, tracker.matched_files_moved - 1)
                tracker.unmatched_files_moved += 1

            entry["current_path"] = target_file_path
            entry["is_unmatched"] = True
            
            if "info_lbl" in entry:
                entry["info_lbl"].configure(text=f"File: {filename}\nMoved to: {os.path.dirname(target_file_path)}")
            
            self.log(f"Moved {filename} to Unmatched folder via History action.")
            
            if "btn_to_unmatched" in entry and entry["btn_to_unmatched"]:
                entry["btn_to_unmatched"].pack_forget()
            
            self.update_metrics_display()
            
            if self.switch_hide_unmatched.get() == 1:
                card_frame.pack_forget()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to move file to unmatched folder: {e}")

    def remove_metadata_action(self, entry):
        curr = entry["current_path"]
        if not os.path.exists(curr):
            messagebox.showwarning("File Not Found", "The file no longer exists at its current path.")
            return

        ext = os.path.splitext(curr)[1].lower()
        try:
            img = Image.open(curr)
            if ext == '.png':
                img.save(curr)
            elif ext in ['.jpg', '.jpeg']:
                import piexif
                exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
                exif_bytes = piexif.dump(exif_dict)
                img.save(curr, exif=exif_bytes, quality=95)
            
            self.log(f"Removed embedded metadata from {entry['filename']}")
            messagebox.showinfo("Success", f"Successfully removed embedded tags from {entry['filename']}.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to remove metadata: {e}")

    def add_history_item(self, original_path, current_path, filename, is_unmatched=False, match_thumb_url=""):
        entry = {
            "original_path": original_path,
            "current_path": current_path,
            "filename": filename,
            "is_unmatched": is_unmatched,
            "match_thumb_url": match_thumb_url
        }
        self.history_items.append(entry)
        
        card = ctk.CTkFrame(self.history_scroll, fg_color=("gray92", "gray18"))
        entry["card"] = card

        if not (is_unmatched and self.switch_hide_unmatched.get() == 1):
            card.pack(fill="x", pady=5, padx=5)
            
        card.grid_columnconfigure(2, weight=1)

        thumb_lbl = ctk.CTkLabel(card, text="[No Image]", width=70, height=70, fg_color=("gray80", "gray30"))
        thumb_lbl.grid(row=0, column=0, rowspan=2, padx=(10, 5), pady=10)
        try:
            pil_img = Image.open(current_path)
            pil_img = pil_img.resize((70, 70))
            ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(70, 70))
            thumb_lbl.configure(image=ctk_img, text="")
            thumb_lbl.image = ctk_img
        except Exception:
            pass

        match_thumb_lbl = ctk.CTkLabel(card, text="[No Match]", width=70, height=70, fg_color=("gray80", "gray30"))
        match_thumb_lbl.grid(row=0, column=1, rowspan=2, padx=(0, 10), pady=10)
        if match_thumb_url:
            threading.Thread(target=self.load_history_match_thumb_async, args=(match_thumb_url, match_thumb_lbl), daemon=True).start()
        else:
            match_thumb_lbl.configure(text="[Vision/AI]" if "Vision" in self.opt_engine.get() else "[No Match]")

        info_text = f"File: {filename}\nMoved to: {os.path.dirname(current_path)}"
        info_lbl = ctk.CTkLabel(card, text=info_text, justify="left", font=ctk.CTkFont(size=11))
        info_lbl.grid(row=0, column=2, sticky="nw", padx=5, pady=(10, 0))
        entry["info_lbl"] = info_lbl

        btn_frame = ctk.CTkFrame(card, fg_color="transparent")
        btn_frame.grid(row=1, column=2, sticky="sw", padx=5, pady=(0, 10))

        open_btn = ctk.CTkButton(btn_frame, text="Open Image", width=85, height=28, fg_color="#3b5bdb", hover_color="#364fc7", command=lambda: self.open_file_external(current_path))
        open_btn.pack(side="left", padx=(0, 5))

        folder_btn = ctk.CTkButton(btn_frame, text="Show in Folder", width=95, height=28, fg_color="#495057", hover_color="#343a40", command=lambda: self.open_folder_selected(current_path))
        folder_btn.pack(side="left", padx=(0, 5))

        btn_to_unmatched = ctk.CTkButton(btn_frame, text="To Unmatched", width=95, height=28, fg_color="#e67700", hover_color="#d9480f", command=lambda: self.move_to_unmatched_action(entry, card))
        entry["btn_to_unmatched"] = btn_to_unmatched
        if "Move" in self.opt_unmatched.get() and not is_unmatched:
            btn_to_unmatched.pack(side="left", padx=(0, 5))

        remove_meta_btn = ctk.CTkButton(btn_frame, text="Remove Tags", width=95, height=28, fg_color="#d9480f", hover_color="#c92a2a", command=lambda: self.remove_metadata_action(entry))
        remove_meta_btn.pack(side="left", padx=(0, 5))

        undo_btn = ctk.CTkButton(btn_frame, text="Undo Move", width=85, height=28, fg_color="#c92a2a", hover_color="#a61e1e", command=lambda: self.undo_move(entry, card))
        undo_btn.pack(side="left")

    def load_history_match_thumb_async(self, url, lbl_widget):
        if url.startswith("//"): url = "https:" + url
        elif url.startswith("/") and not url.startswith("//"): return
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            if "zerochan" in url: headers['Referer'] = 'https://www.zerochan.net/'
            elif "gelbooru" in url: headers['Referer'] = 'https://gelbooru.com/'
            elif "danbooru" in url.lower() or "donmai" in url.lower(): headers['Referer'] = 'https://danbooru.donmai.us/'
            elif "yande" in url: headers['Referer'] = 'https://yande.re/'

            res = requests.get(url, headers=headers, timeout=5)
            if res.status_code == 200:
                img_data = BytesIO(res.content)
                pil_img = Image.open(img_data)
                pil_img = pil_img.resize((70, 70))
                ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(70, 70))
                def _apply():
                    lbl_widget.configure(image=ctk_img, text="")
                    lbl_widget.image = ctk_img
                self.after(0, _apply)
        except Exception:
            pass

    def undo_move(self, entry, card_frame):
        curr = entry["current_path"]
        orig = entry["original_path"]
        if os.path.exists(curr):
            try:
                target_dir = os.path.dirname(orig)
                os.makedirs(target_dir, exist_ok=True)
                back_path = os.path.join(target_dir, entry["filename"])
                shutil.move(curr, back_path)
                
                if entry.get("is_unmatched", False):
                    tracker.unmatched_files_moved = max(0, tracker.unmatched_files_moved - 1)
                else:
                    tracker.matched_files_moved = max(0, tracker.matched_files_moved - 1)
                tracker.files_moved = max(0, tracker.files_moved - 1)
                self.update_metrics_display()

                self.log(f"Undo: Moved {entry['filename']} back to {target_dir}")
                card_frame.destroy()
                if entry in self.history_items:
                    self.history_items.remove(entry)
            except Exception as e:
                messagebox.showerror("Undo Error", f"Failed to undo move: {e}")
        else:
            messagebox.showwarning("File Not Found", "The moved file no longer exists at its destination path.")
            card_frame.destroy()
            if entry in self.history_items:
                self.history_items.remove(entry)
            if entry.get("is_unmatched", False):
                tracker.unmatched_files_moved = max(0, tracker.unmatched_files_moved - 1)
            else:
                tracker.matched_files_moved = max(0, tracker.matched_files_moved - 1)
            tracker.files_moved = max(0, tracker.files_moved - 1)
            self.update_metrics_display()

    def clear_app_cache(self):
        if messagebox.askyesno("Confirm Clear Cache", "Are you sure you want to clear the hash cache, API counters, console logs, and history?\n\nYour folders, toggles, and API keys will be preserved."):
            tracker.hash_cache.clear()
            tracker.files_moved = 0
            tracker.matched_files_moved = 0
            tracker.unmatched_files_moved = 0
            
            tracker.iqdb_calls = 0
            tracker.saucenao_calls = 0
            tracker.danbooru_calls = 0
            tracker.gelbooru_calls = 0
            tracker.yandere_calls = 0
            tracker.konachan_calls = 0
            tracker.zerochan_calls = 0
            tracker.serpapi_calls = 0
            tracker.gvision_calls = 0
            
            tracker.iqdb_limiter.window_start = 0
            tracker.iqdb_limiter.window_calls = 0
            for l in tracker.saucenao_limiter.limiters:
                l["start"] = 0
                l["calls"] = 0
            tracker.gelbooru_limiter.last_call_time = 0
            tracker.yandere_limiter.last_call_time = 0
            tracker.konachan_limiter.last_call_time = 0
            tracker.zerochan_limiter.last_call_time = 0

            for entry in self.history_items:
                card = entry.get("card")
                if card:
                    card.destroy()
            self.history_items.clear()
            self.update_progress_display(0, 0)

            self.txt_console.delete("0.0", "end")
            self.txt_console.insert("0.0", "Cache, history, and counters cleared. System ready.\n")

            self.update_metrics_display()
            self.save_settings()
            self.log("App cache, history, logs, and session counters cleared successfully.")

    def save_settings(self):
        config = {
            "exec_mode": self.opt_exec_mode.get(),
            "source_folder": self.target_source,
            "dest_folder": self.dest_path,
            "unmatched_action": self.opt_unmatched.get(),
            "unmatched_folder": self.unmatched_path,
            "engine": self.opt_engine.get(),
            "tags": self.opt_tags.get(),
            "fallback": self.switch_engine_fallback.get(),
            "vision_fallback": self.switch_vision_fallback.get(),
            "serpapi_enabled": self.switch_serpapi.get(),
            "serpapi_key": self.ent_serpapi_key.get(),
            "gvision_enabled": self.switch_gvision.get(),
            "gvision_key": self.ent_gvision_key.get(),
            "skip_on_web_fail": self.switch_skip_on_web_fail.get(),
            "vision_warning_shown": self.vision_warning_shown,
            "scan_subfolders": self.switch_scan_subfolders.get(),
            "embed_metadata": self.switch_embed_metadata.get(),
            "meta_artist": self.switch_meta_artist.get(),
            "meta_series": self.switch_meta_series.get(),
            "meta_character": self.switch_meta_character.get(),
            "meta_general": self.switch_meta_general.get(),
            "meta_meta": self.switch_meta_meta.get(),
            "db_user": self.ent_db_user.get(),
            "db_key": self.ent_db_key.get(),
            "gb_user": self.ent_gb_user.get(),
            "gb_key": self.ent_gb_key.get(),
            "skey": self.ent_skey.get(),
            "lm_url": self.ent_lm.get(),
            "tracker": {
                "iqdb": tracker.iqdb_calls,
                "saucenao": tracker.saucenao_calls,
                "danbooru": tracker.danbooru_calls,
                "gelbooru": tracker.gelbooru_calls,
                "yandere": tracker.yandere_calls,
                "konachan": tracker.konachan_calls,
                "zerochan": tracker.zerochan_calls,
                "serpapi": tracker.serpapi_calls,
                "gvision": tracker.gvision_calls
            }
        }
        try:
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=4)
        except Exception as e:
            self.log(f"Warning: Failed to save config to disk. ({e})")

    def load_settings(self, filepath=None):
        path = filepath or self.config_file
        if not os.path.exists(path): return
        try:
            with open(path, 'r') as f:
                config = json.load(f)
            
            if "exec_mode" in config: self.opt_exec_mode.set(config["exec_mode"])
            if "source_folder" in config:
                self.target_source = config["source_folder"]
                if self.target_source: self.lbl_source_val.configure(text=f"...{os.path.basename(self.target_source)}")
            if "dest_folder" in config:
                self.dest_path = config["dest_folder"]
                if self.dest_path: self.lbl_dest_val.configure(text=f"...{os.path.basename(self.dest_path)}")
            if "unmatched_action" in config: self.opt_unmatched.set(config["unmatched_action"])
            if "unmatched_folder" in config:
                self.unmatched_path = config["unmatched_folder"]
                if self.unmatched_path: self.lbl_unmatched_val.configure(text=f"...{os.path.basename(self.unmatched_path)}")
            
            if "engine" in config: self.opt_engine.set(config["engine"])
            if "tags" in config: self.opt_tags.set(config["tags"])
            if config.get("fallback", 1): self.switch_engine_fallback.select() 
            else: self.switch_engine_fallback.deselect()
            
            if config.get("vision_fallback", 1): self.switch_vision_fallback.select()
            else: self.switch_vision_fallback.deselect()

            if config.get("serpapi_enabled", 0): self.switch_serpapi.select()
            else: self.switch_serpapi.deselect()

            serpapi_key = config.get("serpapi_key", "")
            if serpapi_key:
                self.ent_serpapi_key.delete(0, 'end')
                self.ent_serpapi_key.insert(0, serpapi_key)

            if config.get("gvision_enabled", 0): self.switch_gvision.select()
            else: self.switch_gvision.deselect()

            gvision_key = config.get("gvision_key", "")
            if gvision_key:
                self.ent_gvision_key.delete(0, 'end')
                self.ent_gvision_key.insert(0, gvision_key)

            if config.get("skip_on_web_fail", 0): self.switch_skip_on_web_fail.select()
            else: self.switch_skip_on_web_fail.deselect()

            self.vision_warning_shown = config.get("vision_warning_shown", False)

            if config.get("scan_subfolders", 0): self.switch_scan_subfolders.select()
            else: self.switch_scan_subfolders.deselect()

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
            
            db_user = config.get("db_user", "")
            if db_user:
                self.ent_db_user.delete(0, 'end')
                self.ent_db_user.insert(0, db_user)

            db_key = config.get("db_key", "")
            if db_key:
                self.ent_db_key.delete(0, 'end')
                self.ent_db_key.insert(0, db_key)

            gb_user = config.get("gb_user", "")
            if gb_user:
                self.ent_gb_user.delete(0, 'end')
                self.ent_gb_user.insert(0, gb_user)

            gb_key = config.get("gb_key", "")
            if gb_key:
                self.ent_gb_key.delete(0, 'end')
                self.ent_gb_key.insert(0, gb_key)

            skey = config.get("skey", "")
            if skey:
                self.ent_skey.delete(0, 'end')
                self.ent_skey.insert(0, skey)

            self.ent_lm.delete(0, 'end')
            self.ent_lm.insert(0, config.get("lm_url", "http://localhost:1234/v1"))
            
            t_data = config.get("tracker", {})
            tracker.iqdb_calls = t_data.get("iqdb", 0)
            tracker.saucenao_calls = t_data.get("saucenao", 0)
            tracker.danbooru_calls = t_data.get("danbooru", 0)
            tracker.gelbooru_calls = t_data.get("gelbooru", 0)
            tracker.yandere_calls = t_data.get("yandere", 0)
            tracker.konachan_calls = t_data.get("konachan", 0)
            tracker.zerochan_calls = t_data.get("zerochan", 0)
            tracker.serpapi_calls = t_data.get("serpapi", 0)
            tracker.gvision_calls = t_data.get("gvision", 0)
            self.update_metrics_display()
            
            self.on_unmatched_change(self.opt_unmatched.get())
            self.on_exec_mode_change(self.opt_exec_mode.get())
            self.log("Settings and usage data loaded successfully.")
        except Exception as e:
            self.log(f"Error loading config: {e}")

    def export_settings(self):
        self.save_settings()
        file_path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON Files", "*.json")], title="Export Settings Data")
        if file_path:
            try:
                shutil.copy(self.config_file, file_path)
                self.log(f"Successfully exported data to: {os.path.basename(file_path)}")
            except Exception as e:
                self.log(f"Export failed: {e}")

    def import_settings(self):
        file_path = filedialog.askopenfilename(filetypes=[("JSON Files", "*.json")], title="Import Settings Data")
        if file_path:
            try:
                shutil.copy(file_path, self.config_file)
                self.load_settings()
                self.log(f"Successfully imported data from: {os.path.basename(file_path)}")
            except Exception as e:
                self.log(f"Import failed: {e}")

    def on_closing(self):
        self.save_settings()
        self.destroy()

    def on_exec_mode_change(self, choice):
        if "Folder" in choice:
            self.btn_select_source.configure(text="Select Source Folder")
        else:
            self.btn_select_source.configure(text="Select Individual File")

    def on_unmatched_change(self, choice):
        if "Move" in choice:
            self.btn_select_unmatched.configure(state="normal")
            self.switch_hide_unmatched.grid(row=0, column=2, padx=5, pady=5, sticky="e")
        else:
            self.btn_select_unmatched.configure(state="disabled")
            self.switch_hide_unmatched.grid_remove()
            self.switch_hide_unmatched.deselect()
            self.toggle_history_filter()
        self.update_history_card_buttons()

    def update_metrics_display(self):
        def _update():
            self.lbl_iqdb_metric.configure(text=f"IQDB: {tracker.iqdb_calls} (Limit: {tracker.IQDB_LIMIT})")
            self.lbl_saucenao_metric.configure(text=f"SauceNAO: {tracker.saucenao_calls} (Limit: {tracker.SAUCENAO_LIMIT})")
            self.lbl_danbooru_metric.configure(text=f"Danbooru: {tracker.danbooru_calls} (Limit: {tracker.DANBOORU_LIMIT})")
            self.lbl_gelbooru_metric.configure(text=f"Gelbooru: {tracker.gelbooru_calls} (Limit: {tracker.GELBOORU_LIMIT})")
            self.lbl_yandere_metric.configure(text=f"Yande.re: {tracker.yandere_calls} (Limit: {tracker.YANDERE_LIMIT})")
            self.lbl_konachan_metric.configure(text=f"Konachan: {tracker.konachan_calls} (Limit: {tracker.KONACHAN_LIMIT})")
            self.lbl_serpapi_metric.configure(text=f"SerpApi: {tracker.serpapi_calls} (Limit: {tracker.SERPAPI_LIMIT})")
            self.lbl_gvision_metric.configure(text=f"Google Vision: {tracker.gvision_calls} (Limit: {tracker.GVISION_LIMIT})")
            self.lbl_files_moved.configure(text=f"Files Moved This Session: {tracker.files_moved}")
            
            if self.switch_hide_unmatched.get() == 1:
                self.lbl_files_moved_hist.configure(text=f"Files Moved This Session: {tracker.matched_files_moved}")
            else:
                self.lbl_files_moved_hist.configure(text=f"Files Moved This Session: {tracker.files_moved}")
        self.after(0, _update)

    def browse_source(self):
        mode = self.opt_exec_mode.get()
        if "Folder" in mode:
            folder = filedialog.askdirectory(title="Select Folder Containing Images")
            if folder:
                self.target_source = folder
                self.lbl_source_val.configure(text=f"...{os.path.basename(folder)}")
                self.log(f"Source folder selected: {folder}")
        else:
            file_path = filedialog.askopenfilename(title="Select Image File", filetypes=[("Image Files", "*.jpg *.jpeg *.png *.webp")])
            if file_path:
                self.target_source = file_path
                self.lbl_source_val.configure(text=f"...{os.path.basename(file_path)}")
                self.log(f"Individual file selected: {file_path}")

    def browse_dest(self):
        folder = filedialog.askdirectory(title="Select Organized Destination Library")
        if folder:
            self.dest_path = folder
            self.lbl_dest_val.configure(text=f"...{os.path.basename(folder)}")
            self.log(f"Destination library updated to: {folder}")

    def browse_unmatched(self):
        folder = filedialog.askdirectory(title="Select Target Folder for Unmatched Images")
        if folder:
            self.unmatched_path = folder
            self.lbl_unmatched_val.configure(text=f"...{os.path.basename(folder)}")
            self.log(f"Unmatched destination updated to: {folder}")

    def log(self, message):
        def _log():
            self.txt_console.insert("end", message + "\n")
            self.txt_console.see("end")
        self.after(0, _log)

    def load_local_thumbnail(self, file_path):
        try:
            pil_img = Image.open(file_path)
            pil_img = pil_img.resize((110, 110))
            ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(110, 110))
            def _apply():
                self.lbl_source_thumb.configure(image=ctk_img, text="")
                self.lbl_source_thumb.image = ctk_img
            self.after(0, _apply)
        except Exception:
            self.after(0, lambda: self.lbl_source_thumb.configure(text="[Load Error]", image=""))

    def update_match_thumbnail(self, url):
        if not url: return
        if url.startswith("//"): url = "https:" + url
        elif url.startswith("/") and not url.startswith("//"): return
        
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            if "zerochan" in url: headers['Referer'] = 'https://www.zerochan.net/'
            elif "gelbooru" in url: headers['Referer'] = 'https://gelbooru.com/'
            elif "danbooru" in url.lower() or "donmai" in url.lower(): headers['Referer'] = 'https://danbooru.donmai.us/'
            elif "yande" in url: headers['Referer'] = 'https://yande.re/'
            
            response = requests.get(url, headers=headers, timeout=5)
            if response.status_code == 200:
                img_data = BytesIO(response.content)
                pil_img = Image.open(img_data)
                pil_img = pil_img.resize((110, 110))
                ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(110, 110))
                
                def _apply():
                    self.lbl_match_thumb.configure(image=ctk_img, text="")
                    self.lbl_match_thumb.image = ctk_img
                self.after(0, _apply)
            else:
                def _show_err():
                    if getattr(self.lbl_match_thumb, "image", None) is None:
                        self.lbl_match_thumb.configure(text="[Fetch Error]", image="")
                self.after(0, _show_err)
        except Exception:
            def _show_err_exc():
                if getattr(self.lbl_match_thumb, "image", None) is None:
                    self.lbl_match_thumb.configure(text="[Fetch Error]", image="")
            self.after(0, _show_err_exc)

    def start_thread(self):
        if self.is_processing: return
        if not self.target_source:
            self.log("Error: Please select a source folder or individual file first.")
            return

        self.save_settings()

        mode = self.opt_exec_mode.get()
        if "Folder" in mode and os.path.exists(self.target_source):
            batch_files = [f for f in os.listdir(self.target_source) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
            if len(batch_files) > 25 and "Local Vision" not in self.opt_engine.get():
                proceed = messagebox.askyesno(
                    "Rate Limit Warning", 
                    f"Folder contains {len(batch_files)} images.\n"
                    "Automated pacing delays will be applied to prevent IP throttling.\n"
                    "Proceed with execution?"
                )
                if not proceed: return

        self.is_processing = True
        self.stop_requested = False
        self.skip_requested = False
        self.btn_start.configure(state="disabled", fg_color="gray")
        self.btn_skip.configure(state="normal", fg_color="#e67700")
        self.btn_cancel.configure(state="normal", fg_color="#c92a2a")
        threading.Thread(target=self.run_workflow, daemon=True).start()

    def cancel_processing(self):
        if self.is_processing:
            self.stop_requested = True
            self.log("\n[User Interrupt] Stop requested. Canceling after current item finishes...")
            self.btn_cancel.configure(state="disabled", fg_color="gray")
            self.btn_skip.configure(state="disabled", fg_color="gray")

    def skip_current_file_action(self):
        if self.is_processing and self.file_in_progress:
            self.skip_requested = True
            self.log("\n[User Action] Skip requested for current file...")
            self.btn_skip.configure(state="disabled", fg_color="gray")

    def safe_get_request(self, url, headers=None, auth=None, timeout=10, retries=2):
        for attempt in range(1, retries + 1):
            if self.skip_requested or self.stop_requested: return None
            try:
                return requests.get(url, headers=headers, auth=auth, timeout=timeout)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt < retries: time.sleep(1.0 * attempt)
        return None

    def search_iqdb_engine(self, file_path, is_fallback=False):
        recorded = tracker.iqdb_limiter.record(self.log, block=not is_fallback)
        if not recorded:
            self.log("  [Rate Limit] IQDB fallback on cooldown. Skipping fallback search...")
            return []
        self.update_metrics_display()

        booru_ids = {'danbooru': None, 'gelbooru': None, 'yandere': None, 'konachan': None, 'zerochan': None}
        primary_title, best_thumb = "", ""
        max_similarity = 0.0

        try:
            with open(file_path, 'rb') as f:
                res = requests.post("https://iqdb.org/", files={'file': f}, timeout=15)
                
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                for table in soup.find_all('table'):
                    table_text = table.get_text()
                    pct_matches = re.findall(r'(\d+(?:\.\d+)?)\s*%', table_text)
                    if not pct_matches: continue

                    try: table_sim = max([float(p) for p in pct_matches])
                    except: continue

                    if table_sim < 75.0:
                        self.log(f"  [IQDB Filter] Rejected table match due to low confidence ({table_sim:.1f}%).")
                        continue

                    if table_sim > max_similarity:
                        max_similarity = table_sim
                        img_tag = table.find('img')
                        if img_tag and img_tag.get('src'):
                            best_thumb = img_tag.get('src')
                            if best_thumb.startswith('//'): best_thumb = "https:" + best_thumb
                            elif best_thumb.startswith('/'): best_thumb = "https://iqdb.org" + best_thumb

                    for link in table.find_all('a'):
                        href = link.get('href', '')
                        if href.startswith('//'): href = "https:" + href
                        elif href.startswith('/'): href = "https://iqdb.org" + href

                        if 'danbooru.donmai.us/posts/' in href and not booru_ids['danbooru']:
                            for p in href.split('/'):
                                if p.isdigit(): booru_ids['danbooru'] = p; break
                            if not primary_title: primary_title = href
                        elif 'gelbooru.com' in href and 'id=' in href and not booru_ids['gelbooru']:
                            try: booru_ids['gelbooru'] = href.split('id=')[-1].split('&')[0]
                            except: pass
                            if not primary_title: primary_title = href
                        elif 'yande.re/post/show/' in href and not booru_ids['yandere']:
                            for p in href.split('/'):
                                if p.isdigit(): booru_ids['yandere'] = p; break
                            if not primary_title: primary_title = href
                        elif 'konachan.com/post/show/' in href and not booru_ids['konachan']:
                            for p in href.split('/'):
                                if p.isdigit(): booru_ids['konachan'] = p; break
                            if not primary_title: primary_title = href
                        elif 'zerochan.net/' in href and not booru_ids['zerochan']:
                            try:
                                z_id = href.split('/')[-1]
                                if z_id.isdigit(): booru_ids['zerochan'] = z_id
                            except: pass
                            if not primary_title: primary_title = href

            if max_similarity >= 75.0 and (any(booru_ids.values()) or primary_title):
                return [{'engine': 'IQDB', 'title': primary_title, 'similarity': max_similarity, 'booru_ids': booru_ids, 'thumb': best_thumb}]
        except Exception as e:
            self.log(f"  [IQDB Error]: {e}")
        return []

    def search_saucenao_engine(self, file_path, api_key, is_fallback=False):
        recorded = tracker.saucenao_limiter.record(self.log, block=not is_fallback)
        if not recorded:
            self.log("  [Rate Limit] SauceNAO fallback on cooldown. Skipping fallback search...")
            return []
        self.update_metrics_display()

        booru_ids = {'danbooru': None, 'gelbooru': None, 'yandere': None, 'konachan': None, 'zerochan': None}
        primary_title, best_thumb = "", ""
        max_similarity = 0.0

        try:
            params = {'output_type': 2, 'numres': 5, 'hide': 0, 'api_key': api_key}
            with open(file_path, 'rb') as f:
                res = requests.post("https://saucenao.com/search.php", params=params, files={'file': f}, timeout=15)
                
            if res.status_code == 200:
                results = res.json().get('results', [])
                for r in results:
                    data = r.get('data', {})
                    header = r.get('header', {})
                    
                    try:
                        sim_val = float(header.get('similarity', 0))
                        if sim_val > max_similarity:
                            max_similarity = sim_val
                            best_thumb = header.get('thumbnail', '')
                    except: pass

                    if not primary_title: primary_title = data.get('title') or data.get('source', 'SauceNAO Match')

                    if data.get('danbooru_id'): booru_ids['danbooru'] = str(data.get('danbooru_id'))
                    if data.get('gelbooru_id'): booru_ids['gelbooru'] = str(data.get('gelbooru_id'))
                    if data.get('yandere_id'): booru_ids['yandere'] = str(data.get('yandere_id'))
                    if data.get('konachan_id'): booru_ids['konachan'] = str(data.get('konachan_id'))

                    for url in data.get('ext_urls', []):
                        if 'zerochan.net' in url and not booru_ids['zerochan']:
                            z_id = url.split('/')[-1]
                            if z_id.isdigit(): booru_ids['zerochan'] = z_id

                if any(booru_ids.values()) or primary_title:
                    return [{'engine': 'SauceNAO', 'title': primary_title, 'similarity': max_similarity, 'booru_ids': booru_ids, 'thumb': best_thumb}]
        except Exception as e:
            self.log(f"  [SauceNAO Error]: {e}")
        return []

    def search_local_vision_engine(self, file_path, client, model_id):
        self.log("  [Local Vision LLM] Analyzing image with vision model...")
        def _set_vision_status():
            self.lbl_match_thumb.configure(text="[Vision AI]", image="")
            self.lbl_match_thumb.image = None
            self.lbl_details.configure(text=f"File: {os.path.basename(file_path)}\nStatus: Querying Local Vision LLM...")
        self.after(0, _set_vision_status)
        
        try:
            web_context = ""
            web_api_enabled_count = 0

            # 1. Try SerpApi if enabled
            if self.switch_serpapi.get() == 1:
                serpapi_key = self.ent_serpapi_key.get().strip()
                if serpapi_key:
                    web_api_enabled_count += 1
                    web_context = fetch_google_lens_context(file_path, serpapi_key, self.log)
                    tracker.serpapi_calls += 1
                    self.update_metrics_display()

            if self.skip_requested: return []

            # 2. Try Google Cloud Vision API if SerpApi didn't run/succeed and Google Vision is enabled
            if not web_context and self.switch_gvision.get() == 1:
                gvision_key = self.ent_gvision_key.get().strip()
                if gvision_key:
                    web_api_enabled_count += 1
                    web_context = fetch_google_vision_web_detection(file_path, gvision_key, self.log)
                    tracker.gvision_calls += 1
                    self.update_metrics_display()

            if self.skip_requested: return []

            # Check if web search APIs were enabled/configured but failed to find any results
            if web_api_enabled_count > 0 and not web_context and self.switch_skip_on_web_fail.get() == 1:
                self.log("  [Web Search] Enabled web search API(s) returned no results or failed. Skipping local vision tagging (Treating as Unmatched).")
                return []

            with open(file_path, "rb") as image_file:
                encoded_image = base64.b64encode(image_file.read()).decode('utf-8')
            
            ext = os.path.splitext(file_path)[1].lower()
            mime_type = "image/png" if ext == ".png" else "image/jpeg"

            context_instruction = ""
            if web_context:
                context_instruction = (
                    f"\n\nAdditional Web Search Hints (Web Detection / Lens Results):\n{web_context}\n"
                    "Use these web search hints to help identify character and series/copyright names accurately."
                )

            system_p = (
                "You are an expert image tagging assistant. Analyze the provided image and generate Danbooru-style tags. "
                "Structure individual multi-word tags using underscores (e.g., hatsune_miku, blue_hair, 1girl) rather than spaces, "
                "and separate each tag with a space.\n\n"
                f"{context_instruction}\n\n"
                "Group tags strictly into these 5 category fields using JSON output:\n"
                "{\n"
                '  "artist": "artist_name",\n'
                '  "copyright": "series_name or franchise",\n'
                '  "character": "character_name_1 character_name_2",\n'
                '  "general": "tag_1 tag_2 tag_3",\n'
                '  "meta": "high_res digital_media"\n'
                "}\n"
                "If a category is unknown or not present, leave its string empty. Output ONLY valid raw JSON."
            )
            user_p = [
                {"type": "text", "text": "Extract all accurate categorized tags for this image:"},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded_image}"}}
            ]

            response = client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": system_p},
                    {"role": "user", "content": user_p}
                ],
                temperature=0.1,
                max_tokens=500
            )
            raw_res = response.choices[0].message.content.strip()
            if raw_res.startswith("```"):
                raw_res = raw_res.split("```")[1]
                if raw_res.startswith("json") or raw_res.startswith("text"):
                    raw_res = raw_res[4:]
            
            try:
                parsed_json = json.loads(raw_res.strip())
                cat_tag_dict = {
                    'artist': normalize_tag(parsed_json.get('artist', '')),
                    'copyright': normalize_tag(parsed_json.get('copyright', '')),
                    'character': normalize_tag(parsed_json.get('character', '')),
                    'general': normalize_tag(parsed_json.get('general', '')),
                    'meta': normalize_tag(parsed_json.get('meta', ''))
                }
                combined_list = [v for v in cat_tag_dict.values() if v]
                cat_tag_dict['combined'] = " ".join(combined_list).strip()
            except Exception:
                norm_str = normalize_tag(raw_res)
                cat_tag_dict = {'artist': '', 'copyright': '', 'character': '', 'general': norm_str, 'meta': '', 'combined': norm_str}

            if cat_tag_dict['combined']:
                self.log(f"  [Local Vision LLM SUCCESS] Generated {len(cat_tag_dict['combined'].split())} tags!")
                return [{
                    'engine': 'Local Vision LLM',
                    'title': 'Local Vision AI Tags',
                    'similarity': 100.0,
                    'booru_ids': {},
                    'thumb': '',
                    'tag_dict_override': cat_tag_dict
                }]
        except Exception as e:
            self.log(f"  [Local Vision LLM Error]: {e}")
        return []

    def fetch_tags(self, file_path, match_meta, primary_choice, db_user, db_key, gb_user, gb_key):
        tag_data = {'artist': '', 'copyright': '', 'character': '', 'general': '', 'meta': '', 'combined': ''}
        booru_ids = match_meta.get('booru_ids', {}) if match_meta else {}
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        db_headers = {'User-Agent': f"TagAIBooruSorter/1.0 (by {db_user if db_user else 'anonymous_user'})"}
        confidence_order = ["danbooru", "yandere", "gelbooru", "konachan", "zerochan"]

        target_key = None
        if "Danbooru" in primary_choice: target_key = "danbooru"
        elif "Yande.re" in primary_choice: target_key = "yandere"
        elif "Gelbooru" in primary_choice: target_key = "gelbooru"
        elif "Konachan" in primary_choice: target_key = "konachan"
        elif "Zerochan" in primary_choice: target_key = "zerochan"

        if target_key and target_key in confidence_order:
            confidence_order.remove(target_key)
            confidence_order.insert(0, target_key)

        for source in confidence_order:
            if self.stop_requested or self.skip_requested: break
            ident = booru_ids.get(source)
            if not ident: continue
            
            db_display_name = {"danbooru": "Danbooru", "yandere": "Yande.re", "gelbooru": "Gelbooru", "konachan": "Konachan", "zerochan": "Zerochan"}.get(source, source.capitalize())

            try:
                if source == 'danbooru':
                    tracker.danbooru_limiter.record(self.log)
                    self.update_metrics_display()
                    status_msg = f"Querying {db_display_name}...\nID: {ident}"
                    self.after(0, lambda msg=status_msg: self.lbl_details.configure(text=msg))
                    self.log(f"  Querying Danbooru ID: {ident}...")
                    auth = (db_user, db_key) if (db_user and db_key) else None
                    res = self.safe_get_request(f"https://danbooru.donmai.us/posts/{ident}.json", headers=db_headers, auth=auth, timeout=6)
                    if res and res.status_code == 200:
                        pdata = res.json()
                        preview_url = pdata.get('preview_file_url') or pdata.get('large_file_url')
                        if preview_url: self.update_match_thumbnail(preview_url)
                        
                        tag_data['artist'] = normalize_tag(pdata.get('tag_string_artist', ''))
                        tag_data['copyright'] = normalize_tag(pdata.get('tag_string_copyright', ''))
                        tag_data['character'] = normalize_tag(pdata.get('tag_string_character', ''))
                        tag_data['general'] = normalize_tag(pdata.get('tag_string_general', ''))
                        tag_data['meta'] = normalize_tag(pdata.get('tag_string_meta', ''))
                        tag_data['combined'] = normalize_tag(pdata.get('tag_string', ''))
                        
                        if tag_data['combined']:
                            self.log(f"  [{db_display_name} API SUCCESS] Retrieved {len(tag_data['combined'].split())} tags!")
                            match_msg = f"Match Found\n({db_display_name} ID: {ident})"
                            self.after(0, lambda msg=match_msg: self.lbl_details.configure(text=msg))
                            return tag_data
                    else:
                        self.log("  [Danbooru API Failed] Attempting HTML scraping fallback...")
                        res_html = self.safe_get_request(f"https://danbooru.donmai.us/posts/{ident}", headers=db_headers, timeout=6)
                        if res_html and res_html.status_code == 200:
                            soup = BeautifulSoup(res_html.text, 'html.parser')
                            tag_elements = soup.find_all('a', class_='search-tag')
                            if tag_elements:
                                raw_str = normalize_tag(" ".join([a.text.strip() for a in tag_elements]))
                                tag_data['general'] = raw_str
                                tag_data['combined'] = raw_str
                                self.log(f"  [{db_display_name} Scraping SUCCESS] Retrieved {len(raw_str.split())} tags!")
                                match_msg = f"Match Found\n({db_display_name} ID: {ident})"
                                self.after(0, lambda msg=match_msg: self.lbl_details.configure(text=msg))
                                return tag_data

                elif source in ['yandere', 'konachan', 'gelbooru', 'zerochan']:
                    if source == 'yandere':
                        tracker.yandere_limiter.record(self.log)
                        url = f"https://yande.re/post.json?tags=id:{ident}"
                    elif source == 'konachan':
                        tracker.konachan_limiter.record(self.log)
                        url = f"https://konachan.com/post.json?tags=id:{ident}"
                    elif source == 'gelbooru':
                        tracker.gelbooru_limiter.record(self.log)
                        auth_params = f"&api_key={gb_key}&user_id={gb_user}" if (gb_user and gb_key) else ""
                        url = f"https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&id={ident}{auth_params}"
                    else:
                        tracker.zerochan_limiter.record(self.log)
                        url = f"https://www.zerochan.net/{ident}?json"

                    self.update_metrics_display()
                    status_msg = f"Querying {db_display_name}...\nID: {ident}"
                    self.after(0, lambda msg=status_msg: self.lbl_details.configure(text=msg))
                    self.log(f"  Querying {db_display_name} ID: {ident}...")
                    
                    res = self.safe_get_request(url, headers=headers, timeout=6)
                    if res and res.status_code == 200:
                        if source == 'zerochan':
                            pdata = res.json()
                            preview_url = pdata.get('thumbnail') or pdata.get('large') or pdata.get('full')
                            if preview_url: self.update_match_thumbnail(preview_url)
                            tags_list = pdata.get('tags', [])
                            if tags_list:
                                raw_str = normalize_tag(" ".join(tags_list))
                                tag_data['general'] = raw_str
                                tag_data['combined'] = raw_str
                                self.log(f"  [{db_display_name} SUCCESS] Retrieved {len(tags_list)} tags!")
                                return tag_data
                        else:
                            data = res.json()
                            posts = data.get('post', []) if isinstance(data, dict) and source == 'gelbooru' else data
                            if isinstance(posts, dict): posts = [posts]
                            if posts:
                                p0 = posts[0]
                                preview_url = p0.get('preview_url') or p0.get('sample_url')
                                if preview_url: self.update_match_thumbnail(preview_url)
                                t_str = normalize_tag(p0.get('tags', ''))
                                if t_str:
                                    tag_data['general'] = t_str
                                    tag_data['combined'] = t_str
                                    self.log(f"  [{db_display_name} SUCCESS] Retrieved {len(t_str.split())} tags!")
                                    return tag_data
            except: pass

        return tag_data

    def embed_metadata(self, file_path, tag_dict):
        if not self.switch_embed_metadata.get() == 1:
            return

        parts = []
        if self.switch_meta_artist.get() and tag_dict.get('artist'):
            parts.append(tag_dict['artist'])
        if self.switch_meta_series.get() and tag_dict.get('copyright'):
            parts.append(tag_dict['copyright'])
        if self.switch_meta_character.get() and tag_dict.get('character'):
            parts.append(tag_dict['character'])
        if self.switch_meta_general.get() and tag_dict.get('general'):
            parts.append(tag_dict['general'])
        if self.switch_meta_meta.get() and tag_dict.get('meta'):
            parts.append(tag_dict['meta'])

        final_string = " ".join([p for p in parts if p]).strip()
        if not final_string:
            return

        ext = os.path.splitext(file_path)[1].lower()
        try:
            img = Image.open(file_path)
            if ext == '.png':
                metadata = PngImagePlugin.PngInfo()
                metadata.add_text("parameters", final_string)
                metadata.add_text("booru_tags", final_string)
                img.save(file_path, pnginfo=metadata)
            elif ext in ['.jpg', '.jpeg']:
                try:
                    import piexif
                    from piexif.helper import UserComment
                    
                    comment_bytes = UserComment.dump(final_string, encoding="unicode")
                    exif_dict = {"0th": {}, "Exif": {piexif.ExifIFD.UserComment: comment_bytes}, "GPS": {}, "1st": {}, "thumbnail": None}
                    exif_bytes = piexif.dump(exif_dict)
                    
                    img.save(file_path, exif=exif_bytes, quality=95)
                except ImportError:
                    self.log("  [Metadata Error] 'piexif' library not installed. Run 'pip install piexif'.")
                    img.save(file_path, quality=95)
                except Exception as ex_err:
                    self.log(f"  [Metadata Error] Failed to write JPEG EXIF: {ex_err}")
                    img.save(file_path, quality=95)
            
            self.log(f"  [Metadata] Successfully embedded tags into {os.path.basename(file_path)}")
        except Exception as e:
            self.log(f"  [Metadata Error]: {e}")

    def run_workflow(self):
        lm_url = self.ent_lm.get().strip()
        engine_choice = self.opt_engine.get()
        tag_choice = self.opt_tags.get()
        exec_mode = self.opt_exec_mode.get()
        saucenao_key = self.ent_skey.get().strip()
        db_user = self.ent_db_user.get().strip()
        db_key = self.ent_db_key.get().strip()
        gb_user = self.ent_gb_user.get().strip()
        gb_key = self.ent_gb_key.get().strip()
        allow_engine_fallback = bool(self.switch_engine_fallback.get())
        allow_vision_fallback = bool(self.switch_vision_fallback.get())

        image_files = []
        if "Single" in exec_mode:
            if os.path.exists(self.target_source): image_files = [self.target_source]
        else:
            if os.path.exists(self.target_source):
                image_files = [os.path.join(self.target_source, f) for f in os.listdir(self.target_source) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]

        if not image_files:
            self.log("Error: No valid image files found at target source path.")
            self.is_processing = False
            def _reset_early():
                self.btn_start.configure(state="normal", fg_color="#2b8a3e")
                self.btn_skip.configure(state="disabled", fg_color="gray")
                self.btn_cancel.configure(state="disabled", fg_color="gray")
                self.title("Tag-AI Booru Image Sorter")
            self.after(0, _reset_early)
            return

        self.log(f"\n--- Connecting to Local LLM Base URL at {lm_url} ---")
        client = OpenAI(base_url=lm_url, api_key="lm-studio")
        try:
            models = client.models.list()
            model_id = models.data[0].id
            self.log(f"Connected! Active model loaded: {model_id}")
        except Exception as e:
            self.log(f"Warning: Could not fetch models list. Using fallback ID.")
            model_id = "local-model"

        total_files = len(image_files)
        self.log(f"Starting execution of {total_files} item(s) in [{exec_mode}]...")

        for idx, file_path in enumerate(image_files, 1):
            if self.stop_requested: break
            self.skip_requested = False
            self.file_in_progress = True
            
            # Watchdog thread to check if file processing takes > 60 seconds
            file_start_time = time.time()
            stuck_warning_logged = [False]
            def check_stuck():
                while self.is_processing and self.file_in_progress and not self.skip_requested and not self.stop_requested:
                    if time.time() - file_start_time > 60 and not stuck_warning_logged[0]:
                        stuck_warning_logged[0] = True
                        self.log(f"  [Warning] Current file '{os.path.basename(file_path)}' has been stuck for over a minute! Click 'Skip Current File' if unresponsive.")
                        break
                    time.sleep(1.0)
            watchdog_thread = threading.Thread(target=check_stuck, daemon=True)
            watchdog_thread.start()

            self.update_progress_display(idx, total_files)
            
            filename = os.path.basename(file_path)
            self.log(f"Scanning file: {filename}")
            
            with open(file_path, 'rb') as f:
                file_hash = hashlib.md5(f.read()).hexdigest()
            
            if file_hash in tracker.hash_cache:
                self.log("  [MD5 Cache Hit] Bypassing APIs, using cached resolution.")
                cached_data = tracker.hash_cache[file_hash]
                target_folder = cached_data['folder']
                tag_dict = cached_data.get('tag_dict', {'combined': cached_data.get('tags', '')})
                cached_thumb = cached_data.get('thumb', '')
                
                final_dest_path = os.path.join(self.dest_path, target_folder)
                os.makedirs(final_dest_path, exist_ok=True)
                target_file_path = os.path.join(final_dest_path, filename)
                shutil.move(file_path, target_file_path)
                
                self.embed_metadata(target_file_path, tag_dict)
                self.log(f"  -> Moved into: /{target_folder}/{filename} (Cached)\n")
                
                tracker.files_moved += 1
                tracker.matched_files_moved += 1
                self.update_metrics_display()
                self.after(0, lambda p=file_path, tp=target_file_path, f=filename, th=cached_thumb: self.add_history_item(p, tp, f, is_unmatched=False, match_thumb_url=th))
                
                self.file_in_progress = False
                if "Single" in exec_mode: break
                else: 
                    time.sleep(0.1)
                    continue
            
            def _reset_match_thumb():
                self.lbl_match_thumb.configure(text="[Searching...]", image="")
                self.lbl_match_thumb.image = None
            self.after(0, _reset_match_thumb)
            self.after(0, lambda: self.lbl_details.configure(text=f"File: {filename}\nStatus: Searching..."))
            self.load_local_thumbnail(file_path)

            matches = []
            if not self.skip_requested and not self.stop_requested:
                if "IQDB" in engine_choice:
                    matches = self.search_iqdb_engine(file_path, is_fallback=False)
                    if not matches and allow_engine_fallback and not self.stop_requested and not self.skip_requested:
                        self.log("  IQDB returned no match. [Engine Fallback -> SauceNAO]...")
                        matches = self.search_saucenao_engine(file_path, saucenao_key, is_fallback=True)
                    if not matches and allow_vision_fallback and not self.stop_requested and not self.skip_requested:
                        self.log("  Traditional search failed. [Engine Fallback -> Local Vision LLM]...")
                        matches = self.search_local_vision_engine(file_path, client, model_id)
                elif "SauceNAO" in engine_choice:
                    matches = self.search_saucenao_engine(file_path, saucenao_key, is_fallback=False)
                    if not matches and allow_engine_fallback and not self.stop_requested and not self.skip_requested:
                        self.log("  SauceNAO returned no match. [Engine Fallback -> IQDB]...")
                        matches = self.search_iqdb_engine(file_path, is_fallback=True)
                    if not matches and allow_vision_fallback and not self.stop_requested and not self.skip_requested:
                        self.log("  Traditional search failed. [Engine Fallback -> Local Vision LLM]...")
                        matches = self.search_local_vision_engine(file_path, client, model_id)
                else: # Local Vision LLM selected as primary engine
                    matches = self.search_local_vision_engine(file_path, client, model_id)

            if self.skip_requested:
                self.log(f"  [Skipped] User requested skip for {filename}.\n")
                self.file_in_progress = False
                self.btn_skip.configure(state="normal", fg_color="#e67700")
                continue

            if self.stop_requested:
                self.file_in_progress = False
                break

            chosen_match = matches[0] if matches else None
            tag_dict = {}
            formatted_tags = ""
            match_thumb = ""

            if chosen_match:
                eng_name = chosen_match.get('engine', engine_choice)
                sim_score = chosen_match.get('similarity', 0.0)
                sim_str = f" ({eng_name} {sim_score:.1f}% Similarity)" if sim_score > 0 else f" ({eng_name} Match)"
                self.log(f"  Match Confirmed{sim_str}: {chosen_match['title']}")
                match_thumb = chosen_match.get('thumb', '')
                if match_thumb: self.update_match_thumbnail(match_thumb)
                self.after(0, lambda: self.lbl_details.configure(text=f"File: {filename}\nStatus: Search Match Confirmed"))
                
                if 'tag_dict_override' in chosen_match:
                    tag_dict = chosen_match['tag_dict_override']
                    formatted_tags = tag_dict.get('combined', '')
                else:
                    tag_dict = self.fetch_tags(file_path, chosen_match, tag_choice, db_user, db_key, gb_user, gb_key)
                    formatted_tags = tag_dict.get('combined', '')
            else:
                self.log("  Search engine returned no matches.")
                def _clear_match_thumb():
                    self.lbl_match_thumb.configure(text="[No Match]", image="")
                    self.lbl_match_thumb.image = None
                self.after(0, _clear_match_thumb)
                self.after(0, lambda: self.lbl_details.configure(text=f"File: {filename}\nStatus: Unmatched"))

            if self.skip_requested:
                self.log(f"  [Skipped] User requested skip for {filename}.\n")
                self.file_in_progress = False
                self.btn_skip.configure(state="normal", fg_color="#e67700")
                continue

            if self.stop_requested:
                self.file_in_progress = False
                break

            is_unmatched = False
            if not chosen_match:
                is_unmatched = True
            elif not formatted_tags and not chosen_match.get('title'):
                is_unmatched = True

            if is_unmatched:
                action = self.opt_unmatched.get()
                if "Leave" in action:
                    self.log(f"  [Unmatched] Leaving {filename} in source directory.\n")
                else:
                    dest_unmatched = self.unmatched_path
                    if not dest_unmatched or not os.path.exists(dest_unmatched):
                        dest_unmatched = os.path.join(self.dest_path, "Unmatched") if self.dest_path else os.path.abspath("./Unmatched")
                    
                    os.makedirs(dest_unmatched, exist_ok=True)
                    target_file_path = os.path.join(dest_unmatched, filename)
                    shutil.move(file_path, target_file_path)
                    self.log(f"  -> Moved into Unmatched Folder: {target_file_path}\n")
                    
                    tracker.files_moved += 1
                    tracker.unmatched_files_moved += 1
                    self.update_metrics_display()
                    self.after(0, lambda p=file_path, tp=target_file_path, f=filename, th=match_thumb: self.add_history_item(p, tp, f, is_unmatched=True, match_thumb_url=th))

                self.file_in_progress = False
                self.btn_skip.configure(state="normal", fg_color="#e67700")
                if "Single" in exec_mode: break
                else: 
                    time.sleep(1.0)
                    continue

            if not formatted_tags and chosen_match:
                self.log("  [Warning] No tags extracted from any booru. Defaulting to HTML Title for routing.")
                formatted_tags = normalize_tag(chosen_match.get('title', ''))
                tag_dict['combined'] = formatted_tags
                tag_dict['general'] = formatted_tags

            if formatted_tags:
                tag_list = formatted_tags.split()
                if len(tag_list) > 400:
                    formatted_tags = " ".join(tag_list[:400])

            available_folders = []
            if not os.path.exists(self.dest_path): os.makedirs(self.dest_path)
            
            scan_subfolders = bool(self.switch_scan_subfolders.get() == 1)

            for item in os.listdir(self.dest_path):
                full_item_path = os.path.join(self.dest_path, item)
                if os.path.isdir(full_item_path) and item not in ["Unmatched", "Unsorted"]:
                    available_folders.append(item)
                    if scan_subfolders:
                        try:
                            for subitem in os.listdir(full_item_path):
                                full_subitem_path = os.path.join(full_item_path, subitem)
                                if os.path.isdir(full_subitem_path):
                                    available_folders.append(f"{item}/{subitem}")
                        except Exception:
                            pass

            target_folder = "Unsorted"
            if available_folders and formatted_tags:
                try:
                    system_p = (
                        "You are a strict file router. Analyze the tags and select the single best matching "
                        "folder path strictly from the provided available folders list. "
                        "If no folder matches well, you MUST output 'Unsorted'. Do not invent or create new folder names. "
                        "Output ONLY valid JSON in this exact format: {\"destination\": \"FolderPath\"}"
                    )
                    user_p = f"Tags: {formatted_tags}\nFolders:\n{json.dumps(available_folders, indent=2)}"
                    
                    response = client.chat.completions.create(
                        model=model_id,
                        messages=[{"role": "system", "content": system_p}, {"role": "user", "content": user_p}],
                        temperature=0.0
                    )
                    content = response.choices[0].message.content.strip()
                    if content.startswith("```"):
                        content = content.split("```")[1]
                        if content.startswith("json"): content = content[4:]
                    res_json = json.loads(content.strip())
                    raw_dest = res_json.get("destination", "Unsorted")
                    target_folder = sanitize_foldername(raw_dest)
                    
                    # PROGRAMMATIC SAFEGUARD: Enforce that folder exists in library
                    if target_folder not in available_folders and target_folder != "Unsorted":
                        self.log(f"  [Router Safeguard] LLM suggested unlisted folder '{target_folder}'. Forcing to 'Unsorted'.")
                        target_folder = "Unsorted"
                        
                except Exception as llm_err:
                    self.log(f"  [LLM Error]: {llm_err}")
                    target_folder = "Unsorted"

            final_dest_path = os.path.join(self.dest_path, target_folder)
            os.makedirs(final_dest_path, exist_ok=True)
            target_file_path = os.path.join(final_dest_path, filename)
            shutil.move(file_path, target_file_path)
            
            self.embed_metadata(target_file_path, tag_dict)

            if formatted_tags and target_folder != "Unsorted":
                tracker.hash_cache[file_hash] = {'tags': formatted_tags, 'folder': target_folder, 'tag_dict': tag_dict, 'thumb': match_thumb}
            
            tracker.files_moved += 1
            tracker.matched_files_moved += 1
            self.update_metrics_display()
            
            self.log(f"  -> Moved into: /{target_folder}/{filename}\n")
            self.after(0, lambda p=file_path, tp=target_file_path, f=filename, th=match_thumb: self.add_history_item(p, tp, f, is_unmatched=False, match_thumb_url=th))

            self.file_in_progress = False
            self.btn_skip.configure(state="normal", fg_color="#e67700")

            if "Single" in exec_mode:
                self.log("Paused after single file (Single File Review Mode active).")
                break
            else:
                time.sleep(1.0)

        self.log("=== Execution Queue Stopped/Complete ==.")
        self.save_settings()
        self.is_processing = False
        self.stop_requested = False
        self.skip_requested = False
        self.file_in_progress = False
        def _reset_final():
            self.btn_start.configure(state="normal", fg_color="#2b8a3e")
            self.btn_skip.configure(state="disabled", fg_color="gray")
            self.btn_cancel.configure(state="disabled", fg_color="gray")
            self.title("Tag-AI Booru Image Sorter")
        self.after(0, _reset_final)

if __name__ == "__main__":
    app = BooruManagerApp()
    app.mainloop()