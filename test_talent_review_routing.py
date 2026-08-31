# -*- coding: utf-8 -*-
"""离线校验：人才盘点/九宫格意图只读取 talent_review_result 指标。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import metrics
import server


def main():
    terms = {"人才盘点", "九宫格", "九宫格人才"}
    metric = metrics.METRICS["talent_review_result_distribution"]
    assert terms.issubset(set(metric["synonyms"]))
    assert "必须读取本指标" in metric["description"]
    assert "不得用于" in (server.get_talent_overview.__doc__ or "")
    assert 'dimension="talent_review_result"' in (server.get_talent_structure.__doc__ or "")

    original_latest = server.get_latest_snapshot_date
    original_query = server._query_distribution_metric
    calls = []
    try:
        server.get_latest_snapshot_date = lambda date, table: "2025-12-31"

        def fake_query(metric_key, snapshot_date, dept_name="", stat_month=None, role="", **kwargs):
            calls.append(metric_key)
            return [{"dimension": "9-超级明星", "count": 3, "percentage": 100.0}]

        server._query_distribution_metric = fake_query
        for alias in ("人才盘点", "九宫格", "九宫格人才", "人才盘点结果", "talent_review_result"):
            result = json.loads(server.get_talent_structure(alias, "2026-08-27"))
            assert result["data"]["dimension"] == "talent_review_result"
            assert result["data"]["metric_name"] == "人才盘点结果分布"
            assert result["data"]["items"][0]["dimension"] == "9-超级明星"
    finally:
        server.get_latest_snapshot_date = original_latest
        server._query_distribution_metric = original_query

    assert calls == ["talent_review_result_distribution"] * 5
    print("人才盘点/九宫格意图路由校验通过")


if __name__ == "__main__":
    main()
