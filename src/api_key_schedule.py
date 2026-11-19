"""Site-wide time boundaries for managed API key request limits."""

import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


LIMIT_FIELDS = ("day_rpm", "day_total", "night_rpm", "night_total")


class ApiKeySchedule:
    def __init__(self):
        self.zone = ZoneInfo(os.getenv("API_KEY_LIMIT_TIMEZONE", "Asia/Singapore"))
        self.day = time.fromisoformat(os.getenv("API_KEY_LIMIT_DAY_START", "08:00"))
        self.night = time.fromisoformat(os.getenv("API_KEY_LIMIT_NIGHT_START", "20:00"))
        if self.day.tzinfo or self.night.tzinfo or self.day >= self.night:
            raise ValueError("白天开始时间必须早于晚间开始时间，请使用当地时间 HH:MM")

    def period(self, timestamp):
        current = datetime.fromtimestamp(timestamp, self.zone)
        date = current.date()
        day_start = datetime.combine(date, self.day, self.zone).timestamp()
        night_start = datetime.combine(date, self.night, self.zone).timestamp()
        if day_start <= timestamp < night_start:
            return "day", day_start
        if timestamp < day_start:
            night_start = datetime.combine(date - timedelta(days=1), self.night, self.zone).timestamp()
        return "night", night_start

    def label(self):
        return f"{self.zone.key} · 白天 {self.day:%H:%M}–{self.night:%H:%M}，其余时间为晚上"
