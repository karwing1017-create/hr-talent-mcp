"""离线校验：人员角色过滤的入参与 SQL 参数完整性（无需数据库）。"""
import re
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import metrics  # noqa: E402  (导入即校验 syntax)
import server  # noqa: E402


def main():
    # 1) 模块导入成功（已在 import 时验证 syntax）
    print("[1] 模块导入 OK")

    # 2) normalize_role 归一化与校验
    cases = [
        ("", ""), ("M岗", "M岗"), ("管理干部", "管理干部"), ("P岗", "P岗"),
        ("关键岗位", "关键岗位"), ("高潜人才", "高潜人才"), ("高潜", "高潜人才"),
        ("本科以上人员", "本科以上人员"), ("本科及以上人员", "本科以上人员"),
        ("本科以上", "本科以上人员"), ("高学历人员", "本科以上人员"),
        ("m", "M岗"), ("P", "P岗"),
    ]
    for inp, exp in cases:
        got = server.normalize_role(inp)
        assert got == exp, f"normalize_role({inp!r})={got!r} 期望 {exp!r}"
    try:
        server.normalize_role("X岗")
        raise AssertionError("非法角色应抛 ValueError")
    except ValueError:
        pass
    print("[2] normalize_role 归一化 + 非法值拦截 OK")

    # 3) build_params 角色映射
    mapping = [
        ("", (None, None, None, None)),
        ("M岗", ("管理职族", None, None, None)),
        ("管理干部", ("管理职族", None, None, None)),
        ("P岗", ("专业职族", None, None, None)),
        ("关键岗位", (None, "是", None, None)),
        ("高潜人才", (None, None, "1", None)),
        ("高潜", (None, None, "1", None)),
        ("本科以上人员", (None, None, None, "1")),
        ("本科及以上", (None, None, None, "1")),
    ]
    for role, (pt, kp, tr, ba) in mapping:
        p = metrics.build_params("2026-07-31", "", stat_month="2026-07", role=role)
        assert p["role_post_type"] == pt, (role, p["role_post_type"])
        assert p["role_is_key_post"] == kp, (role, p["role_is_key_post"])
        assert p["role_talent_review"] == tr, (role, p["role_talent_review"])
        assert p["role_bachelor_above"] == ba, (role, p["role_bachelor_above"])
    print("[3] build_params 角色映射 OK")

    # 4) SQL 参数完整性：所有指标模板引用的 %(name)s 必须都在 params 中
    ph = re.compile(r"%\((\w+)\)s")
    all_keys = list(metrics.METRICS.keys())
    roles = ["", "M岗", "P岗", "关键岗位", "高潜人才", "本科以上人员"]
    for role in roles:
        p = metrics.build_params("2026-07-31", "", stat_month="2026-07", role=role)
        for k in all_keys:
            tpl = metrics.METRICS[k]["sql_template"]
            for name in ph.findall(tpl):
                assert name in p, f"指标 {k} role={role!r}: 缺少参数 {name}"
    print(f"[4] SQL 参数完整性 OK（{len(all_keys)} 个指标 × {len(roles)} 种角色）")

    # 5) 角色过滤已接入 WHERE
    assert "%(role_post_type)s" in metrics.SQL_WHERE_EMP
    assert "%(role_is_key_post)s" in metrics.SQL_WHERE_EMP
    assert "%(role_talent_review)s" in metrics.SQL_WHERE_EMP
    assert "%(role_bachelor_above)s" in metrics.SQL_WHERE_EMP
    assert "%(role_post_type)s" in metrics.METRICS["intern_count"]["sql_template"]
    assert "%(role_talent_review)s" in metrics.METRICS["intern_count"]["sql_template"]
    assert "%(role_bachelor_above)s" in metrics.METRICS["intern_count"]["sql_template"]
    # 高潜人才过滤 SQL 口径：talent_review_result IN (7-绩效之星/8-潜力之星/9-超级明星)
    assert "7-绩效之星" in metrics.SQL_ROLE_FILTER
    assert "8-潜力之星" in metrics.SQL_ROLE_FILTER
    assert "9-超级明星" in metrics.SQL_ROLE_FILTER
    # 本科以上人员沿用本科及以上指标口径
    assert "ed_name IN ('本科', '硕士', '博士')" in metrics.SQL_ROLE_FILTER
    # 留空时角色过滤恒为真（四参数均为 None）
    p_all = metrics.build_params("2026-07-31", "", stat_month="2026-07", role="")
    assert p_all["role_post_type"] is None and p_all["role_is_key_post"] is None
    assert p_all["role_talent_review"] is None
    assert p_all["role_bachelor_above"] is None
    print("[5] 角色过滤已接入 WHERE，且留空时不过滤 OK")

    print("\n全部离线校验通过 ✅")


if __name__ == "__main__":
    main()
