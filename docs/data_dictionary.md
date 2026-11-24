# 数据字典

本文件是字段的唯一权威说明。任何字段语义变更都需在此记录版本。

字段版本：`reviews-28-v1`

---

## 1. 单条评测 `reviews`（28 个固定键）

落盘为 UTF-8 JSONL，每行恰好 28 个键，顺序即下表顺序。

| 键 | 来源（接口字段） | 类型 | 单位 / 含义 | 缺失处理 |
|---|---|---|---|---|
| `review_id` | `recommendationid` | string | 评测唯一 ID，主键 | 缺失或不合法 → 异常记录，不入正式表 |
| `appid` | 请求参数 | int | 游戏 ID，必须在固定清单内 | 必填 |
| `author_steamid` | `author.steamid` | string \| null | SteamID64，保留字符串避免精度丢失 | null，不访问个人主页 |
| `language` | `language` | string | 必须是 `schinese` | 非 schinese → 异常记录，不入表 |
| `content` | `review` | string \| null | 评测正文，保留原文、换行与表情 | 缺失 → null；空串保留且记异常 |
| `is_recommended` | `voted_up` | bool \| null | 推荐 / 不推荐 | 缺失 → null，且不计入推荐比例分母 |
| `created_at` | `timestamp_created` | int | UTC 秒，发表时间 | 缺失/越出窗口 → 异常记录，不入表 |
| `updated_at` | `timestamp_updated` | int \| null | UTC 秒，最后修改时间 | null，0 视为 null |
| `playtime_at_review` | `author.playtime_at_review` | int \| null | 分钟，**发表评测时**的游玩时长 | null，不用当前累计时长顶替 |
| `playtime_forever` | `author.playtime_forever` | int \| null | 分钟，采集时该作者在本作的累计时长 | null，不是账号全部游戏时长 |
| `playtime_last_two_weeks` | `author.playtime_last_two_weeks` | int \| null | 分钟，采集时近两周在本作的时长 | null，不代表评测前两周 |
| `last_played` | `author.last_played` | int \| null | UTC 秒，最近游玩时间 | 0 或缺失 → null |
| `helpful_votes` | `votes_up` | int \| null | 有用票数 | null，负数视为 null |
| `steam_purchase` | `steam_purchase` | bool \| null | 是否 Steam 购买 | null |
| `received_for_free` | `received_for_free` | bool \| null | 是否声明免费获得 | null |
| `early_access` | `written_during_early_access` | bool \| null | 是否写于抢先体验期间 | null |
| `refunded` | `refunded` | bool \| null | 采集时的退款状态 | null，不推断退款原因 |
| `developer_response` | `developer_response` | string \| null | 开发者回复原文 | 未返回或空 → null |
| `timestamp_dev_responded` | `timestamp_dev_responded` | int \| null | UTC 秒，开发者回复时间 | 0 或缺失 → null |
| `comment_count` | `comment_count` | int \| null | 该评测下二级回复数量 | null，不含回复正文 |
| `votes_funny` | `votes_funny` | int \| null | 欢乐票数 | null |
| `weighted_vote_score` | `weighted_vote_score` | float \| null | 有用度加权分，有限数值 | 非有限值 → null；不是可信度概率 |
| `cpu_name` | `hardware.cpu_name` | string \| null | CPU 名称 | null |
| `adapter_description` | `hardware.adapter_description` | string \| null | GPU 名称/描述 | null |
| `system_ram` | `hardware.system_ram` | int \| null | MB，内存容量 | null，不从商店最低配置填补 |
| `vram_size` | `hardware.vram_size` | int \| null | MB，显存容量 | null，同上 |
| `collected_at` | 程序生成 | int | UTC 秒，本条记录获取时间 | 必填 |
| `crawl_run_id` | 程序生成 | string | 采集任务 ID | 必填 |

### 时间口径

- `created_at` / `updated_at` / `timestamp_dev_responded` / `last_played` 均为 **UTC 秒**
- 展示时转北京时间；入库不存本地时间
- `playtime_at_review` 是**历史快照**；`playtime_forever`、`playtime_last_two_weeks`、
  `last_played` 是**采集时状态**，可能晚于研究窗口
- 不得用采集时状态倒推历史，也不得据此断言玩家已弃游

### 明确不采集

昵称、头像、签名、作者公开评测总数、`primarily_steam_deck`、Deck 游玩时长、
`hardware.os`、二级回复正文、个人主页、视频、截图、DLC。

---

## 2. 游戏资料 `games`

| 列 | 类型 | 说明 |
|---|---|---|
| `appid` | int PK | 游戏 ID |
| `name` | text | 商店名称 |
| `store_url` | text | 商店链接 |
| `short_description` | text | 简短介绍（不采完整长文） |
| `developers` / `publishers` | text (JSON 数组) | 开发商 / 发行商 |
| `genres` / `categories` | text (JSON 数组) | 官方类型 / 功能分类，与玩家标签区分 |
| `release_date_raw` | text | 日期原文 |
| `release_date` | text | `YYYY-MM-DD`，可准确解析才填 |
| `coming_soon` | int(0/1) \| null | 是否尚未发售 |
| `platforms` | text (JSON 数组) | windows / mac / linux |
| `supported_languages` | text | 原始语言支持串（含界面/语音/字幕标记） |
| `header_image_url` | text | 头图地址 |
| `cover_local_path` | text | 本地封面路径，下载失败为 null |
| `collected_at` | int | 资料最近采集时间 |
| `first_seen_at` / `last_seen_at` | int | 最早 / 最晚一次采集 |

---

## 3. 价格快照 `game_snapshots`

`snapshot_id, appid, is_free, price_initial, price_final, discount_percent,
currency, region, collected_at, crawl_run_id`

- 只取**本体标准购买项**，不含豪华版、DLC、合集、订阅
- 价格用**最小货币单位整数**（人民币分）
- 免费 / 未发售 / 无法购买 / 获取失败分别记录，未知价格为 `NULL`
- 每次正式采集**追加**一行，不覆盖历史；不声称拥有采集前的历史价格

---

## 4. 评价汇总快照 `review_summary_snapshots`

`summary_id, appid, source, scope, query_params, total_positive, total_negative,
total_reviews, positive_percent_display, review_score_desc, collected_at, crawl_run_id`

| 列 | 说明 |
|---|---|
| `source` | `api`（评测接口）/ `store_page`（商店页） |
| `scope` | `query`（按当前筛选条件）/ `page_overall` / `page_recent` / `page_language` |
| `query_params` | JSON，记录口径，便于复现 |
| `positive_percent_display` | 商店页给出的**整数百分比**，不得当作精确计数 |

三种口径分行存储，**不得混用**：商店整体评价 ≠ 页面近期评价 ≠ 90 天中文采集样本。

---

## 5. 任务与进度

| 表 | 用途 |
|---|---|
| `crawl_runs` | 采集任务：状态、参数、起止时间、累计计数 |
| `crawl_segments` | 每游戏 × 每时间段的边界、游标、页数与完成状态 |
| `import_runs` | 文件导入任务：来源、状态、总数与去重计数 |

状态取值：`pending / running / completed / partial / failed / paused`。

---

## 6. 空值与比例口径

- 未知一律 `NULL`，**不用** `0`、`false`、空字符串替代
- 缺失正文、推荐状态或可选字段**不静默丢弃**，写入 `errors.jsonl`
- 推荐比例分母**只包含 `is_recommended` 非 NULL 的记录**，并同时展示缺失数量
