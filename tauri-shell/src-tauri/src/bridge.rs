//! Python 桥：把 `wbswitch` CLI 包成 Rust 侧可调用的异步函数。
//!
//! 设计原则：**不重新实现任何同步逻辑**。所有业务都在 Python 引擎里，
//! 这里只负责「拼参数 → 跑子进程 → 解析 JSON → 返回给前端」。
//!
//! 这样引擎只有一份实现，Rust 侧不可能和 CLI 行为不一致。

use serde::Deserialize;
use serde_json::Value;
use std::path::PathBuf;
use std::process::Command;

/// 调用结果：成功时带解析好的 JSON，失败时带错误文本。
#[derive(Debug, Deserialize, serde::Serialize)]
pub struct BridgeResult {
    pub ok: bool,
    pub data: Option<Value>,
    pub error: Option<String>,
    pub raw_stdout: String,
    pub raw_stderr: String,
    pub exit_code: i32,
}

/// 找到可用的 Python 解释器。
///
/// 顺序：WBSWITCH_PYTHON 环境变量 → python → py -3
pub fn python_exe() -> String {
    if let Ok(p) = std::env::var("WBSWITCH_PYTHON") {
        if !p.trim().is_empty() {
            return p;
        }
    }
    "python".to_string()
}

/// Python 引擎所在目录（`wbswitch/` 的父目录）。
///
/// 打包后会随应用一起分发；开发时指向仓库根目录。
pub fn engine_root() -> PathBuf {
    if let Ok(p) = std::env::var("WBSWITCH_ROOT") {
        if !p.trim().is_empty() {
            return PathBuf::from(p);
        }
    }
    // 相对当前可执行文件往上找一级（src-tauri/target/debug → 仓库根）
    let mut dir = std::env::current_exe().unwrap_or_else(|_| PathBuf::from("."));
    for _ in 0..6 {
        dir.pop();
        if dir.join("wbswitch").join("cli.py").exists() {
            return dir;
        }
    }
    PathBuf::from(".")
}

/// 执行一次 CLI 调用。
///
/// `args` 是子命令参数，例如 `["--json", "state"]`。
pub fn run_cli(args: &[&str]) -> BridgeResult {
    let root = engine_root();
    let mut cmd = Command::new(python_exe());
    cmd.arg("-m")
        .arg("wbswitch.cli")
        .args(args)
        .current_dir(&root);

    // 让 Python 用 UTF-8 输出，避免 Windows 上中文乱码
    cmd.env("PYTHONIOENCODING", "utf-8");
    cmd.env("PYTHONUTF8", "1");

    match cmd.output() {
        Ok(out) => {
            let stdout = String::from_utf8_lossy(&out.stdout).to_string();
            let stderr = String::from_utf8_lossy(&out.stderr).to_string();
            let code = out.status.code().unwrap_or(-1);

            // 尝试把 stdout 整体当 JSON 解析；失败就退化为纯文本
            let data = serde_json::from_str::<Value>(stdout.trim()).ok();
            let ok = code == 0;

            BridgeResult {
                ok,
                data,
                error: if ok {
                    None
                } else {
                    Some(if stderr.trim().is_empty() {
                        stdout.trim().to_string()
                    } else {
                        stderr.trim().to_string()
                    })
                },
                raw_stdout: stdout,
                raw_stderr: stderr,
                exit_code: code,
            }
        }
        Err(e) => BridgeResult {
            ok: false,
            data: None,
            error: Some(format!("无法启动 Python（{}）：{e}", python_exe())),
            raw_stdout: String::new(),
            raw_stderr: String::new(),
            exit_code: -1,
        },
    }
}
