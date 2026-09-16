//! WorkBuddy Switch — Tauri 外壳入口。
//!
//! 所有命令都只是 `wbswitch.cli` 的薄封装（见 `bridge.rs`）。
//! 业务逻辑一律在 Python 引擎里，本层不做任何数据改写。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod bridge;

use bridge::BridgeResult;
use tauri::Manager;

// ---------------------------------------------------------------------------
// 只读命令
// ---------------------------------------------------------------------------

/// 完整状态：账号列表 / 当前登录 / 客户端路径 / 设置。
#[tauri::command]
async fn get_state() -> BridgeResult {
    tauri::async_runtime::spawn_blocking(|| bridge::run_cli(&["state", "--json"]))
        .await
        .unwrap_or_else(|e| BridgeResult {
            ok: false,
            data: None,
            error: Some(format!("任务失败：{e}")),
            raw_stdout: String::new(),
            raw_stderr: String::new(),
            exit_code: -1,
        })
}

/// 路径 + 各 uid 计数 + 完整性（只读诊断）。
#[tauri::command]
async fn diagnose() -> BridgeResult {
    tauri::async_runtime::spawn_blocking(|| bridge::run_cli(&["diagnose", "--json"]))
        .await
        .unwrap()
}

/// 备份列表。
#[tauri::command]
async fn list_backups() -> BridgeResult {
    tauri::async_runtime::spawn_blocking(|| bridge::run_cli(&["backups", "--json"]))
        .await
        .unwrap()
}

/// 操作流水。
#[tauri::command]
async fn history() -> BridgeResult {
    tauri::async_runtime::spawn_blocking(|| bridge::run_cli(&["history", "--json"]))
        .await
        .unwrap()
}

// ---------------------------------------------------------------------------
// 写操作
// ---------------------------------------------------------------------------

/// 把当前登录的账号存进档案库。
#[tauri::command]
async fn capture(name: Option<String>) -> BridgeResult {
    tauri::async_runtime::spawn_blocking(move || {
        let mut args = vec!["capture", "--json"];
        if let Some(n) = name.as_deref() {
            args.push("--name");
            args.push(n);
        }
        bridge::run_cli(&args)
    })
    .await
    .unwrap()
}

/// 一键换号。
#[tauri::command]
async fn switch(id: String, dry_run: Option<bool>, no_restart: Option<bool>) -> BridgeResult {
    tauri::async_runtime::spawn_blocking(move || {
        let mut args = vec!["switch", "--id", id.as_str(), "--force", "--json"];
        if dry_run.unwrap_or(false) {
            args.push("--dry-run");
        }
        if no_restart.unwrap_or(false) {
            args.push("--no-restart");
        }
        bridge::run_cli(&args)
    })
    .await
    .unwrap()
}

/// 把某个旧账号的数据同步到当前登录的账号。
#[tauri::command]
async fn sync(id: String, dry_run: Option<bool>) -> BridgeResult {
    tauri::async_runtime::spawn_blocking(move || {
        let mut args = vec!["sync", "--id", id.as_str(), "--json"];
        if dry_run.unwrap_or(false) {
            args.push("--dry-run");
        }
        bridge::run_cli(&args)
    })
    .await
    .unwrap()
}

/// 重命名档案。
#[tauri::command]
async fn rename(id: String, name: String) -> BridgeResult {
    tauri::async_runtime::spawn_blocking(move || {
        bridge::run_cli(&["rename", "--id", &id, "--name", &name, "--json"])
    })
    .await
    .unwrap()
}

/// 删除档案。
#[tauri::command]
async fn delete_account(id: String) -> BridgeResult {
    tauri::async_runtime::spawn_blocking(move || {
        bridge::run_cli(&["delete", "--id", &id, "--yes", "--json"])
    })
    .await
    .unwrap()
}

/// 打一份全量备份。
#[tauri::command]
async fn backup(label: Option<String>) -> BridgeResult {
    tauri::async_runtime::spawn_blocking(move || {
        let mut args = vec!["backup", "--json"];
        if let Some(l) = label.as_deref() {
            args.push("--label");
            args.push(l);
        }
        bridge::run_cli(&args)
    })
    .await
    .unwrap()
}

/// 回滚到某个备份。
#[tauri::command]
async fn rollback(tag: String) -> BridgeResult {
    tauri::async_runtime::spawn_blocking(move || {
        bridge::run_cli(&["rollback", &tag, "--yes", "--json"])
    })
    .await
    .unwrap()
}

// ---------------------------------------------------------------------------
// 进程控制
// ---------------------------------------------------------------------------

#[tauri::command]
async fn kill_client() -> BridgeResult {
    tauri::async_runtime::spawn_blocking(|| bridge::run_cli(&["kill", "--json"]))
        .await
        .unwrap()
}

#[tauri::command]
async fn launch_client() -> BridgeResult {
    tauri::async_runtime::spawn_blocking(|| bridge::run_cli(&["launch", "--json"]))
        .await
        .unwrap()
}

// ---------------------------------------------------------------------------
// 环境自检
// ---------------------------------------------------------------------------

#[tauri::command]
fn env_info() -> serde_json::Value {
    serde_json::json!({
        "python": bridge::python_exe(),
        "engine_root": bridge::engine_root().to_string_lossy(),
        "platform": std::env::consts::OS,
    })
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            get_state,
            diagnose,
            list_backups,
            history,
            capture,
            switch,
            sync,
            rename,
            delete_account,
            backup,
            rollback,
            kill_client,
            launch_client,
            env_info,
        ])
        .setup(|app| {
            // 托盘：左键显示 / 隐藏主窗
            let handle = app.handle().clone();
            if let Some(tray) = app.tray_by_id("main") {
                tray.on_tray_icon_event(move |_tray, event| {
                    if let tauri::tray::TrayIconEvent::Click { button, .. } = event {
                        if button == tauri::tray::MouseButton::Left {
                            if let Some(win) = handle.get_webview_window("main") {
                                let visible = win.is_visible().unwrap_or(false);
                                if visible {
                                    let _ = win.hide();
                                } else {
                                    let _ = win.show();
                                    let _ = win.set_focus();
                                }
                            }
                        }
                    }
                });
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("启动 Tauri 应用失败");
}
