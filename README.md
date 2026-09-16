# WorkBuddy Switch

> 为腾讯 **WorkBuddy AI** 桌面客户端做的一键换号 + 本地数据自动同步工具。
>
> 灵感来自 [`pjpv/zcode-switch`](https://github.com/pjpv/zcode-switch)（ZCode 多账号切换器），
> 数据同步逻辑参考 [`xiaoliuzhuan666/workbuddy-account-migrate`](https://github.com/xiaoliuzhuan666/workbuddy-account-migrate)。

---

## 这是什么

WorkBuddy AI 用 `user_id` 做数据隔离 —— 换账号登录后，旧账号的**会话记录、长期记忆、连接器配置、自动化任务**在界面里全都"消失"了（数据其实还在磁盘上，只是被隔离到另一个 `uid` 名下）。

WorkBuddy Switch 解决三件事：

| 功能 | 说明 |
| --- | --- |
| **一键换号** | 点一下就在多个账号间切换：结束客户端 → 写入目标账号身份与登录态 → 恢复该账号私有数据 → 同步本地档案 → 重启客户端 |
| **会话自动恢复** | 会话按账号**长期留存在本地**，登录哪个账号就自动恢复哪个账号的会话；来回切换双向无损 |
| **自动同步本地信息** | 把账号的 7 类数据（Sessions / Memory / Connectors / Automations / 账号设置 / 私有存储 / 身份与凭据）自动同步，附带全量备份与一键回滚 |

**零第三方依赖**：纯 Python 标准库（GUI 用 tkinter），不需要装任何包。

---

## 下载即用（推荐）

到 [Releases](https://github.com/Danjack85/workbuddy-switch/releases) 下载 `WorkBuddySwitch.exe`，
双击即可 —— **不需要安装 Python，不需要装任何依赖**。

它是一个单文件程序（约 15 MB），两种用法：

| 用法 | 操作 |
| --- | --- |
| **图形界面** | 直接双击 exe |
| **命令行** | `WorkBuddySwitch.exe state` / `switch --id 主号` / `sessions` … |

> 首次运行 Windows 可能弹「已保护你的电脑」（SmartScreen）——因为 exe 没有代码签名。
> 点「更多信息」→「仍要运行」即可。也可以自己从源码打包（见下文）。

### 自己打包

```bat
build.bat
```

产物在 `dist\WorkBuddySwitch.exe`。需要 Python 3.9+ 与 PyInstaller（脚本会自动装）。

---

## 会话为什么会"丢"，以及本工具怎么解决

这是本项目最核心的一件事，值得单独讲清楚。

WorkBuddy 的会话表长这样（`workbuddy.db`）：

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,   -- 会话 id，主键
    user_id TEXT NOT NULL, -- 归属账号
    title TEXT, ...
)
```

客户端**只显示当前登录账号名下的会话**，而 `id` 是主键 —— 一条会话只能挂在一个 `user_id` 下。所以：

- 换号后，旧账号的会话仍在磁盘上，但客户端不显示 → 看起来「丢了」；
- 常见的"把会话 UPDATE 成新 uid"的搬法是**错的**：来回切几次，会话就在账号间来回搬，总有一边是空的。

本工具改用**本地会话档案库**：

```
~/.workbuddy-switch/sessions.db      ← 每个会话一条完整副本 + owner_uid（它真正属于谁）
```

- **留存**：切号前先把客户端会话全量收进档案库，并记下 **owner**（首次记录的归属之后不再改变）；
- **恢复**：登录账号 X 时，把档案里所有 `owner=X` 的会话写回客户端并标记为 X（X 立刻看到自己的全部历史）；其余账号的会话归位回各自 owner（对 X 不可见，但**数据仍在**）；
- **补回**：如果客户端里某条会话被误删/丢失，档案里还有，激活时会自动补回。

效果：**A 的会话永远是 A 的**。A → B → A 来回切，每次都能看到自己完整的历史，两边都不丢。

---

## 快速开始

### 环境要求

- Windows 10/11
- Python 3.9+
- 需要 **tkinter**（GUI 用）。官方 Python 安装包自带；部分精简版 / Store 版可能缺失，见下方"常见问题"

### 运行

```bat
:: 启动图形界面
run.bat

:: 或者
python -m wbswitch.cli gui
```

### 命令行

```bat
:: 查看状态与诊断
python -m wbswitch.cli state
python -m wbswitch.cli diagnose

:: 把当前登录的账号存进档案库
python -m wbswitch.cli capture --name "主号"

:: 列出所有账号
python -m wbswitch.cli list

:: 一键换到某个账号（支持 id 前缀 / 名称 / uid 前缀）
python -m wbswitch.cli switch 主号

:: 只搬数据、不切登录态（重启后自行登录）
python -m wbswitch.cli switch 主号 --no-login

:: 反向操作：把某个旧账号的数据同步到"当前登录"的账号
::（等价于 workbuddy-account-migrate 的交互流程）
python -m wbswitch.cli sync 旧号

:: 会话档案库：查看 / 手动留存 / 让某账号的会话就位
python -m wbswitch.cli sessions
python -m wbswitch.cli sessions --capture
python -m wbswitch.cli sessions --activate a1b2c3d4

:: 备份 / 回滚
python -m wbswitch.cli backups
python -m wbswitch.cli backup --label "换号前"
python -m wbswitch.cli rollback 20260916-213000

:: 进程控制
python -m wbswitch.cli kill
python -m wbswitch.cli launch
```

任意命令加 `--lang en` 可切换英文输出。

---

## 界面

`docs/screenshot.png` 是主界面：顶部状态灯 + 工具栏，中间是账号卡片列表（当前账号高亮描边 + 「使用中」标签），底部是运行日志。

每个账号卡片右侧有动作按钮：**换号** / **同步** / **详情** / **重命名** / **删除**。
卡片上会显示该账号**本地留存的会话数**，以及是否存有登录态（`登录态已存`），
这样切过去之前就知道会不会需要重新登录。

---

## 它是怎么工作的

### WorkBuddy 的数据隔离模型

先摸清了数据落在哪里。账号身份的权威来源是：

```
~/.workbuddy-ai/storage/skeleton/account-snapshot.json   →  primary.uid
```

而**登录态本身**（决定"我是谁"）在另一个地方 —— 扩展数据目录，不在数据根下：

```
%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info
```

这是一份 JSON，`account.uid` 是账号，`auth.accessToken` / `refreshToken` 是**明文 JWT**。
客户端每次登录都会在这里落一份（含带时间戳的历史文件），并在登出时写一个
`<同名>.logged-out` 标记；该标记存在时上述文件会被忽略。

围绕 uid，数据分散在 **7 个位置**：

| # | 位置 | 内容 | 本工具如何处理 |
| --- | --- | --- | --- |
| 1 | `storage/skeleton/account-snapshot.json` | 身份快照（uid / 昵称 / Pro 状态） | 切换时写入并回读校验 |
| 2 | 扩展数据目录 `.../auth/*.info` | 登录态（令牌） | 按账号归档，切换时写回 |
| 3 | `storage/user-{uid}-*/` | 该账号的私有存储 | 档案级快照还原 |
| 4 | `sessions` 表（SQLite） | 会话记录，按 `user_id` 隔离 | **本地档案库按 owner 留存/恢复** |
| 5 | `automations` 表 | 定时任务，按 `owner_user_id` 隔离 | UPDATE 重打标 |
| 6 | `memory/{uid}_memory.md` | 长期记忆 | Memory Block 智能合并 |
| 7 | `connectors/{uid}/` | 连接器 / MCP 配置 | JSON 深度合并 |

命令行 `diagnose` 会把这张表实际跑一遍给你看，`sessions` 则单独展示本地档案库的留存情况：

```
会话档案库（本地留存）
======================================================================

  位置   C:\Users\you\.workbuddy-switch\sessions.db
  总数   8 条

  uid                                        会话数  账号
  ----------------------------------------------------------------------
  a1b2c3d4-1111-2222-3333-444455556666            8  you@example.com
```

### 换号的完整流程

```
① 自动保全当前登录（未建档则建档）
② 把 auth 目录里出现过的账号都建档（谁登录过就能直接切到谁）
③ 把客户端现有会话收进本地档案库（记录 owner）
④ 全量备份
⑤ 结束客户端（非热切换）
⑥ 读取登录文件 → 归档当前账号登录态、导入其它账号登录态
⑦ 同步记忆 / 连接器 / 定时任务 / 账号设置（不搬会话）
⑧ 写回目标账号的登录态 + 清除登出标记  ← 免登录的关键
⑨ 激活目标账号的会话（该账号会话就位，其余归位隐藏）
⑩ 写入身份快照 + 回读校验，重启客户端
```

其中第 ⑧ 步让「凭据级换号」成为可能：把目标账号那份 `.info` 写回客户端，
再把登出标记删掉，客户端重启后即认为登录态有效，**不需要重新登录**。

### 同步引擎：踩过的三个坑

上游 `workbuddy-account-migrate` 是个好起点，但直接照搬会出问题。这三个坑在 `wbswitch/engine.py` 里都做了处理：

**坑 1 · 用"最新会话的 user_id"推断当前账号是错的**

旧账号的最后一条会话可能比新账号更晚更新，于是推断出错误的目标 uid，把数据同步到了空气里。

→ 本工具**只认 `account-snapshot.json` 的 `primary.uid`**，并额外读数据库交叉验证。写回身份快照后还会**回读校验**（`_write_identity_verified()`，带重试）。

**坑 2 · 记忆文件合并会污染**

`memory/{uid}_memory.md` 里除了正文，还嵌了一段结构化的 `RAW_JSON` 元数据。朴素的"按行去重追加"会把 `"uid": "..."`、`> Last updated:` 这类碎片当成记忆内容追加进去，越迁越脏。

→ 本工具先解析 `RAW_JSON_START` / `RAW_JSON_END` 段，按 `memoryBlock` 正文合并，再**重新渲染**整个文件（含改写为正确 uid 的 RAW_JSON）。

**坑 3 · 密钥文件不能复制**

`connectors/{uid}/.master.key` 和 `connector-states.json` 里的 `encryption` / `accountIdentityKey` 是**按 uid 绑定的密钥材料**。复制过去会破坏目标账号自己的解密能力，导致连接器全部失效。

→ `_strip_forbidden()` + `_CONNECTOR_FORBIDDEN_FILES` 会在合并前把这些键和文件剔除掉。

另外，迁移前后各执行一次 `PRAGMA wal_checkpoint(TRUNCATE)` —— SQLite 的 WAL 日志如果不落盘，客户端重启后可能丢数据。

### 安全模型：三层防护

```
① 档案库（Profile）  ~/.workbuddy-switch/accounts/{id}.json
     · 每个账号一条记录 + 一份私有数据快照
     · id 白名单校验 [A-Za-z0-9-]，防路径穿越
     · 原子写入（temp + os.replace）

② 全量备份          ~/.workbuddy-switch/backups/{tag}/
     · 换号 / 同步前自动打一份
     · 可一键回滚（回滚前再自动备份一次当前状态）

③ Dry-run
     · 设置里可开启，只报告"会改什么"，不落盘
```

还有一条硬规则：**绝不丢号**。切换前如果发现当前登录的账号还没入库，会自动建档（`auto_preserve_current()`）；auth 目录里登录过的账号也会自动建档（`adopt_accounts_from_auth()`）。

---

## 凭据级换号：什么时候不需要重新登录

和 `zcode-switch` 一样，本工具做的是**真正的凭据替换**：把目标账号的登录态文件写回客户端，
再删掉登出标记，客户端重启后便认为该账号已登录。因此通常情况下**不需要重新登录**。

能不能免登录，取决于本地有没有该账号的登录态：

| 情况 | 结果 |
| --- | --- |
| 该账号在本机登录过（auth 目录里有它的 `.info`） | ✅ 直接切换，无需登录 |
| 该账号从没在本机登录过 | ⚠️ 需要登录一次；登录完成后会话/记忆/连接器已全部就位 |

因此换号前建议先让每个账号各登录一次 —— 之后它们就都能直接互切了。
工具在每次换号时会自动把 auth 目录里的账号全部导入本地档案，所以这一步只需做一次。

**状态一致性**：如果登录态没能切过去（目标账号没有可用凭据），工具**不会**去写身份快照。
否则会出现「快照说 B、实际登录 A」的矛盾状态，下一次换号就会被误导。这种情况会明确提示
需要登录一次，而不是留下一个自相矛盾的状态。

### 两个边界情况

- **热切换**：运行中的客户端持有登录文件，此时**跳过凭据替换**（写了也会被覆写回去），
  只搬数据、其他会话归位。需要免登录就用非热切换（默认）。
- **令牌过期**：登录态里的 token 有有效期。实测 WorkBuddy 签发的令牌**有效期约一年**
  （`expiresAt` ≈ 365 天），若已过期，客户端会要求重新登录一次，之后再切就不需要了。
  工具会在换号**之前**就检查并提示（界面卡片显示「凭据已过期」），数据始终不受影响。

### 关于凭据安全性（实测结论）

- 令牌是**明文 JWT**，不是加密存储 —— 所以文件权限必须收紧，这一点本工具已处理；
- 实测同一账号多次登录会拿到**不同的 refreshToken**（说明服务端会轮换），但
  客户端**切换账号时不调用服务端注销**（`switchAccount` 只把新会话写到本地，
  走 `preserveLogoutMarker`），因此**归档的其它账号凭据不会被切换动作作废**；
- 只有用户主动点「登出」才会请求服务端 `/console/logout`。

所以"多账号凭据共存、随时互切"在本机是成立的：各账号令牌互不影响。

### 权限与安全

登录态文件含**明文令牌**，因此：

- 快照目录与写回的文件都会收紧权限（Windows 用 `icacls` 只授予当前用户，POSIX 用 `0600`）；
- 令牌**绝不**写入日志、档案 JSON 或操作流水；
- 写回走「临时文件 + 原子重命名」，避免半截文件；
- 档案库里的所有写入同样是原子的。

---

## 项目结构

```
workbuddy-switch/
├─ app.py            打包入口（双击开 GUI / 带参数走 CLI）
├─ build.spec        PyInstaller 配置（单文件 exe）
├─ build.bat         一键打包
├─ run.bat           源码模式启动 GUI
├─ icon.ico/.png     应用图标
├─ wbswitch/
│  ├─ paths.py       路径探测（数据根 / 扩展数据目录 / 登录态 / 档案库 / 备份）
│  ├─ i18n.py        中英双语词条
│  ├─ config.py      设置项（dataclass 读写 settings.json）
│  ├─ profiles.py    账号档案库（CRUD + 私有数据快照 + 登录态归档/导入）
│  ├─ sessions.py    会话档案库（按 owner 留存，登录时激活对应账号的会话）
│  ├─ engine.py      同步引擎（记忆/连接器/自动化/账号设置 + 备份回滚）
│  ├─ client.py      WorkBuddy 进程控制（tasklist / taskkill / 启动）
│  ├─ switcher.py    一键换号编排 + 状态聚合 + 历史流水
│  ├─ cli.py         命令行（17 个子命令）
│  └─ gui.py         tkinter 桌面界面（深色主题，零依赖）
├─ tests/selftest.py 沙箱端到端自检（73 项）
├─ tools/
│  ├─ make_icon.py   生成应用图标
│  ├─ check_i18n.py  中英词条一致性检查（CI 会跑）
│  └─ render_shot.py GUI 截图生成
├─ .github/workflows/build.yml   CI：语法检查 + 打包 + 打 tag 自动发 Release
└─ docs/
```

### 工具自己的数据

```
~/.workbuddy-switch/
├─ accounts/{id}.json      账号档案（身份信息，不含令牌）
├─ accounts/{id}/private/  该账号私有存储快照
├─ accounts/{id}/session/  该账号登录态快照（含令牌，权限已收紧）
├─ sessions.db             会话档案库（每个会话一条副本 + owner）
├─ backups/{tag}/          全量备份（含 sessions.db，可一键回滚）
└─ history.jsonl           操作流水
```

### 环境变量（用于沙箱测试）

| 变量 | 作用 |
| --- | --- |
| `WBSWITCH_WORKBUDDY_HOME` | 指定 WorkBuddy 数据根（隔离测试用） |
| `WBSWITCH_STORE` | 指定档案库 / 备份目录 |
| `WBSWITCH_AUTH_DIR` | 指定登录态目录（隔离测试用） |
| `WBSWITCH_SANDBOX=1` | 禁用一切进程操作（kill / launch 变空操作） |
| `WBSWITCH_LANG` | `zh` / `en` |

### 跑自检

```bat
python tests\selftest.py
```

自检会在**临时目录里复制一份真实数据的必要部分**当沙箱，然后验证 10 组共 73 项断言：
沙箱搭建 → 建档 → 同步（含记忆无污染、RAW_JSON uid 改写、连接器深度合并不覆盖、
`.master.key` 未被复制、账号设置补齐、完整性检查）→ 幂等性 → dry-run →
**回滚（含"回滚不会覆盖自身备份"回归）** → 换号（dry-run + 真实，含凭据切换与回读校验）→
**会话档案库（两账号各有会话，来回切换双向无损）** → **归属变更（档案库与客户端一起改）** → 状态与诊断。
**全程不碰真实数据。**

```
73 通过 / 0 失败
```

CI 还会跑 `python tools/check_i18n.py` 检查中英词条对齐（缺键即失败）。

---

## 与上游项目的关系

| | zcode-switch | workbuddy-account-migrate | **WorkBuddy Switch** |
| --- | --- | --- | --- |
| 目标 | ZCode | WorkBuddy | WorkBuddy |
| 形态 | Tauri 2 桌面应用 | Python 脚本 | Python 桌面应用 + CLI |
| 换号 | 凭据文件替换 | ❌ | ✅ 凭据替换 + 数据就位 + 重启 |
| 会话留存 | — | ❌ | ✅ 本地档案库按账号留存，登录自动恢复 |
| 同步 | ❌ | ✅ 单次交互式迁移 | ✅ 可反复调用、可回滚 |
| 备份回滚 | 部分 | 简单备份 | ✅ 全量 + 一键回滚 |
| 依赖 | Rust + Node | 标准库 | **仅标准库** |

本项目是独立实现，参考了 zcode-switch 的交互设计与 account-migrate 的数据模型，但重写了同步引擎并修掉了下面这些坑。

### 相比上游修掉的问题

**坑 1 · 用"最新会话的 user_id"推断当前账号是错的**

旧账号的最后一条会话可能比新账号更晚更新，于是推断出错误的目标 uid。

→ 本工具**只认 `account-snapshot.json` 的 `primary.uid`**，并以登录态文件里的 uid 交叉校验
（两者不一致时以登录态为准并告警，因为那才是持有令牌的账号）。写回后还会**回读校验**。

**坑 2 · 记忆文件合并会污染**

`memory/{uid}_memory.md` 里除了正文，还嵌了一段结构化的 `RAW_JSON` 元数据。朴素的"按行去重追加"会把 `"uid": "..."`、`> Last updated:` 这类碎片当成记忆内容追加进去，越迁越脏。

→ 本工具先解析 `RAW_JSON_START` / `RAW_JSON_END` 段，按 `memoryBlock` 正文合并，再**重新渲染**整个文件（含改写为正确 uid 的 RAW_JSON）。

**坑 3 · 密钥文件不能复制**

`connectors/{uid}/.master.key` 和 `connector-state.json` 里的 `encryption` / `accountIdentityKey` 是**按 uid 绑定的密钥材料**。复制过去会破坏目标账号自己的解密能力。

→ `_strip_forbidden()` + `_CONNECTOR_FORBIDDEN_FILES` 会在合并前把这些键和文件剔除掉。

**坑 4 · 用"改 user_id"的方式搬会话**

这是本项目最重要的修正。在本地副本上实测发现：把会话 `UPDATE` 成目标 uid 之后，
**切回原账号会看到空列表** —— 因为会话真的改了归属，来回切就在账号间来回搬。

→ 改为**本地会话档案库**按 `owner` 留存（见上文「会话为什么会丢」）。另外发现并修掉了一个
自检覆盖不到的严重 bug：备份 tag 只精确到秒，回滚前自动生成的备份会与**被回滚的那个备份同秒同名**，
导致回滚先把要恢复的内容覆盖掉、再从中恢复，**回滚实际上是空操作**。现在 tag 会去重并加了回归测试。

另外，迁移前后各执行一次 `PRAGMA wal_checkpoint(TRUNCATE)` —— SQLite 的 WAL 日志如果不落盘，客户端重启后可能丢数据。


---

## 常见问题

**Q: 换号后旧账号的会话去哪了？**

不会丢。切号前会把客户端会话全量收进本地档案库并记住它属于哪个账号；
切回该账号时自动恢复。见上文「会话为什么会丢」与「换号的完整流程」。

**Q: 点了换号，客户端起来后还是要重新登录？**

看该账号在本机登录过没有。登录过（auth 目录里有它的 `.info`）就直接切过去，无需登录；
从没登录过的需要登录一次，登录后数据已全部就位。见上文「凭据级换号」。

**Q: 会话档案库在哪？会不会很大？**

在 `~/.workbuddy-switch/sessions.db`，每个会话存一行完整记录（含标题与元数据），
不含对话大文本，所以很小。它也会跟全量备份一起走，`rollback` 时会一并还原。

**Q: 提示 `No module named 'tkinter'`？**

你用的 Python 没带 tkinter（常见于 Microsoft Store 版或精简版）。装官方 python.org 的安装包（安装时勾选 `tcl/tk and IDLE`）即可。CLI 不受影响，可以先用 `python -m wbswitch.cli`。

**Q: 数据会不会被改坏？**

每次换号 / 同步前都会自动打全量备份（含数据库、记忆、连接器、账号设置、登录态与会话档案库），
`backups` 里能看到，随时 `rollback`。回滚本身也会先备份当前状态。另外建议先跑 `diagnose` 确认路径识别正确。

**Q: 客户端路径认不出来？**

设置里可以手动指定 `WorkBuddyAI.exe`。工具会依次尝试：设置值 → 运行中进程 → 注册表卸载项 / App Paths → 常见安装路径。

**Q: 支持 macOS / Linux 吗？**

数据层是跨平台的（走 `Path.home()`），但进程控制（`tasklist` / `taskkill`）和注册表探测目前是 Windows 实现。macOS 上 `client.py` 需要补一套适配。

---

## License

MIT
