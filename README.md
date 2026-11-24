# Steam 游戏评测采集项目

这是一个数据采集课程项目，用于采集 Steam 商店资料、封面和简体中文公开评测，并在本地网页中浏览与分析数据。

## 功能

- 按游戏采集商店资料和评测，支持任务队列、暂停和断点续跑。
- 浏览和筛选评测，查看词云、评测数量和推荐比例。
- 对比多款游戏的评测趋势、游玩时长与商店资料。
- 支持日间与夜间主题，适配桌面和移动端。

## 快速开始

需要 Python 3.10 或更高版本。以下命令在 Windows PowerShell 中执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe scripts\init_db.py
.\.venv\Scripts\python.exe scripts\serve_frontend.py
```

打开 http://127.0.0.1:8765/ 。在页面的「评测采集」窗口中启动采集后端，选择候选游戏并创建任务。也可以在另一个终端运行 `python scripts/review_worker.py` 启动采集后端。

仓库包含代码、配置和测试。采集的游戏资料、评测数据、SQLite 数据库及日志由运行过程在本地生成，首次运行需要联网采集数据。先执行 `python scripts/collect.py store --missing` 可准备候选游戏的商店资料。

## 数据口径

原有五款保留在 `config/games.json`。新增目录 `config/appids.json` 只存 AppID。中国区 Steam 热销榜按动作、冒险、角色扮演、策略、模拟、休闲、竞速、体育、解谜、独立十个标签各取 100 款，跨标签去重并顺延补足。Wallpaper Engine（431960）是另加的软件。

新评测任务以启动时的北京时间为截止时刻，向前滚动 90 天，区间为 `[开始, 截止)`。首次运行冻结时间窗，续跑沿用。新发行游戏采集窗口内已有的评测，空分段正常结束。最新月份优先，月份内按最新发表分页。包含全部推荐状态和购买来源，保留题外活动。

每款游戏全部正式评测JSONL合计默认100,000,000字节（100 MB），允许覆盖；跨月份、跨任务累计。不含封面、商店资料、SQLite、日志与导出。默认另保留5 GB磁盘余量，不限制运行时长。不设最低条数。

## 安装与使用

Python 3.10以上；生产依赖requests，配置用标准库JSON，测试用unittest。

### 本地前端

在项目根目录运行 `python scripts/serve_frontend.py`，再打开 <http://127.0.0.1:8765/>。
「评测采集」窗口右上角的开关用于启动和安全关闭采集后端。关闭时，采集进程等待当前请求处理完毕并保存检查点，再退出。重新启动后继续处理队列。
也可在终端单独运行 `python scripts/review_worker.py`。页面能控制使用同一项目和数据库的新版采集进程。
控制接口为 `GET /api/review-worker`（查询状态）和 `POST /api/review-worker`（JSON `{"action":"start"}` 或 `{"action":"stop"}`）。进程启动日志位于 `logs/review-worker-service.log`。
加号卡片从已准备好商店资料的候选池选择游戏，确认后创建一条评测任务。
总览初始包含原来的五款游戏和 Wallpaper Engine，采集完成后自动加入新游戏。
触达容量上限且窗口内已有评测时，可从任务记录手动加入。
游戏对比的 HTTP 接口、统计口径与 Python 调用方式见[游戏对比查询](docs/game-comparison.md)。

同一 AppID 的活跃任务会复用，队列按提交顺序运行，每次只执行一款。
暂停在当前页安全保存后生效，暂停中的任务保留执行位，继续时使用同一检查点。
关闭浏览器或前端服务后，独立 worker 继续执行。worker 重新启动时会接回中断任务。
保留原有商店采集的全局文件写锁，两个任务同时请求写入时，队列会等待。

任务和总览成员保存在 `data/database/steam_reviews.jobs.sqlite`，详细日志保存到各任务目录。
网页显示最近 60 条已结束的任务，活跃任务始终可见。已有采集命令仍可单独使用。

```powershell
python -m pip install -e .
python scripts/init_db.py
python scripts/collect.py verify --appid 1086940 --segment 3
python scripts/collect.py store --missing
python scripts/collect.py reviews --appid 1086940
python scripts/collect.py reviews --run-id 任务ID
python scripts/collect.py reviews --appid 4115450 --max-review-bytes-per-game 150000000
python scripts/collect.py reviews --appid 4115450 --appid-budget 4115450=150000000
python scripts/collect.py status --run-id 任务ID
python scripts/import_data.py --appid 4115450
python scripts/export_data.py --help
python -m unittest discover -s tests -v
```

`store --missing` 可重新扫描尚未写入商店资料的 AppID。评测任务需用 `--appid` 指定对象，或用 `--all` 显式选择整个目录。续跑默认沿用冻结参数，显式额度覆盖写入变更记录。

## 目录

```text
config/games.json, appids.json, collection.json
src/steam_reviews/
  settings.py               # 配置与路径
  collection_policy.py      # 容量默认值、覆盖、校验
  contracts.py              # 28字段与数据类型
  crawlers/                 # HTTP、评测、商店、封面
  processing/               # 归一化与校验
  storage/                  # 数据库、事务、文件提交日志、写锁
  services/                 # 任务编排、容量、查询
  analysis/                 # 统计与导出
scripts/                    # 命令行入口
tests/                      # 离线测试与固定样例
notebooks/                  # 数据分析示例
data/
  games/
    4115450_影之刃零/
      info.json
      header.jpg
      cover.json            # 封面来源与校验
      snapshots.jsonl
      reviews/
        2026-11/
          part_00001.jsonl
          part_00002.jsonl
  tasks/任务ID/
    manifest.json
    checkpoint.json
    errors.jsonl            # 有异常时生成
    summary.json
    pending_page.json       # 仅提交未完成时存在，恢复后删除
  database/steam_reviews.db
  samples/                  # 少量验证响应
  exports/
logs/
docs/
```

游戏以AppID定位，名称只便于阅读。月份来自发表时间的北京时间，不是采集时间。相同评测重跑时更新原分片，不复制一套批次数据。任务目录仅保存执行记录。

## 处理流程

读取配置、获得进程写锁 → 校验冻结参数与恢复待提交页 → 重算游戏评测字节 → 按月份分页请求 → 映射28字段并校验 → 为已有ID更新原分片、新ID分配月分片 → 检查空间增量 → 写提交日志 → 原子替换文件、事务更新数据库与检查点 → 删除提交日志 → 输出摘要。

容量不足时整页拒绝，不写入、不推进游标；该游戏partial/capacity_limit，继续其他游戏。合法空页才表示完成；错误响应和无效游标不能当作结束。

在 `data/tasks/任务ID/STOP` 创建空文件可于下一页边界暂停，恢复前移除。网络超时或退避等待结束后才检查。进程退出后操作系统自动释放写锁。

## 分析边界

- 28字段见docs/data_dictionary.md；缺失为null，正文保留原文。
- 当前时长、近两周时长、退款和开发者回复是采集时观察值，不能倒推发表时状态。
- 因容量停止的数据偏向近期，不是随机样本，不能代表完整90天；展示实际保存日期与完整性。
- 100 MB不是整个项目占盘上限，数据库、任务、旧导出另占空间。
- 状态查询使用实际评测日期；价格、商店汇总与中文样本不能混用分母。

## 维护记录

2026-11-22：正式数据迁移为游戏/月目录并逐条核对；任务记录单独保留。修复冻结参数、异常响应、提交中断恢复、覆盖优先级与覆盖日期。配置改为JSON，移除自制YAML解析器和pytest配置。未启动采集。
