# Remote Mic · Windows 版本（RC003）

这是 `pbook1207/windows-remote-mic-app` 仓库，面向小米蓝牙遥控器 2 Pro（RC003）的 Windows 蓝牙桥接软件：把遥控器的按键和语音转成 Windows 能识别的键盘按键与语音输入，从而在电脑上操控豆包、微信、WPS 等应用。macOS 应用、Swift 工程和 macOS 发布资源不属于本仓库。

Windows 客户端位于 [`apps/windows/rc003`](apps/windows/rc003/README.md)，提供：

- WinRT BLE 连接与 ATVV 语音解码；
- Windows Raw Input + Frida HID 旁路按键监听，SendInput 按键映射；
- 语音输出到用户明确选择的音频端点（配合虚拟声卡供输入法识别）；
- PySide6/Qt Quick 设置窗口、诊断和 PyInstaller/Inno Setup 构建。

当前正式版 `0.2.1` **已通过真实硬件验收**：已在真实 RC003 遥控器上完成
配对、逐键与语音链路验收（方向/OK/Home/Menu/TV/Power/返回/音量± 全部单次触发，
麦克风键可启动语音软件并识别语音）。本版还完成了 H180 本地麦克风与 UU 远程
虚拟音频设备的自动切换和连续使用测试。产物未签名；CI 和自动构建不能替代真实
硬件验收。

## 本仓库的主要更新

在上游 Windows RC003 实现基础上，本仓库主要新增和完善了：

- 修复部分 Windows 环境下 RC003 返回键、音量键失灵的问题；
- 增加经过固定版本与 SHA-256 校验的 Frida HID 旁路、按需 UAC 提权、失败回退和
  成品完整性检查；
- 完成真实 RC003 的蓝牙配对、逐键、语音链路、登录启动和重启恢复测试；
- 麦克风键支持“直接长按”和“短按一次后再次长按”两种手势，分别使用两个可修改、
  可录制的语音快捷键，并为 Typeless 提供防抖优化；
- 普通按键支持单击、双击和长按映射，并增加方案管理、快捷文本菜单和编辑器光标定位；
- 完善虚拟音频桥接、系统麦克风统一输入和没有软件使用时自动释放麦克风；
- 重整 Windows 设置、检查与修复、登录自启动、桥接进程控制和日志入口；
- 增加安装器、便携版 ZIP、`SHA256SUMS.txt` 以及 GitHub Actions 自动测试和构建。

逐版本的完整改动记录见
[`apps/windows/rc003/CHANGELOG.md`](apps/windows/rc003/CHANGELOG.md)。

## 截图

![连接设置页](docs/screenshots/settings-connection.png)

![按键映射页](docs/screenshots/settings-buttons.png)

> 截图在 Windows 11 + RC003 实测环境拍摄；如与你的系统主题/分辨率不同属正常差异。

## 下载与安装

最新正式版：`0.2.1`（Git 标签 `v0.2.1-windows`）。请从
[Releases](https://github.com/pbook1207/windows-remote-mic-app/releases) 页面下载。

从 Release 页面 Assets 下载，二选一：

| 资产 | 适用场景 |
| --- | --- |
| `RemoteMicRC003Setup-0.2.1-unsigned.exe` | 推荐，安装到开始菜单/桌面并创建快捷方式 |
| `RemoteMicRC003-0.2.1-portable-unsigned.zip` | 免安装，解压到任意目录直接运行 |

两个都未签名，Windows SmartScreen 会提示，点“更多信息 → 仍要运行”即可。
建议同时下载 `SHA256SUMS.txt` 校验文件哈希。

## 快速开始（安装版）

1. 下载 `RemoteMicRC003Setup-...exe` 并运行，一路“下一步”完成安装；
2. 首次运行，或双击开始菜单的“Remote Mic 设置”，打开设置窗口（连接页）；
3. 在 Windows 设置 → 蓝牙中把 RC003 遥控器与电脑配对；
4. 回到设置窗口的“连接”页，选择当前 RC003 设备，再点“保存并启动桥接”；
5. 按遥控器方向键/OK/返回/音量键验证；麦克风键会启动豆包输入法语音。

详细配对、虚拟声卡配置、按键映射和故障排查见
[`apps/windows/rc003/README.md`](apps/windows/rc003/README.md)。

## 从源码本地运行

在 Windows PowerShell 中执行：

```powershell
Set-Location apps/windows/rc003
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
.\.venv\Scripts\python.exe -m ovb_rc003 --settings
```

运行桥接（单独的桥接进程）：

```powershell
.\.venv\Scripts\python.exe -m ovb_rc003 --bridge
```

运行测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py' -v
```

构建未签名候选版：

```powershell
.\build\build-candidate.ps1
```

完整安装、配对、VB-CABLE 配置、已知限制和发布流程见 [`apps/windows/rc003/README.md`](apps/windows/rc003/README.md)。

## 来源与许可

- **原始 Remote Mic 项目**：[`HD838A/remote-mic-app`](https://github.com/HD838A/remote-mic-app)（无线麦 Remote Mic：把小米蓝牙遥控器 2 Pro / RC003 变成 Mac 语音输入设备）。
- **GitHub 直接 Fork 来源**：[`miaomiaozii/windows-remote-mic-app`](https://github.com/miaomiaozii/windows-remote-mic-app)。这是本仓库在 GitHub 上的中间 Fork 来源，不是原始 Remote Mic 项目。
- **Windows 上游参考实现**：[`nijez/open-voice-bridge`](https://github.com/nijez/open-voice-bridge)（GPL-3.0-only），提供 WinRT BLE、ATVV 语音协议、Raw Input、SendInput 和 Qt/QML 设置页的参考实现。
- **RC003 HID 旁路参考**：[`xxb26553663-star/remote-bridge-hub`](https://github.com/xxb26553663-star/remote-bridge-hub)（GPL-3.0-only），提供用 Frida Gadget 读取 Windows 普通输入链路拿不到的 RC003 返回/音量 HID 报告的实现思路。

本仓库只保留并继续维护 Windows RC003 客户端；macOS 应用、Swift 工程和 macOS
发布资源不在此仓库维护。Windows 客户端在上述 GPL-3.0-only 来源基础上继续开发，
并增加了按键兼容修复、语音与音频桥接、设置界面、安装构建和真实硬件适配。

Windows 实现的改动说明与第三方边界见
[`apps/windows/rc003/ATTRIBUTION.md`](apps/windows/rc003/ATTRIBUTION.md)、
[`COPYRIGHT.md`](COPYRIGHT.md) 和 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

## 许可证

代码按 `GPL-3.0-only` 发布。完整许可证见 [`LICENSE.md`](LICENSE.md)。

## 维护边界

- 主程序源码只在 `apps/windows/rc003`；
- Windows CI 位于 `.github/workflows/windows-rc003-ci.yml`；
- `device-profiles` 只保留 Windows 客户端使用的设备目录；
- `LICENSE.md`、`COPYRIGHT.md`、`THIRD_PARTY_NOTICES.md` 和
  [`apps/windows/rc003/ATTRIBUTION.md`](apps/windows/rc003/ATTRIBUTION.md) 保留 GPL
  与上游来源义务，不代表继续维护原 macOS 应用。
