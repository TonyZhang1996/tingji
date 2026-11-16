"""Offline, bounded-memory Silero VAD + CAM++ global speaker clustering.

Windows installations use bundled models and dependencies; source checkouts can
reuse an existing environment. This process never downloads models, transcribes
speech, or accesses an API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import traceback

VERSION = "speaker-v5-kmeans-global"
SAMPLE_RATE = 16000
MAX_TURN_SECONDS = 110
_dll_handles = []


def emit(event: str, **data):
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def atomic_json(path: Path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def signature(wav: Path, speakers: int, model: Path):
    st, model_st = wav.stat(), model.stat()
    digest = hashlib.sha256()
    with wav.open("rb") as stream:
        digest.update(stream.read(65536))
        if st.st_size > 65536:
            stream.seek(max(0, st.st_size - 65536))
            digest.update(stream.read(65536))
    return {"algorithm": VERSION, "wav": str(wav), "size": st.st_size,
            "mtime_ns": st.st_mtime_ns, "edge_sha256": digest.hexdigest(),
            "speakers": speakers, "model_size": model_st.st_size,
            "model_mtime_ns": model_st.st_mtime_ns}


def merge_regions(regions):
    result = []
    for start, end in sorted(regions):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([int(start), int(end)])
    return result


def make_windows(regions, width=28800, hop=14400):
    """Windows never cross silence; all regions have at least one window."""
    windows = []
    for region_index, (start, end) in enumerate(regions):
        if end - start <= width:
            windows.append((start, end, region_index))
            continue
        for left in range(start, end - width + 1, hop):
            windows.append((left, left + width, region_index))
        if windows[-1][1] < end:
            windows.append((end - width, end, region_index))
    return windows


def make_turns(regions, windows, labels, uncertain):
    """Center boundaries cover every detected speech frame without overlap."""
    raw = []
    cursor = 0
    for index, (start, end) in enumerate(regions):
        members = []
        while cursor < len(windows) and windows[cursor][2] == index:
            members.append(cursor)
            cursor += 1
        left = start
        for position, window_index in enumerate(members):
            current = windows[window_index]
            if position + 1 < len(members):
                following = windows[members[position + 1]]
                right = round(((current[0] + current[1]) + (following[0] + following[1])) / 4)
            else:
                right = end
            if right <= left:
                continue
            label = int(labels[window_index])
            mark = bool(uncertain[window_index])
            # Brief pauses within the same person's speech retain ASR context.
            # Actual original time, including the pause, is never renumbered.
            uncertain_samples = (right - left) if mark else 0
            if raw and raw[-1][2] == label and left - raw[-1][1] <= SAMPLE_RATE:
                raw[-1][1] = right
                raw[-1][3] += uncertain_samples
            else:
                raw.append([left, right, label, uncertain_samples])
            left = right
    # Assign stable user-facing IDs by first occurrence over the entire meeting.
    names = {}
    turns = []
    max_samples = MAX_TURN_SECONDS * SAMPLE_RATE
    for start, end, label, uncertain_samples in raw:
        speaker = names.setdefault(label, f"S{len(names) + 1}")
        # A brief uncertain reply must not disappear into a longer confident turn.
        mark = uncertain_samples > 0
        for left in range(start, end, max_samples):
            right = min(end, left + max_samples)
            turns.append({"start": round(left / SAMPLE_RATE, 4),
                          "end": round(right / SAMPLE_RATE, 4),
                          "speaker": speaker, "uncertain": mark or right - left < 12800})
    return turns


def detect_speech(wav: Path, frames: int, checkpoint: Path, input_signature):
    from meeting_tool.wav_reader import PCM16Reader
    import torch
    from silero_vad import load_silero_vad, get_speech_timestamps
    state = {"signature": input_signature, "next_sample": 0, "regions": []}
    if checkpoint.is_file():
        try:
            cached = json.loads(checkpoint.read_text(encoding="utf-8"))
            if cached.get("signature") == input_signature:
                state = cached
        except (OSError, ValueError):
            pass
    if state["next_sample"] >= frames:
        emit("progress", message="已恢复语音检测结果")
        return merge_regions(state["regions"])
    model = load_silero_vad(onnx=True)
    block = 180 * SAMPLE_RATE
    overlap = 2 * SAMPLE_RATE
    with PCM16Reader(wav) as audio:
        for base in range(int(state["next_sample"]), frames, block):
            read_start = max(0, base - overlap)
            read_end = min(frames, base + block + overlap)
            audio.seek(read_start)
            data = audio.read(read_end - read_start)
            regions = get_speech_timestamps(torch.from_numpy(data), model,
                       sampling_rate=SAMPLE_RATE, threshold=0.45,
                       min_speech_duration_ms=120, min_silence_duration_ms=250,
                       speech_pad_ms=60)
            for region in regions:
                left, right = read_start + region["start"], read_start + region["end"]
                # Core ownership avoids duplicates but overlap prevents VAD cuts.
                left, right = max(base, left), min(base + block, right, frames)
                if right > left:
                    state["regions"].append([left, right])
            state["regions"] = merge_regions(state["regions"])
            state["next_sample"] = min(frames, base + block)
            atomic_json(checkpoint, state)
            emit("progress", message=f"检测发言位置：{state['next_sample'] / frames:.0%}",
                 phase="vad", completed=state["next_sample"], total=frames)
    return state["regions"]


def extract_embeddings(wav, windows, checkpoint, signature_data, speaker_root):
    import numpy as np
    from meeting_tool.wav_reader import PCM16Reader
    import torch
    import torchaudio.compliance.kaldi as kaldi
    from speakerlab.models.campplus.DTDNN import CAMPPlus
    vectors = np.zeros((len(windows), 192), dtype="float32")
    completed = 0
    if checkpoint.is_file():
        try:
            with np.load(checkpoint, allow_pickle=False) as saved:
                if (json.loads(str(saved["signature"].item())) == signature_data
                        and saved["vectors"].shape == vectors.shape):
                    vectors[:] = saved["vectors"]
                    completed = int(saved["completed"].item())
                    if not 0 <= completed <= len(windows):
                        completed = 0
        except (OSError, ValueError, KeyError):
            completed = 0
    if completed == len(windows):
        emit("progress", message="已恢复全场声纹特征")
        return vectors
    model = CAMPPlus(feat_dim=80, embedding_size=192)
    model_path = speaker_root / "pretrained_models/campplus/campplus_cn_en_common.pt"
    model.load_state_dict(torch.load(model_path, map_location="cpu", weights_only=True))
    model.eval()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        model.to(device)
    except RuntimeError:
        device = "cpu"
        model.to(device)
    emit("progress", message=f"正在分析整场声纹（{'显卡' if device == 'cuda' else 'CPU'}），已完成 {completed}/{len(windows)}")

    def save_checkpoint(count):
        temporary = checkpoint.with_suffix(".tmp.npz")
        np.savez(temporary, signature=json.dumps(signature_data, sort_keys=True),
                 vectors=vectors, completed=count)
        os.replace(temporary, checkpoint)

    with PCM16Reader(wav) as audio, torch.inference_mode():
        for index in range(completed, len(windows)):
            start, end, _ = windows[index]
            audio.seek(start)
            data = audio.read(end - start)
            if not len(data):
                raise RuntimeError("读取声纹音频失败，录音文件可能已改变")
            piece = torch.from_numpy(data)
            if len(piece) < 6400:
                piece = piece.repeat(math.ceil(6400 / len(piece)))[:6400]
            features = kaldi.fbank(piece.unsqueeze(0), num_mel_bins=80,
                                   sample_frequency=SAMPLE_RATE, dither=0)
            features -= features.mean(0, keepdim=True)
            try:
                vector = model(features.unsqueeze(0).to(device)).squeeze(0).cpu().numpy()
            except RuntimeError as error:
                if device != "cuda":
                    raise
                emit("progress", message="显卡暂不可用，自动改用 CPU 继续人物分析")
                device = "cpu"
                model.to(device)
                torch.cuda.empty_cache()
                vector = model(features.unsqueeze(0)).squeeze(0).numpy()
            vectors[index] = vector / max(float(np.linalg.norm(vector)), 1e-12)
            if (index + 1) % 50 == 0 or index + 1 == len(windows):
                save_checkpoint(index + 1)
                emit("progress", message=f"分析声纹：{index + 1}/{len(windows)}",
                     phase="embeddings", completed=index + 1, total=len(windows))
    return vectors


def cluster(windows, vectors, known_count, *, diagnostics=None):
    """Globally group unit speaker vectors without chaining two speakers together.

    Average-linkage clustering can reserve a whole requested speaker for a few
    outliers and merge the two main voices. Multi-start KMeans minimizes each
    window's distance to a global center instead. It does not impose equal sizes.
    """
    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    vectors = np.asarray(vectors, dtype="float32")
    if (vectors.ndim != 2 or len(vectors) != len(windows) or not len(windows)
            or not np.isfinite(vectors).all()):
        raise ValueError("声纹特征不完整，请重新分析人物")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if np.any(norms < 1e-8):
        raise ValueError("声纹特征无效，请重新分析人物")
    vectors = vectors / norms
    reliable = np.array([index for index, (start, end, _) in enumerate(windows)
                         if end - start >= 16000], dtype="int64")
    if known_count > 1 and len(reliable) < known_count:
        raise ValueError(f"只有 {len(reliable)} 个至少 1 秒的可靠声纹片段，不足以区分 {known_count} 人；请减少人数或选择更长录音")
    if not len(reliable):
        reliable = np.arange(len(windows))
    if known_count > len(reliable):
        raise ValueError(f"只有 {len(reliable)} 个可用声纹片段，不足以区分 {known_count} 人；请减少人数或选择更长录音")
    # Keep training and count selection bounded for multi-hour recordings. Every
    # remaining window is assigned against the same meeting-wide centers.
    if len(reliable) > 1800:
        reliable = reliable[np.linspace(0, len(reliable) - 1, 1800, dtype="int64")]
    train = vectors[reliable]
    def fit(count):
        import warnings
        from sklearn.exceptions import ConvergenceWarning
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ConvergenceWarning)
            return KMeans(n_clusters=count, n_init=30, random_state=17).fit_predict(train)

    count_score = None
    if len(train) == 1 or known_count == 1:
        chosen = 1
        train_labels = np.zeros(len(train), dtype="int64")
    elif known_count:
        chosen = known_count
        train_labels = fit(chosen)
        if len(np.unique(train_labels)) != chosen:
            raise ValueError(f"当前声音特征不足以区分 {chosen} 人，请核对实际发言人数或选择更清晰的录音")
    else:
        # Auto-counting is provisional. Require coherent, separated centers and
        # several supporting windows, rather than treating a few outliers as an
        # extra person. Explicit known counts do not have this support filter.
        chosen = 1
        train_labels = np.zeros(len(train), dtype="int64")
        best_score = 0.20
        minimum_support = 2 if len(train) < 10 else 5
        for count in range(2, min(12, len(train) - 1) + 1):
            candidate = fit(count)
            if np.bincount(candidate, minlength=count).min() < minimum_support:
                continue
            centers = np.stack([train[candidate == label].mean(0) for label in range(count)])
            centers /= np.maximum(np.linalg.norm(centers, axis=1, keepdims=True), 1e-12)
            center_similarity = centers @ centers.T
            np.fill_diagonal(center_similarity, -1)
            if center_similarity.max() > 0.80:
                continue
            try:
                score = silhouette_score(train, candidate, metric="cosine",
                                         sample_size=min(400, len(train)), random_state=17) - 0.025 * count
            except ValueError:
                continue
            if score > best_score:
                best_score, chosen, train_labels = score, count, candidate
                count_score = float(score)
        emit("progress", message=f"暂估 {chosen} 位说话人；自动人数和人物归属均需核对，已知人数时建议填写人数")
    prototypes = []
    for label in range(chosen):
        centroid = train[train_labels == label].mean(axis=0)
        prototypes.append(centroid / max(float(np.linalg.norm(centroid)), 1e-12))
    prototype_matrix = np.stack(prototypes)
    similarities = vectors @ prototype_matrix.T
    labels = similarities.argmax(axis=1)
    # Reassign even training windows by the final unit centers: the uncertainty
    # score must describe the label actually returned, not a stale training label.
    ordered = np.sort(similarities, axis=1)
    confidence = similarities[np.arange(len(vectors)), labels]
    margin = np.ones(len(vectors)) if chosen == 1 else ordered[:, -1] - ordered[:, -2]
    uncertain = (confidence < 0.48) | (margin < 0.09)
    uncertain |= np.array([end - start < 12800 for start, end, _ in windows])
    if not known_count:
        uncertain[:] = True
    clustering_warnings = []
    if chosen > 1:
        center_similarity = prototype_matrix @ prototype_matrix.T
        np.fill_diagonal(center_similarity, -1)
        if float(center_similarity.max()) > 0.85:
            clustering_warnings.append("部分人物的声音特征非常相似，请回听确认人物归属；填写人数不代表已经可靠分开")
    # Do not replace isolated replies with the surrounding majority speaker.
    # A short interjection may be real; leave its label and show it as uncertain.
    if diagnostics is not None:
        diagnostics.update({"algorithm": VERSION, "training_windows": len(train),
                            "total_windows": len(windows), "speakers_requested": known_count,
                            "speakers_found": len(set(labels.tolist())),
                            "window_counts": np.bincount(labels, minlength=chosen).tolist(),
                            "automatic_count_score": count_score,
                            "warnings": clustering_warnings})
    return labels, uncertain


def assess_groups(turns, requested_count):
    """Warn about weak identity evidence; never force speakers to equal duration."""
    stats = {}
    for turn in turns:
        row = stats.setdefault(turn["speaker"], {"seconds": 0.0, "turns": 0, "longest_turn": 0.0})
        duration = turn["end"] - turn["start"]
        row["seconds"] += duration
        row["turns"] += 1
        row["longest_turn"] = max(row["longest_turn"], duration)
    warnings = []
    total = sum(row["seconds"] for row in stats.values())
    for speaker, row in stats.items():
        # A person who genuinely spoke briefly remains in the result. The small
        # amount of evidence is disclosed, not "fixed" by redistributing voices.
        weak = row["seconds"] < 3 or row["longest_turn"] < 2
        extreme = total > 30 and row["seconds"] / total < 0.03
        if weak or extreme:
            warnings.append(f"{speaker.replace('S', '说话人 ', 1)} 的有效发言或连续样例偏少，人物归属需回听确认；请勿仅凭人数判断分组正确")
            for turn in turns:
                if turn["speaker"] == speaker:
                    turn["uncertain"] = True
    if requested_count and len(stats) != requested_count:
        warnings.append(f"填写了 {requested_count} 人，但当前只找到 {len(stats)} 组有效声音，请核对人数和试听片段")
        for turn in turns:
            turn["uncertain"] = True
    return stats, warnings


def run(args):
    from meeting_tool.wav_reader import wav_info
    import torch
    wav, output, root = args.wav.resolve(), args.output.resolve(), args.speaker_root.resolve()
    if not 0 <= args.speakers <= 20:
        raise ValueError("会议人数应为 0（自动）或 1～20")
    model = root / "pretrained_models/campplus/campplus_cn_en_common.pt"
    if not model.is_file():
        raise FileNotFoundError(f"缺少本地声纹模型：{model}")
    sys.path.insert(0, str(root))
    torch.set_num_threads(4)
    output.parent.mkdir(parents=True, exist_ok=True)
    input_signature = signature(wav, args.speakers, model)
    info = wav_info(wav)
    if info.frames <= 0:
        raise ValueError("录音为空")
    if output.is_file():
        try:
            saved = json.loads(output.read_text(encoding="utf-8"))
            if saved.get("signature") == input_signature:
                emit("progress", message="已恢复整场人物分析结果")
                emit("result", path=str(output), speaker_count=saved.get("speaker_count", 0))
                return
        except (OSError, ValueError):
            pass
    emit("progress", message=f"开始本地人物分析，录音 {info.duration / 3600:.2f} 小时；仅分块读入音频")
    feature_signature = {key: value for key, value in input_signature.items() if key != "speakers"}
    regions = detect_speech(wav, info.frames, output.with_name("speech_regions.json"), feature_signature)
    if not regions:
        raise RuntimeError("未检测到可辨认的发言，请检查录音音量或内容")
    windows = make_windows(regions)
    vectors = extract_embeddings(wav, windows, output.with_name("speaker_embeddings.npz"), feature_signature, root)
    emit("progress", message="正在统一整场人物编号")
    diagnostics = {}
    labels, uncertainty = cluster(windows, vectors, args.speakers, diagnostics=diagnostics)
    turns = make_turns(regions, windows, labels, uncertainty)
    group_stats, warnings = assess_groups(turns, args.speakers)
    warnings = diagnostics.get("warnings", []) + warnings
    for warning in warnings:
        emit("progress", message="请核对人物：" + warning)
    representatives = {}
    for turn in turns:
        speaker = turn["speaker"]
        if (speaker not in representatives
                or ((not turn["uncertain"], turn["end"] - turn["start"])
                    > (not representatives[speaker]["uncertain"], representatives[speaker]["end"] - representatives[speaker]["start"]))):
            representatives[speaker] = turn
    speech_seconds = sum(turn["end"] - turn["start"] for turn in turns)
    uncertain_seconds = sum(turn["end"] - turn["start"] for turn in turns if turn["uncertain"])
    result = {"signature": input_signature, "method": "Silero VAD + CAM++ normalized KMeans global speaker clustering",
              "note": "人物和边界为自动估计，重叠讲话未分离；S1 等仅为声音编号，不表示真实姓名。",
              "duration": info.duration, "speaker_count": len(representatives),
              "speech_seconds": round(speech_seconds, 3),
              "uncertain_ratio": round(uncertain_seconds / max(speech_seconds, 0.001), 4),
              "representatives": representatives, "turns": turns,
              "clustering": diagnostics, "speaker_statistics": group_stats, "warnings": warnings}
    atomic_json(output, result)
    emit("progress", message=f"人物分析完成：{len(representatives)} 位说话人、{len(turns)} 段发言，待核归属时长占 {result['uncertain_ratio']:.0%}")
    emit("result", path=str(output), speaker_count=len(representatives))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wav", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--speakers", type=int, default=0)
    if not __package__:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from meeting_tool.runtime_paths import is_frozen, resource_path, speaker_root
    parser.add_argument("--speaker-root", type=Path,
                        default=speaker_root() if is_frozen() else Path(r"D:\Project\3D-Speaker"))
    args = parser.parse_args(argv)
    try:
        if os.name == "nt":
            directories = ([resource_path("torch", "lib"), resource_path("torchaudio", "lib"),
                            resource_path("onnxruntime", "capi")] if is_frozen() else
                           [Path(sys.prefix) / "Lib/site-packages/kaldi_native_fbank/lib"])
            for dll in directories:
                if dll.is_dir():
                    _dll_handles.append(os.add_dll_directory(str(dll)))
        run(args)
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        emit("error", message=str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
