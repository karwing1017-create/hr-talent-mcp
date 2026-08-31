"""费用类型分布指标离线测试。"""

import json
import unittest
from unittest.mock import patch

import metrics
import server


class FeeTypeDistributionTest(unittest.TestCase):
    def test_metric_definition(self):
        metric = metrics.METRICS["fee_type_distribution"]
        sql = metric["sql_template"]

        self.assertEqual(metric["name"], "直接间接人员分布")
        self.assertIn("fee_type_name", sql)
        self.assertIn("WHEN '制造费用' THEN '直接人员'", sql)
        self.assertIn("WHEN '期间费用' THEN '间接人员'", sql)
        self.assertIn("fee_type_name IN ('制造费用', '期间费用')", sql)
        for synonym in ("制造成本人数", "期间成本人数", "制造费用人数", "期间费用人数"):
            self.assertIn(synonym, metric["synonyms"])
        self.assertIn('dimension="fee_type"', server.get_talent_structure.__doc__ or "")
        self.assertIn("即使用户只提到其中一类，也不得只返回单项", server.get_talent_structure.__doc__ or "")

    def test_structure_routing(self):
        calls = []

        def fake_query(metric_key, snapshot_date, dept_name="", **kwargs):
            calls.append((metric_key, snapshot_date, dept_name, kwargs.get("role")))
            return [
                {"dimension": "直接人员", "count": 40, "percentage": 40.0},
                {"dimension": "间接人员", "count": 60, "percentage": 60.0},
            ]

        with (
            patch.object(server, "get_latest_snapshot_date", return_value="2025-12-31"),
            patch.object(server, "_resolve_department_matches", return_value=[]),
            patch.object(server, "_query_distribution_metric", side_effect=fake_query),
        ):
            aliases = (
                "fee_type", "费用类型", "费用分类", "直接间接人员", "直接人员", "间接人员",
                "费用类型人数及占比", "成本类型人数及占比",
                "制造成本人数", "期间成本人数", "制造费用人数", "期间费用人数",
                "制造成本人数及占比", "期间成本人数及占比", "制造费用人数及占比", "期间费用人数及占比",
                "制造成本人员", "期间成本人员", "制造费用人员", "期间费用人员",
                "制造成本占比", "期间成本占比", "制造费用占比", "期间费用占比",
            )
            for dimension in aliases:
                result = json.loads(server.get_talent_structure(dimension, "2025-12-31", "瓷砖事业部"))
                self.assertEqual(result["data"]["dimension"], "fee_type")
                self.assertEqual(result["data"]["dimension_label"], "费用类型")
                self.assertEqual(result["data"]["total_count"], 100)
                self.assertEqual(result["data"]["items"][0]["dimension"], "直接人员")

        self.assertEqual(calls, [
            ("fee_type_distribution", "2025-12-31", "瓷砖事业部", "")
        ] * len(aliases))


if __name__ == "__main__":
    unittest.main()
