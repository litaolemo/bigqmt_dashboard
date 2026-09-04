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
JS = (ROOT / "vue-app.js").read_text(encoding="utf-8")


def _slice_between(text, start_marker, end_marker):
    """从 start_marker 起到下一个 end_marker 为止的那段，用来把断言限定在一个弹窗内。"""
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]

# 需要成对出现的容器型标签（自闭合写法也算数）
PAIRED_TAGS = (
    "el-row", "el-col", "el-dialog", "el-table", "el-table-column",
    "el-tabs", "el-tab-pane", "el-select", "el-radio-group", "template",
    "el-option-group",
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


class OrderPricingBlockTests(unittest.TestCase):
    """三个手动下单弹窗共用一整套报价块，少一个就有一条下单路径绕过价格笼子。

    条件单弹窗也报单，但它的报价块是**故意**残缺的（见下面那个测试类）：
    所以这里的三个手动弹窗和四个报单弹窗要分开数，别混成一个数字。
    """

    MANUAL_DIALOGS = 3      # 卖出持仓 / 买入持仓 / 买入新股票
    ORDER_DIALOGS = 4       # 上面三个 + 条件单

    def test_every_order_dialog_has_a_price_type_selector(self):
        self.assertEqual(HTML.count('v-model="orderPriceType"'), self.ORDER_DIALOGS)

    def test_only_the_manual_dialogs_take_a_limit_price(self):
        self.assertEqual(HTML.count('v-model="orderLimitPrice"'), self.MANUAL_DIALOGS)
        self.assertEqual(HTML.count("{{ orderPriceError }}"), self.MANUAL_DIALOGS)

    def test_price_types_are_rendered_grouped_not_as_a_radio_row(self):
        # 二十来个选价类型排成一行 radio 是看不了的
        self.assertNotIn("orderPriceTypes", HTML, "还在平铺全部选项")
        self.assertEqual(HTML.count('v-for="g in orderPriceTypeGroups"'), self.MANUAL_DIALOGS)

    def test_price_input_follows_the_price_role(self):
        # protect 类型也要能填保护限价，所以是 accepts 不是 needs
        self.assertEqual(HTML.count('v-if="orderAcceptsPrice"'), self.MANUAL_DIALOGS)
        self.assertEqual(HTML.count("{{ orderPriceLabel }}"), self.MANUAL_DIALOGS)
        # 笼子只约束限价单，市价指令不该显示它
        self.assertEqual(HTML.count('v-if="orderNeedsPrice"'), self.MANUAL_DIALOGS)

    def test_trade_mode_selector_is_hidden_when_there_is_nothing_to_choose(self):
        # 普通账户只有「普通买卖」一条，不要占一行界面
        self.assertEqual(HTML.count('v-if="orderTradeModeVisible"'), self.ORDER_DIALOGS)
        self.assertEqual(HTML.count('v-model="orderTradeMode"'), self.ORDER_DIALOGS)

    def test_trade_mode_label_comes_from_the_side_aware_list(self):
        # 列表里的 side_label 是前端按买/卖算出来的：
        # 卖出弹窗上写「担保品买入」会让人下错单
        self.assertEqual(HTML.count("m.side_label"), self.ORDER_DIALOGS * 2)


class ConditionalOrderDialogTests(unittest.TestCase):
    """条件单弹窗的报价块跟手动下单**不一样**，而且必须不一样。

    两处差别都是有原因的，抄一份完整的报价块过来会把它们抹掉：

    1. 没有委托价输入框 —— conditional_orders 表只存触发价，没有存委托价的地方。
       给个输入框，用户填的数会被静默丢掉，触发时按市价报出去。
    2. 报价方式过滤掉限价类 —— 服务端 store.create 会直接拒，让人选完再被拒很烦。
    """

    def test_the_dialog_has_no_limit_price_input(self):
        dialog = _slice_between(HTML, 'v-model="showConditionalDialog"', "</el-dialog>")
        self.assertNotIn("orderLimitPrice", dialog,
                         "条件单存不下委托价，给了输入框等于骗用户")

    def test_the_price_types_are_the_filtered_list(self):
        dialog = _slice_between(HTML, 'v-model="showConditionalDialog"', "</el-dialog>")
        self.assertIn("conditionalPriceTypeGroups", dialog)
        self.assertNotIn("orderPriceTypeGroups", dialog,
                         "用了未过滤的列表，限价类会出现在选项里")

    def test_the_plan_is_shown_before_submitting(self):
        # 「设一次出两张单」不该是提交之后才发现的事
        self.assertIn("conditionalPlanText", HTML)

    def test_positions_have_a_conditional_order_button(self):
        self.assertIn("openConditionalDialog(row)", HTML)
        self.assertIn("conditional_action", JS)

    def test_conditional_orders_are_listed_with_a_cancel_button(self):
        self.assertIn("conditionalOrders", HTML)
        self.assertIn("cancelConditionalOrder(", HTML)


class InDomTemplateTrapTests(unittest.TestCase):
    """两个只有在浏览器里才炸、而且报错完全不指向现场的坑。

    模板是 in-DOM 的（Vue 直接编译 index_vue.html 的 DOM），不是 SFC，
    所以有些在 .vue 文件里天经地义的写法在这里是错的。两个都真踩过：

    1. `<el-input-number ... />` 自闭合在 HTML 里不成立 —— 浏览器把它当开标签，
       后面的兄弟节点全变成它的子节点。紧跟着的 `v-else` 于是找不到相邻的
       `v-if`，生产版 Vue 直接抛编译错误，整个面板白屏，控制台只有一句
       `Uncaught SyntaxError: 30`（30 是压掉的错误码），不指向任何文件行号。
    2. 这版 Element Plus（2.3.14）的 `el-radio-button` 取值走 `label`，不是
       `value`。写成 `value` 不报任何错，只是点了以后 v-model 被设成空串 ——
       界面上按钮还高亮着，数据却是空的。
    """

    SELF_CLOSING = re.compile(r"<(el-[a-z-]+)\b[^>]*/>")

    def test_no_self_closing_tag_is_followed_by_a_v_else(self):
        # 只有「紧接着就是另一个开标签」才会被吞。中间隔着闭标签的说明这个自闭合
        # 元素已经被父元素收掉了，那种写法虽然不规范但不会咬人，不在这里管。
        offenders = []
        for match in self.SELF_CLOSING.finditer(HTML):
            rest = HTML[match.end():]
            next_tag = re.search(r"<(/?)([a-zA-Z][^\s/>]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>",
                                 rest, re.S)
            if not next_tag or next_tag.group(1) == "/":
                continue          # 下一个是闭标签 —— 安全
            if re.search(r"\sv-else(-if)?[=\s>]", next_tag.group(3) + ">"):
                line = HTML.count("\n", 0, match.start()) + 1
                offenders.append("第 %d 行 <%s ... /> 自闭合，紧跟着的 <%s> 带 v-else，"
                                 "会被吞成它的子节点"
                                 % (line, match.group(1), next_tag.group(2)))
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_radio_buttons_use_label_not_value(self):
        # 全仓库统一用 label；出现 value 就是照着新版文档写的
        self.assertEqual(
            re.findall(r"<el-radio-button[^>]*\svalue=", HTML), [],
            "el-radio-button 用了 value=，这版 Element Plus 只认 label=")


class PendingOrderPanelTests(unittest.TestCase):
    def test_pending_orders_are_listed_with_a_cancel_button(self):
        self.assertIn("未成交委托", HTML)
        self.assertIn("tradeFlowPending", HTML)
        self.assertIn("cancelPendingOrder(", HTML)


class RemovedFeatureTests(unittest.TestCase):
    """stock_market_data 选股信号那套已整体删除，模板里不该再有引用。"""

    def test_no_signal_feed_bindings_remain(self):
        for name in ("marketDataRecords", "marketBuyStock", "loadMarketDataToday",
                     "showMarketBuyDialog"):
            self.assertNotIn(name, HTML, "模板里还留着已删除的 %s" % name)


if __name__ == "__main__":
    unittest.main()
