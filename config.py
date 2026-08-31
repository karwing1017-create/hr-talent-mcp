"""
配置管理模块 —— 从环境变量读取数据库连接等配置。
支持 .env 文件（开发时）和系统环境变量（部署时）。
"""

import os
import sys
import logging

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # 生产环境可能没有 python-dotenv，直接用环境变量


def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ── 数据库连接配置 ──
DW_HOST = _get_env("DW_HOST")
DW_PORT = _get_env("DW_PORT", "5432")
DW_DATABASE = _get_env("DW_DATABASE")
DW_USER = _get_env("DW_USER")
DW_PASSWORD = _get_env("DW_PASSWORD")

# ── MCP HTTP 服务配置（sse / streamable-http 模式）──
MCP_TRANSPORT = _get_env("MCP_TRANSPORT", "stdio")
MCP_HOST = _get_env("MCP_HOST", "0.0.0.0")
MCP_PORT = int(_get_env("MCP_PORT", "8000"))
MCP_PATH = _get_env("MCP_PATH", "/mcp")

# ── Schema 配置 ──
SCHEMA_DWRDIM = _get_env("DW_SCHEMA_DWRDIM", "DWRDIM")
SCHEMA_DWRHR = _get_env("DW_SCHEMA_DWRHR", "DWRHR")
SCHEMA_DM = _get_env("DW_SCHEMA_DM", "DM")
SCHEMA_UPLOAD = _get_env("DW_SCHEMA_UPLOAD", "upload")

# ── 表名常量（集中管理，方便修改）──
# DWRDIM 层（维度表，stat_date 快照）
TABLE_EMP = f"{SCHEMA_DWRDIM}.DWR_DIM_SAP_EMP_INFO_F"       # 员工信息表
TABLE_DEPT = f"{SCHEMA_DWRDIM}.DWR_DIM_SAP_DEPT_INFO_F"     # 组织架构表
TABLE_ESTBL = f"{SCHEMA_DWRDIM}.DWR_DIM_SAP_ESTBL_STAT_F"   # 组织编制表

# DWRHR 层（事务明细表，stat_date 快照）
TABLE_TRMNT = f"{SCHEMA_DWRHR}.DWR_HR_EMP_TRMNT_INFO_F"     # 员工离职明细表
TABLE_ENTRY = f"{SCHEMA_DWRHR}.DWR_HR_EMP_ENTRY_INFO_F"    # 员工入职明细表
TABLE_PROM = f"{SCHEMA_DWRHR}.DWR_HR_EMP_PROM_INFO_F"       # 人事晋升表

# DM 层（汇总表，stat_month 月度）
TABLE_MNG_STAT = f"{SCHEMA_DM}.DM_HR_SAP_MNG_STAT_T"          # 管理干部管幅表
TABLE_DEPT_STAT = f"{SCHEMA_DM}.DM_HR_SAP_DEPT_STAT_T"         # 组织汇总表
TABLE_TRMNT_STAT = f"{SCHEMA_DM}.DM_HR_SAP_EMP_TRMNT_STAT_T"   # 员工离职汇总表
TABLE_ENTRY_STAT = f"{SCHEMA_DM}.DM_HR_SAP_EMP_ENTRY_STAT_T"   # 员工入职汇总表

# upload 层（历史组织映射填报表）
TABLE_HISTORY_ORG_MAPPING = f"{SCHEMA_UPLOAD}.upload_history_org_mapping_t"

# ── 日志配置 ──
LOG_LEVEL = _get_env("LOG_LEVEL", "INFO")

logging.basicConfig(
    stream=sys.stderr,  # MCP stdio 模式下日志只能输出到 stderr
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("hr-talent-mcp")


def get_connection_params() -> dict:
    """返回 psycopg2 连接参数字典"""
    return {
        "host": DW_HOST,
        "port": DW_PORT,
        "dbname": DW_DATABASE,
        "user": DW_USER,
        "password": DW_PASSWORD,
    }


def check_config() -> bool:
    """检查必要的环境变量是否已配置"""
    required = ["DW_HOST", "DW_DATABASE", "DW_USER", "DW_PASSWORD"]
    missing = [k for k in required if not _get_env(k)]
    if missing:
        logger.error(f"缺少必要环境变量: {', '.join(missing)}")
        logger.error("请复制 .env.example 为 .env 并填写数据库连接信息")
        return False
    return True
