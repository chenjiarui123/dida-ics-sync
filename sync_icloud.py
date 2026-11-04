"""滴答清单 ICS -> iCloud 日历 (CalDAV) 全自动同步。

设计目标：
- 把手环能读到的「系统内置日历」事件自动写入 iCloud 专用日历（手环只读系统日历，不读订阅/第三方日历）。
- 保持滴答清单原始写法：全天事件就是真正的全天事件（DTSTART;VALUE=DATE），带时间的事件原样保留；
  实测手环/iOS 日历能正常显示 CalDAV 写入的全天事件，无需再改成定时事件。
- 幂等：按 UID upsert；内容无变化不写；只删除带 X-DIDA-SYNC 标记的本脚本创建的事件，绝不碰别的日历事件。
- 缺 ICLOUD_* 环境变量时安全退出（不报红），方便先部署后配置。
依赖：caldav + icalendar + requests
"""
import os
import sys
import copy
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

# 全天事件被改成的当天时间（中国无夏令时，用固定 +08:00）
CN = timezone(timedelta(hours=8))
FIX_HOUR = int(os.environ.get("FIX_HOUR", "23"))
FIX_MINUTE = int(os.environ.get("FIX_MINUTE", "58"))
FIX_DURATION_MIN = int(os.environ.get("FIX_DURATION_MIN", "1"))

MARKER = "X-DIDA-SYNC"


def fetch_ics(url: str) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.content


def build_events(raw: bytes):
    """返回 {uid: event_component}，保持滴答原始写法（全天即全天，带时间即带时间），仅加同步标记。"""
    cal = Calendar.from_ical(raw)
    result = {}
    for ev in cal.walk("VEVENT"):
        uid = ev.get("UID")
        if not uid:
            continue
        ev = copy.deepcopy(ev)
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
                             password=ICLOUD_APP_PASSWORD)
    principal = client.principal()
    calendar = get_or_create_calendar(principal, ICLOUD_CALENDAR)

    # 现有事件： uid -> event_obj
    existing = {}
    for e in calendar.events():
        comp = get_component(e)
        if comp is None:
            continue
        uid = comp.get("UID")
        if uid:
            existing[str(uid)] = (e, comp)
    print(f"[info] 目标日历「{ICLOUD_CALENDAR}」现有 {len(existing)} 个事件；源有 {len(src_events)} 个")

    created = updated = skipped = deleted = 0
    for uid, ev in src_events.items():
        ics = wrap(ev)
        if uid in existing:
            e_obj, old_comp = existing[uid]
            if signature(old_comp) == signature(ev):
                skipped += 1
                continue
            e_obj.data = ics
            e_obj.save()
            updated += 1
        else:
            calendar.save_event(ics)
            created += 1

    # 删除：只删本脚本创建过、且源里已不存在的事件
    src_uids = set(src_events.keys())
    for uid, (e_obj, comp) in existing.items():
        if uid in src_uids:
            continue
        if str(comp.get(MARKER)) == "1":
            try:
                e_obj.delete()
                deleted += 1
            except Exception as ex:
                print(f"[warn] 删除失败 {uid}: {ex}")

    print(f"[done] 新建 {created}，更新 {updated}，跳过 {skipped}，删除 {deleted}")


def main():
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
    do_sync(src)
    return 0


if __name__ == "__main__":
    sys.exit(main())
