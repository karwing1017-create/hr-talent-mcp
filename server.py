"""
HR 人才盘点 MCP Server

通过 MCP 协议向智能体暴露人才盘点数据查询能力。
LLM 调用这些工具获取算好的数据，不直接写 SQL、不算指标。

三种运行模式：

  1. 本地 STDIO（默认，单人使用）：
     python server.py

  2. 远程 Streamable HTTP（推荐，多人共享，需 API Key）：
     MCP_TRANSPORT=streamable-http MCP_HOST=0.0.0.0 MCP_PORT=8000 python server.py

  3. 远程 SSE（兼容旧客户端）：
     MCP_TRANSPORT=sse MCP_HOST=0.0.0.0 MCP_PORT=8000 python server.py

环境变量：
  数据库：DW_HOST / DW_PORT / DW_DATABASE / DW_USER / DW_PASSWORD
  传输：  MCP_TRANSPORT (stdio|sse|streamable-http, 默认 stdio)
          MCP_HOST (默认 127.0.0.1, 远程部署设 0.0.0.0)
          MCP_PORT (默认 8000)
  认证：  MCP_API_KEYS (逗号分隔的 API Key 列表, HTTP 模式必填)
"""

import json
import os
import traceback
from typing import Annotated

from mcp.server.mcpserver import MCPServer

from config import logger, check_config, TABLE_HISTORY_ORG_MAPPING
from db import execute_query, get_latest_snapshot_date, get_latest_stat_month
from metrics import (
    METRICS,
    build_params,
    list_metrics,
    get_metric,
    DIMENSION_CONFIG,
    EXCLUDED_EMP_GROUPS,
    MGMT_LEVELS,
    ALL_JOB_LEVELS,
    TABLE_EMP,
    TABLE_DEPT,
    TABLE_DEPT_STAT,
)


mcp = MCPServer("hr-talent-mcp")


# 人员角色可选值（与 metrics.build_params 的归一化保持一致）
ROLE_OPTIONS = ("M岗", "管理干部", "P岗", "关键岗位", "高潜人才", "本科以上人员")


def normalize_role(role: str) -> str:
    """
    校验并归一化人员角色入参。

    返回标准化字符串（"M岗"/"P岗"/"关键岗位"/"高潜人才"/"本科以上人员"），空值返回 ""（查全部人员）。
    非法值抛出 ValueError，附带可选项说明。
    """
    role = (role or "").strip()
    if role == "":
        return ""
    if role in ROLE_OPTIONS:
        return role
    if role.upper() in ("M", "P"):
        return "M岗" if role.upper() == "M" else "P岗"
    if role in ("高潜", "潜力"):
        return "高潜人才"
    if role in ("本科及以上人员", "本科以上", "本科及以上", "高学历人员"):
        return "本科以上人员"
    raise ValueError(
        f"不支持的人员角色 '{role}'，可选：M岗(或管理干部) / P岗 / 关键岗位 / 高潜人才 / 本科以上人员，"
        f"留空表示查询全部人员"
    )


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def _format_result(data: dict, summary: str = "") -> str:
    """将结果格式化为 JSON 字符串，附带摘要"""
    result = {"data": data}
    if summary:
        result["summary"] = summary
    return json.dumps(result, ensure_ascii=False, default=str)


class _OrganizationSelector(str):
    """内部组织选择器：对外表现为完整路径，同时携带精确组织编码。"""

    def __new__(cls, path: str, code: str, level: int):
        value = str.__new__(cls, path)
        value.organization_code = code
        value.organization_level = level
        return value


def _resolve_department_matches(snapshot_date: str, department: str) -> list[dict]:
    """按员工快照中的组织编码识别同名组织，并返回完整层级路径。"""
    name = str(department or "").strip()
    if not name or "/" in name or _is_company_level_one_request(name):
        return []

    sql = f"""
        SELECT DISTINCT
            lvl1_dept_name, lvl2_dept_name, lvl3_dept_name,
            lvl4_dept_name, lvl5_dept_name, lvl6_dept_name,
            lvl7_dept_name, lvl8_dept_name, lvl9_dept_name,
            lvl1_dept_code, lvl2_dept_code, lvl3_dept_code,
            lvl4_dept_code, lvl5_dept_code, lvl6_dept_code,
            lvl7_dept_code, lvl8_dept_code, lvl9_dept_code
        FROM {TABLE_EMP}
        WHERE stat_date = %(snapshot_date)s::date
          AND (
              lvl1_dept_name = %(department)s OR
              lvl2_dept_name = %(department)s OR
              lvl3_dept_name = %(department)s OR
              lvl4_dept_name = %(department)s OR
              lvl5_dept_name = %(department)s OR
              lvl6_dept_name = %(department)s OR
              lvl7_dept_name = %(department)s OR
              lvl8_dept_name = %(department)s OR
              lvl9_dept_name = %(department)s
          )
    """
    rows = execute_query(sql, {"snapshot_date": snapshot_date, "department": name})
    matches: dict[tuple[str, str], dict] = {}
    for row in rows:
        levels = [row.get(f"lvl{i}_dept_name") for i in range(1, 10)]
        codes = [row.get(f"lvl{i}_dept_code") for i in range(1, 10)]
        matched_levels = [i for i, value in enumerate(levels, start=1) if value == name]
        if not matched_levels:
            continue
        matched_level = max(matched_levels)
        path_parts = [str(value).strip() for value in levels[:matched_level] if value]
        if not path_parts:
            continue
        path = "/".join(path_parts)
        organization_code = str(codes[matched_level - 1] or "").strip()
        key = (organization_code, path)
        matches[key] = {
            "department": name,
            "organization_code": organization_code or None,
            "organization_path": path,
            "organization_level": matched_level,
            "parent_department": (
                str(levels[matched_level - 2]).strip()
                if matched_level > 1 and levels[matched_level - 2]
                else None
            ),
        }
    return [matches[key] for key in sorted(matches)]


def _query_ambiguous_departments(
    snapshot_date: str,
    department: str,
    query_one,
) -> str | None:
    """同名组织命中多个路径时，分别查询并返回，不做跨路径聚合。"""
    matches = _resolve_department_matches(snapshot_date, department)
    if len(matches) <= 1:
        return None

    organizations = []
    for match in matches:
        selector = _OrganizationSelector(
            match["organization_path"],
            match.get("organization_code") or "",
            match["organization_level"],
        )
        payload = json.loads(query_one(selector))
        organizations.append({**match, "result": payload.get("data", payload)})

    data = {
        "snapshot_date": snapshot_date,
        "query_department": department.strip(),
        "ambiguous_department_name": True,
        "match_count": len(organizations),
        "organizations": organizations,
    }
    return _format_result(
        data,
        f"组织名称“{department.strip()}”命中 {len(organizations)} 个不同上级组织，已按组织路径分别返回",
    )


def _query_single_metric(
    metric_key: str, snapshot_date: str, dept_name: str = "", stat_month: str | None = None,
    role: str = "", dept_names: list[str] | tuple[str, ...] | None = None,
    company_level_one_excluded_names: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """
    查询单个标量指标的值。

    快照表指标（员工/组织/编制）按 stat_date 取月末快照；
    过程记录表指标（离职明细/留存率）按 stat_month 的 YYYY-MM 取数。
    stat_month 缺省时取 snapshot_date 前 7 位（YYYY-MM）。
    role 为人员角色过滤（空=全部），仅对员工信息表指标生效。
    """
    metric = METRICS[metric_key]
    params = build_params(
        snapshot_date, dept_name, stat_month=stat_month, role=role, dept_names=dept_names,
        company_level_one_excluded_names=company_level_one_excluded_names)
    rows = execute_query(metric["sql_template"], params)
    if rows:
        row = rows[0]
        result = {
            "key": metric_key,
            "name": metric["name"],
            "value": row["value"],
            "unit": metric["unit"],
            "formula": metric["formula"],
            "color_rule": metric["color_rule"],
        }
        # 指标 SQL 可额外返回 cnt 列（如「本科及以上人数及占比」），透传为 count 字段
        if "cnt" in row and row["cnt"] is not None:
            result["count"] = row["cnt"]
        return result
    return {
        "key": metric_key,
        "name": metric["name"],
        "value": None,
        "unit": metric["unit"],
        "error": "无数据",
    }


def _query_dm_metric(
    metric_key: str, target_date: str, dept_name: str = "", role: str = "",
    dept_names: list[str] | tuple[str, ...] | None = None,
    company_level_one_excluded_names: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """查询 DM 层汇总表指标（stat_month 月度）

    注意：DM 汇总表无人员角色字段，role 不参与筛选（仅回传透传）。
    """
    metric = METRICS[metric_key]
    stat_month = get_latest_stat_month(target_date, TABLE_DEPT_STAT)
    params = build_params(
        target_date, dept_name, stat_month=stat_month, role=role, dept_names=dept_names,
        company_level_one_excluded_names=company_level_one_excluded_names)
    rows = execute_query(metric["sql_template"], params)
    if rows:
        return {
            "key": metric_key,
            "name": metric["name"],
            "value": rows[0]["value"],
            "unit": metric["unit"],
            "formula": metric["formula"],
            "color_rule": metric["color_rule"],
            "stat_month": stat_month,
        }
    return {
        "key": metric_key,
        "name": metric["name"],
        "value": None,
        "unit": metric["unit"],
        "error": "无数据",
    }


def _query_mixed_metric(
    metric_key: str, snapshot_date: str, dept_name: str = "", stat_month: str | None = None,
    role: str = "", dept_names: list[str] | tuple[str, ...] | None = None,
    company_level_one_excluded_names: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """
    查询跨表指标（快照表按 stat_date + 汇总/明细表按 stat_month 月度）。

    如：晋升率 = 晋升表(stat_month) / 员工信息表(stat_date)。
    调用方应显式传入 stat_month（如 get_talent_flow 中的目标月度）；
    缺省时回退为按 snapshot_date 查 DM 层最新月度。
    role 为人员角色过滤（空=全部），仅对员工信息表指标生效。
    """
    metric = METRICS[metric_key]
    if stat_month is None:
        stat_month = get_latest_stat_month(snapshot_date, TABLE_DEPT_STAT)
    params = build_params(
        snapshot_date, dept_name, stat_month=stat_month, role=role, dept_names=dept_names,
        company_level_one_excluded_names=company_level_one_excluded_names)
    rows = execute_query(metric["sql_template"], params)
    if rows:
        return {
            "key": metric_key,
            "name": metric["name"],
            "value": rows[0]["value"],
            "unit": metric["unit"],
            "formula": metric["formula"],
            "color_rule": metric["color_rule"],
        }
    return {
        "key": metric_key,
        "name": metric["name"],
        "value": None,
        "unit": metric["unit"],
        "error": "无数据",
    }


def _query_distribution_metric(
    metric_key: str, snapshot_date: str, dept_name: str = "", stat_month: str | None = None,
    role: str = "", dept_names: list[str] | tuple[str, ...] | None = None,
    company_level_one_excluded_names: list[str] | tuple[str, ...] | None = None,
) -> list[dict]:
    """
    查询分布类指标（返回多行分组结果）。

    快照表分布按 stat_date 取月末快照；离职分布（过程记录表）按 stat_month 的 YYYY-MM 取数。
    stat_month 缺省时取 snapshot_date 前 7 位（YYYY-MM）。
    role 为人员角色过滤（空=全部），仅对员工信息表指标生效。
    """
    metric = METRICS[metric_key]
    params = build_params(
        snapshot_date, dept_name, stat_month=stat_month, role=role, dept_names=dept_names,
        company_level_one_excluded_names=company_level_one_excluded_names)
    rows = execute_query(metric["sql_template"], params)
    return [
        {
            "dimension": row.get("dimension_value", "未知"),
            "count": row["count"],
            "percentage": row["percentage"],
        }
        for row in rows
    ]


def _query_structure_by_job_level(
    metric_key: str, snapshot_date: str, dept_name: str = "", stat_month: str | None = None,
    role: str = "", dept_names: list[str] | tuple[str, ...] | None = None,
    company_level_one_excluded_names: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """
    查询带『职级』细分的分布类指标（学历/年龄/司龄结构）。

    指标 SQL 按 (emp_post_lvl_name 职级, 结构维度) 分组，仅返回原始计数。
    本函数在 Python 层聚合出两层结构：
    - overall:      合并所有职级后的整体结构分布（percentage 占全体比例）
    - by_job_level: 每个职级内部的结构分布（percentage 占该职级内部比例）

    role 角色过滤已在 SQL（SQL_WHERE_EMP / SQL_ROLE_FILTER）中应用。
    """
    metric = METRICS[metric_key]
    params = build_params(
        snapshot_date, dept_name, stat_month=stat_month, role=role, dept_names=dept_names,
        company_level_one_excluded_names=company_level_one_excluded_names)
    rows = execute_query(metric["sql_template"], params)

    overall_counts: dict = {}
    by_level: dict = {}
    for r in rows:
        jl = r.get("job_level") or "未分级"
        dim = r.get("dimension_value") or "未知"
        cnt = int(r["count"])
        overall_counts[dim] = overall_counts.get(dim, 0) + cnt
        lvl = by_level.setdefault(jl, {})
        lvl[dim] = lvl.get(dim, 0) + cnt

    grand = sum(overall_counts.values())
    overall = [
        {
            "dimension": d,
            "count": c,
            "percentage": round(c * 100.0 / grand, 1) if grand else None,
        }
        for d, c in sorted(overall_counts.items(), key=lambda x: (-x[1], str(x[0])))
    ]

    # 职级按层级常量排序（未知职级排末尾）
    def _jl_order(k: str):
        try:
            return (0, ALL_JOB_LEVELS.index(k))
        except ValueError:
            return (1, k)

    by_job_level = {}
    for jl in sorted(by_level.keys(), key=_jl_order):
        dims = by_level[jl]
        lvl_total = sum(dims.values())
        by_job_level[jl] = [
            {
                "dimension": d,
                "count": c,
                "percentage": round(c * 100.0 / lvl_total, 1) if lvl_total else None,
            }
            for d, c in sorted(dims.items(), key=lambda x: (-x[1], str(x[0])))
        ]

    return {
        "overall": overall,
        "by_job_level": by_job_level,
        "grand_total": grand,
    }


# ═══════════════════════════════════════════════════════════════
# MCP 工具定义
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
def get_talent_overview(
    date: Annotated[str, "统计日期，格式 YYYY-MM-DD，如 2026-07-31"],
    department: Annotated[str, "部门名称（可选，支持一级~五级部门名称筛选，留空查全公司）"] = "",
    role: Annotated[str, "人员角色筛选（可选）：M岗(或管理干部) / P岗 / 关键岗位 / 高潜人才 / 本科以上人员，留空查全部人员"] = "",
) -> str:
    """
    获取人才综合指标总览数据。

    返回指定日期的人员规模、结构、关键人才、管理干部和组织架构等综合指标快照，包括：
    - 在职人数、实习生人数、平均年龄、年龄中位数、平均司龄、司龄中位数、95后数量及占比
    - 管理干部人数及占比、管理干部平均年龄、管幅平均值、管幅5人以下占比、管理干部本科以上占比
    - 关键岗位人数及占比、关键岗位本科以上占比、本科及以上人数及占比、95后干部占比、新锐班人数
    - 组织机构数量、组织层级数、较年初减少层级数、编制数、满编率
    - 5人以下组织数量
    - 岗位层级分布、MPO分布、年龄/学历/司龄/性别结构、职系/费用类型/前中后台/在岗状态分布、人才盘点结果分布

    role 可按人员角色筛选：M岗(职族=管理职族) / P岗(职族=专业职族) / 关键岗位(是否关键岗位=是) /
    高潜人才(人才盘点结果=7-绩效之星/8-潜力之星/9-超级明星，该字段仅每年12月快照有值，非12月查询可能为0) /
    本科以上人员(学历=本科/硕士/博士)，
    留空则统计全部人员。所有指标在同一快照日期下统计，便于综合分析。

    重要路由规则：本工具不得用于用户仅询问“人才盘点”“九宫格”或“九宫格人才”的场景。
    这些词专指 talent_review_result 人才盘点结果，应调用 get_talent_structure，
    dimension 必须传 talent_review_result，只返回九宫格人才盘点结果分布。
    """
    try:
        snapshot_date = get_latest_snapshot_date(date, TABLE_EMP)
        ambiguous_result = _query_ambiguous_departments(
            snapshot_date, department,
            lambda path: get_talent_overview(date, path, role),
        )
        if ambiguous_result is not None:
            return ambiguous_result
        stat_month = get_latest_stat_month(date, TABLE_DEPT_STAT)
        role = normalize_role(role)
        logger.info(
            "人才盘点总览查询: target=%s, snapshot=%s, stat_month=%s, dept=%s, role=%s",
            date, snapshot_date, stat_month, department, role,
        )

        # stat_date 快照表标量指标
        emp_scalars = [
            "headcount", "intern_count", "avg_age",
            "age_median", "avg_tenure", "tenure_median",
            "post_95s_count", "post_95s_ratio",
            "management_cadre_ratio", "management_span",
            "xrb_count",
            "key_position_ratio",
            "bachelor_above_ratio",
            "small_org_count",
        ]
        scalars = [_query_single_metric(m, snapshot_date, department, role=role) for m in emp_scalars]

        # DM 层汇总表标量指标
        dm_scalars = [
            "span_under_5_ratio", "org_count", "org_levels",
            "level_reduction_yoy", "headcount_quota", "fill_rate",
        ]
        dm_results = [_query_dm_metric(m, date, department, role=role) for m in dm_scalars]
        scalars.extend(dm_results)

        # 分布类指标
        distributions = {
            "job_level": _query_distribution_metric("job_level_distribution", snapshot_date, department, role=role),
            "mpo": _query_distribution_metric("mpo_distribution", snapshot_date, department, role=role),
            "age": _query_structure_by_job_level("age_structure", snapshot_date, department, role=role)["overall"],
            "education": _query_structure_by_job_level("education_structure", snapshot_date, department, role=role)["overall"],
            "tenure": _query_structure_by_job_level("tenure_structure", snapshot_date, department, role=role)["overall"],
            "gender": _query_distribution_metric("gender_structure", snapshot_date, department, role=role),
            "fee_type": _query_distribution_metric("fee_type_distribution", snapshot_date, department, role=role),
            "front_mid_back": _query_distribution_metric("front_mid_back_distribution", snapshot_date, department, role=role),
            "employment_status": _query_distribution_metric("employment_status_distribution", snapshot_date, department, role=role),
            "talent_review_result": _query_distribution_metric("talent_review_result_distribution", snapshot_date, department, role=role),
        }

        # 构建摘要
        hc = next((s for s in scalars if s["key"] == "headcount"), {})
        ms = next((s for s in scalars if s["key"] == "management_span"), {})
        ol = next((s for s in scalars if s["key"] == "org_levels"), {})
        fr = next((s for s in scalars if s["key"] == "fill_rate"), {})
        summary_parts = []
        if hc.get("value") is not None:
            summary_parts.append(f"在职人数 {hc['value']}{hc['unit']}")
        if ms.get("value") is not None:
            summary_parts.append(f"管幅均值 {ms['value']}{ms['unit']}")
        if ol.get("value") is not None:
            summary_parts.append(f"组织层级 {ol['value']}{ol['unit']}")
        if fr.get("value") is not None:
            summary_parts.append(f"满编率 {fr['value']}{fr['unit']}")
        summary = "；".join(summary_parts) if summary_parts else "暂无数据"

        data = {
            "snapshot_date": snapshot_date,
            "stat_month": stat_month,
            "target_date": date,
            "department": department or "全公司",
            "role": role or "全部人员",
            "scalars": scalars,
            "distributions": distributions,
        }
        return _format_result(data, summary)

    except Exception as e:
        logger.error(f"get_talent_overview 失败: {e}\n{traceback.format_exc()}")
        return json.dumps({"error": str(e), "type": type(e).__name__}, ensure_ascii=False)


@mcp.tool()
def get_talent_flow(
    date: Annotated[str, "统计日期，格式 YYYY-MM-DD"],
    department: Annotated[str, "部门名称（可选，留空查全公司）"] = "",
) -> str:
    """
    获取人才流动数据（离职/入职/晋升/编制）。

    返回指定日期的人才流动核心指标，包括：
    - 离职：离职人数、离职率、主动离职率、被动离职率
      MP主动/被动离职率、管理干部主动/被动离职率
      离职司龄分布、离职年龄分布
    - 入职：新员工人数、新员工占比、6个月留存率、1年留存率
    - 晋升：晋升率、近3年晋升人数、近3年未晋升MP人数、降级率、转正人数

    离职/入职汇总数据来自 DM 层月度汇总表，晋升/转正来自人事晋升事务表。
    """
    try:
        snapshot_date = get_latest_snapshot_date(date, TABLE_EMP)
        ambiguous_result = _query_ambiguous_departments(
            snapshot_date, department,
            lambda path: get_talent_flow(date, path),
        )
        if ambiguous_result is not None:
            return ambiguous_result
        stat_month = get_latest_stat_month(date, TABLE_DEPT_STAT)
        logger.info(
            "人才流动查询: target=%s, snapshot=%s, stat_month=%s, dept=%s",
            date, snapshot_date, stat_month, department,
        )

        # 离职指标（明细表按 stat_month 月度取数 + DM 汇总表 stat_month）
        turnover_scalars = []
        turnover_scalars.append(_query_single_metric("turnover_count", snapshot_date, department, stat_month=stat_month))
        for m in ["turnover_rate", "voluntary_turnover_rate", "involuntary_turnover_rate",
                   "mp_voluntary_turnover_rate", "mp_involuntary_turnover_rate",
                   "mgmt_voluntary_turnover_rate", "mgmt_involuntary_turnover_rate"]:
            turnover_scalars.append(_query_dm_metric(m, date, department))

        # 离职分布（司龄/年龄，离职明细表按 stat_month 月度取数）
        turnover_distributions = {
            "by_tenure": _query_distribution_metric("turnover_by_tenure_distribution", snapshot_date, department, stat_month=stat_month),
            "by_age": _query_distribution_metric("turnover_by_age_distribution", snapshot_date, department, stat_month=stat_month),
        }

        # 入职指标（汇总表/留存率，按月取数）
        hiring_scalars = []
        hiring_scalars.append(_query_dm_metric("new_hire_count", date, department))
        hiring_scalars.append(_query_mixed_metric("new_hire_ratio", snapshot_date, department, stat_month=stat_month))
        for m in ["new_hire_6m_retention", "new_hire_1y_retention"]:
            hiring_scalars.append(_query_single_metric(m, snapshot_date, department, stat_month=stat_month))

        # 晋升/发展指标（跨表查询，按 stat_month 月度 + snapshot_date 快照）
        dev_scalars = []
        for m in ["promotion_rate", "promotion_3yr_count", "mp_unpromoted_3yr_count",
                   "demotion_rate", "regularized_count"]:
            dev_scalars.append(_query_mixed_metric(m, snapshot_date, department, stat_month=stat_month))

        all_scalars = turnover_scalars + hiring_scalars + dev_scalars

        # 构建摘要
        tc = next((s for s in all_scalars if s["key"] == "turnover_count"), {})
        tr = next((s for s in all_scalars if s["key"] == "turnover_rate"), {})
        nh = next((s for s in all_scalars if s["key"] == "new_hire_count"), {})
        pr = next((s for s in all_scalars if s["key"] == "promotion_rate"), {})
        summary_parts = []
        if tc.get("value") is not None:
            summary_parts.append(f"离职 {tc['value']}{tc['unit']}")
        if tr.get("value") is not None:
            summary_parts.append(f"离职率 {tr['value']}{tr['unit']}")
        if nh.get("value") is not None:
            summary_parts.append(f"新员工 {nh['value']}{nh['unit']}")
        if pr.get("value") is not None:
            summary_parts.append(f"晋升率 {pr['value']}{pr['unit']}")
        summary = "；".join(summary_parts) if summary_parts else "暂无数据"

        data = {
            "snapshot_date": snapshot_date,
            "stat_month": stat_month,
            "target_date": date,
            "department": department or "全公司",
            "turnover": turnover_scalars,
            "turnover_distributions": turnover_distributions,
            "hiring": hiring_scalars,
            "development": dev_scalars,
        }
        return _format_result(data, summary)

    except Exception as e:
        logger.error(f"get_talent_flow 失败: {e}\n{traceback.format_exc()}")
        return json.dumps({"error": str(e), "type": type(e).__name__}, ensure_ascii=False)


@mcp.tool()
def get_talent_structure(
    dimension: Annotated[str, "分析维度：job_level(岗位层级) / age(年龄) / education(学历) / tenure(司龄) / gender(性别) / mpo(MPO分类) / job_series(职系) / fee_type(费用类型/直接间接人员；提到制造成本人数、期间成本人数等同类词时必须传 fee_type) / front_mid_back(前中后台) / employment_status(在岗状态) / talent_review_result(人才盘点结果，仅12月快照有值)。用户提到‘人才盘点’‘九宫格’‘九宫格人才’时，必须传 talent_review_result；也支持直接传这些中文词。"],
    date: Annotated[str, "统计日期，格式 YYYY-MM-DD"],
    department: Annotated[str, "部门名称（可选，留空查全公司）"] = "",
    role: Annotated[str, "人员角色筛选（可选）：M岗(或管理干部) / P岗 / 关键岗位 / 高潜人才 / 本科以上人员，留空查全部人员"] = "",
) -> str:
    """
    按指定维度获取人才结构分布数据。

    强制意图路由：用户提到“人才盘点”“九宫格”“九宫格人才”或“人才盘点结果”时，
    必须调用本工具并使用 dimension="talent_review_result"，读取 talent_review_result 字段；
    不得改用 get_talent_overview 返回其他综合指标。

    用户提到“制造成本人数”“期间成本人数”“制造费用人数”“期间费用人数”或同类词语时，
    必须调用本工具并使用 dimension="fee_type"，返回直接人员、间接人员两类完整人数及占比分布；
    即使用户只提到其中一类，也不得只返回单项。

    返回该维度下各分组的：
    - 人数（count）
    - 占比（percentage）

    可选维度：
    - job_level: 岗位层级分布（董事长级/总裁级/...员工级）
    - age: 年龄结构分布
    - education: 学历结构分布
    - tenure: 司龄结构分布
    - gender: 性别结构分布
    - mpo: MPO分类分布（MP/O辅/OO）
    - job_series: 职系分布（员工子组）
    - fee_type: 费用类型分布（制造费用=直接人员，期间费用=间接人员）
    - front_mid_back: 前中后台分布
    - employment_status: 在岗状态分布（试用期/已转正）
    - talent_review_result: 人才盘点结果分布（talent_review_result 字段仅每年12月快照有值，其余月份统一记为'未盘点'）

    role 可按人员角色筛选：M岗(职族=管理职族) / P岗(职族=专业职族) / 关键岗位(是否关键岗位=是) /
    高潜人才(人才盘点结果=7-绩效之星/8-潜力之星/9-超级明星，该字段仅每年12月快照有值，非12月查询可能为0) /
    本科以上人员(学历=本科/硕士/博士)，
    留空则统计全部人员。

    注意：education / age / tenure 三个维度在返回『整体结构(overall)』的同时，
    额外返回『by_job_level』——按职级(emp_post_lvl_name)细分的结构分布：
    每个职级内部给出各结构分类的人数及占比（占该职级内部比例），便于做"整体 + 分职级"两层分析。
    其余维度仅返回整体分布(items)。
    """
    try:
        snapshot_date = get_latest_snapshot_date(date, TABLE_EMP)
        role = normalize_role(role)

        ambiguous_result = _query_ambiguous_departments(
            snapshot_date, department,
            lambda path: get_talent_structure(dimension, date, path, role),
        )
        if ambiguous_result is not None:
            return ambiguous_result

        dimension = (dimension or "").strip()
        if dimension in ("人才盘点", "九宫格", "九宫格人才", "人才盘点结果"):
            dimension = "talent_review_result"
        elif dimension in (
            "费用类型", "费用分类", "直接间接人员", "直接人员", "间接人员",
            "费用类型人数及占比", "成本类型人数及占比",
            "制造成本人数", "期间成本人数", "制造费用人数", "期间费用人数",
            "制造成本人数及占比", "期间成本人数及占比", "制造费用人数及占比", "期间费用人数及占比",
            "制造成本人员", "期间成本人员", "制造费用人员", "期间费用人员",
            "制造成本占比", "期间成本占比", "制造费用占比", "期间费用占比",
        ):
            dimension = "fee_type"

        dim_to_metric = {
            "job_level": "job_level_distribution",
            "age": "age_structure",
            "education": "education_structure",
            "tenure": "tenure_structure",
            "gender": "gender_structure",
            "mpo": "mpo_distribution",
            "job_series": "job_series_distribution",
            "fee_type": "fee_type_distribution",
            "front_mid_back": "front_mid_back_distribution",
            "employment_status": "employment_status_distribution",
            "talent_review_result": "talent_review_result_distribution",
        }

        if dimension not in dim_to_metric:
            available = "、".join(dim_to_metric.keys())
            return json.dumps({"error": f"不支持的维度 '{dimension}'，可选: {available}"}, ensure_ascii=False)

        metric_key = dim_to_metric[dimension]
        metric = METRICS[metric_key]

        # 学历/年龄/司龄结构：额外提供『职级』细分（emp_post_lvl_name）
        if dimension in ("education", "age", "tenure"):
            res = _query_structure_by_job_level(metric_key, snapshot_date, department, role=role)
            overall = res["overall"]
            by_jl = res["by_job_level"]
            dim_label = DIMENSION_CONFIG.get(dimension, {}).get("label", dimension)
            top_items = sorted(overall, key=lambda x: x["count"], reverse=True)[:3]
            summary_parts = [f"{r['dimension']} {r['count']}人({r['percentage']}%)" for r in top_items]
            summary = (
                f"{dim_label}整体分布 Top3: " + "、".join(summary_parts) +
                f"；已按 {len(by_jl)} 个职级细分"
            ) if summary_parts else "暂无数据"
            data = {
                "snapshot_date": snapshot_date,
                "dimension": dimension,
                "dimension_label": dim_label,
                "department": department or "全公司",
                "role": role or "全部人员",
                "metric_name": metric["name"],
                "formula": metric["formula"],
                "grand_total": res["grand_total"],
                "overall": overall,
                "by_job_level": by_jl,
            }
            return _format_result(data, summary)

        rows = _query_distribution_metric(metric_key, snapshot_date, department, role=role)

        dim_label = DIMENSION_CONFIG.get(dimension, {}).get("label", dimension)
        top_items = sorted(rows, key=lambda x: x["count"], reverse=True)[:3]
        summary_parts = [f"{r['dimension']} {r['count']}人({r['percentage']}%)" for r in top_items]
        summary = f"{dim_label}分布 Top3: " + "、".join(summary_parts) if summary_parts else "暂无数据"

        data = {
            "snapshot_date": snapshot_date,
            "dimension": dimension,
            "dimension_label": dim_label,
            "department": department or "全公司",
            "role": role or "全部人员",
            "total_count": sum(r["count"] for r in rows),
            "items": rows,
            "metric_name": metric["name"],
            "formula": metric["formula"],
        }
        return _format_result(data, summary)

    except Exception as e:
        logger.error(f"get_talent_structure 失败: {e}\n{traceback.format_exc()}")
        return json.dumps({"error": str(e), "type": type(e).__name__}, ensure_ascii=False)


@mcp.tool()
def detect_talent_risk(
    date: Annotated[str, "统计日期，格式 YYYY-MM-DD"],
    department: Annotated[str, "部门名称（可选，留空查全公司）"] = "",
) -> str:
    """
    识别综合人才指标中的风险项。

    基于当前快照数据，自动检测以下风险：
    - 关键岗位人才不足：关键岗位占比低于 10%
    - 管幅过窄：管幅平均值低于 4 人（管理层冗余）
    - 管幅过宽：管幅平均值超过 12 人（管理跨度过大）
    - 组织层级过深：组织层级数超过 6 层
    - 高学历占比偏低：本科及以上占比低于 30%
    - 离职率偏高：超过 5%
    - 满编率偏低：低于 80%
    - 管幅5人以下占比偏高：超过 40%

    每个风险项包含：风险等级（高/中/低）、当前值、阈值、建议措施。
    """
    try:
        snapshot_date = get_latest_snapshot_date(date, TABLE_EMP)
        ambiguous_result = _query_ambiguous_departments(
            snapshot_date, department,
            lambda path: detect_talent_risk(date, path),
        )
        if ambiguous_result is not None:
            return ambiguous_result
        stat_month = get_latest_stat_month(date, TABLE_DEPT_STAT)
        logger.info(f"人才风险检测: snapshot={snapshot_date}, stat_month={stat_month}, dept={department}")

        risks = []

        # 1. 关键岗位人才不足
        kp_ratio = _query_single_metric("key_position_ratio", snapshot_date, department)
        if kp_ratio.get("value") is not None:
            val = float(kp_ratio["value"])
            if val < 5:
                risks.append({"risk": "关键岗位人才严重不足", "level": "高", "current": f"{val}%",
                              "threshold": "≥10%", "suggestion": "关键岗位占比过低，建议盘点关键岗位清单并制定人才补充计划"})
            elif val < 10:
                risks.append({"risk": "关键岗位人才偏少", "level": "中", "current": f"{val}%",
                              "threshold": "≥10%", "suggestion": "关注关键岗位人才储备，提前布局继任计划"})

        # 2. 管幅异常
        ms = _query_single_metric("management_span", snapshot_date, department)
        if ms.get("value") is not None:
            val = float(ms["value"])
            if val < 4:
                risks.append({"risk": "管幅过窄，管理层冗余", "level": "中", "current": f"平均{val}人",
                              "threshold": "4-12人", "suggestion": "管理干部平均直接下属偏少，考虑精简管理层级或合并职责"})
            elif val > 12:
                risks.append({"risk": "管幅过宽，管理跨度过大", "level": "中", "current": f"平均{val}人",
                              "threshold": "4-12人", "suggestion": "管理跨度偏大，可能影响管理质量，考虑增设副职或拆分团队"})

        # 3. 组织层级过深
        ol = _query_dm_metric("org_levels", date, department)
        if ol.get("value") is not None:
            val = int(ol["value"])
            if val > 7:
                risks.append({"risk": "组织层级过深", "level": "高", "current": f"{val}层",
                              "threshold": "≤6层", "suggestion": "组织架构层级过多，影响决策效率，建议推进组织扁平化"})
            elif val > 6:
                risks.append({"risk": "组织层级偏多", "level": "低", "current": f"{val}层",
                              "threshold": "≤6层", "suggestion": "关注组织层级变化趋势，适时优化"})

        # 4. 高学历占比偏低
        ba = _query_single_metric("bachelor_above_ratio", snapshot_date, department)
        if ba.get("value") is not None:
            val = float(ba["value"])
            if val < 30:
                risks.append({"risk": "高学历人才占比偏低", "level": "中", "current": f"{val}%",
                              "threshold": "≥30%", "suggestion": "本科及以上学历占比偏低，建议在招聘中提升学历要求或加强在职学历提升"})

        # 5. 离职率偏高
        tr = _query_dm_metric("turnover_rate", date, department)
        if tr.get("value") is not None:
            val = float(tr["value"])
            if val > 10:
                risks.append({"risk": "离职率过高", "level": "高", "current": f"{val}%",
                              "threshold": "≤5%", "suggestion": "离职率显著偏高，建议深入分析离职原因，制定留人方案"})
            elif val > 5:
                risks.append({"risk": "离职率偏高", "level": "中", "current": f"{val}%",
                              "threshold": "≤5%", "suggestion": "关注离职趋势，分析关键岗位和核心人才的留任风险"})

        # 6. 满编率偏低
        fr = _query_dm_metric("fill_rate", date, department)
        if fr.get("value") is not None:
            val = float(fr["value"])
            if val < 70:
                risks.append({"risk": "满编率严重不足", "level": "高", "current": f"{val}%",
                              "threshold": "≥80%", "suggestion": "编制使用率严重偏低，大量岗位空缺，建议加快招聘节奏"})
            elif val < 80:
                risks.append({"risk": "满编率偏低", "level": "中", "current": f"{val}%",
                              "threshold": "≥80%", "suggestion": "编制使用率偏低，关注关键岗位的招聘进度"})

        # 7. 管幅5人以下占比偏高
        s5 = _query_dm_metric("span_under_5_ratio", date, department)
        if s5.get("value") is not None:
            val = float(s5["value"])
            if val > 40:
                risks.append({"risk": "管幅5人以下占比过高", "level": "中", "current": f"{val}%",
                              "threshold": "≤40%", "suggestion": "管幅过窄的管理干部占比偏高，管理层可能冗余，建议推进组织扁平化"})

        # 构建摘要
        high_count = sum(1 for r in risks if r["level"] == "高")
        mid_count = sum(1 for r in risks if r["level"] == "中")
        if risks:
            summary = f"检测到 {len(risks)} 个风险项（高风险 {high_count} 个，中风险 {mid_count} 个）"
            if high_count > 0:
                high_items = [r["risk"] for r in risks if r["level"] == "高"]
                summary += "，高风险: " + "、".join(high_items)
        else:
            summary = "未检测到明显人才风险，各项指标在正常范围内"

        data = {
            "snapshot_date": snapshot_date,
            "stat_month": stat_month,
            "department": department or "全公司",
            "risk_count": len(risks),
            "high_risk_count": high_count,
            "medium_risk_count": mid_count,
            "risks": risks,
        }
        return _format_result(data, summary)

    except Exception as e:
        logger.error(f"detect_talent_risk 失败: {e}\n{traceback.format_exc()}")
        return json.dumps({"error": str(e), "type": type(e).__name__}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
# 直属下一级组织横向对比
# ═══════════════════════════════════════════════════════════════

_DEFAULT_SECOND_LEVEL_COMPARE_METRICS = (
    "headcount", "avg_age", "avg_tenure", "management_cadre_ratio",
    "key_position_ratio", "bachelor_above_ratio", "turnover_rate", "fill_rate",
)


_ORG_LEVEL_FIELDS = {
    1: "lvl1_dept_name",
    2: "lvl2_dept_name",
    3: "lvl3_dept_name",
    4: "lvl4_dept_name",
    5: "lvl5_dept_name",
}

_COMPANY_LEVEL_ONE_ALIASES = frozenset({
    "公司一级组织",
    "公司一级组织维度",
    "按公司一级组织维度分析",
    "请按公司一级组织维度分析",
})

_HISTORY_MAPPING_TARGET_ALIASES = {
    # 映射表历史填报名与员工快照当前名不一致。
    "审计稽察部": ("审计稽核部", "审计稽查部"),
}


def _is_company_level_one_request(department: str) -> bool:
    """识别“公司一级组织”这一特殊业务分析口径。"""
    return (department or "").strip() in _COMPANY_LEVEL_ONE_ALIASES


def _org_leaf_name(department: str) -> str:
    """将组织架构表中的完整路径转换为员工指标表使用的短名称。"""
    value = str(department or "").strip()
    return value.rsplit("/", 1)[-1].strip()


def _history_mapping_target_names(department: str) -> list[str]:
    """返回映射表中可用于匹配当前组织的名称（含已知业务别名）。"""
    current = _org_leaf_name(department)
    return list(dict.fromkeys((current, *_HISTORY_MAPPING_TARGET_ALIASES.get(current, ()))))


def _list_company_level_one_orgs(snapshot_date: str) -> list[str]:
    """返回公司一级组织口径：东鹏直属三级组织 + 总裁办/董事会。

    员工指标表存储短组织名，组织架构表可能存储完整路径。这里直接从员工
    快照表枚举，确保返回值可直接用于后续指标查询。
    """
    sql = f"""
        SELECT department
        FROM (
            SELECT DISTINCT lvl3_dept_name AS department
            FROM {TABLE_EMP}
            WHERE stat_date = %(snapshot_date)s::date
              AND lvl2_dept_name = %(dongpeng_name)s
              AND lvl3_dept_name IS NOT NULL
              AND TRIM(lvl3_dept_name) IS NOT NULL

            UNION

            SELECT DISTINCT lvl2_dept_name AS department
            FROM {TABLE_EMP}
            WHERE stat_date = %(snapshot_date)s::date
              AND lvl2_dept_name IN (
                  %(president_office_name)s,
                  %(board_name)s
              )
        ) company_level_one_orgs
        WHERE department IS NOT NULL
          AND TRIM(department) IS NOT NULL
        ORDER BY department
    """
    rows = execute_query(sql, {
        "snapshot_date": snapshot_date,
        "dongpeng_name": "东鹏",
        "president_office_name": "总裁办",
        "board_name": "董事会",
    })
    return [str(row["department"]).strip() for row in rows if row.get("department")]


def _company_level_one_excluded_names(
    snapshot_date: str,
    base_year: int,
    organizations: list[str] | None = None,
) -> list[str]:
    """返回“其他”分组需要排除的当前组织及其历史旧组织名称。"""
    names = set(organizations or _list_company_level_one_orgs(snapshot_date))
    sql = f"""
        SELECT target_org_name, old_org_name
        FROM {TABLE_HISTORY_ORG_MAPPING}
        WHERE CAST(stat_year AS TEXT) = %(stat_year)s
    """
    try:
        rows = execute_query(sql, {"stat_year": str(base_year)})
        for row in rows:
            if row.get("target_org_name"):
                names.add(str(row["target_org_name"]).strip())
            if row.get("old_org_name"):
                names.add(str(row["old_org_name"]).strip())
    except Exception as e:
        logger.warning("公司一级组织“其他”未能读取历史映射，暂仅排除当前组织: %s", e)
    return sorted(name for name in names if name)


def _list_child_orgs(
    snapshot_date: str,
    parent_department: str,
) -> tuple[int | None, int | None, list[str]]:
    """识别组织所在层级，并返回当前快照中的全部直属下一级组织。"""
    path_parts = [part.strip() for part in parent_department.split("/") if part.strip()]
    if len(path_parts) > 1:
        parent_level = len(path_parts)
        if parent_level > 5:
            return None, None, []
        if parent_level == 5:
            return parent_level, None, []
        child_level = parent_level + 1
        child_field = _ORG_LEVEL_FIELDS[child_level]
        path_conditions = "\n          AND ".join(
            f"{_ORG_LEVEL_FIELDS[level]} = %(path_lvl{level})s"
            for level in range(1, parent_level + 1)
        )
        params = {"snapshot_date": snapshot_date}
        params.update({f"path_lvl{i}": value for i, value in enumerate(path_parts, start=1)})
        child_sql = f"""
            SELECT DISTINCT {child_field} AS department
            FROM {TABLE_DEPT}
            WHERE stat_date = %(snapshot_date)s::date
              AND {path_conditions}
              AND {child_field} IS NOT NULL
              AND TRIM({child_field}) IS NOT NULL
            ORDER BY {child_field}
        """
        rows = execute_query(child_sql, params)
        organizations = [
            f"{parent_department}/{_org_leaf_name(row['department'])}"
            for row in rows if row.get("department")
        ]
        return parent_level, child_level, organizations

    level_sql = f"""
        SELECT DISTINCT matched_level
        FROM (
            SELECT CASE
                WHEN lvl1_dept_name = %(parent_department)s THEN 1
                WHEN lvl2_dept_name = %(parent_department)s THEN 2
                WHEN lvl3_dept_name = %(parent_department)s THEN 3
                WHEN lvl4_dept_name = %(parent_department)s THEN 4
                WHEN lvl5_dept_name = %(parent_department)s THEN 5
            END AS matched_level
            FROM {TABLE_DEPT}
            WHERE stat_date = %(snapshot_date)s::date
              AND (
                  lvl1_dept_name = %(parent_department)s OR
                  lvl2_dept_name = %(parent_department)s OR
                  lvl3_dept_name = %(parent_department)s OR
                  lvl4_dept_name = %(parent_department)s OR
                  lvl5_dept_name = %(parent_department)s
              )
        ) matched
        WHERE matched_level IS NOT NULL
        ORDER BY matched_level
    """
    params = {
        "snapshot_date": snapshot_date,
        "parent_department": parent_department,
    }
    level_rows = execute_query(level_sql, params)
    if not level_rows:
        return None, None, []

    # 同名组织意外出现在多个层级时，优先使用更高层级，保证查询结果确定。
    parent_level = min(int(row["matched_level"]) for row in level_rows)
    if parent_level >= 5:
        return parent_level, None, []

    child_level = parent_level + 1
    parent_field = _ORG_LEVEL_FIELDS[parent_level]
    child_field = _ORG_LEVEL_FIELDS[child_level]
    child_sql = f"""
        SELECT DISTINCT {child_field} AS department
        FROM {TABLE_DEPT}
        WHERE stat_date = %(snapshot_date)s::date
          AND {parent_field} = %(parent_department)s
          AND {child_field} IS NOT NULL
          AND TRIM({child_field}) IS NOT NULL
        ORDER BY {child_field}
    """
    rows = execute_query(child_sql, params)
    organizations = [
        _org_leaf_name(row["department"]) for row in rows if row.get("department")
    ]
    return parent_level, child_level, organizations


def _is_level3_department(department: str, snapshot_date: str) -> bool:
    """按员工指标表的短名称判断当前组织是否为三级组织。"""
    sql = f"""
        SELECT EXISTS (
            SELECT 1
            FROM {TABLE_EMP}
            WHERE stat_date = %(snapshot_date)s::date
              AND lvl3_dept_name = %(department)s
        ) AS is_level3
    """
    rows = execute_query(sql, {
        "snapshot_date": snapshot_date,
        "department": _org_leaf_name(department),
    })
    return bool(rows and rows[0].get("is_level3"))


def _get_historical_org_names(
    department: str,
    year: int,
    base_year: int,
    use_history_mapping: bool,
) -> list[str] | None:
    """将当前三级组织转换为指定历史年度对应的一个或多个旧组织名称。"""
    if not use_history_mapping or year >= base_year:
        return None

    sql = f"""
        SELECT old_org_name
        FROM {TABLE_HISTORY_ORG_MAPPING}
        WHERE CAST(stat_year AS TEXT) = %(stat_year)s
          AND target_org_name = ANY(%(target_org_names)s::text[])
          AND old_org_name IS NOT NULL
          AND TRIM(old_org_name) IS NOT NULL
        ORDER BY old_org_name
    """
    rows = execute_query(sql, {
        # stat_year 表示当前目标组织所属年度，不是被查询的历史年度。
        "stat_year": str(base_year),
        "target_org_names": _history_mapping_target_names(department),
    })
    old_names = sorted({
        str(row["old_org_name"]).strip()
        for row in rows
        if row.get("old_org_name")
    })
    # 当前组织可能在历史年度已经存在；历史口径需将其与旧组织一起合并。
    return list(dict.fromkeys([department, *old_names]))


def _query_comparison_metric(
    metric_key: str,
    snapshot_date: str,
    target_date: str,
    stat_month: str,
    department: str,
    role: str,
    company_level_one_excluded_names: list[str] | None = None,
) -> dict:
    """按指标类型复用现有查询口径，供直属下一级组织对比工具使用。"""
    if metric_key in _DM_METRIC_KEYS:
        return _query_dm_metric(
            metric_key, target_date, department, role=role,
            company_level_one_excluded_names=company_level_one_excluded_names)
    if metric_key in _MIXED_METRIC_KEYS:
        return _query_mixed_metric(
            metric_key, snapshot_date, department, stat_month=stat_month, role=role,
            company_level_one_excluded_names=company_level_one_excluded_names)
    return _query_single_metric(
        metric_key, snapshot_date, department, stat_month=stat_month, role=role,
        company_level_one_excluded_names=company_level_one_excluded_names)


@mcp.tool()
def compare_second_level_orgs(
    date: Annotated[str, "统计日期，格式 YYYY-MM-DD，如 2026-07-31"],
    department: Annotated[str, "查询组织名称，支持一级至五级；自动比较其直属下一级组织。用户提到‘按公司一级组织维度分析’时，必须传‘公司一级组织’，工具将比较东鹏直属三级组织、总裁办/董事会，并将其余组织合并为‘其他’。"],
    metric_keys: Annotated[
        str,
        "指标 key，多个用英文逗号分隔；留空使用核心指标：headcount,avg_age,avg_tenure,management_cadre_ratio,key_position_ratio,bachelor_above_ratio,turnover_rate,fill_rate",
    ] = "",
    role: Annotated[str, "人员角色（可选）：M岗/P岗/关键岗位/高潜人才/本科以上人员；留空查全部人员"] = "",
    include_yearly_trend: Annotated[
        bool,
        "是否同时返回每个组织近4年指标趋势，默认否。用户提到年度趋势、近4年趋势、历史趋势时必须传 true；趋势模式支持年龄/学历/九宫格等分布指标。",
    ] = False,
) -> str:
    """自动识别查询组织所在层级，比较其直属下一级组织的指标。

    兼容原工具名 compare_second_level_orgs，但不再限定查询组织必须是一级组织：
    一级组织比较二级组织，二级比较三级，三级比较四级，四级比较五级。
    返回逐指标排名、最大值、最小值和平均值；五级组织没有下一级，返回明确提示。

    强制业务口径：用户提到“请按公司一级组织维度分析”“按公司一级组织维度分析”
    或“公司一级组织”时，必须调用本工具并传 department="公司一级组织"。
    此时比较范围固定为：员工快照表中 lvl2_dept_name='东鹏' 下全部直属
    lvl3_dept_name，再加上 lvl2_dept_name='总裁办' 或 '董事会' 本身，
    其余未落入上述当前组织及历史映射旧组织的记录统一归入“其他”。

    年度趋势规则：用户要求“年度趋势”“近4年趋势”或“历史趋势”时，必须设置
    include_yearly_trend=true。工具会为每个组织调用近4年趋势逻辑；当前三级组织的历史年份
    自动使用 upload.upload_history_org_mapping_t 映射，一个或多个历史组织合并后展示为当前组织。
    趋势模式允许分布类指标，但分布指标只进入 yearly_trend，不参与当前值排名。
    """
    try:
        if not department or not department.strip():
            return json.dumps({"error": "department 不能为空，需传入一级组织名称"}, ensure_ascii=False)

        role = normalize_role(role)
        keys = [k.strip() for k in metric_keys.split(",") if k.strip()] or list(
            _DEFAULT_SECOND_LEVEL_COMPARE_METRICS
        )
        invalid = [key for key in keys if key not in METRICS]
        if invalid:
            return json.dumps(
                {"error": f"未知的指标 key: {invalid}，请调用 list_available_metrics 查看可用指标"},
                ensure_ascii=False,
            )
        distributions = [key for key in keys if _is_distribution_metric(key)]
        if distributions and not include_yearly_trend:
            return json.dumps(
                {"error": f"分布类指标仅支持年度趋势模式，请设置 include_yearly_trend=true: {distributions}"},
                ensure_ascii=False,
            )
        unsupported_distributions = [
            key for key in distributions if key not in _YEARLY_TREND_DISTRIBUTIONS
        ]
        if unsupported_distributions:
            return json.dumps(
                {"error": f"以下分布类指标不支持年度趋势: {unsupported_distributions}"},
                ensure_ascii=False,
            )
        scalar_keys = [key for key in keys if key not in distributions]

        snapshot_date = get_latest_snapshot_date(date, TABLE_EMP)
        ambiguous_result = _query_ambiguous_departments(
            snapshot_date, department,
            lambda path: compare_second_level_orgs(
                date, path, metric_keys, role, include_yearly_trend),
        )
        if ambiguous_result is not None:
            return ambiguous_result
        stat_month = get_latest_stat_month(date, TABLE_DEPT_STAT)
        company_level_one_mode = _is_company_level_one_request(department)
        company_other_exclusions = None
        if company_level_one_mode:
            organizations = _list_company_level_one_orgs(snapshot_date)
            parent_level = None
            child_level = None
            if not organizations:
                return json.dumps(
                    {"error": "公司一级组织口径下未找到东鹏直属三级组织或总裁办/董事会"},
                    ensure_ascii=False,
                )
            company_other_exclusions = _company_level_one_excluded_names(
                snapshot_date, int(date[:4]), organizations)
            organizations = [*organizations, "其他"]
        else:
            parent_level, child_level, organizations = _list_child_orgs(
                snapshot_date, department.strip())
            if parent_level is None:
                return json.dumps(
                    {"error": f"未找到组织 '{department}'，请确认组织名称和快照日期"},
                    ensure_ascii=False,
                )
            if child_level is None:
                return json.dumps(
                    {"error": f"组织 '{department}' 为五级组织，没有可比较的直属下一级组织"},
                    ensure_ascii=False,
                )
            if not organizations:
                return json.dumps(
                    {"error": f"组织 '{department}' 下未找到直属的{child_level}级组织"},
                    ensure_ascii=False,
                )

        rows = []
        for organization in organizations:
            values = {}
            for key in scalar_keys:
                result = _query_comparison_metric(
                    key, snapshot_date, date, stat_month, organization, role,
                    company_level_one_excluded_names=(
                        company_other_exclusions if organization == "其他" else None
                    ))
                values[key] = {
                    "name": METRICS[key]["name"],
                    "value": result.get("value"),
                    "unit": METRICS[key]["unit"],
                    **({"error": result["error"]} if result.get("error") else {}),
                }
            for key in distributions:
                values[key] = {
                    "name": METRICS[key]["name"],
                    "value": None,
                    "unit": METRICS[key]["unit"],
                    "note": "分布类指标请查看 yearly_trend，不参与当前值排名",
                }

            organization_row = {"department": organization, "metrics": values}
            if include_yearly_trend:
                trend_result = json.loads(get_metric_yearly_trend(
                    ",".join(keys), date, department=organization, role=role))
                if trend_result.get("error"):
                    organization_row["yearly_trend"] = {
                        "error": trend_result["error"],
                        "type": trend_result.get("type"),
                    }
                else:
                    trend_data = trend_result.get("data", {})
                    organization_row["yearly_trend"] = {
                        "years": trend_data.get("years", []),
                        "historical_org_mapping": trend_data.get("historical_org_mapping", {}),
                        "historical_org_mapping_status": trend_data.get(
                            "historical_org_mapping_status", {}),
                        "data_quality_warnings": trend_data.get("data_quality_warnings", []),
                        "metrics": trend_data.get("metrics", []),
                    }
            rows.append(organization_row)

        comparison = {}
        for key in scalar_keys:
            values = []
            for row in rows:
                value = row["metrics"][key]["value"]
                try:
                    if value is not None:
                        values.append((row["department"], float(value), value))
                except (TypeError, ValueError):
                    continue
            ranking = [
                {"department": name, "value": original, "rank": rank}
                for rank, (name, _, original) in enumerate(
                    sorted(values, key=lambda item: item[1], reverse=True), start=1
                )
            ]
            comparison[key] = {
                "name": METRICS[key]["name"],
                "unit": METRICS[key]["unit"],
                "average": round(sum(item[1] for item in values) / len(values), 2)
                if values else None,
                "max": max(values, key=lambda item: item[1])[2] if values else None,
                "min": min(values, key=lambda item: item[1])[2] if values else None,
                "ranking": ranking,
            }

        data = {
            "target_date": date,
            "snapshot_date": snapshot_date,
            "stat_month": stat_month,
            "parent_department": "公司一级组织" if company_level_one_mode else department.strip(),
            "parent_level": parent_level,
            "child_level": child_level,
            "analysis_dimension": "公司一级组织" if company_level_one_mode else "直属下一级组织",
            "scope_definition": (
                "东鹏下所有直属三级组织 + 总裁办/董事会（二级组织本身）+ 其他组织"
                if company_level_one_mode else
                f"{parent_level}级组织下的直属{child_level}级组织"
            ),
            "role": role or "全部人员",
            "metric_keys": keys,
            "comparison_metric_keys": scalar_keys,
            "yearly_trend_enabled": include_yearly_trend,
            "organizations": rows,
            "comparison": comparison,
        }
        summary_scope = (
            "公司一级组织" if company_level_one_mode else f"直属{child_level}级组织"
        )
        trend_suffix = "，并返回各组织近4年趋势" if include_yearly_trend else ""
        return _format_result(
            data,
            f"已完成 {len(organizations)} 个{summary_scope}的 {len(keys)} 项指标分析{trend_suffix}",
        )
    except Exception as e:
        logger.error(f"compare_second_level_orgs 失败: {e}\n{traceback.format_exc()}")
        return json.dumps({"error": str(e), "type": type(e).__name__}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
# 年度趋势查询
# ═══════════════════════════════════════════════════════════════

# 标量指标 → 查询类型分类（与 get_talent_overview / get_talent_flow 的调用方式保持一致）
_DM_METRIC_KEYS = frozenset({
    # DM 层月度汇总表指标（按 stat_month 取每年最后一个月）
    "span_under_5_ratio", "org_count", "org_levels", "level_reduction_yoy",
    "headcount_quota", "fill_rate",
    "turnover_rate", "voluntary_turnover_rate", "involuntary_turnover_rate",
    "mp_voluntary_turnover_rate", "mp_involuntary_turnover_rate",
    "mgmt_voluntary_turnover_rate", "mgmt_involuntary_turnover_rate",
    "new_hire_count",
})
_MIXED_METRIC_KEYS = frozenset({
    # 跨表指标（快照表 stat_date + 汇总/过程表 stat_month）
    "new_hire_ratio", "promotion_rate", "promotion_3yr_count",
    "mp_unpromoted_3yr_count", "demotion_rate", "regularized_count",
})
_YEARLY_TREND_DISTRIBUTIONS = frozenset({
    # 允许进入年度趋势的分布类指标（每年取该年 12 月快照的分布作为数据点）
    "talent_review_result_distribution",
    # 员工信息表快照分布：所有月份快照均有值，按年末快照统计整体结构
    "age_structure",
    "education_structure",
    "tenure_structure",
    "gender_structure",
    "job_level_distribution",
    "job_series_distribution",
    "fee_type_distribution",
    "mpo_distribution",
    "front_mid_back_distribution",
    "employment_status_distribution",
    # 离职分布（过程记录表 DWR_HR_EMP_TRMNT_INFO_F）：按该年最后统计月份（stat_month）取数
    "turnover_by_tenure_distribution",
    "turnover_by_age_distribution",
})
# 带职级细分的结构指标：SQL 为双 GROUP BY（emp_post_lvl_name, 维度），无 percentage 列，
# 年度趋势需走 _query_structure_by_job_level 聚合出整体结构（overall）作为该年分布
_STRUCTURE_WITH_JOB_LEVEL = frozenset({"age_structure", "education_structure", "tenure_structure"})
# 离职分布类指标：走 DWRHR 过程记录表，SQL 按 TO_CHAR(stat_date,'YYYY-MM')=%(stat_month)s 取数，
# 年度趋势必须用年内最后统计月份 month 而非年末快照 snap
_TURNOVER_DISTRIBUTION_KEYS = frozenset({
    "turnover_by_tenure_distribution",
    "turnover_by_age_distribution",
})
# 其余标量指标均为快照表/过程明细表指标，走 _query_single_metric


def _is_distribution_metric(metric_key: str) -> bool:
    """分布类指标返回多行分组结果（SQL 含 dimension_value 分组列）。

    默认不支持年度趋势；白名单 _YEARLY_TREND_DISTRIBUTIONS 中的指标除外
    （人才盘点结果、年龄/学历/司龄/性别结构、岗位层级/职系/费用类型/MPO/前中后台/在岗状态/离职司龄/离职年龄分布，
    每年取 12 月快照或 12 月统计月份的分布结构）。
    """
    return "dimension_value" in METRICS[metric_key].get("sql_template", "")


@mcp.tool()
def get_metric_yearly_trend(
    metric_keys: Annotated[str, "指标 key，多个用英文逗号分隔，如 'headcount,avg_age'（完整列表见 list_available_metrics）"],
    date: Annotated[str, "基准日期，格式 YYYY-MM-DD，如 2026-07-31；趋势取该日期所在年及往前 3 年，共 4 个年度"],
    department: Annotated[str, "部门名称（可选，留空查全公司）"] = "",
    role: Annotated[str, "人员角色（可选，留空查全部人员）：M岗(或管理干部)/P岗/关键岗位/高潜人才/本科以上人员"] = "",
) -> str:
    """
    查询指标近 4 年的年度趋势。

    对每个指标，按基准日期所在年份及往前 3 年（共 4 个年度）取每年年末数据点：
    - 快照表指标（在职人数、平均年龄、管理干部等）：取每年 12-31 当天或之前最近的快照（stat_date）
    - 月度指标（离职率、新员工数等 DM 汇总表指标，晋升率等跨表指标）：取每年 12 月或之前最近的月份（stat_month）
    - 过程记录表指标（离职人数、留存率等）：按该年最后一个月（YYYY-12）截取
    - 分布类白名单指标：取每年 12 月快照的分布结构（离职司龄/离职年龄分布按 12 月统计月份取数），
      trend 内每个点含 distribution 列表；其中人才盘点结果字段仅 12 月快照有值，
      其余结构/分布指标各月快照均有值
    - 基准年份尚未到年末时，自动取该年内最新数据点
    - 查询当前三级组织时，历史年度使用历史组织映射表查找旧组织；多个旧组织在同一次 SQL
      中合并计算，并统一以当前组织名称展示趋势
    - 若历史组织映射表不可访问，不中断查询；改按当前组织名称查询，并在返回结果中提供
      historical_org_mapping_status 和 data_quality_warnings，提示历史值可能不完整

    人员角色（role）：M岗=管理职族，P岗=专业职族，关键岗位=is_key_post=是，
    高潜人才=人才盘点结果∈{7-绩效之星,8-潜力之星,9-超级明星}（该字段仅每年12月快照有值），
    本科以上人员=学历∈{本科,硕士,博士}；留空查全部人员。
    仅对员工信息表类指标生效，DM 组织类指标不受角色影响。

    适用场景：分析指标逐年变化趋势（如在职人数、离职率、晋升人数的年度对比），
    以及各类分布指标（人才盘点结果、年龄/学历/司龄/性别结构、岗位层级/职系/费用类型/MPO/前中后台/在岗状态/
    离职司龄/离职年龄分布）的年度变化（跨年对比各分类占比）。
    """
    try:
        keys = [k.strip() for k in metric_keys.split(",") if k.strip()]
        if not keys:
            return json.dumps({"error": "metric_keys 不能为空"}, ensure_ascii=False)

        try:
            role = normalize_role(role)
        except ValueError as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        department = str(department or "").strip()

        invalid = [k for k in keys if k not in METRICS]
        if invalid:
            return json.dumps(
                {"error": f"未知的指标 key: {invalid}，请调用 list_available_metrics 查看可用指标"},
                ensure_ascii=False,
            )
        unsupported = [
            k for k in keys
            if _is_distribution_metric(k) and k not in _YEARLY_TREND_DISTRIBUTIONS
        ]
        if unsupported:
            return json.dumps(
                {"error": f"分布类指标不支持年度趋势: {unsupported}，请改用 get_talent_structure"},
                ensure_ascii=False,
            )

        if department:
            current_snapshot = get_latest_snapshot_date(date, TABLE_EMP)
            ambiguous_result = _query_ambiguous_departments(
                current_snapshot, department,
                lambda path: get_metric_yearly_trend(metric_keys, date, path, role),
            )
            if ambiguous_result is not None:
                return ambiguous_result

        base_year = int(date[:4])
        years = list(range(base_year - 3, base_year + 1))
        logger.info(
            "年度趋势查询: metrics=%s, base=%s, years=%s, dept=%s, role=%s",
            keys, date, years, department, role,
        )

        # 连通性预检：数据库不可达时快速失败，避免逐年定位查询反复超时
        try:
            execute_query("SELECT 1 AS ok")
        except Exception as e:
            return json.dumps(
                {"error": f"数据库暂不可达，请稍后重试: {e}", "type": type(e).__name__},
                ensure_ascii=False,
            )

        # 完整路径用于区分同名组织；路径查询不可退化成短名称历史映射，否则会再次合并。
        # 裸名称仍沿用既有历史映射能力。
        use_history_mapping = bool(department and department != "其他" and "/" not in department)
        company_other_exclusions = (
            _company_level_one_excluded_names(
                get_latest_snapshot_date(date, TABLE_EMP), base_year)
            if department == "其他"
            else None
        )

        historical_mapping = {}
        mapping_status = {
            "required": use_history_mapping,
            "available": True,
            "message": (
                "历史组织映射未启用"
                if not use_history_mapping
                else "历史组织映射表可用；未配置映射的组织沿用当前名称"
            ),
        }
        data_quality_warnings = []
        mapped_names = None
        if use_history_mapping:
            try:
                mapped_names = _get_historical_org_names(
                    department, base_year - 1, base_year, use_history_mapping)
            except Exception as e:
                mapping_status = {
                    "required": use_history_mapping,
                    "available": False,
                    "error_type": type(e).__name__,
                    "message": "历史组织映射表不可访问，历史年度将按当前组织名称查询，结果可能不完整",
                }
                data_quality_warnings.append(mapping_status["message"])
                logger.warning("历史组织映射查询失败，降级为当前组织名称口径: %s", e)
                mapped_names = None
        for y in years:
            year_mapped_names = mapped_names if y < base_year else None
            if year_mapped_names and year_mapped_names != [department]:
                historical_mapping[str(y)] = {
                    "target_department": department,
                    "source_departments": year_mapped_names,
                    "aggregation": "combined",
                }

        # 每年数据点定位：年末快照日期（≤12-31 最近）+ 年内最后统计月份（≤12 月最近）。
        # 年份不匹配说明该年无数据（get_latest_* 的 MIN 兜底会返回其他年份的值，需剔除）。
        anchors: dict[int, tuple[str | None, str | None]] = {}
        for y in years:
            year_end = f"{y}-12-31"
            snap = None
            month = None
            try:
                s = get_latest_snapshot_date(year_end, TABLE_EMP)
                if s[:4] == str(y):
                    snap = s
            except Exception:
                pass
            try:
                m = get_latest_stat_month(year_end, TABLE_DEPT_STAT)
                if m[:4] == str(y):
                    month = m
            except Exception:
                pass
            anchors[y] = (snap, month)

        results = []
        for key in keys:
            metric = METRICS[key]
            trend = []
            for y in years:
                snap, month = anchors[y]
                point: dict = {
                    "year": y,
                    "value": None,
                    "department": department or "全公司",
                }
                mapping_info = historical_mapping.get(str(y))
                mapped_names = mapping_info["source_departments"] if mapping_info else None
                query_department = department
                query_extra = {}
                if department == "其他":
                    query_extra["company_level_one_excluded_names"] = company_other_exclusions
                if mapped_names:
                    # 多个历史组织在同一次 SQL 中过滤：求和类指标合计，均值/比例类指标
                    # 基于合并后的明细重新计算；最终仍以当前组织名称展示趋势点。
                    query_department = ""
                    query_extra["dept_names"] = mapped_names
                    point["historical_source_departments"] = mapped_names
                    point["aggregation"] = "combined"
                    point["historical_org_mapping_used"] = True
                elif (
                    use_history_mapping
                    and not mapping_status["available"]
                    and y < base_year
                ):
                    point["historical_org_mapping_used"] = False
                    point["data_quality_warning"] = mapping_status["message"]
                if snap:
                    point["snapshot_date"] = snap
                if month:
                    point["stat_month"] = month

                if key in _DM_METRIC_KEYS:
                    if month:
                        r = _query_dm_metric(
                            key, f"{y}-12-31", query_department, role=role, **query_extra)
                        point["value"] = r.get("value")
                elif key in _MIXED_METRIC_KEYS:
                    if snap and month:
                        r = _query_mixed_metric(
                            key, snap, query_department, stat_month=month, role=role,
                            **query_extra)
                        point["value"] = r.get("value")
                elif key in _YEARLY_TREND_DISTRIBUTIONS:
                    # 分布类白名单：每年取 12 月快照 / 12 月统计月份的分布结构
                    if key in _TURNOVER_DISTRIBUTION_KEYS:
                        # 离职分布（过程记录表）：按该年最后统计月份取数
                        if month:
                            dist = _query_distribution_metric(
                                key, f"{y}-12-31", query_department,
                                stat_month=month, role=role, **query_extra)
                            point["distribution"] = dist
                            point["total"] = sum(int(d["count"]) for d in dist)
                            if month[5:7] != "12":
                                point["note"] = "该年无12月数据，按年内最新月份统计"
                    elif snap:
                        if key in _STRUCTURE_WITH_JOB_LEVEL:
                            # 年龄/学历/司龄结构：双 GROUP BY SQL，聚合出整体结构作为该年分布
                            res = _query_structure_by_job_level(
                                key, snap, query_department, role=role, **query_extra)
                            dist = res["overall"]
                            point["total"] = res["grand_total"]
                        else:
                            # 单分组 SQL（盘点/性别/层级/职系/MPO/前中后台/在岗状态），直接取分布行
                            dist = _query_distribution_metric(
                                key, snap, query_department, role=role, **query_extra)
                            point["total"] = sum(int(d["count"]) for d in dist)
                        point["distribution"] = dist
                        if snap[5:7] != "12":
                            point["note"] = "该年无12月快照，按年内最新快照统计"
                else:
                    if snap:
                        # 快照表指标不受 stat_month 影响；
                        # 过程表指标（离职人数/留存率）按该年最后月份取数
                        r = _query_single_metric(
                            key, snap, query_department, stat_month=month, role=role,
                            **query_extra)
                        point["value"] = r.get("value")

                if point.get("value") is None and "distribution" not in point:
                    point["note"] = "该年无数据"
                trend.append(point)

            results.append({
                "key": key,
                "name": metric["name"],
                "unit": metric["unit"],
                "formula": metric["formula"],
                "is_distribution": key in _YEARLY_TREND_DISTRIBUTIONS,
                "trend": trend,
            })

        # 构建摘要：标量指标输出 首年值 → 末年值；分布类输出各年 Top3
        summary_parts = []
        for r in results:
            if r.get("is_distribution"):
                parts = []
                for p in r["trend"]:
                    dist = p.get("distribution")
                    if not dist:
                        parts.append(f"{p['year']}年 无数据")
                        continue
                    top = sorted(dist, key=lambda x: x["count"], reverse=True)[:3]
                    top_str = "、".join(
                        f"{x['dimension']} {x['count']}人({x['percentage']}%)" for x in top
                    )
                    parts.append(f"{p['year']}年[{p.get('snapshot_date', '')}] Top3: {top_str}")
                summary_parts.append(f"{r['name']}: " + " | ".join(parts))
                continue
            valid = [p for p in r["trend"] if p["value"] is not None]
            if len(valid) >= 2:
                first, last = valid[0], valid[-1]
                summary_parts.append(
                    f"{r['name']} {first['year']}:{first['value']} → {last['year']}:{last['value']}{r['unit']}"
                )
            elif len(valid) == 1:
                summary_parts.append(f"{r['name']} 仅 {valid[0]['year']} 年有数据: {valid[0]['value']}{r['unit']}")
            else:
                summary_parts.append(f"{r['name']} 近4年无数据")
        summary = "；".join(summary_parts)

        data = {
            "base_date": date,
            "years": years,
            "department": department or "全公司",
            "role": role or "全部人员",
            "historical_org_mapping": historical_mapping,
            "historical_org_mapping_status": mapping_status,
            "data_quality_warnings": data_quality_warnings,
            "metrics": results,
        }
        return _format_result(data, summary)

    except Exception as e:
        logger.error(f"get_metric_yearly_trend 失败: {e}\n{traceback.format_exc()}")
        return json.dumps({"error": str(e), "type": type(e).__name__}, ensure_ascii=False)


@mcp.tool()
def list_available_metrics() -> str:
    """
    列出所有可用的人才指标及工具能力。

    返回指标的关键 key、中文名称、分类、单位、计算公式、可用维度和同义词。
    LLM 可通过此工具了解系统能查询哪些指标，再调用 get_talent_overview / get_talent_flow / get_talent_structure 获取数据。
    用户直接询问“人才盘点”“九宫格”或“九宫格人才”时，不要调用本工具列清单，
    应调用 get_talent_structure 并使用 dimension="talent_review_result"。
    """
    metrics = list_metrics()
    categories = sorted(set(m["category"] for m in metrics))
    data = {
        "total_metrics": len(metrics),
        "categories": categories,
        "metrics": metrics,
        "tools": {
            "get_talent_overview": "人才综合指标总览；不得用于仅询问人才盘点/九宫格/九宫格人才的场景",
            "get_talent_flow": "人才流动（离职/入职/晋升/编制）",
            "get_talent_structure": "按维度获取分布；人才盘点/九宫格/九宫格人才必须使用 talent_review_result",
            "compare_second_level_orgs": "自动识别组织层级并对比直属下级；支持各组织近4年趋势；公司一级组织=东鹏直属三级组织+总裁办",
            "get_metric_yearly_trend": "指标近4年年度趋势（每年年末/最后月份数据点）",
            "detect_talent_risk": "人才风险检测",
            "list_available_metrics": "本工具",
        },
    }
    return _format_result(data, f"共 {len(metrics)} 个人才指标，覆盖 {len(categories)} 个分类，7 个查询工具")


# ═══════════════════════════════════════════════════════════════
# 启动入口
# ═══════════════════════════════════════════════════════════════

def _run_http(transport: str):
    """以 HTTP 模式启动（SSE 或 Streamable HTTP），叠加 API Key 认证。"""
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8000"))
    path = os.getenv("MCP_PATH", "/mcp")
    api_keys = os.getenv("MCP_API_KEYS", "")

    if not api_keys:
        logger.error("HTTP 模式必须配置 MCP_API_KEYS 环境变量！")
        logger.error("示例: MCP_API_KEYS=key1,key2,key3")
        raise SystemExit(1)

    if not check_config():
        logger.error("数据库配置不完整，请检查 .env 文件")
        raise SystemExit(1)

    key_count = len([k for k in api_keys.split(",") if k.strip()])
    logger.info(f"启动 {transport} 模式: host={host}, port={port}, path={path}, API Keys={key_count} 个")

    # 创建 MCP Starlette 应用
    if transport == "streamable-http":
        app = mcp.streamable_http_app(
            streamable_http_path=path,
            json_response=True,
            stateless_http=True,
            host=host,
        )
        endpoint = f"http://{host}:{port}{path}"
    else:
        app = mcp.sse_app(host=host)
        endpoint = f"http://{host}:{port}/sse"

    # 叠加 API Key 认证中间件
    from auth import APIKeyMiddleware, health_endpoint
    app.add_middleware(APIKeyMiddleware)

    # 添加健康检查路由
    from starlette.routing import Route
    app.router.routes.insert(0, Route("/health", health_endpoint, methods=["GET"]))

    logger.info(f"MCP 端点: {endpoint}")
    logger.info(f"健康检查: http://{host}:{port}/health")

    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


def main():
    """启动 MCP Server，根据 MCP_TRANSPORT 环境变量选择传输模式。"""
    transport = os.getenv("MCP_TRANSPORT", "stdio")

    if transport in ("sse", "streamable-http"):
        _run_http(transport)
    else:
        if not check_config():
            logger.warning("数据库配置不完整，MCP Server 将启动但工具调用会报错")
            logger.warning("请配置 .env 文件后再使用")
        else:
            logger.info("数据库配置检查通过，启动 MCP Server (stdio)...")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
