# -*- coding: utf-8 -*-
"""买卖指令类型（passorder 的 opType）。

数值出自迅投官方枚举：
https://dict.thinktrader.net/innerApi/enum_constants.html?id=NF25nX

这里的错法很贵：信用账户的「买」用 23 发出去，交易所收到的是一笔真实但业务类型
不同的单（普通买入而非担保品买入），比直接报错糟得多。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import optypes


class OfficialValueTests(unittest.TestCase):
    def test_values_match_the_official_table(self):
        expected = {
            #  key           买   卖
            "normal":       (23, 24),   # 股票/ETF/可转债
            "collateral":   (33, 34),   # 担保品买入 / 担保品卖出
            "margin":       (27, 28),   # 融资买入 / 融券卖出
            "repay":        (29, 31),   # 买券还券 / 卖券还款
        }
        for key, (buy, sell) in expected.items():
            row = optypes.BY_KEY[key]
            self.assertEqual((row[3], row[4]), (buy, sell), key)

    def test_no_two_modes_share_a_side_value(self):
        pairs = [(row[3], row[4]) for row in optypes._TABLE]
        self.assertEqual(len(pairs), len(set(pairs)))


class AccountTypeTests(unittest.TestCase):
    def test_stock_account_gets_one_mode_only(self):
        # 只有一条时前端不显示选择器
        self.assertEqual([c["value"] for c in optypes.choices_for("STOCK")], ["normal"])

    def test_credit_account_gets_the_credit_modes_plus_the_plain_one(self):
        # 普通买卖不从列表里拿掉：23/24 本身是合法 opType，
        # 信用账户能不能用看券商，不该由面板替他决定。
        self.assertEqual([c["value"] for c in optypes.choices_for("CREDIT")],
                         ["normal", "collateral", "margin", "repay"])

    def test_defaults_are_the_non_leveraged_ones(self):
        # 默认绝不能是融资买入：默认值应该是不产生负债的那条
        self.assertEqual(optypes.default_mode_for("STOCK"), "normal")
        self.assertEqual(optypes.default_mode_for("CREDIT"), "collateral")

    def test_unknown_account_type_falls_back_to_stock(self):
        # 账户类型没配/配错时按普通账户走：绝大多数是普通账户，且 23/24 对它是对的
        for value in (None, "", "  ", "stock", "futures", 123):
            self.assertEqual(optypes.normalize_account_type(value), "STOCK")
        self.assertEqual(optypes.resolve("", 123, "buy")[0], 23)

    def test_credit_is_recognised_case_insensitively(self):
        for value in ("credit", "CREDIT", " Credit "):
            self.assertEqual(optypes.normalize_account_type(value), "CREDIT")


class ResolveTests(unittest.TestCase):
    def test_empty_mode_takes_the_account_default(self):
        self.assertEqual(optypes.resolve("", "STOCK", "buy")[0], 23)
        self.assertEqual(optypes.resolve("", "STOCK", "sell")[0], 24)
        self.assertEqual(optypes.resolve(None, "CREDIT", "buy")[0], 33)
        self.assertEqual(optypes.resolve(None, "CREDIT", "sell")[0], 34)

    def test_credit_modes_resolve_to_their_own_op_types(self):
        self.assertEqual(optypes.resolve("margin", "CREDIT", "buy")[0], 27)
        self.assertEqual(optypes.resolve("margin", "CREDIT", "sell")[0], 28)
        self.assertEqual(optypes.resolve("repay", "CREDIT", "buy")[0], 29)
        self.assertEqual(optypes.resolve("repay", "CREDIT", "sell")[0], 31)

    def test_a_mode_the_account_cannot_use_is_rejected(self):
        # 普通账户报融资买入 = 券商直接拒单，没必要发出去
        with self.assertRaises(ValueError) as ctx:
            optypes.resolve("margin", "STOCK", "buy")
        self.assertIn("CREDIT", str(ctx.exception))

    def test_plain_mode_stays_available_on_a_credit_account(self):
        self.assertEqual(optypes.resolve("normal", "CREDIT", "buy")[0], 23)
        self.assertEqual(optypes.resolve("normal", "CREDIT", "sell")[0], 24)

    def test_unknown_mode_raises_rather_than_guessing(self):
        for value in ("nope", "融资", 27):
            with self.assertRaises(ValueError):
                optypes.resolve(value, "CREDIT", "buy")

    def test_bad_side_raises(self):
        for side in ("", "long", None):
            with self.assertRaises(ValueError):
                optypes.resolve("", "STOCK", side)

    def test_listing_choices_has_no_side_label(self):
        # 列选项时方向还没定，默认给一个“担保品买入”会直接显在卖出弹窗上。
        # 让调用方拿 buy_label / sell_label 自己选。
        for choice in optypes.choices_for("CREDIT"):
            self.assertNotIn("side_label", choice, choice["value"])
            self.assertTrue(choice["buy_label"] and choice["sell_label"])
        for choice in optypes.choices_for("CREDIT", side="sell"):
            self.assertEqual(choice["side_label"], choice["sell_label"])

    def test_side_label_follows_the_direction(self):
        self.assertEqual(optypes.resolve("", "CREDIT", "buy")[1]["side_label"], "担保品买入")
        self.assertEqual(optypes.resolve("margin", "CREDIT", "sell")[1]["side_label"], "融券卖出")


if __name__ == "__main__":
    unittest.main()
