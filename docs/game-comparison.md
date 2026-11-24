# 游戏对比查询

`GET /api/games/compare?game=4115450&game=1245620` 返回 2–10 款游戏的结构化对比数据。可选 AppID 从 `GET /api/games` 获取，结果保留输入顺序。数量不符、AppID 无效或重复时返回 HTTP `400` 和 `{"error":"…"}`。

响应包含 `timezone`（`Asia/Shanghai`）、`observed_at`（本次查询的 Unix 秒）和 `games`（游戏数组）。`games` 中每项复用游戏详情字段，并附加 `recommendation_rate`、`playtime_known`。采集期间，查询在同一 SQLite 读取快照内获取各款游戏。

| 字段 | 口径 |
| --- | --- |
| `appid`, `name`, `has_cover` | 游戏标识、显示名称和封面可用性。 |
| `total`, `rated`, `positive` | 本地入库评测数、推荐状态已知的评测数、推荐评测数。 |
| `recommendation_rate` | `positive / rated`，取值为 0–1。`rated` 为 0 时为 `null`。 |
| `first`, `last`, `status` | 入库评测的最早与最晚发表时间（Unix 秒），以及最近一次采集状态。评测覆盖范围不代表区间内已完成采集。 |
| `daily` | 按北京时间和发表日升序排列的 `{day, total, rated, positive}`。无入库评测的日期省略，前端对齐多个序列时可使用空值保留缺口。 |
| `playtime` | 按发表评测时的游玩分钟数统计的 `{bucket, total, rated, positive}`。`0` 表示不足 2 小时，`1` 表示 2 至不足 10 小时，`2` 表示 10 小时及以上。 |
| `playtime_known` | 已知发表时长的评测总数。各时长组的样本占比以该字段为分母，缺失时长数为 `total - playtime_known`。 |
| `developers`, `publishers`, `genres`, `platforms`, `release_date`, `release_date_raw` | 商店资料和原始发行日期（用于标准化日期缺失时）。 |
| `price` | 最新价格快照 `{price_final, currency, region, collected_at, is_free}`，缺失时为 `null`。`price_final` 为 Steam 返回的价格整数，页面用 `price_final / 100` 展示。地区、币种和采集时间随结果提供。 |
| `store_rating` | Steam 简体中文商店评价的最近快照，独立于本地评测样本统计。 |

后续 Agent 可直接调用 HTTP 接口，也可在 Python 中复用查询。传入设置了 `sqlite3.Row` 的连接和可对比的游戏目录，函数返回与 HTTP 接口相同的字典。

```python
from steam_reviews.services import compare_games

result = compare_games(conn, [4115450, 1245620], overview_catalog)
```
