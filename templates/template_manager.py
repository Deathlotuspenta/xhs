"""模板库管理模块

负责从数据库加载、随机抽取、添加、编辑、导入模板。
"""
from __future__ import annotations

import json
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

import config
from database import get_session, Template


DEFAULT_TEMPLATES_PATH = Path(__file__).parent / "default_templates.json"


class TemplateManager:
    """模板管理器"""

    def __init__(self) -> None:
        self._cooldown: int = config.get("reply.template_cooldown", 10)
        # 最近使用的模板 ID 队列（用于冷却）
        self._recent_ids: list[int] = []

    # ──────────────────────────────────────────────
    # 初始化 / 导入
    # ──────────────────────────────────────────────
    def ensure_defaults(self) -> int:
        """
        确保默认模板已导入数据库。
        如果数据库中一条模板都没有，则从 default_templates.json 导入。
        :return: 导入的模板数量
        """
        with get_session() as session:
            count = session.query(Template).count()
            if count > 0:
                return 0

        return self.import_from_json(DEFAULT_TEMPLATES_PATH)

    def import_from_json(self, path: str | Path) -> int:
        """从 JSON 文件批量导入模板，返回成功导入数量"""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"模板文件不存在: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            raise ValueError("模板 JSON 文件应为数组格式")

        imported = 0
        with get_session() as session:
            for item in data:
                ip = item.get("image_paths")
                if isinstance(ip, list):
                    ip = json.dumps(ip, ensure_ascii=False)
                tpl = Template(
                    name=item.get("name", "未命名"),
                    content=item.get("content", ""),
                    template_type=item.get("type", "comment"),
                    tags=item.get("tags", ""),
                    keywords=item.get("keywords", ""),
                    image_paths=ip if isinstance(ip, str) else None,
                    enabled=item.get("enabled", True),
                )
                if isinstance(tpl.tags, list):
                    tpl.tags = ",".join(tpl.tags)
                session.add(tpl)
                imported += 1
            session.commit()

        logger.info(f"从 {path.name} 导入了 {imported} 条模板")
        return imported

    # ──────────────────────────────────────────────
    # 随机抽取模板
    # ──────────────────────────────────────────────
    def pick_random(
        self,
        template_type: str = "comment",
        keyword: Optional[str] = None,
    ) -> Optional[Template]:
        """
        随机抽取一条模板，优先匹配关键词，避免短期重复。

        :param template_type: 'comment' 或 'dm'
        :param keyword:        帖子关键词，用于优先匹配相关模板
        :return: Template 对象，或 None（没有可用模板）
        """
        with get_session() as session:
            candidates = (
                session.query(Template)
                .filter(
                    Template.template_type == template_type,
                    Template.enabled == True,
                )
                .all()
            )

        if not candidates:
            logger.warning(f"没有找到类型为 [{template_type}] 的可用模板")
            return None

        # 先尝试关键词优先匹配
        matched: list[Template] = []
        if keyword:
            for tpl in candidates:
                kw_str = tpl.keywords or ""
                if any(k.strip() in keyword for k in kw_str.split(",") if k.strip()):
                    matched.append(tpl)

        pool = matched if matched else candidates

        # 排除最近使用过的（冷却）
        non_cold = [t for t in pool if t.id not in self._recent_ids]
        if non_cold:
            pool = non_cold

        chosen = random.choice(pool)
        self._update_cooldown(chosen.id)
        return chosen

    def _update_cooldown(self, template_id: int) -> None:
        """维护最近使用队列"""
        self._recent_ids.append(template_id)
        if len(self._recent_ids) > self._cooldown:
            self._recent_ids.pop(0)

    def record_used(self, template_id: int) -> None:
        """更新模板使用次数和最近使用时间"""
        with get_session() as session:
            tpl = session.get(Template, template_id)
            if tpl:
                tpl.use_count = (tpl.use_count or 0) + 1
                tpl.last_used_at = datetime.utcnow()
                session.commit()

    # ──────────────────────────────────────────────
    # 文本变体渲染
    # ──────────────────────────────────────────────
    def render_content(
        self,
        content: str,
        *,
        keyword: Optional[str] = None,
        author_name: Optional[str] = None,
        used_texts: Optional[set[str]] = None,
        max_retry: int = 6,
    ) -> str:
        """
        将模板内容渲染为变体文本。

        支持：
        1) 词槽随机：{你好|哈喽|嗨}
        2) 占位符：{{keyword}}、{{author}}
        3) 批次去重：若 used_texts 提供，则尽量避免同批次重复
        """
        base = (content or "").strip()
        if not base:
            return ""

        def _expand_spintax(text: str) -> str:
            # 只做一层选择，简单稳定：{a|b|c}
            pattern = re.compile(r"\{([^{}]+)\}")

            def repl(m: re.Match) -> str:
                inner = m.group(1)
                if "|" not in inner:
                    return m.group(0)
                opts = [x.strip() for x in inner.split("|") if x.strip()]
                return random.choice(opts) if opts else ""

            return pattern.sub(repl, text)

        def _replace_vars(text: str) -> str:
            out = text
            out = out.replace("{{keyword}}", (keyword or "").strip())
            out = out.replace("{{author}}", (author_name or "").strip() or "你")
            # 清理因空变量造成的多余空格
            out = re.sub(r"\s{2,}", " ", out).strip()
            return out

        tries = max(1, max_retry)
        for _ in range(tries):
            candidate = _replace_vars(_expand_spintax(base))
            if not used_texts or candidate not in used_texts:
                if used_texts is not None:
                    used_texts.add(candidate)
                return candidate

        # 重试后仍重复，保底返回最后一次
        final_text = _replace_vars(_expand_spintax(base))
        if used_texts is not None:
            used_texts.add(final_text)
        return final_text

    # ──────────────────────────────────────────────
    # CRUD
    # ──────────────────────────────────────────────
    def add_template(
        self,
        name: str,
        content: str,
        template_type: str = "comment",
        tags: str = "",
        keywords: str = "",
        image_paths: Optional[str] = None,
    ) -> Template:
        """添加一条模板"""
        with get_session() as session:
            tpl = Template(
                name=name,
                content=content,
                template_type=template_type,
                tags=tags,
                keywords=keywords,
                image_paths=(image_paths.strip() or None) if image_paths else None,
                enabled=True,
            )
            session.add(tpl)
            session.commit()
            session.refresh(tpl)
            logger.info(f"新增模板: [{tpl.id}] {tpl.name}")
            return tpl

    def list_templates(self, template_type: Optional[str] = None) -> list[Template]:
        """列出所有（或指定类型的）模板"""
        with get_session() as session:
            q = session.query(Template)
            if template_type:
                q = q.filter(Template.template_type == template_type)
            return q.order_by(Template.id).all()

    def toggle_template(self, template_id: int) -> bool:
        """切换模板启用/禁用状态，返回新状态"""
        with get_session() as session:
            tpl = session.get(Template, template_id)
            if not tpl:
                raise ValueError(f"模板 ID {template_id} 不存在")
            tpl.enabled = not tpl.enabled
            session.commit()
            return tpl.enabled

    def delete_template(self, template_id: int) -> None:
        """删除一条模板"""
        with get_session() as session:
            tpl = session.get(Template, template_id)
            if tpl:
                session.delete(tpl)
                session.commit()
                logger.info(f"删除模板: [{template_id}]")
