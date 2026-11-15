"""Atomic plain-text/Markdown/JSON exports with segment-level timing only."""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path


def atomic_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding=encoding, newline="") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, value: dict) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def timestamp(seconds: float, *, srt: bool = False) -> str:
    millis = max(0, round(float(seconds) * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{',' if srt else '.'}{millis:03}"


def export_transcript(job_dir: Path, metadata: dict, segments: list[dict], speaker_names: dict | None = None,
                      *, complete: bool = False) -> dict:
    job_dir = Path(job_dir)
    names = {str(k): str(v).strip() for k, v in (speaker_names or {}).items() if str(v).strip()}
    exported = []
    for segment in segments:
        row = dict(segment)
        row["speaker_name"] = names.get(row.get("speaker", ""), row.get("speaker", "未知人物").replace("S", "说话人 ", 1))
        exported.append(row)
    title = Path(metadata.get("audio_path", "会议录音")).stem
    done_count = sum(s.get("state") == "done" for s in segments)
    status = "转写完成" if complete else f"尚未完成（已完成 {done_count}/{len(segments)} 段）"
    warning = ("这是自动转写初稿，未经逐句听音校对。人物编号由整场声纹聚类得到，不能证明真实身份；"
               "短句、噪声及重叠讲话可能归属有误。时间戳表示音频片段起止，并非逐字对齐。")
    header = [title + " · 分人物转写", status, warning, ""]
    plain = list(header)
    markdown = [f"# {title} · 分人物转写", "", f"**{status}**", "", warning, ""]
    if metadata.get("notice"):
        plain.extend([str(metadata["notice"]), ""])
        markdown.extend([str(metadata["notice"]), ""])
    srt_rows = []
    csv_stream = io.StringIO(newline="")
    writer = csv.writer(csv_stream)
    writer.writerow(["片段", "开始时间", "结束时间", "人物编号", "人物名称", "归属待核", "状态", "文字"])
    for index, row in enumerate(exported, 1):
        start, end = timestamp(row["start"]), timestamp(row["end"])
        who = row["speaker_name"] + ("（归属待核）" if row.get("uncertain") else "")
        state = row.get("state", "pending")
        content = str(row.get("text", "")).strip() if state == "done" else {
            "ambiguous": "[本段结果不明：可能已计费，尚未重新提交]",
            "failed": "[本段转写失败，待重试]", "requesting": "[本段请求未确认结果]",
        }.get(state, "[本段尚未转写]")
        if state == "done" and not content:
            content = "[接口未识别出文字]"
        plain.extend([f"[{start} → {end}] {who}", content, ""])
        markdown.extend([f"### [{start} → {end}] {who}", "", content, ""])
        writer.writerow([index, start, end, row.get("speaker"), row["speaker_name"], bool(row.get("uncertain")), state, content])
        if state == "done" and row.get("text", "").strip():
            srt_rows.append(f"{len(srt_rows) + 1}\n{timestamp(row['start'], srt=True)} --> {timestamp(row['end'], srt=True)}\n{who}：{content}\n")
    payload = {"schema_version": 1, "complete": complete, "timing": "audio_segment_not_word_aligned",
               "notice": warning, "metadata": metadata, "speaker_names": names, "segments": exported}
    paths = {"txt_path": job_dir / "完整转写.txt", "md_path": job_dir / "完整转写.md",
             "json_path": job_dir / "完整转写.json", "csv_path": job_dir / "分段明细.csv",
             "srt_path": job_dir / "片段时间轴_非逐字字幕.srt"}
    atomic_text(paths["txt_path"], "\n".join(plain), "utf-8-sig")
    atomic_text(paths["md_path"], "\n".join(markdown))
    atomic_json(paths["json_path"], payload)
    atomic_text(paths["csv_path"], csv_stream.getvalue(), "utf-8-sig")
    atomic_text(paths["srt_path"], "\n".join(srt_rows), "utf-8-sig")
    return {**{key: str(value) for key, value in paths.items()}, "segments": exported, "complete": complete,
            "job_dir": str(job_dir)}


def refresh_exports(job_dir: Path, speaker_names: dict | None = None) -> dict:
    """Rename people and regenerate exports without invoking any model/API."""
    job_dir = Path(job_dir)
    existing = job_dir / "完整转写.json"
    if not existing.is_file():
        raise ValueError("该目录没有可重新导出的转写结果。")
    document = json.loads(existing.read_text(encoding="utf-8"))
    names = document.get("speaker_names", {}) if speaker_names is None else speaker_names
    return export_transcript(job_dir, document.get("metadata", {}), document.get("segments", []), names,
                             complete=bool(document.get("complete")))
