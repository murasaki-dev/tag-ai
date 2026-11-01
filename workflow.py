"""Batch execution, cancellation, and session progress."""

import providers
from stage_timing import timed_stage, profile
import hashlib
import json
import os
import threading
import traceback
import uuid
from storage import extract_embedded_tags, safe_move, stream_file_md5
from routing import AUTO_MODEL, CONTEXT_METHODS, DATABASE_METHODS, available_model_ids, database_ordered_candidates, enabled_flow_methods, enabled_search_methods, normalize_tag, routing_messages, routing_plan, routing_context_lines
from providers import tracker
from routing import UNMATCHED_ACTIONS

class WorkflowMixin:
    def claim_processing_file(self, path): return True
    def release_processing_file(self, path): pass
    def begin_file_mutation(self): return not self.stop_requested and not self.skip_requested

    @profile("unmatched")
    def _handle_unmatched(self, file_path, filename, thumb, url, reason):
        if not self.begin_file_mutation():
            return False
        current = file_path
        moved = False
        if self._read_widget(self.opt_unmatched) == UNMATCHED_ACTIONS[1]:
            destination = self._run_unmatched_path or os.path.join(self._run_paths[1], 'Unmatched')
            with timed_stage(self, "move.directory"): os.makedirs(destination, exist_ok=True)
            current = safe_move(file_path, os.path.join(destination, os.path.basename(file_path)))
            moved = os.path.abspath(current) != os.path.abspath(file_path)
            if moved:
                tracker.files_moved += 1
                tracker.unmatched_files_moved += 1
                self.update_metrics_display()
        self.log(f"  [Unmatched] {'Moved to ' + current if moved else 'Leaving ' + filename + ' in source'}.\n")
        self.add_history_item(file_path, current, filename, True, thumb, url, reason=reason)
        return moved

    def _restore_session(self):
        try:
            self.pending_session = self.session_store.load()
            if self.pending_session and self.pending_session.get('session_id'):
                self._current_session_id = self.pending_session['session_id']
        except Exception as exc:
            self.pending_session = None
            self.log(f"Could not restore stopped session: {exc}")
        self._refresh_session_controls()

    def _refresh_session_controls(self):
        pending = self.pending_session
        if pending and not self.is_processing:
            done, total = pending['completed'], len(pending['files'])
            self.lbl_session.configure(text=f"Stopped session · {done}/{total} completed · {total - done} remaining")
            self.lbl_sort_progress.configure(text=f"Stopped after {done} of {total} images")
            self.progress_bar.set(done / total if total else 0)
            self.session_controls.pack(fill="x", pady=(8, 4), before=self.lbl_sort_progress)
            self.btn_start.pack_forget()
            self.lbl_run_status.configure(text="Session ready to resume")
        else:
            self.session_controls.pack_forget()
            if not self.btn_start.winfo_manager():
                self.btn_start.pack(side="right")

    def resume_session(self):
        if self.is_processing or not self.pending_session:
            return
        record = self.pending_session
        self.target_source, self.dest_path = record['source'], record['destination']
        self.opt_exec_mode.set(record['mode'])
        self.lbl_source_val.configure(text=self.target_source)
        self.lbl_dest_val.configure(text=self.dest_path)
        self.start_thread(resume=True)

    def restart_session(self):
        if self.is_processing or not self.pending_session:
            return
        # Rescan the stopped session's source. Completed moves stay in place.
        record = self.pending_session
        self.target_source, self.dest_path = record['source'], record['destination']
        self.opt_exec_mode.set(record['mode'])
        self.lbl_source_val.configure(text=self.target_source)
        self.lbl_dest_val.configure(text=self.dest_path)
        self.start_thread(restart=True)

    def start_thread(self, resume=False, restart=False):
        if self.is_processing: return
        if self.pending_session and not (resume or restart):
            self.log("Choose Resume or Start over for the stopped session.")
            return
        if not self.target_source:
            self.log("Error: Please select a source folder or individual file first.")
            return

        if not self.save_settings():
            return

        self._run_session = json.loads(json.dumps(self.pending_session)) if resume else None
        self._current_session_id = (self._run_session.get('session_id') if self._run_session else None) or uuid.uuid4().hex
        if self._run_session:
            self.session_store.set_session_id(self._current_session_id)
        self._history_revision += 1
        self.is_processing = True
        self._refresh_session_controls()
        self.lbl_run_status.configure(text="Sorting in the background")
        self.stop_requested = False
        self.skip_requested = False
        self.btn_start.configure(state="disabled", fg_color=("#ffffff", "#20242b"))
        self.btn_skip.configure(state="normal", fg_color=("#ffffff", "#20242b"))
        self.btn_cancel.configure(state="normal", text="Stop after current request", fg_color=("#ffffff", "#20242b"))
        self._snapshot_widgets()
        self._run_aliases = json.loads(json.dumps(self.folder_aliases))
        self._run_search_pipeline = self.search_flow.get_config()
        self._run_database_pipelines = json.loads(json.dumps(self.database_pipelines))
        self._run_context_pipeline = json.loads(json.dumps(self.context_pipeline))
        self._run_paths = (self.target_source, self.dest_path)
        self._run_unmatched_path = self.unmatched_path
        threading.Thread(target=self.run_workflow, daemon=True).start()

    def cancel_processing(self):
        if self.is_processing:
            if not self.stop_requested:
                self.stop_requested = True
                self.log("\n[User Interrupt] Stop requested. Execution will halt shortly...")
                self.btn_cancel.configure(text="Stopping after active request…")
                self.lbl_run_status.configure(text="Stopping after active request…")
            else:
                self.log("Cancellation pending; waiting for the active request to finish.")

    def skip_current_file_action(self):
        if self.is_processing and self.file_in_progress:
            self.skip_requested = True
            self.log("\n[User Action] Skip requested for current file...")
            self.btn_skip.configure(state="disabled", fg_color=("#ffffff", "#20242b"))

    def run_workflow(self):
        self._workflow_error = ''
        try:
            with timed_stage(self, "setup"):
                source_path, dest_path = self._run_paths
                lm_url = self._read_widget(self.ent_lm).strip()
                engine_choice = self._read_widget(self.opt_engine)
                tag_choice = self._read_widget(self.opt_tags)
                exec_mode = self._read_widget(self.opt_exec_mode)
                saucenao_keys = [self._read_widget(ent).strip() for _, ent in self._entries("skey_entries") if self._read_widget(ent).strip()]
                allow_engine_fallback = bool(self._read_widget(self.switch_engine_fallback))
                allow_vision_fallback = bool(self._read_widget(self.switch_vision_fallback))
                use_embedded = bool(self._read_widget(self.switch_use_embedded_tags) == 1)
                matching_mode = self._read_widget(self.opt_matching_mode)
                skip_no_embedded = bool(self._read_widget(self.switch_skip_no_embedded) == 1)

                record = self._run_session
                image_files = list(record['files']) if record else []
                completed = record['completed'] if record else 0
                with timed_stage(self, "setup.files"):
                    if not record:
                        if "Single" in exec_mode:
                            if os.path.isfile(source_path): image_files = [os.path.abspath(source_path)]
                        elif os.path.isdir(source_path):
                            image_files = sorted(os.path.abspath(os.path.join(source_path, f)) for f in os.listdir(source_path)
                                                 if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
                                                 and os.path.isfile(os.path.join(source_path, f)))

                if not image_files:
                    self.log("Error: No valid image files found at target source path.")
                    return

                if not record:
                    self.session_store.begin(source_path, dest_path, exec_mode, image_files, self._current_session_id)
                if completed:
                    self.log(f"Resuming after {completed} completed file(s); {len(image_files) - completed} remaining.")

                self.log(f"\n--- Connecting to Local LLM Base URL at {lm_url} ---")
                with timed_stage(self, "setup.client"): client = providers.local_model_client(lm_url)
                selected_model = self._read_widget(self.opt_model)
                try:
                    with timed_stage(self, "setup.models"): ids = available_model_ids(client)
                except Exception as exc:
                    self.log(f"Warning: Could not list server models: {exc}")
                    model_id = selected_model if selected_model != AUTO_MODEL else "local-model"
                else:
                    if selected_model != AUTO_MODEL and selected_model not in ids:
                        self.log(f"Selected model '{selected_model}' is unavailable. Refresh models in Settings; no files processed.")
                        return
                    if not ids:
                        self.log("The local server returned no available models; no files processed.")
                        return
                    model_id = ids[0] if selected_model == AUTO_MODEL else selected_model
                vision_selection = self._read_widget(self.opt_vision_model) if hasattr(self, 'opt_vision_model') else 'Same as routing'
                vision_model_id = model_id if vision_selection == 'Same as routing' else vision_selection
                if vision_model_id == AUTO_MODEL: vision_model_id = ids[0] if 'ids' in locals() and ids else model_id
                if 'Local Vision LLM' in enabled_search_methods(self._run_search_pipeline) and 'ids' in locals() and vision_model_id not in ids:
                    self.log(f"Selected vision model '{vision_model_id}' is unavailable; no files processed.")
                    return
                self.log(f"Folder routing model: {model_id}; vision identification model: {vision_model_id}")
                modes = dict(getattr(self, '_run_reasoning_modes', {'vision': 'Server default', 'routing': 'Server default'}))
                if 'Local Vision LLM' not in enabled_search_methods(self._run_search_pipeline): modes['vision'] = 'Server default'
                with timed_stage(self, 'setup.reasoning'):
                    self._run_reasoning_requests = providers.prepare_reasoning_requests(lm_url, {'routing': model_id, 'vision': vision_model_id}, modes)
                routing_aliases = self._run_aliases

                total_files = len(image_files)
                os.makedirs(dest_path, exist_ok=True)
                scan_subfolders = bool(self._read_widget(self.switch_scan_subfolders) == 1)
                available_folders = []
                with timed_stage(self, "setup.folders"):
                    try:
                        with os.scandir(dest_path) as entries:
                            for entry in entries:
                                if not entry.is_dir() or entry.name in ("Unmatched", "Unsorted") or entry.name.startswith("."):
                                    continue
                                available_folders.append(entry.name)
                                if scan_subfolders:
                                    try:
                                        with os.scandir(entry.path) as children:
                                            available_folders.extend(
                                                f"{entry.name}/{child.name}"
                                                for child in children
                                                if child.is_dir() and not child.name.startswith(".")
                                            )
                                    except OSError:
                                        pass
                    except OSError as folder_error:
                        self.log(f"Warning: Could not scan destination folders: {folder_error}")

                with timed_stage(self, "setup.settings"):
                    search_pipeline = self._run_search_pipeline
                    cache_settings = {
                        "search_pipeline": enabled_search_methods(search_pipeline),
                        "database_pipelines": {engine: enabled_flow_methods(flow, DATABASE_METHODS) for engine, flow in self._run_database_pipelines.items()},
                        "context_pipeline": enabled_flow_methods(self._run_context_pipeline, CONTEXT_METHODS),
                        "version": 6,
                        "booru_hash_lookup": bool(getattr(self, '_run_booru_hash', False)),
                        "engine": engine_choice,
                        "tag_choice": tag_choice,
                        "engine_fallback": allow_engine_fallback,
                        "vision_fallback": allow_vision_fallback,
                        "skip_on_web_fail": bool(self._read_widget(self.switch_skip_on_web_fail)),
                        "embedded": use_embedded,
                        "skip_no_embedded": skip_no_embedded,
                        "serpapi_enabled": bool(self._read_widget(self.switch_serpapi)),
                        "serpapi_keys_hash": hashlib.sha256("\n".join(self._read_widget(ent).strip() for _, ent in self._entries("serpapi_entries")).encode("utf-8")).hexdigest(),
                        "gvision_enabled": bool(self._read_widget(self.switch_gvision)),
                        "gvision_keys_hash": hashlib.sha256("\n".join(self._read_widget(ent).strip() for _, ent in self._entries("gvision_entries")).encode("utf-8")).hexdigest(),
                        "saucenao_keys_hash": hashlib.sha256("\n".join(saucenao_keys).encode("utf-8")).hexdigest(),
                        "model": vision_model_id if engine_choice == "Local Vision LLM" or allow_vision_fallback else None,
                        "llm_url": lm_url if engine_choice == "Local Vision LLM" or allow_vision_fallback else None,
                    }
                    if 'Local Vision LLM' in enabled_search_methods(search_pipeline):
                        evidence_options = getattr(self, '_run_evidence_options', {})
                        if evidence_options.get('tagger_enabled') or evidence_options.get('context_mode') == 'Combine enabled':
                            cache_settings['evidence_options'] = evidence_options
                        if self._run_reasoning_requests['vision']: cache_settings['vision_reasoning'] = self._run_reasoning_requests['vision']
                        cache_settings['vision_response_version'] = 2  # Retry misses from the old 500-token vision limit.
                    cache_signature = hashlib.sha256(json.dumps(cache_settings, sort_keys=True).encode("utf-8")).hexdigest()
                self.log(f"Starting sequential execution of {total_files} item(s) in [{exec_mode}]...")

            identification_dirty = False
            def refresh_settings(stage):
                nonlocal client, model_id, vision_model_id, dest_path, available_folders, routing_aliases, search_pipeline
                nonlocal matching_mode, tag_choice, engine_choice, use_embedded, skip_no_embedded, saucenao_keys, cache_settings, cache_signature
                nonlocal identification_dirty
                boundary = getattr(self, 'settings_boundary', None)
                if not boundary: return
                state = boundary(dict(client=client, model_id=model_id, vision_model_id=vision_model_id, dest_path=dest_path,
                    available_folders=available_folders, routing_aliases=routing_aliases, search_pipeline=search_pipeline,
                    matching_mode=matching_mode, tag_choice=tag_choice, engine_choice=engine_choice, use_embedded=use_embedded,
                    skip_no_embedded=skip_no_embedded, saucenao_keys=saucenao_keys, cache_settings=cache_settings, cache_signature=cache_signature), stage)
                client, model_id, vision_model_id = state['client'], state['model_id'], state['vision_model_id']
                dest_path, available_folders, routing_aliases = state['dest_path'], state['available_folders'], state['routing_aliases']
                search_pipeline, matching_mode = state['search_pipeline'], state['matching_mode']
                tag_choice, engine_choice = state['tag_choice'], state['engine_choice']
                use_embedded, skip_no_embedded, saucenao_keys = state['use_embedded'], state['skip_no_embedded'], state['saucenao_keys']
                identification_dirty = identification_dirty or cache_signature != state['cache_signature']
                cache_settings, cache_signature = state['cache_settings'], state['cache_signature']
                self._live_client, self._live_vision_model_id = client, vision_model_id
            self._runtime_boundary = refresh_settings

            for idx, file_path in enumerate(image_files, 1):
                if idx <= completed:
                    continue
                if self.stop_requested: break
                self.skip_requested = False
                self.file_in_progress = True
                self._preview_generation += 1
                file_committed = False
                self._history_identification_method = ''; self._history_from_cache = False; self._history_image_hash = ''; self._evidence_unavailable = False

                try:
                    self.update_progress_display(idx, total_files)
                    if not self.claim_processing_file(file_path):
                        self.log(f"[History] Skipping a file manually changed during this batch: {os.path.basename(file_path)}")
                        continue
                    if not os.path.isfile(file_path):
                        self.log(f"[Session] File no longer at source; skipping: {file_path}")
                        continue

                    filename = os.path.basename(file_path)
                    self.log(f"Scanning file: {filename}")
                    refresh_settings('source preparation')
                    identification_signature = cache_signature
                    identification_dirty = False

                    with timed_stage(self, "source"):
                        with timed_stage(self, "source.hash"): file_hash = stream_file_md5(file_path)
                        self._history_image_hash = file_hash
                        force_search = self.result_cache.needs_refresh(file_hash)
                        if force_search: self.log('  [Cache reset] Searching again; bypassing embedded tags for this attempt.')
                        tracker.serpapi_last_outcome = None
                        self._vision_last_outcome = None; self._last_vision_error = ''
                        cached_data = None if force_search else self.result_cache.get(file_hash, cache_signature)
                        if cached_data is None and not force_search:
                            legacy_settings = dict(cache_settings); legacy_settings['version'] = 5; legacy_settings.pop('booru_hash_lookup', None)
                            legacy_signature = hashlib.sha256(json.dumps(legacy_settings, sort_keys=True).encode('utf-8')).hexdigest()
                            previous = self.result_cache.get(file_hash, legacy_signature)
                            if previous and previous.get('status') == 'resolved': cached_data = previous
                        cached_negative = bool(cached_data and cached_data.get("status") == "miss")
                        if cached_negative and self._read_widget(self.switch_serpapi) and cached_data.get("serpapi_result_version") != 1:
                            cached_negative = False  # Retry old failures once under the corrected key-failover policy.

                        def _reset_match_thumb():
                            self.lbl_match_thumb.configure(text="[Searching...]", image="")
                            self.lbl_match_thumb.image = None
                        self.after(0, _reset_match_thumb)
                        self.after(0, lambda filename=filename: self.lbl_details.configure(text=f"File: {filename}\nStatus: Searching..."))
                        self.load_local_thumbnail(file_path)

                        chosen_match = None
                        tag_dict = {}
                        formatted_tags = ""
                        match_thumb = ""
                        matched_url = ""

                        if cached_data and cached_data.get("status") == "resolved":
                            tag_dict = cached_data.get("tag_dict", {})
                            formatted_tags = tag_dict.get("combined", cached_data.get("tags", ""))
                            chosen_match = cached_data.get("chosen_match") or {"engine": "Cached", "title": "Cached tags", "booru_ids": {}}
                            match_thumb = cached_data.get("thumb", "")
                            matched_url = cached_data.get("matched_url", "")
                            self.log("  [Cache] Reusing image identification; checking current folders.")

                        if use_embedded and not chosen_match and not force_search:
                            with timed_stage(self, "source.embedded"): embedded_str = extract_embedded_tags(file_path)
                            if embedded_str:
                                formatted_tags = normalize_tag(embedded_str)
                                tag_dict = {
                                    'artist': '', 'copyright': '', 'character': '',
                                    'general': formatted_tags, 'meta': '', 'combined': formatted_tags, 'matched_url': '', '_identity_source': 'embedded'
                                }
                                chosen_match = {'engine': 'Embedded Metadata', 'title': 'Embedded Metadata Match', 'similarity': 100.0, 'booru_ids': {}, 'thumb': ''}
                            elif skip_no_embedded:
                                self.log(f"  [Embedded Metadata] No embedded tags found in {filename}. Skipping file entirely.\n")
                                self.file_in_progress = False
                                if "Single" in exec_mode: break
                                else: continue

                    self._hash_lookup_unavailable = False
                    refresh_settings('exact booru hash')
                    if not chosen_match and not cached_negative and getattr(self, '_run_booru_hash', False) and not self.stop_requested and not self.skip_requested:
                        hash_matches = self.search_booru_hash(file_path, file_hash)
                        if hash_matches:
                            chosen_match = hash_matches[0]; tag_dict = chosen_match['tag_dict_override']
                            formatted_tags = tag_dict.get('combined', ''); match_thumb = chosen_match.get('thumb', '')
                            matched_url = tag_dict.get('matched_url', '')

                    if not chosen_match and not self.stop_requested:
                        planned_engines = enabled_search_methods(search_pipeline)
                        primary_engine = planned_engines[0] if planned_engines else "Disabled"
                        active_tag_database = "not used by Local Vision LLM" if primary_engine == "Local Vision LLM" else tag_choice
                        self.log(f"  [Search Plan] Enabled order: {' → '.join(planned_engines) or 'none'}; tag database: {active_tag_database}.", full_only=True)
                        attempted_engines = set()
                        while True:
                            refresh_settings('identification method')
                            if cache_signature != identification_signature: cached_negative = False
                            planned_engines = enabled_search_methods(search_pipeline)
                            engine = next((name for name in planned_engines if name not in attempted_engines), None)
                            if engine is None or cached_negative or self.stop_requested or self.skip_requested: break
                            position = len(attempted_engines); attempted_engines.add(engine)
                            if engine == "IQDB":
                                search = lambda fp, fallback=position > 0: self.search_iqdb_engine(fp, is_fallback=fallback)
                            elif engine == "SauceNAO":
                                search = lambda fp, fallback=position > 0: self.search_saucenao_engine(fp, saucenao_keys, is_fallback=fallback)
                            else:
                                search = lambda fp: self.search_local_vision_engine(fp, client, vision_model_id)
                            eng_label, search_func = engine, search
                            role = "primary" if eng_label == primary_engine else "fallback"
                            self.log(f"  [Search Attempt] {eng_label} ({role}) for {filename}.", full_only=True)

                            self.after(0, lambda engine=eng_label: self.lbl_run_status.configure(text=f"Searching with {engine}…"))
                            matches = search_func(file_path)
                            refresh_settings('database lookup')
                            if not matches:
                                self.log(f"  [Search Result] {eng_label} returned no match candidates.", full_only=True)
                                continue

                            self.log(f"  [Search Result] {eng_label} returned {len(matches)} candidate(s).", full_only=True)

                            original_matches = matches
                            attempted_candidates = set(); candidate_index = 0
                            while not self.stop_requested and not self.skip_requested:
                                refresh_settings('database candidate')
                                matches = original_matches if eng_label == 'Local Vision LLM' else database_ordered_candidates(original_matches, self._run_database_pipelines[eng_label], eng_label)
                                def candidate_key(candidate):
                                    return (tuple(sorted((key, str(value)) for key, value in candidate.get('booru_ids', {}).items())), candidate.get('title', ''), candidate.get('fallback_url', ''))
                                candidate_match = next((candidate for candidate in matches if candidate_key(candidate) not in attempted_candidates), None)
                                if candidate_match is None: break
                                attempted_candidates.add(candidate_key(candidate_match)); candidate_index += 1
                                cand_thumb = candidate_match.get('thumb', '')
                                cand_url = candidate_match.get('fallback_url', '') or candidate_match.get('title', '')
                                self.log(
                                    f"  [Candidate Try] {eng_label} result {candidate_index}/{len(matches)} "
                                    f"({candidate_match.get('similarity', 0):.1f}% — {candidate_match.get('result_source', 'unknown source')}).",
                                    full_only=True
                                )

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
                                    test_tag_dict["_identity_source"] = "booru"
                                    chosen_match = candidate_match
                                    tag_dict = test_tag_dict
                                    formatted_tags = test_combined
                                    matched_url = test_tag_dict.get('matched_url', '') or cand_url
                                    match_thumb = test_tag_dict.get('_match_thumb_url') or cand_thumb
                                    break
                                else:
                                    if not match_thumb and cand_thumb: match_thumb = cand_thumb
                                    if not matched_url and cand_url: matched_url = cand_url
                                    self.log(
                                        f"  [{eng_label}] Candidate {candidate_index} ({candidate_match.get('result_source', 'unknown source')}) "
                                        "had no usable tags; trying the next confidence result.",
                                        full_only=True
                                    )

                            if chosen_match:
                                break

                    if self.stop_requested:
                        self.file_in_progress = False
                        break

                    if self.skip_requested:
                        self.log(f"  [Skipped] User requested skip for {filename}.\n")
                        self.file_in_progress = False
                        self.after(0, lambda: self.btn_skip.configure(state="normal", fg_color=("#ffffff", "#20242b")))
                        continue

                    self._history_identification_method = chosen_match.get('engine', engine_choice) if chosen_match else ''
                    self._history_from_cache = bool((cached_negative and not chosen_match) or (chosen_match and cached_data and cached_data.get('status') == 'resolved'))
                    if chosen_match and chosen_match.get('engine') != 'Embedded Metadata':
                        eng_name = chosen_match.get('engine', engine_choice)
                        sim_score = chosen_match.get('similarity', 0.0)
                        sim_str = f" ({eng_name} {sim_score:.1f}% Similarity)" if sim_score > 0 else f" ({eng_name} Match)"
                        self.log(f"  Match Confirmed{sim_str}: {chosen_match['title']}")
                        if match_thumb: self.update_match_thumbnail(match_thumb)
                        self.after(0, lambda filename=filename: self.lbl_details.configure(text=f"File: {filename}\nStatus: Search Match Confirmed"))
                    elif not chosen_match:
                        self.log("  Search engine returned no matches.")
                        def _clear_match_thumb():
                            self.lbl_match_thumb.configure(text="[No Match]", image="")
                            self.lbl_match_thumb.image = None
                        self.after(0, _clear_match_thumb)
                        self.after(0, lambda filename=filename: self.lbl_details.configure(text=f"File: {filename}\nStatus: Unmatched"))

                    if self.stop_requested:
                        self.file_in_progress = False
                        break

                    if self.skip_requested:
                        self.log(f"  [Skipped] User requested skip for {filename}.\n")
                        self.file_in_progress = False
                        self.after(0, lambda: self.btn_skip.configure(state="normal", fg_color=("#ffffff", "#20242b")))
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
                        if not self._hash_lookup_unavailable and not identification_dirty and cache_signature == identification_signature and not cached_negative and not self.stop_requested and not self.skip_requested and tracker.serpapi_last_outcome != "unavailable" and self._vision_last_outcome != "unavailable" and not self._evidence_unavailable:
                            self.result_cache.put(file_hash, cache_signature, "miss", {"serpapi_result_version": 1})
                        file_committed = self._handle_unmatched(file_path, filename, match_thumb, matched_url, self._last_vision_error or "No reliable image match after enabled searches")
                        self.file_in_progress = False
                        self.after(0, lambda: self.btn_skip.configure(state="normal", fg_color=("#ffffff", "#20242b")))
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

                    if formatted_tags and not self._evidence_unavailable and not identification_dirty and cache_signature == identification_signature:
                        self.result_cache.put(file_hash, cache_signature, "resolved", {
                            "tags": formatted_tags, "tag_dict": tag_dict, "chosen_match": chosen_match,
                            "thumb": match_thumb, "matched_url": matched_url})

                    if "_identity_source" not in tag_dict:
                        tag_dict["_identity_source"] = ("vision" if chosen_match.get("engine") == "Local Vision LLM"
                                                       else "embedded" if chosen_match.get("engine") == "Embedded Metadata" else "booru")
                    refresh_settings('folder routing')
                    tag_dict = self.enrich_booru_aliases(tag_dict, available_folders, routing_aliases)
                    plan = routing_plan(tag_dict, available_folders, routing_aliases, matching_mode)
                    target_folder = plan["direct"] or "Unsorted"
                    route_reason = plan["reason"]
                    messages, raw_response = [], ""
                    self._last_routing_metadata = None
                    if plan['direct']:
                        for line in routing_context_lines(plan, model_id, 'exact tags / aliases; no model request'):
                            self.log(line, full_only=True)
                    if target_folder == "Unsorted" and plan["candidates"] and not self.stop_requested:
                        messages = routing_messages(plan)
                        try:
                            self.after(0, lambda: self.lbl_run_status.configure(text="Choosing among supported folders · you can browse History"))
                            folder, route_reason, messages, raw_response = self.route_with_model(client, model_id, plan)
                            target_folder = folder or "Unsorted"
                        except Exception as exc:
                            route_reason = f"Routing request failed: {exc}"
                            self.log(f"  [Router] {route_reason}")
                    if not plan['candidates'] and not plan['direct']:
                        for line in routing_context_lines(plan, model_id, 'no supported folders; no model request'):
                            self.log(line, full_only=True)
                    self.log(f"  [Folder Router] {route_reason}; destination: {target_folder}")
                    self._record_routing_decision({"file": filename, "image_hash": file_hash, "model": model_id,
                        "plan": plan, "messages": messages, "response": raw_response,
                        "response_metadata": self._last_routing_metadata,
                        "destination": target_folder, "reason": route_reason})

                    if self.stop_requested:
                        self.file_in_progress = False
                        break

                    if self.skip_requested:
                        self.log(f"[Skipped] Leaving {filename} in source.")
                        continue

                    refresh_settings('metadata and move')
                    if target_folder != 'Unsorted' and target_folder not in available_folders:
                        target_folder = 'Unsorted'; route_reason = 'Destination is unavailable after a settings change'
                    if not self.begin_file_mutation(): continue
                    if target_folder == "Unsorted":
                        self.log(f"  [Folder Router] No matching folder for {filename}.")
                        # Identified tags remain useful even when folder selection abstains.
                        # Respect the saved metadata toggle and selected categories.
                        self.embed_metadata(file_path, tag_dict)
                        file_committed = self._handle_unmatched(file_path, filename, match_thumb, matched_url, route_reason)
                        self.file_in_progress = False
                        self.after(0, lambda: self.btn_skip.configure(state="normal", fg_color=("#ffffff", "#20242b")))
                        if "Single" in exec_mode:
                            break
                        else:
                            continue

                    final_dest_path = os.path.join(dest_path, target_folder)
                    with timed_stage(self, "move.directory"): os.makedirs(final_dest_path, exist_ok=True)
                    target_file_path = os.path.join(final_dest_path, filename)
                    target_file_path = safe_move(file_path, target_file_path)
                    file_committed = True

                    self.embed_metadata(target_file_path, tag_dict)

                    tracker.files_moved += 1
                    tracker.matched_files_moved += 1
                    self.update_metrics_display()

                    self.log(f"  -> Moved into: /{target_folder}/{filename}\n")
                    self.add_history_item(file_path, target_file_path, filename, is_unmatched=False, match_thumb_url=match_thumb, match_url=matched_url,
                                          reason=f"{chosen_match.get('engine', engine_choice)}" + (f" · {chosen_match['similarity']:.1f}% similarity" if chosen_match.get('similarity', 0) > 0 else "") + f" · {route_reason}")

                    self.file_in_progress = False
                    self.after(0, lambda: self.btn_skip.configure(state="normal", fg_color=("#ffffff", "#20242b")))

                    if "Single" in exec_mode:
                        break

                except providers.ProcessingStopped:
                    self.log('[User Action] Search cancelled; current image remains pending for Resume.')
                except providers.ImageSkipped:
                    self.log('[User Action] Current image skipped; pending requests disconnected.')
                except Exception as exc:
                    self.log(f"[File Error] {file_path}: {exc}; continuing batch.")
                    if os.path.exists(file_path):
                        self.add_history_item(file_path, file_path, os.path.basename(file_path), True, reason=f"File error: {exc}")
                finally:
                    if self.skip_requested and not self.stop_requested and not file_committed and os.path.isfile(file_path):
                        self.add_history_item(file_path, file_path, os.path.basename(file_path), True, reason='Skipped by user; left in source')
                    self.release_processing_file(file_path)
                    self.file_in_progress = False
                    # Interrupted requests retry this file. Completed moves must never
                    # be repeated even if Stop arrived immediately after the move.
                    if not self.stop_requested or file_committed:
                        self.session_store.advance(idx)
                    self.after(0, lambda: self.btn_skip.configure(state="normal", fg_color=("#ffffff", "#20242b")))

            self.log("=== Execution Queue Stopped/Complete ==.")
        except providers.ProcessingStopped:
            self.log('[User Action] Batch setup cancelled.')
        except Exception as e:
            self._workflow_error = str(e)
            self.log(f"Critical Workflow Error: {e}")
            self.log(traceback.format_exc(), full_only=True)
        finally:
            if "client" in locals():
                try:
                    client.close()
                except Exception:
                    pass
            self.after(0, self.save_settings)
            self.file_in_progress = False
            stopped = self.stop_requested
            def _reset_final():
                self.is_processing = False
                self.stop_requested = False
                self.skip_requested = False
                self.btn_start.configure(state="normal", fg_color=("#256b55", "#8ac9af"))
                self.btn_skip.configure(state="disabled", fg_color=("#ffffff", "#20242b"))
                self.btn_cancel.configure(state="disabled", text="Stop after current request", fg_color=("#ffffff", "#20242b"))
                self.title("Tag-AI Booru Image Sorter")
                self.lbl_run_status.configure(text=("Batch could not finish: " + self._workflow_error) if self._workflow_error else ("Batch stopped" if stopped else "Batch complete"))
                self.lbl_history_counter.configure(text="Ready · local library")
                self._restore_session()
                self.refresh_history_view()
            self.after(0, _reset_final)

