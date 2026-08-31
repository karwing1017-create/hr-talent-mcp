"""
语义层 —— 人才盘点指标字典。

核心设计原则：
  1. 所有指标的计算口径（公式、SQL）在此文件统一定义，LLM 不碰 SQL 和计算
  2. 每个指标包含 SQL 模板、可用维度、同义词、敏感级别、颜色规则
  3. SQL 使用 %(param)s 参数化，防止注入
  4. 复杂指标在 SQL 层算好，返回结构化 JSON，LLM 只做语义理解和业务翻译

数据来源（指标口径及源数据梳理.xlsx）：
  DWRDIM.DWR_DIM_SAP_EMP_INFO_F    员工信息表（stat_date 快照，取月末一天）
  DWRDIM.DWR_DIM_SAP_DEPT_INFO_F   组织架构表（stat_date 快照，取月末一天）
  DWRDIM.DWR_DIM_SAP_ESTBL_STAT_F  组织编制表（stat_date 快照，取月末一天）
  DWRHR.DWR_HR_EMP_TRMNT_INFO_F    员工离职明细表（过程记录表，按 stat_date 的 YYYY-MM 取数）
  DWRHR.DWR_HR_EMP_ENTRY_INFO_F    员工入职明细表（过程记录表，按 entry_date 的 YYYY-MM 取数）
  DWRHR.DWR_HR_EMP_PROM_INFO_F     人事晋升表（事务表，按 start_date 的 YYYY-MM 取数）
  DM.DM_HR_SAP_MNG_STAT_T          管理干部管幅表（stat_month 月度汇总）
  DM.DM_HR_SAP_DEPT_STAT_T         组织汇总表（stat_month 月度汇总）
  DM.DM_HR_SAP_EMP_TRMNT_STAT_T    员工离职汇总表（stat_month 月度汇总）
  DM.DM_HR_SAP_EMP_ENTRY_STAT_T    员工入职汇总表（stat_month 月度汇总）
"""

from config import (
    TABLE_EMP, TABLE_DEPT, TABLE_ESTBL,
    TABLE_TRMNT, TABLE_ENTRY, TABLE_PROM,
    TABLE_MNG_STAT, TABLE_DEPT_STAT, TABLE_TRMNT_STAT, TABLE_ENTRY_STAT,
)


def _NUMERIC_CAST(column: str) -> str:
    """Return a PostgreSQL numeric cast for a trusted internal column name."""
    return f"CAST({column} AS NUMERIC)"


# ═══════════════════════════════════════════════════════════════
# 业务常量（来源：指标口径及源数据梳理.xlsx 指标清单）
# ═══════════════════════════════════════════════════════════════

# 正式员工排除的员工组（5项过滤之一）
EXCLUDED_EMP_GROUPS = (
    "实习生",
    "劳务外包人员",
    "劳务派遣员工",
    "退休人员",
    "离职人员",
    "外部人员",
    "临时工",
    "顾问",
    "残联员工",
)

# 排除的五级部门（5项过滤之二）
EXCLUDED_LVL5_DEPTS = ("陈村和园", "鹏云", "星信厂管理组")

# 排除的三级部门（5项过滤之三）
EXCLUDED_LVL3_DEPTS = ("资本运营部",)

# 排除的员工状态（5项过滤之四）
EXCLUDED_EMP_STATUS = ("内推", "待岗")

# 排除的岗位（5项过滤之五）
EXCLUDED_POST_NAMES = ("调动在途",)

# 管理干部岗位层级（总监及以上）——备用定义
# 注：当前管理干部口径仅按「职族=管理职族」，不再限定岗位层级
MGMT_LEVELS = ("董事长级", "总裁级", "中心总经理级", "总经理级", "总监级")

# 各岗位层级（用于岗位层级分布排序）
ALL_JOB_LEVELS = (
    "董事长级", "总裁级", "中心总经理级", "总经理级",
    "总监级", "经理级", "主管级", "员工级",
)

# MPO 分类规则
# M: 职族=管理职族
# P: 职族=专业职族
# O辅: 操作职族 + 岗位属性=间接产出
# OO: 操作职族(或空) + 岗位属性=直接产出(或空)
MP_POST_TYPES = ("管理职族", "专业职族","MP")

# 离职操作类型
TURNOVER_OPER_TYPES = ("离职", "（停用）终止协议用工")

# 前中后台分类关键词
FRONT_OFFICE_KEYWORDS = ("市场营销",)
MID_OFFICE_KEYWORDS = ("供应链", "生产制造", "品质", "研发")
BACK_OFFICE_KEYWORDS = (
    "财务", "行政支持", "人力资源", "数字赋能",
    "法务审计稽察", "战略运营",
)


# ═══════════════════════════════════════════════════════════════
# 公共 SQL 片段
# ═══════════════════════════════════════════════════════════════

# 5项正式员工过滤条件
SQL_FILTER_REGULAR = f"""
    emp_group_name NOT IN %(excluded_groups)s
    AND COALESCE(lvl5_dept_name, '无') NOT IN %(excluded_lvl5_depts)s
    AND COALESCE(lvl3_dept_name, '无') NOT IN %(excluded_lvl3_depts)s
    AND COALESCE(emp_status_name, '无') NOT IN %(excluded_emp_status)s
    AND COALESCE(emp_post_name, '无') NOT IN %(excluded_post_names)s
"""

# 快照日期筛选（DWRDIM 层维度表，stat_date 列，月末快照取最新一天）
SQL_SNAPSHOT_EMP = "stat_date = %(snapshot_date)s::date"

# 过程记录月份筛选（DWRHR 层离职/入职/晋升明细表是过程记录表：
# 每发生一次变动就落一条记录，stat_date/start_date/entry_date 是事件发生日期，
# 必须按 YYYY-MM 月份截取，不能像快照表那样指定月末某一天）
SQL_SNAPSHOT_MONTH = "TO_CHAR(stat_date, 'YYYY-MM') = %(stat_month)s"

# 部门筛选（员工/离职明细表，lvl1~5_dept_name 列）
# 注意：目标 PostgreSQL 启用 Oracle 兼容模式，空字符串等价于 NULL
SQL_DEPT_FILTER = """
    (
        (%(company_level_one_other)s AND NOT COALESCE(
            lvl1_dept_name = ANY(%(company_level_one_excluded_names)s::text[]) OR
            lvl2_dept_name = ANY(%(company_level_one_excluded_names)s::text[]) OR
            lvl3_dept_name = ANY(%(company_level_one_excluded_names)s::text[]) OR
            lvl4_dept_name = ANY(%(company_level_one_excluded_names)s::text[]) OR
            lvl5_dept_name = ANY(%(company_level_one_excluded_names)s::text[]),
            FALSE
        )) OR
        (NOT %(company_level_one_other)s AND %(dept_code)s IS NOT NULL AND (
            (%(dept_code_level)s = 1 AND lvl1_dept_code = %(dept_code)s) OR
            (%(dept_code_level)s = 2 AND lvl2_dept_code = %(dept_code)s) OR
            (%(dept_code_level)s = 3 AND lvl3_dept_code = %(dept_code)s) OR
            (%(dept_code_level)s = 4 AND lvl4_dept_code = %(dept_code)s) OR
            (%(dept_code_level)s = 5 AND lvl5_dept_code = %(dept_code)s) OR
            (%(dept_code_level)s = 6 AND lvl6_dept_code = %(dept_code)s) OR
            (%(dept_code_level)s = 7 AND lvl7_dept_code = %(dept_code)s) OR
            (%(dept_code_level)s = 8 AND lvl8_dept_code = %(dept_code)s) OR
            (%(dept_code_level)s = 9 AND lvl9_dept_code = %(dept_code)s)
        )) OR
        (NOT %(company_level_one_other)s AND %(dept_code)s IS NULL AND %(dept_path)s AND
            (%(dept_lvl1)s IS NULL OR lvl1_dept_name = %(dept_lvl1)s) AND
            (%(dept_lvl2)s IS NULL OR lvl2_dept_name = %(dept_lvl2)s) AND
            (%(dept_lvl3)s IS NULL OR lvl3_dept_name = %(dept_lvl3)s) AND
            (%(dept_lvl4)s IS NULL OR lvl4_dept_name = %(dept_lvl4)s) AND
            (%(dept_lvl5)s IS NULL OR lvl5_dept_name = %(dept_lvl5)s)
        ) OR
        (NOT %(company_level_one_other)s AND %(dept_code)s IS NULL AND NOT %(dept_path)s AND (
            (%(dept_name)s IS NULL AND %(dept_names)s IS NULL) OR
            lvl1_dept_name = %(dept_name)s OR
            lvl2_dept_name = %(dept_name)s OR
            lvl3_dept_name = %(dept_name)s OR
            lvl4_dept_name = %(dept_name)s OR
            lvl5_dept_name = %(dept_name)s OR
            lvl1_dept_name = ANY(COALESCE(%(dept_names)s::text[], ARRAY[]::text[])) OR
            lvl2_dept_name = ANY(COALESCE(%(dept_names)s::text[], ARRAY[]::text[])) OR
            lvl3_dept_name = ANY(COALESCE(%(dept_names)s::text[], ARRAY[]::text[])) OR
            lvl4_dept_name = ANY(COALESCE(%(dept_names)s::text[], ARRAY[]::text[])) OR
            lvl5_dept_name = ANY(COALESCE(%(dept_names)s::text[], ARRAY[]::text[]))
        ))
    )
"""

# 部门筛选（DM 层汇总表，lvl1~5_dept_name 列）
SQL_DEPT_FILTER_DM = """
    (
        (%(company_level_one_other)s AND NOT COALESCE(
            lvl1_dept_name = ANY(%(company_level_one_excluded_names)s::text[]) OR
            lvl2_dept_name = ANY(%(company_level_one_excluded_names)s::text[]) OR
            lvl3_dept_name = ANY(%(company_level_one_excluded_names)s::text[]) OR
            lvl4_dept_name = ANY(%(company_level_one_excluded_names)s::text[]) OR
            lvl5_dept_name = ANY(%(company_level_one_excluded_names)s::text[]),
            FALSE
        )) OR
        (NOT %(company_level_one_other)s AND %(dept_path)s AND
            (%(dept_lvl1)s IS NULL OR lvl1_dept_name = %(dept_lvl1)s) AND
            (%(dept_lvl2)s IS NULL OR lvl2_dept_name = %(dept_lvl2)s) AND
            (%(dept_lvl3)s IS NULL OR lvl3_dept_name = %(dept_lvl3)s) AND
            (%(dept_lvl4)s IS NULL OR lvl4_dept_name = %(dept_lvl4)s) AND
            (%(dept_lvl5)s IS NULL OR lvl5_dept_name = %(dept_lvl5)s)
        ) OR
        (NOT %(company_level_one_other)s AND NOT %(dept_path)s AND (
            (%(dept_name)s IS NULL AND %(dept_names)s IS NULL) OR
            lvl1_dept_name = %(dept_name)s OR
            lvl2_dept_name = %(dept_name)s OR
            lvl3_dept_name = %(dept_name)s OR
            lvl4_dept_name = %(dept_name)s OR
            lvl5_dept_name = %(dept_name)s OR
            lvl1_dept_name = ANY(COALESCE(%(dept_names)s::text[], ARRAY[]::text[])) OR
            lvl2_dept_name = ANY(COALESCE(%(dept_names)s::text[], ARRAY[]::text[])) OR
            lvl3_dept_name = ANY(COALESCE(%(dept_names)s::text[], ARRAY[]::text[])) OR
            lvl4_dept_name = ANY(COALESCE(%(dept_names)s::text[], ARRAY[]::text[])) OR
            lvl5_dept_name = ANY(COALESCE(%(dept_names)s::text[], ARRAY[]::text[]))
        ))
    )
"""

# 部门筛选（编制表，lvl1~5_dept_short_name 列）
SQL_DEPT_FILTER_ESTBL = """
    (
        (%(company_level_one_other)s AND NOT COALESCE(
            lvl1_dept_short_name = ANY(%(company_level_one_excluded_names)s::text[]) OR
            lvl2_dept_short_name = ANY(%(company_level_one_excluded_names)s::text[]) OR
            lvl3_dept_short_name = ANY(%(company_level_one_excluded_names)s::text[]) OR
            lvl4_dept_short_name = ANY(%(company_level_one_excluded_names)s::text[]) OR
            lvl5_dept_short_name = ANY(%(company_level_one_excluded_names)s::text[]),
            FALSE
        )) OR
        (NOT %(company_level_one_other)s AND %(dept_path)s AND
            (%(dept_lvl1)s IS NULL OR lvl1_dept_short_name = %(dept_lvl1)s) AND
            (%(dept_lvl2)s IS NULL OR lvl2_dept_short_name = %(dept_lvl2)s) AND
            (%(dept_lvl3)s IS NULL OR lvl3_dept_short_name = %(dept_lvl3)s) AND
            (%(dept_lvl4)s IS NULL OR lvl4_dept_short_name = %(dept_lvl4)s) AND
            (%(dept_lvl5)s IS NULL OR lvl5_dept_short_name = %(dept_lvl5)s)
        ) OR
        (NOT %(company_level_one_other)s AND NOT %(dept_path)s AND (
            (%(dept_name)s IS NULL AND %(dept_names)s IS NULL) OR
            lvl1_dept_short_name = %(dept_name)s OR
            lvl2_dept_short_name = %(dept_name)s OR
            lvl3_dept_short_name = %(dept_name)s OR
            lvl4_dept_short_name = %(dept_name)s OR
            lvl5_dept_short_name = %(dept_name)s OR
            lvl1_dept_short_name = ANY(COALESCE(%(dept_names)s::text[], ARRAY[]::text[])) OR
            lvl2_dept_short_name = ANY(COALESCE(%(dept_names)s::text[], ARRAY[]::text[])) OR
            lvl3_dept_short_name = ANY(COALESCE(%(dept_names)s::text[], ARRAY[]::text[])) OR
            lvl4_dept_short_name = ANY(COALESCE(%(dept_names)s::text[], ARRAY[]::text[])) OR
            lvl5_dept_short_name = ANY(COALESCE(%(dept_names)s::text[], ARRAY[]::text[]))
        ))
    )
"""

# 人员角色过滤（get_talent_overview / get_talent_structure 专属入参，留空=不过滤全部人员）
# 由 build_params 写入 role_post_type / role_is_key_post / role_talent_review /
# role_bachelor_above 四个参数实现：
#   M岗(含管理干部) => emp_post_type_name = '管理职族'
#   P岗            => emp_post_type_name = '专业职族'
#   关键岗位        => is_key_post = '是'
#   高潜人才        => talent_review_result IN ('7-绩效之星','8-潜力之星','9-超级明星')
#   （注意：talent_review_result 仅每年12月快照有值，非12月快照高潜人才过滤可能查不到人）
#   本科以上人员    => ed_name IN ('本科','硕士','博士')（沿用“本科及以上人数及占比”指标口径）
#   留空           => 四参数均为 NULL，(NULL IS NULL) 恒为真 → 不过滤
# 依赖员工信息表存在 emp_post_type_name / is_key_post / talent_review_result / ed_name 列
SQL_ROLE_FILTER = """
    (%(role_post_type)s IS NULL OR emp_post_type_name = %(role_post_type)s)
    AND (%(role_is_key_post)s IS NULL OR is_key_post = %(role_is_key_post)s)
    AND (%(role_talent_review)s IS NULL OR talent_review_result IN ('7-绩效之星', '8-潜力之星', '9-超级明星'))
    AND (%(role_bachelor_above)s IS NULL OR ed_name IN ('本科', '硕士', '博士'))
"""

# 完整的员工信息表 WHERE 子句（含人员角色过滤）
SQL_WHERE_EMP = f"""
    WHERE {SQL_SNAPSHOT_EMP}
      AND {SQL_FILTER_REGULAR}
      AND {SQL_DEPT_FILTER}
      AND {SQL_ROLE_FILTER}
"""

# 离职明细表 WHERE 子句（过程记录表，按 stat_date 的 YYYY-MM 取数）
SQL_WHERE_TRMNT = f"""
    WHERE {SQL_SNAPSHOT_MONTH}
      AND oper_type_name IN %(turnover_oper_types)s
      AND {SQL_FILTER_REGULAR}
      AND {SQL_DEPT_FILTER}
"""

# DM 层表 WHERE 子句
SQL_WHERE_DM = f"""
    WHERE stat_month = %(stat_month)s
      AND {SQL_DEPT_FILTER_DM}
"""

# 编制表 WHERE 子句
SQL_WHERE_ESTBL = f"""
    WHERE {SQL_SNAPSHOT_EMP}
      AND {SQL_DEPT_FILTER_ESTBL}
"""


# ═══════════════════════════════════════════════════════════════
# 指标字典
# ═══════════════════════════════════════════════════════════════

METRICS = {

    # ════════════ 人才数量 ════════════

    "headcount": {
        "name": "在职人数",
        "category": "人才数量",
        "formula": "COUNT(DISTINCT emp_num)，5项正式员工过滤",
        "unit": "人",
        "sql_template": f"""
            SELECT COUNT(DISTINCT emp_num) AS value
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
        """,
        "dimensions": [
            "department", "job_level", "job_series", "age_band", "tenure_band",
            "education", "gender", "front_mid_back", "mpo",
            "employment_status",
        ],
        "synonyms": ["人数", "总人数", "在职员工数", "员工总数", "headcount", "编制内人数"],
        "sensitivity": "low",
        "color_rule": "normal",
        "description": "统计期末正式在职员工总数，排除实习生/外包/派遣/退休/离职人员/残联员工等",
    },

    "intern_count": {
        "name": "实习生人数",
        "category": "人才数量",
        "formula": "COUNT(DISTINCT emp_num) WHERE emp_group_name = '实习生'",
        "unit": "人",
        "sql_template": f"""
            SELECT COUNT(DISTINCT emp_num) AS value
            FROM {TABLE_EMP}
            WHERE {SQL_SNAPSHOT_EMP}
              AND emp_group_name = '实习生'
              AND {SQL_DEPT_FILTER}
              AND {SQL_ROLE_FILTER}
        """,
        "dimensions": ["department"],
        "synonyms": ["实习人数", "实习生数量", "实习员工数"],
        "sensitivity": "low",
        "color_rule": "normal",
        "description": "统计期末实习生人数（emp_group_name = '实习生'）",
    },

    "avg_age": {
        "name": "平均年龄",
        "category": "人才数量",
        "formula": "AVG(age)，5项正式员工过滤",
        "unit": "岁",
        "sql_template": f"""
            SELECT ROUND(AVG({_NUMERIC_CAST('age')}), 1) AS value
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
        """,
        "dimensions": ["department"],
        "synonyms": ["平均年龄", "员工平均年龄", "年龄均值"],
        "sensitivity": "low",
        "color_rule": "normal",
        "description": "在职正式员工的平均年龄",
    },

    "age_median": {
        "name": "年龄中位数",
        "category": "人才数量",
        "formula": "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY age)，5项正式员工过滤",
        "unit": "岁",
        "sql_template": f"""
            SELECT ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {_NUMERIC_CAST('age')}), 1) AS value
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
        """,
        "dimensions": ["department"],
        "synonyms": ["年龄中位数", "年龄中位值", "年龄中位"],
        "sensitivity": "low",
        "color_rule": "normal",
        "description": "在职正式员工年龄的中位数（比平均年龄更能反映典型年龄分布，不受极端值影响）",
    },

    "avg_tenure": {
        "name": "平均司龄",
        "category": "人才数量",
        "formula": "AVG((stat_date - entry_date) / 365.25)，5项正式员工过滤",
        "unit": "年",
        "sql_template": f"""
            SELECT ROUND(AVG((stat_date::date - entry_date::date) / 365.25), 1) AS value
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
        """,
        "dimensions": ["department"],
        "synonyms": ["平均司龄", "司龄均值", "平均服务年限", "平均司年"],
        "sensitivity": "low",
        "color_rule": "reverse",
        "description": "在职正式员工的平均司龄（每行以快照日期减入职日期，并按 365.25 天折算为年）",
    },

    "tenure_median": {
        "name": "司龄中位数",
        "category": "人才数量",
        "formula": "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY (stat_date - entry_date) / 365.25)，5项正式员工过滤",
        "unit": "年",
        "sql_template": f"""
            SELECT ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
                ORDER BY (stat_date::date - entry_date::date) / 365.25
            ), 1) AS value
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
        """,
        "dimensions": ["department"],
        "synonyms": ["司龄中位数", "司龄中位值", "司龄中位"],
        "sensitivity": "low",
        "color_rule": "reverse",
        "description": "在职正式员工司龄的中位数（每行以快照日期减入职日期，并按 365.25 天折算为年），比平均司龄更能反映典型司龄",
    },

    "post_95s_count": {
        "name": "95后数量",
        "category": "人才数量",
        "formula": "COUNT(*) WHERE birth_date >= '1995-01-01'",
        "unit": "人",
        "sql_template": f"""
            SELECT COUNT(*) AS value
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
              AND birth_date >= '1995-01-01'
        """,
        "dimensions": ["department"],
        "synonyms": ["95后人数", "95后员工数", "95后数量"],
        "sensitivity": "low",
        "color_rule": "reverse",
        "description": "1995年及以后出生的正式在职员工人数",
    },

    "post_95s_ratio": {
        "name": "95后占比",
        "category": "人才数量",
        "formula": "95后人数 / 总人数 x 100%",
        "unit": "%",
        "sql_template": f"""
            SELECT
                ROUND(
                    COUNT(CASE WHEN birth_date >= '1995-01-01' THEN 1 END) * 100.0
                    / NULLIF(COUNT(*), 0), 1
                ) AS value
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
        """,
        "dimensions": ["department"],
        "synonyms": ["95后比例", "年轻员工占比", "新生代占比"],
        "sensitivity": "low",
        "color_rule": "reverse",
        "description": "1995年及以后出生的员工占总人数的比例",
    },

    # ════════════ 人才结构 ════════════

    "age_structure": {
        "name": "年龄结构",
        "category": "人才结构",
        "formula": "按年龄段分组统计人数及占比（支持按职级 emp_post_lvl_name 细分，返回各职级结构及整体结构）",
        "unit": "人",
        "sql_template": f"""
            SELECT
                COALESCE(emp_post_lvl_name, '未分级') AS job_level,
                age_sectn AS dimension_value,
                COUNT(*) AS count
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
            GROUP BY emp_post_lvl_name, age_sectn
            ORDER BY emp_post_lvl_name, age_sectn
        """,
        "dimensions": ["department", "job_level"],
        "synonyms": ["年龄分布", "年龄段分布", "年龄构成"],
        "sensitivity": "low",
        "color_rule": None,
        "description": "按年龄段分组统计员工人数及占比；支持按职级(emp_post_lvl_name)细分，返回各职级结构分布及整体结构",
    },

    "education_structure": {
        "name": "学历结构",
        "category": "人才结构",
        "formula": "按学历分组统计人数及占比（支持按职级 emp_post_lvl_name 细分，返回各职级结构及整体结构）",
        "unit": "人",
        "sql_template": f"""
            SELECT
                COALESCE(emp_post_lvl_name, '未分级') AS job_level,
                ed_name AS dimension_value,
                COUNT(*) AS count
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
            GROUP BY emp_post_lvl_name, ed_name
            ORDER BY emp_post_lvl_name, COUNT(*) DESC
        """,
        "dimensions": ["department", "job_level"],
        "synonyms": ["学历分布", "学历构成", "教育程度分布"],
        "sensitivity": "low",
        "color_rule": None,
        "description": "按学历分组统计员工人数及占比；支持按职级(emp_post_lvl_name)细分，返回各职级结构分布及整体结构",
    },

    "tenure_structure": {
        "name": "司龄结构",
        "category": "人才结构",
        "formula": "按司龄段分组统计人数及占比（支持按职级 emp_post_lvl_name 细分，返回各职级结构及整体结构）",
        "unit": "人",
        "sql_template": f"""
            SELECT
                COALESCE(emp_post_lvl_name, '未分级') AS job_level,
                div_sectn AS dimension_value,
                COUNT(*) AS count
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
            GROUP BY emp_post_lvl_name, div_sectn
            ORDER BY emp_post_lvl_name, div_sectn
        """,
        "dimensions": ["department", "job_level"],
        "synonyms": ["司龄分布", "司龄构成", "工龄分布", "服务年限分布"],
        "sensitivity": "low",
        "color_rule": None,
        "description": "按司龄段分组统计员工人数及占比；支持按职级(emp_post_lvl_name)细分，返回各职级结构分布及整体结构",
    },

    "gender_structure": {
        "name": "性别结构",
        "category": "人才结构",
        "formula": "按性别分组统计人数及占比",
        "unit": "人",
        "sql_template": f"""
            SELECT
                gender AS dimension_value,
                COUNT(*) AS count,
                ROUND(COUNT(*) * 100.0 / NULLIF(SUM(COUNT(*)) OVER (), 0), 1) AS percentage
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
            GROUP BY gender
            ORDER BY COUNT(*) DESC
        """,
        "dimensions": ["department", "job_level"],
        "synonyms": ["性别分布", "男女比例", "性别构成"],
        "sensitivity": "low",
        "color_rule": None,
        "description": "按性别分组统计员工人数及占比",
    },

    "job_level_distribution": {
        "name": "岗位层级分布",
        "category": "人才结构",
        "formula": "按岗位层级分组统计人数及占比",
        "unit": "人",
        "sql_template": f"""
            SELECT
                emp_lvl_name AS dimension_value,
                COUNT(*) AS count,
                ROUND(COUNT(*) * 100.0 / NULLIF(SUM(COUNT(*)) OVER (), 0), 1) AS percentage
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
            GROUP BY emp_lvl_name
            ORDER BY
                CASE emp_lvl_name
                    {"".join(f"WHEN '{lv}' THEN {i+1}" for i, lv in enumerate(ALL_JOB_LEVELS))}
                    ELSE 99
                END
        """,
        "dimensions": ["department"],
        "synonyms": ["职级分布", "层级分布", "岗位层次分布", "管理层级分布"],
        "sensitivity": "low",
        "color_rule": None,
        "description": "按岗位层级（董事长级/总裁级/...员工级）分组统计人数及占比",
    },

    "job_series_distribution": {
        "name": "职系分布",
        "category": "人才结构",
        "formula": "按员工子组（职系）分组统计人数及占比",
        "unit": "人",
        "sql_template": f"""
            SELECT
                emp_sub_group_name AS dimension_value,
                COUNT(*) AS count,
                ROUND(COUNT(*) * 100.0 / NULLIF(SUM(COUNT(*)) OVER (), 0), 1) AS percentage
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
            GROUP BY emp_sub_group_name
            ORDER BY COUNT(*) DESC
        """,
        "dimensions": ["department"],
        "synonyms": ["职系分布", "员工子组分布", "职系构成"],
        "sensitivity": "low",
        "color_rule": None,
        "description": "按员工子组（职系）分组统计人数及占比",
    },

    "fee_type_distribution": {
        "name": "直接间接人员分布",
        "category": "人才结构",
        "formula": "费用分类=制造费用的为直接人员；费用分类=期间费用的为间接人员；统计两类人员人数及占比",
        "unit": "人",
        "sql_template": f"""
            SELECT
                CASE fee_type_name
                    WHEN '制造费用' THEN '直接人员'
                    WHEN '期间费用' THEN '间接人员'
                END AS dimension_value,
                COUNT(*) AS count,
                ROUND(COUNT(*) * 100.0 / NULLIF(SUM(COUNT(*)) OVER (), 0), 1) AS percentage
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
              AND fee_type_name IN ('制造费用', '期间费用')
            GROUP BY fee_type_name
            ORDER BY
                CASE fee_type_name
                    WHEN '制造费用' THEN 1
                    WHEN '期间费用' THEN 2
                END
        """,
        "dimensions": ["department"],
        "synonyms": [
            "费用类型分布", "费用分类分布", "直接间接人员分布",
            "直接人员占比", "间接人员占比", "直接间接人数及占比",
            "费用类型人数及占比", "成本类型人数及占比",
            "制造成本人数", "期间成本人数", "制造费用人数", "期间费用人数",
            "制造成本人数及占比", "期间成本人数及占比", "制造费用人数及占比", "期间费用人数及占比",
            "制造成本人员", "期间成本人员", "制造费用人员", "期间费用人员",
            "制造成本占比", "期间成本占比", "制造费用占比", "期间费用占比",
        ],
        "sensitivity": "low",
        "color_rule": None,
        "description": "按费用分类统计直接、间接人员人数及占比：制造费用为直接人员，期间费用为间接人员；其他或空费用分类不纳入统计。提到制造成本人数、期间成本人数等同类词语时读取本指标，并返回完整两类分布",
    },

    "talent_review_result_distribution": {
        "name": "人才盘点结果分布",
        "category": "人才结构",
        "formula": "按人才盘点结果(talent_review_result)分组统计人数及占比；该字段仅每年12月快照有值，其余月份记为'未盘点'",
        "unit": "人",
        "sql_template": f"""
            SELECT
                COALESCE(talent_review_result, '未盘点') AS dimension_value,
                COUNT(*) AS count,
                ROUND(COUNT(*) * 100.0 / NULLIF(SUM(COUNT(*)) OVER (), 0), 1) AS percentage
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
            GROUP BY talent_review_result
            ORDER BY COUNT(*) DESC
        """,
        "dimensions": ["department"],
        "synonyms": [
            "人才盘点", "人才盘点结果", "人才盘点结果分布", "盘点结果分布", "人才盘点结果构成",
            "九宫格", "九宫格人才", "九宫格分布", "九宫格人才分布", "talent review distribution",
        ],
        "sensitivity": "low",
        "color_rule": None,
        "description": "用户提到‘人才盘点’‘九宫格’‘九宫格人才’时必须读取本指标；按人才盘点结果分组统计人数及占比（talent_review_result 仅每年12月快照有值，其余月份该字段为空，统一记为'未盘点'）",
    },

    "mpo_distribution": {
        "name": "MPO分布",
        "category": "人才结构",
        "formula": "M=管理/专业职族；O辅=操作职族+间接产出；OO=操作职族(或空)+直接产出(或空)",
        "unit": "人",
        "sql_template": f"""
            SELECT
                CASE
                    WHEN emp_post_type_name IN %(mp_post_types)s THEN 'MP'
                    WHEN emp_post_type_name = '操作职族' AND emp_post_attr_name = '间接产出' THEN 'O辅'
                    WHEN (emp_post_type_name = '操作职族' OR emp_post_type_name IS NULL)
                         AND (emp_post_attr_name = '直接产出' OR emp_post_attr_name IS NULL) THEN 'OO'
                    ELSE '其他'
                END AS dimension_value,
                COUNT(*) AS count,
                ROUND(COUNT(*) * 100.0 / NULLIF(SUM(COUNT(*)) OVER (), 0), 1) AS percentage
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
            GROUP BY 1
            ORDER BY count DESC
        """,
        "dimensions": ["department"],
        "synonyms": ["MPO占比", "MPO分类", "管理专业操作占比", "人员分类"],
        "sensitivity": "low",
        "color_rule": None,
        "description": "按MPO分类（管理专业类/操作间接类/操作直接类）统计人数及占比",
    },

    "front_mid_back_distribution": {
        "name": "前中后台分布",
        "category": "人才结构",
        "formula": "前台=员工子组含市场营销；中台=含供应链/生产制造/品质/研发；后台=含财务/行政支持/人力资源/数字赋能/法务审计稽察/战略运营",
        "unit": "人",
        "sql_template": f"""
            SELECT
                CASE
                    WHEN emp_sub_group_name LIKE %(kw_market)s THEN '前台'
                    WHEN emp_sub_group_name LIKE %(kw_supply)s
                      OR emp_sub_group_name LIKE %(kw_manufacturing)s
                      OR emp_sub_group_name LIKE %(kw_quality)s
                      OR emp_sub_group_name LIKE %(kw_rd)s THEN '中台'
                    WHEN emp_sub_group_name LIKE %(kw_finance)s
                      OR emp_sub_group_name LIKE %(kw_admin)s
                      OR emp_sub_group_name LIKE %(kw_hr)s
                      OR emp_sub_group_name LIKE %(kw_digital)s
                      OR emp_sub_group_name LIKE %(kw_legal)s
                      OR emp_sub_group_name LIKE %(kw_strategy)s THEN '后台'
                    ELSE '其他'
                END AS dimension_value,
                COUNT(*) AS count,
                ROUND(COUNT(*) * 100.0 / NULLIF(SUM(COUNT(*)) OVER (), 0), 1) AS percentage
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
            GROUP BY 1
            ORDER BY count DESC
        """,
        "dimensions": ["department"],
        "synonyms": ["前中后台占比", "前中后台分类", "前后台分布"],
        "sensitivity": "low",
        "color_rule": None,
        "description": "按前中后台分类统计人数及占比（基于员工子组关键词匹配）",
    },

    "employment_status_distribution": {
        "name": "在岗状态分布",
        "category": "人才结构",
        "formula": "试用期=员工状态=试用期；已转正=员工状态=正式/试岗/返聘",
        "unit": "人",
        "sql_template": f"""
            SELECT
                CASE
                    WHEN emp_status_name = '试用期' THEN '试用期'
                    WHEN emp_status_name IN ('正式', '试岗', '返聘') THEN '已转正'
                    ELSE '其他'
                END AS dimension_value,
                COUNT(*) AS count,
                ROUND(COUNT(*) * 100.0 / NULLIF(SUM(COUNT(*)) OVER (), 0), 1) AS percentage
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
            GROUP BY 1
            ORDER BY count DESC
        """,
        "dimensions": ["department"],
        "synonyms": ["在岗状态分布", "用工状态分布", "转正状态分布"],
        "sensitivity": "low",
        "color_rule": None,
        "description": "按在岗状态（试用期/已转正）统计人数及占比",
    },

    "small_org_count": {
        "name": "5人以下组织数量",
        "category": "人才结构",
        "formula": "按二级部门分组，人数<5的部门个数",
        "unit": "个",
        "sql_template": f"""
            SELECT COUNT(*) AS value
            FROM (
                SELECT COALESCE(lvl2_dept_name, '未分配') AS dept,
                       COUNT(DISTINCT emp_num) AS emp_cnt
                FROM {TABLE_EMP}
                {SQL_WHERE_EMP}
                GROUP BY COALESCE(lvl2_dept_name, '未分配')
                HAVING COUNT(DISTINCT emp_num) < 5
            ) t
        """,
        "dimensions": ["department"],
        "synonyms": ["小组织数量", "5人以下部门数", "微型组织数"],
        "sensitivity": "low",
        "color_rule": "normal",
        "description": "二级部门中在职人数少于5人的组织个数（简化口径：仅统计二级部门粒度）",
    },

    # ════════════ 关键人才 ════════════

    "key_position_ratio": {
        "name": "关键岗位占比",
        "category": "关键人才",
        "formula": "关键岗位人数 / 总人数 x 100%",
        "unit": "%",
        "sql_template": f"""
            SELECT
                ROUND(
                    COUNT(CASE WHEN is_key_post = '是' THEN 1 END) * 100.0
                    / NULLIF(COUNT(*), 0), 1
                ) AS value
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
        """,
        "dimensions": ["department", "job_level"],
        "synonyms": ["关键人才占比", "关键岗位覆盖率", "核心人才比例"],
        "sensitivity": "medium",
        "color_rule": "reverse",
        "description": "关键岗位人数占总人数的比例",
    },

    "bachelor_above_ratio": {
        "name": "本科及以上人数及占比",
        "category": "关键人才",
        "formula": "学历为本科/硕士/博士的人数（cnt）及其占正式员工总人数比例（value）= cnt / 总人数 x 100%",
        "unit": "%",
        "sql_template": f"""
            SELECT
                COUNT(CASE WHEN ed_name IN ('本科', '硕士', '博士') THEN 1 END) AS cnt,
                ROUND(
                    COUNT(CASE WHEN ed_name IN ('本科', '硕士', '博士') THEN 1 END) * 100.0
                    / NULLIF(COUNT(*), 0), 1
                ) AS value
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
        """,
        "dimensions": ["department"],
        "synonyms": ["高学历人数及占比", "本科及以上人数", "高学历占比", "本科率", "学历水平", "本科及以上比例"],
        "sensitivity": "low",
        "color_rule": "reverse",
        "description": "学历为本科、硕士、博士的正式员工人数（cnt，单位：人）及其占总人数的比例（value，单位：%）",
    },

    # ════════════ 管理干部 ════════════

    "management_cadre_ratio": {
        "name": "管理干部占比",
        "category": "管理干部",
        "formula": "管理干部人数 / 总人数 x 100%",
        "unit": "%",
        "sql_template": f"""
            SELECT
                ROUND(
                    COUNT(CASE WHEN emp_post_type_name = '管理职族' THEN 1 END) * 100.0
                    / NULLIF(COUNT(*), 0), 1
                ) AS value
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
        """,
        "dimensions": ["department"],
        "synonyms": ["管理层比例", "干部占比", "管理人员占比"],
        "sensitivity": "low",
        "color_rule": "normal",
        "description": "管理干部（职族=管理职族）人数占总人数的比例",
    },

    "management_span": {
        "name": "管幅平均值",
        "category": "管理干部",
        "formula": "管理干部直接下属人数的平均值（自连接计算）",
        "unit": "人",
        "sql_template": f"""
            WITH managers AS (
                SELECT emp_num
                FROM {TABLE_EMP}
                {SQL_WHERE_EMP}
                  AND emp_post_type_name = '管理职族'
            ),
            span AS (
                SELECT
                    m.emp_num,
                    COUNT(e.emp_num) AS direct_reports
                FROM managers m
                LEFT JOIN {TABLE_EMP} e
                    ON e.super_emp_num = m.emp_num
                   AND e.stat_date = %(snapshot_date)s::date
                   AND e.emp_group_name NOT IN %(excluded_groups)s
                GROUP BY m.emp_num
                HAVING COUNT(e.emp_num) > 0
            )
            SELECT ROUND(AVG(direct_reports), 1) AS value
            FROM span
        """,
        "dimensions": ["department"],
        "synonyms": ["管理幅度", "平均管幅", "管理跨度", "管理宽幅"],
        "sensitivity": "low",
        "color_rule": "reverse",
        "description": "有直接下属的管理干部，其直接下属人数的平均值（不含管幅为0的干部）",
    },

    "span_under_5_ratio": {
        "name": "管幅5人以下占比",
        "category": "管理干部",
        "formula": "管幅<5的管理干部数 / 管理干部总数 x 100%（DM汇总表）",
        "unit": "%",
        "sql_template": f"""
            SELECT
                ROUND(
                    COUNT(CASE WHEN emp_cnt < 5 THEN 1 END) * 100.0
                    / NULLIF(COUNT(*), 0), 1
                ) AS value
            FROM {TABLE_MNG_STAT}
            {SQL_WHERE_DM}
        """,
        "dimensions": ["department"],
        "synonyms": ["管幅不足5人占比", "窄管幅占比"],
        "sensitivity": "low",
        "color_rule": "normal",
        "description": "下属员工数少于5人的管理干部占管理干部总数的比例",
    },

    "xrb_count": {
        "name": "新锐班人数",
        "category": "管理干部",
        "formula": "COUNT(*) WHERE is_xrb = '是'",
        "unit": "人",
        "sql_template": f"""
            SELECT COUNT(DISTINCT emp_num) AS value
            FROM {TABLE_EMP}
            {SQL_WHERE_EMP}
              AND is_xrb = '是'
        """,
        "dimensions": ["department", "job_level"],
        "synonyms": ["新锐班成员数", "新锐班人才", "XRB人数"],
        "sensitivity": "low",
        "color_rule": "reverse",
        "description": "新锐班项目在职成员人数",
    },

    # ════════════ 组织架构 ════════════

    "org_count": {
        "name": "组织机构数量",
        "category": "组织架构",
        "formula": "SUM(dept_cnt)（DM汇总表）",
        "unit": "个",
        "sql_template": f"""
            SELECT COALESCE(SUM(dept_cnt), 0) AS value
            FROM {TABLE_DEPT_STAT}
            {SQL_WHERE_DM}
        """,
        "dimensions": ["department"],
        "synonyms": ["部门数量", "组织数量", "机构总数"],
        "sensitivity": "low",
        "color_rule": "normal",
        "description": "组织机构总数（来自DM层组织汇总表）",
    },

    "org_levels": {
        "name": "组织层级数",
        "category": "组织架构",
        "formula": "MAX(level_cnt)（DM汇总表）",
        "unit": "层",
        "sql_template": f"""
            SELECT COALESCE(MAX(level_cnt), 0) AS value
            FROM {TABLE_DEPT_STAT}
            {SQL_WHERE_DM}
        """,
        "dimensions": ["department"],
        "synonyms": ["架构层级", "管理层级数", "组织深度"],
        "sensitivity": "low",
        "color_rule": "normal",
        "description": "组织架构的最大层级深度（来自DM层组织汇总表）",
    },

    "level_reduction_yoy": {
        "name": "较年初减少层级数",
        "category": "组织架构",
        "formula": "去年12月层级数 - 当前层级数（DM汇总表）",
        "unit": "层",
        "sql_template": f"""
            SELECT
                COALESCE(MAX(last_year_12m_level_cnt), 0)
                - COALESCE(MAX(level_cnt), 0) AS value
            FROM {TABLE_DEPT_STAT}
            {SQL_WHERE_DM}
        """,
        "dimensions": ["department"],
        "synonyms": ["层级减少数", "年初对比减少层级", "扁平化进展"],
        "sensitivity": "low",
        "color_rule": "reverse",
        "description": "与年初（去年12月）相比减少的组织层级数，正值=减少=改善",
    },

    "headcount_quota": {
        "name": "编制数",
        "category": "组织架构",
        "formula": "SUM(cur_estbl_num)（编制表）",
        "unit": "人",
        "sql_template": f"""
            SELECT COALESCE(SUM(cur_estbl_num), 0) AS value
            FROM {TABLE_ESTBL}
            {SQL_WHERE_ESTBL}
        """,
        "dimensions": ["department"],
        "synonyms": ["编制总数", "核定编制", "岗位编制数"],
        "sensitivity": "low",
        "color_rule": "normal",
        "description": "现有编制总数（来自组织编制表）",
    },

    "fill_rate": {
        "name": "满编率",
        "category": "组织架构",
        "formula": "SUM(actual_num) / SUM(cur_estbl_num) x 100%（编制表）",
        "unit": "%",
        "sql_template": f"""
            SELECT
                ROUND(
                    COALESCE(SUM(actual_num), 0) * 100.0
                    / NULLIF(SUM(cur_estbl_num), 0), 1
                ) AS value
            FROM {TABLE_ESTBL}
            {SQL_WHERE_ESTBL}
        """,
        "dimensions": ["department"],
        "synonyms": ["编制使用率", "编制达成率", "满编比例"],
        "sensitivity": "low",
        "color_rule": "reverse",
        "description": "实有人数占现有编制数的比例",
    },

    # ════════════ 人才流动 ════════════

    "turnover_count": {
        "name": "离职人数",
        "category": "人才流动",
        "formula": "COUNT(DISTINCT emp_num) WHERE oper_type IN (离职, 终止协议用工)（离职明细表）",
        "unit": "人",
        "sql_template": f"""
            SELECT COUNT(DISTINCT emp_num) AS value
            FROM {TABLE_TRMNT}
            {SQL_WHERE_TRMNT}
        """,
        "dimensions": ["department", "tenure_band", "age_band"],
        "synonyms": ["离职人数", "离职总人数", "流失人数"],
        "sensitivity": "medium",
        "color_rule": "normal",
        "description": "统计期内离职员工人数（操作类型=离职/终止协议用工，5项正式员工过滤）",
    },

    "turnover_by_tenure_distribution": {
        "name": "离职司龄分布",
        "category": "人才流动",
        "formula": "按司龄段分组统计离职人数及占比（离职明细表）",
        "unit": "人",
        "sql_template": f"""
            SELECT
                div_sectn AS dimension_value,
                COUNT(DISTINCT emp_num) AS count,
                ROUND(COUNT(DISTINCT emp_num) * 100.0
                    / NULLIF(SUM(COUNT(DISTINCT emp_num)) OVER (), 0), 1) AS percentage
            FROM {TABLE_TRMNT}
            {SQL_WHERE_TRMNT}
            GROUP BY div_sectn
            ORDER BY div_sectn
        """,
        "dimensions": ["department"],
        "synonyms": ["离职司龄构成", "离职员工司龄分布", "离职工龄分布"],
        "sensitivity": "medium",
        "color_rule": None,
        "description": "按司龄段分组统计离职人数及占比，反映哪个司龄段离职最多",
    },

    "turnover_by_age_distribution": {
        "name": "离职年龄分布",
        "category": "人才流动",
        "formula": "按年龄段分组统计离职人数及占比（离职明细表）",
        "unit": "人",
        "sql_template": f"""
            SELECT
                age_sectn AS dimension_value,
                COUNT(DISTINCT emp_num) AS count,
                ROUND(COUNT(DISTINCT emp_num) * 100.0
                    / NULLIF(SUM(COUNT(DISTINCT emp_num)) OVER (), 0), 1) AS percentage
            FROM {TABLE_TRMNT}
            {SQL_WHERE_TRMNT}
            GROUP BY age_sectn
            ORDER BY age_sectn
        """,
        "dimensions": ["department"],
        "synonyms": ["离职年龄构成", "离职员工年龄分布"],
        "sensitivity": "medium",
        "color_rule": None,
        "description": "按年龄段分组统计离职人数及占比，反映哪个年龄段离职最多",
    },

    "turnover_rate": {
        "name": "离职率",
        "category": "人才流动",
        "formula": "本月离职人数 / 月初人数 x 100%（离职汇总表）",
        "unit": "%",
        "sql_template": f"""
            SELECT
                ROUND(
                    COALESCE(SUM(trmnt_emp_cnt), 0) * 100.0
                    / NULLIF(SUM(emp_cnt), 0), 1
                ) AS value
            FROM {TABLE_TRMNT_STAT}
            {SQL_WHERE_DM}
        """,
        "dimensions": ["department"],
        "synonyms": ["流失率", "离职比例", "员工流失率"],
        "sensitivity": "medium",
        "color_rule": "normal",
        "description": "本月离职人数占月初人数的比例（来自DM层离职汇总表）",
    },

    "voluntary_turnover_rate": {
        "name": "主动离职率",
        "category": "人才流动",
        "formula": "本月主动离职人数 / 月初人数 x 100%（离职汇总表）",
        "unit": "%",
        "sql_template": f"""
            SELECT
                ROUND(
                    COALESCE(SUM(volry_trmnt_emp_cnt), 0) * 100.0
                    / NULLIF(SUM(emp_cnt), 0), 1
                ) AS value
            FROM {TABLE_TRMNT_STAT}
            {SQL_WHERE_DM}
        """,
        "dimensions": ["department"],
        "synonyms": ["主动流失率", "自愿离职率"],
        "sensitivity": "medium",
        "color_rule": "normal",
        "description": "本月主动离职人数占月初人数的比例",
    },

    "involuntary_turnover_rate": {
        "name": "被动离职率",
        "category": "人才流动",
        "formula": "本月被动离职人数 / 月初人数 x 100%（离职汇总表）",
        "unit": "%",
        "sql_template": f"""
            SELECT
                ROUND(
                    COALESCE(SUM(unvolry_trmnt_emp_cnt), 0) * 100.0
                    / NULLIF(SUM(emp_cnt), 0), 1
                ) AS value
            FROM {TABLE_TRMNT_STAT}
            {SQL_WHERE_DM}
        """,
        "dimensions": ["department"],
        "synonyms": ["被动流失率", "非自愿离职率", "辞退率"],
        "sensitivity": "medium",
        "color_rule": "normal",
        "description": "本月被动离职人数占月初人数的比例",
    },

    "mp_voluntary_turnover_rate": {
        "name": "MP主动离职率",
        "category": "人才流动",
        "formula": "本月MP主动离职人数 / MP月初人数 x 100%（离职汇总表，职族=管理/专业职族）",
        "unit": "%",
        "sql_template": f"""
            SELECT
                ROUND(
                    COALESCE(SUM(CASE WHEN emp_post_type_name IN %(mp_post_types)s
                        THEN volry_trmnt_emp_cnt ELSE 0 END), 0) * 100.0
                    / NULLIF(SUM(CASE WHEN emp_post_type_name IN %(mp_post_types)s
                        THEN emp_cnt ELSE 0 END), 0), 1
                ) AS value
            FROM {TABLE_TRMNT_STAT}
            {SQL_WHERE_DM}
        """,
        "dimensions": ["department"],
        "synonyms": ["MP主动流失率", "管理专业主动离职率"],
        "sensitivity": "medium",
        "color_rule": "normal",
        "description": "管理职族和专业职族（MP）的主动离职人数占MP月初人数的比例",
    },

    "mp_involuntary_turnover_rate": {
        "name": "MP被动离职率",
        "category": "人才流动",
        "formula": "本月MP被动离职人数 / MP月初人数 x 100%（离职汇总表，职族=管理/专业职族）",
        "unit": "%",
        "sql_template": f"""
            SELECT
                ROUND(
                    COALESCE(SUM(CASE WHEN emp_post_type_name IN %(mp_post_types)s
                        THEN unvolry_trmnt_emp_cnt ELSE 0 END), 0) * 100.0
                    / NULLIF(SUM(CASE WHEN emp_post_type_name IN %(mp_post_types)s
                        THEN emp_cnt ELSE 0 END), 0), 1
                ) AS value
            FROM {TABLE_TRMNT_STAT}
            {SQL_WHERE_DM}
        """,
        "dimensions": ["department"],
        "synonyms": ["MP被动流失率", "管理专业被动离职率"],
        "sensitivity": "medium",
        "color_rule": "normal",
        "description": "管理职族和专业职族（MP）的被动离职人数占MP月初人数的比例",
    },

    "mgmt_voluntary_turnover_rate": {
        "name": "管理干部主动离职率",
        "category": "人才流动",
        "formula": "本月管理干部主动离职人数 / 管理干部月初人数 x 100%（离职汇总表，is_mgmt_cadres=是）",
        "unit": "%",
        "sql_template": f"""
            SELECT
                ROUND(
                    COALESCE(SUM(CASE WHEN COALESCE(is_mgmt_cadres, '否') = '是'
                        THEN volry_trmnt_emp_cnt ELSE 0 END), 0) * 100.0
                    / NULLIF(SUM(CASE WHEN COALESCE(is_mgmt_cadres, '否') = '是'
                        THEN emp_cnt ELSE 0 END), 0), 1
                ) AS value
            FROM {TABLE_TRMNT_STAT}
            {SQL_WHERE_DM}
        """,
        "dimensions": ["department"],
        "synonyms": ["干部主动离职率", "管理层主动流失率"],
        "sensitivity": "high",
        "color_rule": "normal",
        "description": "管理干部的主动离职人数占管理干部月初人数的比例",
    },

    "mgmt_involuntary_turnover_rate": {
        "name": "管理干部被动离职率",
        "category": "人才流动",
        "formula": "本月管理干部被动离职人数 / 管理干部月初人数 x 100%（离职汇总表，is_mgmt_cadres=是）",
        "unit": "%",
        "sql_template": f"""
            SELECT
                ROUND(
                    COALESCE(SUM(CASE WHEN COALESCE(is_mgmt_cadres, '否') = '是'
                        THEN unvolry_trmnt_emp_cnt ELSE 0 END), 0) * 100.0
                    / NULLIF(SUM(CASE WHEN COALESCE(is_mgmt_cadres, '否') = '是'
                        THEN emp_cnt ELSE 0 END), 0), 1
                ) AS value
            FROM {TABLE_TRMNT_STAT}
            {SQL_WHERE_DM}
        """,
        "dimensions": ["department"],
        "synonyms": ["干部被动离职率", "管理层被动流失率"],
        "sensitivity": "high",
        "color_rule": "normal",
        "description": "管理干部的被动离职人数占管理干部月初人数的比例",
    },

    "new_hire_count": {
        "name": "新员工人数",
        "category": "人才流动",
        "formula": "SUM(entry_emp_cnt)（入职汇总表）",
        "unit": "人",
        "sql_template": f"""
            SELECT COALESCE(SUM(entry_emp_cnt), 0) AS value
            FROM {TABLE_ENTRY_STAT}
            {SQL_WHERE_DM}
        """,
        "dimensions": ["department"],
        "synonyms": ["入职人数", "新入职人数", "本月新员工"],
        "sensitivity": "low",
        "color_rule": "reverse",
        "description": "本月新入职员工人数（来自DM层入职汇总表）",
    },

    "new_hire_ratio": {
        "name": "新员工占比",
        "category": "人才流动",
        "formula": "本月入职人数 / 在职人数 x 100%",
        "unit": "%",
        "sql_template": f"""
            WITH total AS (
                SELECT COUNT(DISTINCT emp_num) AS cnt
                FROM {TABLE_EMP}
                {SQL_WHERE_EMP}
            ),
            hires AS (
                SELECT COALESCE(SUM(entry_emp_cnt), 0) AS cnt
                FROM {TABLE_ENTRY_STAT}
                {SQL_WHERE_DM}
            )
            SELECT ROUND(hires.cnt * 100.0 / NULLIF(total.cnt, 0), 1) AS value
            FROM total, hires
        """,
        "dimensions": ["department"],
        "synonyms": ["新员工比例", "新入职占比", "入职率"],
        "sensitivity": "low",
        "color_rule": "reverse",
        "description": "本月新入职人数占在职人数的比例",
    },

    "new_hire_6m_retention": {
        "name": "新员工6个月留存率",
        "category": "人才流动",
        "formula": "6个月前入职且仍在职的人数 / 6个月前入职总人数 x 100%",
        "unit": "%",
        "sql_template": f"""
            WITH hires_6m AS (
                SELECT DISTINCT emp_num
                FROM {TABLE_ENTRY}
                WHERE TO_CHAR(entry_date, 'YYYY-MM') =
                      TO_CHAR(TO_DATE(%(stat_month)s || '-01', 'YYYY-MM-DD') - INTERVAL '6 months', 'YYYY-MM')
                  AND emp_group_name NOT IN %(excluded_groups)s
            ),
            still_active AS (
                SELECT DISTINCT emp_num
                FROM {TABLE_EMP}
                {SQL_WHERE_EMP}
            )
            SELECT
                ROUND(
                    COUNT(DISTINCT CASE WHEN s.emp_num IS NOT NULL THEN h.emp_num END) * 100.0
                    / NULLIF(COUNT(DISTINCT h.emp_num), 0), 1
                ) AS value
            FROM hires_6m h
            LEFT JOIN still_active s ON h.emp_num = s.emp_num
        """,
        "dimensions": ["department"],
        "synonyms": ["6个月留存率", "半年留存率", "新员工半年留存"],
        "sensitivity": "medium",
        "color_rule": "reverse",
        "description": "6个月前入职的员工中仍在职的占比（部门筛选仅作用于仍在职侧）",
    },

    "new_hire_1y_retention": {
        "name": "新员工1年留存率",
        "category": "人才流动",
        "formula": "1年前入职且仍在职的人数 / 1年前入职总人数 x 100%",
        "unit": "%",
        "sql_template": f"""
            WITH hires_1y AS (
                SELECT DISTINCT emp_num
                FROM {TABLE_ENTRY}
                WHERE TO_CHAR(entry_date, 'YYYY-MM') =
                      TO_CHAR(TO_DATE(%(stat_month)s || '-01', 'YYYY-MM-DD') - INTERVAL '1 year', 'YYYY-MM')
                  AND emp_group_name NOT IN %(excluded_groups)s
            ),
            still_active AS (
                SELECT DISTINCT emp_num
                FROM {TABLE_EMP}
                {SQL_WHERE_EMP}
            )
            SELECT
                ROUND(
                    COUNT(DISTINCT CASE WHEN s.emp_num IS NOT NULL THEN h.emp_num END) * 100.0
                    / NULLIF(COUNT(DISTINCT h.emp_num), 0), 1
                ) AS value
            FROM hires_1y h
            LEFT JOIN still_active s ON h.emp_num = s.emp_num
        """,
        "dimensions": ["department"],
        "synonyms": ["1年留存率", "一年留存率", "新员工年度留存"],
        "sensitivity": "medium",
        "color_rule": "reverse",
        "description": "1年前入职的员工中仍在职的占比（部门筛选仅作用于仍在职侧）",
    },

    # ════════════ 人才发展 ════════════

    "promotion_rate": {
        "name": "晋升率",
        "category": "人才发展",
        "formula": "本月晋升人数 / 在职总人数 x 100%",
        "unit": "%",
        "sql_template": f"""
            WITH total AS (
                SELECT COUNT(DISTINCT emp_num) AS cnt
                FROM {TABLE_EMP}
                {SQL_WHERE_EMP}
            ),
            promotions AS (
                SELECT COUNT(DISTINCT emp_num) AS cnt
                FROM {TABLE_PROM}
                WHERE op_rsn_name LIKE %(kw_promotion)s
                  AND TO_CHAR(start_date, 'YYYY-MM') = %(stat_month)s
            )
            SELECT ROUND(promotions.cnt * 100.0 / NULLIF(total.cnt, 0), 1) AS value
            FROM total, promotions
        """,
        "dimensions": ["department"],
        "synonyms": ["晋升比例", "提拔率", "升职率"],
        "sensitivity": "low",
        "color_rule": "reverse",
        "description": "本月晋升人数占在职总人数的比例（操作原因描述包含'晋升'）",
    },

    "promotion_3yr_count": {
        "name": "近3年晋升人数",
        "category": "人才发展",
        "formula": "COUNT(DISTINCT emp_num) WHERE op_rsn_name LIKE '%晋升%' AND start_date >= 近3年",
        "unit": "人",
        "sql_template": f"""
            SELECT COUNT(DISTINCT emp_num) AS value
            FROM {TABLE_PROM}
            WHERE op_rsn_name LIKE %(kw_promotion)s
              AND start_date >= (TO_DATE(%(stat_month)s || '-01', 'YYYY-MM-DD') + INTERVAL '1 month' - INTERVAL '1 day') - INTERVAL '3 years'
        """,
        "dimensions": ["department"],
        "synonyms": ["三年晋升人数", "近三年晋升总数"],
        "sensitivity": "low",
        "color_rule": "reverse",
        "description": "近3年内发生晋升的员工人数（去重）",
    },

    "mp_unpromoted_3yr_count": {
        "name": "近3年未晋升MP人数",
        "category": "人才发展",
        "formula": "管理职族+专业职族在职人数中，近3年无晋升记录的人数",
        "unit": "人",
        "sql_template": f"""
            WITH mp_employees AS (
                SELECT DISTINCT emp_num
                FROM {TABLE_EMP}
                {SQL_WHERE_EMP}
                  AND emp_post_type_name IN ('管理职族', '专业职族')
            )
            SELECT COUNT(*) AS value
            FROM mp_employees m
            WHERE m.emp_num NOT IN (
                SELECT DISTINCT emp_num
                FROM {TABLE_PROM}
                WHERE op_rsn_name LIKE %(kw_promotion)s
                  AND start_date >= (TO_DATE(%(stat_month)s || '-01', 'YYYY-MM-DD') + INTERVAL '1 month' - INTERVAL '1 day') - INTERVAL '3 years'
                AND emp_num IS NOT NULL
            )
        """,
        "dimensions": ["department"],
        "synonyms": ["三年未晋升MP人数", "管理专业未晋升人数"],
        "sensitivity": "medium",
        "color_rule": "normal",
        "description": "管理职族和专业职族在职员工中近3年未获晋升的人数",
    },

    "demotion_rate": {
        "name": "降级率",
        "category": "人才发展",
        "formula": "本月降级人数 / 在职总人数 x 100%",
        "unit": "%",
        "sql_template": f"""
            WITH total AS (
                SELECT COUNT(DISTINCT emp_num) AS cnt
                FROM {TABLE_EMP}
                {SQL_WHERE_EMP}
            ),
            demotions AS (
                SELECT COUNT(DISTINCT emp_num) AS cnt
                FROM {TABLE_PROM}
                WHERE op_rsn_name LIKE %(kw_demotion)s
                  AND TO_CHAR(start_date, 'YYYY-MM') = %(stat_month)s
            )
            SELECT ROUND(demotions.cnt * 100.0 / NULLIF(total.cnt, 0), 1) AS value
            FROM total, demotions
        """,
        "dimensions": ["department"],
        "synonyms": ["降职率", "降级比例"],
        "sensitivity": "low",
        "color_rule": "normal",
        "description": "本月降级人数占在职总人数的比例（操作原因描述包含'降级'）",
    },

    "regularized_count": {
        "name": "转正人数",
        "category": "人才发展",
        "formula": "COUNT(DISTINCT emp_num) WHERE op_type_name = '转正' AND start_date 在统计月份",
        "unit": "人",
        "sql_template": f"""
            SELECT COUNT(DISTINCT emp_num) AS value
            FROM {TABLE_PROM}
            WHERE op_type_name = '转正'
              AND TO_CHAR(start_date, 'YYYY-MM') = %(stat_month)s
        """,
        "dimensions": ["department"],
        "synonyms": ["转正人数", "本月转正人数"],
        "sensitivity": "low",
        "color_rule": "reverse",
        "description": "本月完成转正的员工人数（操作类型描述='转正'）",
    },

}


# ═══════════════════════════════════════════════════════════════
# 维度分组 SQL 映射
# ═══════════════════════════════════════════════════════════════

DIMENSION_CONFIG = {
    "department": {"field": "lvl2_dept_name", "label": "部门"},
    "job_level": {"field": "emp_lvl_name", "label": "岗位层级"},
    "job_series": {"field": "emp_sub_group_name", "label": "职系"},
    "fee_type": {"field": "fee_type_name", "label": "费用类型"},
    "age_band": {"field": "age_sectn", "label": "年龄段"},
    "tenure_band": {"field": "div_sectn", "label": "司龄段"},
    "education": {"field": "ed_name", "label": "学历"},
    "gender": {"field": "gender", "label": "性别"},
    "mpo": {"field": None, "label": "MPO分类"},
    "front_mid_back": {"field": None, "label": "前中后台"},
    "employment_status": {"field": None, "label": "在岗状态"},
    "talent_review_result": {"field": "talent_review_result", "label": "人才盘点结果"},
}


# ═══════════════════════════════════════════════════════════════
# 颜色规则说明（供 LLM 理解方向性）
# ═══════════════════════════════════════════════════════════════

COLOR_RULES = {
    "normal": "升=红色(恶化), 降=绿色(改善) —— 适用于人数、成本、层级等指标",
    "reverse": "升=绿色(改善), 降=红色(恶化) —— 适用于管幅、高学历占比、关键人才等指标",
    None: "无方向性颜色 —— 适用于结构分布类指标",
}


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def get_metric(metric_name: str) -> dict | None:
    """根据指标名或同义词查找指标定义"""
    if metric_name in METRICS:
        return METRICS[metric_name]
    for key, metric in METRICS.items():
        if metric_name in metric.get("synonyms", []):
            return metric
    return None


def list_metrics() -> list[dict]:
    """列出所有可用指标（供 LLM 了解工具能力）"""
    return [
        {
            "key": key,
            "name": m["name"],
            "category": m["category"],
            "unit": m["unit"],
            "formula": m["formula"],
            "dimensions": m["dimensions"],
            "synonyms": m["synonyms"],
            "description": m["description"],
        }
        for key, m in METRICS.items()
    ]


def build_params(
    snapshot_date: str,
    dept_name: str = "",
    stat_month: str | None = None,
    role: str = "",
    dept_names: list[str] | tuple[str, ...] | None = None,
    company_level_one_excluded_names: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """构建 SQL 参数字典

    role: 人员角色过滤（仅 get_talent_overview / get_talent_structure 使用）
        - "M岗"/"管理干部" => emp_post_type_name = '管理职族'
        - "P岗"           => emp_post_type_name = '专业职族'
        - "关键岗位"       => is_key_post = '是'
        - "高潜人才"       => talent_review_result IN ('7-绩效之星','8-潜力之星','9-超级明星')
        - "本科以上人员"   => ed_name IN ('本科','硕士','博士')
        - 其他/空         => 四个角色参数均为 None，SQL_ROLE_FILTER 恒为真（不过滤）
    """
    role = (role or "").strip()
    if role in ("M岗", "管理干部"):
        role_post_type, role_is_key_post, role_talent_review, role_bachelor_above = "管理职族", None, None, None
    elif role == "P岗":
        role_post_type, role_is_key_post, role_talent_review, role_bachelor_above = "专业职族", None, None, None
    elif role == "关键岗位":
        role_post_type, role_is_key_post, role_talent_review, role_bachelor_above = None, "是", None, None
    elif role in ("高潜人才", "高潜"):
        role_post_type, role_is_key_post, role_talent_review, role_bachelor_above = None, None, "1", None
    elif role in ("本科以上人员", "本科及以上人员", "本科以上", "本科及以上", "高学历人员"):
        role_post_type, role_is_key_post, role_talent_review, role_bachelor_above = None, None, None, "1"
    else:
        role_post_type, role_is_key_post, role_talent_review, role_bachelor_above = None, None, None, None
    normalized_dept_names = [str(name).strip() for name in (dept_names or ()) if str(name).strip()]
    selected_dept_code = str(getattr(dept_name, "organization_code", "") or "").strip() or None
    selected_dept_level = getattr(dept_name, "organization_level", None)
    dept_path_parts = [part.strip() for part in str(dept_name or "").split("/") if part.strip()]
    is_dept_path = len(dept_path_parts) > 1 and not normalized_dept_names
    dept_levels = (dept_path_parts + [None] * 5)[:5] if is_dept_path else [None] * 5
    normalized_company_exclusions = [
        str(name).strip()
        for name in (company_level_one_excluded_names or ())
        if str(name).strip()
    ]
    return {
        "snapshot_date": snapshot_date,
        "stat_month": stat_month or snapshot_date[:7],
        "dept_name": None if (normalized_dept_names or is_dept_path) else (dept_name if dept_name else None),
        "dept_names": normalized_dept_names or None,
        "dept_path": is_dept_path,
        "dept_code": selected_dept_code,
        "dept_code_level": selected_dept_level,
        "dept_lvl1": dept_levels[0],
        "dept_lvl2": dept_levels[1],
        "dept_lvl3": dept_levels[2],
        "dept_lvl4": dept_levels[3],
        "dept_lvl5": dept_levels[4],
        "company_level_one_other": bool(normalized_company_exclusions),
        "company_level_one_excluded_names": normalized_company_exclusions or None,
        "excluded_groups": EXCLUDED_EMP_GROUPS,
        "excluded_lvl5_depts": EXCLUDED_LVL5_DEPTS,
        "excluded_lvl3_depts": EXCLUDED_LVL3_DEPTS,
        "excluded_emp_status": EXCLUDED_EMP_STATUS,
        "excluded_post_names": EXCLUDED_POST_NAMES,
        "mgmt_levels": MGMT_LEVELS,
        "mp_post_types": MP_POST_TYPES,
        "turnover_oper_types": TURNOVER_OPER_TYPES,
        # 前中后台分布的 LIKE 模式（注意：psycopg2 把 %(name)s 当 Python %-格式化处理，
        # 字面量 % 必须作为参数传入，否则会触发 TypeError: dict is not a sequence）
        "kw_market": f"%{FRONT_OFFICE_KEYWORDS[0]}%",
        "kw_supply": f"%{MID_OFFICE_KEYWORDS[0]}%",
        "kw_manufacturing": f"%{MID_OFFICE_KEYWORDS[1]}%",
        "kw_quality": f"%{MID_OFFICE_KEYWORDS[2]}%",
        "kw_rd": f"%{MID_OFFICE_KEYWORDS[3]}%",
        "kw_finance": f"%{BACK_OFFICE_KEYWORDS[0]}%",
        "kw_admin": f"%{BACK_OFFICE_KEYWORDS[1]}%",
        "kw_hr": f"%{BACK_OFFICE_KEYWORDS[2]}%",
        "kw_digital": f"%{BACK_OFFICE_KEYWORDS[3]}%",
        "kw_legal": f"%{BACK_OFFICE_KEYWORDS[4]}%",
        "kw_strategy": f"%{BACK_OFFICE_KEYWORDS[5]}%",
        # 晋升/降级事件的 LIKE 模式（与上述同一规范：字面量 % 必须作为参数传入）
        "kw_promotion": "%晋升%",
        "kw_demotion": "%降级%",
        # 人员角色过滤参数（留空为 None → SQL_ROLE_FILTER 恒为真，不过滤）
        "role_post_type": role_post_type,
        "role_is_key_post": role_is_key_post,
        "role_talent_review": role_talent_review,
        "role_bachelor_above": role_bachelor_above,
    }
