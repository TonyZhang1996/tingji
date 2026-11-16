"""Explicit offline package verification using synthetic audio only."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import wave

from .audio import find_binary, _run, normalize_audio, probe_audio
from .runtime_paths import (is_frozen, resource_root, speaker_root,
                            speaker_executable, user_data_root)


def check_runtime(output: Path) -> int:
    """Write a local diagnostic file; never inspect personal settings/results."""
    from .version import __version__
    result = {"version": __version__, "frozen": is_frozen(),
              "resource_root": str(resource_root()), "data_root": str(user_data_root()),
              "checks": {}, "ok": False}
    checks = result["checks"]
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    active_check = "ffmpeg"
    try:
        for name in ("ffmpeg", "ffprobe"):
            active_check = name
            version = _run([find_binary(name), "-version"], timeout=15).decode("utf-8", errors="replace").splitlines()[0]
            checks[name] = {"ok": True, "version": version[:250]}
        active_check = "python_dependencies"
        import numpy as np
        import torch
        import torchaudio
        from sklearn.cluster import KMeans
        from silero_vad import load_silero_vad, get_speech_timestamps
        torch.set_num_threads(1)
        cpu_build = torch.version.cuda is None
        checks["torch"] = {"ok": cpu_build if is_frozen() else True,
                           "version": torch.__version__, "cpu_build": cpu_build}
        checks["torchaudio"] = {"ok": True, "version": torchaudio.__version__}
        active_check = "synthetic_audio"
        with tempfile.TemporaryDirectory(prefix="tingji-self-check-", dir=output.parent) as folder:
            source = Path(folder) / "synthetic.wav"
            normalized = Path(folder) / "normalized.wav"
            with wave.open(str(source), "wb") as writer:
                writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                writer.writeframes(bytes(16000 * 2))
            normalize_audio(source, normalized)
            info = probe_audio(normalized)
            if info["sample_rate"] != 16000 or info["channels"] != 1:
                raise RuntimeError("synthetic_audio")
            checks["audio"] = {"ok": True, "sample_rate": info["sample_rate"], "duration": info["duration_seconds"]}
            from .wav_reader import PCM16Reader
            with PCM16Reader(normalized) as reader:
                samples = reader.read(16000)
                if samples.shape != (16000,) or samples.dtype != np.float32 or samples.any():
                    raise RuntimeError("pcm16_reader")
            checks["pcm16_reader"] = {"ok": True, "frames": len(samples)}
        active_check = "silero_onnx"
        vad = load_silero_vad(onnx=True)
        regions = get_speech_timestamps(torch.zeros(16000), vad, sampling_rate=16000)
        if regions:
            raise RuntimeError("silent_vad")
        checks["silero_onnx"] = {"ok": True, "silent_regions": 0}
        active_check = "campplus"
        root = speaker_root()
        if not is_frozen():
            from .speaker_backend import _configuration
            _python, root = _configuration()
        sys.path.insert(0, str(root))
        from speakerlab.models.campplus.DTDNN import CAMPPlus
        model = CAMPPlus(feat_dim=80, embedding_size=192)
        model.load_state_dict(torch.load(root / "pretrained_models/campplus/campplus_cn_en_common.pt",
                                         map_location="cpu", weights_only=True))
        model.eval()
        features = torchaudio.compliance.kaldi.fbank(torch.zeros(1, 16000), num_mel_bins=80,
                                                    sample_frequency=16000, dither=0)
        features -= features.mean(0, keepdim=True)
        with torch.inference_mode():
            vector = model(features.unsqueeze(0)).squeeze(0)
        if vector.shape != (192,) or not bool(torch.isfinite(vector).all()):
            raise RuntimeError("speaker_vector")
        checks["campplus"] = {"ok": True, "dimensions": int(vector.numel()), "device": str(vector.device)}
        active_check = "clustering"
        grouped = KMeans(n_clusters=2, n_init=2, random_state=0).fit_predict(
            np.array([[0., 0.], [.1, .1], [1., 1.], [1.1, 1.1]]))
        if len(set(grouped.tolist())) != 2:
            raise RuntimeError("clustering")
        checks["clustering"] = {"ok": True, "groups": 2}
        checks["speaker_helper"] = {"ok": speaker_executable().is_file() if is_frozen() else True}
        checks["web_ui"] = {"ok": (resource_root() / "web/dist/index.html").is_file()}
        result["ok"] = all(value["ok"] for value in checks.values())
    except Exception as exc:
        # Details can contain local filesystem paths. Keep this diagnostic safe
        # to review without echoing exception text, credentials or user files.
        result["error_type"] = type(exc).__name__
        result["failed_check"] = active_check
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1
