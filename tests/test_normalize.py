"""字段映射与空值处理测试。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from steam_reviews.contracts import REVIEW_FIELD_KEYS, REVIEW_FIELD_COUNT  # noqa: E402
from steam_reviews.processing.normalize import normalize_review, to_jsonl_line  # noqa: E402
from steam_reviews.processing.validate import check_keys, check_types  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "sample_review_raw.json"
APPID = 1086940
RUN_ID = "steam_reviews_20261121_001"
WINDOW = (1794000000 - 86400, 1794000000 + 86400)


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def normalize(raw: dict, window: tuple[int, int] = WINDOW):
    return normalize_review(
        raw,
        appid=APPID,
        run_id=RUN_ID,
        collected_at=1795200000,
        window_start=window[0],
        window_end=window[1],
    )


class NormalizeTest(unittest.TestCase):
    def test_output_has_exactly_28_keys(self) -> None:
        record, errors = normalize(load_fixture())
        self.assertEqual(len(record), 28)
        self.assertEqual(REVIEW_FIELD_COUNT, 28)
        self.assertEqual(tuple(record.keys()), REVIEW_FIELD_KEYS)
        self.assertEqual(errors, [])

    def test_field_mapping_values(self) -> None:
        record, _ = normalize(load_fixture())
        self.assertEqual(record["review_id"], "1790000001")
        self.assertEqual(record["appid"], APPID)
        self.assertEqual(record["language"], "schinese")
        self.assertTrue(record["is_recommended"])
        self.assertEqual(record["created_at"], 1794000000)
        self.assertEqual(record["playtime_at_review"], 120)
        self.assertEqual(record["playtime_forever"], 360)
        self.assertEqual(record["helpful_votes"], 5)
        self.assertAlmostEqual(record["weighted_vote_score"], 0.75)
        self.assertEqual(record["crawl_run_id"], RUN_ID)

    def test_hardware_is_flattened_and_os_dropped(self) -> None:
        record, _ = normalize(load_fixture())
        self.assertEqual(record["cpu_name"], "Example CPU")
        self.assertEqual(record["adapter_description"], "Example GPU")
        self.assertEqual(record["system_ram"], 16384)
        self.assertEqual(record["vram_size"], 8192)
        self.assertNotIn("hardware", record)
        self.assertNotIn("os", record)

    def test_missing_optional_fields_become_null_not_zero(self) -> None:
        raw = load_fixture()
        for key in ("votes_up", "votes_funny", "comment_count", "weighted_vote_score", "refunded"):
            raw.pop(key, None)
        raw.pop("hardware", None)
        raw["author"].pop("playtime_forever")
        record, _ = normalize(raw)
        for key in (
            "helpful_votes",
            "votes_funny",
            "comment_count",
            "weighted_vote_score",
            "refunded",
            "cpu_name",
            "system_ram",
            "vram_size",
        ):
            self.assertIsNone(record[key], f"{key} 缺失应为 null")
        self.assertIsNone(record["playtime_forever"])

    def test_zero_timestamps_become_null(self) -> None:
        raw = load_fixture()
        raw["author"]["last_played"] = 0
        raw["timestamp_dev_responded"] = 0
        record, _ = normalize(raw)
        self.assertIsNone(record["last_played"])
        self.assertIsNone(record["timestamp_dev_responded"])

    def test_wrong_language_rejected(self) -> None:
        raw = load_fixture()
        raw["language"] = "english"
        record, errors = normalize(raw)
        self.assertIsNone(record)
        self.assertEqual([e.field for e in errors], ["language"])

    def test_timestamp_outside_window_rejected(self) -> None:
        raw = load_fixture()
        raw["timestamp_created"] = WINDOW[1] + 10
        record, errors = normalize(raw)
        self.assertIsNone(record)
        self.assertEqual(errors[0].field, "created_at")

    def test_missing_or_invalid_id_rejected(self) -> None:
        raw = load_fixture()
        raw.pop("recommendationid")
        record, errors = normalize(raw)
        self.assertIsNone(record)
        self.assertEqual(errors[0].field, "review_id")

    def test_empty_content_kept_with_error(self) -> None:
        raw = load_fixture()
        raw["review"] = ""
        record, errors = normalize(raw)
        self.assertIsNotNone(record)
        self.assertEqual(record["content"], "")
        self.assertEqual([e.field for e in errors], ["content"])

    def test_missing_content_is_null_with_error(self) -> None:
        raw = load_fixture()
        raw.pop("review")
        record, errors = normalize(raw)
        self.assertIsNone(record["content"])
        self.assertEqual(errors[0].field, "content")

    def test_jsonl_line_roundtrip(self) -> None:
        record, _ = normalize(load_fixture())
        line = to_jsonl_line(record)
        restored = json.loads(line)
        self.assertEqual(tuple(restored.keys()), REVIEW_FIELD_KEYS)
        self.assertEqual(restored["review_id"], record["review_id"])
        self.assertIn("🙂", restored["content"])

    def test_record_passes_structural_checks(self) -> None:
        record, _ = normalize(load_fixture())
        self.assertEqual(check_keys(record), [])
        self.assertEqual(check_types(record), [])


if __name__ == "__main__":
    unittest.main()
