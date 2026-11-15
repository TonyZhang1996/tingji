# Tingji / 听记

[简体中文](README.md) · **English**

A Windows desktop tool for meeting recordings: transcribe audio by speaker, listen back and correct the text, then generate a meeting summary with action items at the end.

The interface uses React, shadcn/ui and pywebview. Speaker analysis runs locally; transcription and summaries use the DeepSeek API.

## Features

- **Long recordings:** automatic segmentation, immediate saving of successful segments and resumable processing. Continuing a task skips segments already completed.
- **Speaker review:** consistent speaker IDs throughout a recording, voice samples, editable names and corrections to text or speaker assignments.
- **Playback and exports:** jump to a segment for review; export TXT, Markdown, JSON, CSV or a segment timeline.
- **AI summaries:** process long transcripts in parts and merge them, ending with action items, owners, deadlines and recording references. Summarizing an incomplete transcript requires an explicit partial-coverage choice.

## Getting started

You need Windows, Python 3.11, Microsoft Edge WebView2, and `ffmpeg` and `ffprobe` available on `PATH`. Alternatively, place the FFmpeg executables in the project's `bin` directory.

After cloning or downloading the repository, open PowerShell in the project directory:

```powershell
py -3.11 -m venv .runtime
.\.runtime\Scripts\python.exe -m pip install -r requirements_desktop.txt
```

This installs only the desktop dependencies. **The speaker-analysis environment and model must be prepared separately; model weights are not included.**

Speaker analysis uses Silero VAD, CAM++ and 3D-Speaker. A compatible separate Python environment needs `torch`, `torchaudio`, `soundfile`, `silero-vad`, `onnxruntime`, `numpy` and `scikit-learn`. The 3D-Speaker directory must contain `pretrained_models/campplus/campplus_cn_en_common.pt`.

Replace these example paths with your own locations, then launch from the same PowerShell window:

```powershell
$env:MEETING_SPEAKER_PYTHON = "D:\Envs\meeting-speaker\python.exe"
$env:MEETING_SPEAKER_ROOT = "D:\Models\3D-Speaker"
.\.runtime\Scripts\python.exe -X utf8 desktop.py
```

Once the dependencies are configured, you can also use `启动工具.vbs`. Use `启动工具.cmd` to retain error messages. The repository includes `web/dist`, so Node.js is not needed for normal use.

## Workflow

Choose a recording → specify the speaker count → analyze and name speakers using voice samples → enter a DeepSeek API key and transcribe → review the recording → summarize or export.

The API key stays in the current window's memory and is not written to settings, logs or task results. ASR is charged by uploaded audio; summaries incur separate text-model charges. See [DeepSeek's pricing documentation](https://api-docs.deepseek.com/quick_start/pricing/). Requests with an uncertain outcome are not automatically resubmitted, to avoid duplicate charges.

## Privacy and limitations

Speaker analysis runs locally. Transcription sends audio segments to DeepSeek, while summarization sends transcript text and segment timestamps. Audio caches, transcripts, corrections and summaries are stored locally; the tool does not automatically upload them to GitHub.

The `.gitignore` excludes personal settings, recordings, results, caches, credential files, runtime environments and `node_modules`. Do not force-add these files, even to a private repository.

Before committing, run `.\.runtime\Scripts\python.exe scripts/check_publish.py` to check the Git index against the source allowlist and credential patterns. Add `--ref HEAD` to inspect a commit instead. The check does not print matching secrets or read ignored recordings.

Review speaker assignments, numbers and names against the recording. Timestamps identify segments rather than individual words, and overlapping speech is not separated. A partial transcript cannot support a complete recording summary. Owners or deadlines not explicitly stated are marked as unspecified.

## Development and checks

Rebuilding the interface requires Node.js and npm:

```powershell
cd web
npm ci
npm run build
```

From the project root, run the full test suite with a Python environment containing the speaker-analysis dependencies:

```powershell
& $env:MEETING_SPEAKER_PYTHON -X utf8 -m unittest discover -s tests -v
```

The local full regression passed 82 tests. Tests use temporary synthetic audio and simulated services, with no real paid API calls. This does not establish recognition accuracy on real recordings.

shadcn/ui component sources and third-party notices are retained in [component sources](web/COMPONENT-SOURCES.txt) and the [shadcn/ui license](web/SHADCN-LICENSE.txt).
