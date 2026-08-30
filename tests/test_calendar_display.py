import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "index_vue.html").read_text(encoding="utf-8")


class CalendarDisplayTests(unittest.TestCase):
    def test_position_ratio_is_labeled_and_hidden_in_rate_mode(self):
        self.assertNotIn(
            'v-if="day.positionRatio > 0">{{ day.positionRatio.toFixed(1) }}%',
            HTML,
        )
        self.assertIn(
            'v-if="calendarViewType === \'amount\' && day.positionRatio > 0"',
            HTML,
        )
        self.assertIn("仓{{ day.positionRatio.toFixed(1) }}%", HTML)


if __name__ == "__main__":
    unittest.main()
