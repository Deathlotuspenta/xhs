"""Appium Session 封装：负责连接手机。

依赖（请先安装）：
  pip install Appium-Python-Client==4.* selenium

并需在另一终端启动 Appium Server（必须先设置 ANDROID_HOME）：
  PowerShell:
    $env:ANDROID_HOME = "C:\\path\\to\\platform-tools 的上一级目录"
    appium

  Appium 启动后会监听 http://127.0.0.1:4723
"""
from __future__ import annotations

import json
import socket
import time
from typing import Optional
from urllib.parse import urlparse

import urllib.request
import urllib.error

from loguru import logger

from appium import webdriver
from appium.options.android import UiAutomator2Options


def _check_appium_alive(appium_url: str, timeout: float = 2.0) -> tuple[bool, str]:
    """快速探测 Appium Server 是否在监听，返回 (是否存活, 提示)。"""
    try:
        parsed = urlparse(appium_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 4723
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as e:
        return False, (
            f"无法连接 Appium Server {appium_url}：{e}. "
            "请先在另一终端启动 Appium（必须先 set ANDROID_HOME）："
            "  $env:ANDROID_HOME = '<platform-tools 的上一级目录>' ; appium"
        )
    # 进一步确认 /status
    try:
        with urllib.request.urlopen(f"{appium_url.rstrip('/')}/status", timeout=timeout) as resp:
            if resp.status >= 400:
                return False, f"Appium /status 返回 HTTP {resp.status}"
    except Exception as e:
        return False, f"Appium /status 探测失败: {e}"
    return True, ""


def _list_active_sessions(appium_url: str, timeout: float = 3.0) -> list[dict]:
    """读 Appium /sessions 拿到现存会话列表。"""
    try:
        with urllib.request.urlopen(f"{appium_url.rstrip('/')}/sessions", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
            return data.get("value") or []
    except Exception as e:
        logger.debug(f"[Appium] 读取会话列表失败: {e}")
        return []


def _delete_session(appium_url: str, session_id: str, timeout: float = 5.0) -> bool:
    """主动 DELETE 一个 Appium 会话，返回是否成功。"""
    try:
        req = urllib.request.Request(
            f"{appium_url.rstrip('/')}/session/{session_id}", method="DELETE"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        logger.debug(f"[Appium] 删除会话 {session_id} 失败: {e}")
        return False


def cleanup_stale_sessions(appium_url: str, device_udid: Optional[str] = None) -> int:
    """清理 Appium server 上残留的旧会话（可按 device_udid 过滤）。

    返回成功删除的会话数。这一步至关重要：
    同一台设备上的 io.appium.uiautomator2.server 是单例，旧会话超时清理时
    会 force-stop 这个 server，连带把当前正在使用的会话连接打断（socket hang up）。
    """
    sessions = _list_active_sessions(appium_url)
    if not sessions:
        return 0

    deleted = 0
    for s in sessions:
        sid = s.get("id") or s.get("sessionId")
        caps = s.get("capabilities") or {}
        udid = (
            caps.get("deviceUDID")
            or caps.get("udid")
            or caps.get("appium:udid")
            or caps.get("appium:deviceUDID")
            or caps.get("deviceName")
            or caps.get("appium:deviceName")
        )
        if not sid:
            continue
        if device_udid and udid and udid != device_udid:
            continue
        logger.info(f"[Appium] 清理残留会话 {sid} (device={udid})")
        if _delete_session(appium_url, sid):
            deleted += 1
    return deleted


class AppiumSession:
    """与单台手机/模拟器对应的一个会话。"""

    def __init__(
        self,
        *,
        device_name: str,
        platform_version: str,
        appium_url: str = "http://127.0.0.1:4723",
        app_package: str = "com.xingin.xhs",
        app_activity: Optional[str] = None,
        no_reset: bool = True,
        # 调小超时，旧会话残留时尽快被 Appium 自己回收，
        # 避免它在我们新会话进行中清理 uiautomator2.server 把通道掐断
        new_command_timeout: int = 120,
        # 默认 False = 让 Appium 自检版本并按需重装，避免设备上残留的旧 APK
        # 启动时立即闪退（instrumentation process crashed）。
        # 没装 aapt2 时每次会装一次 (~5s)，但稳定性优先
        skip_settings_install: bool = False,
        skip_server_install: bool = False,
        cleanup_stale: bool = True,
    ) -> None:
        self.device_name = device_name
        self.platform_version = platform_version
        self.appium_url = appium_url
        self.app_package = app_package
        # 不强制指定入口 Activity（小红书版本经常变），由 Appium 通过 launcher intent 自动选
        self.app_activity = app_activity
        self.no_reset = no_reset
        self.new_command_timeout = new_command_timeout
        self.skip_settings_install = skip_settings_install
        self.skip_server_install = skip_server_install
        self.cleanup_stale = cleanup_stale
        self._driver: Optional[webdriver.Remote] = None

    def start(self) -> webdriver.Remote:
        """连接 Appium 并打开小红书 App。"""
        # 1) 先做连通性检查，避免 webdriver.Remote 默认长超时把 UI 挂起
        alive, hint = _check_appium_alive(self.appium_url, timeout=3.0)
        if not alive:
            raise RuntimeError(hint)

        # 2) 清理同设备上残留的旧会话，避免它们超时清理时 force-stop
        #    uiautomator2.server，把当前会话的连接打断（socket hang up）
        if self.cleanup_stale:
            try:
                n = cleanup_stale_sessions(self.appium_url, self.device_name)
                if n > 0:
                    logger.info(f"[Appium] 已清理 {n} 个残留会话")
                    # 给设备一点时间让 instrumentation 进程真正退出
                    time.sleep(1.5)
            except Exception as e:
                logger.warning(f"[Appium] 清理残留会话失败（忽略）：{e}")

        opts = UiAutomator2Options()
        opts.platform_name = "Android"
        opts.device_name = self.device_name
        opts.platform_version = self.platform_version
        # 故意不设置 appPackage / appActivity：
        # 新版小红书 manifest 里的入口 Activity（SplashActivity）已被混淆/移除，
        # Appium 自动解析会拿到一个不存在的类，导致 am start-activity 直接失败。
        # 因此我们让 Appium 只“连接设备”，由我们用 mobile: deepLink 直接打开目标帖子。
        opts.no_reset = self.no_reset
        opts.new_command_timeout = self.new_command_timeout
        # 反检测优先项
        opts.set_capability("disableSuppressAccessibilityService", True)
        opts.set_capability("autoGrantPermissions", True)
        opts.set_capability("unicodeKeyboard", True)
        opts.set_capability("resetKeyboard", True)
        # 跳过 io.appium.settings / uiautomator2.server 反复安装（已在设备上则不再重装）
        # 没有 aapt2 时 Appium 无法判断版本，默认会“保险起见”每次重装，这里强制跳过
        if self.skip_settings_install:
            opts.set_capability("skipDeviceInitialization", True)
        if self.skip_server_install:
            opts.set_capability("skipServerInstallation", True)

        logger.info(
            f"[Appium] 连接 {self.appium_url} → device={self.device_name} "
            f"version={self.platform_version}"
        )
        try:
            self._driver = webdriver.Remote(self.appium_url, options=opts)
        except Exception as e:
            msg = str(e)
            if "ANDROID_HOME" in msg or "ANDROID_SDK_ROOT" in msg:
                raise RuntimeError(
                    "Appium 启动会话失败：缺少 ANDROID_HOME / ANDROID_SDK_ROOT。"
                    " 请关闭 Appium，按下面方式重启它：\n"
                    "  PowerShell: $env:ANDROID_HOME = '<platform-tools 的上一级目录>'; appium\n"
                    f"原始错误：{msg}"
                ) from e
            raise
        # 给 App 一点冷启动时间
        time.sleep(3)
        return self._driver

    @property
    def driver(self) -> webdriver.Remote:
        if self._driver is None:
            raise RuntimeError("Appium 会话尚未启动，请先调用 start()")
        return self._driver

    def quit(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            finally:
                self._driver = None
