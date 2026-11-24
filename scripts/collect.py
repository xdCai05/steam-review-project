#!/usr/bin/env python
"""采集统一入口：verify / reviews / store / status。

只做参数解析、调用服务函数、打印摘要与设置退出码；业务逻辑在 services 层。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from steam_reviews.contracts import (  # noqa: E402
    CollectionRequest,
    ConfigError,
    ReviewPageRequest,
    SteamError,
)
from steam_reviews.crawlers.client import build_client  # noqa: E402
from steam_reviews.crawlers.reviews import fetch_review_page  # noqa: E402
from steam_reviews.services.collection import (  # noqa: E402
    collect_reviews,
    collect_store,
    get_collection_status,
)
from steam_reviews.collection_policy import (  # noqa: E402
    DEFAULT_MAX_REVIEW_BYTES_PER_GAME,
    DEFAULT_MIN_FREE_DISK_BYTES,
    CollectionPolicy,
)
from steam_reviews.settings import Settings, load_settings, new_run_id  # noqa: E402

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


def setup_logging(settings: Settings, run_id: str, level: str = "INFO") -> Path:
    settings.paths.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = settings.paths.logs_dir / f"{run_id}.log"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
    )
    return log_path


def _dump(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def parse_appid_budgets(items: list[str] | None) -> dict[int, int] | None:
    """解析 --appid-budget APPID=BYTES。"""
    if not items:
        return None
    budgets: dict[int, int] = {}
    for item in items:
        if "=" not in item:
            raise ConfigError(f"--appid-budget 需要 APPID=BYTES 形式: {item!r}")
        appid_text, bytes_text = item.split("=", 1)
        try:
            appid, amount = int(appid_text), int(bytes_text)
        except ValueError as exc:
            raise ConfigError('--appid-budget 的AppID和字节数必须是整数') from exc
        if appid <= 0 or amount <= 0:
            raise ConfigError('--appid-budget 的AppID和字节数必须为正整数')
        budgets[appid] = amount
    return budgets


def apply_policy_overrides(args: argparse.Namespace, settings: Settings) -> Settings:
    """命令行显式参数覆盖配置策略（优先级最高）。"""
    from dataclasses import replace

    overrides = {}
    if getattr(args, "max_review_bytes_per_game", None) is not None:
        overrides["max_review_bytes_per_game"] = args.max_review_bytes_per_game
    if getattr(args, "min_free_disk_bytes", None) is not None:
        overrides["min_free_disk_bytes"] = args.min_free_disk_bytes
    if getattr(args, "max_runtime_seconds", None) is not None:
        overrides["max_runtime_seconds"] = args.max_runtime_seconds
    if not overrides:
        return settings
    base = settings.require_policy()
    if "max_review_bytes_per_game" in overrides:
        overrides["per_game_overrides"] = {}
    return replace(settings, policy=replace(base, **overrides))


def cmd_verify(args: argparse.Namespace, settings: Settings) -> int:
    """每款游戏请求一页，核对可访问性、匹配量与字段。"""
    appids = args.appid or list(settings.appids)
    segment = settings.segment(args.segment)
    client = build_client(settings)
    settings.paths.samples_dir.mkdir(parents=True, exist_ok=True)
    report = {"segment": segment.to_dict(), "games": []}
    exit_code = EXIT_OK

    try:
        for appid in appids:
            settings.ensure_appid(appid)
            request = ReviewPageRequest(
                appid=appid,
                segment=segment,
                cursor=None,
                num_per_page=settings.request.verify_num_per_page,
                languages=settings.request.languages,
                review_filter=settings.request.review_filter,
                review_type=settings.request.review_type,
                purchase_type=settings.request.purchase_type,
                filter_offtopic_activity=settings.request.filter_offtopic_activity,
                param_style=args.param_style,
            )
            started = time.time()
            try:
                page = fetch_review_page(client, request)
            except SteamError as exc:
                report["games"].append({"appid": appid, "ok": False, "error": exc.to_dict()})
                exit_code = EXIT_FAILURE
                continue
            elapsed = time.time() - started
            sample = {
                "appid": appid,
                "fetched_at": page.fetched_at,
                "request": request.input_json_body(),
                "query_summary": page.summary.to_dict(),
                "first_review": page.raw_reviews[0] if page.raw_reviews else None,
            }
            sample_path = settings.paths.samples_dir / f"verify_{appid}_{page.fetched_at}.json"
            sample_path.write_text(
                json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            languages = sorted({r.get("language") for r in page.raw_reviews})
            hardware = sum(1 for r in page.raw_reviews if r.get("hardware"))
            dev_response = sum(1 for r in page.raw_reviews if r.get("developer_response"))
            report["games"].append(
                {
                    "appid": appid,
                    "ok": True,
                    "elapsed_s": round(elapsed, 2),
                    "count": len(page.raw_reviews),
                    "languages": languages,
                    "has_next_cursor": bool(page.next_cursor),
                    "total_matching": page.summary.total_matching,
                    "day_range_used": page.summary.day_range_used,
                    "records_with_hardware": hardware,
                    "records_with_developer_response": dev_response,
                    "optional_keys_present": sorted(
                        {k for r in page.raw_reviews for k in r.keys()}
                        - {
                            "recommendationid", "author", "language", "review",
                            "timestamp_created", "timestamp_updated", "voted_up",
                            "votes_up", "votes_funny", "weighted_vote_score",
                            "comment_count", "steam_purchase", "received_for_free",
                            "written_during_early_access", "primarily_steam_deck", "refunded",
                        }
                    ),
                    "sample_path": str(sample_path),
                }
            )
    finally:
        client.close()

    report["ok"] = all(g.get("ok") for g in report["games"])
    _dump(report)
    return EXIT_OK if report["ok"] else EXIT_FAILURE


def cmd_reviews(args: argparse.Namespace, settings: Settings) -> int:
    run_id = args.run_id or new_run_id(settings, "steam_reviews")
    manifest_path = settings.task_dir(run_id) / 'manifest.json'
    saved = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.is_file() else None
    if saved:
        window = saved['window']
        settings = replace(settings, timezone_name=window.get('timezone', settings.timezone_name),
                           window_start=datetime.fromisoformat(window['start']),
                           window_end=datetime.fromisoformat(window['end']),
                           segment_order=saved.get('segment_order', settings.segment_order))
    if not (args.appid or args.all or saved):
        raise ConfigError('请用 --appid 指定评测采集对象，或用 --all 明确选择整个目录')
    setup_logging(settings, run_id, args.log_level)
    settings = apply_policy_overrides(args, settings)
    request = CollectionRequest(
        run_id=run_id,
        appids=args.appid or (list(settings.appids) if args.all else saved['appids']),
        segment_indexes=args.segment_index or ([item['index'] for item in saved['segments']] if saved else None),
        max_pages_per_segment=args.max_pages,
        param_style=args.param_style,
        note=args.note,
        cli_budgets=parse_appid_budgets(args.appid_budget),
        policy_override=any(getattr(args, name, None) is not None for name in
                            ('max_review_bytes_per_game', 'min_free_disk_bytes', 'max_runtime_seconds')),
    )
    summary = collect_reviews(settings, request)
    _dump(summary.to_dict())
    return EXIT_OK if summary.status in ("completed", "partial") else EXIT_FAILURE


def cmd_store(args: argparse.Namespace, settings: Settings) -> int:
    run_id = args.run_id or new_run_id(settings, "store")
    setup_logging(settings, run_id, args.log_level)
    targets = args.appid or list(settings.appids)
    if args.missing:
        targets = [appid for appid in targets if not (settings.game_dir(appid) / 'info.json').is_file()]
    if not targets:
        _dump({'games': [], 'errors': 0})
        return EXIT_OK
    payload = collect_store(settings, run_id, targets, with_cover=not args.no_cover)
    _dump(payload)
    return EXIT_OK if payload.get("errors", 0) == 0 else EXIT_FAILURE


def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    status = get_collection_status(settings, args.run_id)
    _dump(status.to_dict())
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Steam 评测采集入口")
    parser.add_argument("--root", default=None, help="项目根目录（默认自动查找）")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify", help="每款游戏请求一页做结构验证")
    verify.add_argument("--appid", type=int, action="append", help="可重复，默认全部")
    verify.add_argument("--segment", type=int, default=0, help="时间段序号，默认 0")
    verify.add_argument("--param-style", default="input_json", choices=["input_json", "query"])
    verify.set_defaults(func=cmd_verify)

    reviews = sub.add_parser("reviews", help="按窗口采集评测")
    reviews.add_argument("--run-id", default=None)
    targets = reviews.add_mutually_exclusive_group()
    targets.add_argument("--appid", type=int, action="append")
    targets.add_argument("--all", action="store_true", help="明确选择整个目录采集评测")
    reviews.add_argument("--segment-index", type=int, action="append")
    reviews.add_argument("--max-pages", type=int, default=0, help="每段页数上限，0 为不限")
    reviews.add_argument("--max-review-bytes-per-game", type=int, default=None,
                         help=f"单款游戏评测 JSONL 字节额度，默认 {DEFAULT_MAX_REVIEW_BYTES_PER_GAME}")
    reviews.add_argument("--min-free-disk-bytes", type=int, default=None,
                         help=f"磁盘剩余安全阈值，默认 {DEFAULT_MIN_FREE_DISK_BYTES}")
    reviews.add_argument("--max-runtime-seconds", type=int, default=None,
                         help="本次运行时长上限；不传表示不限制")
    reviews.add_argument("--appid-budget", action="append", metavar="APPID=BYTES",
                         help="单游戏额度覆盖，可重复，优先级最高")
    reviews.add_argument("--param-style", default="input_json", choices=["input_json", "query"])
    reviews.add_argument("--note", default=None)
    reviews.set_defaults(func=cmd_reviews)

    store = sub.add_parser("store", help="采集商店资料、价格快照与封面")
    store.add_argument("--run-id", default=None)
    store.add_argument("--appid", type=int, action="append")
    store.add_argument("--no-cover", action="store_true")
    store.add_argument("--missing", action="store_true", help="仅采集尚无 info.json 的商店条目")
    store.set_defaults(func=cmd_store)

    status = sub.add_parser("status", help="查看任务状态")
    status.add_argument("--run-id", required=True)
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.root)
    except ConfigError as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()}, ensure_ascii=False, indent=2))
        return EXIT_USAGE
    try:
        return args.func(args, settings)
    except SteamError as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()}, ensure_ascii=False, indent=2))
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
