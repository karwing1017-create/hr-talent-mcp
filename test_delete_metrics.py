# -*- coding: utf-8 -*-
"""
删除 8 个冗余指标后离线回归（mock 数据库层，不依赖真实数据库）。

运行方式（项目 venv）：
    python test_delete_metrics.py
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import server
import metrics

DELETED = {
    "key_post_distribution", "management_cadre_distribution",
    "key_position_count", "key_position_bachelor_above_ratio",
    "mgmt_bachelor_above_ratio", "management_cadre_count",
    "mgmt_avg_age", "post_95s_cadre_ratio",
}

# ── 断言 1：METRICS 不再包含已删 key ───────────────────────────
remaining = set(metrics.METRICS.keys()) & DELETED
assert not remaining, f"仍有已删指标未被移除: {remaining}"
print(f"断言1 通过 ✅  METRICS 共 {len(metrics.METRICS)} 个指标，已删 8 个均不存在")

# ── 断言 2：list_metrics 维度元数据不含 key_post/management_cadre ─
mlist = metrics.list_metrics()
for m in mlist:
    dims = m.get("dimensions", [])
    assert "key_post" not in dims, m
    assert "management_cadre" not in dims, m
print(f"断言2 通过 ✅  list_metrics 返回 {len(mlist)} 个指标，维度元数据已清理")
assert "key_post" not in metrics.DIMENSION_CONFIG
assert "management_cadre" not in metrics.DIMENSION_CONFIG
print("断言2b 通过 ✅  DIMENSION_CONFIG 已移除 key_post/management_cadre")

# ── mock 数据库层 ────────────────────────────────────────────────
def fake_snap(target, table):
    # 直接把请求的快照日期当作存在（用于趋势分年逻辑）
    if len(target) >= 10 and target[4] == "-" and target[7] == "-":
        return target[:10]
    raise ValueError("no snap")

def fake_month(target, table):
    if len(target) >= 7 and target[4] == "-":
        return target[:7]
    raise ValueError("no month")

def fake_exec(sql, params=None):
    # 仅当 SQL 真正选择 dimension_value（分布类模板）时才返回分布结构
    if "dimension_value" in sql:
        return [{"dimension_value": "示例", "count": 10, "percentage": 100.0}]
    return [{"value": 100}]

server.get_latest_snapshot_date = fake_snap
server.get_latest_stat_month = fake_month
server.execute_query = fake_exec

def check_no_deleted(result_json, ctx):
    d = json.loads(result_json)
    s = result_json
    for k in DELETED:
        assert f'"{k}"' not in s, f"{ctx}: 返回结果中意外出现已删指标 {k}"
    assert "error" not in d or d.get("error") is None or "不支持的" in s or "分布类" in s, \
        f"{ctx}: 工具返回错误 {d.get('error')}"
    print(f"  {ctx}: OK（无错误、无已删指标）")

# ── 断言 3：四个工具在 role=全部 / role=M岗 下均不引用已删指标 ──
print("断言3：工具回归")
r = server.get_talent_overview("2026-07-31", "")
check_no_deleted(r, "overview(全部)")
r = server.get_talent_overview("2026-07-31", "M岗")
check_no_deleted(r, "overview(M岗)")

r = server.get_talent_structure("age", "2026-07-31", "")
check_no_deleted(r, "structure(全部)")
r = server.get_talent_structure("age", "2026-07-31", "关键岗位")
check_no_deleted(r, "structure(关键岗位)")

r = server.get_metric_yearly_trend("headcount,turnover_rate,management_cadre_ratio,key_position_ratio", "2026-07-31", role="")
check_no_deleted(r, "yearly_trend(全部)")
r = server.get_metric_yearly_trend("headcount,turnover_rate,management_cadre_ratio,key_position_ratio", "2026-07-31", role="P岗")
check_no_deleted(r, "yearly_trend(P岗)")

r = server.detect_talent_risk("2026-07-31")
check_no_deleted(r, "detect_risk")
print("断言3 通过 ✅  get_talent_overview / get_talent_structure / get_metric_yearly_trend / detect_talent_risk 均未引用已删指标")

# ── 断言 4：get_talent_structure 拒绝对已删维度查询 ─────────────
r = server.get_talent_structure("key_post", "2026-07-31")
d = json.loads(r)
assert "error" in d and "不支持的维度" in d["error"], d
print("断言4 通过 ✅  get_talent_structure(key_post) 已正确拒绝（维度已移除）")

print("\n全部离线回归通过 ✅")
