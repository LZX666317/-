# 已生成音频的实时语速控制

双击 `START.bat` 打开界面。已打开的旧窗口需要重新启动才能加载代码更新。

- 点击“选择已有音频”即可播放以前生成的 MP3，不需要重新合成。
- 拖动“播放语速（实时）”滑块，可在 **0.50×–2.00×** 之间调节，步长为 0.05×。播放过程中也能调整。
- “暂停试听 / 继续播放”控制当前选中的文件；“停止”后再次播放会从头开始。
- “恢复 1×”恢复原始播放速度。“播放生成音频”切回最近的生成结果。
- “生成语速”只影响下一次合成；“播放语速”只影响试听。**保存生成音频仍保存原始文件，不会把试听倍速写进 MP3。**

播放器会短暂显示“调节中”，等待系统确认实际速度。无法应用的速度会显示提示，并将界面恢复为播放器的实际值。

## 环境与测试

内置试听使用 Windows Media Player（旧版）组件，通过 pywin32 控制；本机环境已安装所需依赖。启动器会在依赖缺失时尝试安装 `requirements-podcast.txt`。其他机器若缺少 Windows Media Player，需要先启用对应系统组件。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_podcast_*.py" -v
.\.venv\Scripts\python.exe podcast_gui.py --smoke-test
.\.venv\Scripts\python.exe tests\check_native_playback.py "音频\你的音频.mp3"
```

最后一个检查会静音播放一个至少 15 秒的现有文件，验证实际播放速度、暂停续播、停止重播和源文件未被修改。不会连接合成服务。

不同格式能否调速及音调表现由系统解码器决定，不能保证所有外部音频均可用；详见微软的 [Settings.rate 文档](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/wmp/settings-rate)。本项目主要以生成的 MP3 验证。
