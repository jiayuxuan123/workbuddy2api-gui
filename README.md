# WorkBuddy2API

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.9+-blue.svg?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/GUI-PySide6_(Qt6)-41CD52.svg?style=flat-square" alt="PySide6">
  <img src="https://img.shields.io/badge/API-OpenAI_Compatible-412991.svg?style=flat-square" alt="OpenAI API">
  <img src="https://img.shields.io/badge/Dual_Realm-Intl_%26_CN-0DBD8B.svg?style=flat-square" alt="Dual Realm">
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat-square" alt="License">
</p>

把 **WorkBuddy**（腾讯 `www.workbuddy.ai` 国际版 / `codebuddy.cn` 国内版）的账号
封装成标准 **OpenAI 兼容接口**，并提供一个完整的**桌面图形界面**。

支持双区域独立路由、多账号轮询、OAuth 一键登录、设备指纹隔离、用量统计、
开机自启；打包后是一个双击即用的独立程序，不需要命令行。

> 本项目是 **[ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)**
> 的衍生版本：在其协议实现基础上重做了图形界面，并修复了若干并发与打包缺陷。
> 逆向成果来自上游，详见文末 [致谢](#致谢)。

---

## 界面

所有操作都在窗口里完成，不再需要 `.bat` 脚本或命令行。

| 标签页 | 用途 |
|---|---|
| **概览** | 运行状态、请求数 / Token / 失败统计、接入地址与 API Key（一键复制） |
| **账号** | OAuth 添加（国内版 / 国际版）、扫描导入桌面凭证、查额度、刷新令牌、启用停用删除 |
| **用量** | 输入 / 输出 / 思考 / 缓存 Token 汇总，按模型拆分 |
| **日志** | 实时日志，按级别过滤、按关键字搜索、自动滚动 |
| **设置** | 端口、局域网开关、**开机自启**、system 提示词、User-Agent、数据目录 |

界面之外还有几个便利之处：

- **系统托盘** —— 关窗口时可选择最小化到托盘，网关在后台继续服务；
  双击托盘图标恢复窗口，右键菜单可直接启停服务
- **关闭时询问** —— 正在运行时关窗口会问「停止并退出 / 最小化到托盘 / 取消」，
  不会误杀正在给客户端服务的实例
- **开机自启** —— 设置里勾选即可，写入 `HKCU\...\Run`，不需要管理员权限，
  任务管理器的「启动」页可见、可随时禁用

---

## 快速开始

### 方式一：安装版（推荐新用户）

从 [Releases](../../releases) 下载 `WorkBuddy2API-<版本>-setup.exe`，双击运行安装向导：

1. 选择安装位置（可「仅为我安装」或「为所有用户安装」）
2. 勾选是否创建**桌面快捷方式** / **开始菜单快捷方式**
3. 完成后从开始菜单或桌面启动

安装后会登记到「程序和功能」，可正常卸载；卸载时默认保留账号与用量数据。

#### 已装过怎么办：向导会自动找到它

双击安装包时，向导会先探测本机已有的安装，**不会**盲目再装一份到默认目录。
探测有三个来源，按可信度排序：

| 来源 | 说明 |
|---|---|
| 注册表 | `HKLM`/`HKCU` 卸载项里的 `InstallLocation`（安装器自己写的，最准） |
| 运行中进程 | 枚举进程镜像路径 —— **正在跑的那一份**一定是真安装 |
| 默认目录 | `%ProgramFiles%\WorkBuddy2API` 或 `%LOCALAPPDATA%\Programs\WorkBuddy2API` |

只有目录里确实存在 `WorkBuddy2API.exe` 才会被认作候选，幽灵目录与已删除的残留
自动过滤，同一路径只计一次。

- **什么都没装过** → 直接进「选择安装位置」页，行为与以前一致。
- **找到一处** → 路径预填为那一份，向导会说明为什么这么填，直接下一步就是升级。
- **找到多处** → 多出一个「选择已有安装」页，列出每条路径的版本、安装模式
  （仅我 / 所有用户）、账号数与运行状态，选哪条就更新哪条。想装到别处就选
  「安装到新位置」。

### 方式二：便携版（免安装，可转移）

从 [Releases](../../releases) 下载 `WorkBuddy2API-vX.Y.Z-win64.zip`，解压到任意目录后
双击 `WorkBuddy2API.exe`：

1. 点右上角 **启动服务**
2. 切到 **账号** 标签页 → **添加账号 (OAuth 授权)**
3. 选择版本（国内版 / 国际版），点「在浏览器中打开」并完成登录
4. 账号自动入池；回概览页复制接口地址即可接入客户端

无需安装 Python 或任何运行时，也不写注册表，适合放在 U 盘或移动硬盘里。

> 装在 `Program Files` 时，程序内的「就地更新」需要管理员权限；
> 想省掉提权就选便携版，放在可写目录里。

### 方式三：源码运行

```bash
pip install PySide6
python wb_gui.py
```

### 方式四：仅服务端（无界面）

```bash
python wb_proxy.py --port 8788
```

行为与上游一致，也适用于 Docker（见 `Dockerfile` / `docker-compose.yml`）。

---

## 客户端配置

| 项目 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:8788/v1` |
| API Key | 仅本机模式无需填写；开放局域网后必填（自动生成） |
| 协议 | Chat Completions 与 Responses API 均支持 |

兼容 Codex、Claude Code 及各类标准 OpenAI 客户端。模型列表可在 `/v1/models`
获取，含上下文长度、多模态、推理档位等能力元数据。

---

## 打包

```bash
pip install pyinstaller PySide6
pyinstaller workbuddy2api.spec --noconfirm            # onedir，推荐（秒开）
ONEFILE=1 pyinstaller workbuddy2api.spec --noconfirm  # 单文件
```

产物位于 `dist/WorkBuddy2API/`。数据目录（`accounts/`、`usage/`、`gateway.json`）
会创建在 EXE 旁边，整个文件夹可随意移动或拷贝，数据跟着走。

> **两个打包陷阱**（都踩过，spec 里已加注释）：
>
> 1. `excludes` 里**不要加 `email`** —— 标准库 `urllib.request` 导入时会用到它，
>    排除后 EXE 启动即 `ModuleNotFoundError`。
> 2. `platforms/qwindows.dll` **必须随包** —— 否则 Qt 起不来，而且因为
>    `console=False`，用户看不到任何报错，表现为「双击没反应」。

### 打安装包

```bash
python -X utf8 _build_installer.py   # → dist/WorkBuddy2API-<版本>-setup.exe
python -X utf8 _pack_release.py      # → dist/WorkBuddy2API-v<版本>-win64.zip
```

`_build_installer.py` 依次做三件事：把 `dist/WorkBuddy2API/` 打成 `payload.zip`
（同时校验载荷里没有 `accounts/`、`usage/`、`gateway.json`、`install.json`
这些**用户数据**）、用 `uninstaller.spec` 编译卸载程序、再用 `installer.spec`
把载荷与卸载程序一起塞进单文件 `setup.exe`。版本号统一取自
`wb_proxy.APP_VERSION`，不另外维护。

---

## 测试

```bash
python _test_usage_equivalence.py   # 18 项：用量读取重构的逐字段等价性
python _test_concurrency.py         #  4 项：并发写入不丢行
python _test_custom_tools.py        # 26 项：工具调用协议回归
python _test_gui_qt.py              # 47 项：界面功能 + 跨线程结果投递
python _test_installer.py          # 118 项：安装器（含已有安装检测、快捷方式回读）
python _test_cat_travel.py          # 27 项：猫猫旅行协议（离线假 urlopen）
python _test_exe_qt.py <EXE目录>    # 11 项：打包产物端到端
```

安装器测试需要真写注册表与磁盘，因此**必须**在临时目录里跑，不要指向真实安装位置。

界面测试里有专门一项验证**跨线程结果投递**：工作线程的结果必须在 UI 线程执行。
这一项存在的意义见下节。

---

## 相对上游的改动

### 图形界面

新增完整的 PySide6 (Qt6) 桌面程序，把原先散落在 `.bat` 与网页看板里的操作
集中到窗口内。

**为什么是 Qt 而不是 tkinter**（我一开始用的是 tkinter）：tkinter 禁止在工作
线程调用 `widget.after()`，会抛

```
RuntimeError: main thread is not in main loop
```

当时的异步实现正是这么写的，后果是**所有网络结果都回不来**，而异常死在后台
线程里无从察觉 —— 界面表现为「等待授权」永远不动。Qt 的信号/槽使用队列连接，
槽函数保证在接收者线程执行，这类缺陷结构上不会出现。上面那项测试就是用来
钉住这个保证的。

### 修复的缺陷

均经运行时验证，不靠推测：

| 问题 | 证据与修复 |
|---|---|
| **并发写日志丢数据** | 16 线程写 640 条，磁盘只落 **541** 条，另有 223 次 `WinError 5`。文本模式追加在并发下会互相覆盖（每次新开 handle 也不能解决），且 summary 重写失败会**中断整个写入**连行一起丢。改为加锁的单次 `os.write` + `O_APPEND`，summary 失败单独报告 → 640/640 零丢失。 |
| **打包后数据蒸发** | 路径原基于 `__file__`，onefile 模式下指向**退出即删的临时目录**，账号与用量每次重启消失。新增 `wb_runtime.py` 区分只读资源（`_MEIPASS`）与可写数据（EXE 旁），并处理只读安装目录的回退。 |
| **`--noconsole` 启动即崩** | `log()` 无条件写 `sys.stderr`，而打包后它是 `None`。因网关几乎每条路径都记日志，等于第一次写日志就整个进程挂掉。 |
| **看板每 5 秒重解析全日志** | 13.2 MB 日志每次轮询 **382 ms**，改用增量索引后 **0.49 ms**（约 785 倍）。输出经 18 项等价性验证，非「感觉更快」。 |
| **虚报可用账号数** | `count_ready()` 不检查冷却与过期，会显示「2/2 可用」而请求返回 503。新增 `count_usable()` 按实际路由判定统计。 |
| **快捷方式指向文件夹** | 安装器写 `.lnk` 的 `IShellLinkW` 虚表下标整组错位（`SetPath` 写成 17，真值 20，17 其实是 `SetIconLocation`）。COM 照样返回 `S_OK`，于是**静默**写出一个「目标 = 安装目录」的快捷方式，双击只打开文件夹。已按虚表顺序改正，并加**回读校验**：`create_shortcut()` 保存后重读 `GetPath` 比对，不符即报错 —— setter 返回成功不代表写对了。 |
| **猫猫旅行恒返回 400** | `depart` 请求体发了空 `{}`，上游固定回 `{"code":400,"msg":"invalid request"}`。真实协议要求 `{"location_id": N, "duration_hours": M}`，目的地须先取自 `travel/config` 的 `locations[]`。原先的错误处理把响应体丢掉，导致「协议写错」和「网络故障」在日志里长得一样；新增 `_api_call()` 保留原始响应体，才定位到根因。`claim` 相反 —— 空请求体才是对的。 |

---

## 数据与安全

| 路径 | 内容 |
|---|---|
| `accounts/` | 每个账号一个 JSON，**凭证明文存储** |
| `usage/usage.jsonl` | 逐条请求记录，追加写入，重启不丢 |
| `gateway.json` | 界面设置（端口、开关等） |

- 凭证是**明文**的，等同于账号使用权 —— 不要提交到仓库，也不要放进同步盘。
  `.gitignore` 已排除 `accounts/*.json` 与 `usage/*.jsonl`
- 默认只监听 `127.0.0.1`；勾选局域网访问后会强制要求 API Key（自动生成并持久化）
- 走的是你自己的账号额度，不建议用于批量并发

---

## 致谢

- **[ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)** —— 本项目的协议实现基础
- **[Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api)** —— 出站请求指纹脱敏管线、DeepSeek 多轮思维链回填、`tool_choice` 归一化、实时积分接口
- **[lovingfish/workbuddy-cliproxy](https://github.com/lovingfish/workbuddy-cliproxy)** 与 **[mmqz/cpa-multi-plugins](https://github.com/mmqz/cpa-multi-plugins)** —— 早期网关通信与 OAuth 流程参考

---

## 免责声明

本项目为非官方自托管网关，仅供技术研究、协议学习与个人合法授权账号在私有环境
测试使用。本项目不提供任何账号及额度。请严格遵守相关服务条款，禁止用于商业
转售、恶意并发或批量违规操作。
