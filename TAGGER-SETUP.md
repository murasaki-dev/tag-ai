# Optional tagger setup

Tagger models are optional downloads. **No model weights are bundled with the executable.** The executable includes ONNX inference libraries, which are runtime dependencies rather than a model.

The supporting classifier supplies evidence to the vision model alongside web searches. It does not replace the vision model, and classifier scores are not treated as verified identities.

## Managed tagger

1. Open **Settings → Local model config → Supporting image tagger**.
2. Choose EVA02 or PixAI and select **Managed by app**.
3. For PixAI, use **Install PixAI runtime** to install its optional CPU PyTorch dependencies. Python is required to create this environment; EVA02 needs no separate Python installation in the packaged app.
4. Click **Download selected model** to fetch the selected weights.
5. Enable the supporting tagger. **Start / check tagger** checks the helper; it does not download weights.

Search-time inference uses downloaded files only. Missing weights produce a download instruction instead of a download during processing. Downloads may be large and run in the background. Installation progress is recorded in `tagger-runtime-install.log`.

**Delete selected model** removes only app-managed weights under `tagger-models`. Stop the batch first. Custom model files and shared Hugging Face caches are not deletion targets. Previously cached evidence remains available independently of downloaded weights.

## Dependencies

There is one dependency file: `requirements.txt`.

```sh
python -m pip install -r requirements.txt
```

This installs core application and ONNX tagger dependencies. Optional PixAI packages are declared as `# pixai:` entries in that same file and installed by **Install PixAI runtime**; ordinary core installs do not pull in PyTorch. Runtime packages and model weights are separate downloads.

The helper retains one loaded classifier and serializes inference. Current presets use CPU; hardware acceleration is not configured.

## External service

Choose **External service** to use a compatible helper you manage. From an environment with the dependencies installed:

```sh
python tagger.py --port 7861
```

For a virtual environment, use `.tagger-env/bin/python` on Linux or `.tagger-env/Scripts/python.exe` on Windows. `--model-dir PATH` selects the directory containing app-managed preset downloads; point it at the same `tagger-models` directory used by the app when sharing weights.

The default endpoint is `http://127.0.0.1:7861/tag`. The helper binds to loopback and is intended for one local user. `/health` reports service/runtime capabilities without inference. An older helper already occupying a port must be restarted separately, or select another endpoint.

The app stops only helpers it starts itself. External helper lifetime, custom model downloads, and custom files remain your responsibility.

## Custom models

For ONNX models with compatible NHWC input and a tag CSV, start the helper with `--custom-model MODEL.onnx --custom-labels TAGS.csv`, then select `custom-onnx` in the app. Custom classifiers must return categorized scores suitable for the evidence thresholds.

Compatible Transformers pipelines can be selected by their configured repository/path. Custom repository code is disabled unless explicitly enabled with `--trust-custom-code`. The known PixAI preset uses the code shipped with its downloaded repository.

References:
- https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3
- https://huggingface.co/pixai-labs/pixai-tagger-v1.0
