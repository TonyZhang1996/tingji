"""Modern local desktop entry point, using the existing transcription engine."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading
import time
import webbrowser

from meeting_tool.desktop_controller import MeetingController
from meeting_tool.desktop_server import DesktopServer
from meeting_tool.runtime_paths import resource_path, user_data_root
from meeting_tool.version import __version__

APP_DIR = Path(__file__).resolve().parent
APP_ICON = resource_path("assets", "tingji.ico")
WINDOWS_APP_ID = "Local.Tingji.MeetingTranscription"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--speaker-worker":
        from meeting_tool.diarization_worker import main as speaker_main
        return speaker_main(argv[1:])
    parser = argparse.ArgumentParser(description="DeepSeek 会议转写工作台")
    parser.add_argument("--browser", action="store_true", help="仅用于本机浏览器预览")
    parser.add_argument("--no-open", action="store_true", help="不自动打开预览浏览器")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--self-check", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.self_check is not None:
        from meeting_tool.runtime_check import check_runtime
        return check_runtime(args.self_check)
    data_dir = user_data_root()
    data_dir.mkdir(parents=True, exist_ok=True)
    controller = MeetingController(data_dir)
    if args.audio:
        controller.select_audio(args.audio)
    server = DesktopServer(controller, resource_path("web", "dist"), port=args.port).start()
    if args.browser:
        print(server.url, flush=True)
        if not args.no_open:
            webbrowser.open(server.url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            controller.close()
            server.shutdown()
        return 0
    try:
        import webview
    except ImportError:
        server.shutdown()
        print("桌面界面环境尚未安装。请使用工具目录 .runtime\\Scripts\\pythonw.exe 启动 desktop.py。", file=sys.stderr)
        return 1
    if sys.platform == "win32":
        # Give this app its own taskbar group instead of sharing pythonw's
        # cached identity and shortcut icons with unrelated Python programs.
        import ctypes
        set_app_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        set_app_id.argtypes = [ctypes.c_wchar_p]
        set_app_id.restype = ctypes.c_long
        result = set_app_id(WINDOWS_APP_ID)
        if result < 0:
            raise OSError(f"无法设置听记任务栏标识：0x{result & 0xffffffff:08X}")
    window = webview.create_window(f"听记 · 会议转写工作台 {__version__}", server.url, width=1280, height=720,
                                   min_size=(980, 680), maximized=True,
                                   background_color="#f7f8fa", text_select=True)
    def on_shown():
        # Apply after the actual native first-show event, so launch show-state
        # hints cannot leave a tall restored window outside the working area.
        window.maximize()
    window.events.shown += on_shown
    def picker(kind):
        if kind == "audio":
            values = window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False,
                       file_types=("音频与视频 (*.m4a;*.mp3;*.wav;*.aac;*.ogg;*.opus;*.flac;*.wma;*.mp4;*.mkv;*.mov;*.amr)", "所有文件 (*.*)"))
        else:
            values = window.create_file_dialog(webview.FileDialog.FOLDER,
                       directory=controller.settings["output_root"], allow_multiple=False)
        return values[0] if values else None
    controller.dialog_picker = picker
    closing = threading.Event()
    def on_closing():
        if controller.state()["task"]["running"]:
            if not closing.is_set():
                closing.set()
                def finish():
                    controller.close()
                    window.destroy()
                threading.Thread(target=finish, daemon=True).start()
            return False
        return True
    window.events.closing += on_closing
    try:
        webview.start(gui="edgechromium", private_mode=True, debug=False,
                      icon=str(APP_ICON))
    finally:
        controller.close()
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
