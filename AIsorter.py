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
import sys
import webbrowser
from io import BytesIO
from PIL import Image, PngImagePlugin
from openai import OpenAI
import customtkinter as ctk
from tkinter import filedialog, messagebox
from bs4 import BeautifulSoup

# Fix window dragging lag across monitors with different DPI scalings on Windows
if platform.system() == "Windows":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

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


class WindowRateLimiter:
    def __init__(self, limit, period_seconds):
        self.limit = limit
        self.period_seconds = period_seconds
        self.window_start = time.time()
        self.window_calls = 0
        self.total_calls = 0

    def get_current_window_calls(self):
        now = time.time()
        if now - self.window_start >= self.period_seconds:
            self.window_start = now
            self.window_calls = 0
        return self.window_calls

    def record(self, log_callback=None, block=True):
        now = time.time()
        if now - self.window_start >= self.period_seconds:
            self.window_start = now
            self.window_calls = 0
            
        if self.window_calls >= self.limit:
            sleep_time = self.period_seconds - (now - self.window_start)
            if sleep_time > 0:
                if not block:
                    return False
                if log_callback: log_callback(f"  [Rate Limit] Cooldown triggered. Sleeping for {sleep_time:.1f}s...", full_only=False)
                time.sleep(sleep_time)
            self.window_start = time.time()
            self.window_calls = 0

        self.window_calls += 1
        self.total_calls += 1
        return self.total_calls if block else True


class MultiWindowRateLimiter:
    def __init__(self, limits_and_periods):
        self.limiters = [{"limit": l, "period": p, "start": time.time(), "calls": 0} for l, p in limits_and_periods]
        self.total_calls = 0

    def get_status_str(self):
        now = time.time()
        statuses = []
        for l in self.limiters:
            if now - l["start"] >= l["period"]:
                l["start"] = now
                l["calls"] = 0
            period_label = f"{l['period']}s" if l['period'] < 3600 else f"{l['period']//3600}h"
            statuses.append(f"{l['calls']}/{l['limit']} ({period_label})")
        return ", ".join(statuses)

    def record(self, log_callback=None, block=True):
        now = time.time()
        max_sleep = 0
        
        for limiter in self.limiters:
            if now - limiter["start"] >= limiter["period"]:
                limiter["start"] = now
                limiter["calls"] = 0
                
            if limiter["calls"] >= limiter["limit"]:
                sleep_time = limiter["period"] - (now - limiter["start"])
                if sleep_time > max_sleep:
                    max_sleep = sleep_time
        
        if max_sleep > 0:
            if not block:
                return False
            if log_callback: log_callback(f"  [Rate Limit] Multi-tier cooldown triggered. Sleeping for {max_sleep:.1f}s...", full_only=False)
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
            if log_callback: log_callback(f"  [Rate Limit] Pacing requests. Sleeping for {sleep_time:.1f}s...", full_only=False)
            time.sleep(sleep_time)
        
        self.last_call_time = time.time()
        self.total_calls += 1
        return self.total_calls if block else True


class ApiTracker:
    def __init__(self):
        self.iqdb_limiter = WindowRateLimiter(30, 60)
        self.saucenao_limiter = MultiWindowRateLimiter([(4, 32), (100, 86400)])
        self.danbooru_limiter = WindowRateLimiter(300, 60)
        self.gelbooru_limiter = GentleRateLimiter(1.5)
        self.yandere_limiter = GentleRateLimiter(1.5)
        self.konachan_limiter = GentleRateLimiter(1.5)
        self.zerochan_limiter = GentleRateLimiter(1.5)
        self.pixiv_limiter = GentleRateLimiter(1.5)
        
        self.serpapi_counter = 0
        self.gvision_counter = 0
        self.files_moved = 0
        self.matched_files_moved = 0
        self.unmatched_files_moved = 0
        self.hash_cache = {}

        self.IQDB_LIMIT = "30/min"
        self.SAUCENAO_LIMIT = "4/32s, 100/day"
        self.DANBOORU_LIMIT = "300/min"
        self.GELBOORU_LIMIT = "Gentle (~1-2s)"
        self.YANDERE_LIMIT = "Gentle (~1-2s)"
        self.KONACHAN_LIMIT = "Gentle (~1-2s)"
        self.PIXIV_LIMIT = "Gentle (~1-2s)"
        self.SERPAPI_LIMIT = "250/month"
        self.GVISION_LIMIT = "1000/month"

    @property
    def iqdb_calls(self): return self.iqdb_limiter.get_current_window_calls()
    @iqdb_calls.setter
    def iqdb_calls(self, value): self.iqdb_limiter.total_calls = value

    @property
    def saucenao_status(self): return self.saucenao_limiter.get_status_str()

    @property
    def danbooru_calls(self): return self.danbooru_limiter.get_current_window_calls()
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
    def pixiv_calls(self): return self.pixiv_limiter.total_calls
    @pixiv_calls.setter
    def pixiv_calls(self, value): self.pixiv_limiter.total_calls = value

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
    if not tag_str:
        return ""
    tags = [t.strip().replace(" ", "_") for t in tag_str.split() if t.strip()]
    return " ".join(tags)

def sanitize_foldername(name):
    if not name:
        return "Unsorted"
    return re.sub(r'[\\/:*?"<>|]', '', name).strip()

def extract_embedded_tags(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    tag_text = ""
    try:
        with Image.open(file_path) as img:
            if ext == '.png':
                tag_text = img.info.get("booru_tags") or img.info.get("parameters", "")
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


def fetch_google_lens_context(file_path, api_keys, log_callback=None):
    if isinstance(api_keys, str):
        api_keys = [api_keys]
    
    for key_idx, api_key in enumerate(api_keys):
        if not api_key.strip():
            continue
        upload_path = file_path
        temp_compressed_path = None
        try:
            if os.path.getsize(file_path) > 500 * 1024:
                if log_callback: log_callback(f"  [SerpApi #{key_idx+1}] Image size exceeds 500KB limit. Optimizing/compressing...", full_only=True)
                with Image.open(file_path) as img:
                    if img.mode in ("RGBA", "P"):
                        img = img.convert("RGB")
                    temp_compressed_path = file_path + f"_serpapi_temp_{key_idx}.jpg"
                    
                    quality = 90
                    while quality >= 20:
                        img.save(temp_compressed_path, "JPEG", quality=quality)
                        if os.path.getsize(temp_compressed_path) <= 500 * 1024:
                            break
                        quality -= 15
                upload_path = temp_compressed_path

            if log_callback: log_callback(f"  [SerpApi #{key_idx+1}] Uploading image to SerpApi Image API...", full_only=True)
            
            upload_url = "https://serpapi.com/image"
            with open(upload_path, "rb") as img_file:
                upload_response = requests.post(
                    upload_url,
                    data={"api_key": api_key.strip()},
                    files={"image": img_file},
                    timeout=15
                )
                
            if upload_response.status_code != 200:
                if log_callback: log_callback(f"  [SerpApi #{key_idx+1} Error] Upload failed (HTTP {upload_response.status_code}): {upload_response.text[:100]}", full_only=False)
                continue
                
            upload_data = upload_response.json()
            image_id = upload_data.get("image_id")
            if not image_id:
                if log_callback: log_callback(f"  [SerpApi #{key_idx+1} Warning] No image_id returned: {upload_data}", full_only=False)
                continue
                
            if log_callback: log_callback(f"  [SerpApi #{key_idx+1}] Querying Google Lens web index using image_id...", full_only=True)
            
            search_url = "https://serpapi.com/search"
            params = {
                "engine": "google_lens",
                "api_key": api_key.strip(),
                "image_id": image_id,
                "hl": "en"
            }
            
            response = requests.get(search_url, params=params, timeout=15)
            
            if response.status_code == 200:
                data = response.json()
                snippets = []
                
                kg = data.get("knowledge_graph", [])
                if kg and isinstance(kg, list):
                    for item in kg:
                        title = item.get("title", "")
                        subtitle = item.get("subtitle", "")
                        if title: snippets.append(f"Entity: {title} ({subtitle})")

                for key in ["visual_matches", "images_results", "inline_images"]:
                    matches = data.get(key, [])
                    if matches and isinstance(matches, list):
                        for match in matches[:6]:
                            m_title = match.get("title", "")
                            m_source = match.get("source", "")
                            m_link = match.get("link", "")
                            if m_title: 
                                snippets.append(f"Match: {m_title} (Source: {m_source}, Link: {m_link})")
                    
                if snippets:
                    context_text = "\n".join(snippets)
                    if log_callback: log_callback(f"  [SerpApi #{key_idx+1} SUCCESS] Found {len(snippets)} search context references!", full_only=True)
                    tracker.serpapi_calls += 1
                    return context_text
            else:
                if log_callback: log_callback(f"  [SerpApi #{key_idx+1} Error] Search failed (HTTP {response.status_code})", full_only=False)
        except Exception as e:
            if log_callback: log_callback(f"  [SerpApi #{key_idx+1} Error] {e}", full_only=False)
        finally:
            if temp_compressed_path and os.path.exists(temp_compressed_path):
                try: os.remove(temp_compressed_path)
                except: pass
    return ""


def fetch_google_vision_web_detection(file_path, api_keys, log_callback=None):
    if isinstance(api_keys, str):
        api_keys = [api_keys]
        
    for key_idx, api_key in enumerate(api_keys):
        if not api_key.strip():
            continue
        url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key.strip()}"
        try:
            if log_callback: log_callback(f"  [Google Vision #{key_idx+1}] Querying Google Cloud Vision Web Detection API...", full_only=True)
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
                    continue
                
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
                    if log_callback: log_callback(f"  [Google Vision #{key_idx+1} SUCCESS] Found {len(snippets)} search context references!", full_only=True)
                    tracker.gvision_calls += 1
                    return context_text
            else:
                if log_callback: log_callback(f"  [Google Vision #{key_idx+1} Error] HTTP {response.status_code}", full_only=False)
        except Exception as e:
            if log_callback: log_callback(f"  [Google Vision #{key_idx+1} Error] {e}", full_only=False)
    return ""


class BooruManagerApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Tag-AI Booru Image Sorter")
        self.geometry("1150x960")
        self.minsize(980, 760)

        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        sys.stdout = StdoutRedirector(self.log)
        sys.stderr = StdoutRedirector(self.log)

        self.is_processing = False
        self.stop_requested = False
        self.skip_requested = False
        self.file_in_progress = False
        self.target_source = ""
        self.dest_path = os.path.abspath("./organized_library")
        self.unmatched_path = ""
        self.vision_warning_shown = False
        self.api_panel_visible = True
        
        self.history_items = []
        self.history_page = 0
        self.history_per_page = 50
        
        # Robust config path supporting both compiled .exe and python script execution
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(os.path.abspath(sys.executable))
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_file = os.path.join(base_dir, "config.json")

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
        self.opt_tags = ctk.CTkOptionMenu(self.scroll_frame, values=["Auto (Highest Confidence)", "Yande.re", "Danbooru", "Gelbooru", "Konachan", "Zerochan", "Pixiv"])
        self.opt_tags.pack(pady=(0, 10), padx=10, fill="x")

        self.switch_engine_fallback = ctk.CTkSwitch(self.scroll_frame, text="Enable Search Engine Fallback")
        self.switch_engine_fallback.pack(pady=(5, 5), padx=10, anchor="w")
        self.switch_engine_fallback.select()

        self.switch_vision_fallback = ctk.CTkSwitch(self.scroll_frame, text="Allow Local Vision LLM as Fallback")
        self.switch_vision_fallback.pack(pady=(0, 10), padx=10, anchor="w")

        self.btn_toggle_api_panel = ctk.CTkButton(
            self.scroll_frame, 
            text="⚙️ Hide API & Credentials Panel", 
            command=self.toggle_api_panel, 
            fg_color="#495057", 
            hover_color="#343a40",
            font=ctk.CTkFont(size=12, weight="bold")
        )
        self.btn_toggle_api_panel.pack(pady=(10, 10), padx=10, fill="x")

        self.api_credentials_container = ctk.CTkFrame(self.scroll_frame, fg_color="transparent")
        self.api_credentials_container.pack(pady=(0, 5), padx=0, fill="x")

        self.switch_serpapi = ctk.CTkSwitch(self.api_credentials_container, text="Enable SerpApi Google Lens")
        self.switch_serpapi.pack(pady=(5, 2), padx=10, anchor="w")

        self.lbl_serpapi_info = ctk.CTkLabel(
            self.api_credentials_container, 
            text="Max filesize: 500KB (auto-compressed). Limit: 250/month.", 
            anchor="w", justify="left", font=ctk.CTkFont(size=10), text_color="gray", wraplength=260
        )
        self.lbl_serpapi_info.pack(pady=(0, 4), padx=10, fill="x")

        # Dynamic Key Lists containers
        self.serpapi_entries = []
        self.gvision_entries = []
        self.db_user_entries = []
        self.db_key_entries = []
        self.gb_user_entries = []
        self.gb_key_entries = []
        self.skey_entries = []

        self.serpapi_list_frame = ctk.CTkFrame(self.api_credentials_container, fg_color="transparent")
        self.serpapi_list_frame.pack(fill="x", padx=5, pady=(0, 5))
        self.add_serpapi_field()

        self.switch_gvision = ctk.CTkSwitch(self.api_credentials_container, text="Enable Google Cloud Vision API")
        self.switch_gvision.pack(pady=(5, 2), padx=10, anchor="w")

        self.lbl_gvision_info = ctk.CTkLabel(
            self.api_credentials_container, 
            text="Web Detection. Free limit: 1,000 requests/month.", 
            anchor="w", justify="left", font=ctk.CTkFont(size=10), text_color="gray", wraplength=260
        )
        self.lbl_gvision_info.pack(pady=(0, 4), padx=10, fill="x")

        self.gvision_list_frame = ctk.CTkFrame(self.api_credentials_container, fg_color="transparent")
        self.gvision_list_frame.pack(fill="x", padx=5, pady=(0, 5))
        self.add_gvision_field()

        self.switch_skip_on_web_fail = ctk.CTkSwitch(self.api_credentials_container, text="Treat as Unmatched if Web Search Fails")
        self.switch_skip_on_web_fail.pack(pady=(0, 10), padx=10, anchor="w")

        # Danbooru fields
        ctk.CTkLabel(self.api_credentials_container, text="Danbooru Credentials (Optional):", font=ctk.CTkFont(size=11, weight="bold"), anchor="w").pack(padx=10, anchor="w", pady=(5, 2))
        self.db_user_list_frame = ctk.CTkFrame(self.api_credentials_container, fg_color="transparent")
        self.db_user_list_frame.pack(fill="x", padx=5, pady=(0, 2))
        self.add_db_user_field()

        self.db_key_list_frame = ctk.CTkFrame(self.api_credentials_container, fg_color="transparent")
        self.db_key_list_frame.pack(fill="x", padx=5, pady=(0, 8))
        self.add_db_key_field()

        # Gelbooru fields
        ctk.CTkLabel(self.api_credentials_container, text="Gelbooru Credentials (Optional):", font=ctk.CTkFont(size=11, weight="bold"), anchor="w").pack(padx=10, anchor="w", pady=(5, 2))
        self.gb_user_list_frame = ctk.CTkFrame(self.api_credentials_container, fg_color="transparent")
        self.gb_user_list_frame.pack(fill="x", padx=5, pady=(0, 2))
        self.add_gb_user_field()

        self.gb_key_list_frame = ctk.CTkFrame(self.api_credentials_container, fg_color="transparent")
        self.gb_key_list_frame.pack(fill="x", padx=5, pady=(0, 8))
        self.add_gb_key_field()

        # SauceNAO fields
        ctk.CTkLabel(self.api_credentials_container, text="SauceNAO API Keys (Optional):", font=ctk.CTkFont(size=11, weight="bold"), anchor="w").pack(padx=10, anchor="w", pady=(5, 2))
        self.skey_list_frame = ctk.CTkFrame(self.api_credentials_container, fg_color="transparent")
        self.skey_list_frame.pack(fill="x", padx=5, pady=(0, 10))
        self.add_skey_field()

        self.lbl_lm = ctk.CTkLabel(self.api_credentials_container, text="Local LLM API Base URL:\n(LM Studio / Ollama OpenAI Endpoint)", anchor="w", justify="left", font=ctk.CTkFont(size=11, weight="bold"))
        self.lbl_lm.pack(pady=(5, 2), padx=10, fill="x")
        self.ent_lm = ctk.CTkEntry(self.api_credentials_container)
        self.ent_lm.insert(0, "http://localhost:1234/v1")
        self.ent_lm.pack(pady=(0, 10), padx=10, fill="x")

        self.lbl_subfolder_warning = ctk.CTkLabel(
            self.scroll_frame, 
            text="Note: Scanning subfolders increases context and will need a more powerful LLM.", 
            anchor="w", justify="left", font=ctk.CTkFont(size=11), text_color="gray", wraplength=260
        )
        self.lbl_subfolder_warning.pack(pady=(5, 2), padx=10, fill="x")

        self.switch_scan_subfolders = ctk.CTkSwitch(self.scroll_frame, text="Scan Immediate Subfolders")
        self.switch_scan_subfolders.pack(pady=(0, 10), padx=10, anchor="w")

        self.switch_use_embedded_tags = ctk.CTkSwitch(
            self.scroll_frame, 
            text="Use Existing Embedded Tags (Skip Search)", 
            command=self.on_use_embedded_tags_toggle
        )
        self.switch_use_embedded_tags.pack(pady=(5, 6), padx=10, anchor="w")

        self.switch_skip_no_embedded = ctk.CTkSwitch(
            self.scroll_frame, 
            text="Skip if No Embedded Tags Found", 
            font=ctk.CTkFont(size=11)
        )

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

        self.console_tabview = ctk.CTkTabview(self.tab_monitor)
        self.console_tabview.grid(row=2, column=0, sticky="nsew")

        self.tab_simp = self.console_tabview.add("Simplified Logs")
        self.tab_full = self.console_tabview.add("Full Logs")

        self.tab_simp.grid_rowconfigure(0, weight=1)
        self.tab_simp.grid_columnconfigure(0, weight=1)
        self.txt_console_simp = ctk.CTkTextbox(self.tab_simp, wrap="word", font=ctk.CTkFont(family="Courier", size=12))
        self.txt_console_simp.grid(row=0, column=0, sticky="nsew")
        self.txt_console_simp.insert("0.0", "System initialized. Simplified logs active.\n")

        self.tab_full.grid_rowconfigure(0, weight=1)
        self.tab_full.grid_columnconfigure(0, weight=1)
        self.txt_console_full = ctk.CTkTextbox(self.tab_full, wrap="word", font=ctk.CTkFont(family="Courier", size=12))
        self.txt_console_full.grid(row=0, column=0, sticky="nsew")
        self.txt_console_full.insert("0.0", "System initialized. Full audit & debug logs active.\n")

        self.tab_history.grid_rowconfigure(1, weight=1)
        self.tab_history.grid_columnconfigure(0, weight=1)

        self.history_top_frame = ctk.CTkFrame(self.tab_history, fg_color="transparent")
        self.history_top_frame.grid(row=0, column=0, sticky="ew", padx=5, pady=(5, 5))
        self.history_top_frame.grid_columnconfigure(3, weight=1)

        self.lbl_history_counter = ctk.CTkLabel(self.history_top_frame, text="Processed: 0/0 images", font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_history_counter.grid(row=0, column=0, padx=5, pady=5, sticky="w")

        self.lbl_files_moved_hist = ctk.CTkLabel(self.history_top_frame, text="Files Moved This Session: 0", font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_files_moved_hist.grid(row=0, column=1, padx=15, pady=5, sticky="w")

        self.history_pagination_frame = ctk.CTkFrame(self.history_top_frame, fg_color="transparent")
        self.history_pagination_frame.grid(row=0, column=2, padx=10, pady=2, sticky="w")

        self.btn_prev_page = ctk.CTkButton(self.history_pagination_frame, text="◀ Prev", width=65, height=26, command=lambda: self.change_history_page(-1))
        self.btn_prev_page.pack(side="left", padx=2)

        self.lbl_page_indicator = ctk.CTkLabel(self.history_pagination_frame, text="Page 1/1", font=ctk.CTkFont(size=11))
        self.lbl_page_indicator.pack(side="left", padx=5)

        self.btn_next_page = ctk.CTkButton(self.history_pagination_frame, text="Next ▶", width=65, height=26, command=lambda: self.change_history_page(1))
        self.btn_next_page.pack(side="left", padx=2)

        self.switch_hide_unmatched = ctk.CTkSwitch(self.history_top_frame, text="Hide Unmatched Files", command=self.refresh_history_view)
        self.switch_hide_unmatched.grid(row=0, column=3, padx=5, pady=5, sticky="e")

        self.history_scroll = ctk.CTkScrollableFrame(self.tab_history, fg_color="transparent")
        self.history_scroll.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)
        self.history_scroll.grid_columnconfigure(0, weight=1)

        # History Header Clarification Labels
        self.history_header_frame = ctk.CTkFrame(self.history_scroll, fg_color="transparent")
        self.history_header_frame.pack(fill="x", padx=5, pady=(0, 2))
        
        lbl_local_hdr = ctk.CTkLabel(self.history_header_frame, text="Local Image", font=ctk.CTkFont(size=11, weight="bold"), width=70, anchor="center")
        lbl_local_hdr.pack(side="left", padx=(10, 5))
        
        lbl_match_hdr = ctk.CTkLabel(self.history_header_frame, text="Matched Artwork", font=ctk.CTkFont(size=11, weight="bold"), width=70, anchor="center")
        lbl_match_hdr.pack(side="left", padx=(0, 10))

        self.on_unmatched_change(self.opt_unmatched.get())
        self.load_settings()

    # Dynamic API key field helpers with + / - buttons
    def add_serpapi_field(self, val=""):
        row = ctk.CTkFrame(self.serpapi_list_frame, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ent = ctk.CTkEntry(row, placeholder_text="SerpApi Key (Optional)")
        ent.pack(side="left", fill="x", expand=True, padx=(0, 2))
        if val: ent.insert(0, val)
        
        btn_add = ctk.CTkButton(row, text="+", width=28, height=28, fg_color="#2b8a3e", hover_color="#237032", command=lambda: self.add_serpapi_field())
        btn_add.pack(side="left", padx=2)
        btn_rem = ctk.CTkButton(row, text="-", width=28, height=28, fg_color="#c92a2a", hover_color="#a61e1e", command=lambda: self.remove_key_row(row, self.serpapi_entries, ent))
        btn_rem.pack(side="left")
        self.serpapi_entries.append((row, ent))

    def add_gvision_field(self, val=""):
        row = ctk.CTkFrame(self.gvision_list_frame, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ent = ctk.CTkEntry(row, placeholder_text="Google Cloud Vision Key (Optional)")
        ent.pack(side="left", fill="x", expand=True, padx=(0, 2))
        if val: ent.insert(0, val)
        
        btn_add = ctk.CTkButton(row, text="+", width=28, height=28, fg_color="#2b8a3e", hover_color="#237032", command=lambda: self.add_gvision_field())
        btn_add.pack(side="left", padx=2)
        btn_rem = ctk.CTkButton(row, text="-", width=28, height=28, fg_color="#c92a2a", hover_color="#a61e1e", command=lambda: self.remove_key_row(row, self.gvision_entries, ent))
        btn_rem.pack(side="left")
        self.gvision_entries.append((row, ent))

    def add_db_user_field(self, val=""):
        row = ctk.CTkFrame(self.db_user_list_frame, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ent = ctk.CTkEntry(row, placeholder_text="Danbooru User ID (Optional)")
        ent.pack(side="left", fill="x", expand=True, padx=(0, 2))
        if val: ent.insert(0, val)
        
        btn_add = ctk.CTkButton(row, text="+", width=28, height=28, fg_color="#2b8a3e", hover_color="#237032", command=lambda: self.add_db_user_field())
        btn_add.pack(side="left", padx=2)
        btn_rem = ctk.CTkButton(row, text="-", width=28, height=28, fg_color="#c92a2a", hover_color="#a61e1e", command=lambda: self.remove_key_row(row, self.db_user_entries, ent))
        btn_rem.pack(side="left")
        self.db_user_entries.append((row, ent))

    def add_db_key_field(self, val=""):
        row = ctk.CTkFrame(self.db_key_list_frame, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ent = ctk.CTkEntry(row, placeholder_text="Danbooru API Key (Optional)")
        ent.pack(side="left", fill="x", expand=True, padx=(0, 2))
        if val: ent.insert(0, val)
        
        btn_add = ctk.CTkButton(row, text="+", width=28, height=28, fg_color="#2b8a3e", hover_color="#237032", command=lambda: self.add_db_key_field())
        btn_add.pack(side="left", padx=2)
        btn_rem = ctk.CTkButton(row, text="-", width=28, height=28, fg_color="#c92a2a", hover_color="#a61e1e", command=lambda: self.remove_key_row(row, self.db_key_entries, ent))
        btn_rem.pack(side="left")
        self.db_key_entries.append((row, ent))

    def add_gb_user_field(self, val=""):
        row = ctk.CTkFrame(self.gb_user_list_frame, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ent = ctk.CTkEntry(row, placeholder_text="Gelbooru User ID (Optional)")
        ent.pack(side="left", fill="x", expand=True, padx=(0, 2))
        if val: ent.insert(0, val)
        
        btn_add = ctk.CTkButton(row, text="+", width=28, height=28, fg_color="#2b8a3e", hover_color="#237032", command=lambda: self.add_gb_user_field())
        btn_add.pack(side="left", padx=2)
        btn_rem = ctk.CTkButton(row, text="-", width=28, height=28, fg_color="#c92a2a", hover_color="#a61e1e", command=lambda: self.remove_key_row(row, self.gb_user_entries, ent))
        btn_rem.pack(side="left")
        self.gb_user_entries.append((row, ent))

    def add_gb_key_field(self, val=""):
        row = ctk.CTkFrame(self.gb_key_list_frame, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ent = ctk.CTkEntry(row, placeholder_text="Gelbooru API Key (Optional)")
        ent.pack(side="left", fill="x", expand=True, padx=(0, 2))
        if val: ent.insert(0, val)
        
        btn_add = ctk.CTkButton(row, text="+", width=28, height=28, fg_color="#2b8a3e", hover_color="#237032", command=lambda: self.add_gb_key_field())
        btn_add.pack(side="left", padx=2)
        btn_rem = ctk.CTkButton(row, text="-", width=28, height=28, fg_color="#c92a2a", hover_color="#a61e1e", command=lambda: self.remove_key_row(row, self.gb_key_entries, ent))
        btn_rem.pack(side="left")
        self.gb_key_entries.append((row, ent))

    def add_skey_field(self, val=""):
        row = ctk.CTkFrame(self.skey_list_frame, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ent = ctk.CTkEntry(row, placeholder_text="SauceNAO API Key (Optional)")
        ent.pack(side="left", fill="x", expand=True, padx=(0, 2))
        if val: ent.insert(0, val)
        
        btn_add = ctk.CTkButton(row, text="+", width=28, height=28, fg_color="#2b8a3e", hover_color="#237032", command=lambda: self.add_skey_field())
        btn_add.pack(side="left", padx=2)
        btn_rem = ctk.CTkButton(row, text="-", width=28, height=28, fg_color="#c92a2a", hover_color="#a61e1e", command=lambda: self.remove_key_row(row, self.skey_entries, ent))
        btn_rem.pack(side="left")
        self.skey_entries.append((row, ent))

    def remove_key_row(self, row_frame, entry_list, ent_widget):
        if len(entry_list) > 1:
            entry_list[:] = [item for item in entry_list if item[1] != ent_widget]
            row_frame.destroy()
        else:
            ent_widget.delete(0, 'end')

    def toggle_api_panel(self):
        if self.api_panel_visible:
            self.api_credentials_container.pack_forget()
            self.btn_toggle_api_panel.configure(text="⚙️ Show API & Credentials Panel")
            self.api_panel_visible = False
        else:
            self.api_credentials_container.pack(after=self.btn_toggle_api_panel, pady=(0, 5), padx=0, fill="x")
            self.btn_toggle_api_panel.configure(text="⚙️ Hide API & Credentials Panel")
            self.api_panel_visible = True

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

    def on_use_embedded_tags_toggle(self):
        if self.switch_use_embedded_tags.get() == 1:
            self.switch_skip_no_embedded.pack(after=self.switch_use_embedded_tags, pady=(2, 6), padx=20, anchor="w")
        else:
            self.switch_skip_no_embedded.pack_forget()

    def update_progress_display(self, current, total):
        def _update():
            self.lbl_history_counter.configure(text=f"Processed: {current}/{total} images")
            self.title(f"Tag-AI {current}/{total}")
        self.after(0, _update)

    def get_filtered_history_items(self):
        hide = self.switch_hide_unmatched.get() == 1
        if hide:
            return [e for e in self.history_items if not e.get("is_unmatched", False)]
        return self.history_items

    def change_history_page(self, delta):
        filtered = self.get_filtered_history_items()
        max_pages = max(1, (len(filtered) + self.history_per_page - 1) // self.history_per_page)
        
        new_page = self.history_page + delta
        if 0 <= new_page < max_pages:
            self.history_page = new_page
            self.refresh_history_view()

    def refresh_history_view(self):
        for widget in list(self.history_scroll.winfo_children()):
            widget.destroy()

        # Re-add header labels
        self.history_header_frame = ctk.CTkFrame(self.history_scroll, fg_color="transparent")
        self.history_header_frame.pack(fill="x", padx=5, pady=(0, 2))
        lbl_local_hdr = ctk.CTkLabel(self.history_header_frame, text="Local Image", font=ctk.CTkFont(size=11, weight="bold"), width=70, anchor="center")
        lbl_local_hdr.pack(side="left", padx=(10, 5))
        lbl_match_hdr = ctk.CTkLabel(self.history_header_frame, text="Matched Artwork", font=ctk.CTkFont(size=11, weight="bold"), width=70, anchor="center")
        lbl_match_hdr.pack(side="left", padx=(0, 10))

        filtered = self.get_filtered_history_items()
        total_items = len(filtered)
        max_pages = max(1, (total_items + self.history_per_page - 1) // self.history_per_page)
        
        if self.history_page >= max_pages:
            self.history_page = max_pages - 1

        self.lbl_page_indicator.configure(text=f"Page {self.history_page + 1}/{max_pages}")

        start_idx = self.history_page * self.history_per_page
        end_idx = min(start_idx + self.history_per_page, total_items)

        page_items = filtered[start_idx:end_idx]

        for entry in page_items:
            self.render_history_card(entry)

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
            
            self.refresh_history_view()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to move file to unmatched folder: {e}")

    def remove_metadata_action(self, entry):
        curr = entry["current_path"]
        if not os.path.exists(curr):
            messagebox.showwarning("File Not Found", "The file no longer exists at its current path.")
            return

        ext = os.path.splitext(curr)[1].lower()
        try:
            with Image.open(curr) as img:
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

    def add_history_item(self, original_path, current_path, filename, is_unmatched=False, match_thumb_url="", match_url=""):
        entry = {
            "original_path": original_path,
            "current_path": current_path,
            "filename": filename,
            "is_unmatched": is_unmatched,
            "match_thumb_url": match_thumb_url,
            "match_url": match_url
        }
        self.history_items.append(entry)
        
        filtered = self.get_filtered_history_items()
        max_pages = max(1, (len(filtered) + self.history_per_page - 1) // self.history_per_page)
        self.history_page = max_pages - 1

        def _refresh():
            self.refresh_history_view()
        self.after(0, _refresh)

    def render_history_card(self, entry):
        current_path = entry["current_path"]
        filename = entry["filename"]
        match_thumb_url = entry["match_thumb_url"]
        match_url = entry.get("match_url", "")
        is_unmatched = entry["is_unmatched"]

        card = ctk.CTkFrame(self.history_scroll, fg_color=("gray92", "gray18"))
        entry["card"] = card
        card.pack(fill="x", pady=5, padx=5)
        card.grid_columnconfigure(2, weight=1)

        thumb_lbl = ctk.CTkLabel(card, text="[No Image]", width=70, height=70, fg_color=("gray80", "gray30"), cursor="hand2")
        thumb_lbl.grid(row=0, column=0, rowspan=2, padx=(10, 5), pady=10)
        thumb_lbl.bind("<Button-1>", lambda e, p=current_path: self.open_file_external(p))
        try:
            with Image.open(current_path) as pil_img:
                pil_img = pil_img.resize((70, 70))
                ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(70, 70))
                thumb_lbl.configure(image=ctk_img, text="")
                thumb_lbl.image = ctk_img
        except Exception:
            pass

        match_thumb_lbl = ctk.CTkLabel(
            card, 
            text="[No Match]" if not match_url else "[Artwork]", 
            width=70, 
            height=70, 
            fg_color=("gray80", "gray30"),
            cursor="hand2" if match_url else ""
        )
        match_thumb_lbl.grid(row=0, column=1, rowspan=2, padx=(0, 10), pady=10)
        
        if match_thumb_url:
            threading.Thread(target=self.load_history_match_thumb_async, args=(match_thumb_url, match_thumb_lbl, match_url), daemon=True).start()
        elif match_url:
            match_thumb_lbl.configure(text="[Artwork]")

        if match_url:
            match_thumb_lbl.bind("<Button-1>", lambda e, u=match_url: webbrowser.open(u))

        info_text = f"File: {filename}\nMoved to: {os.path.dirname(current_path)}"
        info_lbl = ctk.CTkLabel(card, text=info_text, justify="left", font=ctk.CTkFont(size=11))
        info_lbl.grid(row=0, column=2, sticky="nw", padx=5, pady=(10, 0))
        entry["info_lbl"] = info_lbl

        btn_frame = ctk.CTkFrame(card, fg_color="transparent")
        btn_frame.grid(row=1, column=2, sticky="sw", padx=5, pady=(0, 10))

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

    def load_history_match_thumb_async(self, url, lbl_widget, match_url=""):
        if url.startswith("//"): url = "https:" + url
        elif url.startswith("/") and not url.startswith("//"): return
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            if "zerochan" in url: headers['Referer'] = 'https://www.zerochan.net/'
            elif "gelbooru" in url: headers['Referer'] = 'https://gelbooru.com/'
            elif "danbooru" in url.lower() or "donmai" in url.lower(): headers['Referer'] = 'https://danbooru.donmai.us/'
            elif "yande" in url: headers['Referer'] = 'https://yande.re/'
            elif "pixiv" in url: headers['Referer'] = 'https://www.pixiv.net/'

            res = requests.get(url, headers=headers, timeout=5)
            if res.status_code == 200:
                img_data = BytesIO(res.content)
                with Image.open(img_data) as pil_img:
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
                if entry in self.history_items:
                    self.history_items.remove(entry)
                self.refresh_history_view()
            except Exception as e:
                messagebox.showerror("Undo Error", f"Failed to undo move: {e}")
        else:
            messagebox.showwarning("File Not Found", "The moved file no longer exists at its destination path.")
            if entry in self.history_items:
                self.history_items.remove(entry)
            if entry.get("is_unmatched", False):
                tracker.unmatched_files_moved = max(0, tracker.unmatched_files_moved - 1)
            else:
                tracker.matched_files_moved = max(0, tracker.matched_files_moved - 1)
            tracker.files_moved = max(0, tracker.files_moved - 1)
            self.update_metrics_display()
            self.refresh_history_view()

    def clear_app_cache(self):
        if messagebox.askyesno("Confirm Clear Cache", "Are you sure you want to clear the hash cache, API counters, console logs, and history?\n\nYour folders, toggles, and API keys will be preserved."):
            tracker.hash_cache.clear()
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

            self.history_items.clear()
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

    def save_settings(self):
        config = {
            "exec_mode": self.opt_exec_mode.get(),
            "source_folder": self.target_source,
            "dest_folder": self.dest_path,
            "unmatched_action": self.opt_unmatched.get(),
            "unmatched_folder": self.unmatched_path,
            "engine": self.opt_engine.get(),
            "tags": self.opt_tags.get(),
            "fallback": int(self.switch_engine_fallback.get()),
            "vision_fallback": int(self.switch_vision_fallback.get()),
            "serpapi_enabled": int(self.switch_serpapi.get()),
            "gvision_enabled": int(self.switch_gvision.get()),
            "skip_on_web_fail": int(self.switch_skip_on_web_fail.get()),
            "vision_warning_shown": self.vision_warning_shown,
            "scan_subfolders": int(self.switch_scan_subfolders.get()),
            "use_embedded_tags": int(self.switch_use_embedded_tags.get()),
            "skip_no_embedded": int(self.switch_skip_no_embedded.get()),
            "embed_metadata": int(self.switch_embed_metadata.get()),
            "meta_artist": int(self.switch_meta_artist.get()),
            "meta_series": int(self.switch_meta_series.get()),
            "meta_character": int(self.switch_meta_character.get()),
            "meta_general": int(self.switch_meta_general.get()),
            "meta_meta": int(self.switch_meta_meta.get()),
            "serpapi_keys": [ent.get().strip() for _, ent in self.serpapi_entries if ent.get().strip()],
            "gvision_keys": [ent.get().strip() for _, ent in self.gvision_entries if ent.get().strip()],
            "db_users": [ent.get().strip() for _, ent in self.db_user_entries if ent.get().strip()],
            "db_keys": [ent.get().strip() for _, ent in self.db_key_entries if ent.get().strip()],
            "gb_users": [ent.get().strip() for _, ent in self.gb_user_entries if ent.get().strip()],
            "gb_keys": [ent.get().strip() for _, ent in self.gb_key_entries if ent.get().strip()],
            "skeys": [ent.get().strip() for _, ent in self.skey_entries if ent.get().strip()],
            "lm_url": self.ent_lm.get(),
            "serpapi_counter": tracker.serpapi_calls,
            "gvision_counter": tracker.gvision_calls
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
            
            tracker.serpapi_calls = config.get("serpapi_counter", 0)
            tracker.gvision_calls = config.get("gvision_counter", 0)
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
            self.switch_hide_unmatched.grid(row=0, column=3, padx=5, pady=5, sticky="e")
        else:
            self.btn_select_unmatched.configure(state="disabled")
            self.switch_hide_unmatched.grid_remove()
            self.switch_hide_unmatched.deselect()
            self.refresh_history_view()
        self.update_history_card_buttons()

    def update_metrics_display(self):
        def _update():
            self.lbl_iqdb_metric.configure(text=f"IQDB: {tracker.iqdb_calls} (Limit: {tracker.IQDB_LIMIT})")
            self.lbl_saucenao_metric.configure(text=f"SauceNAO: {tracker.saucenao_status} (Limit: {tracker.SAUCENAO_LIMIT})")
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

    def log(self, message, full_only=False):
        def _log():
            if not full_only:
                self.txt_console_simp.insert("end", message + "\n")
                lc_simp = int(self.txt_console_simp.index('end-1c').split('.')[0])
                if lc_simp > 1000:
                    self.txt_console_simp.delete("1.0", "2.0")
                self.txt_console_simp.see("end")

            self.txt_console_full.insert("end", message + "\n")
            lc_full = int(self.txt_console_full.index('end-1c').split('.')[0])
            if lc_full > 1000:
                self.txt_console_full.delete("1.0", "2.0")
            self.txt_console_full.see("end")
        self.after(0, _log)

    def load_local_thumbnail(self, file_path):
        try:
            with Image.open(file_path) as pil_img:
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
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            if "zerochan" in url: headers['Referer'] = 'https://www.zerochan.net/'
            elif "gelbooru" in url: headers['Referer'] = 'https://gelbooru.com/'
            elif "danbooru" in url.lower() or "donmai" in url.lower(): headers['Referer'] = 'https://danbooru.donmai.us/'
            elif "yande" in url: headers['Referer'] = 'https://yande.re/'
            elif "pixiv" in url: headers['Referer'] = 'https://www.pixiv.net/'
            
            response = requests.get(url, headers=headers, timeout=5)
            if response.status_code == 200:
                img_data = BytesIO(response.content)
                with Image.open(img_data) as pil_img:
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

        self.is_processing = True
        self.stop_requested = False
        self.skip_requested = False
        self.btn_start.configure(state="disabled", fg_color="gray")
        self.btn_skip.configure(state="normal", fg_color="#e67700")
        self.btn_cancel.configure(state="normal", text="Cancel Execution", fg_color="#c92a2a")
        threading.Thread(target=self.run_workflow, daemon=True).start()

    def cancel_processing(self):
        if self.is_processing:
            if not self.stop_requested:
                self.stop_requested = True
                self.log("\n[User Interrupt] Stop requested. Execution will halt shortly...")
                self.btn_cancel.configure(text="Cancelling...", fg_color="#a61e1e")
            else:
                self.log("\n[User Interrupt] Force-abort triggered. Resetting application state...")
                self.is_processing = False
                self.file_in_progress = False
                self.stop_requested = False
                self.skip_requested = False
                def _reset_force():
                    self.btn_start.configure(state="normal", fg_color="#2b8a3e")
                    self.btn_skip.configure(state="disabled", fg_color="gray")
                    self.btn_cancel.configure(state="disabled", text="Cancel Execution", fg_color="#c92a2a")
                    self.title("Tag-AI Booru Image Sorter")
                    self.lbl_details.configure(text="Status: Execution canceled immediately.")
                self.after(0, _reset_force)

    def skip_current_file_action(self):
        if self.is_processing and self.file_in_progress:
            self.skip_requested = True
            self.log("\n[User Action] Skip requested for current file...")
            self.btn_skip.configure(state="disabled", fg_color="gray")

    def safe_get_request(self, url, headers=None, auth=None, timeout=10, retries=2):
        for attempt in range(1, retries + 1):
            if self.skip_requested or self.stop_requested: return None
            try:
                res = requests.get(url, headers=headers, auth=auth, timeout=timeout)
                if res.status_code != 200:
                    self.log(f"  [API Error] HTTP {res.status_code} for {url.split('?')[0]} ({res.reason})", full_only=False)
                return res
            except requests.exceptions.Timeout as te:
                self.log(f"  [API Timeout] Request timed out ({url.split('?')[0]}): {te}", full_only=False)
            except requests.exceptions.ConnectionError as ce:
                self.log(f"  [API Connection Error] Failed to connect ({url.split('?')[0]}): {ce}", full_only=False)
            except Exception as ex:
                self.log(f"  [API Exception] {url.split('?')[0]}: {ex}", full_only=False)
            
            if attempt < retries:
                time.sleep(1.0 * attempt)
        return None

    def search_iqdb_engine(self, file_path, is_fallback=False):
        recorded = tracker.iqdb_limiter.record(self.log, block=not is_fallback)
        if not recorded:
            self.log("  [Rate Limit] IQDB fallback on cooldown. Skipping fallback search...")
            return []
        self.update_metrics_display()

        booru_ids = {'danbooru': None, 'gelbooru': None, 'yandere': None, 'konachan': None, 'zerochan': None, 'pixiv': None}
        primary_title, best_thumb = "", ""
        max_similarity = 0.0

        try:
            with open(file_path, 'rb') as f:
                res = requests.post("https://iqdb.org/", files={'file': f}, timeout=15)
                
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                for table in soup.find_all('table'):
                    if self.stop_requested or self.skip_requested: break
                    table_text = table.get_text()
                    pct_matches = re.findall(r'(\d+(?:\.\d+)?)\s*%', table_text)
                    if not pct_matches: continue

                    try: table_sim = max([float(p) for p in pct_matches])
                    except: continue

                    if table_sim < 75.0:
                        self.log(f"  [IQDB] Ignored low-similarity match ({table_sim:.1f}% < 75.0%)", full_only=True)
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
                        elif 'pixiv.net' in href and not booru_ids['pixiv']:
                            m = re.search(r'illust_id=(\d+)', href) or re.search(r'/artworks/(\d+)', href)
                            if m: booru_ids['pixiv'] = m.group(1)
                            if not primary_title: primary_title = href

            else:
                self.log(f"  [IQDB Error] HTTP {res.status_code} ({res.reason})", full_only=False)

            if max_similarity >= 75.0 and (any(booru_ids.values()) or primary_title):
                return [{
                    'engine': 'IQDB',
                    'title': primary_title,
                    'similarity': max_similarity,
                    'booru_ids': booru_ids,
                    'thumb': best_thumb,
                    'fallback_url': primary_title
                }]
        except Exception as e:
            if not self.stop_requested:
                self.log(f"  [IQDB Error] {e}", full_only=False)
        return []

    def search_saucenao_engine(self, file_path, api_keys, is_fallback=False):
        if isinstance(api_keys, str):
            api_keys = [api_keys]

        for key_idx, api_key in enumerate(api_keys):
            if not api_key.strip():
                continue
            recorded = tracker.saucenao_limiter.record(self.log, block=not is_fallback)
            if not recorded:
                self.log("  [Rate Limit] SauceNAO fallback on cooldown. Skipping fallback search...")
                return []
            self.update_metrics_display()

            booru_ids = {'danbooru': None, 'gelbooru': None, 'yandere': None, 'konachan': None, 'zerochan': None, 'pixiv': None}
            primary_title, best_thumb, fallback_ext_url = "", "", ""
            max_similarity = 0.0

            try:
                params = {'output_type': 2, 'numres': 5, 'hide': 0, 'api_key': api_key.strip()}
                with open(file_path, 'rb') as f:
                    res = requests.post("https://saucenao.com/search.php", params=params, files={'file': f}, timeout=15)
                    
                if res.status_code == 200:
                    results = res.json().get('results', [])
                    for r in results:
                        if self.stop_requested or self.skip_requested: break
                        data = r.get('data', {})
                        header = r.get('header', {})
                        
                        try:
                            sim_val = float(header.get('similarity', 0))
                            if sim_val < 75.0:
                                self.log(f"  [SauceNAO #{key_idx+1}] Ignored low-similarity match ({sim_val:.1f}% < 75.0%)", full_only=True)
                                continue
                            if sim_val > max_similarity:
                                max_similarity = sim_val
                                best_thumb = header.get('thumbnail', '')
                        except: pass

                        if not primary_title: primary_title = data.get('title') or data.get('source', 'SauceNAO Match')

                        if data.get('danbooru_id'): booru_ids['danbooru'] = str(data.get('danbooru_id'))
                        if data.get('gelbooru_id'): booru_ids['gelbooru'] = str(data.get('gelbooru_id'))
                        if data.get('yandere_id'): booru_ids['yandere'] = str(data.get('yandere_id'))
                        if data.get('konachan_id'): booru_ids['konachan'] = str(data.get('konachan_id'))
                        if data.get('pixiv_id'): booru_ids['pixiv'] = str(data.get('pixiv_id'))

                        for url in data.get('ext_urls', []):
                            if not fallback_ext_url: fallback_ext_url = url
                            if 'zerochan.net' in url and not booru_ids['zerochan']:
                                z_id = url.split('/')[-1]
                                if z_id.isdigit(): booru_ids['zerochan'] = z_id
                            elif 'pixiv.net' in url and not booru_ids['pixiv']:
                                m = re.search(r'illust_id=(\d+)', url) or re.search(r'/artworks/(\d+)', url)
                                if m: booru_ids['pixiv'] = m.group(1)

                    if max_similarity >= 75.0 and (any(booru_ids.values()) or primary_title or fallback_ext_url):
                        return [{
                            'engine': f'SauceNAO (#{key_idx+1})',
                            'title': primary_title,
                            'similarity': max_similarity,
                            'booru_ids': booru_ids,
                            'thumb': best_thumb,
                            'fallback_url': fallback_ext_url
                        }]
                else:
                    self.log(f"  [SauceNAO #{key_idx+1} Error] HTTP {res.status_code} - {res.text[:80]}", full_only=False)
            except Exception as e:
                if not self.stop_requested:
                    self.log(f"  [SauceNAO #{key_idx+1} Error] {e}", full_only=False)
        return []

    def search_local_vision_engine(self, file_path, client, model_id):
        self.log(f"  [Local Vision LLM] Analyzing {os.path.basename(file_path)}...")
        
        try:
            web_context = ""
            web_api_enabled_count = 0

            if self.switch_serpapi.get() == 1:
                serpapi_keys = [ent.get().strip() for _, ent in self.serpapi_entries if ent.get().strip()]
                if serpapi_keys and not self.stop_requested:
                    web_api_enabled_count += 1
                    web_context = fetch_google_lens_context(file_path, serpapi_keys, self.log)
                    self.update_metrics_display()

            if self.skip_requested or self.stop_requested: return []

            if not web_context and self.switch_gvision.get() == 1:
                gvision_keys = [ent.get().strip() for _, ent in self.gvision_entries if ent.get().strip()]
                if gvision_keys and not self.stop_requested:
                    web_api_enabled_count += 1
                    web_context = fetch_google_vision_web_detection(file_path, gvision_keys, self.log)
                    self.update_metrics_display()

            if self.skip_requested or self.stop_requested: return []

            if web_api_enabled_count > 0 and not web_context and self.switch_skip_on_web_fail.get() == 1:
                self.log("  [Web Search] Enabled web search API(s) returned no results or failed. Skipping local vision tagging.", full_only=False)
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

            if self.stop_requested: return []

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
            except Exception as parse_err:
                norm_str = normalize_tag(raw_res)
                cat_tag_dict = {'artist': '', 'copyright': '', 'character': '', 'general': norm_str, 'meta': '', 'combined': norm_str}

            if cat_tag_dict['combined']:
                return [{
                    'engine': 'Local Vision LLM',
                    'title': 'Local Vision AI Tags',
                    'similarity': 100.0,
                    'booru_ids': {},
                    'thumb': '',
                    'tag_dict_override': cat_tag_dict
                }]
        except Exception as e:
            if not self.stop_requested:
                self.log(f"  [Local Vision LLM Error] {e}", full_only=False)
        return []

    def fetch_tags(self, file_path, match_meta, primary_choice):
        tag_data = {'artist': '', 'copyright': '', 'character': '', 'general': '', 'meta': '', 'combined': '', 'matched_url': ''}
        booru_ids = match_meta.get('booru_ids', {}) if match_meta else {}
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        
        db_users = [ent.get().strip() for _, ent in self.db_user_entries if ent.get().strip()]
        db_keys = [ent.get().strip() for _, ent in self.db_key_entries if ent.get().strip()]
        gb_users = [ent.get().strip() for _, ent in self.gb_user_entries if ent.get().strip()]
        gb_keys = [ent.get().strip() for _, ent in self.gb_key_entries if ent.get().strip()]

        confidence_order = ["danbooru", "yandere", "gelbooru", "konachan", "zerochan", "pixiv"]

        target_key = None
        if "Danbooru" in primary_choice: target_key = "danbooru"
        elif "Yande.re" in primary_choice: target_key = "yandere"
        elif "Gelbooru" in primary_choice: target_key = "gelbooru"
        elif "Konachan" in primary_choice: target_key = "konachan"
        elif "Zerochan" in primary_choice: target_key = "zerochan"
        elif "Pixiv" in primary_choice: target_key = "pixiv"

        if target_key and target_key in confidence_order:
            confidence_order.remove(target_key)
            confidence_order.insert(0, target_key)

        for source in confidence_order:
            if self.stop_requested or self.skip_requested: break
            ident = booru_ids.get(source)
            if not ident: continue
            
            db_display_name = {"danbooru": "Danbooru", "yandere": "Yande.re", "gelbooru": "Gelbooru", "konachan": "Konachan", "zerochan": "Zerochan", "pixiv": "Pixiv"}.get(source, source.capitalize())

            try:
                if source == 'danbooru':
                    tracker.danbooru_limiter.record(self.log)
                    self.update_metrics_display()
                    self.log(f"  Querying Danbooru ID: {ident}...")
                    
                    # Try backup credential combinations if primary fails
                    success_danbooru = False
                    pdata = {}
                    
                    auth_pairs = []
                    if db_users and db_keys:
                        for u, k in zip(db_users, db_keys):
                            auth_pairs.append((u, k))
                    if db_keys and not db_users:
                        for k in db_keys:
                            auth_pairs.append(("", k))
                    if not auth_pairs:
                        auth_pairs.append((None, None))

                    for u, k in auth_pairs:
                        db_headers = {'User-Agent': f"TagAIBooruSorter/1.0 (by {u if u else 'anonymous_user'})"}
                        auth = (u, k) if (u or k) else None
                        res = self.safe_get_request(f"https://danbooru.donmai.us/posts/{ident}.json", headers=db_headers, auth=auth, timeout=6)
                        if res and res.status_code == 200:
                            pdata = res.json()
                            success_danbooru = True
                            break

                    if success_danbooru and pdata:
                        preview_url = pdata.get('preview_file_url') or pdata.get('large_file_url')
                        if preview_url: self.update_match_thumbnail(preview_url)
                        
                        tag_data['artist'] = normalize_tag(pdata.get('tag_string_artist', ''))
                        tag_data['copyright'] = normalize_tag(pdata.get('tag_string_copyright', ''))
                        tag_data['character'] = normalize_tag(pdata.get('tag_string_character', ''))
                        tag_data['general'] = normalize_tag(pdata.get('tag_string_general', ''))
                        tag_data['meta'] = normalize_tag(pdata.get('tag_string_meta', ''))
                        tag_data['combined'] = normalize_tag(pdata.get('tag_string', ''))
                        tag_data['matched_url'] = f"https://danbooru.donmai.us/posts/{ident}"
                        
                        if tag_data['combined']:
                            return tag_data

                elif source in ['yandere', 'konachan', 'gelbooru', 'zerochan', 'pixiv']:
                    if source == 'yandere':
                        tracker.yandere_limiter.record(self.log)
                        url = f"https://yande.re/post.json?tags=id:{ident}"
                    elif source == 'konachan':
                        tracker.konachan_limiter.record(self.log)
                        url = f"https://konachan.com/post.json?tags=id:{ident}"
                    elif source == 'gelbooru':
                        tracker.gelbooru_limiter.record(self.log)
                        # Try with backup gelbooru credentials
                        gb_pairs = []
                        if gb_users and gb_keys:
                            for u, k in zip(gb_users, gb_keys):
                                gb_pairs.append((u, k))
                        if gb_keys and not gb_users:
                            for k in gb_keys:
                                gb_pairs.append(("", k))
                        if not gb_pairs:
                            gb_pairs.append(("", ""))

                        res = None
                        for u, k in gb_pairs:
                            auth_params = f"&api_key={k}&user_id={u}" if (u and k) else ""
                            gel_url = f"https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&id={ident}{auth_params}"
                            res = self.safe_get_request(gel_url, headers=headers, timeout=6)
                            if res and res.status_code == 200:
                                break
                    elif source == 'zerochan':
                        tracker.zerochan_limiter.record(self.log)
                        url = f"https://www.zerochan.net/{ident}?json"
                    else:
                        tracker.pixiv_limiter.record(self.log)
                        url = f"https://www.pixiv.net/ajax/illust/{ident}"

                    self.update_metrics_display()
                    self.log(f"  Querying {db_display_name} ID: {ident}...")
                    
                    req_headers = headers.copy()
                    if source == 'pixiv':
                        req_headers['Referer'] = f"https://www.pixiv.net/artworks/{ident}"

                    if source != 'gelbooru':
                        res = self.safe_get_request(url, headers=req_headers, timeout=6)

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
                                tag_data['matched_url'] = f"https://www.zerochan.net/{ident}"
                                return tag_data
                        elif source == 'pixiv':
                            pdata = res.json()
                            if not pdata.get('error'):
                                body = pdata.get('body', {})
                                preview_url = body.get('urls', {}).get('regular') or body.get('urls', {}).get('small')
                                if preview_url: self.update_match_thumbnail(preview_url)
                                
                                artist_name = body.get('userName', '')
                                if artist_name: tag_data['artist'] = normalize_tag(artist_name)
                                
                                tags_obj = body.get('tags', {}).get('tags', [])
                                tag_names = [t.get('tag') for t in tags_obj if t.get('tag')]
                                if tag_names:
                                    raw_str = normalize_tag(" ".join(tag_names))
                                    tag_data['general'] = raw_str
                                    tag_data['combined'] = raw_str
                                    tag_data['matched_url'] = f"https://www.pixiv.net/artworks/{ident}"
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
                                    if source == 'yandere':
                                        tag_data['matched_url'] = f"https://yande.re/post/show/{ident}"
                                    elif source == 'konachan':
                                        tag_data['matched_url'] = f"https://konachan.com/post/show/{ident}"
                                    elif source == 'gelbooru':
                                        tag_data['matched_url'] = f"https://gelbooru.com/index.php?page=post&s=view&id={ident}"
                                    return tag_data
            except Exception as e:
                if not self.stop_requested:
                    self.log(f"  [{db_display_name} Error] {e}", full_only=False)

        if not tag_data['matched_url'] and match_meta and match_meta.get('fallback_url'):
            tag_data['matched_url'] = match_meta.get('fallback_url')

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
            with Image.open(file_path) as img:
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
                    except Exception:
                        img.save(file_path, quality=95)
        except Exception:
            pass

    def run_workflow(self):
        try:
            lm_url = self.ent_lm.get().strip()
            engine_choice = self.opt_engine.get()
            tag_choice = self.opt_tags.get()
            exec_mode = self.opt_exec_mode.get()
            saucenao_keys = [ent.get().strip() for _, ent in self.skey_entries if ent.get().strip()]
            allow_engine_fallback = bool(self.switch_engine_fallback.get())
            allow_vision_fallback = bool(self.switch_vision_fallback.get())
            use_embedded = bool(self.switch_use_embedded_tags.get() == 1)
            skip_no_embedded = bool(self.switch_skip_no_embedded.get() == 1)

            image_files = []
            if "Single" in exec_mode:
                if os.path.exists(self.target_source): image_files = [self.target_source]
            else:
                if os.path.exists(self.target_source):
                    image_files = [os.path.join(self.target_source, f) for f in os.listdir(self.target_source) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]

            if not image_files:
                self.log("Error: No valid image files found at target source path.")
                return

            self.log(f"\n--- Connecting to Local LLM Base URL at {lm_url} ---")
            client = OpenAI(base_url=lm_url, api_key="lm-studio")
            try:
                models = client.models.list()
                model_id = models.data[0].id
                self.log(f"Connected! Active model loaded: {model_id}")
            except Exception as e:
                self.log(f"Warning: Could not fetch models list. Using fallback ID. ({e})", full_only=False)
                model_id = "local-model"

            total_files = len(image_files)
            self.log(f"Starting sequential execution of {total_files} item(s) in [{exec_mode}]...")

            for idx, file_path in enumerate(image_files, 1):
                if self.stop_requested: break
                self.skip_requested = False
                self.file_in_progress = True

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
                    cached_match_url = cached_data.get('matched_url', '')
                    
                    final_dest_path = os.path.join(self.dest_path, target_folder)
                    os.makedirs(final_dest_path, exist_ok=True)
                    target_file_path = os.path.join(final_dest_path, filename)
                    shutil.move(file_path, target_file_path)
                    
                    self.embed_metadata(target_file_path, tag_dict)
                    self.log(f"  -> Moved into: /{target_folder}/{filename} (Cached)\n")
                    
                    tracker.files_moved += 1
                    tracker.matched_files_moved += 1
                    self.update_metrics_display()
                    self.add_history_item(file_path, target_file_path, filename, is_unmatched=False, match_thumb_url=cached_thumb, match_url=cached_match_url)
                    
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

                chosen_match = None
                tag_dict = {}
                formatted_tags = ""
                match_thumb = ""
                matched_url = ""

                if use_embedded:
                    embedded_str = extract_embedded_tags(file_path)
                    if embedded_str:
                        formatted_tags = normalize_tag(embedded_str)
                        tag_dict = {
                            'artist': '', 'copyright': '', 'character': '',
                            'general': formatted_tags, 'meta': '', 'combined': formatted_tags, 'matched_url': ''
                        }
                        chosen_match = {'engine': 'Embedded Metadata', 'title': 'Embedded Metadata Match', 'similarity': 100.0, 'booru_ids': {}, 'thumb': ''}
                    elif skip_no_embedded:
                        self.log(f"  [Embedded Metadata] No embedded tags found in {filename}. Skipping file entirely.\n")
                        self.file_in_progress = False
                        if "Single" in exec_mode: break
                        else: continue

                if not chosen_match and not self.stop_requested:
                    primary_engine = engine_choice
                    fallback_sequence = []
                    
                    if "IQDB" in primary_engine:
                        fallback_sequence = [
                            ('IQDB', lambda fp: self.search_iqdb_engine(fp, is_fallback=False)),
                            ('SauceNAO', lambda fp: self.search_saucenao_engine(fp, saucenao_keys, is_fallback=True)) if allow_engine_fallback else None,
                            ('Local Vision LLM', lambda fp: self.search_local_vision_engine(fp, client, model_id)) if allow_vision_fallback else None
                        ]
                    elif "SauceNAO" in primary_engine:
                        fallback_sequence = [
                            ('SauceNAO', lambda fp: self.search_saucenao_engine(fp, saucenao_keys, is_fallback=False)),
                            ('IQDB', lambda fp: self.search_iqdb_engine(fp, is_fallback=True)) if allow_engine_fallback else None,
                            ('Local Vision LLM', lambda fp: self.search_local_vision_engine(fp, client, model_id)) if allow_vision_fallback else None
                        ]
                    else:
                        fallback_sequence = [
                            ('Local Vision LLM', lambda fp: self.search_local_vision_engine(fp, client, model_id))
                        ]

                    for step_info in fallback_sequence:
                        if not step_info or self.stop_requested or self.skip_requested: continue
                        eng_label, search_func = step_info
                        
                        matches = search_func(file_path)
                        if not matches: continue
                        
                        candidate_match = matches[0]
                        cand_thumb = candidate_match.get('thumb', '')
                        cand_url = candidate_match.get('fallback_url', '') or candidate_match.get('title', '')
                        
                        if eng_label == 'Local Vision LLM':
                            chosen_match = candidate_match
                            tag_dict = candidate_match.get('tag_dict_override', {})
                            formatted_tags = tag_dict.get('combined', '')
                            matched_url = ''
                            match_thumb = ''
                            break
                        
                        test_tag_dict = self.fetch_tags(file_path, candidate_match, tag_choice)
                        test_combined = test_tag_dict.get('combined', '')
                        
                        if test_combined:
                            chosen_match = candidate_match
                            tag_dict = test_tag_dict
                            formatted_tags = test_combined
                            matched_url = test_tag_dict.get('matched_url', '') or cand_url
                            match_thumb = cand_thumb
                            break
                        else:
                            if not match_thumb and cand_thumb: match_thumb = cand_thumb
                            if not matched_url and cand_url: matched_url = cand_url
                            self.log(f"  [{eng_label}] Match found on unsupported source (e.g. Twitter), continuing search chain for extractable tags...")

                if self.stop_requested:
                    self.file_in_progress = False
                    break

                if self.skip_requested:
                    self.log(f"  [Skipped] User requested skip for {filename}.\n")
                    self.file_in_progress = False
                    self.btn_skip.configure(state="normal", fg_color="#e67700")
                    continue

                if chosen_match and chosen_match.get('engine') != 'Embedded Metadata':
                    eng_name = chosen_match.get('engine', engine_choice)
                    sim_score = chosen_match.get('similarity', 0.0)
                    sim_str = f" ({eng_name} {sim_score:.1f}% Similarity)" if sim_score > 0 else f" ({eng_name} Match)"
                    self.log(f"  Match Confirmed{sim_str}: {chosen_match['title']}")
                    if match_thumb: self.update_match_thumbnail(match_thumb)
                    self.after(0, lambda: self.lbl_details.configure(text=f"File: {filename}\nStatus: Search Match Confirmed"))
                elif not chosen_match:
                    self.log("  Search engine returned no matches.")
                    def _clear_match_thumb():
                        self.lbl_match_thumb.configure(text="[No Match]", image="")
                        self.lbl_match_thumb.image = None
                    self.after(0, _clear_match_thumb)
                    self.after(0, lambda: self.lbl_details.configure(text=f"File: {filename}\nStatus: Unmatched"))

                if self.stop_requested:
                    self.file_in_progress = False
                    break

                if self.skip_requested:
                    self.log(f"  [Skipped] User requested skip for {filename}.\n")
                    self.file_in_progress = False
                    self.btn_skip.configure(state="normal", fg_color="#e67700")
                    continue

                is_unmatched = False
                if not chosen_match:
                    is_unmatched = True
                elif not formatted_tags and not chosen_match.get('title'):
                    is_unmatched = True
                elif chosen_match and not any(chosen_match.get('booru_ids', {}).values()) and not formatted_tags and chosen_match.get('engine') != 'Local Vision LLM':
                    is_unmatched = True
                    self.log("  [Unsupported Source] All engines exhausted without booru tag support. Treating as unmatched/skipped for tagging, but recording match history.")

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
                        self.add_history_item(file_path, target_file_path, filename, is_unmatched=True, match_thumb_url=match_thumb, match_url=matched_url)

                    self.file_in_progress = False
                    self.btn_skip.configure(state="normal", fg_color="#e67700")
                    if "Single" in exec_mode: break
                    else: continue

                if not formatted_tags and chosen_match:
                    formatted_tags = normalize_tag(chosen_match.get('title', ''))
                    tag_dict['combined'] = formatted_tags
                    tag_dict['general'] = formatted_tags

                if formatted_tags:
                    tag_list = formatted_tags.split()
                    if len(tag_list) > 50:
                        formatted_tags = " ".join(tag_list[:50])

                available_folders = []
                if not os.path.exists(self.dest_path): os.makedirs(self.dest_path)
                
                scan_subfolders = bool(self.switch_scan_subfolders.get() == 1)

                for item in os.listdir(self.dest_path):
                    full_item_path = os.path.join(self.dest_path, item)
                    if os.path.isdir(full_item_path) and item not in ["Unmatched", "Unsorted"] and not item.startswith("."):
                        available_folders.append(item)
                        if scan_subfolders:
                            try:
                                for subitem in os.listdir(full_item_path):
                                    full_subitem_path = os.path.join(full_item_path, subitem)
                                    if os.path.isdir(full_subitem_path) and not subitem.startswith("."):
                                        available_folders.append(f"{item}/{subitem}")
                            except Exception:
                                pass

                target_folder = "Unsorted"
                if available_folders and formatted_tags and not self.stop_requested:
                    try:
                        system_p = (
                            "You are a strict file router. Analyze the tags and select the single best matching "
                            "folder path strictly from the provided available folders list. "
                            "If no folder matches well, you MUST output 'Unsorted'. Do not invent or create new folder names. "
                            "Output ONLY valid JSON in this exact format: {\"destination\": \"FolderPath\"}"
                        )
                        user_p = f"Tags: {formatted_tags}\nFolders:\n{json.dumps(available_folders)}"
                        
                        response = client.chat.completions.create(
                            model=model_id,
                            messages=[{"role": "system", "content": system_p}, {"role": "user", "content": user_p}],
                            temperature=0.0,
                            max_tokens=100
                        )
                        content = response.choices[0].message.content.strip()

                        if content.startswith("```"):
                            content = content.split("```")[1]
                            if content.startswith("json"): content = content[4:]
                        res_json = json.loads(content.strip())
                        raw_dest = res_json.get("destination", "Unsorted")
                        target_folder = sanitize_foldername(raw_dest)
                        
                        if target_folder not in available_folders and target_folder != "Unsorted":
                            target_folder = "Unsorted"
                            
                    except Exception as llm_err:
                        if not self.stop_requested:
                            self.log(f"  [LLM Error]: {llm_err}", full_only=False)
                        target_folder = "Unsorted"

                if self.stop_requested:
                    self.file_in_progress = False
                    break

                final_dest_path = os.path.join(self.dest_path, target_folder)
                os.makedirs(final_dest_path, exist_ok=True)
                target_file_path = os.path.join(final_dest_path, filename)
                shutil.move(file_path, target_file_path)
                
                self.embed_metadata(target_file_path, tag_dict)

                if formatted_tags and target_folder != "Unsorted":
                    tracker.hash_cache[file_hash] = {'tags': formatted_tags, 'folder': target_folder, 'tag_dict': tag_dict, 'thumb': match_thumb, 'matched_url': matched_url}
                
                tracker.files_moved += 1
                tracker.matched_files_moved += 1
                self.update_metrics_display()
                
                self.log(f"  -> Moved into: /{target_folder}/{filename}\n")
                self.add_history_item(file_path, target_file_path, filename, is_unmatched=False, match_thumb_url=match_thumb, match_url=matched_url)

                self.file_in_progress = False
                self.btn_skip.configure(state="normal", fg_color="#e67700")

                if "Single" in exec_mode:
                    break

            self.log("=== Execution Queue Stopped/Complete ==.")
        except Exception as e:
            self.log(f"Critical Workflow Error: {e}")
        finally:
            self.save_settings()
            self.is_processing = False
            self.stop_requested = False
            self.skip_requested = False
            self.file_in_progress = False
            def _reset_final():
                self.btn_start.configure(state="normal", fg_color="#2b8a3e")
                self.btn_skip.configure(state="disabled", fg_color="gray")
                self.btn_cancel.configure(state="disabled", text="Cancel Execution", fg_color="#c92a2a")
                self.title("Tag-AI Booru Image Sorter")
            self.after(0, _reset_final)

if __name__ == "__main__":
    app = BooruManagerApp()
    app.mainloop()