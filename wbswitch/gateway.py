# -*- coding: utf-8 -*-
"""OpenAI 兼容网关：把 WorkBuddy 的模型额度变成标准的 `/v1` 接口。

有自制客户端的说明：这是本工具里最「重」的一块，它让任何支持自定义
`base_url` 的 OpenAI 客户端（Cherry Studio / Cursor / 各类 SDK）都能用你
自己账号的额度，端点填 `http://127.0.0.1:3065/v1`。

## 数据流

```
OpenAI 客户端
   │  POST /v1/chat/completions
   ▼
本网关（127.0.0.1:3065）        选账号 · 429 降级 · 非流式聚合 · 断连取消
   │  HTTPS
   ▼
{endpoint}/v2/chat/completions        国内版 copilot.tencent.com
                                      国际版 www.workbuddy.ai
```

## 三个必须处理的坑

1. **上游只支持流式**：请求里写 `stream:false` 会被上游拒绝（`code 11101`）。
   因此内部一律以 `stream:true` 发出去；调用方要非流式时，我们把 SSE 聚合成
   一个完整的 `chat.completion` 返回。聚合要点：`content` 与 `reasoning_content`
   按序拼接、`tool_calls` 按 `index` 合并且 `arguments` 也要拼、`usage` 取最后
   一次出现的。

2. **首条消息必须是 system**：否则上游返回 400 `first message is not system prompt`。
   调用方没带时补一条兜底系统消息。

3. **限额是「账号 × 模型」维度的**：某个账号对某模型触顶（HTTP 429 /
   `code 6004`）时，换别的模型可能仍然可用。恢复时间只写在上游的提示文本里
   （`...将在 2026-09-11 19:43:46 UTC+8 重置...`），需要正则解析。命中后标记
   冷却并降级到下一个账号；请求成功即清除标记。

## 与参考实现的一处不同

参考实现把「思考分片」攒到 60 字符以上再下发，避免客户端把思考渲染成碎块。
这里也做了（`_Coalescer`），但只作用于 `reasoning_content`，`content` 原样透传 ——
正文分片必须实时到达，否则流式体验会变卡。
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

from . import paths, profiles, upstream
from .upstream import Client, Session, UpstreamError

#: 上游限额码（HTTP 429）：账号×模型维度的用量限额
CODE_QUOTA_LIMIT = 6004
#: 上游要求首条消息为 system，否则 400
DEFAULT_SYSTEM_PROMPT = "你是一个得力助手"
#: 思考分片攒到这么多字符再下发
REASONING_COALESCE_CHARS = 60
#: 模型目录缓存时间（秒）
MODEL_CACHE_TTL = 600


# --------------------------------------------------------------------------
# 模型目录
# --------------------------------------------------------------------------

#: 内置兜底清单：字段与上游 `/v3/config` 的 models[] 对齐。
#: 远程清单可用时以远程为准，这份只在首次运行/离线时兜底。
BUILTIN_MODELS: list[dict] = [
    {"id": "auto", "name": "Auto", "maxInputTokens": 168000, "maxOutputTokens": 32000,
     "isDefault": True, "descriptionZh": "自动选择，平衡效果与速度"},
    {"id": "default-model", "name": "Default", "maxInputTokens": 176000,
     "maxOutputTokens": 24000, "isDefault": True, "descriptionZh": "默认模型"},
    {"id": "fast-model", "name": "Fast", "maxInputTokens": 200000,
     "maxOutputTokens": 32000, "credits": "x0.34 credits", "descriptionZh": "响应快，适合简单任务"},
    {"id": "balanced-model", "name": "Balanced", "maxInputTokens": 256000,
     "maxOutputTokens": 32000, "credits": "x0.59 credits", "descriptionZh": "速度与质量兼顾"},
    {"id": "primary-model", "name": "Primary", "maxInputTokens": 272000,
     "maxOutputTokens": 72000, "credits": "x3.31 credits", "descriptionZh": "高质量输出"},
    {"id": "deep-model", "name": "Deep", "maxInputTokens": 176000,
     "maxOutputTokens": 24000, "credits": "x3.33 credits"},
    {"id": "deepseek-v4.1-flash", "name": "DeepSeek-V4.1-Flash", "maxInputTokens": 1000000,
     "maxOutputTokens": 128000, "credits": "x0.00", "descriptionZh": "免费额度模型"},
    {"id": "gpt-5.4", "name": "GPT-5.4", "maxInputTokens": 272000,
     "maxOutputTokens": 72000, "credits": "x1.65 credits"},
    {"id": "glm-5.3", "name": "GLM-5.3", "maxInputTokens": 1000000,
     "maxOutputTokens": 48000, "credits": "x0.79 credits"},
    {"id": "gemini-3.5-flash", "name": "Gemini-3.5-Flash", "maxInputTokens": 1000000,
     "maxOutputTokens": 65536, "credits": "x0.99 credits"},
    {"id": "kimi-k2.6", "name": "Kimi-K2.6", "maxInputTokens": 256000,
     "maxOutputTokens": 32000, "credits": "x0.52 credits"},
]

#: id 里含这些片段的是非对话模型，不对外暴露
_EXCLUDE_RE = re.compile(r"(embed|rerank|image|video|tts|asr|whisper|moderation|vision-only)",
                         re.IGNORECASE)


def is_chat_model(m: dict) -> bool:
    """过滤非对话模型。与参考实现的判定一致：排除特定 id + 要求输出上限够大。"""
    mid = str(m.get("id") or "")
    if not mid or _EXCLUDE_RE.search(mid):
        return False
    if m.get("supportsExtra"):
        return False
    out = m.get("maxOutputTokens")
    if not isinstance(out, int):
        return False
    return out >= 16000


def _normalize_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a or not b:
        return len(a) or len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


class ModelCatalog:
    """模型目录：内置兜底 + 远程刷新 + 磁盘缓存。

    远程清单来自上游 `GET /v3/config` 的 `models[]`（实测 22 个对话模型）。
    拉不到时用磁盘缓存，再不行用内置清单 —— 保证网关在离线/首次运行时也能起。
    """

    def __init__(self, client: Client | None = None, verbose=None):
        self.client = client
        self.verbose = verbose or (lambda *_: None)
        self._models: list[dict] = []
        self._fetched_at = 0.0
        self._lock = threading.Lock()
        self._load_cache()
        if not self._models:
            self._models = [m for m in BUILTIN_MODELS if is_chat_model(m)]

    def cache_path(self):
        return paths.store_dir() / "models.json"

    def _load_cache(self) -> None:
        try:
            f = self.cache_path()
            if not f.exists():
                return
            data = json.loads(f.read_text(encoding="utf-8"))
            ms = data.get("models") if isinstance(data, dict) else None
            if isinstance(ms, list) and ms:
                self._models = [m for m in ms if isinstance(m, dict) and is_chat_model(m)]
                self._fetched_at = float(data.get("fetched_at") or 0)
        except Exception:
            pass

    def _save_cache(self) -> None:
        try:
            paths.ensure_store_dirs()
            self.cache_path().write_text(
                json.dumps({"fetched_at": self._fetched_at, "models": self._models},
                           ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception:
            pass

    def refresh(self, session: Session, force: bool = False) -> bool:
        """从上游刷新模型清单。返回是否刷新成功。"""
        with self._lock:
            if not force and self._models and (time.time() - self._fetched_at) < MODEL_CACHE_TTL:
                return True
        try:
            resp = self.client.call(session, f"{session.edition.endpoint}/v3/config",
                                    method="GET") if self.client else None
            if resp is None or resp.raw is None:
                return False
            data = json.loads(resp.raw)
            models = ((data or {}).get("data") or {}).get("models")
            if not isinstance(models, list):
                return False
            chat = [m for m in models if isinstance(m, dict) and is_chat_model(m)]
            if not chat:
                return False
            with self._lock:
                self._models = chat
                self._fetched_at = time.time()
            self._save_cache()
            self.verbose(f"模型目录已刷新：{len(chat)} 个")
            return True
        except Exception as e:
            self.verbose(f"模型目录刷新失败（沿用缓存）：{e}")
            return False

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._models)

    def has(self, model_id: str) -> bool:
        return any(str(m.get("id")) == model_id for m in self.all())

    def default_id(self) -> str:
        for m in self.all():
            if m.get("isDefault"):
                return str(m.get("id"))
        for m in self.all():
            if m.get("id") == "auto":
                return "auto"
        ms = self.all()
        return str(ms[0].get("id")) if ms else "auto"

    def resolve(self, requested: str | None) -> tuple[str | None, list[str]]:
        """解析调用方点名的模型。

        返回 (解析后的 id, 近似名建议)。**不做静默回退** —— 点名不存在的模型
        直接判失败，因为「请求 A 实跑 B」是很难被察觉的事故。
        `auto` / 空值走默认模型（这是唯一允许的替换，且调用方明确写了 auto）。
        """
        rid = str(requested or "").strip()
        if not rid or rid == "auto":
            return self.default_id(), []
        if self.has(rid):
            return rid, []
        # 近似名建议：归一化后取编辑距离最近的几个
        target = _normalize_id(rid)
        scored = sorted(
            ((_edit_distance(target, _normalize_id(str(m.get("id")))), str(m.get("id")))
             for m in self.all()),
            key=lambda x: x[0],
        )
        near = [mid for d, mid in scored[:3] if d <= max(4, len(target) // 3)]
        return None, near


# --------------------------------------------------------------------------
# 限额冷却
# --------------------------------------------------------------------------


@dataclass
class RateLimit:
    reset_at: float = 0.0     # 秒级时间戳
    code: int | None = None
    status: int = 429
    message: str = ""

    def active(self, now: float | None = None) -> bool:
        return self.reset_at > (now if now is not None else time.time())


class RateLimitStore:
    """「账号 × 模型」维度的限额冷却。请求成功即清除。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str], RateLimit] = {}

    def get(self, uid: str, model: str) -> RateLimit | None:
        with self._lock:
            rl = self._data.get((uid, model))
        return rl if rl and rl.active() else None

    def mark(self, uid: str, model: str, rl: RateLimit) -> None:
        with self._lock:
            self._data[(uid, model)] = rl

    def clear(self, uid: str, model: str) -> None:
        with self._lock:
            self._data.pop((uid, model), None)

    def snapshot(self) -> list[dict]:
        now = time.time()
        with self._lock:
            items = list(self._data.items())
        out = []
        for (uid, model), rl in items:
            if rl.active(now):
                out.append({"uid": uid, "model": model, "status": rl.status,
                            "code": rl.code, "reset_at": rl.reset_at,
                            "message": rl.message[:120]})
        return out

    def earliest(self, model: str) -> RateLimit | None:
        """该模型下恢复最早的那条冷却记录（全部账号都限额时用它再试一次）。"""
        now = time.time()
        with self._lock:
            cands = [rl for (_, m), rl in self._data.items() if m == model and rl.active(now)]
        return min(cands, key=lambda r: r.reset_at) if cands else None


_RESET_RE = re.compile(
    r"(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2}):(\d{2})"
)


def parse_reset_at(message: str, now: float | None = None) -> float:
    """从上游提示文本解析恢复时间（形如 `将在 2026-09-11 19:43:46 UTC+8 重置`）。

    文本里是 UTC+8 墙上时间，用 `time.mktime` 按本地时区解析 —— 本工具面向的
    用户就在 UTC+8，这样得到的就是正确的绝对时刻。解析不到时给一个保守的
    5 分钟后（避免把账号永久锁死）。
    """
    m = _RESET_RE.search(str(message or ""))
    if not m:
        return (now if now is not None else time.time()) + 300.0
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    try:
        import calendar
        return float(calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)) - 8 * 3600)
    except Exception:
        return (now if now is not None else time.time()) + 300.0


# --------------------------------------------------------------------------
# 账号选路
# --------------------------------------------------------------------------


def load_sessions() -> list[tuple[str, Session]]:
    """从账号档案取出可用会话，按档案顺序（优先级）排列。"""
    out: list[tuple[str, Session]] = []
    seen: set[str] = set()
    for acc in profiles.list_accounts():
        if acc.uid in seen:
            continue
        seen.add(acc.uid)
        s = Session.from_account(acc)
        if s and s.access_token and not s.expired():
            out.append((acc.name, s))
    return out


# --------------------------------------------------------------------------
# SSE 处理
# --------------------------------------------------------------------------


class _Coalescer:
    """把过小的思考分片攒起来再发，避免客户端把思考渲染成一堆碎块。

    只作用于 `reasoning_content`；`content` 必须原样实时透传。
    """

    def __init__(self, min_chars: int = REASONING_COALESCE_CHARS):
        self.min_chars = min_chars
        self._buf = ""

    def feed(self, chunk: dict) -> list[dict]:
        """输入一个 SSE chunk，返回可以立即下发的 chunk 列表。"""
        try:
            delta = chunk.get("choices", [{}])[0].get("delta") or {}
        except Exception:
            return [chunk]
        piece = delta.get("reasoning_content")
        if not isinstance(piece, str):
            # 非文本增量：先把攒着的思考冲出去，再原样下发
            out = self.flush()
            out.append(chunk)
            return out

        self._buf += piece
        if len(self._buf) < self.min_chars:
            # 还没攒够：本 chunk 只发非思考部分
            rest = dict(delta)
            rest.pop("reasoning_content", None)
            if not rest:
                return []
            return [{"id": chunk.get("id"), "object": chunk.get("object", "chat.completion.chunk"),
                     "created": chunk.get("created"), "model": chunk.get("model"),
                     "choices": [{"index": 0, "delta": rest,
                                  "finish_reason": chunk.get("choices", [{}])[0].get("finish_reason")}]}]
        return self.flush()

    def flush(self) -> list[dict]:
        if not self._buf:
            return []
        text, self._buf = self._buf, ""
        return [{"object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"reasoning_content": text},
                              "finish_reason": None}]}]


def iter_sse_events(response) -> Iterator[dict]:
    """从上游响应里逐行读 `data:` 事件并解析成 chunk。

    用 `readline()` 而不是 `for line in response` —— 后者会启用预读缓冲，
    把本该立刻到达的首 token 卡在缓冲区里，流式就失去意义了。
    """
    while True:
        raw = response.readline()
        if not raw:
            return
        line = raw.decode("utf-8", "replace").strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if isinstance(obj, dict):
            yield obj


def aggregate_chunks(chunks: list[dict]) -> dict:
    """把 SSE chunk 聚合成一个完整的 `chat.completion`。

    · `content` / `reasoning_content` 按序拼接
    · `tool_calls` 按 `index` 合并，`function.arguments` 也要拼（不然 JSON 是断的）
    · `usage` 取最后一次出现的
    """
    cid = cmodel = ""
    created = 0
    role = ""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    finish = ""
    usage = None
    tool_calls: dict[int, dict] = {}

    for ch in chunks:
        if ch.get("error"):
            err = ch["error"]
            raise UpstreamError(str(err.get("message") or "上游流式返回错误"),
                                status=502)
        cid = ch.get("id") or cid
        cmodel = ch.get("model") or cmodel
        created = ch.get("created") or created
        if isinstance(ch.get("usage"), dict):
            usage = ch["usage"]
        choices = ch.get("choices") or []
        if not choices:
            continue
        c0 = choices[0] or {}
        delta = c0.get("delta") or {}
        role = delta.get("role") or role
        if isinstance(delta.get("content"), str):
            content_parts.append(delta["content"])
        if isinstance(delta.get("reasoning_content"), str):
            reasoning_parts.append(delta["reasoning_content"])
        if c0.get("finish_reason"):
            finish = c0["finish_reason"]
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index") if isinstance(tc.get("index"), int) else 0
            cur = tool_calls.setdefault(
                idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
            if tc.get("id"):
                cur["id"] = tc["id"]
            if tc.get("type"):
                cur["type"] = tc["type"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                cur["function"]["name"] += str(fn["name"])
            if fn.get("arguments"):
                cur["function"]["arguments"] += str(fn["arguments"])

    message: dict = {"role": role or "assistant", "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]

    return {
        "id": cid or f"wb-agg-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": cmodel,
        "choices": [{"index": 0, "message": message, "finish_reason": finish or "stop"}],
        "usage": usage,
    }


# --------------------------------------------------------------------------
# 网关核心
# --------------------------------------------------------------------------


@dataclass
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 3065
    api_key: str = ""              # 为空则不校验（仅监听回环时安全）
    default_model: str = "auto"


@dataclass
class GatewayStats:
    requests: int = 0
    ok: int = 0
    failed: int = 0
    downgrades: int = 0
    started_at: float = field(default_factory=time.time)


class Gateway:
    """OpenAI 兼容网关的核心逻辑（与 HTTP 层解耦，便于测试）。"""

    def __init__(self, config: GatewayConfig | None = None, client: Client | None = None,
                 verbose=None, transport=None):
        self.config = config or GatewayConfig()
        self.verbose = verbose or (lambda *_: None)
        self.client = client or Client(verbose=self.verbose, transport=transport)
        self.catalog = ModelCatalog(self.client, verbose=self.verbose)
        self.limits = RateLimitStore()
        self.stats = GatewayStats()

    # ---- 模型 ----

    def models_payload(self) -> dict:
        """OpenAI 的 `/v1/models` 格式，附带 credits 与上下文长度。"""
        data = []
        for m in self.catalog.all():
            item = {
                "id": str(m.get("id")),
                "object": "model",
                "created": int(self.stats.started_at),
                "owned_by": "workbuddy",
            }
            if m.get("name"):
                item["name"] = m["name"]
            if m.get("credits"):
                item["credits"] = m["credits"]
            if isinstance(m.get("maxInputTokens"), int):
                item["context_window"] = m["maxInputTokens"]
            if isinstance(m.get("maxOutputTokens"), int):
                item["max_output_tokens"] = m["maxOutputTokens"]
            if m.get("descriptionZh"):
                item["description"] = m["descriptionZh"]
            data.append(item)
        return {"object": "list", "data": data}

    def ensure_catalog(self) -> None:
        """首次调用时尝试刷新模型目录（失败也不影响服务）。"""
        sessions = load_sessions()
        if sessions:
            self.catalog.refresh(sessions[0][1])

    # ---- 选路 ----

    def pick(self, model: str) -> tuple[str, Session] | None:
        """按优先级挑一个可用账号，跳过对该模型处于冷却期的账号。

        全部冷却时，挑恢复最早的那个再试一次 —— 上游可能已经实际解除限额，
        试一次比直接报错更贴近真实。
        """
        sessions = load_sessions()
        if not sessions:
            return None
        for name, s in sessions:
            if self.limits.get(s.uid, model) is None:
                return name, s
        if self.limits.earliest(model) is not None:
            return sessions[0]
        return None

    def mark_limited(self, session: Session, model: str, status: int,
                     code: int | None, message: str) -> float:
        reset = parse_reset_at(message)
        self.limits.mark(session.uid, model, RateLimit(
            reset_at=reset, code=code, status=status or 429, message=message))
        self.stats.downgrades += 1
        return reset

    # ---- 请求体准备 ----

    @staticmethod
    def prepare_body(body: dict, model: str) -> dict:
        """整理请求体：注入 system prompt、强制流式、固定模型。

        上游只认 `stream:true`，所以这里无条件改写 —— 调用方要非流式时由我们
        聚合，这个差异不外泄。
        """
        out = dict(body)
        out["model"] = model
        out["stream"] = True

        msgs = out.get("messages")
        if isinstance(msgs, list) and msgs:
            first = str((msgs[0] or {}).get("role") or "").lower()
            if first != "system":
                out["messages"] = [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}, *msgs]
        return out

    def _open_upstream(self, session: Session, body: dict):
        """向上游发起对话请求，返回原始响应对象（供流式读取）。"""
        url = session.edition.url("chat/completions")
        headers = upstream.build_headers(session)
        headers["Accept"] = "text/event-stream"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        return urllib.request.urlopen(req, timeout=300)

    def stream(self, body: dict):
        """流式：把上游 SSE 逐块泵给调用方，首包延迟等于上游首 token 延迟。

        产出 (kind, payload) 二元组：
          ("chunk", dict)   一个要下发的 chunk（已做思考分片合并）
          ("done", None)    正常结束
          ("error", dict)   错误体（遵循 OpenAI 结构，含 __status）

        选账号 / 限额降级的判定发生在**收到首个数据块之前**；一旦开始下发内容
        就不能再换账号了（那样会拼出两个模型的输出）。
        """
        self.stats.requests += 1
        requested = str(body.get("model") or "")
        model, near = self.catalog.resolve(requested)
        if model is None:
            self.stats.failed += 1
            hint = f"，相近的模型：{', '.join(near)}" if near else ""
            yield "error", _err(f"模型 {requested!r} 不存在{hint}", "model_not_found", 400)
            return

        prepared = self.prepare_body(body, model)
        sessions = load_sessions()
        if not sessions:
            self.stats.failed += 1
            yield "error", _err("没有可用的账号登录态，请先在本工具里保全账号", "no_account", 503)
            return

        last_err: dict | None = None
        tried: set[str] = set()
        for _, session in sessions:
            if session.uid in tried:
                continue
            tried.add(session.uid)
            if self.limits.get(session.uid, model) is not None:
                continue

            try:
                resp = self._open_upstream(session, prepared)
            except urllib.error.HTTPError as e:
                detail = _read_error_detail(e)
                code = detail.get("code")
                msg = str(detail.get("msg") or detail.get("message") or str(e))
                if e.code == 429 or code == CODE_QUOTA_LIMIT:
                    reset = self.mark_limited(session, model, e.code, code, msg)
                    self.verbose(f"账号 {session.label()} 对 {model} 限额，"
                                 f"恢复于 {time.strftime('%H:%M:%S', time.localtime(reset))}")
                    last_err = _err(msg or "上游限额", "rate_limit_exceeded", 429,
                                    {"reset_at": int(reset)})
                    continue
                last_err = _err(msg, "upstream_error", e.code or 502)
                continue
            except Exception as e:
                last_err = _err(f"上游请求失败：{e}", "upstream_error", 502)
                continue

            # 开始透传
            coalescer = _Coalescer()
            started = False
            try:
                for ch in iter_sse_events(resp):
                    if ch.get("error"):
                        em = str((ch.get("error") or {}).get("message") or "上游流式返回错误")
                        if started:
                            # 已经下发过内容：以错误块收尾，不换账号
                            self.stats.failed += 1
                            yield "error", _err(em, "upstream_error", 502)
                            return
                        last_err = _err(em, "upstream_error", 502)
                        break

                    if not started:
                        started = True
                        self.limits.clear(session.uid, model)
                        self.stats.ok += 1
                    for out in coalescer.feed(ch):
                        yield "chunk", out
                else:
                    for out in coalescer.flush():
                        yield "chunk", out
                    yield "done", None
                    return
            except CLIENT_GONE:
                # 调用方断开：结束即可，不算失败
                yield "done", None
                return
            except Exception as e:
                if started:
                    self.stats.failed += 1
                    yield "error", _err(f"上游流中断：{e}", "upstream_error", 502)
                    return
                last_err = _err(f"上游请求失败：{e}", "upstream_error", 502)
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

        self.stats.failed += 1
        yield "error", last_err or _err("所有账号均不可用", "no_account", 503)

    # ---- 对话（非流式：内部流式 + 聚合） ----

    def complete(self, body: dict) -> tuple[dict | None, dict | None]:
        """非流式：内部走流式，把 chunk 聚合成一个完整 `chat.completion`。

        复用 `stream()` 的选路与降级逻辑，避免两套实现分叉 —— 上游只支持流式，
        「非流式」本质上就是「把流收完再返回」。
        """
        chunks: list[dict] = []
        for kind, payload in self.stream(body):
            if kind == "chunk":
                chunks.append(payload)
            elif kind == "error":
                return None, payload
            elif kind == "done":
                break
        if not chunks:
            return None, _err("上游没有返回任何内容", "upstream_error", 502)
        try:
            out = aggregate_chunks(chunks)
        except UpstreamError as e:
            return None, _err(str(e), "upstream_error", 502)
        requested = str(body.get("model") or "")
        model, _ = self.catalog.resolve(requested)
        if model:
            out["model"] = out.get("model") or model
        return out, None


# --------------------------------------------------------------------------
# 错误体
# --------------------------------------------------------------------------


def _err(message: str, etype: str, status: int, extra: dict | None = None) -> dict:
    body = {"error": {"message": message, "type": etype, "code": etype}}
    if extra:
        body["error"].update(extra)
    body["__status"] = status
    return body


def _read_error_detail(e: urllib.error.HTTPError) -> dict:
    try:
        raw = e.read().decode("utf-8", "replace")
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


#: 客户端主动断开时可能出现的异常。Windows 上是 ConnectionAbortedError
#: (WinError 10053)，POSIX 上多是 BrokenPipeError/ConnectionResetError —— 
#: 都是 OSError 的子类，统一按「客户端走了」处理，不要打堆栈吓人。
CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError)


# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------


def make_handler(gateway: Gateway):
    """构造 HTTP 处理器（闭包持有 gateway，便于测试注入）。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "WorkBuddySwitchGateway/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 静默：日志走 gateway.verbose
            pass

        # ---- 工具 ----

        def _auth_ok(self) -> bool:
            key = gateway.config.api_key
            if not key:
                return True
            got = self.headers.get("Authorization", "")
            if got.startswith("Bearer "):
                got = got[7:].strip()
            got = got or self.headers.get("X-API-Key", "")
            return got == key

        def _send_json(self, obj: dict, status: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8", "replace"))
            except Exception:
                return {}

        # ---- 路由 ----

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0].rstrip("/") or "/"
            if path == "/health":
                sessions = load_sessions()
                return self._send_json({
                    "ok": True,
                    "accounts": len(sessions),
                    "models": len(gateway.catalog.all()),
                    "default_model": gateway.catalog.default_id(),
                    "auth_required": bool(gateway.config.api_key),
                    "stats": {
                        "requests": gateway.stats.requests,
                        "ok": gateway.stats.ok,
                        "failed": gateway.stats.failed,
                        "downgrades": gateway.stats.downgrades,
                    },
                })
            if path in ("/v1/models", "/models"):
                gateway.ensure_catalog()
                if not self._auth_ok():
                    return self._send_json(_err("API Key 无效", "invalid_api_key", 401), 401)
                return self._send_json(gateway.models_payload())
            if path in ("/v1/limits", "/limits"):
                if not self._auth_ok():
                    return self._send_json(_err("API Key 无效", "invalid_api_key", 401), 401)
                return self._send_json({"limits": gateway.limits.snapshot()})
            self._send_json(_err(f"未实现的路径 {path}", "not_found", 404), 404)

        def do_POST(self):  # noqa: N802
            path = self.path.split("?")[0].rstrip("/") or "/"
            if path not in ("/v1/chat/completions", "/chat/completions"):
                return self._send_json(_err(f"未实现的路径 {path}", "not_found", 404), 404)
            if not self._auth_ok():
                return self._send_json(_err("API Key 无效", "invalid_api_key", 401), 401)

            body = self._read_body()
            if not isinstance(body, dict) or not body.get("messages"):
                return self._send_json(
                    _err("请求体缺少 messages", "invalid_request_error", 400), 400)

            want_stream = bool(body.get("stream"))
            if want_stream:
                return self._stream(body)
            out, err = gateway.complete(body)
            if err is not None:
                status = int(err.pop("__status", 500))
                return self._send_json(err, status)
            return self._send_json(out or {})

        def _stream(self, body: dict) -> None:
            """流式：把 gateway.stream() 产出的 chunk 直接写成 SSE。

            真正的边收边发 —— 首包延迟等于上游首 token 延迟，不是等整段生成完。

            客户端断开时（关掉窗口、按 Esc、切走页面）这里会收到
            ConnectionAbortedError / BrokenPipeError，属于正常情况：
            静默结束，不打印堆栈、不计为失败。
            """
            first = True
            sent_any = False
            try:
                for kind, payload in gateway.stream(body):
                    if kind == "error":
                        err = payload or {}
                        status = int(err.pop("__status", 500))
                        if first:
                            # 还没开始下发：可以正常回一个错误响应
                            return self._send_json(err, status)
                        # 已经下发过内容：以 SSE 错误块收尾（HTTP 头已发出，改不了状态码）
                        self._write_sse({"error": err.get("error", err)})
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                        return

                    if first:
                        # 第一个 chunk 到达时才发响应头，这样错误能走正常 HTTP 状态码
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        first = False

                    if kind == "chunk":
                        self._write_sse(payload)
                        sent_any = True
                    elif kind == "done":
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                        return
            except CLIENT_GONE:
                # 客户端走了：正常结束
                return

            if first and not sent_any:
                # 没有任何产出（理论上不会走到）：给个明确的错误
                return self._send_json(
                    _err("上游没有返回任何内容", "upstream_error", 502), 502)

        def _write_sse(self, obj: dict) -> None:
            self.wfile.write(
                ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")
            )
            self.wfile.flush()

    return Handler


class GatewayServer(ThreadingHTTPServer):
    """多线程 HTTP 服务。

    覆盖 `handle_error`：客户端断开是流式接口的常态（关窗口、按 Esc），
    socketserver 默认会往 stderr 打一整段堆栈，既吵又容易被误当成服务故障。
    这里只吞掉「客户端走了」这类异常，真正的服务端错误照常打印。
    """

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):  # noqa: D102
        import sys
        import traceback

        exc = sys.exc_info()[1]
        if isinstance(exc, CLIENT_GONE) or isinstance(exc, ConnectionError):
            return
        traceback.print_exc()


def serve(gateway: Gateway, ready_callback=None) -> GatewayServer:
    """启动 HTTP 服务（阻塞式在调用方线程运行）。

    只绑定回环地址时无需鉴权；绑定非回环且没设 API Key 会直接拒绝 ——
    那等于把额度开放给同网段所有人。
    """
    cfg = gateway.config
    if cfg.host not in ("127.0.0.1", "localhost", "::1") and not cfg.api_key:
        raise RuntimeError(
            f"监听 {cfg.host} 但未设置 API Key —— 这会把你的账号额度开放给"
            "同网段的任何人。请设置 API Key，或改回只监听 127.0.0.1。"
        )
    httpd = GatewayServer((cfg.host, cfg.port), make_handler(gateway))
    if ready_callback:
        ready_callback(httpd)
    gateway.verbose(f"网关已启动：http://{cfg.host}:{cfg.port}/v1")
    gateway.verbose(f"模型目录：{len(gateway.catalog.all())} 个")
    httpd.serve_forever()
    return httpd
