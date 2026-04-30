"""任务调度模块

使用 APScheduler 按 Cron 表达式定时执行抓取和回复任务。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

import config
from crawler.xhs_crawler import XhsCrawler
from replier.auto_replier import AutoReplier


class TaskScheduler:
    """定时任务调度器"""

    def __init__(self) -> None:
        # APScheduler 在 shutdown() 后不能复用，需每次 start 时新建
        self._scheduler: Optional[BackgroundScheduler] = None
        self._crawler = XhsCrawler()
        self._replier = AutoReplier()

    @staticmethod
    def _make_scheduler() -> BackgroundScheduler:
        return BackgroundScheduler(
            timezone="Asia/Shanghai",
            job_defaults={"coalesce": True, "max_instances": 1},
        )

    # ──────────────────────────────────────────────
    # 启动 / 停止
    # ──────────────────────────────────────────────
    def start(self, run_now: bool = False) -> None:
        """
        启动调度器并注册所有任务。
        :param run_now: 若为 True，则立即执行一次抓取+回复（用于测试）
        """
        if self._scheduler is not None and self._scheduler.running:
            logger.warning("调度器已在运行中，跳过重复启动")
            return

        self._scheduler = self._make_scheduler()
        self._register_jobs()
        self._scheduler.start()
        logger.success("调度器已启动，已注册任务：")
        for job in self._scheduler.get_jobs():
            logger.info(f"  [{job.id}] {job.name} → 下次执行: {job.next_run_time}")

        if run_now:
            logger.info("run_now=True，立即执行一次全量任务 ...")
            self._job_crawl_all()
            self._job_reply_all()

    def stop(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=True)
            logger.info("调度器已停止")
        self._scheduler = None

    def is_running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    # ──────────────────────────────────────────────
    # 注册任务
    # ──────────────────────────────────────────────
    def _register_jobs(self) -> None:
        crawl_cron = config.get("scheduler.crawl_cron", "0 9,14,20 * * *")
        reply_cron = config.get("scheduler.reply_cron", "0 10,15,21 * * *")

        self._scheduler.add_job(
            func=self._job_crawl_all,
            trigger=CronTrigger.from_crontab(crawl_cron, timezone="Asia/Shanghai"),
            id="crawl_all",
            name="定时抓取帖子",
            replace_existing=True,
        )

        self._scheduler.add_job(
            func=self._job_reply_all,
            trigger=CronTrigger.from_crontab(reply_cron, timezone="Asia/Shanghai"),
            id="reply_all",
            name="定时自动回复",
            replace_existing=True,
        )

        logger.info(f"抓取任务 Cron: {crawl_cron}")
        logger.info(f"回复任务 Cron: {reply_cron}")

    # ──────────────────────────────────────────────
    # 任务实现
    # ──────────────────────────────────────────────
    def _job_crawl_all(self) -> None:
        """执行全关键词抓取"""
        logger.info(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ▶ 开始执行定时抓取任务 ...")
        try:
            results = self._crawler.crawl_all_keywords()
            total_new = sum(results.values())
            logger.success(
                f"抓取任务完成：关键词 {list(results.keys())} | 共新增 {total_new} 条帖子"
            )
        except Exception as e:
            logger.error(f"抓取任务异常: {e}")

    def _job_reply_all(self) -> None:
        """执行批量自动回复"""
        logger.info(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ▶ 开始执行定时回复任务 ...")
        try:
            stats = self._replier.run_batch()
            logger.success(
                f"回复任务完成：共 {stats['total']} | "
                f"评论 {stats['comment_ok']} | 私信 {stats['dm_ok']} | 失败 {stats['failed']}"
            )
        except Exception as e:
            logger.error(f"回复任务异常: {e}")

    # ──────────────────────────────────────────────
    # 手动触发（供 CLI 使用）
    # ──────────────────────────────────────────────
    def trigger_crawl(self, keyword: Optional[str] = None) -> None:
        """手动立即触发抓取"""
        if keyword:
            self._crawler.crawl_keyword(keyword)
        else:
            self._job_crawl_all()

    def trigger_reply(self, dry_run: bool = False) -> dict:
        """手动立即触发回复"""
        return self._replier.run_batch(dry_run=dry_run)

    def list_jobs(self) -> list[dict]:
        """返回当前调度任务信息"""
        if self._scheduler is None or not self._scheduler.running:
            return []
        jobs = []
        for job in self._scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time),
            })
        return jobs
