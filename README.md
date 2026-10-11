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

1. 在“协议配置”中加载配置或填写与目标 SCI 收发模块一致的参数，点击“应用配置”。官方 FOC 示例也可在对应控制页点击“使用预设”。
2. 选择端口和波特率，点击“连接”。勾选“模拟数据”可离线试用。
3. 在“通用收发”中输入发送值，选择手动或周期发送；控制官方示例时使用“单电机 FOC”或“对拖 FOC”。
4. 在示波器中选择通道和显示方式，调整时间窗口与存储深度。
5. 点击“开始记录”保存数据；记录完成后，在“记录分析”中打开会话目录，选择记录段和时间区间进行分析或导出。

### 收发配置

帧格式为：

```text
帧头 + 指定数量、统一类型的数据元素 + 帧尾
```

帧头、帧尾可为空，支持 ASCII 和十六进制输入。载荷支持 `single`、`int8`、`uint8`、`int16`、`uint16`、`int32`、`uint32`，字节序可选小端或大端。

发送的 `Data length` 表示元素数量；接收元素数量为 `通道数 × 每帧采样组数`。例如 600 组、2 通道表示每路 600 个采样点。通道名称、单位和换算参数在“字段名称与换算”中设置。

内置配置位于 `profiles/`，包括通用 SCI、单电机 FOC 和对拖 FOC 预设。自定义配置可保存为 JSON。帧格式在连接前设置，修改时需先断开连接；增加通道也需要同步修改目标程序。

### 示波器

实时显示持续跟随最新数据。“存储深度”表示每通道保留的采样点数；暂停显示后可查看缓存快照，采集和记录仍继续，恢复显示后回到最新数据。

绘图抽稀仅用于显示，记录与导出保留未抽稀的数据。存储深度属于示波器设置，不影响通信协议或记录长度。

### FOC 控制

选择与目标程序对应的预设，填写电机基准值和给定值。修改参数后点击“更新参数”发送，启停按钮立即发送对应命令。“联动接收通道”按调试组设置通道名称与换算，使用自定义上传通道时应关闭。

连接不会自动启动电机；断开连接或关闭软件不会自动停止电机。控制页显示的是主机发送的命令，目标协议没有命令确认。

### 记录与分析

记录以 HDF5 保存，按会话管理；长时间记录、接收重同步或通道语义变化时自动分段。在“记录分析”中选择通道和时间区间，可查看波形、测量游标、计算统计量及 FFT，或导出 CSV、MAT。

“用 MATLAB 打开”需要本机安装 MATLAB。安装了 Simulink 时使用 Simulation Data Inspector，否则使用 MATLAB 绘图。MATLAB 不是软件日常使用的必需依赖。

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
