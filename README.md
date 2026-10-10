# Tag-AI Booru Anime Image Sorter

A robust, local-first Python desktop application built with **CustomTkinter** for automatically sorting, tagging, and organizing image and artwork libraries. It combines traditional booru reverse image search engines (IQDB, SauceNAO) with local Vision LLMs (via LM Studio / Ollama) and intelligent local folder routing.

---

## Features

- **Dual Execution Modes:** Choose between automated full-folder batch processing or single-file review mode.
- **Multi-Engine Reverse Search:** Integrates **IQDB**, **SauceNAO**, **Google Lens (SerpApi)**, and **Google Cloud Vision** with configurable search engine and local vision model fallbacks.
- **Intelligent Folder Routing:** Uses local LLMs to route images into your existing library subfolders with strict programmatic safeguards against directory hallucinations.
- **Metadata Embedding:** Automatically embeds Danbooru-style tags into PNG metadata chunks or JPEG EXIF data (`piexif`).
- **Safety & Watchdog Controls:** Includes multi-tier rate limiters, a 60-second watchdog timer, and an instant **"Skip Current File"** button to prevent hanging on unresponsive requests.
- **Interactive History Dashboard:** View thumbnails, inspect source vs. matched artwork, open files externally, view folders, or undo recent moves.

---

## Prerequisites & Dependencies

Ensure you have **Python 3.8 or higher** installed on your system.

### Required Python Packages
Install all required dependencies with pip:

\`\`\`bash
pip install customtkinter pillow opencv-python openai beautifulsoup4 requests piexif
\`\`\`

*(Optional)* If you plan to build a `requirements.txt` file for your repository, you can generate it via:
\`\`\`bash
pip freeze > requirements.txt
\`\`\`

---

## Installation & Running

1. **Clone the repository:**
   \`\`\`bash
   git clone https://github.com/YOUR_USERNAME/tag-ai-booru-sorter.git
   cd tag-ai-booru-sorter
   \`\`\`

2. **Install dependencies:**
   \`\`\`bash
   pip install -r requirements.txt
   \`\`\`

3. **Launch the application:**
   \`\`\`bash
   python main.py
   \`\`\`

---

## Compiling into a Standalone Executable

If you want to compile the script into a standalone executable (e.g., a `.exe` file on Windows) so you can run it without a Python terminal:

1. **Install PyInstaller:**
   \`\`\`bash
   pip install pyinstaller
   \`\`\`

2. **Build the executable:**
   \`\`\`bash
   pyinstaller --noconsole --onefile --name "TagAISorter" main.py
   \`\`\`

3. Your standalone binary will be generated inside the newly created `dist/` folder.

---

## Configuration & Usage

- **Local LLM Setup:** If utilizing local vision tagging or folder routing, make sure your local LLM server (such as **LM Studio** or **Ollama**) is running and exposing an OpenAI-compatible endpoint (default: `http://localhost:1234/v1`).
- **Config Persistence:** The app automatically saves your source/destination folders, API keys, and UI toggles to a local `config.json` file in the application directory.

---

## Notes on LLM usage

I've tested many different local models for this program, I would say the bare minimum for it to be usable (without the automatic tag feature with vision tool), is a 1B model like gemma 3.
Of course, the better model you use the better results you'll get, since it's very likely the AI will make mistakes when assigning a proper folder, or just make one up and be filtered by the program if it can't find an appropriate one.
Ministral 3 3B seemed like the sweet spot for me in terms of minimum errors and size of the model.

For the vision tool, I've gotten favorable results using Qwen 3.5 9B, just remember to disable the thinking module as it'll get the response stuck on a loop and end up timing out.
The vision feature works surprisingly well when backed up by a Google lense API, but the free usage credits for those are considerably low, so it's better to keep an eye on the API call counter when using them. 
I recommend toggling the "Treat as Unmatched if Web Search fails" option on, since the fallback to the local vision trying to apply its own tags is highly innacurate, but the option is still there in case you want to experiment with some more powerful LLMs.


Thanks to prushka and ningyou for the brainstorming ideas.