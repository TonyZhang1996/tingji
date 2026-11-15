# 听记 / Tingji

**简体中文** · [English](README.en.md)

Windows 本地会议转写工具：导入录音，按人物整理文字，回听校对，再生成附带待办事项的会议总结。

界面使用 React、shadcn/ui 和 pywebview。人物分析在本地完成，文字转写与总结使用 DeepSeek API。

## 界面预览

以下为真实界面截图，会议内容、人物和待办均为虚构演示数据，不代表实际转写或人物识别效果。

### 分人物转写与录音回听

按人物查看发言，试听并确认人物姓名，点击片段回听录音。

![分人物转写与录音回听界面](docs/screenshots/transcript.png)

### AI 总结与待办事项

总结末尾集中列出待办事项、负责人、期限及对应的录音时间。

![AI 总结末尾的待办事项、负责人和期限](docs/screenshots/summary.png)

### 片段校对

回听单段录音，修改文字或人物归属，再标记为已校对。

![片段文字与人物归属校对界面](docs/screenshots/review.png)

## 功能

- **长录音转写**：自动分段、逐段保存、恢复进度；成功片段不会因继续转写而重新提交。
- **人物校对**：整场统一人物编号，试听声音、填写姓名、修改文字或人物归属。
- **录音回听与导出**：点击片段跳转播放，支持 TXT、Markdown、JSON、CSV 和片段时间轴。
- **AI 总结**：长文字分批整理后合并，末尾列出待办、负责人、期限和录音依据。部分转写须明确选择总结范围。

## 开始使用

需要 Windows、Python 3.11、Microsoft Edge WebView2，以及可在 `PATH` 中找到的 `ffmpeg` 和 `ffprobe`。也可把 FFmpeg 程序放进项目的 `bin` 目录。

克隆或下载仓库后，在项目目录打开 PowerShell：

```powershell
py -3.11 -m venv .runtime
.\.runtime\Scripts\python.exe -m pip install -r requirements_desktop.txt
```

以上只安装桌面界面依赖。**人物分析环境和模型需要另外准备，仓库不附带模型。**

人物分析使用 Silero VAD、CAM++ 和 3D-Speaker。兼容的独立 Python 环境需安装 `torch`、`torchaudio`、`soundfile`、`silero-vad`、`onnxruntime`、`numpy`、`scikit-learn`；3D-Speaker 目录中需有 `pretrained_models/campplus/campplus_cn_en_common.pt`。

将下面两个示例路径换成自己的位置，并从同一个 PowerShell 窗口启动：

```powershell
$env:MEETING_SPEAKER_PYTHON = "D:\Envs\meeting-speaker\python.exe"
$env:MEETING_SPEAKER_ROOT = "D:\Models\3D-Speaker"
.\.runtime\Scripts\python.exe -X utf8 desktop.py
```

依赖配置完成后，也可使用 `启动工具.vbs`；`启动工具.cmd` 会保留错误提示。仓库包含 `web/dist`，普通使用不需要 Node.js。

## 使用流程

选择录音 → 确认人数 → 分析人物并试听命名 → 填入 DeepSeek API Key 开始转写 → 回听校对 → AI 总结或导出。

API Key 仅在当前窗口内存中使用，不写入设置、日志或任务结果。ASR 按上传音频计费，总结按文字量另行计费，具体以 [DeepSeek 官方说明](https://api-docs.deepseek.com/quick_start/pricing/)为准。结果不明的请求默认不自动重发，避免重复扣费。

## 隐私与识别边界

人物分析在本地运行；开始转写会把音频片段发送至 DeepSeek，生成总结会发送转写文字与片段时间。录音缓存、转写、校对和总结保存在本机，不会由工具自动上传 GitHub。

`.gitignore` 排除个人设置、录音、结果、缓存、密钥文件、运行环境和 `node_modules`。即使使用私有仓库，也不要手动强制添加这些文件。

提交前可运行 `.\.runtime\Scripts\python.exe scripts/check_publish.py`，检查 Git 暂存内容中的文件范围和凭据特征；提交后可加 `--ref HEAD` 检查该提交。检查不会输出匹配到的密钥或读取被忽略的录音。

自动人物归属、数字和姓名仍需回听确认。片段时间不是逐字对齐，重叠讲话没有单独分离；未完成的转写不能作为整条录音的完整总结。原文未明确的负责人或期限会标为“未明确”。

## 开发与验证

重新构建界面需要 Node.js 和 npm：

```powershell
cd web
npm ci
npm run build
```

在项目根目录，使用具备人物分析依赖的 Python 运行完整测试：

```powershell
& $env:MEETING_SPEAKER_PYTHON -X utf8 -m unittest discover -s tests -v
```

本地完整回归已通过 82 项测试。测试使用临时合成音频和模拟接口，不调用真实付费 API；此结果不代表真实录音的识别准确率。

shadcn/ui 组件的来源和第三方许可见 [组件来源](web/COMPONENT-SOURCES.txt)与 [shadcn/ui 许可](web/SHADCN-LICENSE.txt)。
