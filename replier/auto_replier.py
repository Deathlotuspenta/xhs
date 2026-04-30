"""自动回复模块

负责从数据库取出待回复的热门帖子，调用 xhs 客户端发评论或私信，
并将回复结果写回数据库（打已回复标记）。
"""
from __future__ import annotations

import json
import time
import random
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

import config
from database import get_session, Post, ReplyLog, ReplyStatus
from analyzer.hot_analyzer import HotAnalyzer
from templates.template_manager import TemplateManager
from replier.comment_image import build_image_entries, post_web_comment, resolve_disk_paths


class AutoReplier:
    """自动回复执行器"""

    def __init__(self) -> None:
        self._analyzer = HotAnalyzer()
        self._tmpl_mgr = TemplateManager()
        self._client = None
        self._account_id: Optional[int] = None
        self._fatal_stop: bool = False
        self._batch_used_texts: set[str] = set()

        self._enable_comment: bool = config.get("reply.enable_comment", True)
        self._enable_dm: bool = config.get("reply.enable_dm", False)
        self._delay_min: float = config.get("reply.pre_reply_delay_min", 5)
        self._delay_max: float = config.get("reply.pre_reply_delay_max", 15)
        self._reply_interval: float = config.get("scheduler.reply_interval", 30)
        self._dm_interval: float = config.get("scheduler.dm_interval", 60)
        self._image_comment_strict: bool = bool(
            config.get("reply.image_comment_strict", True)
        )
        # 风控守护（降低风险，不保证不封）
        self._rc_enabled: bool = bool(config.get("risk_control.enabled", True))
        self._rc_daily_comment_limit: int = int(config.get("risk_control.daily_comment_limit", 8))
        self._rc_max_consecutive_failures: int = int(config.get("risk_control.max_consecutive_failures", 2))
        self._rc_work_start_hour: int = int(config.get("risk_control.work_start_hour", 9))
        self._rc_work_end_hour: int = int(config.get("risk_control.work_end_hour", 22))
        self._rc_stop_keywords: list[str] = list(
            config.get(
                "risk_control.stop_keywords",
                ["禁言", "违反社区规范", "账号存在异常", "操作频繁", "验证码"],
            )
        )

    # ──────────────────────────────────────────────
    # XHS 客户端
    # ──────────────────────────────────────────────
    def _get_client(self):
        if self._client is not None:
            return self._client

        try:
            from xhs import XhsClient
        except ImportError as e:
            raise RuntimeError("请先安装 xhs 库：pip install xhs") from e

        from crawler.account_pool import pick_account_cookie
        self._account_id, cookies = pick_account_cookie()
        cookies = cookies or config.XHS_COOKIES
                
        if not cookies:
            raise ValueError("没有可用的账号 Cookie！请在账号管理中扫码登录，或填写 config.yaml")

        # 检查 Cookie 是否包含 web_session（登录态的关键字段）
        cookie_kv = {p.split("=", 1)[0].strip(): p.split("=", 1)[1].strip()
                     for p in cookies.split(";") if "=" in p}
        if not cookie_kv.get("web_session"):
            raise ValueError(
                "账号 Cookie 缺少 web_session，登录未完成或已失效！\n"
                "请在「账号管理」中删除旧账号，重新扫码登录。"
            )

        # 使用 xhshow 纯算法签名（参考 MediaCrawler 项目，无需浏览器/JS逆向）
        from crawler.xhs_sign import make_sign_func
        sign_func = make_sign_func(cookies)

        self._client = XhsClient(cookie=cookies, sign=sign_func)
        return self._client

    @staticmethod
    def _is_banned_error(err_msg: str) -> bool:
        txt = (err_msg or "").strip()
        if not txt:
            return False
        # 常见禁言错误：{'code': 10001, 'msg': '因违反社区规范被禁言'}
        return ("10001" in txt and "禁言" in txt) or ("违反社区规范" in txt and "禁言" in txt)

    def _handle_fatal_ban(self, err_msg: str, log_fn=None) -> None:
        """检测到禁言后：标记账号 banned 并停止当前批次。"""
        if not self._is_banned_error(err_msg):
            return
        from crawler.account_pool import mark_account_status

        mark_account_status(self._account_id, "banned")
        self._fatal_stop = True
        self._client = None
        logger.error("检测到账号被禁言，已自动标记为 banned 并停止本批次")
        if log_fn:
            log_fn("⛔ 检测到账号被禁言（code=10001），已自动停机并将账号标记为 banned")

    def _is_risk_error(self, err_msg: str) -> bool:
        txt = (err_msg or "").strip()
        if not txt:
            return False
        return any(k in txt for k in self._rc_stop_keywords if k)

    def _in_work_hours(self) -> bool:
        h = datetime.now().hour
        return self._rc_work_start_hour <= h < self._rc_work_end_hour

    def _today_success_comment_count(self) -> int:
        """按本地日统计今日成功评论数（全局守护阈值）。"""
        now = datetime.now()
        start = datetime(now.year, now.month, now.day)
        end = start + timedelta(days=1)
        with get_session() as session:
            return (
                session.query(ReplyLog)
                .filter(
                    ReplyLog.reply_type == "comment",
                    ReplyLog.success == True,
                    ReplyLog.replied_at >= start,
                    ReplyLog.replied_at < end,
                )
                .count()
            )

    # ──────────────────────────────────────────────
    # 批量回复主入口
    # ──────────────────────────────────────────────
    def run_batch(
        self,
        max_count: Optional[int] = None,
        keyword: Optional[str] = None,
        dry_run: bool = False,
        force_all: bool = False,
        cancel_flag=None,
        log_fn=None,
    ) -> dict:
        """
        执行一批回复任务。
        cancel_flag 为 TaskRecord 实例，检测 .cancelled 属性。
        log_fn 为前端日志回调函数，接受一个字符串参数。
        """
        def _log(msg: str):
            logger.info(msg)
            if log_fn:
                log_fn(msg)

        max_count = max_count or config.get("scheduler.max_replies_per_batch", 5)
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
            _log("没有待回复的热门帖子")
            return {"total": 0, "comment_ok": 0, "dm_ok": 0, "failed": 0}

        _log(f"本批待回复帖子数：{len(posts)}")

        stats = {"total": len(posts), "comment_ok": 0, "dm_ok": 0, "failed": 0}

        for i, post in enumerate(posts):
            # 检测取消信号
            if cancel_flag and getattr(cancel_flag, 'cancelled', False):
                _log("⛔ 回复任务已被用户停止")
                break
            if self._fatal_stop:
                _log("⛔ 任务已因账号风控终止，请更换账号后再试")
                break

            _log(
                f"[{i+1}/{len(posts)}] 处理帖子「{post.title or post.note_id}」"
                f"  score={post.hot_score:.1f}"
            )

            comment_ok = False
            dm_ok = False

            # 发评论
            if self._enable_comment:
                comment_ok = self._reply_comment(post, dry_run, log_fn=log_fn)
                if comment_ok:
                    stats["comment_ok"] += 1
                    consecutive_failures = 0
                else:
                    stats["failed"] += 1
                    consecutive_failures += 1

                if self._rc_enabled and consecutive_failures >= self._rc_max_consecutive_failures:
                    _log(
                        f"⛔ 连续失败 {consecutive_failures} 次，触发风控熔断，停止本批次"
                    )
                    break

                if i < len(posts) - 1:
                    self._random_sleep(self._reply_interval)

            # 发私信
            if self._enable_dm:
                dm_ok = self._reply_dm(post, dry_run)
                if dm_ok:
                    stats["dm_ok"] += 1

                if i < len(posts) - 1:
                    self._random_sleep(self._dm_interval)

            # 更新帖子状态
            self._update_post_status(post, comment_ok, dm_ok)

        _log(
            f"批次完成：共 {stats['total']} 条 | "
            f"评论成功 {stats['comment_ok']} | "
            f"私信成功 {stats['dm_ok']} | "
            f"失败 {stats['failed']}"
        )
        return stats

    # ──────────────────────────────────────────────
    # 评论回复
    # ──────────────────────────────────────────────
    def _reply_comment(self, post: Post, dry_run: bool, log_fn=None) -> bool:
        """向帖子发一条评论，返回是否成功"""
        def _log(msg):
            logger.info(msg)
            if log_fn: log_fn(msg)

        tpl = self._tmpl_mgr.pick_random("comment", keyword=post.keyword)
        if not tpl:
            _log(f"  [评论] 无可用模板，跳过帖子 {post.note_id}")
            return False

        content = self._tmpl_mgr.render_content(
            tpl.content,
            keyword=post.keyword,
            author_name=post.author_name,
            used_texts=self._batch_used_texts,
        )
        _prev = (content or "")[:40] or "（无文字）"
        _log(f"  [评论] 使用模板「{tpl.name}」: {_prev}…")

        # 解析配图路径：模板内 JSON 优先，否则用 config 全局列表
        tpl_paths: list[str] = []
        tpl_image_parse_error = False
        raw_ip = getattr(tpl, "image_paths", None)
        if raw_ip:
            try:
                ar = json.loads(raw_ip) if isinstance(raw_ip, str) else raw_ip
                if isinstance(ar, list):
                    tpl_paths = [str(x).strip() for x in ar if str(x).strip()]
                else:
                    tpl_image_parse_error = True
            except Exception:
                tpl_image_parse_error = True
        global_paths = list(config.get("reply.comment_image_paths") or [])
        image_rel_paths = tpl_paths if tpl_paths else global_paths
        use_images = bool(image_rel_paths) and (
            bool(tpl_paths) or bool(config.get("reply.comment_with_images", False))
        )
        wanted_images = bool(raw_ip and str(raw_ip).strip()) or bool(
            config.get("reply.comment_with_images", False) and global_paths
        )

        if tpl_image_parse_error:
            err_msg = "模板配图字段 image_paths 不是合法 JSON 数组，已拒绝发送"
            _log(f"  [评论] 发送失败: {err_msg}")
            self._save_log(post.note_id, tpl.id, "comment", content, False, err_msg)
            self._update_post_error(post, err_msg)
            return False

        if self._image_comment_strict and wanted_images and not use_images:
            err_msg = "已配置图文评论，但未解析到有效图片路径，已拒绝降级为纯文本发送"
            _log(f"  [评论] 发送失败: {err_msg}")
            self._save_log(post.note_id, tpl.id, "comment", content, False, err_msg)
            self._update_post_error(post, err_msg)
            return False
        if use_images:
            _log(f"  [评论] 配图 {len(image_rel_paths)} 张（Web 端是否支持带图评论以接口为准）")

        if dry_run:
            _log("  [评论][DRY-RUN] 不实际发送")
            self._save_log(post.note_id, tpl.id, "comment", content, True)
            self._tmpl_mgr.record_used(tpl.id)
            return True

        # 发送前随机等待，模拟人工操作
        self._random_sleep(self._delay_min, self._delay_max)

        try:
            client = self._get_client()
            if use_images:
                local_paths = resolve_disk_paths(image_rel_paths)
                entries = build_image_entries(client, local_paths)
                ph = (content or "").strip() or (
                    config.get("reply.comment_image_placeholder") or "[图片]"
                )
                post_web_comment(client, post.note_id, ph, entries)
                self._save_log(post.note_id, tpl.id, "comment", f"{content}\n[配图×{len(local_paths)}]", True)
            else:
                client.comment_note(post.note_id, content)
                self._save_log(post.note_id, tpl.id, "comment", content, True)
            self._tmpl_mgr.record_used(tpl.id)
            _log(f"  [评论] 发送成功 ✓")
            return True
        except Exception as e:
            err_msg = str(e)
            _log(f"  [评论] 发送失败: {err_msg}")
            self._handle_fatal_ban(err_msg, log_fn=log_fn)
            if self._rc_enabled and self._is_risk_error(err_msg):
                self._fatal_stop = True
                _log("⛔ 命中高风险关键词，已自动停止本批次")
            self._save_log(post.note_id, tpl.id, "comment", content, False, err_msg)
            self._update_post_error(post, err_msg)
            return False

    # ──────────────────────────────────────────────
    # 私信回复
    # ──────────────────────────────────────────────
    def _reply_dm(self, post: Post, dry_run: bool) -> bool:
        """向帖子作者发一条私信，返回是否成功"""
        if not post.author_id:
            logger.warning(f"  [私信] 帖子 {post.note_id} 无作者ID，跳过")
            return False

        tpl = self._tmpl_mgr.pick_random("dm", keyword=post.keyword)
        if not tpl:
            logger.warning(f"  [私信] 无可用模板，跳过帖子 {post.note_id}")
            return False

        content = self._tmpl_mgr.render_content(
            tpl.content,
            keyword=post.keyword,
            author_name=post.author_name,
        )
        logger.info(f"  [私信] 使用模板 [{tpl.id}] {tpl.name}: {content[:30]}...")

        if dry_run:
            logger.info("  [私信][DRY-RUN] 不实际发送")
            self._save_log(post.note_id, tpl.id, "dm", content, True)
            self._tmpl_mgr.record_used(tpl.id)
            return True

        self._random_sleep(self._delay_min, self._delay_max)

        try:
            client = self._get_client()
            client.send_note_to_private_msg(post.author_id, post.note_id)
            self._save_log(post.note_id, tpl.id, "dm", content, True)
            self._tmpl_mgr.record_used(tpl.id)
            logger.success(f"  [私信] 发送成功 ✓")
            return True
        except Exception as e:
            err_msg = str(e)
            logger.error(f"  [私信] 发送失败: {err_msg}")
            self._handle_fatal_ban(err_msg)
            if self._rc_enabled and self._is_risk_error(err_msg):
                self._fatal_stop = True
            self._save_log(post.note_id, tpl.id, "dm", content, False, err_msg)
            return False

    # ──────────────────────────────────────────────
    # 数据库写回
    # ──────────────────────────────────────────────
    def _update_post_status(self, post: Post, comment_ok: bool, dm_ok: bool) -> None:
        """根据回复结果更新帖子状态"""
        with get_session() as session:
            db_post = session.get(Post, post.id)
            if not db_post:
                return

            if comment_ok and dm_ok:
                db_post.reply_status = ReplyStatus.REPLIED_BOTH
            elif comment_ok:
                db_post.reply_status = ReplyStatus.REPLIED_COMMENT
            elif dm_ok:
                db_post.reply_status = ReplyStatus.REPLIED_DM
            else:
                db_post.reply_status = ReplyStatus.FAILED

            if comment_ok:
                db_post.replied_comment_at = datetime.utcnow()
            if dm_ok:
                db_post.replied_dm_at = datetime.utcnow()

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

    # ──────────────────────────────────────────────
    # 工具方法
    # ──────────────────────────────────────────────
    @staticmethod
    def _random_sleep(base: float, upper: Optional[float] = None) -> None:
        """随机等待，若 upper 为 None 则在 [base*0.8, base*1.2] 范围内"""
        if upper is None:
            lo, hi = base * 0.8, base * 1.2
        else:
            lo, hi = base, upper
        sleep_time = random.uniform(lo, hi)
        logger.debug(f"  等待 {sleep_time:.1f}s ...")
        time.sleep(sleep_time)
