# SCI Scope

SCI Scope 是一款面向 TI TMS320F28379D 平台 SCI 收发模块的中文串口上位机，提供实时波形显示、数据记录和分析功能，无需运行 Host Simulink 模型。

软件通过可配置的帧格式适配目标程序，并提供 MathWorks 官方单电机 FOC 与对拖 FOC 示例的预设和控制面板。

## 主要功能

- **通用收发**：配置收发帧格式、数据类型和通道数量，支持手动发送、周期发送和字节预览。
- **实时示波器**：多通道叠加或分图、缩放、自动量程、可调存储深度、暂停显示和游标测量。
- **数据记录**：保存原始采样、配置、通道信息和控制事件。
- **记录分析**：按区间查看波形，计算统计量和 FFT，导出 CSV 或 MAT，也可使用 MATLAB 查看。
- **FOC 控制**：独立的单电机与对拖控制页，支持启停、给定值设置和调试信号选择。
- **模拟数据**：无需连接开发板即可试用示波器、记录和分析功能。

## 安装与运行

### 发布包

下载对应系统的发布包，完整解压后运行：

| 系统 | 发布包 | 启动方式 |
| --- | --- | --- |
| Windows | `SCIScope-windows-x64.zip` | `SCIScope.exe` 或 `启动上位机.bat` |
| Linux | `SCIScope-linux-x64.tar.gz` | `./SCIScope` 或 `./start.sh` |

发布包无需安装 Python。请保留完整的程序目录，包括 `_internal`。

### 从源码运行

需要 Python 3.12 或以上版本。以下命令在项目根目录执行。

**Windows（PowerShell）**

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
.\.venv\Scripts\python.exe .\run.py
```

**Linux（Bash）**

```bash
./install.sh
./.venv/bin/python run.py
```

Linux 需要图形桌面、Qt 运行库和中文字体。Ubuntu 24.04 可安装：

```bash
sudo apt-get install python3-venv libegl1 libopengl0 libxkbcommon-x11-0 libxcb-cursor0 fonts-noto-cjk
```

无法访问串口时，请检查当前用户的设备权限；Ubuntu 通常使用 `dialout` 组，Arch 使用 `uucp` 组。

日常可使用 `启动上位机.bat` 或 `./start.sh` 启动。启动脚本优先运行 `dist/SCIScope` 中的程序，未找到时使用项目虚拟环境运行源码。

## 快速使用

- [快速上手](doc/quick_start.md)：常见用法与操作步骤。
- [界面说明](doc/UI.md)：各页面、选项和控件的作用。

## 使用注意

- 收发格式、通道数和采样率必须与目标程序一致；没有有效数据或频繁重同步时，检查配置、线路和传输带宽。
- 时间轴按配置采样率重建。默认协议没有序号、CRC 或命令确认，无法准确判断整帧数据丢失或命令是否执行。
- 正常结束记录时完成写盘。写盘失败或记录队列溢出会结束记录并标记不完整，实时监看继续。

## 开发

使用 `install.ps1 -Dev` 或 `./install.sh --dev` 安装开发依赖，激活项目虚拟环境后运行：

```bash
python -m pytest -q
```

无图形桌面的测试环境需设置 `QT_QPA_PLATFORM=offscreen`。

打包命令：

| 系统 | 命令 |
| --- | --- |
| Windows（PowerShell） | `powershell -ExecutionPolicy Bypass -File .\build.ps1 -Archive` |
| Linux（Bash） | `./build.sh --archive` |

输出固定为 `dist/SCIScope/` 和对应的发布压缩包，成功构建后替换上一份输出。在虚拟环境中执行 `python tools/smoke_bundle.py` 可检查发布包的采集和记录功能。

GitHub Actions 提供自动测试、构建和发布。版本标签需与 `sciscope/__init__.py` 中的版本一致。

| 路径 | 内容 |
| --- | --- |
| `sciscope/` | 应用源码 |
| `profiles/` | 内置配置 |
| `tests/` | 自动化测试与参考数据 |
| `tools/` | 构建、自检和性能测试工具 |
| `requirements.txt` | 运行依赖 |
| `requirements-dev.txt` | 开发依赖 |
