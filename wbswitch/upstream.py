# -*- coding: utf-8 -*-
"""WorkBuddy 上游 HTTP 客户端：版本探测、请求头复刻、容错。

这是「联网」能力的唯一入口 —— 签到、积分查询、OpenAI 兼容网关都走这里。
工具其余部分（换号、会话留存、备份）依然完全离线。

## 为什么需要复刻请求头

上游按请求头识别「是不是官方客户端通道」。少了几个关键头，会被判为
非授权通道（`code 11128`）或只下发精简版模型清单。这里按官方桌面端的
实际行为复刻：

| 头 | 值 |
| --- | --- |
| `User-Agent` | `<产品名>/<版本> <产品名>/<版本> CLI/<cli版本>` |
| `X-IDE-Type` / `X-IDE-Name` / `X-IDE-Version` | 平台标识 / 产品名 / 客户端版本 |
| `X-Product` / `X-Agent-Intent` | 产品名 / craft |
| `X-Request-ID` 等追踪头 | 随机 UUID |
| `Authorization` / `X-User-Id` | 账号令牌与 uid |

## 版本不写死

客户端版本从本机 `last-launch.json` 实时读取，读不到才退回内置常量。
上游升版本时不会因为我们写死而失效。

## 两个版本（edition）

| | 国内版 cn | 国际版 intl |
| --- | --- | --- |
| 端点 | `copilot.tencent.com` | `www.workbuddy.ai` |
| 产品名 | WorkBuddy | WorkBuddy AI |
| 鉴权前缀 | `/plugin` | `/plugin` |

鉴权类接口（登录/账号）带前缀 `/v2/plugin/...`；
计费、签到、LLM 类接口**不带**前缀，直接 `/v2/...`。
"""

from __future__ import annotations

import json
import random
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import paths

# --------------------------------------------------------------------------
# 版本常量
# --------------------------------------------------------------------------

#: 兜底客户端版本（读不到本机版本时使用）
FALLBACK_CLIENT_VERSION = "5.5.2"
#: CLI 扩展段版本（UA 里必须有这一段，否则上游只给精简模型清单）
CLI_VERSION = "2.137.1"

#: 上游成功码
CODE_OK = 0

#: 频率风控码。命中后退避重试，间隔故意拉长 ——
#: 拉黑期间每次重试都会给黑名单续期，宁可让调用方拿到明确错误。
CODE_RATE_LIMITED = 11128
CODE_RETRY_FETCH_TOKEN = 11217
CODE_ACCOUNT_BLOCKED = 12151
CODE_LICENSE_EXPIRED = 11212
CODE_TRIAL_EXPIRED = 11216
CODE_IP_LIMIT = 10081
CODE_NON_STREAM_NOT_SUPPORTED = 11101
#: 签到活动未开启/已过期
CODE_CHECKIN_INACTIVE = 10001

#: 风控退避间隔（毫秒）
WAF_RETRY_DELAYS = (10.0, 25.0)

IDEMPOTENT_OK_CODES = {CODE_OK}


@dataclass(frozen=True)
class Edition:
    """一个产品版本（国内版 / 国际版）。"""

    id: str
    label: str
    endpoint: str
    ua_platform: str
    product_name: str
    prefix_path: str = "/plugin"

    def auth_url(self, path: str) -> str:
        """鉴权类接口 URL（带 /plugin 前缀）。"""
        return f"{self.endpoint}/v2{self.prefix_path}/{path.lstrip('/')}"

    def url(self, path: str) -> str:
        """普通接口 URL（不带前缀）。"""
        return f"{self.endpoint}/v2/{path.lstrip('/')}"


EDITIONS: dict[str, Edition] = {
    "cn": Edition(
        id="cn",
        label="国内版",
        endpoint="https://copilot.tencent.com",
        ua_platform="WorkBuddy",
        product_name="WorkBuddy",
    ),
    "intl": Edition(
        id="intl",
        label="国际版",
        endpoint="https://www.workbuddy.ai",
        ua_platform="WorkBuddy",
        product_name="WorkBuddy AI",
    ),
}
DEFAULT_EDITION = "cn"

#: 允许出站访问的主机白名单 —— **所有**请求必须命中其中之一。
#:
#: 这道校验是必需的，不是走形式：账号令牌会随请求头发出去，一旦有人能影响
#: 出站目标（篡改登录态文件里的 domain、将来新增功能时误用了外部传入的 URL），
#: 令牌就会泄露给第三方。把目标主机收紧成常量集合，这类问题从根上不可能发生。
#:
#: 白名单外的域名一律拒绝，即使它看起来像官方（如 www.workbuddy.ai.evil.com）。
ALLOWED_HOSTS: frozenset[str] = frozenset({
    "copilot.tencent.com",
    "www.workbuddy.ai",
    "staging-copilot.tencent.com",
    "staging-codebuddy.tencent.com",
})


def validate_outbound_url(url: str) -> str:
    """校验出站 URL：必须 https，且主机在白名单内。返回原 URL。

    长度也做个上限，避免构造超长 URL 打上游。
    """
    if not isinstance(url, str) or not url:
        raise UpstreamError("拒绝空的出站 URL")
    if len(url) > 2048:
        raise UpstreamError("拒绝超长出站 URL")
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as e:
        raise UpstreamError(f"出站 URL 无法解析：{e}") from e
    if parsed.scheme != "https":
        # 只允许 https：http 会把令牌明文暴露在网络里
        raise UpstreamError(f"拒绝非 HTTPS 出站请求：{parsed.scheme or '(无协议)'}")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise UpstreamError(f"拒绝访问白名单外的主机：{host or '(空)'}")
    return url


_CN_HINTS = ("tencent.com", "codebuddy.ai", "codebuddy.cn")
_INTL_HINTS = ("workbuddy.ai",)


def resolve_edition(value: str | None = None) -> Edition:
    """宽松解析版本：接受 cn/intl 及常见写法，未知值回落国内版。"""
    key = str(value or "").strip().lower()
    if not key:
        return EDITIONS[DEFAULT_EDITION]
    if key in EDITIONS:
        return EDITIONS[key]
    if key in ("international", "global", "ai", "workbuddy-ai", "workbuddyai", "en"):
        return EDITIONS["intl"]
    if key in ("china", "mainland", "workbuddy", "zh", "cn-intl"):
        return EDITIONS["cn"]
    return EDITIONS[DEFAULT_EDITION]


def edition_from_domain(domain: str | None) -> Edition:
    """按登录态里的 domain 判版本 —— 这是最可靠的依据。"""
    d = str(domain or "").lower()
    if any(h in d for h in _INTL_HINTS):
        return EDITIONS["intl"]
    if any(h in d for h in _CN_HINTS):
        return EDITIONS["cn"]
    return EDITIONS[DEFAULT_EDITION]


def local_client_version() -> str:
    """从本机客户端读版本，避免写死。"""
    try:
        f = paths.workbuddy_dir() / "last-launch.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            v = str(data.get("version") or "").strip()
            if v:
                return v
    except Exception:
        pass
    return FALLBACK_CLIENT_VERSION


def user_agent(edition: Edition, version: str | None = None) -> str:
    """完整三段 UA：产品名/版本 产品名/版本 CLI/版本。"""
    v = version or local_client_version()
    return f"{edition.ua_platform}/{v} {edition.product_name}/{v} CLI/{CLI_VERSION}"


# --------------------------------------------------------------------------
# 错误类型
# --------------------------------------------------------------------------


class UpstreamError(Exception):
    """上游调用失败。带 HTTP 状态码与上游业务码，便于上层分辨原因。"""

    def __init__(self, message: str, status: int = 0, code: int | None = None,
                 body: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body

    def __str__(self) -> str:
        bits = [super().__str__()]
        if self.status:
            bits.append(f"HTTP {self.status}")
        if self.code is not None:
            bits.append(f"code {self.code}")
        return " · ".join(bits)


def explain_code(code: int | None, msg: str = "") -> str:
    """把上游业务码翻成可读原因。"""
    table = {
        CODE_RATE_LIMITED: "上游频率风控中（稍后再试）",
        CODE_RETRY_FETCH_TOKEN: "登录态待刷新",
        CODE_ACCOUNT_BLOCKED: "账号信息获取失败",
        CODE_LICENSE_EXPIRED: "套餐已过期",
        CODE_TRIAL_EXPIRED: "试用已结束",
        CODE_IP_LIMIT: "IP 受限",
        CODE_NON_STREAM_NOT_SUPPORTED: "上游不支持非流式",
        CODE_CHECKIN_INACTIVE: "签到活动未开启或已过期",
    }
    if code is None:
        return msg or "未知错误"
    return table.get(code, msg or f"上游返回 code {code}")


# --------------------------------------------------------------------------
# 会话（一个账号的调用凭证）
# --------------------------------------------------------------------------


@dataclass
class Session:
    """一次上游调用所需的账号信息。令牌不落日志。"""

    uid: str
    access_token: str
    domain: str = ""
    nickname: str = ""
    enterprise_id: str = ""
    refresh_token: str = ""
    expires_at: int = 0
    edition: Edition = field(default_factory=lambda: EDITIONS[DEFAULT_EDITION])

    @classmethod
    def from_file(cls, path) -> "Session":
        """从登录态文件（auth/*.info）构造。"""
        data = json.loads(path.read_text(encoding="utf-8"))
        acc = data.get("account") or {}
        auth = data.get("auth") or {}
        domain = str(auth.get("domain") or "")
        return cls(
            uid=str(acc.get("uid") or ""),
            access_token=str(auth.get("accessToken") or ""),
            domain=domain,
            nickname=str(acc.get("nickname") or ""),
            enterprise_id=str(acc.get("enterpriseId") or ""),
            refresh_token=str(auth.get("refreshToken") or ""),
            expires_at=int(auth.get("expiresAt") or 0),
            edition=edition_from_domain(domain),
        )

    @classmethod
    def from_account(cls, acc) -> "Session | None":
        """从本工具的账号档案构造（读档案里存的登录态快照）。"""
        snap = acc.session_dir()
        if not snap.exists():
            return None
        files = [f for f in sorted(snap.glob("*.info")) if f.is_file()]
        if not files:
            return None
        main = [f for f in files if "." not in f.stem]
        pick = main[0] if main else files[0]
        try:
            return cls.from_file(pick)
        except Exception:
            return None

    def expired(self, skew_ms: int = 0) -> bool:
        if not self.expires_at:
            return False
        return self.expires_at - skew_ms <= int(time.time() * 1000)

    def label(self) -> str:
        return self.nickname or (self.uid[:8] if self.uid else "?")


# --------------------------------------------------------------------------
# 请求
# --------------------------------------------------------------------------


def _random_trace_id() -> str:
    return str(uuid.uuid4())


def build_headers(
    session: Session,
    *,
    version: str | None = None,
    accept_language: str = "zh-CN",
    client_identity: bool = True,
) -> dict[str, str]:
    """按官方桌面端行为构造请求头。

    client_identity=True 时带完整客户端身份头（`X-IDE-*` / `X-Product` / UA）。
    计费与签到接口只带鉴权头也够，但带上更稳妥 —— 上游对通道有校验。
    """
    ed = session.edition
    trace = _random_trace_id()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Language": accept_language,
        "Authorization": f"Bearer {session.access_token}",
        "X-User-Id": session.uid,
        "X-Request-ID": trace,
        "X-Conversation-Request-ID": trace,
        "X-Conversation-ID": trace,
        "X-Session-ID": trace,
    }
    if session.enterprise_id:
        headers["X-Enterprise-Id"] = session.enterprise_id
        headers["X-Tenant-Id"] = session.enterprise_id
    if session.domain:
        headers["X-Domain"] = session.domain
    if client_identity:
        v = version or local_client_version()
        headers.update(
            {
                "User-Agent": user_agent(ed, v),
                "X-IDE-Type": ed.ua_platform,
                "X-IDE-Name": ed.product_name,
                "X-IDE-Version": v,
                "X-Product": ed.product_name,
                "X-Agent-Intent": "craft",
            }
        )
    return headers


@dataclass
class Response:
    """一次上游调用的结果。"""

    status: int
    code: int | None
    message: str
    data: Any
    raw: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.code == CODE_OK or (self.code is None and 200 <= self.status < 300)


class Client:
    """上游 HTTP 客户端。

    · 超时可配（默认 20 秒，与官方客户端量级一致）
    · 频率风控（11128）自动退避重试，间隔拉长避免续期黑名单
    · 所有调用串行友好；批量场景由上层控制并发（签到必须串行）
    """

    def __init__(self, timeout: float = 20.0, retries_on_ratelimit: int = 2,
                 verbose=None, transport=None):
        self.timeout = timeout
        self.retries_on_ratelimit = retries_on_ratelimit
        self.verbose = verbose or (lambda *_: None)
        #: 测试注入点：替换实际发送（假上游）。签名 (url, data, headers, method) -> Response
        self._transport = transport

    # ---- 底层发送 ----

    def _send(self, url: str, payload: dict | None, headers: dict[str, str],
              method: str = "POST") -> Response:
        # 出口唯一收口点：任何出站请求都要先过主机白名单，防止令牌被发到别处
        validate_outbound_url(url)

        if self._transport is not None:
            return self._transport(url, payload, headers, method)

        body = None
        if method not in ("GET", "HEAD"):
            body = json.dumps(payload if payload is not None else {}, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as r:
                text = r.read().decode("utf-8", "replace")
                status = r.status
                hdrs = dict(r.headers.items())
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            status = e.code
            hdrs = dict(getattr(e, "headers", {}) or {})
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
            raise UpstreamError(f"网络请求失败：{e}") from e

        parsed: dict = {}
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                parsed = obj
        except Exception:
            pass

        code = parsed.get("code")
        msg = str(parsed.get("msg") or parsed.get("message") or "")
        data = parsed.get("data")
        return Response(status=status, code=code if isinstance(code, int) else None,
                        message=msg, data=data, raw=text, headers=hdrs)

    def call(
        self,
        session: Session,
        url: str,
        *,
        payload: dict | None = None,
        method: str = "POST",
        accept_language: str = "zh-CN",
        raise_on_error: bool = False,
    ) -> Response:
        """发起一次调用，命中风控时退避重试。"""
        headers = build_headers(session, accept_language=accept_language)
        attempt = 0
        while True:
            self.verbose(f"→ {method} {url}")
            resp = self._send(url, payload, headers, method)
            self.verbose(f"← HTTP {resp.status} code={resp.code} {resp.message[:80]}")

            if resp.code == CODE_RATE_LIMITED and attempt < self.retries_on_ratelimit:
                delay = WAF_RETRY_DELAYS[min(attempt, len(WAF_RETRY_DELAYS) - 1)]
                self.verbose(f"   命中风控 {CODE_RATE_LIMITED}，{delay:.0f}s 后重试")
                time.sleep(delay)
                attempt += 1
                continue

            if raise_on_error and not resp.ok:
                raise UpstreamError(explain_code(resp.code, resp.message),
                                    status=resp.status, code=resp.code, body=resp.raw)
            return resp

    # ---- 健康检查 ----

    def health(self, session: Session) -> dict:
        """只读的连通性检查：拿签到活动状态当探针（不领取）。"""
        out = {
            "ok": False,
            "status": 0,
            "code": None,
            "message": "",
            "edition": session.edition.id,
            "endpoint": session.edition.endpoint,
        }
        try:
            resp = self.call(session, session.edition.url("billing/meter/checkin-activity-status"))
            out.update(status=resp.status, code=resp.code, message=resp.message)
            out["ok"] = resp.ok
        except UpstreamError as e:
            out["message"] = str(e)
        return out
