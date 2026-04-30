"""
小红书扫码登录管理器 (基于 Playwright)
"""
from __future__ import annotations

import asyncio
import sys
import uuid
import time
import threading
from typing import Dict, Any

from loguru import logger
from database import get_session, Account


import os


async def _ensure_chromium_async(session: "LoginSession"):
    """
    确保可用的浏览器存在：
    1. 优先使用系统已安装的 Chrome（完全跳过下载）
    2. 其次检测 playwright 已下载的 Chromium
    3. 若都没有，自动下载（使用 npmmirror CDN 加速）
    """
    # ── 1. 优先检测 Edge（避免和用户 Chrome 会话冲突）────────
    edge_paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    ]
    found_edge = next((p for p in edge_paths if os.path.exists(p)), None)
    if found_edge:
        session._use_system_chrome = True
        session._chrome_executable = found_edge
        logger.info(f"[{session.session_id[:8]}] 检测到 Microsoft Edge，使用 Edge 打开登录页")
        return

    # ── 2. 检测系统 Chrome ─────────────────────────────────
    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    found_chrome = next((p for p in chrome_paths if os.path.exists(p)), None)
    if found_chrome:
        session._use_system_chrome = True
        session._chrome_executable = found_chrome
        logger.info(f"[{session.session_id[:8]}] 检测到系统 Chrome")
        return

    # ── 2. 检测 playwright 已下载的 Chromium ───────────────
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exec_path = p.chromium.executable_path
            if os.path.exists(exec_path):
                session._use_system_chrome = False
                logger.info(f"[{session.session_id[:8]}] 检测到已安装的 Playwright Chromium")
                return
    except Exception:
        pass

    # ── 3. 自动下载（npmmirror CDN 国内加速）─────────────
    session._use_system_chrome = False
    session.status = "installing"
    session.install_progress = "正在通过国内镜像下载 Chromium，请稍候..."
    logger.info(f"[{session.session_id[:8]}] 开始通过 npmmirror CDN 安装 Chromium...")

    env = os.environ.copy()
    env["PLAYWRIGHT_DOWNLOAD_HOST"] = "https://cdn.npmmirror.com/binaries/playwright"

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "playwright", "install", "chromium",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )

    async for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").strip()
        if line:
            session.install_progress = line
            logger.info(f"[playwright install] {line}")

    await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("Chromium 自动安装失败，请手动执行：python -m playwright install chromium")

    session.install_progress = "Chromium 安装完成，正在启动浏览器..."
    logger.success(f"[{session.session_id[:8]}] Chromium 安装成功！")


class LoginSession:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.qrcode_src: str = ""
        self.status: str = "init"   # init -> installing -> pending -> success/failed
        self.install_progress: str = ""
        self.error: str = ""
        self.account_id: int | None = None
        self._use_system_chrome: bool = False
        self.browser = None
        self.context = None
        self.page = None
        self.created_at = time.time()


class LoginManager:
    def __init__(self):
        self.sessions: Dict[str, LoginSession] = {}

    async def start_login_session(self) -> str:
        """创建登录会话，立即返回 session_id，后台在独立线程执行登录流程"""
        session_id = str(uuid.uuid4())
        session = LoginSession(session_id)
        self.sessions[session_id] = session

        # Windows 下 uvicorn 使用 SelectorEventLoop，不支持 subprocess_exec。
        # Playwright 启动浏览器时需要 asyncio 子进程支持，必须在单独线程里
        # 用 ProactorEventLoop（Windows）或新建 event loop（其他平台）执行。
        def _run_in_thread():
            if sys.platform == "win32":
                loop = asyncio.ProactorEventLoop()
            else:
                loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._login_workflow(session))
            finally:
                loop.close()

        thread = threading.Thread(target=_run_in_thread, daemon=True,
                                  name=f"login-{session_id[:8]}")
        thread.start()
        return session_id

    def get_session_info(self, session_id: str) -> Dict[str, Any]:
        """查询登录会话状态"""
        session = self.sessions.get(session_id)
        if not session:
            return {"status": "not_found", "error": "会话不存在或已过期"}

        return {
            "session_id": session.session_id,
            "status": session.status,
            "qrcode": session.qrcode_src,
            "install_progress": session.install_progress,
            "error": session.error,
        }

    async def _login_workflow(self, session: LoginSession):
        """后台工作流：检测/安装 Chromium → 启动浏览器 → 获取二维码 → 等待扫码"""
        try:
            # Step 1: 确保 Chromium 已安装
            await _ensure_chromium_async(session)

            # 安装完成后重置为 init，让前端知道进入获取二维码阶段
            if session.status == "installing":
                session.status = "init"
                session.install_progress = ""

            # Step 2: 启动浏览器获取二维码
            from playwright.async_api import async_playwright
            import tempfile
            logger.info(f"[{session.session_id[:8]}] 启动浏览器实例")
            async with async_playwright() as p:
                # 使用全新临时用户目录，避免复用系统 Chrome 现有 Cookie
                temp_dir = tempfile.mkdtemp(prefix="pw_xhs_")
                launch_kwargs = dict(
                    headless=False,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                )
                if getattr(session, "_use_system_chrome", False):
                    launch_kwargs["executable_path"] = session._chrome_executable

                # launch_persistent_context 保证使用独立临时目录，不会带入已有 Cookie
                session.context = await p.chromium.launch_persistent_context(
                    temp_dir, **launch_kwargs
                )
                session.browser = None  # persistent_context 无独立 browser 对象
                session.page = await session.context.new_page()

                await session.page.goto(
                    "https://www.xiaohongshu.com/explore",
                    wait_until="domcontentloaded",
                )

                # 等待页面稳定
                await asyncio.sleep(2)

                # ── 尝试触发登录弹窗 ──────────────────────────────────
                login_trigger_selectors = [
                    ".login-btn", ".sign-in", "[class*='login']",
                    "button:has-text('登录')", "a:has-text('登录')",
                ]
                for sel in login_trigger_selectors:
                    try:
                        await session.page.click(sel, timeout=1500)
                        await asyncio.sleep(1)
                        break
                    except Exception:
                        continue

                # ── 尝试多个二维码选择器 ─────────────────────────────
                qr_selectors = [
                    "img.qrcode-img",
                    "img[class*='qrcode']",
                    "img[src*='qrcode']",
                    ".qrcode-container img",
                    ".qrcode img",
                    "[class*='qrcode'] img",
                    ".login-container img",
                    ".modal img",
                ]
                src = None
                for sel in qr_selectors:
                    try:
                        await session.page.wait_for_selector(sel, timeout=3000)
                        el = session.page.locator(sel).first
                        src = await el.get_attribute("src")
                        if src:
                            logger.info(f"[{session.session_id[:8]}] QR 选择器命中: {sel}")
                            break
                    except Exception:
                        continue

                if not src:
                    # 兜底：截取整页截图作为二维码区域展示
                    logger.warning(f"[{session.session_id[:8]}] 未找到二维码元素，改用页面截图")
                    import base64
                    shot = await session.page.screenshot(full_page=False, type="png")
                    src = "data:image/png;base64," + base64.b64encode(shot).decode()

                session.qrcode_src = src
                session.status = "pending"
                logger.info(f"[{session.session_id[:8]}] 二维码已获取，等待扫码...")

                # Step 3: 记录当前基准 web_session，等待其变化才算真正登录
                # XHS 对所有访客立即设置匿名 web_session，扫码后会替换为真实 auth token
                await asyncio.sleep(1)
                baseline_cookies = await session.context.cookies()
                baseline_ws = next(
                    (c["value"] for c in baseline_cookies if c["name"] == "web_session"), ""
                )
                logger.info(f"[{session.session_id[:8]}] 基准 web_session = {baseline_ws[:16]}...")

                logged_in = False
                # 双重验证（参考 MediaCrawler）：
                # 1. 优先检测 DOM：侧边栏出现用户头像/「我」按钮
                # 2. 备用：检测 web_session Cookie 值变化
                user_profile_selector = "xpath=//a[contains(@href, '/user/profile/')]//span[text()='我']"
                for _ in range(120):  # 最多等待 2 分钟
                    await asyncio.sleep(1)
                    # ── 方式1：DOM 检测（最可靠）
                    try:
                        is_visible = await session.page.is_visible(user_profile_selector, timeout=500)
                        if is_visible:
                            logger.info(f"[{session.session_id[:8]}] DOM 检测到「我」按钮，确认登录")
                            logged_in = True
                            break
                    except Exception:
                        pass
                    # ── 方式2：Cookie 变化检测（兼容备用）
                    cookies = await session.context.cookies()
                    cdict = {c["name"]: c["value"] for c in cookies}
                    ws = cdict.get("web_session", "")
                    if ws and ws != baseline_ws:
                        logger.info(f"[{session.session_id[:8]}] web_session 已更新，确认登录")
                        logged_in = True
                        break

                if not logged_in:
                    raise Exception("等待扫码超时或未获取到完整 Cookie")

                # Step 4: 等待页面完成跳转，确保所有 auth Cookie 都已写入
                logger.info(f"[{session.session_id[:8]}] 等待页面跳转完成...")
                await asyncio.sleep(5)
                # 等待主页加载完毕（timeout 10s）
                try:
                    await session.page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                await asyncio.sleep(2)  # 额外缓冲，确保 cookie 写入完毕

                # Step 5: 采集 Cookie
                cookies = await session.context.cookies()
                # 验证 web_session 存在
                cdict_final = {c["name"]: c["value"] for c in cookies}
                if not cdict_final.get("web_session"):
                    raise Exception("Cookie 中缺少 web_session，登录可能未完成，请重试")

                cookie_str = "; ".join(
                    [f"{c['name']}={c['value']}" for c in cookies]
                )

                phone_or_name = "未命名账号"
                try:
                    name_el = session.page.locator(".user-name").first
                    if await name_el.count() > 0:
                        phone_or_name = await name_el.inner_text()
                except Exception:
                    pass

                with get_session() as db:
                    account = Account(
                        phone=phone_or_name,
                        cookie_str=cookie_str,
                        status="active",
                    )
                    db.add(account)
                    db.commit()
                    db.refresh(account)
                    session.account_id = account.id

                session.status = "success"
                logger.success(
                    f"[{session.session_id[:8]}] 扫码登录成功！账号: {phone_or_name}"
                )

        except Exception as e:
            logger.error(f"[{session.session_id[:8]}] 登录工作流异常: {e}")
            session.error = str(e)
            session.status = "failed"
        finally:
            # persistent_context 没有独立 browser，直接关闭 context
            target = session.browser or session.context
            if target:
                try:
                    await target.close()
                except Exception:
                    pass


# 全局登录管理器
login_manager = LoginManager()
