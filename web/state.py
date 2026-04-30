"""
全局单例状态管理器
- 维护调度器实例
- 维护后台任务执行状态和日志缓冲区
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Optional

from loguru import logger


# ──────────────────────────────────────────────
# 调度器单例
# ──────────────────────────────────────────────
_scheduler_instance = None
_scheduler_lock = threading.Lock()


def get_scheduler():
    """获取全局调度器单例（懒加载）"""
    global _scheduler_instance
    with _scheduler_lock:
        if _scheduler_instance is None:
            from scheduler.task_scheduler import TaskScheduler
            _scheduler_instance = TaskScheduler()
        return _scheduler_instance


# ──────────────────────────────────────────────
# 后台任务管理器
# ──────────────────────────────────────────────
class TaskRecord:
    """单个后台任务的状态记录"""

    def __init__(self, task_id: str, task_type: str, params: dict):
        self.task_id = task_id
        self.task_type = task_type      # "crawl" | "reply"
        self.params = params
        self.status = "running"         # running | done | failed | cancelled
        self.logs: list[str] = []
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.started_at = datetime.now()
        self.finished_at: Optional[datetime] = None
        self._lock = threading.Lock()
        self.cancelled: bool = False    # 取消信号标志位

    def add_log(self, msg: str) -> None:
        with self._lock:
            ts = datetime.now().strftime("%H:%M:%S")
            self.logs.append(f"[{ts}] {msg}")
            # 最多保留 500 行，避免内存无限增长
            if len(self.logs) > 500:
                self.logs = self.logs[-400:]

    def finish(self, result: dict) -> None:
        with self._lock:
            self.status = "done"
            self.result = result
            self.finished_at = datetime.now()

    def fail(self, error: str) -> None:
        with self._lock:
            self.status = "failed"
            self.error = error
            self.finished_at = datetime.now()

    def cancel(self) -> None:
        with self._lock:
            if self.status == "running":
                self.cancelled = True
                self.status = "cancelled"
                self.finished_at = datetime.now()

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "task_id": self.task_id,
                "task_type": self.task_type,
                "params": self.params,
                "status": self.status,
                "logs": list(self.logs),
                "result": self.result,
                "error": self.error,
                "started_at": self.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": self.finished_at.strftime("%Y-%m-%d %H:%M:%S") if self.finished_at else None,
            }


class TaskManager:
    """后台任务管理器（线程安全）"""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = threading.Lock()
        # 每种类型最多同时跑1个
        self._running: dict[str, Optional[str]] = {"crawl": None, "reply": None}

    def is_running(self, task_type: str) -> bool:
        tid = self._running.get(task_type)
        if not tid:
            return False
        rec = self._tasks.get(tid)
        return rec is not None and rec.status == "running"

    def get_running_task_id(self, task_type: str) -> Optional[str]:
        tid = self._running.get(task_type)
        if tid and self.is_running(task_type):
            return tid
        return None

    def submit_crawl(self, keyword: Optional[str] = None, pages: Optional[int] = None) -> str:
        if self.is_running("crawl"):
            raise RuntimeError("抓取任务正在运行中，请等待完成后再触发")
        task_id = str(uuid.uuid4())[:8]
        rec = TaskRecord(task_id, "crawl", {"keyword": keyword, "pages": pages})
        with self._lock:
            self._tasks[task_id] = rec
            self._running["crawl"] = task_id

        thread = threading.Thread(target=self._run_crawl, args=(rec, keyword, pages), daemon=True)
        thread.start()
        return task_id

    def submit_reply(self, count: Optional[int] = None, keyword: Optional[str] = None, dry_run: bool = False, force_all: bool = False) -> str:
        if self.is_running("reply"):
            raise RuntimeError("回复任务正在运行中，请等待完成后再触发")
        task_id = str(uuid.uuid4())[:8]
        rec = TaskRecord(task_id, "reply", {"count": count, "keyword": keyword, "dry_run": dry_run, "force_all": force_all})
        with self._lock:
            self._tasks[task_id] = rec
            self._running["reply"] = task_id

        thread = threading.Thread(target=self._run_reply, args=(rec, count, keyword, dry_run, force_all), daemon=True)
        thread.start()
        return task_id

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        return self._tasks.get(task_id)

    def cancel_task(self, task_type: str) -> bool:
        """取消指定类型的正在运行任务，返回是否成功"""
        tid = self._running.get(task_type)
        if not tid:
            return False
        rec = self._tasks.get(tid)
        if rec and rec.status == "running":
            rec.cancel()
            rec.add_log("⛔ 任务已被用户手动停止")
            return True
        return False

    def get_status(self) -> dict:
        crawl_id = self._running.get("crawl")
        reply_id = self._running.get("reply")
        return {
            "crawl": self._tasks[crawl_id].to_dict() if crawl_id and crawl_id in self._tasks else None,
            "reply": self._tasks[reply_id].to_dict() if reply_id and reply_id in self._tasks else None,
        }

    # ── 实际任务执行 ──────────────────────────────
    def _run_crawl(self, rec: TaskRecord, keyword: Optional[str], pages: Optional[int]) -> None:
        from crawler.xhs_crawler import XhsCrawler
        crawler = XhsCrawler(log_fn=rec.add_log)
        rec.add_log("▶ 开始执行抓取任务")
        try:
            if keyword:
                if rec.cancelled: return
                rec.add_log(f"搜索关键词: {keyword}")
                new_count = crawler.crawl_keyword(keyword, pages)
                result = {keyword: new_count}
                rec.add_log(f"✓ 关键词 [{keyword}] 新增 {new_count} 条帖子")
            else:
                import config as cfg
                keywords = cfg.get("xhs.keywords", [])
                rec.add_log(f"搜索所有关键词: {keywords}")
                result = {}
                for kw in keywords:
                    if rec.cancelled:
                        rec.add_log("⛔ 任务已停止")
                        break
                    rec.add_log(f"  → 搜索: {kw}")
                    try:
                        n = crawler.crawl_keyword(kw, pages)
                        result[kw] = n
                        rec.add_log(f"  ✓ [{kw}] 新增 {n} 条")
                    except Exception as e:
                        rec.add_log(f"  ✗ [{kw}] 失败: {e}")
                        result[kw] = 0

            if not rec.cancelled:
                total = sum(result.values())
                rec.add_log(f"✅ 抓取完成，共新增 {total} 条帖子")
                rec.finish({"keywords": result, "total_new": total})
        except Exception as e:
            if not rec.cancelled:
                rec.add_log(f"❌ 任务失败: {e}")
                rec.fail(str(e))

    def _run_reply(self, rec: TaskRecord, count: Optional[int], keyword: Optional[str], dry_run: bool, force_all: bool = False) -> None:
        from replier.auto_replier import AutoReplier
        if rec.cancelled:
            return
        replier = AutoReplier()
        mode_str = "【DRY-RUN 不实际发送】" if dry_run else ""
        force_str = "【全量模式，忽略热度门槛】" if force_all else ""
        rec.add_log(f"▶ 开始执行回复任务 {mode_str}{force_str}")
        try:
            stats = replier.run_batch(
                max_count=count,
                keyword=keyword,
                dry_run=dry_run,
                force_all=force_all,
                cancel_flag=rec,
                log_fn=rec.add_log,
            )
            if not rec.cancelled:
                rec.add_log(f"✅ 回复完成")
                rec.add_log(f"   共处理: {stats['total']} 条")
                rec.add_log(f"   评论成功: {stats['comment_ok']} 条")
                rec.add_log(f"   私信成功: {stats['dm_ok']} 条")
                rec.add_log(f"   失败: {stats['failed']} 条")
                rec.finish(stats)
        except Exception as e:
            rec.add_log(f"❌ 任务失败: {e}")
            rec.fail(str(e))

# 全局任务管理器实例
task_manager = TaskManager()
