"""Select evenly from the Steam China store's top sellers by tag."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from steam_reviews.catalog import add_appids, load_catalog
from steam_reviews.crawlers.client import build_client
from steam_reviews.settings import load_settings

TAGS = {
    '动作': 19, '冒险': 21, '角色扮演': 122, '策略': 9, '模拟': 599,
    '休闲': 597, '竞速': 699, '体育': 701, '解谜': 1664, '独立': 492,
}
WALLPAPER_ENGINE = 431960
ROW = re.compile(r'<a\s+href="https://store\.steampowered\.com/app/(\d+)/[^"\s]*[^>]*class="search_result_row\b')


def ranked_games(client, tag):
    start = 0
    while True:
        page, _ = client.get_json('https://store.steampowered.com/search/results/', params={
            'query': '', 'start': start, 'count': 100, 'filter': 'topsellers',
            'category1': 998, 'tags': tag, 'cc': 'cn', 'l': 'schinese', 'infinite': 1,
        })
        if not page.get('success') or 'results_html' not in page:
            raise RuntimeError(f'Steam 搜索响应异常，标签 {tag}')
        yield from map(int, ROW.findall(page['results_html']))
        start += 100
        if start >= int(page['total_count']):
            return


def main():
    settings = load_settings(ROOT)
    existing = set(load_catalog(ROOT, include_added=False)) | {WALLPAPER_ENGINE}
    selections = {name: [] for name in TAGS}
    client = build_client(settings)
    try:
        sources = {name: ranked_games(client, tag) for name, tag in TAGS.items()}
        for _ in range(100):
            for name, source in sources.items():
                appid = next((appid for appid in source if appid not in existing), None)
                if appid is None:
                    raise RuntimeError(f'{name}的热销列表不足以分配 100 个互不重复的游戏')
                selections[name].append(appid)
                existing.add(appid)
    finally:
        client.close()
    ids = [appid for entries in zip(*selections.values()) for appid in entries]
    add_appids(ROOT, [WALLPAPER_ENGINE, *ids])
    print(json.dumps({name: len(entries) for name, entries in selections.items()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
