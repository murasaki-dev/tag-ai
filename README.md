# Tag-AI

A Python desktop application for sorting and tagging image libraries, with a Qt Quick interface.

Development happens against `homelab/tag-ai` on the homelab Gitea instance; `murasaki-dev/tag-ai` on GitHub follows manually.

## Features

- IQDB and SauceNAO reverse searches, with booru tag inheritance.
- Optional exact-file MD5 lookups before reverse search, using enabled Danbooru, Gelbooru, Yande.re and Konachan database flows.
- Optional local vision models supported by web and classifier evidence.
- Separate vision and folder-routing model selection.
- Exact or flexible routing into existing series/character folders.
- PNG/JPEG metadata, resumable batches, and optional unmatched destinations.
- History with comparison thumbnails, paging, undo, tag removal, and cache clearing.
- Configurable flows, per-step timings, logs, and per-key request counts.

Uncertain images stay in their source folder by default. Folder decisions are made afresh; identification and web evidence can be cached. Settings apply between steps, while active requests keep their original inputs. The source queue stays fixed until a new batch.

Exact booru hash lookup is enabled by default and can be toggled in the flowchart or reverse-search settings. A returned MD5 must equal the original file's MD5; modified or recompressed copies continue through normal reverse search. Existing successful identification caches remain reusable. When a matched post has no usable tags, lookup may follow explicit post/source links to enabled databases, with bounded hops and cycle detection. Shared-source searches require a unique exact source URL match; artist profiles and ambiguous results are not accepted. Usable booru tags bypass local vision/tagging. Database fallback order and reverse-search confidence thresholds are unchanged.

Folder routing can use booru-maintained identity aliases. Enable **Use booru-maintained identity aliases** in Local model config → Folder routing. When Danbooru is enabled in a reverse-search database flow, active Danbooru copyright/character aliases help match alternate existing folder names. Already-resolved matches need no alias request; unresolved identities use at most four requests per image. General/artist tags and tag implications are excluded. Raw and embedded tags, folder names, and user-entered folder aliases are not changed. Generated vision identities remain unverified even when their names have known aliases.

Alias mappings are shared across images and independent of model selection or folder decisions. Positive entries refresh after 30 days; completed empty results expire after six hours. Network/authentication failures are not stored as empty results. Per-image cache clearing leaves shared alias knowledge intact; the maintenance cache-clear action removes it. Detailed logs and routing substep timings identify alias requests and mappings.

## Run from source

Use Python 3.10 or newer:

```sh
python -m venv .venv
```

Activate with `source .venv/bin/activate` on Linux, or `.venv\Scripts\Activate.ps1` in Windows PowerShell, then:

```sh
python -m pip install -r requirements.txt
python qt_app.py
```

To use the packaged application's existing data:

```sh
python qt_app.py --data-dir dist
```

Configure source/destination folders and enabled services in Settings. Model features require an OpenAI-compatible local server; the default endpoint is `http://localhost:1234/v1`. External services require their own credentials. Credentials are stored locally in `config.json`; do not commit it.

`qt_app.py` is the application entry point. The old launcher and standalone preview have been removed.

## Build

Install PyInstaller in the environment containing the application dependencies:

```sh
python -m pip install pyinstaller
pyinstaller TagAI.spec --distpath dist
```

On Windows, the executable is `dist/TagAI-Qt.exe`. Editing source does not update an existing executable.

Build separately on each target operating system. The Windows executable cannot run natively on Linux. Tagger environments use platform-specific interpreter paths. Linux compatibility still needs validation on a Linux machine.

Keep `packaging-hooks/`: it handles local-module resolution, lazy download dependencies, and optional SSL companion data used by some Python distributions. TLS verification remains enabled.

All dependency declarations are in `requirements.txt`. Core requirements are installed normally; commented `# pixai:` entries declare optional packages used by the in-app PixAI runtime installer. They are not bundled into the desktop executable.

## Supporting tagger

The executable contains no model weights. It includes ONNX inference libraries so EVA02 can run after you download its weights. Choose a tagger in **Settings → Local model config**, then click **Download selected model**. Searches do not download preset weights automatically.

PixAI needs additional Python/PyTorch dependencies. **Install PixAI runtime** installs its CPU environment into `.tagger-env`; creating that environment requires Python. Model weights are downloaded separately. Installation progress is saved in `tagger-runtime-install.log`.

**Delete selected model** removes only app-managed weights. Stop the batch before deleting weights or changing the runtime. External services, custom files, and shared Hugging Face caches are not deleted. See [TAGGER-SETUP.md](TAGGER-SETUP.md) for manual setup.

Classifier predictions support the vision model rather than replacing it. Restart an outdated helper separately, or choose another endpoint.

## Application data

Packaged runs store configuration, databases, models, and logs beside the executable. Source runs use the source directory by default; `--data-dir` selects a different data directory.

| File | Purpose |
| --- | --- |
| `config.json` | Settings and credentials. |
| `operation_history.sqlite3` | History. |
| `image_result_cache.sqlite3` | Identification/context caches. |
| `batch_session.sqlite3` | Resumable batch state. |
| `routing_decisions.jsonl` | Routing diagnostics. |
| `tagai-runtime.log`, `tagai-crash.log` | Runtime/crash diagnostics. |

Keep SQLite sidecars with their databases while running. Local data, environments, logs, and builds are excluded by `.gitignore`.

This working copy links `dist/.tagger-env` to the optional environment in the project root. Keep the target if using that environment. Earlier packaged settings/cache are preserved in `dist/legacy-data`.

## Source layout

| Path | Responsibility |
| --- | --- |
| `qt_app.py`, `qml/` | Application entry point, desktop backend and interface. |
| `qt_core.py` | Shared History models, asynchronous thumbnails and desktop file actions. |
| `workflow.py` | Batch processing, skip/stop, and resume. |
| `providers.py` | Search/model clients, evidence, rate limits, and usage. |
| `routing.py` | Folder shortlists, prompts, and response validation. |
| `storage.py` | Settings, caches, metadata, moves, and checkpoints. |
| `tagger.py` | Classifier helper, model management, and inference. |
| `stage_timing.py` | Per-step timings. |
| `ui.py`, `history.py` | Previous interface and its History implementation. |
| `tests/` | Regression tests and packaged checks. |

## Checks

```sh
python -m unittest discover -s tests -v
```

Windows packaged workflow check (temporary files and loopback service only):

```sh
python tests/frozen_app_smoke.py dist/TagAI-Qt.exe
```

The optional real ONNX helper check additionally requires the development-only `onnx` package:

```sh
python tests/frozen_tagger_smoke.py dist/TagAI-Qt.exe
```

## Credits

Thanks to prushka and ningyou for brainstorming ideas.
