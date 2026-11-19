"""滴答清单 ICS -> iCloud 日历 (CalDAV) 全自动同步。

设计目标：
- 把手环能读到的「系统内置日历」事件自动写入 iCloud 专用日历（手环只读系统日历，不读订阅/第三方日历）。
- 手环只显示「未来 7 天内、尚未开始」的事件：当天的全天事件因被视为「已开始」而不显示，
  隔天的才显示。因此统一改成事件当天最后一分钟开始的 1 分钟定时事件（默认 23:58→23:59），
  无论是全天事件还是带时间事件，都落在当天 23:58 —— 这样当天的事件也算「未开始」，手环就会显示。
- 幂等 + 防重复：每次先读取目标日历；按 UID 分组，同一 UID 若存在多条（历史重复）会清理到只剩 1 条；
  再按 UID upsert；内容无变化不写；只删除带 X-DIDA-SYNC 标记且源里已不存在的事件，绝不碰别的日历事件。
  用 CLEAR_FIRST=1 可在每次同步前先清空目标日历（目标日历必须专用）。
- 缺 ICLOUD_* 环境变量时安全退出（不报红），方便先部署后配置。
依赖：caldav + icalendar + requests
"""
import os
import re
import sys
import copy
import time
from datetime import datetime, timedelta, timezone, date

import requests
from icalendar import Calendar, Event
from icalendar.prop import vDatetime

try:
    import caldav
except Exception:  # caldav 未安装时也能 --dry-run
    caldav = None

DIDA_ICS_URL = os.environ.get("DIDA_ICS_URL", "")
ICLOUD_USERNAME = os.environ.get("ICLOUD_USERNAME", "")
ICLOUD_APP_PASSWORD = os.environ.get("ICLOUD_APP_PASSWORD", "")
ICLOUD_CALENDAR = os.environ.get("ICLOUD_CALENDAR") or "滴答日程"
ICLOUD_URL = os.environ.get("ICLOUD_CALDAV_URL") or "https://caldav.icloud.com/"

# 全天/带时间事件统一被改成的当天时间（中国无夏令时，用固定 +08:00）
CN = timezone(timedelta(hours=8))
FIX_HOUR = int(os.environ.get("FIX_HOUR", "23"))
FIX_MINUTE = int(os.environ.get("FIX_MINUTE", "58"))
FIX_DURATION_MIN = int(os.environ.get("FIX_DURATION_MIN", "1"))
# 为 1 时，每次同步前先清空目标日历（仅当该日历专用于本脚本时使用）
CLEAR_FIRST = os.environ.get("CLEAR_FIRST", "0") == "1"

MARKER = "X-DIDA-SYNC"

# 网络抖动（DNS 解析失败、读超时、连接重置）属于临时故障：重试，而不是让整次运行变红。
# iCloud 在访问过于频繁时会返回 429 RateLimitError，或临时 401 Unauthorized——同一套密码
# 过几分钟又能成功，说明并非密码错，而是被 Apple 限流/风控，这类也应重试。真正密码错/服务器
# 错时重试几次后依旧失败，所以不会掩盖问题。
# 单次最坏耗时：HTTP_TIMEOUT*RETRY_ATTEMPTS + 退避（3+8 或按服务端 Retry-After）≈ 2~3 分钟，
# 5 分钟一拍的调度会自然排队跟上。
TRANSIENT_HINTS = (
    "connectionerror", "readtimeout", "connecttimeout", "timeout", "timed out",
    "name or service not known", "failed to resolve", "name resolution",
    "remote disconnected", "connection reset", "connection aborted",
    "protocolerror", "chunkedencodingerror", "sslerror", "proxyerror",
    "temporarily unavailable", "eof occurred", "max retries exceeded",
    "ratelimit", "rate limit", "too many requests", "429",
    "403", "forbidden", "bad gateway", "service unavailable",
    "gateway timeout", "internal server error", "500", "502", "503", "504",
)
AUTH_HINTS = ("authorization", "unauthorized", "bad credentials", "401")
# 这些 HTTP 状态码属于“稍后重试可能成功”，而不是配置/代码错误。
HTTP_TRANSIENT_STATUS = frozenset((403, 408, 425, 429, 500, 502, 503, 504))
RETRY_ATTEMPTS = int(os.environ.get("RETRY_ATTEMPTS", "3"))
RETRY_DELAYS = (3, 8)          # 网络抖动的退避
AUTH_RETRY_DELAYS = (20, 45)   # 401 多半是限流/风控，给更长的冷却时间
RATE_LIMIT_DELAYS = (15, 45)   # 403/429 多半是源站限流/风控，需要更久冷却
RETRY_DELAY_CAP = int(os.environ.get("RETRY_DELAY_CAP", "90"))
RETRY_ON_AUTH = os.environ.get("RETRY_ON_AUTH", "1") == "1"
# 临时故障重试仍失败时，是否把整次 run 标成成功（只留 warning）。
# 这样偶发限流/网络波动不会每小时发一封失败邮件；真错误（配置、解析、权限等）仍会报红。
SOFT_FAIL_TRANSIENT = os.environ.get("SOFT_FAIL_TRANSIENT", "1") == "1"
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "40"))

_RETRY_AFTER_RE = re.compile(r"retry[- ]after[:\s]+(\d+)", re.I)
_STATUS_RE = re.compile(r"\b([45]\d{2})\b")


def _status_code(ex: BaseException):
    """尽量从 requests/caldav 异常里取 HTTP 状态码；取不到返回 None。"""
    resp = getattr(ex, "response", None)
    code = getattr(resp, "status_code", None)
    if isinstance(code, int):
        return code
    m = _STATUS_RE.search(str(ex))
    return int(m.group(1)) if m else None


def _classify(ex: BaseException):
    """把异常归为 'auth'（401/鉴权）、'transient'（网络抖动/限流）或 None。"""
    text = f"{type(ex).__name__} {ex}".lower()
    names = " ".join(t.__name__ for t in type(ex).__mro__).lower()
    if any(h in names or h in text for h in AUTH_HINTS):
        return "auth"
    if _status_code(ex) in HTTP_TRANSIENT_STATUS:
        return "transient"
    if any(h in names or h in text for h in TRANSIENT_HINTS):
        return "transient"
    return None


def _is_transient(ex: BaseException) -> bool:
    kind = _classify(ex)
    if kind == "auth":
        return RETRY_ON_AUTH
    return kind == "transient"


def _retry_delay(ex: BaseException, attempt: int) -> int:
    # 优先遵守服务端给出的 Retry-After（真实响应头，或 caldav 文本里的 "Retry after: 60"）
    resp = getattr(ex, "response", None)
    headers = getattr(resp, "headers", None) or {}
    header_ra = headers.get("Retry-After") if hasattr(headers, "get") else None
    if header_ra:
        try:
            return min(int(str(header_ra).strip()), RETRY_DELAY_CAP)
        except ValueError:
            pass
    m = _RETRY_AFTER_RE.search(str(ex))
    if m:
        return min(int(m.group(1)), RETRY_DELAY_CAP)
    if _status_code(ex) in (403, 429):
        return RATE_LIMIT_DELAYS[min(attempt - 1, len(RATE_LIMIT_DELAYS) - 1)]
    if _classify(ex) == "auth":
        return AUTH_RETRY_DELAYS[min(attempt - 1, len(AUTH_RETRY_DELAYS) - 1)]
    return RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]


def with_retry(fn, what: str):
    """执行 fn()，遇到网络抖动 / 限流(429) / 偶发 401 时退避重试。"""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as ex:
            if attempt >= RETRY_ATTEMPTS or not _is_transient(ex):
                raise
            delay = _retry_delay(ex, attempt)
            kind = _classify(ex)
            print(f"[retry] {what} 第 {attempt} 次失败（{kind}）：{type(ex).__name__}: {str(ex)[:160]}")
            print(f"[retry] {delay}s 后重试（{attempt + 1}/{RETRY_ATTEMPTS}）")
            time.sleep(delay)


def fetch_ics(url: str) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    def _get():
        resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.content

    return with_retry(_get, "拉取滴答 ICS")


def _event_day(dtstart):
    """返回事件应落在哪一天（date）。全天事件取 date；定时事件按中国墙钟取 date。"""
    d = dtstart.dt
    if isinstance(d, datetime):
        if d.tzinfo is not None:
            return d.astimezone(CN).date()
        return d.date()
    return d  # 已经是 date


def build_events(raw: bytes):
    """返回 {uid: event_component}。

    不论原事件是全天(VALUE=DATE)还是带时间，都改成事件当天 FIX_HOUR:FIX_MINUTE 起的
    FIX_DURATION_MIN 分钟定时事件（默认 23:58→23:59）。原因：小米手环只显示未来 7 天内
    「尚未开始」的事件，当天全天事件被视为已开始而不显示；放到当天 23:58 这一分钟即可
    让当天的事件也显示出来。仅额外打同步标记。
    """
    cal = Calendar.from_ical(raw)
    result = {}
    for ev in cal.walk("VEVENT"):
        uid = ev.get("UID")
        if not uid:
            continue
        ev = copy.deepcopy(ev)
        dtstart = ev.get("DTSTART")
        if dtstart is not None:
            day = _event_day(dtstart)
            start_dt = datetime(day.year, day.month, day.day,
                                FIX_HOUR, FIX_MINUTE, 0, tzinfo=CN)
            end_dt = start_dt + timedelta(minutes=FIX_DURATION_MIN)
            ev["DTSTART"] = vDatetime(start_dt)
            ev["DTEND"] = vDatetime(end_dt)
            # 清掉旧的 VALUE=DATE 参数（vDatetime 赋值通常已重建属性，这里兜底）
            for key in ("DTSTART", "DTEND"):
                p = ev.get(key)
                params = getattr(p, "params", None)
                if params and "VALUE" in params:
                    del params["VALUE"]
        ev[MARKER] = "1"
        result[str(uid)] = ev
    return result


def wrap(ev) -> str:
    c = Calendar()
    c.add("PRODID", "-//dida-icloud-sync//EN")
    c.add("VERSION", "2.0")
    c.add_component(ev)
    return c.to_ical().decode("utf-8")


def signature(comp):
    """用于判断内容是否变化（忽略 DTSTAMP 等易变字段）。"""
    def dt(v):
        x = comp.get(v)
        if x is None:
            return ""
        try:
            d = x.dt
            # 统一归一到「中国墙钟时间」再比较。本地写入是 +08:00 的 aware 时间；
            # iCloud 读回时把 DTSTART;TZID=UTC+08:00 解析成 naive 本地墙钟，
            # 同一时刻两种表示不同会造成「每次都被判为变化」，必须归一化。
            if isinstance(d, datetime):
                if d.tzinfo is None:
                    return d.isoformat()
                return d.astimezone(CN).replace(tzinfo=None).isoformat()
            return d.isoformat()  # 纯 date 全天
        except Exception:
            return str(x)

    def txt(k):
        v = comp.get(k)
        return "" if v is None else str(v)

    r = comp.get("RRULE")
    rr = r.to_ical().decode() if r is not None else ""
    return (txt("SUMMARY"), dt("DTSTART"), dt("DTEND"), rr,
            txt("DESCRIPTION"), txt("LOCATION"), txt("STATUS"))


def get_component(event_obj):
    """从 caldav Event 对象里取出 VEVENT 组件。"""
    try:
        data = event_obj.data
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        cal = Calendar.from_ical(data)
        for c in cal.walk("VEVENT"):
            return c
    except Exception as e:
        print(f"[warn] 解析已有事件失败: {e}")
    return None


def get_or_create_calendar(principal, name):
    for c in principal.calendars():
        try:
            if (c.name or "").strip() == name:
                return c
        except Exception:
            continue
    print(f"[info] 未找到日历「{name}」，尝试创建…")
    return principal.make_calendar(name=name)


def do_sync(src_events):
    client = caldav.DAVClient(url=ICLOUD_URL, username=ICLOUD_USERNAME,
                             password=ICLOUD_APP_PASSWORD, timeout=HTTP_TIMEOUT)
    principal = client.principal()
    calendar = get_or_create_calendar(principal, ICLOUD_CALENDAR)

    # 关键：先读取当前日历。防止重复写入的前提。
    # 同一 UID 可能因历史 bug 存在多条，所以用 uid -> list 收集，后面统一去重。
    existing = {}   # uid -> list[(event_obj, comp)]
    total_existing = 0
    for e in calendar.events():
        comp = get_component(e)
        if comp is None:
            continue
        total_existing += 1
        uid = comp.get("UID")
        key = str(uid) if uid else f"__no_uid__{id(e)}"
        existing.setdefault(key, []).append((e, comp))
    print(f"[info] 目标日历「{ICLOUD_CALENDAR}」现有 {total_existing} 个事件；源有 {len(src_events)} 个")

    deleted = 0

    # 可选：整体清空目标日历后再重建（仅当该日历专用于本脚本时使用）
    if CLEAR_FIRST:
        for key, lst in existing.items():
            for e_obj, _ in lst:
                try:
                    e_obj.delete()
                    deleted += 1
                except Exception as ex:
                    print(f"[warn] 清空删除失败 {key}: {ex}")
        existing = {}
        print(f"[info] CLEAR_FIRST=1：已清空 {deleted} 个事件，将全量重建")

    # 去重：同一 UID 存在多条（历史重复写入）时保留最合适的一条，其余删除
    for key, lst in list(existing.items()):
        if len(lst) <= 1:
            continue
        src = src_events.get(key)
        keep = 0
        if src is not None:
            for i, (_e, c) in enumerate(lst):
                if signature(c) == signature(src):
                    keep = i
                    break
        for i, (e_obj, _c) in enumerate(lst):
            if i == keep:
                continue
            try:
                e_obj.delete()
                deleted += 1
            except Exception as ex:
                print(f"[warn] 去重删除失败 {key}: {ex}")
        existing[key] = [lst[keep]]

    created = updated = skipped = 0
    for uid, ev in src_events.items():
        ics = wrap(ev)
        lst = existing.get(uid)
        if lst:
            e_obj, old_comp = lst[0]
            if signature(old_comp) == signature(ev):
                skipped += 1
                continue
            e_obj.data = ics
            e_obj.save()
            updated += 1
        else:
            calendar.save_event(ics)
            created += 1

    # 删除：源里已经没有、但日历里还留着、且是本脚本创建的事件 → 删掉
    # （这就是「滴答里删掉 → 手环日历里也跟着删掉」的删除功能）
    src_uids = set(src_events.keys())
    for uid, lst in existing.items():
        if uid in src_uids:
            continue
        for e_obj, comp in lst:
            if str(comp.get(MARKER)) == "1":
                try:
                    e_obj.delete()
                    deleted += 1
                    print(f"[del] 已删除（滴答里已不存在）：{comp.get('SUMMARY')}")
                except Exception as ex:
                    print(f"[warn] 删除失败 {uid}: {ex}")

    print(f"[done] 新建 {created}，更新 {updated}，跳过 {skipped}，删除 {deleted}")


def _run():
    if not DIDA_ICS_URL:
        print("[skip] 缺少 DIDA_ICS_URL")
        return 0
    raw = fetch_ics(DIDA_ICS_URL)
    src = build_events(raw)
    print(f"[info] 源解析出 {len(src)} 个事件")
    for uid, ev in list(src.items())[:5]:
        print(f"  - {ev.get('SUMMARY')} | {ev.get('DTSTART').dt} -> {ev.get('DTEND').dt}")

    dry = "--dry-run" in sys.argv
    if dry:
        print("[dry-run] 不连接 iCloud，仅校验解析与转换")
        return 0

    if not (ICLOUD_USERNAME and ICLOUD_APP_PASSWORD):
        print("[skip] 未配置 ICLOUD_USERNAME / ICLOUD_APP_PASSWORD，本次跳过（不报错）。"
              "配置 Secrets 后自动生效。")
        return 0
    if caldav is None:
        print("[error] 未安装 caldav，请 pip install caldav")
        return 1
    with_retry(lambda: do_sync(src), "同步到 iCloud")
    return 0


def main():
    try:
        return _run()
    except Exception as ex:
        # 临时故障（网络抖动/限流/偶发 401）在重试后仍失败时，默认不让整次 run 变红。
        # 否则 Worker 每分钟的调度会在一次限流风暴里发出几十封失败邮件。
        if SOFT_FAIL_TRANSIENT and _is_transient(ex):
            kind = _classify(ex) or "unknown"
            detail = f"{type(ex).__name__}: {str(ex)[:300]}"
            print(f"[warn] 临时故障（{kind}），重试 {RETRY_ATTEMPTS} 次后仍未恢复：{detail}")
            print("::warning title=Dida→iCloud 同步临时失败::本轮按成功处理，下一轮调度会自动重试；"
                  "如持续失败请查看日志或把 SOFT_FAIL_TRANSIENT 设为 0。")
            return 0
        print(f"[error] {type(ex).__name__}: {str(ex)[:500]}")
        raise


if __name__ == "__main__":
    sys.exit(main())
