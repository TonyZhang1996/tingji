# 听记 Android

[简体中文](#简体中文) · [English](#english)

## 简体中文

听记 Android 的当前预发布版本为 **2.3.0-android.3**。手机导入已有会议录音，在本机分析人物，再使用自己的 DeepSeek API Key 转写和总结。手机可独立处理任务，不依赖电脑保持开机，也没有听记中转服务器。

当前主要适配目标为 **vivo X300 等 ARM64 安卓手机**。工程最低安卓版本为 Android 8.0；这不代表所有机型均已验收。尚未完成 vivo X300 真机测试，处理速度、发热、耗电和真实会议人物区分效果需在目标手机上验证。预发布版不应作为重要会议内容的唯一备份。

### 安装和开始使用

1. 用手机[下载 Android APK](https://github.com/TonyZhang1996/tingji/releases/download/v2.3.0-android.3/Tingji-2.3.0-android.3-arm64-v8a.apk)（约 60 MB），或查看 [Android 发布说明](https://github.com/TonyZhang1996/tingji/releases/tag/v2.3.0-android.3)。Windows 安装包不能安装到安卓。
2. 使用安卓系统安装程序安装 APK，打开听记。已有版本请直接覆盖安装，保留应用内任务；不要先卸载。人物模型随安装包提供，不需要安装 Python、FFmpeg 或配置模型环境。
3. 点击“选择本地录音”，通过系统文件选择器导入 MP3、M4A、WAV 等系统可解码的音频。也可从支持系统分享的应用将音频分享给听记。
4. 知道实际发言人数时填写人数；不确定时保留自动判断。先“分析人物”，在“人物”页试听样例并填写姓名。
5. 首次打开会引导填写自己的 DeepSeek API Key，也可选择“稍后填写”。Key 仅留在当前应用会话内存中；以后重新打开应用时，可在设置中重新填写，再点击“开始转写”。成功片段会逐段保存，转写和总结费用由自己的 DeepSeek 账户承担。
6. “文字”和“总结”页均可直接上滑，让任务信息和操作区滚出屏幕；顶部有醒目的“全屏阅读”入口，点击后使用更大的正文区域，退出按钮或系统返回键可恢复布局和位置。点击“显示工具栏”可回到顶部操作区。
7. 通过“导出文字”或总结页的“导出分享”保存结果，也可以交给系统分享面板中的其它应用。

播放器右侧的箭头用于收起或展开播放栏；倍速通过独立的“1×”按钮选择。点片段时间会等待跳转完成再播放，并在该片段末尾暂停；点播放器的完整播放或手动拖动进度，可继续播放整条录音。

界面为中文。底部的“任务、文字、人物、总结”可切换主要内容；手机系统返回键会先关闭当前弹窗或面板。

### 长录音和恢复

- 音频按块解码和分析，人物特征、转写和总结进度保存在应用内。
- 人物编号在整条录音中统一；短插话、相似声线、远距离拾音和重叠讲话仍需回听校对。
- 处理任务时通过安卓前台服务显示进度通知。系统仍可能限制后台执行；重新打开后可以继续已保存的任务，不能保证永不被系统暂停。
- 文字按片段串行提交。已成功片段不会因继续转写而自动重复提交。
- 上次请求结果不明时，默认不重发。只有明确选择重试后才重新请求，可能再次计费。
- 长文字分批总结并合并，已有成功小结可复用；总结失败时保留旧版完整总结。

### 隐私和数据保存

**本地人物分析不调用云端 API。** 开始转写会将音频片段发送至 DeepSeek；生成总结会发送转写文字、人物标签和时间位置。需要联网的部分是 DeepSeek 转写与总结。

API Key 仅在当前应用会话和任务内存中使用，不保存到配置、任务记录、日志、安装包或 GitHub。应用没有账户登录、云同步或自动上传会议结果的功能。

首次 Key 引导只保存一个“已填写或跳过引导”的布尔标记，避免每次打开都弹窗；它不包含 Key。

录音导入副本、音频缓存、文字、校对和总结存放在应用私有目录。**安卓卸载会删除应用内数据，这与 Windows 版不同。** 卸载或更换手机前，请先导出需要保留的结果。通过系统分享交给其它应用后，文件将按接收应用的方式保存。

删除任务会删除该任务在听记中的录音副本、缓存和文字结果。听记不会主动修改或删除系统文件选择器提供的外部原文件；请确认重要原录音仍有独立备份。

安装包、源码和仓库演示内容都不应包含个人 API Key、真实会议录音、转写或总结。模型与运行库是公开第三方组件，许可和来源以构建材料及随包声明为准。

### 当前能力边界

- 仅处理导入的已有录音，未加入实时录音或通话录音。
- 人物区分是自动声音归类，不会自动知道人物的真实姓名；需要试听确认。
- 片段时间不是逐字时间轴，不能把导出的片段字幕当成逐字对齐字幕。
- 只能校对现有片段的文字和人物；句中拆分、批量改人物及其它扩展功能尚未包含。
- 支持总体总结、逐人总结和待办；这些内容来自转写初稿，人物、金额、日期及明确承诺应回听核对。
- 不完整的转写需明确选择仅总结已完成部分，不能作为整场会议完整总结。

### 开发和发布

Android 原生工程位于 `mobile/android`，共用界面源码位于 `web/src`。Android 通过本地原生消息桥访问文件、任务、模型和 DeepSeek；Windows 继续使用原有桌面服务。

构建需要 Python 3.11、Node.js/npm、JDK 17、Android SDK 36、Build Tools 35.0.0 和工程内固定版本的 Gradle Wrapper。下面的 JDK、SDK、工作副本目录仅为示例，需换成开发者自己的目录；工作副本建议使用不含中文和空格的路径。

```powershell
py -3.11 mobile/scripts/prepare_android.py --work-dir work/android-build
py -3.11 mobile/scripts/build_android.py --java-home 'D:\Tools\jdk-17' --sdk 'D:\Tools\android-sdk' --stage-dir 'D:\tingji-android-stage'
```

[准备脚本](scripts/prepare_android.py) 根据 [依赖清单](vendor-manifest.json) 准备并核对公开模型和 sherpa-onnx AAR。已有完整缓存时可添加 `--offline` 仅做本地准备。[构建脚本](scripts/build_android.py) 默认构建 debug 版本；正式发布使用 `--variant release`，再由发布者在仓库外完成签名。`--skip-web` 仅用于已经准备好本次前端产物的构建。具体参数可运行相应脚本的 `--help`。

使用 [安装包审核脚本](scripts/audit_apk.py) 检查最终 APK，例如：

```powershell
py -3.11 mobile/scripts/audit_apk.py 'D:\tingji-release\Tingji.apk' --sdk 'D:\Tools\android-sdk' --require-release --report work/android-build/apk-audit.json
```

`--require-release` 用于核对准备公开发布的包。公开模型、AAR、SDK 配置 `local.properties`、构建目录、APK/AAB 和签名私钥都不存入 Git。前端第三方许可见 [完整声明](android/app/src/main/assets/licenses/frontend-licenses.txt) 与 [组件清单](android/app/src/main/assets/licenses/frontend-components.json)。

测试与打包应在隔离的数据目录中使用合成或取得授权的测试音频，发布前核对依赖来源、模型哈希、安装包内容及凭据排除。具体测试结果以相应 Release 的验收记录为准；没有实测的数据不作为性能承诺。

## English

The current Android prerelease is **2.3.0-android.3**. Import a meeting recording, analyze speakers locally, and use **your own DeepSeek API key** for transcription and summaries. The phone processes tasks independently; a running desktop computer or a Tingji relay server is not required.

The primary target is **ARM64 Android phones such as the vivo X300**. The project has a minimum API level corresponding to Android 8.0, which does not establish compatibility with every device. It has not yet been validated on a physical vivo X300. Processing time, thermal behavior, battery use and real-meeting diarization accuracy need device testing. The interface is in Chinese.

### Install and use

Download the APK from the Android prerelease on [GitHub Releases](https://github.com/TonyZhang1996/tingji/releases), install it with the Android system installer and open Tingji. Models are bundled; no Python, FFmpeg or manual model environment is required.

Install an update over the existing app to retain tasks; do not uninstall first. Choose an existing audio file using the system picker, or share supported audio from another app. MP3, M4A and WAV are initial supported examples; decoding depends on Android's available media codecs. Set the speaker count if known, analyze speakers, listen to the samples and enter names. A first-use dialog accepts your API key or lets you skip; the key remains in session memory, so enter it again in Settings in a later session when needed. Transcribe, review the text, then generate the overall summary, per-speaker summaries and action items. Use the export/share controls to save results with other apps.

Bottom navigation switches between tasks, transcript, speakers and summary. Android Back closes the current dialog or panel first.

On the transcript page, “全屏阅读” expands the existing scrollable segment list and hides the surrounding controls. Use the exit button or Android Back to return. The onboarding preference stores only a boolean that the prompt was completed or skipped, never the API key.

Both transcript and summary pages share one continuous content scroll area so the task information and tools move out of view when scrolling down. The visible full-screen action expands either view; “显示工具栏” returns to the tools. The player's separate arrow collapses or expands it, while the rate control remains independent. Segment previews wait for loading/seeking and pause at the selected segment's end; manual full playback or seeking clears that segment limit.

### Long recordings

Audio is decoded and analyzed in blocks. Local checkpoints preserve speaker features, transcription and summary progress. Foreground processing displays a system notification; Android can still restrict execution, so recovery is supported rather than promising unlimited background operation.

ASR requests are submitted one segment at a time. Successful segments are reused on continuation. Ambiguous interrupted requests are not automatically resubmitted; explicitly retrying them may incur another charge. Long summaries are processed in parts, and completed parts can be reused. A failed regeneration retains the prior successful report.

### Privacy and retention

Speaker analysis runs locally. Transcription sends audio segments to DeepSeek; summarization sends transcript text, speaker labels and time references. These operations need network access and use the user's DeepSeek balance.

API keys stay in application/session task memory and are not written to settings, logs, results, the APK or GitHub. Tingji does not add accounts, cloud synchronization or automatic meeting-result uploads.

Imported copies, audio caches, transcripts, corrections and summaries live in private app storage. **Uninstalling Android removes this data, unlike the Windows version.** Export important results before uninstalling or changing phones. Files shared to another app follow that app's retention behavior.

Deleting a task removes its app-local recording copy, caches and results. The external original selected from another provider is not intentionally modified or deleted. Keep a separate copy of important source recordings.

### Scope and development

This version imports existing recordings; it does not record live meetings or calls. Speaker labels need human confirmation, overlapping speech is not separated into clean tracks, and segment timestamps are not word-level alignments. Review names, financial figures, dates and commitments against the audio. Partial summaries explicitly exclude unfinished transcript segments. Sentence splitting and bulk speaker correction are future work.

The Android project is in `mobile/android`; the shared React UI is in `web/src`. Native messages connect the UI to Android file access, task execution, models and DeepSeek calls. The desktop HTTP implementation remains separate.

Build prerequisites are Python 3.11, Node.js/npm, JDK 17, Android SDK 36, Build Tools 35.0.0 and the pinned Gradle Wrapper. Run [prepare_android.py](scripts/prepare_android.py) with `--work-dir work/android-build` to prepare the public models and sherpa-onnx AAR against [vendor-manifest.json](vendor-manifest.json). Use `--offline` only when the required cache is already complete.

Run [build_android.py](scripts/build_android.py) with `--java-home`, `--sdk` and an ASCII-only `--stage-dir` appropriate for your machine. The default variant is debug; `--variant release` builds a release variant for signing outside the repository. Use `--skip-web` only when the current frontend has already been prepared. Each script provides `--help`. The PowerShell examples in the Chinese section use example toolchain paths.

Validate the final APK with [audit_apk.py](scripts/audit_apk.py): supply the APK path, `--sdk`, an output `--report` path and `--require-release` for a public release. Never commit models, AARs, local SDK configuration, build output, signing keys or private meeting data. Frontend notices are in [frontend-licenses.txt](android/app/src/main/assets/licenses/frontend-licenses.txt), with a lockfile-bound [component inventory](android/app/src/main/assets/licenses/frontend-components.json).

Release validation must use isolated profiles and synthetic or authorized recordings. Refer to the specific Release notes for verified results; unmeasured device performance is not a guarantee.
