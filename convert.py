import os
import requests
from datetime import datetime, timedelta
from icalendar import Calendar
from icalendar.prop import vDatetime

# 配置
DIDA_ICS_URL = os.environ.get("DIDA_ICS_URL")
OUTPUT_FILE = "converted.ics"

# 全天事件 -> 当天 23:59:00 ~ 次日 00:00:00（只在当天最后一分钟）
FIX_START_HOUR = 23
FIX_START_MINUTE = 59
FIX_DURATION_MIN = 1

if not DIDA_ICS_URL:
    raise Exception("缺少环境变量 DIDA_ICS_URL")


def fetch_ics(url: str) -> Calendar:
    # 兼容滴答给的 webcal:// 订阅链接，requests 只认 http(s)
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return Calendar.from_ical(resp.content)


def transform_calendar(cal: Calendar):
    for event in cal.walk("VEVENT"):
        dtstart = event.get("DTSTART")
        if dtstart is None:
            continue
        # 判断：VALUE=DATE 代表全天事件
        if dtstart.params.get("VALUE") == "DATE":
            day = dtstart.dt  # date对象
            start_dt = datetime(day.year, day.month, day.day, FIX_START_HOUR, FIX_START_MINUTE, 0)
            end_dt = start_dt + timedelta(minutes=FIX_DURATION_MIN)

            event["DTSTART"] = vDatetime(start_dt)
            event["DTEND"] = vDatetime(end_dt)
            # 移除DATE标记，变成带时分秒的时间事件
            if "VALUE" in event["DTSTART"].params:
                del event["DTSTART"].params["VALUE"]
    return cal


def main():
    raw_cal = fetch_ics(DIDA_ICS_URL)
    new_cal = transform_calendar(raw_cal)
    new_content = new_cal.to_ical()

    # 和旧文件对比，没有变化就不写入
    try:
        with open(OUTPUT_FILE, "rb") as f:
            old_content = f.read()
        if old_content == new_content:
            print("内容无变化，跳过写入")
            return
    except FileNotFoundError:
        pass

    with open(OUTPUT_FILE, "wb") as f:
        f.write(new_content)
    print(f"已生成 {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
