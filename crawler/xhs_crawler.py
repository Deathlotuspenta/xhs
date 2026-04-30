"""小红书爬虫模块

使用 xhs 库（非官方 API）抓取帖子，依赖 Cookie 登录态。
"""
from __future__ import annotations

import time
import random
from datetime import datetime
from typing import Any

from loguru import logger

import config
from database import get_session, Post, CrawlTask, ReplyStatus
from analyzer.hot_analyzer import HotAnalyzer


class XhsCrawler:
    """小红书帖子爬虫"""

    def __init__(self, log_fn=None) -> None:
        self._client = None
        self._analyzer = HotAnalyzer()
        self._log_fn = log_fn  # 前端日志回调

    def _log(self, msg: str) -> None:
        logger.info(msg)
        if self._log_fn:
            self._log_fn(msg)

    # ──────────────────────────────────────────────
    # 初始化 XHS 客户端
    # ──────────────────────────────────────────────
    def _get_client(self):
        """懒加载 xhs 客户端，从账号池获取有效 Cookie"""
        if self._client is not None:
            return self._client

        try:
            from xhs import XhsClient
        except ImportError as e:
            raise RuntimeError("请先安装 xhs 库：pip install xhs") from e

        from crawler.account_pool import pick_cookie_str
        cookies = pick_cookie_str() or config.XHS_COOKIES
                
        if not cookies:
            raise ValueError(
                "没有可用的账号 Cookie！\n"
                "请先在账号池添加账号，或在 config.yaml 填写 xhs.cookies"
            )

        # 检查 Cookie 是否包含 web_session（登录态的关键字段）
        cookie_kv = {p.split("=", 1)[0].strip(): p.split("=", 1)[1].strip()
                     for p in cookies.split(";") if "=" in p}
        if not cookie_kv.get("web_session"):
            raise ValueError(
                "账号 Cookie 中缺少 web_session，说明登录未完成或 Cookie 已失效！\n"
                "请在「账号管理」中删除旧账号，重新扫码登录。"
            )

        # 使用 xhshow 纯算法签名（参考 MediaCrawler 项目，无需浏览器/JS逆向）
        from crawler.xhs_sign import make_sign_func
        sign_func = make_sign_func(cookies)

        self._client = XhsClient(cookie=cookies, sign=sign_func)
        logger.info("XhsClient 初始化成功（xhshow 纯算法签名）")
        return self._client

    @staticmethod
    def _parse_cookies(cookie_str: str) -> dict[str, str]:
        """将 'a=1; b=2' 格式的 Cookie 字符串解析为字典"""
        result = {}
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                result[k.strip()] = v.strip()
        return result

    @staticmethod
    def _sign_request(uri: str, data: Any, a1: str = "", web_session: str = "") -> dict:
        """
        请求签名函数。
        xhs 官方签名需要 Node.js 环境（执行 JS 签名脚本），
        这里提供一个占位实现，实际使用时替换为真实签名逻辑。
        参考：https://github.com/ReaJason/xhs/blob/master/xhs/help.py
        """
        try:
            import subprocess, json
            # 如果本地有签名脚本，调用它
            # 此处为示例，实际根据 xhs 库版本调整
            return {"x-s": "", "x-t": str(int(time.time() * 1000))}
        except Exception:
            return {"x-s": "", "x-t": str(int(time.time() * 1000))}

    # ──────────────────────────────────────────────
    # 核心抓取方法
    # ──────────────────────────────────────────────
    def crawl_keyword(self, keyword: str, pages: int | None = None) -> int:
        """
        搜索指定关键词并将帖子存入数据库。

        :param keyword: 搜索关键词
        :param pages:   抓取页数（None 则读配置）
        :return: 新增帖子数量
        """
        pages = pages or config.get("xhs.search_pages", 3)
        delay = config.get("xhs.request_delay", 3)

        client = self._get_client()
        task = self._start_task(keyword, pages)

        total_found = 0
        total_new = 0

        try:
            for page in range(1, pages + 1):
                self._log(f"[{keyword}] 正在抓取第 {page}/{pages} 页 ...")
                try:
                    notes = self._fetch_page(client, keyword, page)
                except Exception as e:
                    self._log(f"[{keyword}] 第 {page} 页抓取失败: {e}")
                    break

                for raw_note in notes:
                    total_found += 1
                    new = self._save_note(raw_note, keyword)
                    if new:
                        total_new += 1

                # 随机延迟，避免风控
                sleep_time = delay + random.uniform(0, delay * 0.5)
                logger.debug(f"等待 {sleep_time:.1f}s ...")
                time.sleep(sleep_time)

            self._finish_task(task, total_found, total_new, "done")
            self._log(f"[{keyword}] 抓取完成：共 {total_found} 条，新增 {total_new} 条")

        except Exception as e:
            self._finish_task(task, total_found, total_new, "failed", str(e))
            self._log(f"[{keyword}] 抓取异常: {e}")
            raise

        return total_new

    def crawl_all_keywords(self) -> dict[str, int]:
        """按配置中的关键词列表依次抓取"""
        keywords: list[str] = config.get("xhs.keywords", [])
        if not keywords:
            logger.warning("config.yaml 中 xhs.keywords 为空，跳过抓取")
            return {}

        results = {}
        for kw in keywords:
            try:
                new_count = self.crawl_keyword(kw)
                results[kw] = new_count
            except Exception as e:
                logger.error(f"关键词 [{kw}] 抓取失败: {e}")
                results[kw] = 0
        return results

    # ──────────────────────────────────────────────
    # 内部辅助方法
    # ──────────────────────────────────────────────
    def _fetch_page(self, client, keyword: str, page: int) -> list[dict]:
        """调用 xhs API 搜索帖子，返回原始数据列表"""
        result = client.get_note_by_keyword(keyword, page=page, page_size=20)
        items = result.get("items", [])
        notes = []
        for item in items:
            # XHS 搜索结果：item 内嵌 note_card
            note_card = (
                item.get("note_card")
                or item.get("noteCard")
                or item.get("note")
            )
            if note_card and isinstance(note_card, dict):
                # 把外层 item 的部分字段补充到 note_card（如 id 做备用）
                if not note_card.get("id") and not note_card.get("note_id"):
                    outer_id = item.get("id", "")
                    # 外层 id 格式为 UUID#timestamp，取 # 前部分
                    note_card["_outer_id"] = outer_id.split("#")[0] if outer_id else ""
                logger.debug(f"note_card keys: {list(note_card.keys())[:8]}")
                notes.append(note_card)
            else:
                # 兜底：item 本身当做 note 使用，记录 key 以便排查
                logger.debug(f"no note_card found, item keys: {list(item.keys())[:8]}")
                notes.append(item)
        if not notes and items:
            logger.warning(f"搜索返回 {len(items)} 条但解析为 0 条，首条 item keys: {list(items[0].keys())}")
        return notes

    @staticmethod
    def _clean_note_id(raw_id: str) -> str:
        """
        XHS 搜索 API 的 item.id 格式为 'UUID#timestamp'，
        note_card 内部的 id 才是真正的 note_id。
        本函数去掉 '#...' 后缀，并跳过明显无效的 ID。
        """
        if not raw_id:
            return ""
        # 去掉 '#timestamp' 后缀
        clean = raw_id.split("#")[0].strip()
        # 过滤掉纯 UUID 格式（带 '-'）作为 item-level ID 时，
        # note_card 里的 note_id / id 通常是 24 位 hex，不含横杠
        # 不再过滤，让上层通过 note_id 字段优先来规避
        return clean

    def _save_note(self, raw: dict, keyword: str) -> bool:
        """
        将原始帖子数据解析并存入数据库。
        :return: True 表示是新增帖子，False 表示已存在（已更新热度）
        """
        # ── note_id：优先用 note_id/noteId 字段，其次 id，去掉 #timestamp 后缀
        raw_id = (
            raw.get("note_id")
            or raw.get("noteId")
            or raw.get("id")
            or raw.get("_outer_id")
            or ""
        )
        note_id = self._clean_note_id(str(raw_id)) if raw_id else ""
        if not note_id:
            return False

        # ── title：优先 display_title，其次 title
        title = (
            raw.get("display_title")
            or raw.get("displayTitle")
            or raw.get("title")
            or raw.get("desc")
            or ""
        ).strip()

        # ── URL：直接存 XHS 帖子 URL
        url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_source=pc_search"

        interact = raw.get("interact_info") or raw.get("interactInfo") or {}
        # XHS API 的互动数为字符串（如 "1.2万"、"99"），_parse_num 统一转 int
        likes    = self._parse_num(interact.get("liked_count")     or interact.get("likedCount")     or 0)
        comments = self._parse_num(interact.get("comment_count")   or interact.get("commentCount")   or 0)
        collects = self._parse_num(interact.get("collected_count") or interact.get("collectedCount") or 0)
        shares   = self._parse_num(interact.get("share_count")     or interact.get("shareCount")     or 0)
        hot_score = self._analyzer.score(likes, comments, collects, shares)

        logger.debug(
            f"  note_id={note_id} title={title[:20]!r} likes={likes} comments={comments} score={hot_score:.1f}"
        )

        desc = raw.get("desc") or raw.get("description") or ""
        user = raw.get("user") or raw.get("author") or {}
        author_id = user.get("user_id") or user.get("userId") or user.get("id") or ""
        author_name = user.get("nickname") or user.get("name") or ""
        note_type = raw.get("type") or "normal"

        with get_session() as session:
            existing = session.query(Post).filter_by(note_id=note_id).first()
            if existing:
                # 更新互动数据和热度
                existing.likes = likes
                existing.comments = comments
                existing.collects = collects
                existing.shares = shares
                existing.hot_score = hot_score
                existing.updated_at = datetime.utcnow()
                session.commit()
                return False
            else:
                post = Post(
                    note_id=note_id,
                    title=title[:500] if title else "",
                    desc=desc[:2000] if desc else "",
                    author_id=author_id,
                    author_name=author_name,
                    url=url,
                    keyword=keyword,
                    type=note_type,
                    likes=likes,
                    comments=comments,
                    collects=collects,
                    shares=shares,
                    hot_score=hot_score,
                    reply_status=ReplyStatus.PENDING,
                )
                session.add(post)
                session.commit()
                return True

    @staticmethod
    def _parse_num(val: Any) -> int:
        """将 '1.2万' / '999' / 1234 等格式统一转为整数"""
        if isinstance(val, int):
            return val
        if isinstance(val, float):
            return int(val)
        s = str(val).strip().replace(",", "")
        if s.endswith("万"):
            try:
                return int(float(s[:-1]) * 10000)
            except ValueError:
                return 0
        try:
            return int(float(s))
        except ValueError:
            return 0

    # ──────────────────────────────────────────────
    # 任务记录辅助
    # ──────────────────────────────────────────────
    def _start_task(self, keyword: str, pages: int) -> CrawlTask:
        with get_session() as session:
            task = CrawlTask(keyword=keyword, pages=pages, status="running")
            session.add(task)
            session.commit()
            session.refresh(task)
            return task

    def _finish_task(
        self, task: CrawlTask, found: int, new: int, status: str, error: str = ""
    ) -> None:
        with get_session() as session:
            db_task = session.get(CrawlTask, task.id)
            if db_task:
                db_task.posts_found = found
                db_task.posts_new = new
                db_task.status = status
                db_task.error_msg = error or None
                db_task.finished_at = datetime.utcnow()
                session.commit()
