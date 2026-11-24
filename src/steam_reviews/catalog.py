"""共享 AppID 目录，兼容原有五款游戏的显示名称。"""
import json
from pathlib import Path

from .contracts import ConfigError
from .storage.lease import exclusive_file


def load_catalog(root: Path, *, include_added: bool = True) -> dict:
    config = root / 'config'
    original = json.loads((config / 'games.json').read_text(encoding='utf-8'))['games']
    games = {int(game['appid']): game for game in original}
    added = config / 'appids.json'
    if include_added and added.is_file():
        for appid in json.loads(added.read_text(encoding='utf-8')):
            validate_appid(appid)
            games.setdefault(appid, {'appid': appid})
    return games


def validate_appid(appid: int) -> None:
    if type(appid) is not int or not 0 < appid <= 2**32 - 1:
        raise ConfigError('请输入有效的 Steam AppID')


def add_appids(root: Path, appids: list[int]) -> list[int]:
    for appid in appids:
        validate_appid(appid)
    path = root / 'config' / 'appids.json'
    with exclusive_file(path.with_name('.appids.lock')):
        catalog = load_catalog(root)
        new_ids = [appid for appid in dict.fromkeys(appids) if appid not in catalog]
        if new_ids:
            current = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
            temporary = path.with_suffix('.json.tmp')
            temporary.write_text(json.dumps([*current, *new_ids], indent=2) + '\n', encoding='utf-8')
            temporary.replace(path)
    return new_ids
