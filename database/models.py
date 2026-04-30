"""数据库模型定义（SQLite + SQLAlchemy）"""
from __future__ import annotations

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

import config


class Base(DeclarativeBase):
    pass


class ReplyStatus(str, PyEnum):
    PENDING = "pending"       # 待回复
    REPLIED_COMMENT = "replied_comment"   # 已发评论
    REPLIED_DM = "replied_dm"             # 已发私信
    REPLIED_BOTH = "replied_both"         # 评论+私信都发了
    SKIPPED = "skipped"       # 跳过（低热度/黑名单）
    FAILED = "failed"         # 回复失败


class Post(Base):
    """小红书帖子"""
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    note_id = Column(String(64), unique=True, nullable=False, index=True, comment="帖子ID")
    title = Column(String(512), nullable=True, comment="帖子标题")
    desc = Column(Text, nullable=True, comment="帖子正文")
    author_id = Column(String(64), nullable=True, comment="作者ID")
    author_name = Column(String(128), nullable=True, comment="作者昵称")
    url = Column(String(512), nullable=True, comment="帖子链接")
    keyword = Column(String(128), nullable=True, comment="搜索关键词")
    type = Column(String(32), nullable=True, comment="帖子类型 normal/video")

    # 互动数据
    likes = Column(Integer, default=0, comment="点赞数")
    comments = Column(Integer, default=0, comment="评论数")
    collects = Column(Integer, default=0, comment="收藏数")
    shares = Column(Integer, default=0, comment="分享数")
    hot_score = Column(Float, default=0.0, comment="热度综合评分(0-100)")

    # 状态
    reply_status = Column(
        Enum(ReplyStatus),
        default=ReplyStatus.PENDING,
        nullable=False,
        index=True,
        comment="回复状态"
    )
    comment_template_id = Column(Integer, nullable=True, comment="使用的评论模板ID")
    dm_template_id = Column(Integer, nullable=True, comment="使用的私信模板ID")
    replied_comment_at = Column(DateTime, nullable=True, comment="评论时间")
    replied_dm_at = Column(DateTime, nullable=True, comment="私信时间")
    reply_error = Column(Text, nullable=True, comment="回复失败原因")

    # 元数据
    crawled_at = Column(DateTime, default=datetime.utcnow, comment="抓取时间")
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment="最后更新时间"
    )

    def __repr__(self) -> str:
        return f"<Post {self.note_id} score={self.hot_score:.1f} status={self.reply_status}>"


class Template(Base):
    """回复模板"""
    __tablename__ = "templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, comment="模板名称")
    content = Column(Text, nullable=False, comment="模板内容")
    template_type = Column(
        String(32), nullable=False, default="comment", comment="模板类型: comment/dm"
    )
    tags = Column(String(256), nullable=True, comment="标签，逗号分隔，用于关键词匹配")
    keywords = Column(String(512), nullable=True, comment="适用关键词，逗号分隔")
    image_paths = Column(Text, nullable=True, comment='配图路径 JSON 数组，相对项目根，如 ["assets/a.jpg"]')
    enabled = Column(Boolean, default=True, comment="是否启用")
    use_count = Column(Integer, default=0, comment="使用次数")
    last_used_at = Column(DateTime, nullable=True, comment="最近使用时间")
    created_at = Column(DateTime, default=datetime.utcnow, comment="创建时间")

    def __repr__(self) -> str:
        return f"<Template {self.id} [{self.template_type}] {self.name}>"


class ReplyLog(Base):
    """回复日志"""
    __tablename__ = "reply_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    post_note_id = Column(String(64), nullable=False, index=True, comment="帖子ID")
    template_id = Column(Integer, nullable=True, comment="模板ID")
    reply_type = Column(String(32), nullable=False, comment="回复类型: comment/dm")
    content = Column(Text, nullable=False, comment="实际发送内容")
    success = Column(Boolean, default=True, comment="是否成功")
    error_msg = Column(Text, nullable=True, comment="失败信息")
    replied_at = Column(DateTime, default=datetime.utcnow, comment="回复时间")

    def __repr__(self) -> str:
        return f"<ReplyLog post={self.post_note_id} type={self.reply_type} ok={self.success}>"


class CrawlTask(Base):
    """抓取任务记录"""
    __tablename__ = "crawl_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    keyword = Column(String(128), nullable=False, comment="搜索关键词")
    pages = Column(Integer, default=1, comment="抓取页数")
    posts_found = Column(Integer, default=0, comment="发现帖子数")
    posts_new = Column(Integer, default=0, comment="新增帖子数")
    status = Column(String(32), default="running", comment="状态: running/done/failed")
    error_msg = Column(Text, nullable=True, comment="失败信息")
    started_at = Column(DateTime, default=datetime.utcnow, comment="开始时间")
    finished_at = Column(DateTime, nullable=True, comment="完成时间")

    def __repr__(self) -> str:
        return f"<CrawlTask keyword={self.keyword} new={self.posts_new} status={self.status}>"


class Account(Base):
    """小红书账号池"""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone = Column(String(32), nullable=True, comment="手机号/账号标识")
    cookie_str = Column(Text, nullable=False, comment="Cookie字符串")
    status = Column(String(32), default="active", comment="状态: active/invalid/banned")
    use_count = Column(Integer, default=0, comment="使用次数")
    last_used_at = Column(DateTime, nullable=True, comment="最后使用时间")
    created_at = Column(DateTime, default=datetime.utcnow, comment="创建时间")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment="更新时间")

    def __repr__(self) -> str:
        return f"<Account {self.id} status={self.status}>"


# ──────────────────────────────────────────────────────────────
# 引擎 & 会话工厂
# ──────────────────────────────────────────────────────────────
_engine = None
_SessionFactory = None


def get_engine():
    global _engine
    if _engine is None:
        db_path = config.DB_PATH
        _engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(conn, _):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(_engine)
        _migrate_sqlite_templates_image_paths(_engine)
    return _engine


def _migrate_sqlite_templates_image_paths(engine) -> None:
    """为旧库增加 templates.image_paths 列（SQLite 无自动迁移）。"""
    from sqlalchemy import text

    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(templates)")).fetchall()
        cols = {r[1] for r in rows}
        if "image_paths" not in cols:
            conn.execute(text("ALTER TABLE templates ADD COLUMN image_paths TEXT"))


def get_session() -> Session:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionFactory()
