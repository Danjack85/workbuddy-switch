//! 多开：为每个账号启动一个数据目录独立的 ZCode 实例，与主实例并存。
//!
//! ZCode（Electron）暴露了两个覆盖点，正好够用来隔离一个实例：
//! - `ZCODE_DATA_BASE_DIR`：应用数据根目录（`<base>/.zcode/v2/credentials.json` 等都在其下）；
//! - `ZCODE_DESKTOP_USER_DATA_DIR`：Electron userData（单实例锁、session、rum-electron-store 都在其下）。
//!
//! 实例目录结构（`<store>/instances/<account_id>/`）：
//! ```text
//! .zcode/v2/            每实例真实目录：credentials/config/setting/telemetry + 应用自建文件
//! .zcode/<其他条目>      与主数据目录共享：目录走 junction（Windows 免管理员），文件启动时复制
//! desktop/              Electron userData（独立单实例锁）
//! instance.json         启动记录（pid），用于快速判断实例是否在运行
//! ```

use crate::i18n::{tr, trf};
use crate::store::{self, atomic_write, is_logged_in, no_window, Account, Paths};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use uuid::Uuid;

const RECORD_FILE: &str = "instance.json";
/// Chromium 在 userData 下的单实例锁文件名（Windows 为 lockfile，POSIX 为 SingletonLock）。
pub const LOCK_FILE_WIN: &str = "lockfile";
pub const LOCK_FILE_UNIX: &str = "SingletonLock";

/// 当前平台的单实例锁文件名。
pub const fn lock_name() -> &'static str {
    if cfg!(windows) {
        LOCK_FILE_WIN
    } else {
        LOCK_FILE_UNIX
    }
}

/// userData 下的单实例锁是否被占用（共享实现，主实例判定也用这个）。
/// Windows：锁文件被 Chromium 独占持有，能独占打开 = 没有进程在用。
pub(crate) fn lock_file_held(p: &Path) -> Option<bool> {
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        match fs::OpenOptions::new().read(true).write(true).share_mode(0).open(p) {
            Ok(_) => Some(false),
            Err(e) => match e.raw_os_error() {
                Some(2) | Some(3) => Some(false), // 文件/路径不存在
                _ => Some(true),
            },
        }
    }
    #[cfg(not(windows))]
    {
        if !p.exists() && fs::symlink_metadata(p).is_err() {
            return Some(false);
        }
        // POSIX 下 SingletonLock 是 symlink，形如 <host>-<pid>
        if let Ok(target) = fs::read_link(p) {
            let s = target.to_string_lossy().to_string();
            if let Some(pid) = s.rsplit('-').next().and_then(|x| x.parse::<u32>().ok()) {
                return Some(pid_alive(pid));
            }
        }
        None
    }
}

pub fn instances_root(paths: &Paths) -> PathBuf {
    paths.store_dir().join("instances")
}

#[derive(Clone, Debug)]
pub struct InstancePaths {
    pub id: String,
    pub root: PathBuf,
}

impl InstancePaths {
    pub fn new(paths: &Paths, id: &str) -> InstancePaths {
        InstancePaths { id: id.to_string(), root: instances_root(paths).join(id) }
    }
    pub fn zcode(&self) -> PathBuf { self.root.join(".zcode") }
    pub fn v2(&self) -> PathBuf { self.zcode().join("v2") }
    pub fn desktop(&self) -> PathBuf { self.root.join("desktop") }
    pub fn rum_store(&self) -> PathBuf { self.desktop().join("rum-electron-store") }
    pub fn record(&self) -> PathBuf { self.root.join(RECORD_FILE) }
    fn lock_candidate(&self) -> PathBuf {
        #[cfg(windows)]
        { self.desktop().join(LOCK_FILE_WIN) }
        #[cfg(not(windows))]
        { self.desktop().join(LOCK_FILE_UNIX) }
    }
    pub fn creds(&self) -> PathBuf { self.v2().join("credentials.json") }
    pub fn config(&self) -> PathBuf { self.v2().join("config.json") }
    pub fn setting(&self) -> PathBuf { self.v2().join("setting.json") }
    pub fn telemetry(&self) -> PathBuf { self.v2().join("telemetry-state.json") }
}

fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
}

pub fn list_instance_ids(paths: &Paths) -> Vec<String> {
    let dir = instances_root(paths);
    let Ok(rd) = fs::read_dir(&dir) else { return vec![] };
    let mut out: Vec<String> = rd
        .flatten()
        .filter(|e| e.path().is_dir())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .filter(|n| valid_id(n))
        .collect();
    out.sort();
    out
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct InstanceRecord {
    #[serde(default)]
    pub pid: u32,
    #[serde(default)]
    pub started_at: u64,
    #[serde(default)]
    pub exe: String,
}

fn read_record(inst: &InstancePaths) -> Option<InstanceRecord> {
    let raw = fs::read_to_string(inst.record()).ok()?;
    serde_json::from_str(&raw).ok()
}

fn write_record(inst: &InstancePaths, rec: &InstanceRecord) {
    let body = serde_json::to_string_pretty(rec).unwrap_or_default() + "\n";
    if let Err(e) = atomic_write(&inst.record(), &body) {
        eprintln!("写入多开记录失败(忽略): {e}");
    }
}

fn read_json(path: &Path) -> Option<Value> {
    let raw = fs::read_to_string(path).ok()?;
    serde_json::from_str(&raw).ok()
}

fn write_json(path: &Path, v: &Value) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| trf("err.mkdir", &[("e", &e.to_string())]))?;
    }
    let body = serde_json::to_string_pretty(v).unwrap_or_default() + "\n";
    atomic_write(path, &body)
}

// ---------------- 进程 / 锁检测 ----------------

#[cfg(windows)]
fn pid_alive(pid: u32) -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, WAIT_TIMEOUT};
    use windows_sys::Win32::System::Threading::{OpenProcess, WaitForSingleObject, PROCESS_SYNCHRONIZE};
    unsafe {
        let h = OpenProcess(PROCESS_SYNCHRONIZE, 0, pid);
        if h == 0 {
            return false;
        }
        let r = WaitForSingleObject(h, 0);
        let _ = CloseHandle(h);
        r == WAIT_TIMEOUT
    }
}

#[cfg(windows)]
fn pid_image(pid: u32) -> Option<String> {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Threading::{
        OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if h == 0 {
            return None;
        }
        let mut buf = [0u16; 1024];
        let mut len = buf.len() as u32;
        let ok = QueryFullProcessImageNameW(h, PROCESS_NAME_WIN32, buf.as_mut_ptr(), &mut len);
        let _ = CloseHandle(h);
        (ok != 0).then(|| String::from_utf16_lossy(&buf[..len as usize]))
    }
}

#[cfg(not(windows))]
fn pid_alive(pid: u32) -> bool {
    std::process::Command::new("kill")
        .args(["-0", &pid.to_string()])
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

#[cfg(windows)]
fn pid_is_zcode(pid: u32) -> bool {
    pid_image(pid)
        .map(|p| {
            Path::new(&p)
                .file_name()
                .map(|n| n.to_string_lossy().to_ascii_lowercase().starts_with("zcode"))
                .unwrap_or(false)
        })
        .unwrap_or(false)
}

#[cfg(not(windows))]
fn pid_is_zcode(pid: u32) -> bool {
    pid_alive(pid)
}

/// 进程创建时间与启动记录是否吻合（防 PID 复用误杀）。无法判定时按吻合处理。
#[cfg(windows)]
fn pid_created_near(pid: u32, started_at_ms: u64) -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, FILETIME};
    use windows_sys::Win32::System::Threading::{
        GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    if started_at_ms == 0 {
        return true;
    }
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if h == 0 {
            return false;
        }
        let mut creation = FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 };
        let (mut exit, mut kernel, mut user) = (creation, creation, creation);
        let ok = GetProcessTimes(h, &mut creation, &mut exit, &mut kernel, &mut user);
        let _ = CloseHandle(h);
        if ok == 0 {
            return true;
        }
        let ft = ((creation.dwHighDateTime as u64) << 32) | creation.dwLowDateTime as u64;
        let created_ms = ft / 10_000;
        // FILETIME(1601) → UNIX 毫秒
        const EPOCH_DIFF_MS: u64 = 11_644_473_600_000;
        if created_ms < EPOCH_DIFF_MS {
            return true;
        }
        let created_unix_ms = created_ms - EPOCH_DIFF_MS;
        created_unix_ms.abs_diff(started_at_ms) < 60_000
    }
}

#[cfg(not(windows))]
fn pid_created_near(_pid: u32, _started_at_ms: u64) -> bool {
    true
}

fn record_pid_alive(rec: &InstanceRecord) -> bool {
    rec.pid != 0 && pid_alive(rec.pid) && pid_is_zcode(rec.pid)
}

/// 实例是否在运行：以 userData 单实例锁为准，锁不可判定时回落到启动记录。
pub fn instance_running(paths: &Paths, id: &str) -> bool {
    if store::in_sandbox() {
        return false;
    }
    let inst = InstancePaths::new(paths, id);
    if !inst.root.exists() {
        return false;
    }
    if lock_file_held(&inst.lock_candidate()) == Some(true) {
        return true;
    }
    read_record(&inst).map(|r| record_pid_alive(&r)).unwrap_or(false)
}

pub fn running_ids(paths: &Paths) -> Vec<String> {
    list_instance_ids(paths).into_iter().filter(|id| instance_running(paths, id)).collect()
}

// ---------------- 进程枚举（慢路径，仅关闭/兜底时使用） ----------------

#[derive(Clone, Debug, Default)]
struct ProcInfo {
    pid: u32,
    ppid: u32,
    cmd: String,
}

#[cfg(windows)]
fn list_zcode_procs() -> Result<Vec<ProcInfo>, String> {
    let script = "Get-CimInstance Win32_Process -Filter \"Name='ZCode.exe'\" | \
                  Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress";
    let out = no_window("powershell")
        .args(["-NoProfile", "-NonInteractive", "-Command", script])
        .output()
        .map_err(|e| trf("err.multi.ps", &[("e", &e.to_string())]))?;
    let raw = String::from_utf8_lossy(&out.stdout);
    parse_proc_json(raw.trim())
}

#[cfg(windows)]
fn parse_proc_json(raw: &str) -> Result<Vec<ProcInfo>, String> {
    if raw.is_empty() {
        return Ok(vec![]);
    }
    let v: Value = serde_json::from_str(raw).map_err(|e| trf("err.multi.ps_json", &[("e", &e.to_string())]))?;
    let items = match v {
        Value::Array(a) => a,
        obj @ Value::Object(_) => vec![obj],
        _ => vec![],
    };
    Ok(items
        .into_iter()
        .map(|it| ProcInfo {
            pid: it.get("ProcessId").and_then(|x| x.as_u64()).unwrap_or(0) as u32,
            ppid: it.get("ParentProcessId").and_then(|x| x.as_u64()).unwrap_or(0) as u32,
            cmd: it.get("CommandLine").and_then(|x| x.as_str()).unwrap_or("").to_string(),
        })
        .filter(|p| p.pid != 0)
        .collect())
}

#[cfg(not(windows))]
fn list_zcode_procs() -> Result<Vec<ProcInfo>, String> {
    let out = no_window("ps")
        .args(["-axo", "pid=,ppid=,command="])
        .output()
        .map_err(|e| trf("err.multi.ps", &[("e", &e.to_string())]))?;
    let raw = String::from_utf8_lossy(&out.stdout);
    Ok(raw
        .lines()
        .filter_map(|l| {
            let l = l.trim_start();
            let mut it = l.splitn(3, ' ');
            let pid = it.next()?.trim().parse::<u32>().ok()?;
            let ppid = it.next()?.trim().parse::<u32>().ok()?;
            let cmd = it.next().unwrap_or("").trim().to_string();
            let name = cmd.split('/').next_back().unwrap_or("");
            (name.starts_with("zcode") || name.starts_with("ZCode") || cmd.to_ascii_lowercase().contains("zcode.exe"))
                .then_some(ProcInfo { pid, ppid, cmd })
        })
        .collect())
}

fn norm_win_path(s: &str) -> String {
    s.replace('/', "\\").to_ascii_lowercase()
}

/// 某进程的命令线是否直接指向该实例（浏览器主进程与各子进程都带 `--user-data-dir=<实例>desktop`；
/// crashpad 等还会带实例数据目录 `--database=<实例>\.zcode\...`，应用自更新重启后仍能据此定位）。
fn cmd_matches_instance(cmd: &str, needles: &[String]) -> bool {
    let c = norm_win_path(cmd);
    needles.iter().any(|n| !n.is_empty() && c.contains(n.as_str()))
}

/// 某进程自身或父链上是否出现过该实例标记（覆盖不带路径参数的 app-server 等孙进程）。
fn chain_owned(start: &ProcInfo, by_pid: &std::collections::HashMap<u32, &ProcInfo>, needles: &[String]) -> bool {
    let mut cur = start;
    for _ in 0..32 {
        if cmd_matches_instance(&cur.cmd, needles) {
            return true;
        }
        match by_pid.get(&cur.ppid) {
            Some(parent) if parent.pid != cur.pid => cur = parent,
            _ => break,
        }
    }
    false
}

/// 从种子进程向上找到各自所在 ZCode 进程树的顶端（浏览器进程），供 taskkill /T 整树关闭。
/// 浏览器进程自身的命令线在自更新重启后可能不再带实例参数，向上取顶仍然命中。
fn tops_of(procs: &[ProcInfo], seeds: Vec<u32>) -> Vec<u32> {
    let by_pid: std::collections::HashMap<u32, &ProcInfo> = procs.iter().map(|p| (p.pid, p)).collect();
    let mut tops: Vec<u32> = vec![];
    for pid in seeds {
        let mut top = pid;
        for _ in 0..32 {
            let Some(p) = by_pid.get(&top) else { break };
            match by_pid.get(&p.ppid) {
                Some(parent) if parent.pid != p.pid => top = parent.pid,
                _ => break,
            }
        }
        tops.push(top);
    }
    tops.sort_unstable();
    tops.dedup();
    tops
}

/// 实例进程链的顶端集合。慢路径。
fn scan_instance_pids(paths: &Paths, id: &str) -> Result<Vec<u32>, String> {
    if !valid_id(id) {
        return Err(tr("err.store.bad_id"));
    }
    let inst = InstancePaths::new(paths, id);
    let needles = vec![
        norm_win_path(&inst.desktop().to_string_lossy()),
        norm_win_path(&inst.zcode().to_string_lossy()),
    ];
    let procs = list_zcode_procs()?;
    let by_pid: std::collections::HashMap<u32, &ProcInfo> = procs.iter().map(|p| (p.pid, p)).collect();
    let seeds: Vec<u32> = procs
        .iter()
        .filter(|p| chain_owned(p, &by_pid, &needles))
        .map(|p| p.pid)
        .collect();
    Ok(tops_of(&procs, seeds))
}

/// 关闭主实例（默认 userData）的进程树：多开实例不受影响。
/// 没有多开实例时直接整体关闭（旧行为，无需枚举）；有多开时必须按目录区分，枚举失败则报错。
pub(crate) fn kill_main_processes(paths: &Paths) -> Result<(), String> {
    if list_instance_ids(paths).is_empty() {
        #[cfg(windows)]
        let _ = no_window("taskkill").args(["/F", "/IM", "ZCode.exe"]).output();
        #[cfg(not(windows))]
        {
            for name in ["zcode", "ZCode"] {
                let _ = no_window("pkill").args(["-x", name]).output();
            }
        }
        return Ok(());
    }
    let procs = list_zcode_procs()?;
    if procs.is_empty() {
        return Ok(());
    }
    let inst_needles: Vec<String> = list_instance_ids(paths)
        .into_iter()
        .map(|id| norm_win_path(&InstancePaths::new(paths, &id).desktop().to_string_lossy()))
        .collect();
    let by_pid: std::collections::HashMap<u32, &ProcInfo> = procs.iter().map(|p| (p.pid, p)).collect();
    // 主实例 = 命令线链上带过 user-data-dir 但不属于任何多开实例，或完全没有 user-data-dir 的浏览器进程
    let is_main = |p: &ProcInfo| {
        let mut cur = p;
        for _ in 0..32 {
            let cmd = norm_win_path(&cur.cmd);
            if cmd.contains("--user-data-dir") {
                return !inst_needles.iter().any(|n| cmd.contains(n.as_str()));
            }
            match by_pid.get(&cur.ppid) {
                Some(parent) if parent.pid != cur.pid => cur = parent,
                _ => break,
            }
        }
        true
    };
    let seeds: Vec<u32> = procs.iter().filter(|p| is_main(p)).map(|p| p.pid).collect();
    let roots = tops_of(&procs, seeds);
    if roots.is_empty() {
        return Ok(());
    }
    for pid in roots {
        taskkill_tree(pid);
    }
    Ok(())
}

fn taskkill_tree(pid: u32) {
    let _ = no_window("taskkill")
        .args(["/F", "/T", "/PID", &pid.to_string()])
        .output();
}

/// 关闭某个多开实例（连同其子进程）。返回是否确实关闭了一个运行中的实例。
pub fn close_instance(paths: &Paths, id: &str) -> Result<bool, String> {
    if !valid_id(id) {
        return Err(tr("err.store.bad_id"));
    }
    if store::in_sandbox() {
        return Ok(true);
    }
    let inst = InstancePaths::new(paths, id);
    if !inst.root.exists() || !instance_running(paths, id) {
        return Ok(false);
    }
    let mut pids: Vec<u32> = read_record(&inst)
        .filter(|r| r.pid != 0 && pid_is_zcode(r.pid) && pid_created_near(r.pid, r.started_at))
        .map(|r| r.pid)
        .into_iter()
        .collect();
    if pids.is_empty() {
        pids = scan_instance_pids(paths, id)?;
    }
    if pids.is_empty() {
        return Err(tr("err.multi.no_proc"));
    }
    for pid in &pids {
        taskkill_tree(*pid);
    }
    let deadline = Instant::now() + Duration::from_secs(12);
    while Instant::now() < deadline {
        if !instance_running(paths, id) {
            let _ = fs::remove_file(inst.record());
            return Ok(true);
        }
        std::thread::sleep(Duration::from_millis(350));
    }
    // 记录里的 pid 可能已过期（应用自更新重启）：退回到全量扫描再杀一次
    if let Ok(found) = scan_instance_pids(paths, id) {
        if !found.is_empty() {
            for pid in &found {
                taskkill_tree(*pid);
            }
            let deadline = Instant::now() + Duration::from_secs(8);
            while Instant::now() < deadline {
                if !instance_running(paths, id) {
                    let _ = fs::remove_file(inst.record());
                    return Ok(true);
                }
                std::thread::sleep(Duration::from_millis(350));
            }
        }
    }
    Err(tr("err.multi.kill_timeout"))
}

/// 删除账号时清理其多开数据（先关实例，再删目录）。尽力而为，失败只告警。
pub fn cleanup_instance(paths: &Paths, id: &str) {
    let inst = InstancePaths::new(paths, id);
    if !inst.root.exists() {
        return;
    }
    if instance_running(paths, id) {
        if let Err(e) = close_instance(paths, id) {
            eprintln!("关闭多开实例失败(继续清理): {e}");
        }
    }
    if inst.root.exists() {
        if let Err(e) = fs::remove_dir_all(&inst.root) {
            eprintln!("删除多开数据目录失败(忽略): {e}");
        }
    }
}

// ---------------- 共享链接 ----------------

/// 实例 `.zcode` 下与主数据目录共享的条目：目录用 junction/符号链接，文件每次启动复制。
/// 失败只记入 warnings，不阻断启动。
fn ensure_shared_root(paths: &Paths, inst: &InstancePaths, warnings: &mut Vec<String>) {
    let main_root = paths.home.join(".zcode");
    let inst_root = inst.zcode();
    if let Err(e) = fs::create_dir_all(&inst_root) {
        warnings.push(format!(".zcode: {e}"));
        return;
    }
    let Ok(rd) = fs::read_dir(&main_root) else { return };
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        if name == "v2" || name == "mimosa-debug.log" {
            continue;
        }
        let src = e.path();
        let dst = inst_root.join(&name);
        let Ok(meta) = fs::metadata(&src) else { continue };
        if meta.is_dir() {
            // symlink_metadata：链接已存在（含暂时断链）就不重建
            if fs::symlink_metadata(&dst).is_ok() {
                continue;
            }
            if let Err(err) = link_dir(&src, &dst) {
                warnings.push(format!("{name}: {err}"));
            }
        } else if meta.is_file() {
            if let Err(err) = fs::copy(&src, &dst) {
                warnings.push(format!("{name}: {err}"));
            }
        }
    }
}

#[cfg(windows)]
fn link_dir(target: &Path, link: &Path) -> Result<(), String> {
    let out = no_window("cmd")
        .args(["/C", "mklink", "/J"])
        .arg(link)
        .arg(target)
        .output()
        .map_err(|e| e.to_string())?;
    if out.status.success() {
        Ok(())
    } else {
        Err(String::from_utf8_lossy(&out.stdout).trim().to_string())
    }
}

#[cfg(not(windows))]
fn link_dir(target: &Path, link: &Path) -> Result<(), String> {
    std::os::unix::fs::symlink(target, link).map_err(|e| e.to_string())
}

// ---------------- 供应实例数据 ----------------

#[derive(Serialize, Clone, Debug, Default)]
pub struct OpenInstanceResult {
    pub name: String,
    pub instance_dir: String,
    pub launched: bool,
    pub already_running: bool,
    pub restarted: bool,
    pub synced: bool,
    pub config_synth: bool,
    pub warnings: Vec<String>,
}

struct Provisioned {
    synced: bool,
    config_synth: bool,
}

fn cred_plain(creds: &Value, key: &str, home: &Path) -> Option<String> {
    store::cred_plain(creds, key, home)
}

/// 实例里的登录态是否就是该账号（哈希或身份任一匹配）。
fn instance_login_matches(paths: &Paths, acc: &Account, creds: &Value) -> bool {
    if !is_logged_in(creds) {
        return false;
    }
    if store::canonical_hash(creds) == acc.hash {
        return true;
    }
    let a = crate::zcrypto::account_identity(creds, &paths.home);
    let b = crate::zcrypto::account_identity(&acc.credentials, &paths.home);
    let has = |i: &crate::zcrypto::Identity| {
        i.user_id.as_deref().is_some_and(|s| !s.is_empty())
            || i.email.as_deref().is_some_and(|s| !s.is_empty())
            || i.username.as_deref().is_some_and(|s| !s.is_empty())
    };
    has(&a) && has(&b) && crate::store::identity_matches_pub(&a, &b)
}

/// 基于主实例 config + 该账号凭据，拼一份可用的 config.json（账号无快照时的兜底）。
fn synth_config(paths: &Paths, creds: &Value) -> Value {
    let mut out = read_json(&paths.live_config()).unwrap_or_else(|| json!({}));
    if !out.is_object() {
        out = json!({});
    }
    let provider = cred_plain(creds, "oauth:active_provider", &paths.home)
        .filter(|p| p == "bigmodel" || p == "zai");
    let Some(provider) = provider else { return out };
    let jwt = cred_plain(creds, "zcodejwttoken", &paths.home)
        .filter(|j| !j.trim().is_empty());
    let Some(jwt) = jwt else { return out };
    let at = cred_plain(creds, &format!("oauth:{provider}:access_token"), &paths.home).unwrap_or_default();
    let at = if provider == "zai" && !at.is_empty() {
        crate::oauth::resolve_zai_business_token(&at).unwrap_or(at)
    } else {
        at
    };
    let fresh = crate::oauth::assemble_config(&provider, &jwt, &at);
    store::merge_fresh_providers(&mut out, &fresh, true);
    out
}

fn ensure_arms_uid(paths: &Paths, acc: &Account, inst: &InstancePaths) -> Result<(), String> {
    let uid = store::ensure_virtual_arms_uid(paths, &acc.id)?;
    let dir = inst.rum_store();
    fs::create_dir_all(&dir).map_err(|e| trf("err.mkdir", &[("e", &e.to_string())]))?;
    let f = dir.join(store::ARMS_DEFAULT_STORE_FILE);
    let mut v = read_json(&f).unwrap_or_else(|| json!({}));
    if !v.is_object() {
        v = json!({});
    }
    match v.get("_arms_uid").and_then(|u| u.as_str()) {
        // 实例里已由应用写入身份（用户在该实例里登录过）：保留不回写，避免和活动身份打架
        Some(cur) if !cur.trim().is_empty() => return Ok(()),
        _ => {}
    }
    let obj = v.as_object_mut().unwrap();
    obj.insert("_arms_uid".into(), json!(uid));
    obj.remove("_arms_session");
    write_json(&f, &v)
}

fn provision(paths: &Paths, acc: &Account, inst: &InstancePaths, warnings: &mut Vec<String>) -> Result<Provisioned, String> {
    fs::create_dir_all(inst.v2()).map_err(|e| trf("err.mkdir", &[("e", &e.to_string())]))?;
    fs::create_dir_all(inst.desktop()).map_err(|e| trf("err.mkdir", &[("e", &e.to_string())]))?;
    ensure_shared_root(paths, inst, warnings);

    // 1) 登录态：实例内若已是该账号（token 可能已刷新），优先采信并回写账号库
    let mut synced = false;
    let inst_creds = read_json(&inst.creds());
    let (creds, inst_login_ok) = match inst_creds {
        Some(v) if instance_login_matches(paths, acc, &v) => {
            if store::canonical_hash(&v) != acc.hash {
                let mut upd = acc.clone();
                upd.credentials = v.clone();
                upd.hash = store::canonical_hash(&v);
                if let Some(cfg) = read_json(&inst.config()) {
                    upd.config = Some(cfg);
                }
                upd.updated_at = store::now_ts();
                if let Err(e) = store::save_account(paths, &upd) {
                    warnings.push(e);
                } else {
                    synced = true;
                }
            }
            (v, true)
        }
        _ => {
            let mut c = acc.credentials.clone();
            c = store::inject_relay_pass_hash(&c, store::current_relay_pass(paths).as_ref());
            store::backfill_relay_pass_hash(paths, &acc.id, &c);
            (c, false)
        }
    };
    write_json(&inst.creds(), &creds)?;

    // 2) config：账号有快照就用快照；否则用主 config 打底 + 该账号凭据补写。
    //    实例里已有该账号的 config 时保留（用户可能在实例内改过 provider）
    let config_synth = acc.config.is_none();
    let keep_inst_cfg = inst_login_ok && inst.config().exists();
    if !keep_inst_cfg {
        let cfg = match &acc.config {
            Some(c) => c.clone(),
            None => synth_config(paths, &creds),
        };
        write_json(&inst.config(), &cfg)?;
    }

    // 3) setting.json：沿用主实例设置，仅对齐 provider family domain
    if let Some(mut s) = read_json(&paths.live_setting()) {
        if s.is_object() {
            if let Some(provider) = cred_plain(&creds, "oauth:active_provider", &paths.home)
                .filter(|p| p == "bigmodel" || p == "zai")
            {
                let now_ms = chrono::Local::now().timestamp_millis();
                s["providerFamilyDomain"] = json!(provider);
                s["providerFamilyDomainUpdatedAt"] = json!(now_ms);
            }
            write_json(&inst.setting(), &s)?;
        }
    }

    // 4) telemetry-state.json：设备 mid 换成该账号的虚拟设备
    let mid = store::ensure_virtual_device_mid(paths, &acc.id)?;
    let mut t = read_json(&paths.live_telemetry()).unwrap_or_else(|| json!({}));
    if !t.is_object() {
        t = json!({});
    }
    t["deviceMid"] = json!(mid);
    write_json(&inst.telemetry(), &t)?;

    // 5) provider_config.json（用户自定义 provider 定义）与 onboarding-record.json（跳过引导）
    let v2_main = paths.home.join(".zcode").join("v2");
    for name in ["provider_config.json", "onboarding-record.json"] {
        let src = v2_main.join(name);
        let dst = inst.v2().join(name);
        if src.is_file() && !dst.exists() {
            if let Err(e) = fs::copy(&src, &dst) {
                warnings.push(format!("{name}: {e}"));
            }
        }
    }

    // 6) 活动身份（rum-electron-store 里的 _arms_uid）
    if let Err(e) = ensure_arms_uid(paths, acc, inst) {
        warnings.push(e);
    }

    Ok(Provisioned { synced, config_synth })
}

fn launch_instance_process(paths: &Paths, inst: &InstancePaths) -> Result<u32, String> {
    if store::in_sandbox() {
        return Ok(0);
    }
    let (p, ok) = store::effective_zcode_path(paths);
    if !ok {
        return Err(trf("err.zcode.path_invalid_hint", &[("p", &p)]));
    }
    let exe = PathBuf::from(&p);
    if !exe.exists() {
        return Err(trf("err.zcode.missing", &[("path", &p)]));
    }
    let desktop = inst.desktop();
    fs::create_dir_all(&desktop).map_err(|e| trf("err.mkdir", &[("e", &e.to_string())]))?;
    let mut cmd = store::detached(std::process::Command::new(&exe));
    cmd.env("ZCODE_DATA_BASE_DIR", &inst.root);
    cmd.env("ZCODE_DESKTOP_USER_DATA_DIR", &desktop);
    cmd.arg(format!("--user-data-dir={}", desktop.display()));
    cmd.current_dir(&inst.root);
    let child = cmd.spawn().map_err(|e| trf("err.zcode.launch", &[("e", &e.to_string())]))?;
    Ok(child.id())
}

/// 打开（或重启）某账号的多开实例。`restart` 时先关掉已在运行的实例。
pub fn open_instance(paths: &Paths, id: &str, restart: bool, reset: bool) -> Result<OpenInstanceResult, String> {
    if !valid_id(id) {
        return Err(tr("err.store.bad_id"));
    }
    let acc = store::load_account(paths, id)?;
    let inst = InstancePaths::new(paths, id);

    if reset && inst.root.exists() {
        close_instance(paths, id).ok();
        fs::remove_dir_all(&inst.root)
            .map_err(|e| trf("err.multi.reset", &[("e", &e.to_string())]))?;
    }

    let mut restarted = false;
    if inst.root.exists() && instance_running(paths, id) {
        if !restart {
            return Ok(OpenInstanceResult {
                name: acc.name.clone(),
                instance_dir: inst.root.to_string_lossy().to_string(),
                launched: false,
                already_running: true,
                restarted: false,
                synced: false,
                config_synth: false,
                warnings: vec![],
            });
        }
        if !close_instance(paths, id)? {
            return Err(tr("err.multi.kill_timeout"));
        }
        restarted = true;
    } else if restart {
        // 记录里显示未运行，但可能残留进程（应用到更新重启过）：兜底扫一遍
        if !store::in_sandbox() && inst.root.exists() {
            if let Ok(found) = scan_instance_pids(paths, id) {
                for pid in found {
                    taskkill_tree(pid);
                }
            }
        }
    }

    let mut warnings: Vec<String> = vec![];
    let prov = provision(paths, &acc, &inst, &mut warnings)?;
    let pid = launch_instance_process(paths, &inst)?;
    if pid != 0 {
        let exe = store::effective_zcode_path(paths).0;
        let rec = InstanceRecord {
            pid,
            started_at: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_millis() as u64)
                .unwrap_or(0),
            exe,
        };
        write_record(&inst, &rec);
    }
    Ok(OpenInstanceResult {
        name: acc.name,
        instance_dir: inst.root.to_string_lossy().to_string(),
        launched: pid != 0,
        already_running: false,
        restarted,
        synced: prov.synced,
        config_synth: prov.config_synth,
        warnings,
    })
}

/// 供 delete_account 使用：账号 id 合法性检查（避免路径穿越）
pub fn cleanup_on_delete(paths: &Paths, id: &str) {
    if !valid_id(id) {
        return;
    }
    cleanup_instance(paths, id);
}

#[allow(dead_code)]
pub fn new_instance_uuid() -> String {
    Uuid::new_v4().to_string()
}
