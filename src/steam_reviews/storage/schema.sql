-- Steam 评测采集数据库结构（SQLite）
-- 约定：未知为 NULL，不用 0 / false / 空字符串替代未知。

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS review_locations (
    review_id TEXT PRIMARY KEY,
    appid INTEGER NOT NULL,
    relative_path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_locations_path ON review_locations(appid, relative_path);

CREATE TABLE IF NOT EXISTS games (
    appid               INTEGER PRIMARY KEY,
    name                TEXT,
    store_url           TEXT,
    short_description   TEXT,
    developers          TEXT,            -- JSON 数组
    publishers          TEXT,            -- JSON 数组
    genres              TEXT,            -- JSON 数组，官方类型
    categories          TEXT,            -- JSON 数组，官方功能分类
    release_date_raw    TEXT,
    release_date        TEXT,            -- YYYY-MM-DD，可准确解析时
    coming_soon         INTEGER,         -- 0/1/NULL
    platforms           TEXT,            -- JSON 数组
    supported_languages TEXT,
    header_image_url    TEXT,
    cover_local_path    TEXT,
    collected_at        INTEGER NOT NULL,
    first_seen_at       INTEGER,
    last_seen_at        INTEGER
);

CREATE TABLE IF NOT EXISTS game_snapshots (
    snapshot_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    appid            INTEGER NOT NULL,
    is_free          INTEGER,
    price_initial    INTEGER,
    price_final      INTEGER,
    discount_percent INTEGER,
    currency         TEXT,
    region           TEXT NOT NULL,
    collected_at     INTEGER NOT NULL,
    crawl_run_id     TEXT NOT NULL,
    UNIQUE (appid, region, collected_at, crawl_run_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_appid_time
    ON game_snapshots (appid, collected_at);

CREATE TABLE IF NOT EXISTS review_summary_snapshots (
    summary_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    appid                   INTEGER NOT NULL,
    source                  TEXT NOT NULL,   -- api / store_page
    scope                   TEXT NOT NULL,   -- query / page_overall / page_recent / page_language
    query_params            TEXT,            -- JSON，记录口径
    total_positive          INTEGER,
    total_negative          INTEGER,
    total_reviews           INTEGER,
    positive_percent_display REAL,
    review_score_desc       TEXT,
    collected_at            INTEGER NOT NULL,
    crawl_run_id            TEXT NOT NULL,
    UNIQUE (appid, source, scope, collected_at, crawl_run_id)
);

CREATE INDEX IF NOT EXISTS idx_summary_appid_time
    ON review_summary_snapshots (appid, collected_at);

CREATE TABLE IF NOT EXISTS reviews (
    review_id                TEXT PRIMARY KEY,
    appid                    INTEGER NOT NULL,
    author_steamid           TEXT,
    language                 TEXT,
    content                  TEXT,
    is_recommended           INTEGER,
    created_at               INTEGER NOT NULL,
    updated_at               INTEGER,
    playtime_at_review       INTEGER,
    playtime_forever         INTEGER,
    playtime_last_two_weeks  INTEGER,
    last_played              INTEGER,
    helpful_votes            INTEGER,
    steam_purchase           INTEGER,
    received_for_free        INTEGER,
    early_access             INTEGER,
    refunded                 INTEGER,
    developer_response       TEXT,
    timestamp_dev_responded  INTEGER,
    comment_count            INTEGER,
    votes_funny              INTEGER,
    weighted_vote_score      REAL,
    cpu_name                 TEXT,
    adapter_description      TEXT,
    system_ram               INTEGER,
    vram_size                INTEGER,
    collected_at             INTEGER NOT NULL,
    crawl_run_id             TEXT NOT NULL,
    first_seen_at            INTEGER NOT NULL,
    last_seen_at             INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reviews_appid_created
    ON reviews (appid, created_at);

CREATE INDEX IF NOT EXISTS idx_reviews_author
    ON reviews (author_steamid);

CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id      TEXT PRIMARY KEY,
    status      TEXT NOT NULL,      -- running / completed / partial / failed / paused
    params      TEXT NOT NULL,      -- JSON
    note        TEXT,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER,
    inserted    INTEGER NOT NULL DEFAULT 0,
    updated     INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    review_bytes INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT
);

CREATE TABLE IF NOT EXISTS crawl_segments (
    run_id           TEXT NOT NULL,
    appid            INTEGER NOT NULL,
    segment_index    INTEGER NOT NULL,
    start_ts         INTEGER NOT NULL,
    end_ts           INTEGER NOT NULL,
    status           TEXT NOT NULL,    -- pending / running / completed / partial / failed / paused
    pages            INTEGER NOT NULL DEFAULT 0,
    cursor           TEXT,
    fetched          INTEGER NOT NULL DEFAULT 0,
    inserted         INTEGER NOT NULL DEFAULT 0,
    updated          INTEGER NOT NULL DEFAULT 0,
    errors           INTEGER NOT NULL DEFAULT 0,
    review_bytes     INTEGER NOT NULL DEFAULT 0,
    stop_reason      TEXT,
    param_fingerprint TEXT,
    updated_at       INTEGER NOT NULL,
    finished_at      INTEGER,
    PRIMARY KEY (run_id, appid, segment_index)
);

CREATE TABLE IF NOT EXISTS import_runs (
    import_id   TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    status      TEXT NOT NULL,      -- running / completed / partial / failed
    total       INTEGER NOT NULL DEFAULT 0,
    inserted    INTEGER NOT NULL DEFAULT 0,
    updated     INTEGER NOT NULL DEFAULT 0,
    unchanged   INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER
);
