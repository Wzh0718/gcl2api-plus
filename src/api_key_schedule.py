"""Site-wide time boundaries for managed API key request limits."""

import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


LIMIT_FIELDS = ("day_rpm", "day_total", "night_rpm", "night_total")


class ApiKeySchedule:
    def __init__(self):
        self.zone = ZoneInfo(os.getenv("API_KEY_LIMIT_TIMEZONE", "Asia/Singapore"))
        self.day = time.fromisoformat(os.getenv("API_KEY_LIMIT_DAY_START", "09:00"))
        self.night = time.fromisoformat(os.getenv("API_KEY_LIMIT_NIGHT_START", "18:30"))
        if self.day.tzinfo or self.night.tzinfo or self.day >= self.night:
            raise ValueError("工作日白天开始时间必须早于结束时间，请使用当地时间 HH:MM")

    def period(self, timestamp):
        current = datetime.fromtimestamp(timestamp, self.zone)
        date = current.date()
        day_start = datetime.combine(date, self.day, self.zone).timestamp()
        night_start = datetime.combine(date, self.night, self.zone).timestamp()
        if date.weekday() < 5 and timestamp >= day_start:
            if timestamp < night_start:
                return "day", day_start
            return "night", night_start

        # Other hours run continuously from the last weekday's closing time.
        last_workday = date - timedelta(days=1)
        if last_workday.weekday() >= 5:
            last_workday -= timedelta(days=last_workday.weekday() - 4)
        night_start = datetime.combine(last_workday, self.night, self.zone).timestamp()
        return "night", night_start

    def label(self):
        return f"{self.zone.key} · 周一至周五 {self.day:%H:%M}–{self.night:%H:%M} 为工作日白天，其余为其他时间"
