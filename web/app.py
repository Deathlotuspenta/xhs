"""FastAPI Web 应用 - 小红书自动化回复系统"""
from __future__ import annotations

import asyncio
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from loguru import logger

# Windows 下必须使用 ProactorEventLoop，否则 Playwright 无法创建子进程
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# 确保项目根目录在 sys.path 中
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, File, HTTPException, BackgroundTasks, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import get_engine, get_session, Post, Template, ReplyLog, ReplyStatus
from analyzer.hot_analyzer import HotAnalyzer
from templates.template_manager import TemplateManager
from web.state import task_manager, get_scheduler
import config

get_engine()  # 确保表存在


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时可选自动开启 APScheduler；关闭进程时停止调度器。"""
    if config.get("scheduler.auto_start", True):
        try:
            get_scheduler().start()
            logger.info("scheduler.auto_start=true，定时调度器已自动启动")
        except Exception as e:
            logger.exception(f"自动启动定时调度器失败: {e}")
    yield
    sch = get_scheduler()
    if sch.is_running():
        sch.stop()
        logger.info("应用退出，定时调度器已停止")


app = FastAPI(title="XHS Bot", docs_url="/api/docs", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ═══════════════════════════════════════════════════════
# 页面路由
# ═══════════════════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ═══════════════════════════════════════════════════════
# 统计 API
# ═══════════════════════════════════════════════════════
@app.get("/api/stats")
async def get_stats():
    analyzer = HotAnalyzer()
    stats = analyzer.get_stats()

    # 各关键词帖子数
    with get_session() as session:
        from sqlalchemy import func
        kw_rows = (
            session.query(Post.keyword, func.count(Post.id))
            .group_by(Post.keyword)
            .all()
        )
        kw_stats = [{"keyword": r[0] or "未知", "count": r[1]} for r in kw_rows]

        # 最近7天新增帖子（按天）
        from sqlalchemy import cast, Date
        from datetime import date, timedelta
        today = date.today()
        daily = []
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            cnt = session.query(Post).filter(
                cast(Post.crawled_at, Date) == d
            ).count()
            daily.append({"date": d.strftime("%m/%d"), "count": cnt})

        # 最近回复成功数
        recent_replied = session.query(Post).filter(
            Post.reply_status.in_([
                ReplyStatus.REPLIED_COMMENT,
                ReplyStatus.REPLIED_DM,
                ReplyStatus.REPLIED_BOTH,
            ])
        ).order_by(Post.updated_at.desc()).limit(5).all()
        recent_list = [
            {
                "note_id": p.note_id,
                "title": p.title or "",
                "score": p.hot_score,
                "status": p.reply_status.value,
                "keyword": p.keyword or "",
            }
            for p in recent_replied
        ]

    return {
        **stats,
        "kw_stats": kw_stats,
        "daily_new": daily,
        "recent_replied": recent_list,
    }


# ═══════════════════════════════════════════════════════
# 帖子 API
# ═══════════════════════════════════════════════════════
@app.get("/api/posts")
async def list_posts(
    page: int = 1,
    size: int = 20,
    status: str = "",
    keyword: str = "",
    min_score: float = 0,
    sort: str = "hot_score",
):
    with get_session() as session:
        q = session.query(Post)
        if status:
            try:
                q = q.filter(Post.reply_status == ReplyStatus(status))
            except ValueError:
                pass
        if keyword:
            q = q.filter(Post.keyword == keyword)
        if min_score > 0:
            q = q.filter(Post.hot_score >= min_score)

        sort_col = {
            "hot_score": Post.hot_score.desc(),
            "likes": Post.likes.desc(),
            "comments": Post.comments.desc(),
            "crawled_at": Post.crawled_at.desc(),
        }.get(sort, Post.hot_score.desc())

        total = q.count()
        items = q.order_by(sort_col).offset((page - 1) * size).limit(size).all()

    return {
        "total": total,
        "page": page,
        "size": size,
        "pages": (total + size - 1) // size,
        "items": [
            {
                "id": p.id,
                "note_id": p.note_id,
                "title": p.title or "",
                "author_name": p.author_name or "",
                "url": p.url or "",
                "keyword": p.keyword or "",
                "likes": p.likes or 0,
                "comments": p.comments or 0,
                "collects": p.collects or 0,
                "shares": p.shares or 0,
                "hot_score": round(p.hot_score or 0, 1),
                "reply_status": p.reply_status.value,
                "crawled_at": p.crawled_at.strftime("%m-%d %H:%M") if p.crawled_at else "",
                "replied_comment_at": p.replied_comment_at.strftime("%m-%d %H:%M") if p.replied_comment_at else "",
            }
            for p in items
        ],
    }


@app.get("/api/posts/keywords")
async def list_keywords():
    """返回已有的关键词列表"""
    with get_session() as session:
        from sqlalchemy import func
        rows = session.query(Post.keyword).filter(Post.keyword != None).distinct().all()
        return [r[0] for r in rows if r[0]]


@app.delete("/api/posts/{note_id}")
async def delete_post(note_id: str):
    """删除指定帖子及其回复日志"""
    with get_session() as session:
        post = session.query(Post).filter(Post.note_id == note_id).first()
        if not post:
            raise HTTPException(status_code=404, detail="帖子不存在")
        from database import ReplyLog
        session.query(ReplyLog).filter(ReplyLog.post_note_id == note_id).delete()
        session.delete(post)
        session.commit()
    return {"message": "删除成功"}


class BatchDeleteRequest(BaseModel):
    note_ids: list[str]


@app.post("/api/posts/batch-delete")
async def batch_delete_posts(req: BatchDeleteRequest):
    """批量删除帖子及其回复日志"""
    if not req.note_ids:
        raise HTTPException(status_code=400, detail="note_ids 不能为空")
    from database import ReplyLog
    with get_session() as session:
        session.query(ReplyLog).filter(ReplyLog.post_note_id.in_(req.note_ids)).delete(synchronize_session=False)
        deleted = session.query(Post).filter(Post.note_id.in_(req.note_ids)).delete(synchronize_session=False)
        session.commit()
    return {"message": f"已删除 {deleted} 条帖子", "deleted": deleted}


# ═══════════════════════════════════════════════════════
# 任务 API
# ═══════════════════════════════════════════════════════
class CrawlRequest(BaseModel):
    keyword: Optional[str] = None
    pages: Optional[int] = None


class ReplyRequest(BaseModel):
    count: Optional[int] = None
    keyword: Optional[str] = None
    dry_run: bool = False
    force_all: bool = False   # True = 忽略热度门槛，所有 pending 帖子都回复


@app.post("/api/tasks/crawl")
async def start_crawl(req: CrawlRequest):
    try:
        task_id = task_manager.submit_crawl(req.keyword, req.pages)
        return {"task_id": task_id, "message": "抓取任务已启动"}
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/tasks/reply")
async def start_reply(req: ReplyRequest):
    try:
        task_id = task_manager.submit_reply(req.count, req.keyword, req.dry_run, req.force_all)
        return {"task_id": task_id, "message": "回复任务已启动"}
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/api/tasks/status")
async def get_task_status():
    return task_manager.get_status()


@app.post("/api/tasks/{task_type}/cancel")
async def cancel_task(task_type: str):
    if task_type not in ("crawl", "reply"):
        raise HTTPException(status_code=400, detail="task_type 只能是 crawl / reply")
    ok = task_manager.cancel_task(task_type)
    if not ok:
        raise HTTPException(status_code=404, detail="没有正在运行的任务")
    return {"message": f"{task_type} 任务已停止"}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    rec = task_manager.get_task(task_id)
    if not rec:
        raise HTTPException(status_code=404, detail="任务不存在")
    return rec.to_dict()


# ═══════════════════════════════════════════════════════
# 调度器 API
# ═══════════════════════════════════════════════════════
@app.get("/api/scheduler/status")
async def scheduler_status():
    sch = get_scheduler()
    jobs = sch.list_jobs() if sch.is_running() else []
    return {
        "running": sch.is_running(),
        "jobs": jobs,
        "crawl_cron": config.get("scheduler.crawl_cron", "0 9,14,20 * * *"),
        "reply_cron": config.get("scheduler.reply_cron", "0 10,15,21 * * *"),
        "auto_start": bool(config.get("scheduler.auto_start", True)),
    }


@app.post("/api/scheduler/start")
async def start_scheduler():
    sch = get_scheduler()
    if sch.is_running():
        return {"message": "调度器已在运行中", "running": True}
    sch.start()
    return {"message": "调度器已启动", "running": True}


@app.post("/api/scheduler/stop")
async def stop_scheduler():
    sch = get_scheduler()
    if not sch.is_running():
        return {"message": "调度器未在运行", "running": False}
    sch.stop()
    return {"message": "调度器已停止", "running": False}


# ═══════════════════════════════════════════════════════
# 模板 API
# ═══════════════════════════════════════════════════════
class TemplateCreate(BaseModel):
    name: str
    content: str
    template_type: str = "comment"
    tags: str = ""
    keywords: str = ""
    image_paths: Optional[str] = None  # JSON 数组字符串，如 ["assets/a.jpg"]


class TemplateUpdate(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[str] = None
    keywords: Optional[str] = None
    image_paths: Optional[str] = None
    enabled: Optional[bool] = None


@app.get("/api/templates")
async def list_templates(template_type: str = ""):
    mgr = TemplateManager()
    mgr.ensure_defaults()
    items = mgr.list_templates(template_type if template_type else None)
    return [
        {
            "id": t.id,
            "name": t.name,
            "content": t.content,
            "template_type": t.template_type,
            "tags": t.tags or "",
            "keywords": t.keywords or "",
            "image_paths": t.image_paths or "",
            "enabled": t.enabled,
            "use_count": t.use_count or 0,
            "last_used_at": t.last_used_at.strftime("%m-%d %H:%M") if t.last_used_at else "",
        }
        for t in items
    ]


@app.post("/api/templates")
async def create_template(body: TemplateCreate):
    mgr = TemplateManager()
    tpl = mgr.add_template(
        body.name,
        body.content,
        body.template_type,
        body.tags,
        body.keywords,
        image_paths=body.image_paths,
    )
    return {"id": tpl.id, "message": "模板已创建"}


@app.put("/api/templates/{template_id}")
async def update_template(template_id: int, body: TemplateUpdate):
    with get_session() as session:
        tpl = session.get(Template, template_id)
        if not tpl:
            raise HTTPException(status_code=404, detail="模板不存在")
        if body.name is not None:
            tpl.name = body.name
        if body.content is not None:
            tpl.content = body.content
        if body.tags is not None:
            tpl.tags = body.tags
        if body.keywords is not None:
            tpl.keywords = body.keywords
        if body.enabled is not None:
            tpl.enabled = body.enabled
        if body.image_paths is not None:
            tpl.image_paths = body.image_paths.strip() or None
        session.commit()
    return {"message": "模板已更新"}


@app.post("/api/templates/{template_id}/toggle")
async def toggle_template(template_id: int):
    mgr = TemplateManager()
    try:
        new_state = mgr.toggle_template(template_id)
        return {"enabled": new_state, "message": "启用" if new_state else "已禁用"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.delete("/api/templates/{template_id}")
async def delete_template(template_id: int):
    mgr = TemplateManager()
    mgr.delete_template(template_id)
    return {"message": "模板已删除"}


# 允许的配图扩展名与大小（与评论上传接口一致）
_TEMPLATE_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_TEMPLATE_IMG_MAX_BYTES = 5 * 1024 * 1024
_TEMPLATE_IMG_MAX_COUNT = 9


@app.post("/api/template-images/upload")
async def upload_template_images(files: list[UploadFile] = File(...)):
    """保存到 assets/templates/，返回相对项目根的路径列表（供模板 image_paths JSON 使用）。"""
    if not files:
        raise HTTPException(status_code=400, detail="请选择图片文件")
    if len(files) > _TEMPLATE_IMG_MAX_COUNT:
        raise HTTPException(status_code=400, detail=f"单次最多上传 {_TEMPLATE_IMG_MAX_COUNT} 张")

    dest_dir = ROOT / "assets" / "templates"
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []

    for uf in files:
        raw_name = uf.filename or "image"
        ext = Path(raw_name).suffix.lower()
        if ext not in _TEMPLATE_IMG_EXT:
            raise HTTPException(status_code=400, detail=f"不支持的图片格式: {ext or '无后缀'}，允许: jpg/png/webp/gif")
        data = await uf.read()
        if len(data) > _TEMPLATE_IMG_MAX_BYTES:
            raise HTTPException(status_code=400, detail="单张图片不能超过 5MB")
        fname = f"{uuid.uuid4().hex}{ext}"
        (dest_dir / fname).write_bytes(data)
        saved.append(f"assets/templates/{fname}".replace("\\", "/"))

    return {"paths": saved, "message": f"已保存 {len(saved)} 张"}


# ═══════════════════════════════════════════════════════
# 日志 API
# ═══════════════════════════════════════════════════════
@app.get("/api/logs")
async def list_logs(page: int = 1, size: int = 30, reply_type: str = "", success: str = ""):
    with get_session() as session:
        q = session.query(ReplyLog)
        if reply_type:
            q = q.filter(ReplyLog.reply_type == reply_type)
        if success == "true":
            q = q.filter(ReplyLog.success == True)
        elif success == "false":
            q = q.filter(ReplyLog.success == False)
        total = q.count()
        items = q.order_by(ReplyLog.replied_at.desc()).offset((page - 1) * size).limit(size).all()

    return {
        "total": total,
        "page": page,
        "size": size,
        "items": [
            {
                "id": log.id,
                "post_note_id": log.post_note_id,
                "template_id": log.template_id,
                "reply_type": log.reply_type,
                "content": log.content,
                "success": log.success,
                "error_msg": log.error_msg or "",
                "replied_at": log.replied_at.strftime("%Y-%m-%d %H:%M:%S") if log.replied_at else "",
            }
            for log in items
        ],
    }


# ═══════════════════════════════════════════════════════
# 配置 API
# ═══════════════════════════════════════════════════════
@app.get("/api/config")
async def get_config():
    import yaml
    cfg_path = ROOT / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = f.read()
    return {"content": raw}


class ConfigUpdate(BaseModel):
    content: str


@app.post("/api/config")
async def save_config(body: ConfigUpdate):
    import yaml
    cfg_path = ROOT / "config.yaml"
    try:
        yaml.safe_load(body.content)  # 验证 YAML 格式
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"YAML 格式错误: {e}")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(body.content)
    config.reload()
    return {"message": "配置已保存"}


# ═══════════════════════════════════════════════════════
# 账号管理与登录 API
# ═══════════════════════════════════════════════════════
from crawler.login_manager import login_manager
from database import Account

@app.get("/api/accounts")
async def list_accounts():
    with get_session() as session:
        accounts = session.query(Account).order_by(Account.id.desc()).all()
        return [
            {
                "id": acc.id,
                "phone": acc.phone,
                "status": acc.status,
                "use_count": acc.use_count,
                "last_used_at": acc.last_used_at.strftime("%m-%d %H:%M") if acc.last_used_at else "从未",
                "created_at": acc.created_at.strftime("%Y-%m-%d %H:%M") if acc.created_at else "",
            }
            for acc in accounts
        ]

@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: int):
    with get_session() as session:
        acc = session.get(Account, account_id)
        if not acc:
            raise HTTPException(status_code=404, detail="账号不存在")
        session.delete(acc)
        session.commit()
    return {"message": "账号已删除"}

@app.post("/api/accounts/{account_id}/toggle")
async def toggle_account(account_id: int):
    with get_session() as session:
        acc = session.get(Account, account_id)
        if not acc:
            raise HTTPException(status_code=404, detail="账号不存在")
        acc.status = "invalid" if acc.status == "active" else "active"
        new_status = acc.status
        session.commit()
    return {"status": new_status, "message": f"账号状态已更新为 {new_status}"}

@app.post("/api/login/start")
async def start_login():
    # 立即返回 session_id，安装/登录流程在后台异步进行
    session_id = await login_manager.start_login_session()
    return {"session_id": session_id}

@app.get("/api/login/status")
async def login_status(session_id: str):
    info = login_manager.get_session_info(session_id)
    if info.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=info.get("error"))
    return info


# 静态资源：模板上传的图片等（便于弹窗内预览）
_assets_dir = ROOT / "assets"
_assets_dir.mkdir(parents=True, exist_ok=True)
app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")
