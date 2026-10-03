# -*- coding: utf-8 -*-
"""扫码登录 / 添加账号。

让工具能在**不打扰当前登录**的前提下，把另一个账号加进档案库 —— 这样多账号
互切才真正可用（否则只能切换"以前登录过的那几个"）。

## 流程（与官方桌面端 createSession 逐步对齐）

```
① POST /v2/plugin/auth/state?platform=workbuddy      （匿名）
     → data.authUrl + data.state
② 打开 authUrl，在浏览器里完成登录
③ GET  /v2/plugin/auth/token?state=<state>           （匿名，轮询）
     → 尚未完成时上游返回 code=11217，继续等
     → 完成后给出 accessToken / refreshToken / expiresIn / refreshExpiresIn
④ GET  /v2/plugin/login/account?state=<state>        （Bearer，拿账号详情）
⑤ 落盘成一份登录态文件，交给账号档案库接管
```

关键区别（容易写错）：

| 接口类别 | 路径 |
| --- | --- |
| 登录 / 账号 | `{endpoint}/v2/plugin/...` ← **带**前缀 |
| 计费 / 签到 / LLM | `{endpoint}/v2/...` ← **不带**前缀 |

## 为什么轮询而不是等着

授权是在**浏览器**里完成的，我们这边只能反复问上游「好了没」。实测节奏：
每 3 秒问一次，总超时 5 分钟 —— 与桌面端 `SIGN_IN_FETCH_INTERVAL` /
`SIGN_IN_PENDING_TIMEOUT` 一致。
"""

from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import paths, profiles
from .upstream import (
    CODE_OK,
    CODE_RETRY_FETCH_TOKEN,
    Client,
    Edition,
    Response,
    Session,
    UpstreamError,
    build_anonymous_headers,
    resolve_edition,
)

#: 登录轮询节奏（与桌面端一致）
POLL_INTERVAL_SEC = 3.0
#: 登录总超时
LOGIN_TIMEOUT_SEC = 300.0
#: 平台标识，auth/state 的查询参数
PLATFORM = "workbuddy"


@dataclass
class LoginHandle:
    """一次登录尝试的上下文。"""

    state: str
    auth_url: str
    edition: Edition
    started_at: float = field(default_factory=time.time)

    def elapsed(self) -> float:
        return time.time() - self.started_at


class LoginCancelled(Exception):
    """调用方主动取消了登录。"""


def start_login(client: Client | None = None,
                edition: Edition | str | None = None) -> LoginHandle:
    """第①步：拿授权链接与 state。

    这一步是匿名调用 —— 此时我们还没有任何令牌，而且**不应该**带上当前
    登录账号的令牌，否则上游可能把它当成"给已登录用户续期"而不是新登录。

    `platform` 是**必需的查询参数**，缺了上游直接回 `10001 platform is empty`。
    """
    ed = resolve_edition(edition) if isinstance(edition, (str, type(None))) else edition
    c = client or Client()
    query = urllib.parse.urlencode({"platform": PLATFORM})
    url = ed.auth_url("auth/state") + "?" + query
    resp = c.call(None, url, payload={}, method="POST", anonymous=True)

    if not resp.ok:
        raise UpstreamError(
            f"获取授权链接失败：{resp.message or '上游未返回预期数据'}",
            status=resp.status, code=resp.code, body=resp.raw,
        )

    data = resp.data if isinstance(resp.data, dict) else {}
    state = str(data.get("state") or data.get("authState") or "")
    auth_url = str(data.get("authUrl") or "")
    if not state or not auth_url:
        raise UpstreamError("上游未返回 state / authUrl，无法发起登录")
    # 防御：登录页靠 state 关联这次授权。实测上游返回的 authUrl 一般已带
    # state，但若某天返回的是不带 state 的裸链接，客户端登录就会报
    # 「登录链接不完整」—— 这里统一保证 state 在 URL 上。
    if state not in auth_url:
        auth_url += ("&" if "?" in auth_url else "?") + urllib.parse.urlencode({"state": state})
    return LoginHandle(state=state, auth_url=auth_url, edition=ed)


def poll_login(handle: LoginHandle,
               client: Client | None = None,
               *,
               timeout: float = LOGIN_TIMEOUT_SEC,
               interval: float = POLL_INTERVAL_SEC,
               on_wait: Callable[[float], None] | None = None,
               should_cancel: Callable[[], bool] | None = None) -> Session:
    """第③④步：轮询直到拿到令牌，并组装成会话。

    `code=11217`（RetryFetchToken）表示「用户还没在浏览器里点完」，属于正常
    中间态，继续等即可。其他错误也继续等 —— 网络抖动不该直接判失败。

    超时或取消抛异常，由调用方提示用户。
    """
    c = client or Client()
    deadline = time.time() + max(10.0, timeout)

    while time.time() < deadline:
        if should_cancel and should_cancel():
            raise LoginCancelled("登录已取消")
        time.sleep(max(0.5, interval))
        if on_wait:
            on_wait(handle.elapsed())

        try:
            resp = c.call(
                None,
                handle.edition.auth_url("auth/token") + f"?state={handle.state}",
                method="GET",
                anonymous=True,
            )
        except UpstreamError:
            # 网络抖动：继续等，不要因为一次超时就判失败
            continue

        if resp.code == CODE_RETRY_FETCH_TOKEN or not resp.ok:
            continue

        token_data = resp.data if isinstance(resp.data, dict) else {}
        if not token_data.get("accessToken"):
            continue

        return _build_session(c, handle, token_data)

    raise TimeoutError(
        f"登录轮询超时（{int(timeout // 60)} 分钟）——"
        "请确认已在浏览器里完成授权"
    )


def _build_session(client: Client, handle: LoginHandle, token_data: dict) -> Session:
    """补齐令牌有效期，并拉取账号详情。"""
    now_ms = int(time.time() * 1000)
    expires_in = _int(token_data.get("expiresIn"))
    refresh_in = _int(token_data.get("refreshExpiresIn"))

    session = Session(
        uid="",
        access_token=str(token_data.get("accessToken") or ""),
        refresh_token=str(token_data.get("refreshToken") or ""),
        domain="",  # 由账号详情里的 domain 补上，或按版本推导
        edition=handle.edition,
        expires_at=now_ms + expires_in * 1000 if expires_in else 0,
    )

    account: dict = {}
    # ④ 拿账号详情（这一步才带 Bearer）
    try:
        resp = client.call(
            session,
            handle.edition.auth_url("login/account") + f"?state={handle.state}",
            method="GET",
            extra_headers={
                "X-No-User-Id": "true",
                "X-No-Enterprise-Id": "true",
                "X-No-Department-Info": "true",
            },
        )
        if isinstance(resp.data, dict):
            account = resp.data
    except UpstreamError:
        pass

    # 退一步：拿账号列表，挑 lastLogin 的那个
    if not account.get("uid"):
        try:
            resp = client.call(session, handle.edition.auth_url("accounts"), method="GET")
            data = resp.data if isinstance(resp.data, dict) else {}
            arr = data.get("accounts")
            if isinstance(arr, list) and arr:
                account = next(
                    (a for a in arr if isinstance(a, dict) and a.get("lastLogin")),
                    arr[0] if isinstance(arr[0], dict) else {},
                )
        except UpstreamError:
            pass

    if not account.get("uid"):
        raise UpstreamError("登录成功但拿不到账号信息（缺少 uid），请重试")

    session.uid = str(account.get("uid") or "")
    session.nickname = str(account.get("nickname") or "")
    session.enterprise_id = str(account.get("enterpriseId") or "")
    session.domain = str(account.get("domain") or "") or _domain_for(handle.edition)
    return session


def _domain_for(edition: Edition) -> str:
    """按版本推导 domain（账号详情没给时用）。"""
    return "www.workbuddy.ai" if edition.id == "intl" else "copilot.tencent.com"


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def persist_login(session: Session, name: str | None = None):
    """把刚登录的账号落盘成档案。

    写法刻意与客户端的登录态文件同构（`account` + `auth` 两段），这样：
      · 后续换号时可以直接把它写回客户端，不需要转换格式；
      · 用同一套 `Session.from_account()` 读取，不必为"工具登录的账号"走特殊分支。

    返回创建好的 Account。同 uid 已存在时抛错（由调用方决定是否走更新）。
    """
    if not session.uid:
        raise UpstreamError("会话缺少 uid，无法建档")

    primary = {
        "uid": session.uid,
        "nickname": session.nickname or "",
        "type": "personal",
        "editionType": "",
        "isPro": False,
        "isAdmin": False,
        "oneidAccountId": "",
    }
    acc = profiles.capture_from_identity(primary, name or session.nickname or session.uid[:8])

    # 写入与客户端同构的登录态快照
    snap_dir = acc.session_dir()
    snap_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "account": {
            "uid": session.uid,
            "nickname": session.nickname or "",
            "type": "personal",
            "lastLogin": True,
            "isCreator": False,
            "isAdmin": False,
            "pluginEnabled": True,
            "enterpriseId": session.enterprise_id or "",
        },
        "auth": {
            "accessToken": session.access_token,
            "refreshToken": session.refresh_token,
            "tokenType": "Bearer",
            "domain": session.domain,
            "expiresAt": session.expires_at,
            "refreshExpiresAt": session.expires_at,
            "lastRefreshTime": int(time.time() * 1000),
        },
        "accounts": [],
        "allAccounts": [],
    }
    target = snap_dir / (paths.auth_file_name())
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    profiles._harden(target)

    acc.uid = session.uid
    acc.nickname = session.nickname or acc.nickname
    acc.has_login_state = True
    profiles.save_account(acc)
    return acc


def login_and_save(client: Client | None = None,
                   edition: Edition | str | None = None,
                   *,
                   open_browser: bool = True,
                   timeout: float = LOGIN_TIMEOUT_SEC,
                   interval: float = POLL_INTERVAL_SEC,
                   on_auth_url: Callable[[str], None] | None = None,
                   on_wait: Callable[[float], None] | None = None,
                   should_cancel: Callable[[], bool] | None = None,
                   name: str | None = None):
    """完整跑一遍登录并建档。返回 (Account, LoginHandle)。"""
    import webbrowser

    handle = start_login(client, edition)
    if on_auth_url:
        on_auth_url(handle.auth_url)
    if open_browser:
        try:
            webbrowser.open(handle.auth_url)
        except Exception:
            pass

    session = poll_login(
        handle, client, timeout=timeout, interval=interval,
        on_wait=on_wait, should_cancel=should_cancel,
    )
    acc = persist_login(session, name)
    return acc, handle


# --------------------------------------------------------------------------
# 分步登录（给 Tauri 前端用）
# --------------------------------------------------------------------------
#
# 上面的 login_and_save 是**阻塞**的：拿到链接后在同一个进程里一直轮询到
# 用户完成授权（最长 5 分钟）。给图形前端用不合适 —— 界面会被一个调用卡住。
#
# 所以把流程拆成三步，每步都是一次独立的短调用：
#   begin()  拿链接，把 state 存盘，立刻返回
#   poll()   轮询一段短时间就返回（pending / done），由前端决定循环节奏
#   cancel() 清掉待完成的登录
#
# 这样前端可以自己控制节奏、随时取消，也能在等待期间继续响应界面。


def _pending_path() -> Path:
    return paths.store_dir() / "pending-login.json"


def begin(edition: Edition | str | None = None,
          client: Client | None = None) -> dict:
    """开始登录：拿链接、存 state、立刻返回。"""
    handle = start_login(client, edition)
    payload = {
        "state": handle.state,
        "auth_url": handle.auth_url,
        "edition": handle.edition.id,
        "started_at": handle.started_at,
    }
    try:
        paths.ensure_store_dirs()
        f = _pending_path()
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(f)
    except Exception:
        pass
    return payload


def pending() -> dict | None:
    """读取尚未完成的登录（没有则返回 None）。"""
    try:
        data = json.loads(_pending_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("state"):
        return None
    return data


def cancel() -> None:
    """放弃当前待完成的登录。"""
    try:
        _pending_path().unlink(missing_ok=True)
    except Exception:
        pass


def poll(client: Client | None = None,
         *,
         window: float = 25.0,
         interval: float = POLL_INTERVAL_SEC,
         name: str | None = None) -> dict:
    """轮询一段短时间（默认 25 秒）就返回，不让前端卡住。

    返回三种之一：
      {"status": "none"}                      没有待完成的登录
      {"status": "pending", "elapsed": 12.3}  还在等用户授权
      {"status": "done", "account": {...}}    完成并已建档
      {"status": "expired", "error": "..."}   超过总时限
    """
    data = pending()
    if data is None:
        return {"status": "none"}

    try:
        edition = resolve_edition(str(data.get("edition") or "cn"))
    except Exception:
        edition = resolve_edition("cn")
    handle = LoginHandle(
        state=str(data.get("state") or ""),
        auth_url=str(data.get("auth_url") or ""),
        edition=edition,
        started_at=float(data.get("started_at") or time.time()),
    )

    # 总时限按 handle 起始时间算，避免前端反复调用把时限无限延长
    remaining = LOGIN_TIMEOUT_SEC - handle.elapsed()
    if remaining <= 0:
        cancel()
        return {"status": "expired", "error": "登录已超时，请重新发起"}

    try:
        session = poll_login(
            handle, client,
            timeout=min(window, remaining),
            interval=interval,
        )
    except TimeoutError:
        # 这一段没等到：正常，继续等前端下次调用
        return {"status": "pending", "elapsed": round(handle.elapsed(), 1)}

    try:
        acc = persist_login(session, name)
    except Exception as e:
        cancel()
        return {"status": "error", "error": f"登录成功但建档失败：{e}"}

    cancel()
    return {
        "status": "done",
        "account": {"id": acc.id, "name": acc.name, "uid": acc.uid,
                    "nickname": acc.nickname},
    }
