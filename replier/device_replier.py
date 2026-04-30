"""真机版批量回复器：与 AutoReplier 同样的批量流程，

只是“发送”这一步由小红书 Web 接口换成 ADB + Appium 操作真机。

负责：
  - 取出待回复帖子（含热度/关键词/force_all）
  - 模板抽取/渲染（沿用 TemplateManager）
  - 通过 device_runner 真机操作发送评论
  - 写回帖子状态、写 ReplyLog
  - 复用风控配置（工作时段/每日上限/连续失败熔断）

注意：私信不支持（真机端未实现），仅做评论。
"""
from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

import config
from analyzer.hot_analyzer import HotAnalyzer
from database import Post, ReplyLog, ReplyStatus, get_session
from replier.comment_image import resolve_disk_paths
from templates.template_manager import TemplateManager


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DeviceReplier:
    """真机版批量回复执行器（评论场景）。"""

    def __init__(
        self,
        *,
        device: str,
        version: str,
        appium_url: str = "http://127.0.0.1:4723",
        adb_path: Optional[str] = None,
    ) -> None:
        self.device = device
        self.version = version
        self.appium_url = appium_url
        self.adb_path = adb_path or None

        self._analyzer = HotAnalyzer()
        self._tmpl_mgr = TemplateManager()
        self._batch_used_texts: set[str] = set()
        self._fatal_stop: bool = False

        self._delay_min: float = float(config.get("reply.pre_reply_delay_min", 5))
        self._delay_max: float = float(config.get("reply.pre_reply_delay_max", 15))
        self._reply_interval: float = float(config.get("scheduler.reply_interval", 30))
        self._safe_mode: bool = bool(config.get("risk_control.safe_mode", True))
        self._safe_batch_min: int = int(config.get("risk_control.safe_batch_min", 1))
        self._safe_batch_max: int = int(config.get("risk_control.safe_batch_max", 3))
        self._safe_interval_min: float = float(config.get("risk_control.safe_interval_min", 45))
        self._safe_interval_max: float = float(config.get("risk_control.safe_interval_max", 120))

        # 风控
        self._rc_enabled: bool = bool(config.get("risk_control.enabled", True))
        self._rc_daily_comment_limit: int = int(config.get("risk_control.daily_comment_limit", 8))
        self._rc_max_consecutive_failures: int = int(
            config.get("risk_control.max_consecutive_failures", 2)
        )
        self._rc_work_start_hour: int = int(config.get("risk_control.work_start_hour", 9))
        self._rc_work_end_hour: int = int(config.get("risk_control.work_end_hour", 22))
        self._rc_work_windows: list[str] = list(config.get("risk_control.work_windows", []))
        self._rc_stop_keywords: list[str] = list(
            config.get(
                "risk_control.stop_keywords",
                ["禁言", "违反社区规范", "账号存在异常", "操作频繁", "验证码"],
            )
        )

    # ──────────────────────────────────────────────
    # 风控辅助（与 AutoReplier 一致）
    # ──────────────────────────────────────────────
    def _in_work_hours(self) -> bool:
        if self._rc_work_windows:
            h = datetime.now().hour
            for w in self._rc_work_windows:
                try:
                    s, e = str(w).split("-", 1)
                    sh = int(s.strip())
                    eh = int(e.strip())
                    if sh <= h < eh:
                        return True
                except Exception:
                    continue
            return False
        h = datetime.now().hour
        return self._rc_work_start_hour <= h < self._rc_work_end_hour

    def _today_success_comment_count(self) -> int:
        now = datetime.now()
        start = datetime(now.year, now.month, now.day)
        end = start + timedelta(days=1)
        with get_session() as session:
            return (
                session.query(ReplyLog)
                .filter(
                    ReplyLog.reply_type == "comment",
                    ReplyLog.success == True,  # noqa: E712
                    ReplyLog.replied_at >= start,
                    ReplyLog.replied_at < end,
                )
                .count()
            )

    def _is_risk_error(self, err_msg: str) -> bool:
        txt = (err_msg or "").strip()
        if not txt:
            return False
        return any(k in txt for k in self._rc_stop_keywords if k)

    @staticmethod
    def _random_sleep(base: float, upper: Optional[float] = None) -> None:
        if upper is None:
            lo, hi = base * 0.8, base * 1.2
        else:
            lo, hi = base, upper
        sleep_time = random.uniform(lo, hi)
        time.sleep(sleep_time)

    # ──────────────────────────────────────────────
    # 配图解析（模板内 JSON 优先；否则用全局；最后从图库取第一张；可被 force_image_paths 覆盖）
    # ──────────────────────────────────────────────
    def _resolve_template_images(
        self, tpl, force_image_paths: Optional[list[str]] = None
    ) -> list[str]:
        if force_image_paths:
            return [p for p in force_image_paths if str(p).strip()]

        raw_ip = getattr(tpl, "image_paths", None) if tpl is not None else None
        tpl_paths: list[str] = []
        if raw_ip:
            try:
                ar = json.loads(raw_ip) if isinstance(raw_ip, str) else raw_ip
                if isinstance(ar, list):
                    tpl_paths = [str(x).strip() for x in ar if str(x).strip()]
            except Exception:
                tpl_paths = []
        if tpl_paths:
            return tpl_paths

        global_paths = list(config.get("reply.comment_image_paths") or [])
        if global_paths:
            return global_paths

        # 最后兜底：从图库目录里按文件名排序取第一张
        first = self._first_image_from_library()
        if first:
            return [first]
        return []

    def _first_image_from_library(self) -> Optional[str]:
        """从评论图库目录取第一张图，目录不存在就自动创建。"""
        lib_rel = config.get("reply.comment_image_library_dir", "images/comment_library")
        lib_dir = (PROJECT_ROOT / str(lib_rel)).resolve()
        try:
            lib_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        if not lib_dir.is_dir():
            return None
        exts = {".jpg", ".jpeg", ".png", ".webp"}
        files = sorted(
            (p for p in lib_dir.iterdir() if p.is_file() and p.suffix.lower() in exts),
            key=lambda p: p.name.lower(),
        )
        if not files:
            return None
        # 返回相对项目根目录的路径，方便 resolve_disk_paths 复用
        try:
            return str(files[0].relative_to(PROJECT_ROOT))
        except ValueError:
            return str(files[0])

    # ──────────────────────────────────────────────
    # 主入口
    # ──────────────────────────────────────────────
    def run_batch(
        self,
        max_count: Optional[int] = None,
        keyword: Optional[str] = None,
        dry_run: bool = False,
        force_all: bool = False,
        force_image_paths: Optional[list[str]] = None,
        force_text: Optional[str] = None,
        cancel_flag=None,
        log_fn=None,
    ) -> dict:
        """批量执行真机评论。"""

        def _log(msg: str) -> None:
            logger.info(msg)
            if log_fn:
                log_fn(msg)

        max_count = max_count or int(config.get("scheduler.max_replies_per_batch", 5))
        if self._safe_mode:
            max_count = max(self._safe_batch_min, min(max_count, self._safe_batch_max))
        self._tmpl_mgr.ensure_defaults()
        self._batch_used_texts = set()
        self._fatal_stop = False
        consecutive_failures = 0

        if self._rc_enabled:
            if not self._in_work_hours():
                _log(
                    f"⏸ 当前时间不在安全发送窗口 "
                    f"({self._rc_work_start_hour}:00-{self._rc_work_end_hour}:00)，已跳过本批次"
                )
                return {"total": 0, "comment_ok": 0, "dm_ok": 0, "failed": 0}
            today_ok = self._today_success_comment_count()
            if today_ok >= self._rc_daily_comment_limit:
                _log(
                    f"⏸ 今日成功评论 {today_ok} 条，达到风控上限 "
                    f"{self._rc_daily_comment_limit} 条，已停止发送"
                )
                return {"total": 0, "comment_ok": 0, "dm_ok": 0, "failed": 0}

        posts = self._analyzer.get_hot_pending_posts(
            limit=max_count, keyword=keyword, force_all=force_all
        )
        if not posts:
            _log("没有待回复的帖子")
            return {"total": 0, "comment_ok": 0, "dm_ok": 0, "failed": 0}

        _log(f"本批待真机回复帖子数：{len(posts)}（设备 {self.device} / Android {self.version}）")
        stats = {"total": len(posts), "comment_ok": 0, "dm_ok": 0, "failed": 0}

        # 整批共用一个 Appium 会话，避免反复启动 App
        from device_runner.appium_session import AppiumSession
        from device_runner.xhs_actions import XhsAppActions, push_to_album

        session = None
        actions = None
        if not dry_run:
            session = AppiumSession(
                device_name=self.device,
                platform_version=self.version,
                appium_url=self.appium_url,
            )
            session.start()
            actions = XhsAppActions(session)

        try:
            for i, post in enumerate(posts):
                if cancel_flag and getattr(cancel_flag, "cancelled", False):
                    _log("⛔ 任务已被用户停止")
                    break
                if self._fatal_stop:
                    _log("⛔ 任务已因风控终止")
                    break

                _log(
                    f"[{i+1}/{len(posts)}] 处理帖子「{post.title or post.note_id}」"
                    f"  score={post.hot_score:.1f}"
                )

                # 选模板 + 渲染文本
                tpl = self._tmpl_mgr.pick_random("comment", keyword=post.keyword)
                if not tpl and not force_text:
                    _log("  [评论] 无可用模板，跳过")
                    stats["failed"] += 1
                    continue

                if force_text:
                    content = force_text
                    tpl_id = tpl.id if tpl else None
                else:
                    content = self._tmpl_mgr.render_content(
                        tpl.content,
                        keyword=post.keyword,
                        author_name=post.author_name,
                        used_texts=self._batch_used_texts,
                    )
                    tpl_id = tpl.id

                preview = (content or "")[:40] or "（无文字）"
                _log(f"  [评论] 模板「{tpl.name if tpl else '自定义'}»{preview}…")

                # 配图
                image_rel_paths = self._resolve_template_images(tpl, force_image_paths)
                local_paths: list[str] = []
                if image_rel_paths:
                    try:
                        local_paths = [str(p) for p in resolve_disk_paths(image_rel_paths)]
                        _log(f"  [评论] 待发图片 {len(local_paths)} 张")
                    except Exception as e:
                        err_msg = f"配图解析失败: {e}"
                        _log(f"  [评论] {err_msg}")
                        self._save_log(post.note_id, tpl_id, "comment", content, False, err_msg)
                        self._update_post_error(post, err_msg)
                        stats["failed"] += 1
                        consecutive_failures += 1
                        if (
                            self._rc_enabled
                            and consecutive_failures >= self._rc_max_consecutive_failures
                        ):
                            _log("⛔ 连续失败达阈值，熔断停止")
                            break
                        continue

                if dry_run:
                    _log("  [评论][DRY-RUN] 不实际发送")
                    self._save_log(post.note_id, tpl_id, "comment", content, True)
                    if tpl_id:
                        self._tmpl_mgr.record_used(tpl_id)
                    stats["comment_ok"] += 1
                    self._update_post_status(post, True)
                    if i < len(posts) - 1:
                        if self._safe_mode:
                            self._random_sleep(self._safe_interval_min, self._safe_interval_max)
                        else:
                            self._random_sleep(self._reply_interval)
                    continue

                # 发送前随机等待
                self._random_sleep(self._delay_min, self._delay_max)

                # 真机发送
                ok = False
                err_msg = ""
                try:
                    if local_paths:
                        push_to_album(local_paths, device=self.device, adb_path=self.adb_path)
                    actions.open_note_by_id(post.note_id)
                    actions.open_comment_input()
                    actions.type_comment(content)
                    if local_paths:
                        actions.attach_first_album_image()
                    actions.send()
                    ok = True
                except Exception as e:
                    err_msg = str(e)
                    if self._rc_enabled and self._is_risk_error(err_msg):
                        self._fatal_stop = True

                if ok:
                    stats["comment_ok"] += 1
                    consecutive_failures = 0
                    label = content + (f"\n[配图×{len(local_paths)}]" if local_paths else "")
                    self._save_log(post.note_id, tpl_id, "comment", label, True)
                    if tpl_id:
                        self._tmpl_mgr.record_used(tpl_id)
                    self._update_post_status(post, True)
                    _log("  [评论] 真机发送成功 ✓")
                else:
                    stats["failed"] += 1
                    consecutive_failures += 1
                    _log(f"  [评论] 真机发送失败: {err_msg}")
                    self._save_log(post.note_id, tpl_id, "comment", content, False, err_msg)
                    self._update_post_error(post, err_msg)
                    if self._rc_enabled and consecutive_failures >= self._rc_max_consecutive_failures:
                        _log("⛔ 连续失败达阈值，熔断停止")
                        break

                if i < len(posts) - 1:
                    if self._safe_mode:
                        self._random_sleep(self._safe_interval_min, self._safe_interval_max)
                    else:
                        self._random_sleep(self._reply_interval)

        finally:
            if session is not None:
                try:
                    session.quit()
                except Exception:
                    pass

        _log(
            f"批次完成：共 {stats['total']} | 评论成功 {stats['comment_ok']} | "
            f"失败 {stats['failed']}"
        )
        return stats

    # ──────────────────────────────────────────────
    # 数据库写回
    # ──────────────────────────────────────────────
    def _update_post_status(self, post: Post, comment_ok: bool) -> None:
        with get_session() as session:
            db_post = session.get(Post, post.id)
            if not db_post:
                return
            if comment_ok:
                db_post.reply_status = ReplyStatus.REPLIED_COMMENT
                db_post.replied_comment_at = datetime.utcnow()
            else:
                db_post.reply_status = ReplyStatus.FAILED
            db_post.updated_at = datetime.utcnow()
            session.commit()

    def _update_post_error(self, post: Post, error: str) -> None:
        with get_session() as session:
            db_post = session.get(Post, post.id)
            if db_post:
                db_post.reply_error = error[:500]
                db_post.updated_at = datetime.utcnow()
                session.commit()

    def _save_log(
        self,
        note_id: str,
        template_id: Optional[int],
        reply_type: str,
        content: str,
        success: bool,
        error_msg: str = "",
    ) -> None:
        with get_session() as session:
            log = ReplyLog(
                post_note_id=note_id,
                template_id=template_id,
                reply_type=reply_type,
                content=content,
                success=success,
                error_msg=error_msg or None,
            )
            session.add(log)
            session.commit()
