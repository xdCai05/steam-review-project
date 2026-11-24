"""存储层测试：幂等、更新规则、冲突与事务。"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from steam_reviews.contracts import ReviewQuery, StatisticsQuery  # noqa: E402
from steam_reviews.analysis import statistics as stats_mod  # noqa: E402
from steam_reviews.processing.normalize import normalize_review  # noqa: E402
from steam_reviews.storage import repository  # noqa: E402
from steam_reviews.storage.database import connect, init_db, transaction  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "sample_review_raw.json"
APPID = 1086940
RUN_ID = "steam_reviews_test_001"


def make_record(collected_at: int = 1795200000, appid: int = APPID) -> dict:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    record, _ = normalize_review(
        raw,
        appid=appid,
        run_id=RUN_ID,
        collected_at=collected_at,
        window_start=1794000000 - 86400,
        window_end=1794000000 + 86400,
    )
    assert record is not None
    return record


class StorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = connect(self.db_path)
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_insert_then_duplicate_is_idempotent(self) -> None:
        record = make_record()
        self.assertEqual(repository.upsert_review(self.conn, record), "inserted")
        self.assertEqual(repository.upsert_review(self.conn, record), "updated")
        self.assertEqual(repository.count_reviews(self.conn), 1)

    def test_older_batch_does_not_overwrite_newer_state(self) -> None:
        newer = make_record(collected_at=1795300000)
        newer["helpful_votes"] = 99
        repository.upsert_review(self.conn, newer)

        older = make_record(collected_at=1795100000)
        older["helpful_votes"] = 1
        self.assertEqual(repository.upsert_review(self.conn, older), "unchanged")

        row = self.conn.execute(
            "SELECT helpful_votes, first_seen_at, last_seen_at FROM reviews WHERE review_id = ?",
            (older["review_id"],),
        ).fetchone()
        self.assertEqual(row["helpful_votes"], 99, "旧批次不得覆盖新状态")
        self.assertEqual(row["first_seen_at"], 1795100000)
        self.assertEqual(row["last_seen_at"], 1795300000)

    def test_appid_conflict_detected(self) -> None:
        repository.upsert_review(self.conn, make_record(appid=APPID))
        outcome = repository.upsert_review(self.conn, make_record(appid=1245620))
        self.assertEqual(outcome, "conflict")
        self.assertEqual(repository.count_reviews(self.conn), 1)

    def test_bulk_upsert_counts(self) -> None:
        records = [make_record(), make_record()]
        records[0]["review_id"] = "A"
        records[1]["review_id"] = "B"
        counts = repository.bulk_upsert_reviews(self.conn, records)
        self.assertEqual(counts["inserted"], 2)
        counts_again = repository.bulk_upsert_reviews(self.conn, records)
        self.assertEqual(counts_again["inserted"], 0)
        self.assertEqual(repository.count_reviews(self.conn), 2)

    def test_transaction_rolls_back_on_error(self) -> None:
        record = make_record()
        with self.assertRaises(RuntimeError):
            with transaction(self.conn):
                repository.upsert_review(self.conn, record)
                raise RuntimeError("boom")
        self.assertEqual(repository.count_reviews(self.conn), 0)

    def test_select_reviews_pagination(self) -> None:
        for i in range(5):
            record = make_record()
            record["review_id"] = f"R{i}"
            record["created_at"] = 1794000000 + i
            repository.upsert_review(self.conn, record)
        total, items = repository.select_reviews(self.conn, ReviewQuery(limit=2, offset=0))
        self.assertEqual(total, 5)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["review_id"], "R4")

    def test_statistics_denominator_excludes_missing(self) -> None:
        ok = make_record()
        ok["review_id"] = "OK"
        missing = make_record()
        missing["review_id"] = "MISS"
        missing["is_recommended"] = None
        repository.bulk_upsert_reviews(self.conn, [ok, missing])

        payload = stats_mod.compute(self.conn, StatisticsQuery(appids=[APPID]))
        totals = payload["totals"]
        self.assertEqual(totals["total_reviews"], 2)
        self.assertEqual(totals["known_recommendation"], 1)
        self.assertEqual(totals["missing_recommendation"], 1)
        self.assertEqual(totals["recommendation_rate"], 1.0)

    def test_segment_checkpoint_roundtrip(self) -> None:
        repository.ensure_run(self.conn, RUN_ID, {"appids": [APPID]})
        repository.upsert_segment(
            self.conn, run_id=RUN_ID, appid=APPID, segment_index=0,
            start_ts=1, end_ts=2, status="running", pages=3, cursor="CUR",
            fetched=300, inserted=300, updated=0, errors=0,
        )
        row = repository.get_segment(self.conn, RUN_ID, APPID, 0)
        self.assertEqual(row["pages"], 3)
        self.assertEqual(row["cursor"], "CUR")
        repository.upsert_segment(
            self.conn, run_id=RUN_ID, appid=APPID, segment_index=0,
            start_ts=1, end_ts=2, status="completed", pages=4, cursor=None,
            fetched=400, inserted=400, updated=0, errors=0, finished=True,
        )
        row = repository.get_segment(self.conn, RUN_ID, APPID, 0)
        self.assertEqual(row["status"], "completed")
        self.assertIsNotNone(row["finished_at"])

    def test_booleans_round_trip_as_bool(self) -> None:
        repository.upsert_review(self.conn, make_record())
        _, items = repository.select_reviews(self.conn, ReviewQuery(limit=1))
        self.assertIs(items[0]["is_recommended"], True)
        self.assertIs(items[0]["steam_purchase"], True)


if __name__ == "__main__":
    unittest.main()
