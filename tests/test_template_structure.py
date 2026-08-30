"""index_vue.html 的结构完整性。

起因：改「历史买入列表 → 买卖流水」时，替换掉的那段末尾带着左栏的 </el-col>，
一起被删了。结果右栏 <el-col :md="8"> 嵌进了左栏里，交易日历从右边掉到下面、
宽度只剩 8/16。Vue 不会报错，测试也全绿 —— 只有肉眼看得出来。

这里对模板做结构体检：标签配对 + 主栅格布局。
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HTML = (ROOT / "index_vue.html").read_text(encoding="utf-8")

# 需要成对出现的容器型标签（自闭合写法也算数）
PAIRED_TAGS = (
    "el-row", "el-col", "el-dialog", "el-table", "el-table-column",
    "el-tabs", "el-tab-pane", "el-select", "el-radio-group", "template",
)


class TagBalanceTests(unittest.TestCase):
    def test_container_tags_are_balanced(self):
        for tag in PAIRED_TAGS:
            opens = len(re.findall(r"<%s[\s>]" % tag, HTML))
            closes = len(re.findall(r"</%s>" % tag, HTML))
            self_closing = len(re.findall(r"<%s[^>]*/>" % tag, HTML))
            self.assertEqual(
                opens, closes + self_closing,
                "%s 标签不配对：开 %d，闭 %d，自闭合 %d" % (tag, opens, closes, self_closing))


class MainGridTests(unittest.TestCase):
    """主面板是「左 2/3 持仓 + 右 1/3 统计」的两栏布局。"""

    def _main_row(self):
        start = HTML.index('<el-row :gutter="20">\n')
        end = HTML.index("</el-row>", start)
        return HTML[start:end]

    def test_main_row_has_exactly_two_columns(self):
        row = self._main_row()
        cols = re.findall(r"<el-col[^>]*>", row)
        self.assertEqual(len(cols), 2, "主栅格应该正好两栏，实际 %d 个：%s" % (len(cols), cols))
        self.assertIn(':md="16"', cols[0])
        self.assertIn(':md="8"', cols[1])

    def test_main_row_columns_are_closed(self):
        row = self._main_row()
        self.assertEqual(len(re.findall(r"<el-col", row)),
                         len(re.findall(r"</el-col>", row)),
                         "主栅格里有 el-col 没闭合 —— 右栏会被嵌进左栏")

    def test_right_column_is_a_sibling_not_a_child(self):
        # 左栏的 </el-col> 必须出现在右栏的 <el-col> 之前
        row = self._main_row()
        right = row.index(':md="8"')
        closes_before_right = len(re.findall(r"</el-col>", row[:right]))
        self.assertGreaterEqual(
            closes_before_right, 1,
            "右栏 <el-col :md=\"8\"> 前面没有 </el-col>，说明它被嵌在左栏里了")

    def test_key_panels_live_in_the_expected_column(self):
        row = self._main_row()
        split = row.index(':md="8"')
        left, right = row[:split], row[split:]
        for title in ("持仓详情", "资产曲线", "买卖流水"):
            self.assertIn(title, left, "%s 应该在左栏" % title)
        for title in ("交易日历", "持仓分布"):
            self.assertIn(title, right, "%s 应该在右栏" % title)


class RemovedFeatureTests(unittest.TestCase):
    """stock_market_data 选股信号那套已整体删除，模板里不该再有引用。"""

    def test_no_signal_feed_bindings_remain(self):
        for name in ("marketDataRecords", "marketBuyStock", "loadMarketDataToday",
                     "showMarketBuyDialog"):
            self.assertNotIn(name, HTML, "模板里还留着已删除的 %s" % name)


if __name__ == "__main__":
    unittest.main()
