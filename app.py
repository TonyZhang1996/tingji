from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import webbrowser

APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "settings.json"
TITLE = "DeepSeek 会议转写 v1.1"


def read_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def check_environment() -> list[tuple[str, bool, str]]:
    from meeting_tool import speaker_backend
    checks = [(name, bool(shutil.which(name)), shutil.which(name) or "未找到")
              for name in ("ffmpeg", "ffprobe")]
    # These dependencies were already installed on this computer; no downloads.
    dependencies = {
        "人物分析 Python": Path(r"D:\miniconda3\envs\firered-speaker\python.exe"),
        "人物声纹模型": Path(r"D:\Project\3D-Speaker\pretrained_models\campplus\campplus_cn_en_common.pt"),
    }
    checks.extend((label, p.exists(), str(p)) for label, p in dependencies.items())
    return checks


def time_label(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


class MeetingApp(tk.Tk):
    def __init__(self, initial_audio: str | None = None):
        super().__init__()
        self.title(TITLE)
        self.geometry("1020x850")
        self.minsize(850, 720)
        self.configure(bg="#F3F7F9")
        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.current_job_dir: Path | None = None
        self.samples: dict[str, str] = {}
        self.sample_names: dict[str, tk.StringVar] = {}
        self.audio_revision = 0
        self.pending_close = False
        settings = read_settings()
        self.audio_var = tk.StringVar(value=initial_audio or "")
        self.output_var = tk.StringVar(value=settings.get("output_root", str(APP_DIR / "转写结果")))
        self.key_var = tk.StringVar()
        self.speakers_var = tk.StringVar(value=str(settings.get("num_speakers", 0)))
        self.price_var = tk.StringVar(value=str(settings.get("hourly_price", 0.39)))
        self.workers_var = tk.StringVar(value=str(settings.get("max_workers", 2)))
        self.retry_uncertain = tk.BooleanVar(value=False)
        self.show_key = tk.BooleanVar(value=False)
        self.media_info = tk.StringVar(value="选择一个音频或视频文件，先分析人物，再开始转写。")
        self.status_var = tk.StringVar(value="等待选择录音")
        self.people_warnings = tk.StringVar()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._drain)
        if initial_audio:
            self._probe()

    def _build(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("TFrame", background="#F3F7F9")
        style.configure("TLabel", background="#F3F7F9", foreground="#182D3B")
        style.configure("Hint.TLabel", foreground="#475569")
        style.configure("Warning.TLabel", foreground="#9A3412")
        style.configure("Heading.TLabel", font=("Microsoft YaHei UI", 21, "bold"))
        style.configure("TLabelframe", background="#F3F7F9", padding=12)
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 11, "bold"), background="#F3F7F9")
        style.configure("TButton", padding=(12, 8))
        style.configure("Primary.TButton", background="#0F766E", foreground="white")
        style.map("Primary.TButton", background=[("active", "#115E59"), ("disabled", "#CBD5E1")],
                  foreground=[("disabled", "#475569")])
        style.configure("TCheckbutton", background="#F3F7F9")
        style.configure("TNotebook", background="#F3F7F9")
        style.configure("TNotebook.Tab", padding=(18, 8))
        shell = ttk.Frame(self, padding=22)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="把长会议，整理成每个人的发言", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(shell, text="本地分析人物  →  DeepSeek 分段转写  →  按时间合并文字", style="Hint.TLabel").pack(anchor="w", pady=(6, 16))
        self.notebook = ttk.Notebook(shell)
        self.notebook.pack(fill="both", expand=True)
        setup = ttk.Frame(self.notebook, padding=16)
        result = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(setup, text="录音与进度")
        self.notebook.add(result, text="人物与结果")
        setup.columnconfigure(0, weight=1)
        setup.rowconfigure(4, weight=1)

        files = ttk.LabelFrame(setup, text="1  选择录音")
        files.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        files.columnconfigure(1, weight=1)
        ttk.Label(files, text="录音文件").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.audio_entry = ttk.Entry(files, textvariable=self.audio_var)
        self.audio_entry.grid(row=0, column=1, sticky="ew")
        self.audio_entry.bind("<FocusOut>", lambda _event: self._probe())
        self.audio_entry.bind("<Return>", lambda _event: self._probe())
        self.choose_audio = ttk.Button(files, text="选择文件…", command=self._pick_audio)
        self.choose_audio.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(files, text="保存到").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(files, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(files, text="选择目录…", command=self._pick_output).grid(row=1, column=2, padx=(8, 0), pady=(8, 0))
        ttk.Label(files, textvariable=self.media_info, style="Hint.TLabel", wraplength=820).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))

        options = ttk.LabelFrame(setup, text="2  人物与接口")
        options.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="参会人数").grid(row=0, column=0, sticky="w", padx=(0, 12))
        count_frame = ttk.Frame(options)
        count_frame.grid(row=0, column=1, columnspan=2, sticky="ew")
        ttk.Spinbox(count_frame, from_=0, to=20, width=5, textvariable=self.speakers_var).pack(side="left")
        ttk.Label(count_frame, text="  0 = 自动判断；知道实际发言人数时，填人数更可靠。", style="Hint.TLabel").pack(side="left", padx=6)
        ttk.Label(options, text="API Key").grid(row=1, column=0, sticky="w", pady=(9, 0))
        self.key_entry = ttk.Entry(options, textvariable=self.key_var, show="●")
        self.key_entry.grid(row=1, column=1, sticky="ew", pady=(9, 0))
        ttk.Checkbutton(options, text="显示", variable=self.show_key,
                        command=lambda: self.key_entry.configure(show="" if self.show_key.get() else "●")).grid(row=1, column=2, padx=(8, 0), pady=(9, 0))
        ttk.Label(options, text="仅保留在本次窗口内，不保存到设置文件。音频片段会发送给 DeepSeek 官方接口。", style="Hint.TLabel", wraplength=790).grid(row=2, column=1, columnspan=2, sticky="w", pady=(7, 0))
        advanced = ttk.Frame(options)
        advanced.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Label(advanced, text="估算单价（元/小时）").pack(side="left")
        ttk.Entry(advanced, textvariable=self.price_var, width=7).pack(side="left", padx=(6, 18))
        ttk.Label(advanced, text="同时转写").pack(side="left")
        ttk.Spinbox(advanced, textvariable=self.workers_var, from_=1, to=4, width=4).pack(side="left", padx=6)
        ttk.Label(advanced, text="段", style="Hint.TLabel").pack(side="left")
        ttk.Checkbutton(options, text="重试结果不明的片段（上次超时可能已扣费，重试可能再次计费）", variable=self.retry_uncertain).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

        actions = ttk.Frame(setup)
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.analyze_btn = ttk.Button(actions, text="先分析人物 · 不调用 API", command=lambda: self._start(True))
        self.analyze_btn.pack(side="left")
        self.start_btn = ttk.Button(actions, text="开始 / 继续转写", style="Primary.TButton", command=lambda: self._start(False))
        self.start_btn.pack(side="left", padx=8)
        self.stop_btn = ttk.Button(actions, text="停止并保留进度", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left")
        ttk.Button(actions, text="环境检查", command=self._check).pack(side="right")
        progress = ttk.Frame(setup)
        progress.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(progress, textvariable=self.status_var, wraplength=820).pack(anchor="w")
        self.bar = ttk.Progressbar(progress, mode="determinate")
        self.bar.pack(fill="x", pady=(7, 0))
        self.log_box = scrolledtext.ScrolledText(setup, height=6, wrap="word", font=("Microsoft YaHei UI", 9), bg="#FFFFFF", fg="#334155", relief="flat", padx=10, pady=8)
        self.log_box.grid(row=4, column=0, sticky="nsew")
        self.log_box.configure(state="disabled")

        ttk.Label(result, text="先听人物样例，再填写姓名", font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        ttk.Label(result, text="人物编号在整场录音中统一。很短的插话、相似声音或同时讲话仍可能分错，带“待核”的发言请回听。", wraplength=820, style="Hint.TLabel").pack(anchor="w", pady=(6, 12))
        ttk.Label(result, textvariable=self.people_warnings, wraplength=820,
                  style="Warning.TLabel").pack(anchor="w", pady=(0, 8))
        people_shell = ttk.LabelFrame(result, text="人物编号 → 姓名（可留空）")
        people_shell.pack(fill="x")
        people_canvas = tk.Canvas(people_shell, height=175, highlightthickness=0, background="#F3F7F9")
        people_scroll = ttk.Scrollbar(people_shell, orient="vertical", command=people_canvas.yview)
        people_scroll.pack(side="right", fill="y")
        people_canvas.pack(side="left", fill="both", expand=True)
        people_canvas.configure(yscrollcommand=people_scroll.set)
        self.people_frame = ttk.Frame(people_canvas)
        people_window = people_canvas.create_window((0, 0), window=self.people_frame, anchor="nw")
        self.people_frame.bind("<Configure>", lambda _event: people_canvas.configure(scrollregion=people_canvas.bbox("all")))
        people_canvas.bind("<Configure>", lambda event: people_canvas.itemconfigure(people_window, width=event.width))
        self.people_note = ttk.Label(self.people_frame, text="完成“分析人物”后，会出现人物编号和可播放的声音样例。", style="Hint.TLabel")
        self.people_note.pack(anchor="w")
        result_actions = ttk.Frame(result)
        result_actions.pack(fill="x", pady=12)
        self.rename_btn = ttk.Button(result_actions, text="保存姓名并重新导出", command=self._rename, state="disabled")
        self.rename_btn.pack(side="left")
        ttk.Button(result_actions, text="打开结果目录", command=self._open_result).pack(side="left", padx=8)
        ttk.Button(result_actions, text="载入已有任务…", command=self._load_existing).pack(side="left")
        ttk.Label(result, text="完整文字预览", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(4, 8))
        self.preview = scrolledtext.ScrolledText(result, wrap="word", font=("Microsoft YaHei UI", 10), bg="white", fg="#182D3B", relief="flat", padx=12, pady=12)
        self.preview.pack(fill="both", expand=True)
        self.preview.configure(state="disabled")
        footer = ttk.Frame(shell)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, text="v1.1 · 自动识别初稿 · 时间为片段时间 · 重叠讲话未单独分离", style="Hint.TLabel").pack(side="left")
        ttk.Button(footer, text="使用说明", command=lambda: os.startfile(str(APP_DIR / "使用说明.html"))).pack(side="right")

    def _log(self, text: str):
        # The worker never receives GUI widgets; all updates pass through a queue.
        self.events.put(("log", text))

    def _append_log(self, text: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text.rstrip() + "\n")
        # Keep the UI responsive even for a meeting with thousands of turns.
        if int(self.log_box.index("end-1c").split(".")[0]) > 1500:
            self.log_box.delete("1.0", "300.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _pick_audio(self):
        path = filedialog.askopenfilename(title="选择会议录音", filetypes=[("音频和视频", "*.m4a *.mp3 *.wav *.aac *.ogg *.opus *.flac *.wma *.mp4 *.mkv *.mov *.amr"), ("所有文件", "*.*")])
        if path:
            self.audio_var.set(path)
            self._probe()

    def _pick_output(self):
        path = filedialog.askdirectory(title="选择结果保存目录")
        if path:
            self.output_var.set(path)

    def _probe(self):
        path = Path(self.audio_var.get().strip().strip('"'))
        if not path.is_file():
            return
        self.audio_revision += 1
        revision = self.audio_revision
        try:
            price = float(self.price_var.get())
        except ValueError:
            price = 0.39
        def task():
            try:
                from meeting_tool.audio import probe_audio
                info = probe_audio(path)
                seconds = info["duration_seconds"]
                note = f"录音时长 {time_label(seconds)}  ·  按整段时长估算约 ¥{seconds / 3600 * price:.3f}（未计入重试，实际按上传音频秒数收费）"
                self.events.put(("media", (revision, note)))
            except Exception as exc:
                self.events.put(("media", (revision, f"无法读取录音：{exc}")))
        threading.Thread(target=task, daemon=True).start()

    def _config(self, analyze: bool):
        from meeting_tool.engine import RunConfig
        source = Path(self.audio_var.get().strip().strip('"'))
        if not source.is_file():
            raise ValueError("请选择一个存在的会议录音文件。")
        if not analyze and not self.key_var.get().strip():
            raise ValueError("请粘贴 DeepSeek API Key；也可以先点“分析人物”，这一步不调用 API。")
        count = int(self.speakers_var.get())
        workers = int(self.workers_var.get())
        price = float(self.price_var.get())
        if not 0 <= count <= 20:
            raise ValueError("参会人数请填写 0 到 20；0 表示自动判断。")
        if not 1 <= workers <= 4:
            raise ValueError("同时转写的片段数请填写 1 到 4。")
        if not 0 < price < 100:
            raise ValueError("请填写有效的每小时单价。")
        output = self.output_var.get().strip().strip('"')
        if not output:
            raise ValueError("请选择结果保存目录。")
        names = {speaker: var.get().strip() for speaker, var in self.sample_names.items() if var.get().strip()}
        # Names only apply to the task currently loaded, not a newly selected file.
        config = RunConfig(audio_path=source.resolve(), output_root=Path(output).resolve(),
                           api_key=self.key_var.get().strip(), num_speakers=count,
                           max_workers=workers, hourly_price=price, speaker_names={},
                           retry_uncertain=self.retry_uncertain.get())
        from meeting_tool.engine import job_directory
        if self.current_job_dir and job_directory(config).resolve() == self.current_job_dir.resolve():
            config.speaker_names = names
        return config

    def _start(self, analyze: bool):
        if self.worker and self.worker.is_alive():
            return
        try:
            config = self._config(analyze)
            SETTINGS_FILE.write_text(json.dumps({"output_root": str(config.output_root), "num_speakers": config.num_speakers,
                                                  "hourly_price": config.hourly_price, "max_workers": config.max_workers}, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("请检查输入", str(exc), parent=self)
            return
        self.cancel_event = threading.Event()
        self._busy(True)
        self.status_var.set("正在准备人物分析…" if analyze else "正在准备转写…")
        self.bar.configure(mode="indeterminate")
        self.bar.start(15)
        self.notebook.select(0)
        def task():
            try:
                from meeting_tool.engine import MeetingJob, job_directory
                self.events.put(("job_dir", str(job_directory(config))))
                job = MeetingJob(config, log=self._log, progress=lambda info: self.events.put(("progress", info)), cancel_event=self.cancel_event)
                result = job.analyze_only() if analyze else job.run()
                self.events.put(("done", (analyze, result)))
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("idle", None))
        self.worker = threading.Thread(target=task, daemon=True)
        self.worker.start()

    def _busy(self, running: bool):
        for widget in (self.analyze_btn, self.start_btn, self.choose_audio, self.rename_btn):
            widget.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.audio_entry.configure(state="disabled" if running else "normal")
        if not running and not self.current_job_dir:
            self.rename_btn.configure(state="disabled")

    def _stop(self):
        self.cancel_event.set()
        self.status_var.set("正在停止；已成功的转写会保留。网络请求结束可能需要一点时间…")
        self.stop_btn.configure(state="disabled")

    def _check(self):
        try:
            checks = check_environment()
            text = "\n\n".join(f"{'可用' if ok else '缺少'} · {label}\n{path}" for label, ok, path in checks)
            messagebox.showinfo("本机环境检查", text, parent=self)
        except Exception as exc:
            messagebox.showerror("环境检查", str(exc), parent=self)

    def _drain(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self._append_log(value)
                elif kind == "media":
                    if value[0] == self.audio_revision:
                        self.media_info.set(value[1])
                elif kind == "job_dir":
                    self.current_job_dir = Path(value)
                elif kind == "progress":
                    message = value.get("message") or value.get("stage", "处理中")
                    self.status_var.set(message)
                    total, current = value.get("total", 0), value.get("current", 0)
                    if total:
                        self.bar.stop()
                        self.bar.configure(mode="determinate", maximum=total, value=current)
                    else:
                        if self.bar.cget("mode") != "indeterminate":
                            self.bar.configure(mode="indeterminate")
                            self.bar.start(15)
                elif kind == "done":
                    analyze, result = value
                    self.current_job_dir = Path(result["job_dir"])
                    self._load_job(self.current_job_dir, result)
                    if analyze:
                        self.status_var.set("人物分析完成；到“人物与结果”听样例、填姓名，再开始转写。")
                    elif result.get("complete"):
                        self.status_var.set("完整转写已导出；人物归属和识别文字仍建议回听校对。")
                    else:
                        self.status_var.set("已保存部分结果；有待处理或结果不明的片段，请查看日志后继续。")
                    self.notebook.select(1)
                elif kind == "error":
                    self._append_log(value)
                    self.status_var.set("任务已停止，进度已保留。" if self.cancel_event.is_set() else "任务未完成，请查看说明。")
                    if not self.cancel_event.is_set():
                        messagebox.showerror("任务未完成", value, parent=self)
                    if self.current_job_dir:
                        self._load_job(self.current_job_dir)
                elif kind == "idle":
                    self.bar.stop()
                    self._busy(False)
                    if self.pending_close:
                        self.destroy()
                        return
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _load_job(self, job_dir: Path, result: dict | None = None):
        self.current_job_dir = job_dir
        analysis = {}
        for candidate in (job_dir / "analysis.json", job_dir / "cache" / "analysis.json"):
            if candidate.is_file():
                try:
                    analysis = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
                break
        if result:
            analysis = {**analysis, **result}
        from meeting_tool.engine import WORKFLOW_VERSION, DIARIZATION_VERSION
        warnings = [str(warning) for warning in analysis.get("warnings", [])]
        fingerprint = analysis.get("fingerprint", {})
        if analysis and (fingerprint.get("workflow_version") != WORKFLOW_VERSION
                         or analysis.get("diarization_version", fingerprint.get("diarization_version")) != DIARIZATION_VERSION):
            warnings.insert(0, "这是旧版人物结果。请回到“录音与进度”，用同一录音先分析人物；新版会另建任务并保留旧文字，开始转写会重新计费。")
        self.people_warnings.set("\n".join(dict.fromkeys(warnings)))
        source_path = fingerprint.get("path") or analysis.get("metadata", {}).get("audio_path")
        if source_path:
            self.audio_var.set(source_path)
            self.output_var.set(str(job_dir.parent))
            self.speakers_var.set(str(fingerprint.get("num_speakers", analysis.get("num_speakers_requested", 0))))
            self._probe()
        samples = analysis.get("samples", [])
        self.samples = {s["speaker"]: str(s["path"]) for s in samples}
        speakers = sorted({turn.get("speaker", "S1") for turn in analysis.get("turns", analysis.get("segments", []))}, key=lambda x: (int(x[1:]) if x[1:].isdigit() else 999, x))
        speakers = speakers or sorted(self.samples)
        previous_names = {key: var.get() for key, var in self.sample_names.items()} if getattr(self, "people_job", None) == job_dir else {}
        names_file = job_dir / "speaker_names.json"
        if names_file.is_file():
            try:
                previous_names.update(json.loads(names_file.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        self.people_job = job_dir
        for child in self.people_frame.winfo_children():
            child.destroy()
        self.sample_names = {}
        if not speakers:
            ttk.Label(self.people_frame, text="任务目录已载入；人物分析尚未完成。", style="Hint.TLabel").pack(anchor="w")
        for speaker in speakers:
            row = ttk.Frame(self.people_frame)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=f"说话人 {speaker.removeprefix('S')}", width=12).pack(side="left")
            var = tk.StringVar(value=previous_names.get(speaker, ""))
            self.sample_names[speaker] = var
            ttk.Entry(row, textvariable=var, width=24).pack(side="left", padx=(4, 12))
            sample = self.samples.get(speaker)
            ttk.Button(row, text="播放声音样例", command=lambda p=sample: self._play(p), state="normal" if sample else "disabled").pack(side="left")
            sample_info = next((item for item in samples if item.get("speaker") == speaker), {})
            if sample_info.get("too_short"):
                ttk.Label(row, text="样例不足 3 秒，需回听确认", style="Warning.TLabel").pack(side="left", padx=8)
            elif sample_info.get("uncertain"):
                ttk.Label(row, text="样例归属待核，请先回听", style="Warning.TLabel").pack(side="left", padx=8)
        self.rename_btn.configure(state="normal" if speakers and not (self.worker and self.worker.is_alive()) else "disabled")
        txt_path = Path(result["txt_path"]) if result and result.get("txt_path") else next(iter(job_dir.glob("*.txt")), None)
        text = "人物已分析完成。确认姓名后，在“录音与进度”页点击“开始 / 继续转写”。"
        if txt_path and txt_path.is_file():
            try:
                text = txt_path.read_text(encoding="utf-8-sig")
            except OSError:
                pass
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text[:1_000_000])
        self.preview.configure(state="disabled")

    def _play(self, path: str | None):
        if path and Path(path).is_file():
            os.startfile(path)
        else:
            messagebox.showinfo("声音样例", "暂时没有可播放的声音样例。", parent=self)

    def _rename(self):
        if not self.current_job_dir:
            return
        names = {speaker: var.get().strip() for speaker, var in self.sample_names.items() if var.get().strip()}
        try:
            from meeting_tool.engine import refresh_exports
            # Persist names separately; this operation never calls the ASR API.
            (self.current_job_dir / "speaker_names.json").write_text(json.dumps(names, ensure_ascii=False, indent=2), encoding="utf-8")
            if not (self.current_job_dir / "完整转写.json").is_file():
                self.status_var.set("人物姓名已保存，开始转写时会带入文字。")
                return
            refresh_exports(self.current_job_dir, speaker_names=names)
            self._load_job(self.current_job_dir)
            self.status_var.set("人物姓名已保存，并重新导出已有文字；未调用 API。")
        except FileNotFoundError:
            self.status_var.set("人物姓名已保存，完成转写后会带入文字。")
        except Exception as exc:
            messagebox.showerror("重新导出未完成", str(exc), parent=self)

    def _open_result(self):
        path = self.current_job_dir or Path(self.output_var.get())
        if path.is_dir():
            os.startfile(str(path))
        else:
            messagebox.showinfo("结果目录", "完成分析或转写后，结果目录会自动创建。", parent=self)

    def _load_existing(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("任务正在运行", "请先停止当前任务，再载入其他任务。", parent=self)
            return
        path = filedialog.askdirectory(title="选择一个包含 analysis.json 的任务目录", initialdir=self.output_var.get())
        if path:
            self._load_job(Path(path))

    def _close(self):
        if self.worker and self.worker.is_alive():
            self.pending_close = True
            self._stop()
            self.status_var.set("正在停止并保存进度，完成后关闭窗口…")
            return
        self.key_var.set("")
        self.destroy()


def main():
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("audio", nargs="?", help="可选：启动时选中的会议录音")
    parser.add_argument("--check-env", action="store_true")
    args = parser.parse_args()
    if args.check_env:
        checks = check_environment()
        for name, ok, path in checks:
            print(f"{'OK' if ok else 'MISSING'} {name}: {path}")
        return 0 if all(ok for _, ok, _ in checks) else 1
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    app = MeetingApp(args.audio)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
