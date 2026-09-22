import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from swarm_scraper.inventory import filter_records, load_records
from tests.helpers import INVENTORY


class RealInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_records(INVENTORY)

    def test_loads_all_rows(self):
        self.assertGreater(len(self.records), 300)
        self.assertTrue(all(r.doc_id.startswith("D") for r in self.records))

    def test_types(self):
        r = self.records[0]
        self.assertIsInstance(r.priority, int)
        self.assertTrue(r.url)

    def test_top3_filter(self):
        top = filter_records(self.records, top3_only=True)
        self.assertTrue(top)
        self.assertTrue(all(r.top3_rank in (1, 2, 3) for r in top))

    def test_tech_filter_is_case_insensitive_substring(self):
        px4 = filter_records(self.records, technologies=["px4 autopilot"])
        self.assertTrue(px4)
        self.assertTrue(all("PX4 Autopilot" in r.technology for r in px4))

    def test_priority_and_confirmed_filters(self):
        core = filter_records(self.records, max_priority=1, confirmed_only=True)
        self.assertTrue(all(r.priority == 1 and r.is_confirmed for r in core))

    def test_every_method_has_a_handler(self):
        from swarm_scraper.dispatch import HANDLERS, MANUAL
        methods = {r.method for r in self.records}
        self.assertEqual(methods - set(HANDLERS) - {MANUAL}, set())


class SyntheticInventoryTests(unittest.TestCase):
    def test_missing_column_is_reported(self):
        wb = Workbook()
        ws = wb.active
        ws.title = "Documents"
        ws.append(["Doc ID", "Technology"])
        path = Path(tempfile.mkdtemp()) / "bad.xlsx"
        wb.save(path)
        with self.assertRaises(ValueError):
            load_records(path)

    def test_blank_rows_skipped_and_unknown_columns_kept(self):
        wb = Workbook()
        ws = wb.active
        ws.title = "Documents"
        ws.append(["Doc ID", "Technology", "URL", "Scrape method", "Priority", "Extra col"])
        ws.append(["D1", "T", "https://x.org", "Web page to markdown", "2", "hello"])
        ws.append([None, None, None, None, None, None])
        path = Path(tempfile.mkdtemp()) / "ok.xlsx"
        wb.save(path)
        recs = load_records(path)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].priority, 2)
        self.assertEqual(recs[0].extra["Extra col"], "hello")


if __name__ == "__main__":
    unittest.main()
