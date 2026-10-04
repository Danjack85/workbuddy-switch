// WorkBuddy 界面：账号、会话、备份。
//
// 与 ZCode 视图的关系：两者是同一窗口里的两个「页签」，共用顶栏与样式。
// 这个模块只负责 WorkBuddy 这一侧的渲染与交互；后端是 Python 引擎（侧车），
// 由 Rust 转调（见 src-tauri/src/workbuddy.rs）。
//
// 稳定性约定（这个文件里的异步操作都遵守）：
//   1. 所有会失败的操作都走 `call()`，它统一捕错并弹提示 —— 不产生
//      未处理的 Promise rejection；
//   2. 任何操作期间把界面置为 loading，按钮禁用，避免重复点击；
//   3. 后端返回的是 JSON，字段可能缺失（不同版本引擎），渲染时一律做兜底。
import { invoke } from "@tauri-apps/api/core";
import { esc, toast, openConfirmModal } from "./ui.js";
import { ic } from "./icons.js";
import { t } from "./i18n.js";

// ---------------------------------------------------------------- 模块状态

/** 后端账号总览（`wb_state` 的返回）。 */
let state = null;
/** 侧车是否就位（首次加载前为 null）。 */
let sidecar = null;
/** 会话档案库摘要。 */
let sessions = null;
/** 正文归档摘要。 */
let bodies = null;
/** 备份列表。 */
let backups = null;

/** 当前正在进行的操作名（用于禁用按钮与显示状态）。 */
let busy = "";
/** 登录流程状态：null | {phase, url, elapsed, error} */
let login = null;
let loginTimer = null;

/** 重新渲染由 main.js 注入，避免两个模块互相 import 成环。 */
let rerender = () => {};

export function bindRerender(fn) {
  rerender = typeof fn === "function" ? fn : () => {};
}

/** 当前是否应显示这个视图（由 main.js 的 view 变量决定）。 */
export function isActive() {
  return true;
}

// ---------------------------------------------------------------- 调用封装

/** 调用后端命令并统一处理错误与忙碌状态。
 *
 * 失败时弹 toast 并返回 undefined（而不是抛异常），调用方拿到 undefined
 * 就知道失败了 —— 这样不会产生未处理的 rejection。
 */
async function call(label, fn, { silent = false } = {}) {
  if (busy) {
    toast(t("wb.busy"), "warn");
    return undefined;
  }
  busy = label;
  if (!silent) rerender();
  try {
    const r = await fn();
    return r;
  } catch (e) {
    toast(String(e), "err");
    return undefined;
  } finally {
    busy = "";
    if (!silent) rerender();
  }
}

/** 静默加载（不显示忙碌态）：用于首屏与后台刷新。 */
async function load(fn) {
  try {
    return await fn();
  } catch {
    return undefined;
  }
}

// ---------------------------------------------------------------- 数据加载

export async function refresh({ silent = true } = {}) {
  const doIt = async () => {
    sidecar = await load(() => invoke("wb_sidecar_info"));
    if (!sidecar || !sidecar.found) return;
    // 首屏数据失败要让人看见 —— 吞掉的话界面会永远停在「加载中」，没法排查
    try {
      state = await invoke("wb_state");
    } catch (e) {
      state = null;
      if (!silent) toast(String(e), "err");
      console.warn("[WorkBuddy] wb_state failed:", e);
      return;
    }
    sessions = await load(() => invoke("wb_sessions"));
    bodies = await load(() => invoke("wb_bodies", { archive: false, restore: null }));
    backups = await load(() => invoke("wb_backups"));
  };
  if (silent) await doIt();
  else await call("refresh", doIt, { silent: true });
  rerender();
}

/** 只刷新会变化的部分（切号 / 签到之后）。 */
async function refreshData() {
  state = await load(() => invoke("wb_state"));
  sessions = await load(() => invoke("wb_sessions"));
  rerender();
}

// ---------------------------------------------------------------- 渲染

/** 顶栏状态文案。 */
function statusText() {
  if (!sidecar || !sidecar.found) return t("wb.engineMissing");
  if (!state) return t("wb.loading");
  if (state.running) return t("wb.clientRunning");
  if (state.live_logged_in) {
    const active = (state.accounts || []).find((a) => a.is_active);
    return active ? t("wb.loggedInAs", { name: active.name }) : t("wb.loggedInUnsaved");
  }
  return t("wb.loggedOut");
}

function statusDotCls() {
  if (!sidecar || !sidecar.found) return "off";
  if (!state) return "";
  return state.running ? "run" : state.live_logged_in ? "" : "off";
}

function btn(label, icon, handler, { disabled = false, kind = "ghost", title = "" } = {}) {
  const cls = kind === "primary" ? "btn-primary" : "btn-ghost";
  return `<button class="${cls} has-ic" ${disabled ? "disabled" : ""}
    ${title ? `title="${esc(title)}"` : ""}
    click="${handler}">${ic(icon, 16)} ${esc(label)}</button>`;
}

/** 单个 WorkBuddy 账号行。 */
function rowHtml(a) {
  const isActive = !!a.is_active;
  const loginOk = a.has_login_state && !a.login_expired;
  const tag = isActive
    ? `<span class="tag-use">${t("wb.inUse")}</span>`
    : "";
  const cred = a.has_login_state
    ? a.login_expired
      ? `<span class="warn-line">${t("wb.credExpired")}</span>`
      : `<span class="ok-line">${t("wb.credSaved")}</span>`
    : `<span class="no-cfg">${t("wb.noCred")}</span>`;

  const archived = a.archived_sessions || 0;
  const meta = [
    a.identity_text || a.nickname || "",
    t("wb.sessions", { n: a.stats?.sessions ?? 0 }),
    archived ? t("wb.archived", { n: archived }) : "",
  ].filter(Boolean).join(" · ");

  return `
  <div class="row${isActive ? " active" : ""}" data-id="${esc(a.id)}">
    <div class="row-top">
      <div class="row-main">
        <div class="row-name">${esc(a.name)}${tag}</div>
        <div class="row-meta">${esc(meta)} · ${cred}</div>
        <div class="row-meta mono">${esc(a.uid || "")}</div>
      </div>
      <div class="row-actions">
        ${!isActive && (a.archived_sessions || 0) > 0
          ? `<button class="icon-btn" title="${esc(t("wb.copySessionsTitle"))}" aria-label="${esc(t("wb.copySessions"))}"
              click="wbActions.copySessions('${esc(a.uid)}')">${ic("dup", 16)}</button>`
          : ""}
        <button class="btn-switch has-ic" click="wbActions.switchTo('${esc(a.id)}')" ${isActive || busy ? "disabled" : ""}>
          ${isActive ? esc(t("wb.current")) : ic("swap", 14) + " " + esc(t("wb.switch"))}
        </button>
      </div>
    </div>
  </div>`;
}

/** 会话卡片：档案库摘要 + 本地/云端归属状态。 */
function sessionsHtml() {
  if (!sessions) return "";
  const total = sessions.total ?? 0;
  const owners = sessions.by_owner || {};
  const drift = sessions.drift || {};
  const driftCount = drift.count || 0;
  const cloud = drift.cloud || {};
  const cloudCount = cloud.count || 0;

  const ownerRows = Object.entries(owners)
    .map(([uid, n]) => {
      const acc = (state?.accounts || []).find((a) => a.uid === uid);
      const name = acc ? acc.name : t("wb.unknownAccount");
      return `<div class="wb-kv"><span class="k">${esc(name)}</span><span class="v">${n}
        <button class="icon-btn" title="${esc(t("wb.dedupeHint"))}" aria-label="${esc(t("wb.dedupe"))}"
          click="wbActions.dedupeSessions('${esc(uid)}')">${ic("clean", 14)}</button></span></div>`;
    })
    .join("");

  const localNote = driftCount
    ? `<div class="wb-note warn">${ic("alert", 14)}<span>${esc(t("wb.driftWarn", { n: driftCount }))}</span></div>`
    : `<div class="wb-note ok">${ic("check", 14)}<span>${esc(t("wb.driftOk"))}</span></div>`;
  const cloudNote = cloud.missing_db
    ? `<div class="wb-note"><span>${esc(t("wb.cloudMissing"))}</span></div>`
    : cloudCount
      ? `<div class="wb-note warn">${ic("alert", 14)}<span>${esc(t("wb.cloudDrift", { n: cloudCount }))}</span></div>`
      : `<div class="wb-note ok">${ic("check", 14)}<span>${esc(t("wb.cloudOk"))}</span></div>`;

  const bodyTotal = bodies?.blobs ?? 0;
  const bodyBytes = bodies?.bytes ?? 0;
  const withBody = bodies?.with_body ?? 0;

  return `
  <div class="section-head">
    <h2>${esc(t("wb.sessionsTitle"))}</h2>
    <span class="count">${esc(t("wb.sessionsCount", { n: total }))}</span>
  </div>
  <div class="wb-card">
    ${ownerRows || `<div class="wb-dim">${esc(t("wb.noSessions"))}</div>`}
    ${localNote}
    ${cloudNote}
    <div class="wb-kv">
      <span class="k">${esc(t("wb.bodyArchive"))}</span>
      <span class="v">${bodyTotal} · ${fmtBytes(bodyBytes)}</span>
    </div>
    <div class="wb-kv">
      <span class="k">${esc(t("wb.bodyPresent"))}</span>
      <span class="v">${withBody}/${total}</span>
    </div>
    <div class="wb-actions">
      ${btn(t("wb.archiveBodies"), "capture", "wbActions.archiveBodies()", { disabled: !!busy })}
      ${(driftCount || cloudCount)
        ? btn(t("wb.fixDrift"), "refresh", "wbActions.fixDrift()", {
            disabled: !!busy, kind: "primary",
            title: t("wb.fixDriftTitle"),
          })
        : ""}
    </div>
  </div>`;
}

function backupsHtml() {
  const list = Array.isArray(backups) ? backups : [];
  const rows = list.slice(0, 6).map((b) => `
    <div class="wb-kv">
      <span class="k">${esc(b.tag || "")}<br><span class="wb-dim">${esc(b.created_at || "")}</span></span>
      <span class="v">
        ${esc(b.size_text || "")}
        <button class="btn-ghost" style="margin-left:8px;padding:2px 8px"
          click="wbActions.rollback('${esc(b.tag || "")}')" ${busy ? "disabled" : ""}>
          ${esc(t("wb.rollback"))}
        </button>
      </span>
    </div>`).join("");

  return `
  <div class="section-head">
    <h2>${esc(t("wb.backupsTitle"))}</h2>
    <span class="count">${list.length}</span>
  </div>
  <div class="wb-card">
    ${rows || `<div class="wb-dim">${esc(t("wb.noBackups"))}</div>`}
    <div class="wb-actions">
      ${btn(t("wb.makeBackup"), "export", "wbActions.makeBackup()", { disabled: !!busy })}
    </div>
  </div>`;
}

/** 登录流程面板（在页面内联显示，不用弹窗 —— 弹窗一旦被误关就断了流程）。 */
function loginHtml() {
  if (!login) return "";

  // 第一步：选版本。两个版本是不同产品的登录页（国内版微信/手机号，
  // 国际版邮箱/SSO），点「添加账号」时不该替用户默认任何一个。
  if (login.phase === "choose") {
    return `
    <div class="wb-card highlight">
      <div class="wb-row-between">
        <b>${esc(t("wb.addAccount"))}</b>
        <button class="btn-ghost" click="wbActions.loginCancel()">${esc(t("common.cancel"))}</button>
      </div>
      <div class="wb-dim">${esc(t("wb.chooseEdition"))}</div>
      <div class="wb-actions">
        ${btn(t("wb.edCn"), "play", "wbActions.loginStart('cn')", { disabled: !!busy })}
        ${btn(t("wb.edIntl"), "play", "wbActions.loginStart('intl')", { disabled: !!busy })}
      </div>
    </div>`;
  }

  const step = login.phase === "waiting"
    ? t("wb.loginWaiting", { sec: login.elapsed ?? 0 })
    : login.phase === "error"
      ? esc(login.error || t("wb.loginFailed"))
      : t("wb.loginStarting");

  return `
  <div class="wb-card highlight">
    <div class="wb-row-between">
      <b>${esc(t("wb.addAccount"))}</b>
      <button class="btn-ghost" click="wbActions.loginCancel()">${esc(t("common.cancel"))}</button>
    </div>
    ${login.url
      ? `<div class="wb-dim">${esc(t("wb.loginOpen"))}</div>
         <div class="wb-url">${esc(login.url)}</div>
         <div class="wb-actions">
           <button class="btn-ghost has-ic" click="wbActions.loginCopy()">${ic("export", 14)} ${esc(t("wb.copyLink"))}</button>
           <button class="btn-ghost has-ic" click="wbActions.loginOpenBrowser()">${ic("play", 14)} ${esc(t("wb.openBrowser"))}</button>
         </div>`
      : ""}
    <div class="wb-note ${login.phase === "error" ? "warn" : ""}">${step}</div>
  </div>`;
}

function missingEngineHtml() {
  const tried = (sidecar?.tried || []).map((p) => `<div class="wb-dim mono">${esc(p)}</div>`).join("");
  return `
  <div class="wb-card highlight">
    <div class="wb-note warn">
      ${ic("alert", 14)}
      <span>${esc(t("wb.engineMissingHint"))}</span>
    </div>
    ${tried ? `<details><summary>${esc(t("wb.triedPaths"))}</summary>${tried}</details>` : ""}
    <div class="wb-actions">
      ${btn(t("common.refresh"), "refresh", "wbActions.reload()")}
    </div>
  </div>`;
}

/** 渲染整个 WorkBuddy 视图（返回 HTML 字符串，由 main.js 注入 #app）。 */
export function viewHtml(tabsHtml) {
  const header = `
  <header class="topbar">
    <div class="wordmark">SwitchSuite${state?.app_version ? ` <span class="ver">v${esc(state.app_version)}</span>` : ""}</div>
    ${tabsHtml}
    <div class="top-status">
      <span class="status-dot ${statusDotCls()}"></span>
      <span class="status-text">${esc(statusText())}</span>
    </div>
  </header>`;

  if (!sidecar || !sidecar.found) {
    return header + `<main class="list"><div class="wb-wrap">${missingEngineHtml()}</div></main>`;
  }

  const busyBar = busy
    ? `<div class="wb-note"><span class="spinner"></span><span>${esc(t("wb.working", { what: busyLabel(busy) }))}</span></div>`
    : "";

  const accounts = state?.accounts || [];
  const listHtml = accounts.length
    ? accounts.map(rowHtml).join("")
    : `<div class="empty">
         <div class="glyph">${ic("empty", 34)}</div>
         ${esc(t("wb.emptyTitle"))}<br>${esc(t("wb.emptyBody"))}
       </div>`;

  const toolbar = `
  <section class="toolbar">
    ${btn(t("wb.saveLogin"), "capture", "wbActions.capture()", {
      disabled: !!busy || !state?.live_logged_in, kind: "primary" })}
    ${btn(t("wb.addAccount"), "userPlus", "wbActions.loginStart()", { disabled: !!busy })}
    ${btn(t("wb.checkin"), "gift", "wbActions.checkin()", {
      disabled: !!busy, title: t("wb.checkinTitle") })}
    ${btn(t("wb.credits"), "gauge", "wbActions.credits()", { disabled: !!busy })}
    ${btn(t("wb.models"), "empty", "wbActions.models()", { disabled: !!busy })}
    <span class="tb-spacer"></span>
    ${state?.running
      ? btn(t("wb.kill"), "power", "wbActions.kill()", { disabled: !!busy })
      : btn(t("wb.launch"), "play", "wbActions.launch()", { disabled: !!busy })}
    ${btn(t("wb.diagnostics"), "sliders", "wbActions.diagnostics()", { disabled: !!busy })}
    ${btn(t("common.refresh"), "refresh", "wbActions.reload()", { disabled: !!busy })}
  </section>`;

  return header + `
  <main class="list wb-list">
    <div class="wb-wrap">
      ${busyBar}
      ${loginHtml()}
      ${toolbar}
      <div class="section-head">
        <h2>${esc(t("wb.accountsTitle"))}</h2>
        <span class="count">${accounts.length}</span>
      </div>
      ${listHtml}
      ${sessionsHtml()}
      ${backupsHtml()}
      <div class="wb-foot">
        ${esc(t("wb.engineInfo", {
          ver: sidecar.version || "?",
          path: sidecar.path || "",
        }))}
      </div>
    </div>
  </main>`;
}

function busyLabel(key) {
  const map = {
    switch: t("wb.opSwitch"),
    capture: t("wb.opCapture"),
    checkin: t("wb.opCheckin"),
    credits: t("wb.opCredits"),
    models: t("wb.opModels"),
    sessions: t("wb.opSessions"),
    bodies: t("wb.opBodies"),
    backup: t("wb.opBackup"),
    rollback: t("wb.opRollback"),
    login: t("wb.opLogin"),
    kill: t("wb.opKill"),
    launch: t("wb.opLaunch"),
    diagnostics: t("wb.opDiagnostics"),
  };
  return map[key] || key;
}

function fmtBytes(n) {
  if (!n) return "0";
  if (n >= 1 << 20) return (n / (1 << 20)).toFixed(1) + " MB";
  if (n >= 1 << 10) return (n / (1 << 10)).toFixed(0) + " KB";
  return n + " B";
}

/** 展示一个只读结果（模型清单 / 额度）—— 用系统确认框太重，这里用 toast + 控制台。 */
function showText(title, lines) {
  toast(title, "ok", lines.slice(0, 3).join(" · ") + (lines.length > 3 ? " …" : ""));
  // 完整内容写控制台，方便需要时查看
  console.log(`[WorkBuddy] ${title}`, lines);
}

// ---------------------------------------------------------------- 交互

export const wbActions = {
  async reload() {
    await call("refresh", () => refresh({ silent: false }), { silent: false });
  },

  async switchTo(id) {
    const acc = (state?.accounts || []).find((a) => a.id === id);
    const name = acc?.name || id;
    const running = !!state?.running;

    // 客户端在跑时换号需要先结束它 —— 要在确认框里说清楚
    openConfirmModal({
      kind: "warn",
      icon: "swap",
      title: t("wb.switchTitle", { name }),
      desc: running
        ? `<span class="warn-line">${esc(t("wb.switchBodyRunning"))}</span>`
        : esc(t("wb.switchBody")),
      yesLabel: t("wb.switch"),
      onYes: async () => {
        const r = await call("switch", () =>
          invoke("wb_switch", { id, force: true, restart: null }));
        if (!r) return;

        const bits = [];
        if (r.login_state) bits.push(t("wb.credentialsSwitched"));
        if (r.sessions_visible) bits.push(t("wb.sessionsReady", { n: r.sessions_visible }));
        if (r.needs_login) bits.push(t("wb.needLogin"));
        if (r.warnings?.length) bits.push(r.warnings[0]);
        toast(t("wb.switched", { name: r.name || name }), "ok", bits.join(" · "));

        await refreshData();
      },
    });
  },

  async capture() {
    const r = await call("capture", () => invoke("wb_capture", { name: null }));
    if (!r) return;
    toast(t("wb.captured", { name: r.name }), "ok");
    await refreshData();
  },

  async checkin() {
    const r = await call("checkin", () => invoke("wb_checkin", { id: null }));
    if (!r) return;
    const bits = [t("wb.ckOk", { n: r.ok ?? 0 })];
    if (r.already) bits.push(t("wb.ckAlready", { n: r.already }));
    if (r.inactive) bits.push(t("wb.ckInactive", { n: r.inactive }));
    if (r.failed) bits.push(t("wb.ckFailed", { n: r.failed }));
    if (r.skipped?.length) bits.push(t("wb.ckSkipped", { n: r.skipped.length }));
    toast(t("wb.checkin"), (r.ok || r.already) ? "ok" : "warn", bits.join(" · "));
    await refreshData();
  },

  async credits() {
    const r = await call("credits", () => invoke("wb_credits", { id: null }));
    if (!r) return;
    const lines = (Array.isArray(r) ? r : []).map((x) => {
      if (!x.ok) return `${x.name}: ${x.error}`;
      return `${x.name}: ${x.remain}/${x.total}${x.checkin ? " · " + x.checkin : ""}`;
    });
    showText(t("wb.credits"), lines);
  },

  async models() {
    const r = await call("models", () => invoke("wb_models"));
    if (!r) return;
    const list = Array.isArray(r) ? r : [];
    showText(t("wb.modelsTitle", { n: list.length }),
      list.map((m) => `${m.id}${m.credits ? " " + m.credits : ""}`));
  },

  async archiveBodies() {
    const r = await call("bodies", () => invoke("wb_bodies", { archive: true, restore: null }));
    if (!r) return;
    toast(t("wb.bodiesDone"), "ok",
      t("wb.bodiesDetail", { stored: r.stored ?? 0, deduped: r.deduped ?? 0, missing: r.missing ?? 0 }));
    bodies = await load(() => invoke("wb_bodies", { archive: false, restore: null }));
    rerender();
  },

  async fixDrift() {
    // 归属修正：本地漂移（客户端索引 ≠ 档案库）用 adopt 划归；
    // 云端漂移（edge-sync 映射 ≠ 档案库）用 sync-cloud 修回。两者可同时存在。
    const localCount = sessions?.drift?.count || 0;
    const cloudCount = sessions?.drift?.cloud?.count || 0;
    if (!localCount && !cloudCount) {
      toast(t("wb.driftOk"), "ok");
      return;
    }

    const body = localCount && cloudCount
      ? t("wb.fixBodyBoth", { cloud: cloudCount, local: localCount })
      : cloudCount
        ? t("wb.fixBodyCloud", { cloud: cloudCount })
        : t("wb.fixBodyLocal", { local: localCount });

    openConfirmModal({
      kind: "warn",
      icon: "refresh",
      title: t("wb.fixDriftTitle"),
      desc: esc(body),
      yesLabel: t("wb.fixDriftConfirm"),
      onYes: async () => {
        if (cloudCount) {
          const r = await call("sessions", () =>
            invoke("wb_sessions_sync_cloud", { dryRun: false }));
          if (r) toast(t("wb.syncCloudDone", { n: r.fixed ?? 0 }), "ok");
        }
        if (localCount) {
          const src = driftSourceUid();
          if (src) {
            const r = await call("sessions", () =>
              invoke("wb_adopt", { sourceUid: src, dryRun: false }));
            if (r) toast(t("wb.fixDriftDone", { n: r.adopted ?? 0 }), "ok",
              t("wb.fixDriftDetail", { cloud: r.edge_rows ?? 0 }));
          }
        }
        await refreshData();
        // 云端库被客户端占用时修正不会生效 —— 刷新后仍有漂移就如实提醒
        if (cloudCount && ((sessions?.drift?.cloud || {}).count || 0)) {
          toast(t("wb.cloudBusy"), "warn", t("wb.cloudBusyDetail"));
        }
      },
    });
  },

  /** 会话复制：把某账号的会话复制一份给当前账号（原会话保留，真共享）。 */
  async copySessions(sourceUid) {
    // 复制要写客户端独占的会话数据库与云端映射 —— 运行中必然失败，先拦截
    if (state?.running) {
      toast(t("wb.needCloseClient"), "warn");
      return;
    }
    const acc = (state?.accounts || []).find((x) => x.uid === sourceUid);
    const name = acc?.name || sourceUid;

    // 先演练拿条数，再让用户确认
    const dry = await call("sessions", () =>
      invoke("wb_sessions_copy", { sourceUid, targetUid: null, dryRun: true }));
    if (!dry) return;
    if (!dry.would_copy) {
      toast(t("wb.copyNothing"), "warn", dry.text ? "" : "");
      return;
    }

    openConfirmModal({
      kind: "warn",
      icon: "dup",
      title: t("wb.copyTitle", { name }),
      desc: esc(t("wb.copyBody", { n: dry.would_copy })),
      yesLabel: t("wb.copySessions"),
      onYes: async () => {
        const r = await call("sessions", () =>
          invoke("wb_sessions_copy", { sourceUid, targetUid: null, dryRun: false }));
        if (!r) return;
        toast(t("wb.copyDone", { n: r.copied ?? 0 }), "ok",
          t("wb.copyDetail", {
            copied: r.copied ?? 0, skipped: r.skipped_exists ?? 0,
            missing: r.missing_body ?? 0, failed: r.failed ?? 0,
          }));
        await refreshData();
      },
    });
  },

  /** 同根源去重：某账号名下重复副本只留内容最完整的一份。 */
  async dedupeSessions(uid) {
    // 去重要写客户端独占的会话数据库与云端映射 —— 运行中必然失败，先拦截
    if (state?.running) {
      toast(t("wb.needCloseClient"), "warn");
      return;
    }
    const acc = (state?.accounts || []).find((x) => x.uid === uid);
    const name = acc?.name || uid;

    // 先演练拿组数，再让用户确认
    const dry = await call("sessions", () =>
      invoke("wb_sessions_dedupe", { uid, clearError: true, dryRun: true }));
    if (!dry) return;
    if (!dry.removed) {
      toast(t("wb.dedupeNone"), "ok");
      return;
    }

    openConfirmModal({
      kind: "danger",
      icon: "clean",
      title: t("wb.dedupeTitle", { name }),
      desc: esc(t("wb.dedupeBody", { groups: dry.groups ?? 0, n: dry.removed ?? 0 })),
      yesLabel: t("wb.dedupe"),
      onYes: async () => {
        const r = await call("sessions", () =>
          invoke("wb_sessions_dedupe", { uid, clearError: true, dryRun: false }));
        if (!r) return;
        toast(t("wb.dedupeDone"), "ok",
          t("wb.dedupeDetail", {
            removed: r.removed ?? 0, cleared: r.cleared_errors ?? 0,
          }));
        await refreshData();
      },
    });
  },

  async makeBackup() {
    const r = await call("backup", () => invoke("wb_backup", { label: null }));
    if (!r) return;
    toast(t("wb.backupDone", { tag: r.tag || "" }), "ok");
    backups = await load(() => invoke("wb_backups"));
    rerender();
  },

  async rollback(tag) {
    openConfirmModal({
      kind: "danger",
      icon: "refresh",
      title: t("wb.rollbackTitle", { tag }),
      desc: t("wb.rollbackBody"),
      yesLabel: t("wb.rollback"),
      onYes: async () => {
        const r = await call("rollback", () => invoke("wb_rollback", { tag }));
        if (!r) return;
        toast(t("wb.rollbackDone"), "ok");
        await refreshData();
      },
    });
  },

  async kill() {
    const r = await call("kill", () => invoke("wb_kill"));
    if (!r) return;
    toast(t("wb.killed"), "ok");
    await refreshData();
  },

  async launch() {
    const r = await call("launch", () => invoke("wb_launch"));
    if (!r) return;
    toast(t("wb.launched"), "ok");
    await refreshData();
  },

  async diagnostics() {
    const r = await call("diagnostics", () => invoke("wb_diagnose"));
    if (!r) return;
    const d = r.paths || {};
    showText(t("wb.diagnostics"), [
      `${t("wb.diagHome")}: ${d.workbuddy_home || "-"}`,
      `${t("wb.diagDb")}: ${d.db_exists ? "OK" : "-"}`,
      `${t("wb.diagAuth")}: ${d.login_state_exists ? "OK" : "-"}`,
      `${t("wb.diagIntegrity")}: ${r.integrity || "-"}`,
    ]);
  },

  // ---- 登录流程（分步：拿链接 → 轮询 → 完成）----

  async loginStart(edition) {
    if (!edition) {
      // 没带版本 = 进入选择页（国内版与国际版是不同产品的登录页）
      login = { phase: "choose", url: "", elapsed: 0 };
      rerender();
      return;
    }
    login = { phase: "starting", url: "", elapsed: 0 };
    rerender();
    const r = await call("login", () => invoke("wb_login_start", { edition }));
    if (!r) {
      login = null;
      rerender();
      return;
    }
    login = { phase: "waiting", url: r.auth_url || "", elapsed: 0 };
    rerender();
    // 自动打开系统浏览器（比让用户手动复制更省事）
    try {
      await invoke("open_external", { url: r.auth_url });
    } catch {
      /* 打不开也没关系，页面上有链接和复制按钮 */
    }
    startLoginPolling();
  },

  async loginCopy() {
    if (!login?.url) return;
    try {
      await navigator.clipboard.writeText(login.url);
      toast(t("wb.copied"), "ok");
    } catch {
      toast(t("wb.copyFailed"), "warn");
    }
  },

  async loginOpenBrowser() {
    if (!login?.url) return;
    try {
      await invoke("open_external", { url: login.url });
    } catch {
      toast(t("wb.openFailed"), "warn");
    }
  },

  async loginCancel() {
    stopLoginPolling();
    login = null;
    await load(() => invoke("wb_login_cancel"));
    rerender();
  },
};

window.wbActions = wbActions;

/** 漂移的源账号：取档案库里出现、但不属于当前登录账号的第一个 uid。 */
function driftSourceUid() {
  const drifted = sessions?.drift?.drifted || {};
  for (const [, pair] of Object.entries(drifted)) {
    // pair = [客户端 uid, 档案库 owner uid]
    if (Array.isArray(pair) && pair[1]) return pair[1];
  }
  return "";
}

// ---- 登录轮询：由前端控制节奏，单次只等一小段 ----

function startLoginPolling() {
  stopLoginPolling();
  const tick = async () => {
    if (!login || login.phase !== "waiting") return;
    login.elapsed = (login.elapsed || 0) + 1;
    rerender();
    const r = await load(() => invoke("wb_login_poll", { window: 20, name: null }));
    if (!login) return;
    if (!r) {
      login.phase = "error";
      login.error = t("wb.loginFailed");
      rerender();
      return;
    }
    if (r.status === "done") {
      stopLoginPolling();
      login = null;
      toast(t("wb.loginOk", { name: r.account?.name || "" }), "ok");
      await refreshData();
      return;
    }
    if (r.status === "expired" || r.status === "error" || r.status === "none") {
      stopLoginPolling();
      login.phase = "error";
      login.error = r.error || t("wb.loginFailed");
      rerender();
      return;
    }
    // pending：继续下一轮
    if (login) { login.elapsed = r.elapsed ?? login.elapsed; }
  };
  // 立刻跑一轮，之后每 3 秒一次（轮询内部还会等 20 秒，所以实际节奏是 ~23 秒）
  tick();
  loginTimer = setInterval(tick, 3000);
}

function stopLoginPolling() {
  if (loginTimer) {
    clearInterval(loginTimer);
    loginTimer = null;
  }
}

/** 切到别的视图时清理定时器，避免后台空转。 */
export function onLeave() {
  stopLoginPolling();
}
