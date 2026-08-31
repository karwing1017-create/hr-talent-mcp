"""
探查脚本：查看员工信息表的实际数据分布，帮助调整 SQL 过滤条件。
用法：.venv\\Scripts\\python.exe explore_data.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import execute_query


def explore():
    print('=== 1. 查看员工表最近 10 个快照日期 ===')
    sql = """
    SELECT stat_date, COUNT(*) AS cnt
    FROM DWRDIM.DWR_DIM_SAP_EMP_INFO_F
    GROUP BY stat_date
    ORDER BY stat_date DESC
    LIMIT 10
    """
    rows = execute_query(sql)
    for r in rows:
        print(f'  {r["stat_date"]}: {r["cnt"]} 人')

    print('\n=== 2. 查看最近快照日期的员工组分布 ===')
    sql = """
    SELECT emp_group_name, COUNT(*) AS cnt
    FROM DWRDIM.DWR_DIM_SAP_EMP_INFO_F
    WHERE stat_date = (SELECT MAX(stat_date) FROM DWRDIM.DWR_DIM_SAP_EMP_INFO_F)
    GROUP BY emp_group_name
    ORDER BY cnt DESC
    """
    rows = execute_query(sql)
    for r in rows:
        print(f'  {r["emp_group_name"] or "(空)"}: {r["cnt"]} 人')

    print('\n=== 3. 查看最新快照日期的岗位层级分布 ===')
    sql = """
    SELECT emp_lvl_name, COUNT(*) AS cnt
    FROM DWRDIM.DWR_DIM_SAP_EMP_INFO_F
    WHERE stat_date = (SELECT MAX(stat_date) FROM DWRDIM.DWR_DIM_SAP_EMP_INFO_F)
    GROUP BY emp_lvl_name
    ORDER BY cnt DESC
    """
    rows = execute_query(sql)
    for r in rows:
        print(f'  {r["emp_lvl_name"] or "(空)"}: {r["cnt"]} 人')

    print('\n=== 4. 查看关键岗位字段的实际值 ===')
    sql = """
    SELECT is_key_post, COUNT(*) AS cnt
    FROM DWRDIM.DWR_DIM_SAP_EMP_INFO_F
    WHERE stat_date = (SELECT MAX(stat_date) FROM DWRDIM.DWR_DIM_SAP_EMP_INFO_F)
    GROUP BY is_key_post
    ORDER BY cnt DESC
    """
    rows = execute_query(sql)
    for r in rows:
        print(f'  {r["is_key_post"] or "(空)"}: {r["cnt"]} 人')

    print('\n=== 5. 查看新锐班字段的实际值 ===')
    sql = """
    SELECT is_xrb, COUNT(*) AS cnt
    FROM DWRDIM.DWR_DIM_SAP_EMP_INFO_F
    WHERE stat_date = (SELECT MAX(stat_date) FROM DWRDIM.DWR_DIM_SAP_EMP_INFO_F)
    GROUP BY is_xrb
    ORDER BY cnt DESC
    """
    rows = execute_query(sql)
    for r in rows:
        print(f'  {r["is_xrb"] or "(空)"}: {r["cnt"]} 人')

    print('\n=== 6. 查看岗位属性、职族等字段示例 ===')
    sql = """
    SELECT gender, ed_name, age, age_sectn, div_age, div_sectn, emp_post_type_name, emp_post_attr_name
    FROM DWRDIM.DWR_DIM_SAP_EMP_INFO_F
    WHERE stat_date = (SELECT MAX(stat_date) FROM DWRDIM.DWR_DIM_SAP_EMP_INFO_F)
    LIMIT 5
    """
    rows = execute_query(sql)
    for r in rows:
        print(f'  {dict(r)}')

    print('\n=== 7. 查看岗位属性分布（用于 MPO 分类） ===')
    sql = """
    SELECT emp_post_attr_name, COUNT(*) AS cnt
    FROM DWRDIM.DWR_DIM_SAP_EMP_INFO_F
    WHERE stat_date = (SELECT MAX(stat_date) FROM DWRDIM.DWR_DIM_SAP_EMP_INFO_F)
    GROUP BY emp_post_attr_name
    ORDER BY cnt DESC
    """
    rows = execute_query(sql)
    for r in rows:
        print(f'  {r["emp_post_attr_name"] or "(空)"}: {r["cnt"]} 人')

    print('\n=== 8. 查看职族分布 ===')
    sql = """
    SELECT emp_post_type_name, COUNT(*) AS cnt
    FROM DWRDIM.DWR_DIM_SAP_EMP_INFO_F
    WHERE stat_date = (SELECT MAX(stat_date) FROM DWRDIM.DWR_DIM_SAP_EMP_INFO_F)
    GROUP BY emp_post_type_name
    ORDER BY cnt DESC
    """
    rows = execute_query(sql)
    for r in rows:
        print(f'  {r["emp_post_type_name"] or "(空)"}: {r["cnt"]} 人')


if __name__ == '__main__':
    explore()
