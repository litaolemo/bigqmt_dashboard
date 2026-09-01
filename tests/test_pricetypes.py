"""下单选价类型（passorder 的 prType）。

数值和适用范围来自迅投官方枚举，不是按规律推的：
https://dict.thinktrader.net/innerApi/enum_constants.html?id=NF25nX

最要紧的一条是**分交易所**：42/43 只有沪/北能用，46/47/48 只有深能用。
给深市票报 42 会被交易所直接拒单，所以下拉里就不该出现。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import pricetypes as pt


class OfficialValueTests(unittest.TestCase):
    """每个 key 的 prType 必须和官方表一致。"""

    OFFICIAL = {
        "latest": 5,    # 最新价
        "fix": 11,      # 指定价
        "stop": 12,     # 涨跌停价
        "mine_l1": 13,  # 挂单价（本方一档）
        "peer_l1": 14,  # 对手价（对方一档）
        "peer": 44,     # 对手方最优价格委托
        "mine": 45,     # 本方最优价格委托
        "sh_five_cancel": 42,
        "sh_five_limit": 43,
        "sz_cancel": 46,
        "sz_five_cancel": 47,
        "sz_fok": 48,
        "after_hours": 49,
        "ask1": 4, "ask5": 0, "bid1": 6, "bid5": 10,
    }

    def test_values_match_the_official_table(self):
        for key, prtype in self.OFFICIAL.items():
            self.assertEqual(pt.BY_KEY[key][1], prtype, key)

    def test_legacy_keys_keep_their_historical_values(self):
        # peer/mine 一直是 44/45。改成 14/13 会让同一个请求从交易所市价指令
        # 变成一档限价单 —— 对既有调用方是静默行为变更。
        self.assertEqual(pt.resolve("peer")[0], 44)
        self.assertEqual(pt.resolve("mine")[0], 45)

    def test_no_duplicate_pr_types(self):
        values = [row[1] for row in pt._TABLE]
        self.assertEqual(len(values), len(set(values)), "prType 有重复")

    def test_undefined_values_are_absent(self):
        # 官方表里没有 15/16/17、25、30-41
        for missing in (15, 16, 17, 25, 30, 35, 41):
            self.assertNotIn(missing, pt.BY_PRTYPE, missing)


class ExchangeFilterTests(unittest.TestCase):
    def _values(self, code):
        return {c["value"] for c in pt.choices_for(code)}

    def test_shanghai_gets_the_shanghai_market_orders_only(self):
        values = self._values("600000.SH")
        self.assertIn("sh_five_cancel", values)
        self.assertIn("sh_five_limit", values)
        for shenzhen_only in ("sz_cancel", "sz_five_cancel", "sz_fok"):
            self.assertNotIn(shenzhen_only, values, shenzhen_only)

    def test_shenzhen_gets_the_shenzhen_market_orders_only(self):
        values = self._values("000001.SZ")
        for shenzhen_only in ("sz_cancel", "sz_five_cancel", "sz_fok"):
            self.assertIn(shenzhen_only, values, shenzhen_only)
        for shanghai_only in ("sh_five_cancel", "sh_five_limit"):
            self.assertNotIn(shanghai_only, values, shanghai_only)

    def test_beijing_follows_shanghai_and_has_no_after_hours(self):
        values = self._values("920819.BJ")
        self.assertIn("sh_five_cancel", values)
        self.assertNotIn("sz_fok", values)
        # 盘后定价是科创板/创业板的，北交所没有
        self.assertNotIn("after_hours", values)

    def test_common_types_are_available_everywhere(self):
        for code in ("600000.SH", "000001.SZ", "920819.BJ", "113050.SH", "123281.SZ"):
            values = self._values(code)
            for common in ("latest", "fix", "peer", "mine", "stop"):
                self.assertIn(common, values, "%s / %s" % (code, common))

    def test_unknown_code_gets_the_full_set(self):
        # 认不出交易所时给全集：少给选项比给错更烦人，服务端和交易所还会把关
        self.assertEqual(len(pt.choices_for("")), len(pt._TABLE))

    def test_wrong_exchange_is_rejected_with_a_reason(self):
        with self.assertRaises(ValueError) as ctx:
            pt.resolve("sh_five_cancel", "000001.SZ")
        self.assertIn("只适用于", str(ctx.exception))
        self.assertIn("SH", str(ctx.exception))

        with self.assertRaises(ValueError):
            pt.resolve("sz_fok", "600000.SH")


class PriceRoleTests(unittest.TestCase):
    """price 字段有三种语义，混了就会出错单。"""

    def test_limit_types_treat_price_as_the_order_price(self):
        for key in ("fix", "after_hours"):
            self.assertEqual(pt.BY_KEY[key][4], pt.PRICE_ROLE_ORDER, key)
            self.assertTrue(pt._as_dict(pt.BY_KEY[key])["needs_price"], key)

    def test_market_types_treat_price_as_a_protective_limit(self):
        for key in ("peer", "mine", "sh_five_cancel", "sz_fok"):
            self.assertEqual(pt.BY_KEY[key][4], pt.PRICE_ROLE_PROTECT, key)
            spec = pt._as_dict(pt.BY_KEY[key])
            self.assertFalse(spec["needs_price"], key)   # 可以不填
            self.assertTrue(spec["accepts_price"], key)  # 但填了有意义

    def test_level_types_ignore_price(self):
        for key in ("latest", "stop", "peer_l1", "mine_l1", "ask1", "bid5"):
            self.assertEqual(pt.BY_KEY[key][4], pt.PRICE_ROLE_NONE, key)
            self.assertFalse(pt._as_dict(pt.BY_KEY[key])["accepts_price"], key)


class ResolveTests(unittest.TestCase):
    def test_numeric_pr_type_is_accepted(self):
        self.assertEqual(pt.resolve(44, "600000.SH")[0], 44)
        self.assertEqual(pt.resolve("44", "600000.SH")[0], 44)

    def test_empty_falls_back_to_the_default(self):
        self.assertEqual(pt.resolve(None)[0], pt.BY_KEY[pt.DEFAULT_KEY][1])
        self.assertEqual(pt.resolve("")[0], 5)

    def test_unknown_raises_rather_than_guessing(self):
        # 猜一个报价方式去下单是最不该做的事
        for value in ("nope", 999, object()):
            with self.assertRaises(ValueError):
                pt.resolve(value)

    def test_groups_are_ordered_for_the_ui(self):
        groups = [c["group"] for c in pt.choices_for("600000.SH")]
        seen = []
        for g in groups:
            if g not in seen:
                seen.append(g)
        self.assertEqual(seen, [g for g in pt.GROUP_ORDER if g in seen])
        self.assertEqual(seen[0], "常用")


if __name__ == "__main__":
    unittest.main()
