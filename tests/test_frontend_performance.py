import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "vue-app.js").read_text(encoding="utf-8")


def extract_const_arrow(name):
    pattern = re.compile(
        rf"const\s+{re.escape(name)}\s*=\s*async\s*\((?P<args>[^)]*)\)\s*=>\s*\{{"
    )
    match = pattern.search(JS)
    if not match:
        raise AssertionError(f"{name} function was not found")

    start = match.end() - 1
    depth = 0
    for idx in range(start, len(JS)):
        char = JS[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return match.group("args"), JS[start + 1:idx]
    raise AssertionError(f"{name} function body was not closed")


class FrontendUserSwitchPerformanceTests(unittest.TestCase):
    def test_user_switch_bundle_loads_independent_requests_without_aux_duplication(self):
        _, body = extract_const_arrow("loadCurrentAccountBundle")

        self.assertIn("Promise.all", body)
        self.assertIn("loadData({ loadAux: false, loadMarket: !marketLoaded })", body)
        for expected_call in (
            "loadChartData()",
            "loadTradeStats()",
            "loadTradeDates()",
            "loadTradeFactors()",
        ):
            self.assertIn(expected_call, body)
        self.assertIn("if (!boardLoaded) tasks.push(loadResearchBoard(false));", body)
        self.assertIn("const tradeFlowLoaded = tradeFlowRecords.value && tradeFlowRecords.value.length > 0;", body)
        self.assertIn("if (!tradeFlowLoaded) tasks.push(Promise.resolve(loadTradeFlow(true)).catch(() => {}));", body)

    def test_load_data_can_skip_auxiliary_work_and_fetch_market_in_parallel(self):
        args, body = extract_const_arrow("loadData")

        self.assertIn("options = {}", args)
        self.assertIn("loadAux = true", body)
        self.assertIn("loadMarket = true", body)
        self.assertIn("marketPromise", body)
        self.assertIn("dataPromise", body)
        self.assertIn("Promise.all([marketPromise, dataPromise])", body)
        self.assertIn("if (loadAux)", body)

    def test_sparkline_rendering_is_scheduled_instead_of_duplicated(self):
        _, load_data_body = extract_const_arrow("loadData")

        self.assertIn("let sparklineRenderTimer = null;", JS)
        self.assertIn("const scheduleRenderSparklines", JS)
        self.assertIn("scheduleRenderSparklines();", load_data_body)
        self.assertNotIn("renderSparklines();", load_data_body)
        self.assertRegex(
            JS,
            r"watch\(filteredPositions,\s*\(\)\s*=>\s*\{\s*scheduleRenderSparklines\(\);",
        )

    def test_t0_stats_are_indexed_instead_of_nested_position_trade_scan(self):
        _, load_data_body = extract_const_arrow("loadData")

        self.assertIn("const buildTodayTradeStatsMap", JS)
        self.assertIn("const todayTradeStatsMap = buildTodayTradeStatsMap(trades.value, todayStr);", load_data_body)
        self.assertNotRegex(
            load_data_body,
            r"newPositions\.forEach\([\s\S]*trades\.value\.forEach",
        )



    def test_delete_user_calls_permanent_account_delete_endpoint(self):
        _, body = extract_const_arrow("deleteUser")

        self.assertIn("/api/admin/delete-account", body)
        self.assertIn("account_id: user.account_id", body)
        self.assertNotIn("/api/admin/delete-user", body)

if __name__ == "__main__":
    unittest.main()
