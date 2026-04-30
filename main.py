#!/usr/bin/env python3
"""
小红书自动化帖子监控与回复系统
主入口 CLI

用法示例：
  python main.py webui               # 启动 Web 可视化界面
  python main.py webui --port 8080   # 指定端口
  python main.py start               # 启动定时调度器（前台运行）
  python main.py crawl               # 立即执行一次抓取
  python main.py crawl -k 护肤       # 抓取指定关键词
  python main.py reply               # 立即执行一次回复
  python main.py reply --dry-run     # 干跑（不实际发送）
  python main.py status              # 查看数据库统计
  python main.py templates list      # 查看所有模板
  python main.py templates add       # 交互式添加模板
  python main.py templates import templates/my_templates.json
  python main.py posts list          # 查看帖子列表
  python main.py posts pending       # 查看待回复帖子
"""
from __future__ import annotations

import signal
import sys
import time

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from utils.logger import setup_logger
from database import get_engine, get_session, Post, Template, ReplyLog, ReplyStatus
from analyzer.hot_analyzer import HotAnalyzer
from crawler.xhs_crawler import XhsCrawler
from replier.auto_replier import AutoReplier
from scheduler.task_scheduler import TaskScheduler
from templates.template_manager import TemplateManager

setup_logger()
console = Console()


# ═══════════════════════════════════════════════════════════════
# CLI 根命令
# ═══════════════════════════════════════════════════════════════
@click.group()
def cli():
    """🍠 小红书自动化帖子监控与回复系统"""
    # 确保数据库表存在
    get_engine()


# ═══════════════════════════════════════════════════════════════
# start：启动定时调度器
# ═══════════════════════════════════════════════════════════════
@cli.command()
@click.option("--run-now", is_flag=True, help="启动后立即执行一次抓取+回复")
def start(run_now: bool):
    """启动定时调度器（持续运行，Ctrl+C 停止）"""
    scheduler = TaskScheduler()

    def _shutdown(sig, frame):
        console.print("\n[yellow]正在停止调度器...[/yellow]")
        scheduler.stop()
        console.print("[green]已停止。再见！[/green]")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    console.print(Panel.fit(
        "[bold green]小红书自动化回复系统已启动[/bold green]\n"
        "按 [bold]Ctrl+C[/bold] 停止",
        title="🍠 XHS Bot"
    ))

    scheduler.start(run_now=run_now)

    _print_jobs(scheduler)

    # 主循环
    while True:
        time.sleep(60)


def _print_jobs(scheduler: TaskScheduler):
    table = Table(title="定时任务列表", show_lines=True)
    table.add_column("任务ID", style="cyan")
    table.add_column("任务名称", style="white")
    table.add_column("下次执行时间", style="yellow")
    for job in scheduler.list_jobs():
        table.add_row(job["id"], job["name"], job["next_run"])
    console.print(table)


# ═══════════════════════════════════════════════════════════════
# crawl：立即抓取
# ═══════════════════════════════════════════════════════════════
@cli.command()
@click.option("-k", "--keyword", default=None, help="指定关键词（不填则使用配置中所有关键词）")
@click.option("-p", "--pages", default=None, type=int, help="抓取页数")
def crawl(keyword: str, pages: int):
    """立即执行帖子抓取"""
    crawler = XhsCrawler()
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
        task = progress.add_task("正在抓取...", total=None)
        if keyword:
            new_count = crawler.crawl_keyword(keyword, pages)
            progress.update(task, description=f"[green]完成！新增 {new_count} 条[/green]")
        else:
            results = crawler.crawl_all_keywords()
            total = sum(results.values())
            progress.update(task, description=f"[green]完成！共新增 {total} 条[/green]")

    _print_stats()


# ═══════════════════════════════════════════════════════════════
# reply：立即执行回复
# ═══════════════════════════════════════════════════════════════
@cli.command()
@click.option("-n", "--count", default=None, type=int, help="本次最多回复条数")
@click.option("-k", "--keyword", default=None, help="只回复指定关键词的帖子")
@click.option("--dry-run", is_flag=True, help="干跑模式：不实际发送，只打印日志")
def reply(count: int, keyword: str, dry_run: bool):
    """立即执行自动回复"""
    if dry_run:
        console.print("[yellow][DRY-RUN] 干跑模式，不会实际发送任何消息[/yellow]")

    replier = AutoReplier()
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
        task = progress.add_task("正在回复...", total=None)
        stats = replier.run_batch(max_count=count, keyword=keyword, dry_run=dry_run)
        progress.update(task, description=(
            f"[green]完成！评论 {stats['comment_ok']} 条 | "
            f"私信 {stats['dm_ok']} 条 | 失败 {stats['failed']} 条[/green]"
        ))

    _print_stats()


# ═══════════════════════════════════════════════════════════════
# status：统计信息
# ═══════════════════════════════════════════════════════════════
@cli.command()
def status():
    """查看数据库统计信息"""
    _print_stats(verbose=True)


def _print_stats(verbose: bool = False):
    analyzer = HotAnalyzer()
    s = analyzer.get_stats()

    table = Table(title="帖子统计", show_lines=True)
    table.add_column("指标", style="cyan")
    table.add_column("数量", justify="right", style="bold white")

    table.add_row("总帖子数", str(s["total"]))
    table.add_row("待回复", str(s["pending"]))
    table.add_row("热门待回复", f"[green]{s['hot_pending']}[/green]")
    table.add_row("已回复", f"[blue]{s['replied']}[/blue]")
    table.add_row("已跳过", str(s["skipped"]))
    table.add_row("失败", f"[red]{s['failed']}[/red]" if s["failed"] else "0")

    console.print(table)


# ═══════════════════════════════════════════════════════════════
# templates：模板管理子命令组
# ═══════════════════════════════════════════════════════════════
@cli.group()
def templates():
    """模板库管理"""
    pass


@templates.command("list")
@click.option("-t", "--type", "ttype", default=None, type=click.Choice(["comment", "dm"]),
              help="按类型过滤")
def templates_list(ttype: str):
    """列出所有模板"""
    mgr = TemplateManager()
    mgr.ensure_defaults()
    items = mgr.list_templates(ttype)

    table = Table(title=f"模板列表（共 {len(items)} 条）", show_lines=True)
    table.add_column("ID", style="cyan", width=4)
    table.add_column("类型", width=8)
    table.add_column("名称", width=15)
    table.add_column("内容预览", width=40)
    table.add_column("使用次数", justify="right", width=8)
    table.add_column("启用", width=6)

    for tpl in items:
        enabled_str = "[green]✓[/green]" if tpl.enabled else "[red]✗[/red]"
        type_str = "[blue]评论[/blue]" if tpl.template_type == "comment" else "[magenta]私信[/magenta]"
        preview = (tpl.content[:38] + "…") if len(tpl.content) > 38 else tpl.content
        table.add_row(str(tpl.id), type_str, tpl.name, preview, str(tpl.use_count or 0), enabled_str)

    console.print(table)


@templates.command("add")
def templates_add():
    """交互式添加新模板"""
    console.print("[bold]添加新模板[/bold]")
    name = click.prompt("模板名称")
    ttype = click.prompt("类型", type=click.Choice(["comment", "dm"]), default="comment")
    console.print("请输入模板内容（多行请直接回车换行，输入 END 单独一行结束）：")
    lines = []
    while True:
        line = input()
        if line.strip() == "END":
            break
        lines.append(line)
    content = "\n".join(lines)
    tags = click.prompt("标签（逗号分隔，可留空）", default="")
    keywords = click.prompt("适用关键词（逗号分隔，可留空）", default="")

    mgr = TemplateManager()
    tpl = mgr.add_template(name, content, ttype, tags, keywords)
    console.print(f"[green]✓ 模板已添加，ID={tpl.id}[/green]")


@templates.command("import")
@click.argument("path")
def templates_import(path: str):
    """从 JSON 文件批量导入模板"""
    mgr = TemplateManager()
    count = mgr.import_from_json(path)
    console.print(f"[green]✓ 成功导入 {count} 条模板[/green]")


@templates.command("toggle")
@click.argument("template_id", type=int)
def templates_toggle(template_id: int):
    """启用/禁用指定模板"""
    mgr = TemplateManager()
    state = mgr.toggle_template(template_id)
    status_str = "[green]已启用[/green]" if state else "[red]已禁用[/red]"
    console.print(f"模板 {template_id} {status_str}")


@templates.command("delete")
@click.argument("template_id", type=int)
@click.confirmation_option(prompt="确定要删除该模板吗？")
def templates_delete(template_id: int):
    """删除指定模板"""
    mgr = TemplateManager()
    mgr.delete_template(template_id)
    console.print(f"[green]✓ 模板 {template_id} 已删除[/green]")


# ═══════════════════════════════════════════════════════════════
# posts：帖子查看子命令组
# ═══════════════════════════════════════════════════════════════
@cli.group()
def posts():
    """帖子数据查看"""
    pass


@posts.command("list")
@click.option("-n", "--limit", default=20, show_default=True, help="显示条数")
@click.option("-k", "--keyword", default=None, help="按关键词过滤")
@click.option("-s", "--status", "fstatus", default=None,
              type=click.Choice(["pending", "replied_comment", "replied_dm",
                                 "replied_both", "skipped", "failed"]),
              help="按回复状态过滤")
def posts_list(limit: int, keyword: str, fstatus: str):
    """列出帖子列表"""
    with get_session() as session:
        q = session.query(Post)
        if keyword:
            q = q.filter(Post.keyword == keyword)
        if fstatus:
            q = q.filter(Post.reply_status == ReplyStatus(fstatus))
        items = q.order_by(Post.hot_score.desc()).limit(limit).all()

    _render_posts_table(items)


@posts.command("pending")
@click.option("-n", "--limit", default=20, show_default=True)
def posts_pending(limit: int):
    """查看待回复的热门帖子"""
    analyzer = HotAnalyzer()
    items = analyzer.get_hot_pending_posts(limit=limit)
    _render_posts_table(items)


def _render_posts_table(items: list):
    table = Table(title=f"帖子列表（{len(items)} 条）", show_lines=True)
    table.add_column("note_id", style="dim", width=24)
    table.add_column("标题", width=28)
    table.add_column("热度", justify="right", width=6)
    table.add_column("👍", justify="right", width=6)
    table.add_column("💬", justify="right", width=6)
    table.add_column("状态", width=14)
    table.add_column("关键词", width=10)

    STATUS_COLOR = {
        ReplyStatus.PENDING: "white",
        ReplyStatus.REPLIED_COMMENT: "blue",
        ReplyStatus.REPLIED_DM: "cyan",
        ReplyStatus.REPLIED_BOTH: "green",
        ReplyStatus.SKIPPED: "dim",
        ReplyStatus.FAILED: "red",
    }

    for p in items:
        color = STATUS_COLOR.get(p.reply_status, "white")
        title = (p.title[:26] + "…") if p.title and len(p.title) > 26 else (p.title or "")
        table.add_row(
            p.note_id,
            title,
            f"{p.hot_score:.1f}",
            str(p.likes or 0),
            str(p.comments or 0),
            f"[{color}]{p.reply_status.value}[/{color}]",
            p.keyword or "",
        )

    console.print(table)


# ═══════════════════════════════════════════════════════════════
# logs：回复日志
# ═══════════════════════════════════════════════════════════════
@cli.command()
@click.option("-n", "--limit", default=20, show_default=True)
def logs(limit: int):
    """查看最近的回复日志"""
    with get_session() as session:
        items = (
            session.query(ReplyLog)
            .order_by(ReplyLog.replied_at.desc())
            .limit(limit)
            .all()
        )

    table = Table(title=f"回复日志（最近 {len(items)} 条）", show_lines=True)
    table.add_column("时间", width=18)
    table.add_column("帖子ID", style="dim", width=24)
    table.add_column("类型", width=8)
    table.add_column("内容预览", width=35)
    table.add_column("结果", width=6)

    for log in items:
        result_str = "[green]✓[/green]" if log.success else "[red]✗[/red]"
        t_str = "[blue]评论[/blue]" if log.reply_type == "comment" else "[magenta]私信[/magenta]"
        preview = (log.content[:33] + "…") if len(log.content) > 33 else log.content
        time_str = log.replied_at.strftime("%m-%d %H:%M:%S") if log.replied_at else ""
        table.add_row(time_str, log.post_note_id, t_str, preview, result_str)

    console.print(table)


# ═══════════════════════════════════════════════════════════════
# webui：启动 Web 可视化界面
# ═══════════════════════════════════════════════════════════════
@cli.command()
@click.option("--host", default="0.0.0.0", show_default=True, help="监听地址")
@click.option("--port", default=8000, show_default=True, type=int, help="监听端口")
@click.option("--reload", is_flag=True, help="开发模式（代码变更自动重载）")
def webui(host: str, port: int, reload: bool):
    """启动 Web 可视化管理界面"""
    import uvicorn
    console.print(Panel.fit(
        f"[bold green]Web 界面已启动[/bold green]\n"
        f"请在浏览器打开: [bold cyan]http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}[/bold cyan]",
        title="🍠 XHS Bot Web UI"
    ))
    uvicorn.run(
        "web.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


if __name__ == "__main__":
    cli()
