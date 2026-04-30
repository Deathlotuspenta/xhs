"""全局配置加载模块"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.yaml"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


def _load_yaml() -> dict[str, Any]:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_cfg: dict[str, Any] = _load_yaml()


def get(key_path: str, default: Any = None) -> Any:
    """使用点号分隔路径读取配置，例如 get('xhs.cookies')"""
    keys = key_path.split(".")
    node = _cfg
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k, default)
        if node is None:
            return default
    return node


def reload() -> None:
    """重新从磁盘加载配置"""
    global _cfg
    _cfg = _load_yaml()


# 便捷访问常量
XHS_COOKIES: str = os.getenv("XHS_COOKIES", "") or get("xhs.cookies", "")
XHS_UA: str = get("xhs.user_agent", "")
DB_PATH: str = str(DATA_DIR / Path(get("database.path", "data/xhs_bot.db")).name)
LOG_FILE: str = get("logging.file", "logs/xhs_bot.log")
LOG_LEVEL: str = get("logging.level", "INFO")
