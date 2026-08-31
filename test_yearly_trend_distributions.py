"""离线校验：年度趋势分布类白名单（全量 12 个分布指标）——无需数据库。

覆盖：
1. 全部分布类指标均已进入白名单（无残留拒绝）
2. 年龄/学历/司龄结构（双 GROUP BY SQL）年度点聚合为整体分布（dimension 不重复、percentage/total 正确）
3. 单分组 SQL 分布（性别/岗位层级/职系/MPO/前中后台/在岗状态/人才盘点）直接取分布行
4. 离职司龄/离职年龄分布（过程记录表）按 stat_month 取数，且含 role 注入
5. 无 12 月快照/无 12 月数据时 note 标记
6. 角色过滤参数正确注入（role_talent_review='1'）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402


def main():
    # 1) 白名单全覆盖：所有分布类指标均进白名单
    def is_dist(k):
        return "dimension_value" in server.METRICS[k]["sql_template"]

    dist_keys = [k for k in server.METRICS if is_dist(k)]
    not_in = [k for k in dist_keys if k not in server._YEARLY_TREND_DISTRIBUTIONS]
    assert not not_in, f"仍有分布类未进白名单: {not_in}"
    assert len(dist_keys) == len(server._YEARLY_TREND_DISTRIBUTIONS), (
        dist_keys, server._YEARLY_TREND_DISTRIBUTIONS)
    print(f"[1] 全量 {len(dist_keys)} 个分布指标已进白名单 OK")

    # 2) 构造假数据：按 SQL 特征路由（先离职表，再员工表；员工表内先精确后宽泛）
    age_rows = [
        {"job_level": "员工级", "dimension_value": "25-29岁", "count": 40},
        {"job_level": "员工级", "dimension_value": "30-34岁", "count": 30},
        {"job_level": "经理级", "dimension_value": "30-34岁", "count": 20},
        {"job_level": "经理级", "dimension_value": "35-39岁", "count": 10},
    ]
    review_rows = [
        {"dimension_value": "7-绩效之星", "count": 5, "percentage": 50.0},
        {"dimension_value": "未盘点", "count": 5, "percentage": 50.0},
    ]
    gender_rows = [
        {"dimension_value": "男", "count": 60, "percentage": 60.0},
        {"dimension_value": "女", "count": 40, "percentage": 40.0},
    ]
    lvl_rows = [
        {"dimension_value": "经理级", "count": 30, "percentage": 30.0},
        {"dimension_value": "员工级", "count": 70, "percentage": 70.0},
    ]
    turnover_rows = [
        {"dimension_value": "0-1年", "count": 12, "percentage": 60.0},
        {"dimension_value": "1-3年", "count": 8, "percentage": 40.0},
    ]

    seen_role = []
    seen_turnover_month = []

    def fake_execute(sql, params=None):
        if sql.strip().startswith("SELECT 1"):
            return [{"ok": 1}]
        # 角色过滤参数注入检查（高潜人才 => role_talent_review='1'）
        if params and params.get("role_talent_review") == "1":
            seen_role.append(sql[:60])
        # 离职分布（过程记录表）：校验 stat_month 透传（格式 YYYY-MM）
        if "DWR_HR_EMP_TRMNT_INFO_F" in sql:
            assert params.get("stat_month") and "-" in params["stat_month"], params.get("stat_month")
            seen_turnover_month.append(sql[:60])
            return turnover_rows
        # —— 以下均为员工信息表 ——
        # 注意：角色过滤 WHERE 也含 talent_review_result，须先匹配结构字段
        if "emp_lvl_name" in sql:
            return lvl_rows
        if "age_sectn" in sql:
            return age_rows
        if "div_sectn" in sql:
            return age_rows
        if "ed_name AS dimension_value" in sql:
            return age_rows
        if "gender AS dimension_value" in sql:
            return gender_rows
        if "kw_market" in sql:
            return lvl_rows
        if "mp_post_types" in sql:
            return lvl_rows
        if "试用期" in sql:
            return lvl_rows
        if "emp_sub_group_name" in sql:
            return lvl_rows
        if "fee_type_name" in sql:
            return [
                {"dimension_value": "直接人员", "count": 40, "percentage": 40.0},
                {"dimension_value": "间接人员", "count": 60, "percentage": 60.0},
            ]
        if "talent_review_result" in sql:
            return review_rows
        raise AssertionError(f"未识别的 SQL: {sql[:80]}")

    server.execute_query = fake_execute
    server.get_latest_snapshot_date = lambda date, table: f"{date[:4]}-12-31"
    server.get_latest_stat_month = lambda date, table: f"{date[:4]}-12"

    all_keys = ",".join(sorted(dist_keys))
    out = server.get_metric_yearly_trend(all_keys, "2026-07-31", role="高潜人才")
    res = json.loads(out)
    assert "error" not in res, res
    data = res["data"]  # _format_result 把内容包在 data 下，summary 在顶层
    metrics = {m["key"]: m for m in data["metrics"]}
    print(f"[2] 调用成功，返回 {len(metrics)} 个指标: {'、'.join(metrics)}")

    # 3) 年龄结构年度点：聚合整体 + 不重复 + percentage/total 正确
    age_trend = metrics["age_structure"]["trend"]
    assert len(age_trend) == 4, len(age_trend)
    for p in age_trend:
        dist = p["distribution"]
        dims = [d["dimension"] for d in dist]
        assert len(dims) == len(set(dims)), f"维度重复: {dims}"
        assert p["total"] == 100, p["total"]
        by_dim = {d["dimension"]: d for d in dist}
        assert by_dim["25-29岁"]["count"] == 40 and by_dim["25-29岁"]["percentage"] == 40.0, dist
        assert by_dim["30-34岁"]["count"] == 50 and by_dim["30-34岁"]["percentage"] == 50.0, dist
        assert by_dim["35-39岁"]["count"] == 10 and by_dim["35-39岁"]["percentage"] == 10.0, dist
    print("[3] 年龄结构年度点聚合 OK（dimension 去重、percentage/total 正确）")

    # 4) 单分组 SQL 分布：直接取分布行（含 percentage 原值）
    gender_trend = metrics["gender_structure"]["trend"]
    for p in gender_trend:
        assert p["total"] == 100
        assert p["distribution"][0]["percentage"] == 60.0, p["distribution"]
    for k in ("job_level_distribution", "job_series_distribution", "mpo_distribution",
              "front_mid_back_distribution", "employment_status_distribution"):
        t = metrics[k]["trend"]
        for p in t:
            assert p["total"] == 100
            assert any(d["dimension"] == "经理级" for d in p["distribution"]), k
    print("[4] 单分组分布（性别/层级/职系/MPO/前中后台/在岗状态）年度点 OK")

    fee_type_trend = metrics["fee_type_distribution"]["trend"]
    for p in fee_type_trend:
        assert p["total"] == 100
        by_dim = {d["dimension"]: d for d in p["distribution"]}
        assert by_dim["直接人员"]["count"] == 40
        assert by_dim["间接人员"]["percentage"] == 60.0
    print("[4b] 费用类型分布（直接/间接人员）年度点 OK")

    # 5) 人才盘点结果分布仍正常
    review_trend = metrics["talent_review_result_distribution"]["trend"]
    for p in review_trend:
        assert p["total"] == 10
        assert "7-绩效之星" in [d["dimension"] for d in p["distribution"]]
    print("[5] 人才盘点结果分布年度点 OK")

    # 6) 离职分布：按 stat_month=YYYY-12 取数 + total 正确
    for k in ("turnover_by_tenure_distribution", "turnover_by_age_distribution"):
        t = metrics[k]["trend"]
        for p in t:
            assert p["stat_month"].endswith("-12"), p
            assert p["total"] == 20, p
            by_dim = {d["dimension"]: d for d in p["distribution"]}
            assert by_dim["0-1年"]["count"] == 12 and by_dim["0-1年"]["percentage"] == 60.0, p
    assert seen_turnover_month, "离职分布未按 stat_month 取数"
    print(f"[6] 离职分布按 stat_month 取数 OK（{len(seen_turnover_month)} 次查询）")

    # 7) role 透传：高潜人才过滤参数已注入 SQL（员工表类分布）
    assert seen_role, "角色过滤参数未注入 SQL"
    print(f"[7] role=高潜人才 已注入 SQL（{len(seen_role)} 次）OK")

    # 8) 无 12 月快照/无 12 月数据场景：该年点带 note
    server.get_latest_snapshot_date = lambda date, table: (
        "2026-10-31" if date[:4] == "2026" else f"{date[:4]}-12-31"
    )
    server.get_latest_stat_month = lambda date, table: (
        "2026-10" if date[:4] == "2026" else f"{date[:4]}-12"
    )
    out2 = server.get_metric_yearly_trend(
        "age_structure,turnover_by_age_distribution", "2026-07-31")
    d2 = json.loads(out2)
    assert "error" not in d2, d2
    m2 = {m["key"]: m for m in d2["data"]["metrics"]}
    p_age26 = [p for p in m2["age_structure"]["trend"] if p["year"] == 2026][0]
    assert "note" in p_age26 and "12月快照" in p_age26["note"], p_age26
    assert p_age26["snapshot_date"] == "2026-10-31", p_age26
    p_turn26 = [p for p in m2["turnover_by_age_distribution"]["trend"] if p["year"] == 2026][0]
    assert "note" in p_turn26 and "12月数据" in p_turn26["note"], p_turn26
    assert p_turn26["stat_month"] == "2026-10", p_turn26
    print("[8] 无12月快照/无12月数据 note 标记 OK")

    print("\n全部离线校验通过 ✅")


if __name__ == "__main__":
    main()
