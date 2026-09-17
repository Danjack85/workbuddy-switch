# -*- coding: utf-8 -*-
"""积分 / 签到 / 额度查询。

接口（都挂在 `{endpoint}/v2/billing/meter/...`，**不带** `/plugin` 前缀）：

| 接口 | 用途 |
| --- | --- |
| `POST checkin-activity-status` | 签到活动状态（只读探针，不领取） |
| `POST daily-checkin` | 领取每日签到积分（幂等） |
| `POST get-user-resource` | 个人账号积分包（日额度/月会员/加油包…） |
| `POST get-enterprise-user-usage` | 企业账号额度 |

字段名以**实测响应**为准，不是照抄文档。签到状态的实际返回形如：

```json
{"code":0,"msg":"OK","data":{
  "active":false, "today_checked_in":false, "streak_days":0,
  "daily_credit":100, "today_credit":100, "total_credits":0,
  "start_time":"", "end_time":"", "theme_name":"Buddy 加油站",
  "season":1, "activity_name":"本期：专家能量包",
  "claim_button_text":"立即领取", "week_progress":[false,...]}}
```

注意 `active=false` + 起止时间为空 = 活动未开启；此时领取会返回
`code 10001 签到活动未开启或已过期`，属于正常业务状态而非故障。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import upstream
from .upstream import CODE_CHECKIN_INACTIVE, CODE_OK, Client, Session, UpstreamError

#: WorkBuddy 产品码（个人积分包查询用）
PRODUCT_CODE = "p_tcaca"


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class CheckinStatus:
    """签到活动状态。"""

    active: bool = False
    checked_in: bool = False
    streak_days: int = 0
    total_days: int = 0
    daily_credit: int = 0
    today_credit: int = 0
    total_credits: int = 0
    theme_name: str = ""
    activity_name: str = ""
    season: int = 0
    start_time: str = ""
    end_time: str = ""
    week_progress: list[bool] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_data(cls, data: Any) -> "CheckinStatus":
        d = data if isinstance(data, dict) else {}
        def i(key: str) -> int:
            try:
                return int(d.get(key) or 0)
            except (TypeError, ValueError):
                return 0
        prog = d.get("week_progress")
        return cls(
            active=bool(d.get("active")),
            checked_in=bool(d.get("today_checked_in")),
            streak_days=i("streak_days"),
            total_days=i("total_days"),
            daily_credit=i("daily_credit"),
            today_credit=i("today_credit"),
            total_credits=i("total_credits"),
            theme_name=str(d.get("theme_name") or ""),
            activity_name=str(d.get("activity_name") or ""),
            season=i("season"),
            start_time=str(d.get("start_time") or ""),
            end_time=str(d.get("end_time") or ""),
            week_progress=[bool(x) for x in prog] if isinstance(prog, list) else [],
            raw=d,
        )

    def state_text(self) -> str:
        """一句话描述当前状态，供 UI 直接显示。"""
        if not self.active:
            return "活动未开启"
        if self.checked_in:
            return f"今日已签到（连续 {self.streak_days} 天）"
        return f"今日可领 {self.today_credit or self.daily_credit} 积分"


@dataclass
class CreditPack:
    """一个积分包。"""

    name: str = ""
    code: str = ""
    remain: float = 0.0
    total: float = 0.0
    unlimited: bool = False

    def text(self) -> str:
        if self.unlimited:
            return f"{self.name} 不限量"
        return f"{self.name} {self.remain:g}/{self.total:g}"


@dataclass
class CreditSummary:
    """账号额度总览。"""

    kind: str = ""                 # personal | enterprise
    remain: float = 0.0
    total: float = 0.0
    used: float = 0.0
    unlimited: bool = False
    packs: list[CreditPack] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def text(self) -> str:
        if self.unlimited:
            return "不限量"
        if not self.total:
            return "—"
        return f"{self.remain:g}/{self.total:g}"


# --------------------------------------------------------------------------
# 结果包装
# --------------------------------------------------------------------------


@dataclass
class CheckinResult:
    """一次签到尝试的结果。"""

    uid: str = ""
    label: str = ""
    success: bool = False
    code: int | None = None
    message: str = ""
    already: bool = False            # 幂等：今天已经领过
    inactive: bool = False           # 活动未开启
    status: CheckinStatus | None = None
    error: str = ""

    def text(self) -> str:
        if self.error:
            return f"失败：{self.error}"
        if self.success:
            return "签到成功"
        if self.already:
            return "今日已签到"
        if self.inactive:
            return "活动未开启"
        return f"未成功：{self.message or '未知原因'}"


# --------------------------------------------------------------------------
# 业务
# --------------------------------------------------------------------------


class Billing:
    """积分与签到。串行调用 —— 批量时绝不并发，避免触发上游风控。"""

    def __init__(self, client: Client | None = None, verbose=None):
        self.client = client or Client(verbose=verbose)
        self.verbose = verbose or (lambda *_: None)

    # ---- 签到状态（只读） ----

    def checkin_status(self, session: Session) -> CheckinStatus | None:
        resp = self.client.call(
            session, session.edition.url("billing/meter/checkin-activity-status")
        )
        if resp.code != CODE_OK or not resp.data:
            return None
        return CheckinStatus.from_data(resp.data)

    # ---- 领取 ----

    def claim(self, session: Session) -> CheckinResult:
        """领取每日签到积分。

        幂等：今天已领过时上游返回非 0 码，这里识别为 `already` 而非失败。
        活动未开启时上游返回 10001，识别为 `inactive`，同样不算错误。
        """
        result = CheckinResult(uid=session.uid, label=session.label())
        try:
            resp = self.client.call(
                session, session.edition.url("billing/meter/daily-checkin")
            )
        except UpstreamError as e:
            result.error = str(e)
            return result

        result.code = resp.code
        result.message = resp.message

        if resp.code == CODE_OK and resp.data:
            result.success = True
            result.status = CheckinStatus.from_data(resp.data)
            return result

        # 非 0 码：区分「已领过」与「活动未开启」，两者都不该显示成红色错误
        if resp.code == CODE_CHECKIN_INACTIVE:
            result.inactive = True
        elif _looks_already_claimed(resp.message):
            result.already = True
        else:
            result.message = upstream.explain_code(resp.code, resp.message)
        return result

    # ---- 签到并回报积分 ----

    def claim_and_report(self, session: Session) -> tuple[CheckinResult, CreditSummary | None]:
        """签到之后顺手查一次额度（签到会改变积分）。"""
        before = self.checkin_status(session)
        result = self.claim(session)
        # 额度可能因签到变化，稍等一下再查
        if result.success:
            time.sleep(0.5)
        summary = None
        try:
            summary = self.credits(session)
        except UpstreamError:
            pass
        if result.status is None:
            result.status = before
        if summary is not None and before is not None and result.status is not None:
            result.status.total_credits = result.status.total_credits or int(summary.remain)
        return result, summary

    # ---- 额度 ----

    def credits(self, session: Session) -> CreditSummary:
        """查额度。企业账号走企业接口，个人账号走资源包接口。"""
        if session.enterprise_id:
            return self._enterprise_credits(session)
        return self._personal_credits(session)

    def _personal_credits(self, session: Session) -> CreditSummary:
        payload = {
            "PageNumber": 1,
            "PageSize": 100,
            "ProductCode": PRODUCT_CODE,
            "Status": [0, 3],
            "OnlyValidPeriod": True,
        }
        resp = self.client.call(
            session, session.edition.url("billing/meter/get-user-resource"), payload=payload
        )
        if resp.code != CODE_OK:
            raise UpstreamError(upstream.explain_code(resp.code, resp.message),
                                status=resp.status, code=resp.code, body=resp.raw)

        data = resp.data or {}
        # 结果路径：data.Response.Data.Accounts[]
        accounts = (((data.get("Response") or {}).get("Data") or {}).get("Accounts")) or []
        packs: list[CreditPack] = []
        remain = total = 0.0
        for r in accounts if isinstance(accounts, list) else []:
            if not isinstance(r, dict):
                continue
            left = _num(r.get("CycleCapacityRemainPrecise"))
            cap = _num(r.get("CycleCapacitySizePrecise"))
            code = str(r.get("PackageCode") or "")
            packs.append(
                CreditPack(
                    name=str(r.get("PackageName") or r.get("ResourceName") or code or "积分"),
                    code=code,
                    remain=left,
                    total=cap,
                )
            )
            remain += left
            total += cap
        return CreditSummary(
            kind="personal", remain=remain, total=total,
            used=max(total - remain, 0.0), packs=packs, raw=data,
        )

    def _enterprise_credits(self, session: Session) -> CreditSummary:
        resp = self.client.call(
            session, session.edition.url("billing/meter/get-enterprise-user-usage")
        )
        if resp.code != CODE_OK:
            raise UpstreamError(upstream.explain_code(resp.code, resp.message),
                                status=resp.status, code=resp.code, body=resp.raw)
        d = resp.data or {}
        limit = _num(d.get("limitNum"))
        credit = _num(d.get("credit"))
        used = max(limit - credit, 0.0)
        return CreditSummary(
            kind="enterprise", remain=credit, total=limit, used=used, raw=d,
        )


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _looks_already_claimed(message: str) -> bool:
    """上游对「今天已领过」没有专用码，只能看文案。"""
    m = str(message or "")
    return any(k in m for k in ("已签到", "已领取", "重复", "already"))


# --------------------------------------------------------------------------
# 批量签到
# --------------------------------------------------------------------------


@dataclass
class BatchReport:
    """批量签到结果。"""

    results: list[CheckinResult] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (账号名, 原因)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def already_count(self) -> int:
        return sum(1 for r in self.results if r.already)

    @property
    def inactive_count(self) -> int:
        return sum(1 for r in self.results if r.inactive)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not (r.success or r.already or r.inactive))

    def summary(self) -> str:
        bits = [f"成功 {self.ok_count}"]
        if self.already_count:
            bits.append(f"已签到 {self.already_count}")
        if self.inactive_count:
            bits.append(f"活动未开启 {self.inactive_count}")
        if self.fail_count:
            bits.append(f"失败 {self.fail_count}")
        if self.skipped:
            bits.append(f"跳过 {len(self.skipped)}")
        return " · ".join(bits)


def sessions_from_accounts(accounts) -> tuple[list[tuple[str, Session]], list[tuple[str, str]]]:
    """从账号档案里取出可用会话。

    返回 (可用列表, 跳过列表)；跳过原因用于向用户解释为什么某个账号没签。
    """
    usable: list[tuple[str, Session]] = []
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for acc in accounts:
        if acc.uid in seen:
            continue
        seen.add(acc.uid)
        session = Session.from_account(acc)
        if session is None or not session.access_token:
            skipped.append((acc.name, "没有登录态快照"))
            continue
        if session.expired():
            skipped.append((acc.name, "登录凭据已过期"))
            continue
        usable.append((acc.name, session))
    return usable, skipped


def claim_all(accounts, client: Client | None = None, verbose=None,
              on_progress=None) -> BatchReport:
    """给所有可用账号签到。

    **串行执行**：多个账号同时打上游会触发频率风控（code 11128），
    而且拉黑期间的重试会给黑名单续期。所以这里一个接一个来，
    宁可慢也不要形成重试风暴。
    """
    billing = Billing(client, verbose=verbose)
    report = BatchReport()
    usable, skipped = sessions_from_accounts(accounts)
    report.skipped = skipped

    for idx, (name, session) in enumerate(usable, 1):
        if on_progress:
            on_progress(idx, len(usable), name)
        result = billing.claim(session)
        if not result.label:
            result.label = name
        report.results.append(result)
    return report

