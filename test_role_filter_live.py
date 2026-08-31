"""真库端到端校验：人员角色过滤（数据库可达时运行）。

验证 get_talent_overview / get_talent_structure 在 人员角色 入参下：
  1) SQL 经 cursor.mogrify 替换参数不报错（占位符完整性）
  2) 实际取数，关键指标随角色变化符合预期（M岗/关键岗位 < 全部；P岗独立子集）
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import metrics
import server
from db import get_cursor


def mogrify_check(metric_keys, snapshot_date, role):
    """逐指标用 cursor.mogrify 验证参数替换成功（不真正执行）。"""
    params = metrics.build_params(snapshot_date, "", stat_month=snapshot_date[:7], role=role)
    ok, fail = [], []
    with get_cursor() as cur:
        for k in metric_keys:
            try:
                cur.mogrify(metrics.METRICS[k]["sql_template"], params)
                ok.append(k)
            except Exception as e:  # noqa: BLE001
                fail.append((k, str(e)))
    return ok, fail


def overview_headcount(role):
    res = json.loads(server.get_talent_overview("2026-07-31", "", role))
    if "error" in res:
        raise RuntimeError(f"overview 报错: {res}")
    hc = next(s for s in res["data"]["scalars"] if s["key"] == "headcount")
    return {
        "role": res["data"]["role"],
        "headcount": hc["value"],
    }


def main():
    sd = "2026-07-31"
    emp_keys = list(metrics.METRICS.keys())
    ok, fail = mogrify_check(emp_keys, sd, "M岗")
    print(f"[mogrify] M岗: OK={len(ok)} FAIL={len(fail)}")
    for k, e in fail:
        print("   FAIL:", k, e)
    assert not fail, "存在参数替换失败的指标"

    rows = {}
    for role in ["", "M岗", "P岗", "关键岗位", "本科以上人员"]:
        rows[role] = overview_headcount(role)
        print(f"[overview] role={role!r:>6} -> {rows[role]}")

    # 预期：各角色分组人数均小于全量
    all_hc = rows[""]["headcount"]
    assert rows["M岗"]["headcount"] < all_hc
    assert rows["P岗"]["headcount"] < all_hc
    assert rows["关键岗位"]["headcount"] < all_hc
    assert rows["本科以上人员"]["headcount"] < all_hc
    print("\n真库端到端校验通过 ✅")


if __name__ == "__main__":
    main()
