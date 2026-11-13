"""后台日期时钟：生产取 UTC 当日，测试可通过 TEST_BACKOFFICE_DATE 钉死。

跨日账本的一切日期判断都走这里，保证测试可重复。
"""

from datetime import date

from flask import current_app


def today_str() -> str:
    """返回后台当前日期（YYYY-MM-DD），测试可钉死。"""
    pinned = current_app.config.get("TEST_BACKOFFICE_DATE")
    if pinned:
        return str(pinned)
    return date.today().isoformat()
