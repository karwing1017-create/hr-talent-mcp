# -*- coding: utf-8 -*-
"""
get_metric_yearly_trend 离线单测（mock 数据库层，不依赖真实数据库）。

运行方式（项目 venv）：
    python test_yearly_trend.py
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import server
from server import get_metric_yearly_trend

# ── mock 数据库层 ──────────────────────────────────────────────
# 快照表：2023-2026 每年都有 12-31 快照；DM 月度：2024-2026 有数据，2023 无
SNAPSHOTS = {
    "2023-12-31": "2023-12-31", "2024-12-31": "2024-12-31",
    "2025-12-31": "2025-12-31", "2026-12-31": "2026-07-31",  # 今年只到 7 月
}
MONTHS = {
    "2023-12": None, "2024-12": "2024-12", "2025-12": "2025-12", "2026-12": "2026-07",
}


def fake_get_latest_snapshot_date(target_date, table):
    v = SNAPSHOTS.get(target_date)
    if v is None:
        raise ValueError("无快照")
    return v


def fake_get_latest_stat_month(target_date, table):
    v = MONTHS.get(target_date[:7])
    if v is None:
        raise ValueError("无月度")
    return v


CALLS = []


def fake_query_single(metric_key, snapshot_date, dept_name="", stat_month=None, role=""):
    CALLS.append(("single", metric_key, snapshot_date, stat_month, role))
    return {"value": 100, "unit": "人"}


def fake_query_dm(metric_key, target_date, dept_name="", role=""):
    CALLS.append(("dm", metric_key, target_date, role))
    return {"value": 1.5, "unit": "%"}


def fake_query_mixed(metric_key, snapshot_date, dept_name="", stat_month=None, role=""):
    CALLS.append(("mixed", metric_key, snapshot_date, stat_month, role))
    return {"value": 50, "unit": "人"}


server.get_latest_snapshot_date = fake_get_latest_snapshot_date
server.get_latest_stat_month = fake_get_latest_stat_month
server._query_single_metric = fake_query_single
server._query_dm_metric = fake_query_dm
server._query_mixed_metric = fake_query_mixed
server.execute_query = lambda sql, params=None: [{"ok": 1}]

# ── 主流程 ────────────────────────────────────────────────────
result = get_metric_yearly_trend(
    "headcount,turnover_rate,promotion_3yr_count,turnover_count", "2026-07-31"
)
data = json.loads(result)
for m in data["data"]["metrics"]:
    for p in m["trend"]:
        extra = p.get("snapshot_date") or p.get("stat_month") or "-"
        print(f"{m['key']:>22} {p['year']}: value={p['value']} [{extra}] {p.get('note', '')}")

by_key = {m["key"]: m for m in data["data"]["metrics"]}
hc = [p["value"] for p in by_key["headcount"]["trend"]]
tr = [p["value"] for p in by_key["turnover_rate"]["trend"]]
pr = [p["value"] for p in by_key["promotion_3yr_count"]["trend"]]
tc = [p["value"] for p in by_key["turnover_count"]["trend"]]
assert hc == [100, 100, 100, 100], hc                  # 快照表 4 年全有
assert tr == [None, 1.5, 1.5, 1.5], tr                 # DM 表 2023 无数据
assert pr == [None, 50, 50, 50], pr                    # mixed 需 snap+month
assert tc == [100, 100, 100, 100], tc                  # 过程表随快照+month
print("主流程断言通过 ✅")

# ── 异常分支 ──────────────────────────────────────────────────
r1 = get_metric_yearly_trend("not_exist_key", "2026-07-31")
assert "未知的指标 key" in r1
r2 = get_metric_yearly_trend("", "2026-07-31")
assert "metric_keys 不能为空" in r2
r3 = get_metric_yearly_trend("headcount", "bad-date!!")
assert "error" in json.loads(r3)


def boom(sql, params=None):
    raise RuntimeError("db down")


server.execute_query = boom
r4 = get_metric_yearly_trend("headcount", "2026-07-31")
assert "数据库暂不可达" in r4
print("异常分支断言通过 ✅（未知key / 空指标 / 非法日期 / 库不可达）")

# ── role 透传 ──────────────────────────────────────────────────
server.execute_query = lambda sql, params=None: [{"ok": 1}]  # 恢复连通
# (原始入参, 展示值data.role, 传给查询的规范值)
ROLE_CASES = [
    ("", "全部人员", ""), ("M岗", "M岗", "M岗"), ("管理干部", "管理干部", "管理干部"),
    ("P岗", "P岗", "P岗"), ("关键岗位", "关键岗位", "关键岗位"),
    ("m", "M岗", "M岗"), ("P", "P岗", "P岗"),
]
for raw, display, canonical in ROLE_CASES:
    CALLS.clear()
    res = get_metric_yearly_trend("headcount,turnover_rate", "2026-07-31", role=raw)
    rd = json.loads(res)
    assert rd["data"]["role"] == display, (raw, rd["data"]["role"])
    # 所有查询调用都带上归一化后的 canonical role
    assert all(c[-1] == canonical for c in CALLS), (raw, CALLS)
# 非法角色应返回友好错误
bad = get_metric_yearly_trend("headcount", "2026-07-31", role="X岗")
assert "不支持的人员角色" in bad, bad
print("role 透传断言通过 ✅（含 M/P/关键岗位归一化、留空=全部人员、非法值报错）")
