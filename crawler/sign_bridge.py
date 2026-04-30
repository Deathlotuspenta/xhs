"""
Playwright 签名桥接模块

技术原理（参考 MediaCrawler）：
  - 保留登录态的浏览器上下文环境，通过 JS 表达式调用 XHS 原生签名函数
  - 无需 JS 逆向，直接让浏览器执行 XHS 自己的 window._webmsxyw(uri, data)
  - 比 Python 版签名实现更可靠，随 XHS 代码自动更新
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import os
import threading
import time
from typing import Optional

from loguru import logger


# XHS 签名函数在页面上下文中的获取脚本
# window._webmsxyw 是 XHS 注入的签名函数（MediaCrawler 中确认的函数名）
_GET_SIGN_JS = """
(args) => {
    const [uri, data] = args;
    try {
        // 主要签名函数
        if (typeof window._webmsxyw === 'function') {
            const res = window._webmsxyw(uri, data);
            return { xs: res['X-s'] || res['x-s'] || res.s || '', xt: String(res['X-t'] || res['x-t'] || res.t || Date.now()) };
        }
        // 备用：遍历常见函数名
        const names = ['sign', '_sign', 'webmsxyw', '_webmsxyw'];
        for (const n of names) {
            if (typeof window[n] === 'function') {
                const res = window[n](uri, data);
                if (res && (res['X-s'] || res['x-s'] || res.s)) {
                    return { xs: res['X-s'] || res['x-s'] || res.s || '', xt: String(res['X-t'] || res['x-t'] || res.t || Date.now()) };
                }
            }
        }
    } catch(e) {}
    return null;
}
"""


class PlaywrightSignBridge:
    """
    保持一个已登录的 Playwright 浏览器上下文，
    通过浏览器内 JS 调用 XHS 原生签名函数，
    对外暴露同步 sign() 接口供 XhsClient 使用。
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._context = None
        self._page = None
        self._ready = threading.Event()
        self._init_error: Optional[str] = None
        self._lock = threading.Lock()

    # ── 初始化 ────────────────────────────────────────────────
    def start(self, cookie_str: str, executable_path: Optional[str] = None) -> None:
        """
        在后台线程启动 Playwright 浏览器，加载 XHS 页面。
        :param cookie_str: 登录 Cookie 字符串（"a=1; b=2" 格式）
        :param executable_path: 可选，系统 Chrome/Edge 路径
        """
        self._ready.clear()
        self._init_error = None
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(cookie_str, executable_path),
            daemon=True,
            name="playwright-sign-bridge",
        )
        self._thread.start()
        # 等待浏览器初始化完成（最多 30 秒）
        if not self._ready.wait(timeout=30):
            raise RuntimeError(f"PlaywrightSignBridge 初始化超时: {self._init_error}")
        if self._init_error:
            raise RuntimeError(f"PlaywrightSignBridge 初始化失败: {self._init_error}")
        logger.success("PlaywrightSignBridge 已就绪，使用浏览器原生签名")

    def _run_loop(self, cookie_str: str, executable_path: Optional[str]) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._init_browser(cookie_str, executable_path))
        self._loop.run_forever()

    async def _init_browser(self, cookie_str: str, executable_path: Optional[str]) -> None:
        try:
            from playwright.async_api import async_playwright
            temp_dir = tempfile.mkdtemp(prefix="pw_sign_")
            launch_kwargs: dict = dict(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            if executable_path and os.path.exists(executable_path):
                launch_kwargs["executable_path"] = executable_path

            self._pw = await async_playwright().start()
            self._context = await self._pw.chromium.launch_persistent_context(
                temp_dir, **launch_kwargs
            )

            # 注入 Cookie
            cookies_list = self._parse_cookies(cookie_str)
            if cookies_list:
                await self._context.add_cookies(cookies_list)

            # 打开 XHS 主页，让 JS 签名函数加载
            self._page = await self._context.new_page()
            await self._page.goto(
                "https://www.xiaohongshu.com/explore",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            await asyncio.sleep(2)  # 等待 XHS JS 完全初始化
            logger.info("PlaywrightSignBridge: XHS 页面已加载")
            self._ready.set()
        except Exception as e:
            self._init_error = str(e)
            logger.error(f"PlaywrightSignBridge 初始化异常: {e}")
            self._ready.set()

    # ── 签名接口 ──────────────────────────────────────────────
    def sign(self, uri: str, data=None, a1: str = "", web_session: str = "") -> dict:
        """
        同步签名接口，供 XhsClient.sign 参数使用。
        优先通过浏览器 JS 获取，失败时回退到 Python 实现。
        """
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._sign_async(uri, data), self._loop
            )
            try:
                result = future.result(timeout=8)
                if result:
                    return result
            except Exception as e:
                logger.warning(f"浏览器签名失败，回退到 Python 实现: {e}")

        # 回退：使用 Python 版签名
        return self._fallback_sign(uri, data, a1)

    async def _sign_async(self, uri: str, data) -> Optional[dict]:
        if not self._page:
            return None
        try:
            result = await self._page.evaluate(_GET_SIGN_JS, [uri, data])
            if result:
                return {"x-s": result["xs"], "x-t": result["xt"]}
        except Exception as e:
            logger.debug(f"JS 签名异常: {e}")
        return None

    @staticmethod
    def _fallback_sign(uri: str, data, a1: str = "") -> dict:
        """回退到 xhs.help.sign Python 实现"""
        try:
            from xhs.help import sign as _xhs_sign
            return _xhs_sign(uri, data, a1=a1)
        except Exception:
            return {"x-s": "", "x-t": str(int(time.time() * 1000))}

    # ── 工具方法 ──────────────────────────────────────────────
    @staticmethod
    def _parse_cookies(cookie_str: str) -> list[dict]:
        """将 'a=1; b=2' 格式的 Cookie 字符串转为 Playwright 需要的格式"""
        cookies = []
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                cookies.append({
                    "name": k.strip(),
                    "value": v.strip(),
                    "domain": ".xiaohongshu.com",
                    "path": "/",
                })
        return cookies

    def stop(self) -> None:
        """关闭浏览器，释放资源"""
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._close_async(), self._loop)

    async def _close_async(self) -> None:
        try:
            if self._context:
                await self._context.close()
            if hasattr(self, "_pw"):
                await self._pw.stop()
        except Exception:
            pass
        self._loop.stop()


# ── 全局桥接实例（懒加载，按账号 Cookie 初始化）──────────────
_bridge_instance: Optional[PlaywrightSignBridge] = None
_bridge_cookie: str = ""
_bridge_lock = threading.Lock()


def get_sign_bridge(cookie_str: str, executable_path: Optional[str] = None) -> PlaywrightSignBridge:
    """
    获取（或初始化）全局签名桥接实例。
    当 Cookie 变化时自动重建。
    """
    global _bridge_instance, _bridge_cookie
    with _bridge_lock:
        if _bridge_instance is None or _bridge_cookie != cookie_str:
            if _bridge_instance:
                try:
                    _bridge_instance.stop()
                except Exception:
                    pass
            _bridge_instance = PlaywrightSignBridge()
            _bridge_instance.start(cookie_str, executable_path)
            _bridge_cookie = cookie_str
    return _bridge_instance
