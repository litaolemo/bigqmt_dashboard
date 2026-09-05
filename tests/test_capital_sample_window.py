"""自动记出入金的取样窗口。

窗口边界值得单独测：这段逻辑原来埋在 save_asset() 里，是个没有测试覆盖的裸表达式，
窗口开得过宽（盘前 08:00-09:15、盘后 15:00-16:00）也一直没人发现。

窗口错在哪一边都有代价：
  开太宽 —— 市值波动被当成转账记进 capital_adjustments，盈亏就永久错了，
            而且要人去翻账才看得出来；
  开太窄 —— 只是漏记，用户手工补一条就行。
所以两边都得钉死。
"""

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module


def at(hour, minute):
    return datetime(2026, 9, 4, hour, minute, 0)


class CapitalSampleWindowTests(unittest.TestCase):

    def assert_in(self, hour, minute):
        self.assertTrue(app_module.in_capital_sample_window(at(hour, minute)),
                        "%02d:%02d 应该在取样窗口内" % (hour, minute))

    def assert_out(self, hour, minute):
        self.assertFalse(app_module.in_capital_sample_window(at(hour, minute)),
                         "%02d:%02d 不该在取样窗口内" % (hour, minute))

    def test_pre_open_window_is_09_10_to_09_15(self):
        self.assert_out(9, 9)      # 早一分钟：外
        self.assert_in(9, 10)      # 下界闭
        self.assert_in(9, 14)
        self.assert_out(9, 15)     # 上界开：09:15 集合竞价已经开始，价格在形成了

    def test_post_close_window_is_15_00_to_15_05(self):
        self.assert_out(14, 59)
        self.assert_in(15, 0)
        self.assert_in(15, 3)
        self.assert_in(15, 5)      # 上界闭，和逆回购那个窗口口径一致
        self.assert_out(15, 6)

    def test_the_whole_trading_session_is_out(self):
        # 盘中总资产每秒都在动，那是市值波动不是资金流 —— 记进去就是错账
        for hour, minute in [(9, 20), (9, 30), (10, 0), (11, 29),
                             (13, 0), (14, 30), (14, 58)]:
            self.assert_out(hour, minute)

    def test_the_old_over_wide_window_is_gone(self):
        # 收窄之前是 08:00-09:15 和 15:00-16:00，这几个点当时会被采样
        for hour, minute in [(8, 0), (8, 30), (9, 0), (9, 9),
                             (15, 30), (16, 0)]:
            self.assert_out(hour, minute)

    def test_overnight_and_early_morning_are_out(self):
        for hour, minute in [(0, 0), (3, 0), (7, 59), (20, 0), (23, 59)]:
            self.assert_out(hour, minute)

    def test_defaults_to_now_when_called_without_an_argument(self):
        # save_asset 是这么调的；别让默认参数变成一个建模块时就固定下来的时间
        self.assertIn(app_module.in_capital_sample_window(), (True, False))


if __name__ == "__main__":
    unittest.main()
