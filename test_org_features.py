# -*- coding: utf-8 -*-
"""离线校验：直属下一级组织对比与三级组织历史映射。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import server
from metrics import build_params


def test_org_leaf_name_normalizes_full_paths():
    assert server._org_leaf_name("东鹏控股/东鹏/瓷砖事业部") == "瓷砖事业部"
    assert server._org_leaf_name("瓷砖事业部") == "瓷砖事业部"


def test_same_name_orgs_are_resolved_by_code_and_queried_by_full_path():
    original_execute = server.execute_query
    try:
        server.execute_query = lambda sql, params=None: [
            {
                "lvl1_dept_name": "东鹏",
                "lvl2_dept_name": "瓷砖事业部",
                "lvl3_dept_name": "供应链中心",
                "lvl3_dept_code": "TILE-SC",
                "lvl4_dept_name": "采购部",
                "lvl5_dept_name": None,
            },
            {
                "lvl1_dept_name": "东鹏",
                "lvl2_dept_name": "瓷砖事业部",
                "lvl3_dept_name": "供应链中心",
                "lvl3_dept_code": "TILE-SC",
                "lvl4_dept_name": "物流部",
                "lvl5_dept_name": None,
            },
            {
                "lvl1_dept_name": "东鹏",
                "lvl2_dept_name": "卫浴事业部",
                "lvl3_dept_name": "供应链中心",
                "lvl3_dept_code": "BATH-SC",
                "lvl4_dept_name": "采购部",
                "lvl5_dept_name": None,
            },
        ]

        matches = server._resolve_department_matches("2026-07-31", "供应链中心")
        assert matches == [
            {
                "department": "供应链中心",
                "organization_code": "BATH-SC",
                "organization_path": "东鹏/卫浴事业部/供应链中心",
                "organization_level": 3,
                "parent_department": "卫浴事业部",
            },
            {
                "department": "供应链中心",
                "organization_code": "TILE-SC",
                "organization_path": "东鹏/瓷砖事业部/供应链中心",
                "organization_level": 3,
                "parent_department": "瓷砖事业部",
            },
        ]

        queried_paths = []

        def query_one(path):
            queried_paths.append(path)
            assert path.organization_code in ("BATH-SC", "TILE-SC")
            assert path.organization_level == 3
            return json.dumps({"data": {"department": path, "value": len(queried_paths)}})

        result = json.loads(server._query_ambiguous_departments(
            "2026-07-31", "供应链中心", query_one))
        assert queried_paths == [row["organization_path"] for row in matches]
        assert result["data"]["ambiguous_department_name"] is True
        assert result["data"]["match_count"] == 2
        assert [row["parent_department"] for row in result["data"]["organizations"]] == [
            "卫浴事业部", "瓷砖事业部",
        ]
    finally:
        server.execute_query = original_execute


def test_full_org_path_builds_exact_hierarchy_filter_params():
    params = build_params("2026-07-31", "东鹏/瓷砖事业部/供应链中心")
    assert params["dept_path"] is True
    assert params["dept_name"] is None
    assert [params[f"dept_lvl{i}"] for i in range(1, 6)] == [
        "东鹏", "瓷砖事业部", "供应链中心", None, None,
    ]
    selector = server._OrganizationSelector(
        "东鹏/瓷砖事业部/供应链中心", "TILE-SC", 3)
    coded = build_params("2026-07-31", selector)
    assert coded["dept_code"] == "TILE-SC"
    assert coded["dept_code_level"] == 3
    assert server._history_mapping_target_names("审计稽察部") == [
        "审计稽察部", "审计稽核部", "审计稽查部"
    ]


def test_child_org_resolution_for_all_levels():
    original_execute = server.execute_query
    levels = {"一级": 1, "二级": 2, "三级": 3, "四级": 4, "五级": 5}
    try:
        def fake_execute(sql, params=None):
            parent = params["parent_department"]
            if "SELECT DISTINCT matched_level" in sql:
                return ([{"matched_level": levels[parent]}] if parent in levels else [])
            level = levels[parent]
            expected_child_field = f"lvl{level + 1}_dept_name"
            assert f"SELECT DISTINCT {expected_child_field} AS department" in sql
            return [{"department": f"{parent}的下级A"}, {"department": f"{parent}的下级B"}]

        server.execute_query = fake_execute
        for parent, parent_level in (("一级", 1), ("二级", 2), ("三级", 3), ("四级", 4)):
            got_parent, got_child, children = server._list_child_orgs("2026-07-31", parent)
            assert got_parent == parent_level
            assert got_child == parent_level + 1
            assert children == [f"{parent}的下级A", f"{parent}的下级B"]

        assert server._list_child_orgs("2026-07-31", "五级") == (5, None, [])
        assert server._list_child_orgs("2026-07-31", "不存在") == (None, None, [])
    finally:
        server.execute_query = original_execute


def test_compare_second_level_orgs():
    original = {
        "get_latest_snapshot_date": server.get_latest_snapshot_date,
        "get_latest_stat_month": server.get_latest_stat_month,
        "execute_query": server.execute_query,
        "_query_single_metric": server._query_single_metric,
        "_query_dm_metric": server._query_dm_metric,
    }
    try:
        server.get_latest_snapshot_date = lambda date, table: "2026-07-31"
        server.get_latest_stat_month = lambda date, table: "2026-07"

        def fake_execute(sql, params=None):
            if "SELECT DISTINCT matched_level" in sql:
                return [{"matched_level": 2}]
            if "SELECT DISTINCT lvl3_dept_name AS department" in sql:
                return [{"department": "三级A"}, {"department": "三级B"}]
            return [{"ok": 1}]

        server.execute_query = fake_execute

        def fake_single(metric_key, snapshot_date, dept_name="", stat_month=None, role="", **kwargs):
            values = {"三级A": 10, "三级B": 20}
            return {"value": values[dept_name]}

        def fake_dm(metric_key, target_date, dept_name="", role="", **kwargs):
            values = {"三级A": 30, "三级B": 15}
            return {"value": values[dept_name]}

        server._query_single_metric = fake_single
        server._query_dm_metric = fake_dm
        result = json.loads(server.compare_second_level_orgs(
            "2026-07-31", "东鹏", "headcount,turnover_rate"))
        data = result["data"]
        assert data["parent_department"] == "东鹏"
        assert data["parent_level"] == 2
        assert data["child_level"] == 3
        assert [row["department"] for row in data["organizations"]] == ["三级A", "三级B"]
        assert data["comparison"]["headcount"]["ranking"][0]["department"] == "三级B"
        assert data["comparison"]["turnover_rate"]["ranking"][0]["department"] == "三级A"
    finally:
        for name, value in original.items():
            setattr(server, name, value)


def test_company_level_one_business_scope():
    original = {
        "get_latest_snapshot_date": server.get_latest_snapshot_date,
        "get_latest_stat_month": server.get_latest_stat_month,
        "execute_query": server.execute_query,
        "_query_single_metric": server._query_single_metric,
        "get_metric_yearly_trend": server.get_metric_yearly_trend,
    }
    trend_calls = []
    try:
        server.get_latest_snapshot_date = lambda date, table: "2026-07-31"
        server.get_latest_stat_month = lambda date, table: "2026-07"

        def fake_execute(sql, params=None):
            if params and params.get("dongpeng_name") == "东鹏":
                assert params["president_office_name"] == "总裁办"
                assert params["board_name"] == "董事会"
                assert "lvl3_dept_name AS department" in sql
                assert "lvl2_dept_name AS department" in sql
                assert server.TABLE_EMP in sql
                assert "TRIM(lvl3_dept_name) IS NOT NULL" in sql
                assert "<> ''" not in sql
                return [
                    {"department": "东鹏三级A"},
                    {"department": "东鹏三级B"},
                    {"department": "总裁办"},
                ]
            return [{"ok": 1}]

        server.execute_query = fake_execute
        server._query_single_metric = lambda key, snapshot, dept_name="", **kwargs: {
            "value": {"东鹏三级A": 10, "东鹏三级B": 20, "总裁办": 5, "其他": 3}[dept_name]
        }

        def fake_yearly_trend(metric_keys, date, department="", role=""):
            trend_calls.append((metric_keys, department, role))
            return json.dumps({
                "data": {
                    "years": [2023, 2024, 2025, 2026],
                    "historical_org_mapping": (
                        {"2023": {"target_department": department}}
                        if department.startswith("东鹏三级") else {}
                    ),
                    "metrics": [
                        {
                            "key": "headcount",
                            "trend": [{"year": year, "value": year - 2000} for year in range(2023, 2027)],
                        },
                        {
                            "key": "age_structure",
                            "is_distribution": True,
                            "trend": [
                                {
                                    "year": year,
                                    "distribution": [{"dimension": "30-39岁", "count": year - 2000}],
                                }
                                for year in range(2023, 2027)
                            ],
                        },
                    ],
                }
            }, ensure_ascii=False)

        server.get_metric_yearly_trend = fake_yearly_trend
        result = json.loads(server.compare_second_level_orgs(
            "2026-07-31",
            "请按公司一级组织维度分析",
            "headcount,age_structure",
            include_yearly_trend=True,
        ))
        data = result["data"]
        assert data["parent_department"] == "公司一级组织"
        assert data["parent_level"] is None and data["child_level"] is None
        assert data["analysis_dimension"] == "公司一级组织"
        assert data["scope_definition"] == "东鹏下所有直属三级组织 + 总裁办/董事会（二级组织本身）+ 其他组织"
        assert [row["department"] for row in data["organizations"]] == [
            "东鹏三级A", "东鹏三级B", "总裁办", "其他"
        ]
        assert data["yearly_trend_enabled"] is True
        assert data["comparison_metric_keys"] == ["headcount"]
        assert "age_structure" not in data["comparison"]
        assert data["comparison"]["headcount"]["ranking"][0]["department"] == "东鹏三级B"
        assert all(row["yearly_trend"]["years"] == [2023, 2024, 2025, 2026]
                   for row in data["organizations"])
        assert all(len(row["yearly_trend"]["metrics"]) == 2
                   for row in data["organizations"])
        assert trend_calls == [
            ("headcount,age_structure", "东鹏三级A", ""),
            ("headcount,age_structure", "东鹏三级B", ""),
            ("headcount,age_structure", "总裁办", ""),
            ("headcount,age_structure", "其他", ""),
        ]
        for alias in server._COMPANY_LEVEL_ONE_ALIASES:
            assert server._is_company_level_one_request(alias)
    finally:
        for name, value in original.items():
            setattr(server, name, value)


def test_yearly_trend_uses_historical_org_mapping():
    original = {
        "get_latest_snapshot_date": server.get_latest_snapshot_date,
        "get_latest_stat_month": server.get_latest_stat_month,
        "execute_query": server.execute_query,
        "_query_single_metric": server._query_single_metric,
    }
    calls = []
    try:
        server.get_latest_snapshot_date = lambda date, table: (
            "2026-07-31" if date.startswith("2026") else f"{date[:4]}-12-31"
        )
        server.get_latest_stat_month = lambda date, table: (
            "2026-07" if date.startswith("2026") else f"{date[:4]}-12"
        )

        def fake_execute(sql, params=None):
            if "AS is_level3" in sql:
                return [{"is_level3": True}]
            if "old_org_name" in sql:
                assert params["stat_year"] == "2026"
                assert params["target_org_names"] == ["当前三级组织"]
                return [
                    {"old_org_name": "历史组织A"},
                    {"old_org_name": "历史组织B"},
                ]
            return [{"ok": 1}]

        server.execute_query = fake_execute

        def fake_single(metric_key, snapshot_date, dept_name="", stat_month=None, role="", **kwargs):
            calls.append({"year": snapshot_date[:4], "dept_name": dept_name, **kwargs})
            # 一次查询收到两个历史组织，模拟数据库在同一 SQL 中完成合并计算。
            return {"value": len(kwargs.get("dept_names") or [dept_name])}

        server._query_single_metric = fake_single
        result = json.loads(server.get_metric_yearly_trend(
            "headcount", "2026-07-31", department="当前三级组织"))
        data = result["data"]
        expected_2023 = ["当前三级组织", "历史组织A", "历史组织B"]
        mapping_2023 = data["historical_org_mapping"]["2023"]
        assert mapping_2023 == {
            "target_department": "当前三级组织",
            "source_departments": expected_2023,
            "aggregation": "combined",
        }
        trend_2023 = data["metrics"][0]["trend"][0]
        assert trend_2023["department"] == "当前三级组织"
        assert trend_2023["historical_source_departments"] == expected_2023
        assert trend_2023["aggregation"] == "combined"
        assert trend_2023["value"] == 3
        assert calls[0]["dept_name"] == ""
        assert calls[0]["dept_names"] == expected_2023
        assert len([call for call in calls if call["year"] == "2023"]) == 1
        assert calls[-1]["dept_name"] == "当前三级组织"
    finally:
        for name, value in original.items():
            setattr(server, name, value)


def test_yearly_trend_degrades_with_mapping_permission_warning():
    original = {
        "get_latest_snapshot_date": server.get_latest_snapshot_date,
        "get_latest_stat_month": server.get_latest_stat_month,
        "execute_query": server.execute_query,
        "_query_single_metric": server._query_single_metric,
    }
    try:
        server.get_latest_snapshot_date = lambda date, table: (
            "2026-07-31" if date.startswith("2026") else f"{date[:4]}-12-31"
        )
        server.get_latest_stat_month = lambda date, table: (
            "2026-07" if date.startswith("2026") else f"{date[:4]}-12"
        )

        def fake_execute(sql, params=None):
            if "AS is_level3" in sql:
                return [{"is_level3": True}]
            if "old_org_name" in sql:
                raise PermissionError("SELECT permission denied")
            return [{"ok": 1}]

        server.execute_query = fake_execute
        server._query_single_metric = lambda *args, **kwargs: {"value": 7}

        result = json.loads(server.get_metric_yearly_trend(
            "headcount", "2026-07-31", department="当前三级组织"))
        data = result["data"]
        status = data["historical_org_mapping_status"]
        assert status["required"] is True
        assert status["available"] is False
        assert status["error_type"] == "PermissionError"
        assert data["data_quality_warnings"] == [status["message"]]
        trend = data["metrics"][0]["trend"]
        assert all(point["value"] == 7 for point in trend)
        assert all(
            point["historical_org_mapping_used"] is False
            and point["data_quality_warning"] == status["message"]
            for point in trend[:3]
        )
        assert "data_quality_warning" not in trend[-1]
    finally:
        for name, value in original.items():
            setattr(server, name, value)


if __name__ == "__main__":
    test_org_leaf_name_normalizes_full_paths()
    test_same_name_orgs_are_resolved_by_code_and_queried_by_full_path()
    test_full_org_path_builds_exact_hierarchy_filter_params()
    test_child_org_resolution_for_all_levels()
    test_compare_second_level_orgs()
    test_company_level_one_business_scope()
    test_yearly_trend_uses_historical_org_mapping()
    test_yearly_trend_degrades_with_mapping_permission_warning()
    print("组织对比与历史映射离线校验通过")
