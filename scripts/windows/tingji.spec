# PyInstaller onedir: one GUI executable and one hidden-console worker.
import json
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

stage = Path(os.environ["TINGJI_BUILD_STAGE"]).resolve()
app = stage / "app"
speaker = stage / "vendor" / "3d-speaker"
version_file = stage / "version_info.txt"
datas = [(str(app / "assets"), "assets"), (str(app / "web" / "dist"), "web/dist"),
         (str(speaker / "pretrained_models"), "models/3d-speaker/pretrained_models"),
         (str(stage / "vendor" / "licenses"), "licenses")]
binaries = [(str(p), "bin") for p in sorted((stage / "vendor" / "ffmpeg" / "bin").iterdir())]
datas += collect_data_files("silero_vad")
datas += collect_data_files("webview")
for distribution in ("torch", "torchaudio", "silero-vad", "onnxruntime", "numpy", "scipy", "scikit-learn", "pywebview"):
    datas += copy_metadata(distribution)

a = Analysis(
    [str(app / "desktop.py")],
    pathex=[str(app), str(speaker)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["webview.platforms.winforms", "webview.platforms.edgechromium", "pythonnet", "clr_loader",
                   "meeting_tool.diarization_worker", "speakerlab.models.campplus.DTDNN",
                   "speakerlab.models.campplus.layers", "sklearn.utils._cython_blas"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torchvision", "tensorflow", "transformers", "triton", "matplotlib", "IPython", "notebook", "pytest",
              "soundfile", "_soundfile", "_soundfile_data"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
common = dict(exclude_binaries=True, debug=False, bootloader_ignore_signals=False,
              strip=False, upx=False, icon=str(app / "assets" / "tingji.ico"),
              version=str(version_file), contents_directory="_internal")
gui = EXE(pyz, a.scripts, [], name="Tingji", console=False, **common)
worker = EXE(pyz, a.scripts, [], name="TingjiSpeaker", console=True, **common)
coll = COLLECT(gui, worker, a.binaries, a.datas, strip=False, upx=False, name="Tingji")
