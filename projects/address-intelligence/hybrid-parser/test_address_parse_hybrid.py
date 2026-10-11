import unittest
from unittest.mock import patch

import address_parse_hybrid as aph


class AddressParseHybridTests(unittest.TestCase):
    def test_multiple_room_segments_are_preserved(self):
        result = aph.parse_rule("广州市海珠区新港中路397号101室；102室")

        self.assertEqual(result["road"], "新港中路")
        self.assertEqual(result["road_no"], "397号")
        self.assertEqual(result["room_or_shop"], "101室；102室")

    def test_poi_before_road_does_not_pollute_road(self):
        result = aph.parse_rule("天易茶业广场芳村大道中443号B101铺；B102铺")

        self.assertEqual(result["poi_or_market"], "天易茶业广场")
        self.assertEqual(result["road"], "芳村大道中")
        self.assertEqual(result["road_no"], "443号")
        self.assertEqual(result["room_or_shop"], "B101铺；B102铺")

    def test_admin_fields_are_generic_not_guangzhou_only(self):
        result = aph.parse_rule("浙江省杭州市西湖区创新大厦文三路90号A座501室（临时经营）")

        self.assertEqual(result["province"], "浙江省")
        self.assertEqual(result["city"], "杭州市")
        self.assertEqual(result["district"], "西湖区")
        self.assertEqual(result["poi_or_market"], "创新大厦")
        self.assertEqual(result["road"], "文三路")
        self.assertEqual(result["road_no"], "90号")
        self.assertIn("A座", result["remark"])
        self.assertIn("临时经营", result["remark"])

    def test_llm_only_remark_is_merged_instead_of_dropped(self):
        rule_result = {
            "raw_address": "仓库后门蓝色铁门旁",
            "clean_address": "仓库后门蓝色铁门旁",
            "province": None,
            "city": None,
            "district": None,
            "town_or_street": None,
            "road": None,
            "road_no": None,
            "sub_no": None,
            "poi_or_market": None,
            "floor": None,
            "room_or_shop": None,
            "remark": None,
            "parse_method": None,
            "rule_status": "llm_fallback",
            "rule_reason": "missing_road",
            "needs_review": 0,
        }

        with patch.object(aph, "call_llm", return_value={"remark": "仓库后门蓝色铁门旁"}):
            result = aph.resolve_one(rule_result, client=object(), model="fake-model")

        self.assertEqual(result["parse_method"], "llm")
        self.assertEqual(result["remark"], "仓库后门蓝色铁门旁")


if __name__ == "__main__":
    unittest.main()
