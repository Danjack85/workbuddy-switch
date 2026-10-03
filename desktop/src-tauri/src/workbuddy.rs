//! WorkBuddy 支持：驱动 Python 侧车引擎。
//!
//! 为什么用侧车而不是移植到 Rust：WorkBuddy 那边的业务逻辑（会话三件套、
//! 正文归档、签到、额度、网关）有 4000 多行经过 145 项自检的 Python 实现。
//! 移植既要重写已验证的代码，又要重新踩一遍上游的坑（`platform` 是必需
//! 查询参数、`code 11128` 是频率风控…），代价远高于收益。
//!
//! 所以把那个引擎当**外部进程**用：它已经会输出稳定的 JSON 信封
//! （`--json --envelope`），Rust 只负责调用、解析、把结果交给前端。
//!
//! ## 三个约束
//!
//! 1. **走 Tauri 的 shell 插件执行**，不用裸 `std::process::Command`：
//!    进程创建由框架统一管理，权限受 capabilities 声明约束，也自动处理了
//!    「不弹控制台窗口」这类平台细节。
//!
//! 2. **必须有超时**：签到 / 额度 / 登录轮询都要联网，上游卡住时不能让
//!    界面永远转圈。
//!
//! 3. **参数必须来自受信来源**：用 [`Arg`] 类型把「只能传校验过的值」
//!    固化下来 —— 值若以 `-` 开头会被下游当成开关（参数注入），校验里
//!    明确拒绝。

use std::path::{Path, PathBuf};

use serde::Serialize;
use tauri::{AppHandle, Manager};
use tauri_plugin_shell::ShellExt;

/// 默认超时：本地读操作约 0.3 秒，这个值只用于兜底。
const DEFAULT_TIMEOUT_SECS: u64 = 60;
/// 联网操作的超时（签到、额度、模型目录）。
const NETWORK_TIMEOUT_SECS: u64 = 180;
/// 登录轮询（前端分段调用，单次窗口默认 25 秒）。
const LOGIN_TIMEOUT_SECS: u64 = 120;

// ==========================================================================
// 受信参数
// ==========================================================================

/// 一个**已校验**、可以安全传给子进程的命令行参数。
///
/// 字段私有 ⇒ 外部无法凭空构造。要拿到一个 `Arg` 只有两条路：
///
/// * [`Arg::lit`] —— 代码里写死的字面量（子命令名、开关名）；
/// * [`Arg::token`] / [`Arg::text`] / [`Arg::choice`] / [`Arg::num`] ——
///   校验函数。它们拒绝空值、超长、换行，**并拒绝以 `-` 开头**。
///
/// 于是「把未校验的输入塞进命令行」在类型层面就写不出来。
#[derive(Debug, Clone)]
pub struct Arg(String);

impl Arg {
    /// 代码里写死的字面量（子命令、开关名）。常量，无需校验。
    fn lit(s: &'static str) -> Self {
        Arg(s.to_string())
    }

    /// 标识符（账号 id / uid / 会话 id / 备份标签）：字母数字与 `- _ .`。
    fn token(value: &str, field: &str) -> Result<Self, String> {
        let v = value.trim();
        if v.is_empty() {
            return Err(format!("{field}不能为空"));
        }
        if v.len() > 128 {
            return Err(format!("{field}过长"));
        }
        if v.starts_with('-') {
            return Err(format!("{field}不能以「-」开头"));
        }
        if !v
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.')
        {
            return Err(format!("{field}含非法字符（只允许字母、数字与 - _ .）"));
        }
        Ok(Arg(v.to_string()))
    }

    /// 自由文本（账号备注名等）：允许任意可见字符，但拒绝 `-` 开头与换行。
    fn text(value: &str, field: &str) -> Result<Self, String> {
        let v = value.trim();
        if v.is_empty() {
            return Err(format!("{field}不能为空"));
        }
        if v.len() > 200 {
            return Err(format!("{field}过长"));
        }
        if v.starts_with('-') {
            return Err(format!("{field}不能以「-」开头"));
        }
        if v.contains('\n') || v.contains('\r') {
            return Err(format!("{field}不能含换行"));
        }
        Ok(Arg(v.to_string()))
    }

    /// 受限枚举（版本等）：只接受白名单取值。
    fn choice(value: &str, allowed: &[&str], field: &str) -> Result<Self, String> {
        let v = value.trim().to_ascii_lowercase();
        if allowed.contains(&v.as_str()) {
            Ok(Arg(v))
        } else {
            Err(format!("{field}取值不合法"))
        }
    }

    /// 数字参数（范围由调用方先夹紧）。
    fn num(v: u32) -> Self {
        Arg(v.to_string())
    }
}

/// 便捷组装参数列表。
macro_rules! args {
    ($($a:expr),* $(,)?) => { vec![$($a),*] };
}

// ==========================================================================
// 侧车定位
// ==========================================================================

/// 侧车文件名。
const SIDECAR_FILE: &str = if cfg!(windows) { "wbswitch.exe" } else { "wbswitch" };

/// 侧车所在目录名。
const SIDECAR_DIR: &str = "wbswitch";

/// 侧车位置信息（给诊断用，也便于排查「找不到侧车」）。
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SidecarInfo {
    pub found: bool,
    pub path: String,
    pub version: Option<String>,
    /// 引擎根目录（供「打开所在目录」这类操作使用）。
    pub dir: String,
}

/// 判定一个候选路径确实是我们的侧车。
///
/// 三重检查：是个文件、文件名等于 [`SIDECAR_FILE`]、父目录名等于
/// [`SIDECAR_DIR`]。即便某个候选位置被别的东西占了，也不会被当成引擎执行。
fn verified(candidate: &Path) -> bool {
    if !candidate.is_file() {
        return false;
    }
    let name_ok = candidate
        .file_name()
        .and_then(|n| n.to_str())
        .map(|n| n.eq_ignore_ascii_case(SIDECAR_FILE))
        .unwrap_or(false);
    if !name_ok {
        return false;
    }
    candidate
        .parent()
        .and_then(|d| d.file_name())
        .and_then(|n| n.to_str())
        .map(|n| n.eq_ignore_ascii_case(SIDECAR_DIR))
        .unwrap_or(false)
}

/// 定位侧车可执行文件。
///
/// 只从受信来源推导：Tauri 给的资源目录、我们自己的进程位置、编译期常量。
/// 要试多个位置，是因为「开发运行」和「安装后运行」的布局不同，而打包工具
/// 对 resources 的落点也可能随版本变化 —— 与其猜一个，不如把已知可能都试一遍，
/// 每个候选都要过 [`verified`]。
fn resolve(app: &AppHandle) -> Option<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();

    let leaf = |base: PathBuf| base.join(SIDECAR_DIR).join(SIDECAR_FILE);

    // 1) 安装后：资源目录（resources 清单里声明的是 sidecar/wbswitch）
    if let Ok(dir) = app.path().resource_dir() {
        candidates.push(dir.join("sidecar").join(SIDECAR_DIR).join(SIDECAR_FILE));
        candidates.push(leaf(dir.clone()));
    }

    // 2) 安装后：主程序所在目录（部分打包方式会把资源放在 exe 同级）
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join("sidecar").join(SIDECAR_DIR).join(SIDECAR_FILE));
            candidates.push(leaf(dir.to_path_buf()));
            candidates.push(dir.join("resources").join("sidecar").join(SIDECAR_DIR).join(SIDECAR_FILE));
            candidates.push(leaf(dir.join("resources")));
        }
    }

    // 3) 开发运行：源码树里的 src-tauri/sidecar（编译期常量推出）
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    candidates.push(manifest.join("sidecar").join(SIDECAR_DIR).join(SIDECAR_FILE));
    if let Some(root) = manifest.parent() {
        candidates.push(root.join("sidecar").join(SIDECAR_DIR).join(SIDECAR_FILE));
    }

    candidates
        .into_iter()
        .find(|c| verified(c))
        .map(normalize_path)
}

/// 去掉 Windows verbatim 前缀（`\\?\C:\...` → `C:\...`）。
///
/// `resource_dir()` 可能返回 verbatim 路径。CreateProcess 本身能执行它，
/// 但下游（日志显示、字符串比较、某些库的路径处理）都会被这个前缀搅乱，
/// 而且没有任何好处 —— 统一剥掉。
fn normalize_path(p: PathBuf) -> PathBuf {
    let s = p.to_string_lossy();
    if let Some(rest) = s.strip_prefix(r"\\?\UNC\") {
        return PathBuf::from(format!(r"\\{}", rest));
    }
    if let Some(rest) = s.strip_prefix(r"\\?\") {
        return PathBuf::from(rest);
    }
    p
}

// ==========================================================================
// 调用
// ==========================================================================

/// 运行侧车命令并解析 JSON 信封。
///
/// `args` 的每一项都是 [`Arg`]；函数自动补上 `--json --envelope`。
pub async fn run(
    app: &AppHandle,
    args: &[Arg],
    timeout_secs: u64,
) -> Result<serde_json::Value, String> {
    let exe = resolve(app).ok_or_else(|| {
        "找不到 WorkBuddy 引擎（侧车）。若刚安装完，请重启应用；仍不行请反馈。".to_string()
    })?;

    // 参数逐项收集：每一项都来自 Arg（字面量或已净化的值）
    let mut argv: Vec<String> = Vec::with_capacity(args.len() + 2);
    for a in args {
        argv.push(a.0.clone());
    }
    argv.push("--json".to_string());
    argv.push("--envelope".to_string());

    let fut = app
        .shell()
        .command(exe.to_string_lossy().to_string())
        .args(argv)
        .output();

    let out = match tokio::time::timeout(std::time::Duration::from_secs(timeout_secs), fut).await {
        Ok(Ok(o)) => o,
        Ok(Err(e)) => return Err(format!("启动引擎失败：{e}")),
        Err(_) => {
            return Err(format!(
                "引擎执行超时（{timeout_secs} 秒）。若操作需要联网，请检查网络后重试。"
            ))
        }
    };

    let stdout = String::from_utf8_lossy(&out.stdout).trim().to_string();
    let stderr = String::from_utf8_lossy(&out.stderr).trim().to_string();

    if stdout.is_empty() {
        return Err(if stderr.is_empty() {
            format!("引擎没有返回内容（退出码 {:?}）", out.status.code())
        } else {
            stderr
        });
    }

    let value: serde_json::Value = serde_json::from_str(&stdout).map_err(|e| {
        format!(
            "解析引擎输出失败：{e}\n原始输出（前 300 字）：{}",
            stdout.chars().take(300).collect::<String>()
        )
    })?;

    let ok = value.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
    if !ok {
        let msg = value
            .get("error")
            .and_then(|v| v.as_str())
            .unwrap_or("命令执行失败")
            .to_string();
        return Err(msg);
    }
    Ok(value.get("data").cloned().unwrap_or(serde_json::Value::Null))
}

// ==========================================================================
// 给前端的命令
// ==========================================================================
//
// 薄封装：Rust 侧不做业务判断，只「校验参数 → 传下去 → 把 JSON 拿上来」。
// 业务规则留在 Python 引擎里（那边有测试覆盖），避免两套实现分叉。

/// 引擎自检：前端打开 WorkBuddy 页时先调它。
#[tauri::command]
pub async fn wb_sidecar_info(app: AppHandle) -> SidecarInfo {
    let path = resolve(&app);
    // 顺手真正执行一次 --version：既拿到版本号，也验证「执行通道」可用 ——
    // 定位到了但跑不起来（被杀软拦、路径异常）都算未就位，别等用户点按钮才炸。
    let version = match &path {
        Some(p) => {
            let fut = app
                .shell()
                .command(p.to_string_lossy().to_string())
                .arg("--version")
                .output();
            match tokio::time::timeout(std::time::Duration::from_secs(30), fut).await {
                Ok(Ok(o)) if o.status.success() => {
                    let v = String::from_utf8_lossy(&o.stdout).trim().to_string();
                    if v.is_empty() { None } else { Some(v) }
                }
                _ => None,
            }
        }
        None => None,
    };
    SidecarInfo {
        found: path.is_some() && version.is_some(),
        dir: path
            .as_ref()
            .and_then(|p| p.parent())
            .map(|d| d.to_string_lossy().to_string())
            .unwrap_or_default(),
        path: path
            .as_ref()
            .map(|p| p.to_string_lossy().to_string())
            .unwrap_or_default(),
        version,
    }
}

/// 账号总览（当前登录、客户端是否在运行、路径诊断）。
#[tauri::command]
pub async fn wb_state(app: AppHandle) -> Result<serde_json::Value, String> {
    run(&app, &args![Arg::lit("state")], DEFAULT_TIMEOUT_SECS).await
}

/// 一键换号。
#[tauri::command]
pub async fn wb_switch(
    app: AppHandle,
    id: String,
    force: bool,
    restart: Option<bool>,
) -> Result<serde_json::Value, String> {
    let mut a = args![
        Arg::lit("switch"),
        Arg::lit("--id"),
        Arg::token(&id, "账号 id")?,
    ];
    if force {
        a.push(Arg::lit("--force"));
    }
    match restart {
        Some(true) => a.push(Arg::lit("--restart")),
        Some(false) => a.push(Arg::lit("--no-restart")),
        None => {}
    }
    run(&app, &a, NETWORK_TIMEOUT_SECS).await
}

/// 保全当前登录（未入库时建档）。
#[tauri::command]
pub async fn wb_capture(
    app: AppHandle,
    name: Option<String>,
) -> Result<serde_json::Value, String> {
    let mut a = args![Arg::lit("capture")];
    if let Some(n) = name.as_deref() {
        a.push(Arg::lit("--name"));
        a.push(Arg::text(n, "名称")?);
    }
    run(&app, &a, DEFAULT_TIMEOUT_SECS).await
}

/// 会话档案库状态（含归属漂移检测）。
#[tauri::command]
pub async fn wb_sessions(app: AppHandle) -> Result<serde_json::Value, String> {
    run(&app, &args![Arg::lit("sessions")], DEFAULT_TIMEOUT_SECS).await
}

/// 把某账号的会话正式划归当前账号（三件套一起改）。
#[tauri::command]
pub async fn wb_adopt(
    app: AppHandle,
    source_uid: String,
    dry_run: bool,
) -> Result<serde_json::Value, String> {
    let mut a = args![
        Arg::lit("sessions"),
        Arg::lit("--adopt"),
        Arg::token(&source_uid, "源账号 uid")?,
    ];
    if dry_run {
        a.push(Arg::lit("--dry-run"));
    }
    run(&app, &a, NETWORK_TIMEOUT_SECS).await
}

/// 会话复制（真共享）：源账号会话原样保留，目标账号得到独立副本。
#[tauri::command]
pub async fn wb_sessions_copy(
    app: AppHandle,
    source_uid: String,
    target_uid: Option<String>,
    dry_run: bool,
) -> Result<serde_json::Value, String> {
    let mut a = args![
        Arg::lit("sessions"),
        Arg::lit("--copy"),
        Arg::token(&source_uid, "源账号 uid")?,
    ];
    if let Some(t) = target_uid.as_deref() {
        a.push(Arg::lit("--to"));
        a.push(Arg::token(t, "目标账号 uid")?);
    }
    if dry_run {
        a.push(Arg::lit("--dry-run"));
    }
    run(&app, &a, NETWORK_TIMEOUT_SECS).await
}

/// 把云端归属映射修正为与档案库一致。
#[tauri::command]
pub async fn wb_sessions_sync_cloud(
    app: AppHandle,
    dry_run: bool,
) -> Result<serde_json::Value, String> {
    let mut a = args![Arg::lit("sessions"), Arg::lit("--sync-cloud")];
    if dry_run {
        a.push(Arg::lit("--dry-run"));
    }
    run(&app, &a, NETWORK_TIMEOUT_SECS).await
}

/// 会话正文归档：查看 / 收录 / 还原。
#[tauri::command]
pub async fn wb_bodies(
    app: AppHandle,
    archive: bool,
    restore: Option<String>,
) -> Result<serde_json::Value, String> {
    let mut a = args![Arg::lit("bodies")];
    if archive {
        a.push(Arg::lit("--archive"));
    }
    if let Some(sid) = restore.as_deref() {
        a.push(Arg::lit("--restore"));
        a.push(Arg::token(sid, "会话 id")?);
    }
    run(&app, &a, NETWORK_TIMEOUT_SECS).await
}

/// 一键签到（联网）。
#[tauri::command]
pub async fn wb_checkin(
    app: AppHandle,
    id: Option<String>,
) -> Result<serde_json::Value, String> {
    let mut a = args![Arg::lit("checkin")];
    if let Some(i) = id.as_deref() {
        a.push(Arg::lit("--id"));
        a.push(Arg::token(i, "账号 id")?);
    }
    run(&app, &a, NETWORK_TIMEOUT_SECS).await
}

/// 积分与额度（联网）。
#[tauri::command]
pub async fn wb_credits(
    app: AppHandle,
    id: Option<String>,
) -> Result<serde_json::Value, String> {
    let mut a = args![Arg::lit("credits")];
    if let Some(i) = id.as_deref() {
        a.push(Arg::lit("--id"));
        a.push(Arg::token(i, "账号 id")?);
    }
    run(&app, &a, NETWORK_TIMEOUT_SECS).await
}

/// 可用模型清单（联网）。
#[tauri::command]
pub async fn wb_models(app: AppHandle) -> Result<serde_json::Value, String> {
    run(&app, &args![Arg::lit("models")], NETWORK_TIMEOUT_SECS).await
}

/// 只读诊断。
#[tauri::command]
pub async fn wb_diagnose(app: AppHandle) -> Result<serde_json::Value, String> {
    run(&app, &args![Arg::lit("diagnose")], DEFAULT_TIMEOUT_SECS).await
}

/// 结束 WorkBuddy 客户端。
#[tauri::command]
pub async fn wb_kill(app: AppHandle) -> Result<serde_json::Value, String> {
    run(&app, &args![Arg::lit("kill")], DEFAULT_TIMEOUT_SECS).await
}

/// 启动 WorkBuddy 客户端。
#[tauri::command]
pub async fn wb_launch(app: AppHandle) -> Result<serde_json::Value, String> {
    run(&app, &args![Arg::lit("launch")], DEFAULT_TIMEOUT_SECS).await
}

/// 备份列表。
#[tauri::command]
pub async fn wb_backups(app: AppHandle) -> Result<serde_json::Value, String> {
    run(&app, &args![Arg::lit("backups")], DEFAULT_TIMEOUT_SECS).await
}

/// 创建备份。
#[tauri::command]
pub async fn wb_backup(
    app: AppHandle,
    label: Option<String>,
) -> Result<serde_json::Value, String> {
    let mut a = args![Arg::lit("backup")];
    if let Some(l) = label.as_deref() {
        a.push(Arg::lit("--label"));
        a.push(Arg::text(l, "标签")?);
    }
    run(&app, &a, NETWORK_TIMEOUT_SECS).await
}

/// 回滚到指定备份。
#[tauri::command]
pub async fn wb_rollback(app: AppHandle, tag: String) -> Result<serde_json::Value, String> {
    run(
        &app,
        &args![
            Arg::lit("rollback"),
            Arg::token(&tag, "备份标签")?,
            Arg::lit("--yes"),
        ],
        NETWORK_TIMEOUT_SECS,
    )
    .await
}

/// 操作流水。
#[tauri::command]
pub async fn wb_history(
    app: AppHandle,
    limit: Option<u32>,
) -> Result<serde_json::Value, String> {
    run(
        &app,
        &args![
            Arg::lit("history"),
            Arg::lit("--limit"),
            Arg::num(limit.unwrap_or(30).clamp(1, 500)),
        ],
        DEFAULT_TIMEOUT_SECS,
    )
    .await
}

// ---- 分步登录（前端控制节奏）----

/// 开始登录：拿授权链接，立刻返回（不阻塞界面）。
#[tauri::command]
pub async fn wb_login_start(
    app: AppHandle,
    edition: String,
) -> Result<serde_json::Value, String> {
    run(
        &app,
        &args![
            Arg::lit("login"),
            Arg::lit("--start"),
            Arg::lit("--edition"),
            Arg::choice(&edition, &["cn", "intl"], "账号版本")?,
        ],
        NETWORK_TIMEOUT_SECS,
    )
    .await
}

/// 轮询登录结果（单次只等一小段，由前端决定循环节奏）。
#[tauri::command]
pub async fn wb_login_poll(
    app: AppHandle,
    window: Option<f64>,
    name: Option<String>,
) -> Result<serde_json::Value, String> {
    // 窗口限制在 5~60 秒：太短会让前端轮询过密，太长会让界面响应变钝
    let w = window.unwrap_or(25.0).clamp(5.0, 60.0);
    let mut a = args![
        Arg::lit("login"),
        Arg::lit("--poll"),
        Arg::lit("--window"),
        Arg::num(w.round() as u32),
    ];
    if let Some(n) = name.as_deref() {
        a.push(Arg::lit("--name"));
        a.push(Arg::text(n, "名称")?);
    }
    run(&app, &a, LOGIN_TIMEOUT_SECS).await
}

/// 放弃待完成的登录。
#[tauri::command]
pub async fn wb_login_cancel(app: AppHandle) -> Result<serde_json::Value, String> {
    run(
        &app,
        &args![Arg::lit("login"), Arg::lit("--cancel")],
        DEFAULT_TIMEOUT_SECS,
    )
    .await
}
