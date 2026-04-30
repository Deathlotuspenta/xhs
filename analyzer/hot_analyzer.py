"""热度分析模块

综合点赞、评论、收藏、分享四个维度，计算帖子热度评分（0-100）。
"""
from __future__ import annotations

import math
from typing import Sequence

import config
from database import get_session, Post, ReplyStatus


class HotAnalyzer:
    """帖子热度评分计算器"""

    def __init__(self) -> None:
        self._reload_cfg()

    def _reload_cfg(self) -> None:
        w = config.get("hot_threshold.weights", {})
        self._w_likes = float(w.get("likes", 0.4))
        self._w_comments = float(w.get("comments", 0.3))
        self._w_collects = float(w.get("collects", 0.2))
        self._w_shares = float(w.get("shares", 0.1))

        self._base_likes = float(config.get("hot_threshold.likes_base", 1000))
        self._base_comments = float(config.get("hot_threshold.comments_base", 100))
        self._base_collects = float(config.get("hot_threshold.collects_base", 500))
        self._base_shares = float(config.get("hot_threshold.shares_base", 200))
        self._min_score = float(config.get("hot_threshold.min_score", 50))

    def score(
        self,
        likes: int,
        comments: int,
        collects: int,
        shares: int,
    ) -> float:
        """
        计算综合热度评分，返回 0-100 之间的浮点数。

        使用对数归一化，将各指标映射到 [0, 1]，
        再加权求和后乘以 100。
        """
        def norm(val: float, base: float) -> float:
            """对数归一化：log(val+1) / log(base+1)，结果 clamp 到 [0, 1]"""
            if base <= 0:
                return 0.0
            return min(1.0, math.log(val + 1) / math.log(base + 1))

        n_likes = norm(likes, self._base_likes)
        n_comments = norm(comments, self._base_comments)
        n_collects = norm(collects, self._base_collects)
        n_shares = norm(shares, self._base_shares)

        raw = (
            self._w_likes * n_likes
            + self._w_comments * n_comments
            + self._w_collects * n_collects
            + self._w_shares * n_shares
        )
        return round(raw * 100, 2)

    def is_hot(self, hot_score: float) -> bool:
        """判断帖子是否达到热度门槛"""
        return hot_score >= self._min_score

    # ──────────────────────────────────────────────
    # 批量查询热门帖子
    # ──────────────────────────────────────────────
    def get_hot_pending_posts(
        self, limit: int | None = None, keyword: str | None = None, force_all: bool = False
    ) -> list[Post]:
        """
        查询未回复的帖子，按热度降序排列。

        :param limit:     最多返回几条（None 表示不限）
        :param keyword:   按关键词过滤（None 表示不过滤）
        :param force_all: True = 忽略 min_score 门槛，返回全部 pending 帖子
        """
        self._reload_cfg()
        with get_session() as session:
            q = session.query(Post).filter(Post.reply_status == ReplyStatus.PENDING)
            if not force_all:
                q = q.filter(Post.hot_score >= self._min_score)
            if keyword:
                q = q.filter(Post.keyword == keyword)
            q = q.order_by(Post.hot_score.desc())
            if limit:
                q = q.limit(limit)
            return q.all()

    def get_stats(self) -> dict:
        """返回数据库中帖子的统计信息"""
        with get_session() as session:
            total = session.query(Post).count()
            pending = session.query(Post).filter(
                Post.reply_status == ReplyStatus.PENDING
            ).count()
            hot_pending = session.query(Post).filter(
                Post.reply_status == ReplyStatus.PENDING,
                Post.hot_score >= self._min_score,
            ).count()
            replied = session.query(Post).filter(
                Post.reply_status.in_([
                    ReplyStatus.REPLIED_COMMENT,
                    ReplyStatus.REPLIED_DM,
                    ReplyStatus.REPLIED_BOTH,
                ])
            ).count()
            skipped = session.query(Post).filter(
                Post.reply_status == ReplyStatus.SKIPPED
            ).count()
            failed = session.query(Post).filter(
                Post.reply_status == ReplyStatus.FAILED
            ).count()

        return {
            "total": total,
            "pending": pending,
            "hot_pending": hot_pending,
            "replied": replied,
            "skipped": skipped,
            "failed": failed,
        }
