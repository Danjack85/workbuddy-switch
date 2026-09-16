# WorkBuddy Switch — Tauri 外壳（可选）

这是 **可选** 的实验性外壳，把已经跑通的 Python 引擎包成 Tauri 2 桌面应用（视觉与 `zcode-switch` 更接近）。

## 为什么它是「可选」的

主交付是 `wbswitch/` 里的纯 Python 实现 + tkinter GUI —— **零依赖、开箱即用**。

Tauri 外壳需要更重的工具链：

| 依赖 | 说明 |
| --- | --- |
| Rust 工具链 | `rustup` + `cargo` |
| **MSVC C++ Build Tools** | ⚠️ 关键：Tauri 在 Windows 走 `x86_64-pc-windows-msvc` target，链接需要 `link.exe` |
| Node.js 18+ | 前端构建 |
| WebView2 | Win10/11 通常已内置 |

**注意**：只装 `rustup` 是不够的。如果 `rustc` 报：

```
error: linker `link.exe` not found
you may need to install Visual Studio build tools with the C++ build tools workload
```

说明缺 MSVC。装 [Visual Studio Build Tools](https://visualstudio.microsoft.com/downloads/) 并勾选 **「使用 C++ 的桌面开发」** 工作负载即可。

## 架构

```
┌──────────────────────────────────────────────┐
│  Tauri 2 (Rust)                              │
│  ┌────────────────────────────────────────┐  │
│  │  Web UI (原生 HTML/JS)                 │  │
│  │  账号卡片列表 / 状态灯 / 日志            │  │
│  └───────────────┬────────────────────────┘  │
│                  │ invoke()                  │
│  ┌───────────────▼────────────────────────┐  │
│  │  Rust commands (src/main.rs)           │  │
│  │  · 展开 ~ 路径                          │  │
│  │  · 拼 python -m wbswitch.cli ... 参数    │  │
│  │  · 跑子进程、解析 JSON 输出              │  │
│  └───────────────┬────────────────────────┘  │
└──────────────────┼───────────────────────────┘
                   │ stdout (JSON)
      ┌────────────▼────────────┐
      │  Python 引擎 wbswitch/  │   ← 复用，不重写
      │  engine / profiles /    │
      │  switcher / client      │
      └─────────────────────────┘
```

**关键设计**：Rust 层**不重新实现**任何同步逻辑，只做「调 Python + 转发 JSON」。这样引擎只有一份实现，两边行为必然一致，也避免了在 Rust 里重写 SQLite 合并 / 记忆解析这类容易出错的逻辑。

## 已提供的文件

```
tauri-shell/
├─ src-tauri/
│  ├─ Cargo.toml
│  ├─ tauri.conf.json
│  ├─ build.rs
│  └─ src/
│     ├─ main.rs        命令注册 + 入口
│     └─ bridge.rs      Python 桥（跑 CLI、解析 JSON）
└─ src/
   └─ index.html        最小可用的账号列表 UI
```

## 构建

```bat
:: 先确认引擎可用
python -m wbswitch.cli state

:: 再构建外壳
cd tauri-shell
npm install
npm run tauri dev      :: 开发
npm run tauri build    :: 打包
```

## 需要的 CLI 契约

Rust 桥通过下面的命令与引擎通信（都在 `wbswitch/cli.py` 里已实现）：

| 调用 | 返回 |
| --- | --- |
| `python -m wbswitch.cli --json state` | 完整状态：账号列表 / 当前登录 / 客户端路径 / 设置 |
| `python -m wbswitch.cli --json diagnose` | 路径 + 各 uid 计数 + 完整性 |
| `python -m wbswitch.cli --json list` | 账号数组 |
| `python -m wbswitch.cli capture --name X` | 建档结果 |
| `python -m wbswitch.cli switch X` | `SwitchResult` |
| `python -m wbswitch.cli sync X` | `SyncReport` |
| `python -m wbswitch.cli --json backups` | 备份列表 |

> `--json` 开关见 `cli.py`。若不存在，桥会退化为解析美化后的文本输出。

## 当前状态

外壳是**脚手架**，不是完成品：桥、命令、配置和一份最小 UI 都在，但缺托盘、自动更新、多语言切换等打磨项。如果你想走 Tauri 路线，从这里接着做即可 —— 引擎侧不需要改动。
