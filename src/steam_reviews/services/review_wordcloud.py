"""Build and persist the latest document-frequency result for each review scope."""

import json
import logging
import re
import sqlite3
import time
import unicodedata
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from functools import lru_cache
from threading import Lock

from steam_reviews.contracts import ConfigError

STOPWORDS = set("""
游戏 玩游戏 这个 那个 这些 那些 一个 一些 什么 怎么 这么 那么 这样 那样 真的 感觉
觉得 可以 还是 不是 就是 比较 非常 真的 实在 确实 但是 然后 因为 所以 如果 而且
不过 其实 只是 可能 应该 需要 没有 不会 不能 不想 大家 自己 他们 我们 你们
时候 之后 之前 现在 一直 一样 多少 很多 这么多 这款 这作 玩过 入手 完全
推荐 好玩 不错 好评 差评 体验 小时 最后 第一 直接 总体 简直 基本 一下
无需 即使 早已 还有 知道 喜欢 尔登 这样子
the and for with this that you are not was have from but just http https www
""".split())
TERMS = """
黄金树幽影 黄金树 开放世界 大世界 魂系 法环 艾尔登法环 影之地 女武神 碎星拉塔恩
玛莲妮亚 米凯拉 褪色者 大树守卫 黑暗之魂 只狼 黑神话 影之刃零 帧率 打击感 魂类
键鼠 手柄 优化 掉帧 联机 读指令 空气墙 新手引导 数值设计 战斗系统 支线任务
""".split()
MARKUP = re.compile(r'\[/?[a-z][a-z0-9]*(?:=[^\]\r\n]*)?\]|https?://\S+', re.I)
WORD = re.compile(r'[\u3400-\u9fff]{2,12}|[a-z][a-z0-9]{1,19}')
SCOPE_VERSION = 2


def scope_key(version, clause, values):
    return json.dumps([version, clause, values], separators=(',', ':'))


@lru_cache(maxsize=1)
def tokenizer():
    import rjieba

    segmenter = rjieba.Jieba()
    for term in TERMS:
        segmenter.add_word(term, 100000, 'n')
    return segmenter


def summarize(rows, game_name=''):
    freq = Counter()
    first = last = None
    size = 0
    excluded = STOPWORDS | {game_name.casefold(), '艾尔登法环', 'elden', 'ring'}
    segmenter = tokenizer()
    for created_at, content in rows:
        text = MARKUP.sub(' ', unicodedata.normalize('NFKC', content)).casefold().strip()
        if not text:
            continue
        size += 1
        first = created_at if first is None else min(first, created_at)
        last = created_at if last is None else max(last, created_at)
        terms = {term for term in segmenter.cut(text, False)
                 if WORD.fullmatch(term) and term not in excluded}
        freq.update(terms)
    return {
        'first': first, 'last': last, 'reviews': size, 'generated_at': int(time.time()),
        'words': [{'word': word, 'reviews': hits} for word, hits in
                  sorted(freq.items(), key=lambda item: (-item[1], item[0]))[:140] if hits >= 2],
    }


class WordClouds:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='word-cloud')
        self.lock = Lock()
        self.pending = set()
        self.errors = OrderedDict()

    @staticmethod
    def connect(database):
        db = sqlite3.connect(database.with_suffix('.wordcloud.sqlite'))
        db.execute('''CREATE TABLE IF NOT EXISTS wordcloud (
            scope TEXT PRIMARY KEY, payload TEXT NOT NULL, saved INTEGER NOT NULL)''')
        return db

    def get(self, database, clause, values):
        scope = scope_key(SCOPE_VERSION, clause, values)
        with self.lock, closing(self.connect(database)) as cache:
            row = cache.execute('SELECT payload FROM wordcloud WHERE scope=?', (scope,)).fetchone()
            needs_refresh = row is None
            if needs_refresh:
                row = cache.execute('SELECT payload FROM wordcloud WHERE scope=?',
                                    (scope_key(1, clause, values),)).fetchone()
            key = (database, scope)
            return {'running': key in self.pending, 'error': self.errors.get(key),
                    'needs_refresh': needs_refresh and row is not None,
                    'result': json.loads(row[0]) if row else None}

    def start(self, database, clause, values, game_name):
        scope = scope_key(SCOPE_VERSION, clause, values)
        key = (database, scope)
        with self.lock:
            if key in self.pending:
                return
            if len(self.pending) >= 6:
                raise ConfigError('词云任务较多，请稍后重试')
            self.errors.pop(key, None)
            self.pending.add(key)
            self.pool.submit(self._build, key, clause, values, game_name)

    def _build(self, key, clause, values, game_name):
        database, scope = key
        try:
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as reviews:
                result = summarize(reviews.execute(
                    'SELECT created_at, content FROM reviews' + clause +
                    ' AND content IS NOT NULL AND content != ?', [*values, '']), game_name)
            with self.lock, closing(self.connect(database)) as cache, cache:
                cache.execute('INSERT OR REPLACE INTO wordcloud VALUES (?, ?, ?)',
                              (scope, json.dumps(result, ensure_ascii=False), time.time_ns()))
                cache.execute('DELETE FROM wordcloud WHERE scope IN '
                              '(SELECT scope FROM wordcloud ORDER BY saved DESC LIMIT -1 OFFSET 128)')
        except Exception:
            logging.exception('Unable to build review word cloud')
            with self.lock:
                self.errors[key] = '词云生成失败，请重试'
                if len(self.errors) > 32:
                    self.errors.popitem(last=False)
        finally:
            with self.lock:
                self.pending.discard(key)


wordclouds = WordClouds()
