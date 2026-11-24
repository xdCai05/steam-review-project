#!/usr/bin/env python
"""把落盘的 JSONL 清洗、校验后入库；重复导入不重复写入。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from steam_reviews.contracts import ConfigError, REVIEW_FIELD_KEYS, ValidationError  # noqa: E402
from steam_reviews.processing.normalize import mapping_to_record  # noqa: E402
from steam_reviews.processing.validate import check_record  # noqa: E402
from steam_reviews.settings import load_settings  # noqa: E402
from steam_reviews.storage import repository  # noqa: E402
from steam_reviews.storage.database import connect, init_db, transaction  # noqa: E402


def iter_lines(paths: list[Path]):
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                yield path, lineno, text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JSONL 入库")
    parser.add_argument("--root", default=None)
    parser.add_argument("--appid", type=int, default=None, help="导入指定游戏的全部月份评测")
    parser.add_argument("--path", default=None, help="读取单个文件或目录")
    parser.add_argument("--strict", action="store_true", help="出现校验失败即整批回滚")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.root)
    except ConfigError as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()}, ensure_ascii=False, indent=2))
        return 2

    if args.path:
        target = Path(args.path)
        files = [target] if target.is_file() else sorted(target.rglob("*.jsonl"))
    elif args.appid:
        settings.ensure_appid(args.appid)
        target = settings.review_dir(args.appid)
        files = sorted(target.rglob("*.jsonl")) if target.is_dir() else []
    else:
        print(json.dumps({"ok": False, "error": {"message": "需要 --appid 或 --path"}}, ensure_ascii=False, indent=2))
        return 2

    if not files:
        print(json.dumps({"ok": False, "error": {"message": "没有找到 JSONL 文件"}}, ensure_ascii=False, indent=2))
        return 2

    settings.paths.ensure()
    conn = connect(settings.paths.database)
    init_db(conn)

    import_id = f"import_{time.strftime('%Y%m%d_%H%M%S')}_{args.appid or 'path'}"
    repository.start_import(conn, import_id, str(target))

    counts = {"total": 0, "inserted": 0, "updated": 0, "unchanged": 0, "conflict": 0, "invalid": 0}
    problems: list[dict] = []
    window = (settings.window_start_ts, settings.window_end_ts)
    allowed = set(settings.appids)

    try:
        with transaction(conn):
            for path, lineno, text in iter_lines(files):
                counts["total"] += 1
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    counts["invalid"] += 1
                    problems.append({"file": str(path), "line": lineno, "reason": f"JSON 解析失败: {exc}"})
                    if args.strict:
                        raise ValidationError("JSON 解析失败", detail={"file": str(path), "line": lineno})
                    continue

                record = mapping_to_record(payload)
                issues = check_record(
                    record,
                    window=window,
                    expected_language=settings.request.languages[0],
                    allowed_appids=allowed,
                )
                if issues:
                    counts["invalid"] += 1
                    problems.append({"file": str(path), "line": lineno, "issues": issues})
                    if args.strict:
                        raise ValidationError("记录校验失败", detail={"file": str(path), "line": lineno, "issues": issues})
                    continue

                outcome = repository.upsert_review(conn, record)
                counts[outcome] += 1
    except ValidationError as exc:
        repository.finish_import(
            conn, import_id, status="failed", total=counts["total"],
            inserted=0, updated=0, unchanged=0, errors=counts["invalid"],
        )
        conn.close()
        print(json.dumps({"ok": False, "error": exc.to_dict(), "counts": counts}, ensure_ascii=False, indent=2))
        return 1

    status = "completed" if counts["invalid"] == 0 else "partial"
    repository.finish_import(
        conn, import_id, status=status, total=counts["total"],
        inserted=counts["inserted"], updated=counts["updated"],
        unchanged=counts["unchanged"], errors=counts["invalid"],
    )
    in_db = repository.count_reviews(conn)
    conn.close()

    print(
        json.dumps(
            {
                "ok": True,
                "import_id": import_id,
                "status": status,
                "files": len(files),
                "counts": counts,
                "reviews_in_db": in_db,
                "field_keys": len(REVIEW_FIELD_KEYS),
                "problems_sample": problems[:10],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
