"""从账号池选取可用 Cookie，支持随机与轮询。"""
from __future__ import annotations

import random
import threading
from datetime import datetime
from typing import Optional

import config
from database import get_session, Account

_lock = threading.Lock()
_round_robin_i: int = 0


def pick_cookie_str() -> Optional[str]:
    """
    从 active 账号中取一条 cookie，并更新使用统计。
    无账号时返回 None，由调用方回退到 config.xhs.cookies。
    """
    mode = (config.get("xhs.account_pick", "random") or "random").lower().strip()

    with get_session() as session:
        accounts = (
            session.query(Account)
            .filter(Account.status == "active")
            .order_by(Account.id)
            .all()
        )
        if not accounts:
            return None

        if mode == "round_robin":
            global _round_robin_i
            with _lock:
                acc = accounts[_round_robin_i % len(accounts)]
                _round_robin_i += 1
        else:
            acc = random.choice(accounts)

        cookie = acc.cookie_str
        acc.use_count = (acc.use_count or 0) + 1
        acc.last_used_at = datetime.utcnow()
        session.commit()
        return cookie


def pick_account_cookie() -> tuple[Optional[int], Optional[str]]:
    """
    返回 (account_id, cookie_str)。
    若账号池为空，则返回 (None, None)。
    """
    mode = (config.get("xhs.account_pick", "random") or "random").lower().strip()

    with get_session() as session:
        accounts = (
            session.query(Account)
            .filter(Account.status == "active")
            .order_by(Account.id)
            .all()
        )
        if not accounts:
            return None, None

        if mode == "round_robin":
            global _round_robin_i
            with _lock:
                acc = accounts[_round_robin_i % len(accounts)]
                _round_robin_i += 1
        else:
            acc = random.choice(accounts)

        acc.use_count = (acc.use_count or 0) + 1
        acc.last_used_at = datetime.utcnow()
        session.commit()
        return acc.id, acc.cookie_str


def mark_account_status(account_id: Optional[int], status: str) -> None:
    """更新账号状态（active/invalid/banned）。"""
    if not account_id:
        return
    with get_session() as session:
        acc = session.get(Account, account_id)
        if not acc:
            return
        acc.status = status
        acc.updated_at = datetime.utcnow()
        session.commit()
