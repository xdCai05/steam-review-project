"""Cover comparison ordering and the distinction between zero and missing data."""

import sqlite3
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from steam_reviews.services.game_queries import ComparisonError, compare_games
from steam_reviews.storage.database import init_db


def timestamp(value):
    return int(datetime.fromisoformat(value).timestamp())


class GameComparisonTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        init_db(self.db)
        self.catalog = {appid: {'appid': appid} for appid in range(10, 111, 10)}

    def test_preserves_selection_order_and_beijing_dates(self):
        selected_reviews = [
            ('a', 10, timestamp('2026-01-01T15:59:59+00:00'), 1, 119),
            ('b', 10, timestamp('2026-01-01T16:00:00+00:00'), 0, 120),
            ('c', 10, timestamp('2026-01-03T16:00:00+00:00'), None, None),
            ('d', 10, timestamp('2026-01-03T16:01:00+00:00'), None, 600),
        ]
        self.db.executemany('''INSERT INTO reviews(review_id, appid, created_at, is_recommended, playtime_at_review,
            collected_at, crawl_run_id, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, 0, 'test', 0, 0)''', selected_reviews)

        result = compare_games(self.db, [20, 10], self.catalog)
        self.assertEqual(result['timezone'], 'Asia/Shanghai')
        empty, game = result['games']
        self.assertEqual([empty['appid'], game['appid']], [20, 10])
        self.assertEqual(empty['total'], 0)
        self.assertIsNone(empty['first'])
        self.assertIsNone(empty['last'])
        self.assertIsNone(empty['recommendation_rate'])
        self.assertIsNone(empty['price'])
        self.assertEqual(empty['daily'], [])
        self.assertEqual(game['recommendation_rate'], 0.5)
        self.assertEqual(game['playtime_known'], 3)
        self.assertEqual(game['daily'], [
            {'day': '2026-01-01', 'total': 1, 'rated': 1, 'positive': 1},
            {'day': '2026-01-02', 'total': 1, 'rated': 1, 'positive': 0},
            {'day': '2026-01-04', 'total': 2, 'rated': 0, 'positive': 0},
        ])
        self.assertEqual(game['playtime'], [
            {'bucket': 0, 'total': 1, 'rated': 1, 'positive': 1},
            {'bucket': 1, 'total': 1, 'rated': 1, 'positive': 0},
            {'bucket': 2, 'total': 1, 'rated': 0, 'positive': 0},
        ])
        self.assertTrue(self.db.in_transaction)

    def test_rejects_invalid_selections(self):
        for appids in ([10], [10, 10], [10, 999], [10, '20'], list(self.catalog)):
            with self.subTest(appids=appids), self.assertRaises(ComparisonError):
                compare_games(self.db, appids, self.catalog)

    def test_accepts_ten_distinct_games_in_requested_order(self):
        appids = list(self.catalog)[1:][::-1]
        result = compare_games(self.db, appids, self.catalog)
        self.assertEqual([game['appid'] for game in result['games']], appids)
