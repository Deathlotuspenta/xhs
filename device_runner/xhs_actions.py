"""真机版小红书操作动作：进入帖子 → 评论 → 选图 → 发送。

注意：
1. 小红书 App 控件 ID/文字会随版本变化，本模块用「多重 fallback」尽量稳，
   仍可能因新版本失效，需要时打开 Appium Inspector 重新定位。
2. 图片要先 push 到手机相册，并触发媒体扫描，详见 push_to_album()。
"""
from __future__ import annotations

import os
import random
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from loguru import logger
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from appium.webdriver.common.appiumby import AppiumBy

from .appium_session import AppiumSession


# ──────────────────────────────────────────────────────────────
# ADB 工具
# ──────────────────────────────────────────────────────────────
def _resolve_adb_bin(preferred: Optional[str] = None) -> str:
    """尽量自动定位 adb，可兼容 PATH 未配置场景。

    preferred 可以是：
      - adb.exe / adb 的完整文件路径
      - 一个目录（其中含 adb.exe / adb）
    """
    if preferred:
        p = Path(preferred).expanduser()
        if p.is_file():
            return str(p)
        if p.is_dir():
            for name in ("adb.exe", "adb"):
                cand = p / name
                if cand.is_file():
                    return str(cand)
            # 也兼容用户填到 SDK 根目录的情况
            for sub in (p / "platform-tools" / "adb.exe", p / "platform-tools" / "adb"):
                if sub.is_file():
                    return str(sub)

    hit = shutil.which("adb")
    if hit:
        return hit

    candidates: list[Path] = []
    sdk_roots = [
        os.environ.get("ANDROID_SDK_ROOT"),
        os.environ.get("ANDROID_HOME"),
    ]
    for sdk in sdk_roots:
        if sdk:
            candidates.append(Path(sdk) / "platform-tools" / "adb.exe")
            candidates.append(Path(sdk) / "platform-tools" / "adb")
            # 用户也可能直接把 platform-tools 目录配进了 ANDROID_HOME
            candidates.append(Path(sdk) / "adb.exe")
            candidates.append(Path(sdk) / "adb")

    local_appdata = os.environ.get("LOCALAPPDATA")
    userprofile = os.environ.get("USERPROFILE")
    if local_appdata:
        candidates.append(Path(local_appdata) / "Android" / "Sdk" / "platform-tools" / "adb.exe")
    if userprofile:
        up = Path(userprofile)
        candidates.append(up / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe")
        # 用户经常解压到 Downloads 的两种常见目录名
        candidates.append(up / "Downloads" / "platform-tools" / "adb.exe")
        candidates.append(up / "Downloads" / "platform-tools-latest-windows" / "platform-tools" / "adb.exe")

    for c in candidates:
        if c.is_file():
            return str(c)
    raise FileNotFoundError(
        "未找到 adb。请在前端「真机操作 → 高级」里填 adb 所在目录或可执行路径，"
        "或重启 webui 让它继承到新的 PATH。"
    )


def _adb(args: list[str], device: Optional[str] = None, adb_path: Optional[str] = None) -> tuple[int, str]:
    cmd = [_resolve_adb_bin(adb_path)]
    if device:
        cmd += ["-s", device]
    cmd += args
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def push_to_album(local_files: list[str], device: Optional[str] = None, adb_path: Optional[str] = None) -> list[str]:
    """把本地图片 push 到手机相册并刷新媒体库；返回手机里的绝对路径列表。"""
    remote_paths: list[str] = []
    for f in local_files:
        f = str(Path(f).resolve())
        if not Path(f).is_file():
            raise FileNotFoundError(f"待推送图片不存在: {f}")
        name = Path(f).name
        remote = f"/sdcard/Pictures/{name}"
        rc, out = _adb(["push", f, remote], device, adb_path=adb_path)
        if rc != 0:
            raise RuntimeError(f"adb push 失败: {out}")
        remote_paths.append(remote)
        # 触发媒体扫描，相册才能立刻看到
        _adb(
            [
                "shell",
                "am",
                "broadcast",
                "-a",
                "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                "-d",
                f"file://{remote}",
            ],
            device,
            adb_path=adb_path,
        )
    time.sleep(1.0)
    return remote_paths


# ──────────────────────────────────────────────────────────────
# UI 操作工具
# ──────────────────────────────────────────────────────────────
def _human_sleep(a: float = 0.6, b: float = 1.4) -> None:
    time.sleep(random.uniform(a, b))


def _find_first(driver, candidates: list[tuple[str, str]], timeout: float = 8.0):
    """按候选定位顺序找元素，命中第一个即返回。"""
    end = time.time() + timeout
    last_exc: Optional[Exception] = None
    while time.time() < end:
        for by, value in candidates:
            try:
                el = driver.find_element(by, value)
                if el is not None:
                    return el
            except (NoSuchElementException, WebDriverException) as e:
                last_exc = e
        time.sleep(0.4)
    if last_exc:
        raise last_exc
    raise NoSuchElementException(f"未找到元素: {candidates}")


def _click_safe(el) -> None:
    try:
        el.click()
    except WebDriverException:
        # 兜底：用坐标点击
        rect = el.rect
        x = rect["x"] + rect["width"] // 2
        y = rect["y"] + rect["height"] // 2
        el.parent.tap([(x, y)], 100)


# ──────────────────────────────────────────────────────────────
# 业务动作
# ──────────────────────────────────────────────────────────────
class XhsAppActions:
    """封装一组小红书 App 操作。"""

    def __init__(self, session: AppiumSession) -> None:
        self.session = session

    @property
    def driver(self):
        return self.session.driver

    # 进入指定帖子（通过 Deeplink，最稳定的方式）
    def open_note_by_id(self, note_id: str) -> None:
        deeplink = f"xhsdiscover://item/{note_id}"
        logger.info(f"[App] 打开帖子: {deeplink}")
        self.driver.execute_script(
            "mobile: deepLink",
            {"url": deeplink, "package": self.session.app_package},
        )
        _human_sleep(2.5, 3.5)

    # 在帖子页打开评论输入框
    def open_comment_input(self) -> None:
        """点击帖子详情页底部那条"说点什么..."占位条，唤起真正的评论输入框。

        小红书新版控件 id 经常变（被 R8 混淆），所以这里堆叠多种策略：
          1. UiSelector textContains "说点什么"（服务端 native 过滤，最快也最稳）
          2. content-desc 命中
          3. 兜底：直接 tap 屏幕左下那块占位条所在区域的坐标
        """
        logger.info("[App] 开始定位底部评论占位条…")
        # 策略 1+2：服务端 UiSelector / content-desc / xpath
        candidates = [
            (
                AppiumBy.ANDROID_UIAUTOMATOR,
                'new UiSelector().textContains("说点什么")',
            ),
            (
                AppiumBy.ANDROID_UIAUTOMATOR,
                'new UiSelector().descriptionContains("评论")',
            ),
            (AppiumBy.ID, f"{self.session.app_package}:id/edit_text"),
            (AppiumBy.XPATH, "//*[contains(@text,'说点什么') or contains(@text,'留个评论')]"),
            (AppiumBy.XPATH, "//*[contains(@content-desc,'评论') and @clickable='true']"),
        ]
        try:
            el = _find_first(self.driver, candidates, timeout=8)
            _click_safe(el)
            _human_sleep()
            return
        except Exception as e:
            logger.warning(f"[App] 选择器没命中评论占位条，使用坐标兜底: {e}")

        # 策略 3：坐标兜底——点击屏幕左下角约 12% 宽 / 96% 高的位置
        try:
            size = self.driver.get_window_size()
            w, h = size["width"], size["height"]
        except Exception:
            w, h = 1080, 2400
        x = int(w * 0.12)
        y = int(h * 0.96)
        logger.info(f"[App] 坐标点击底部评论占位条: ({x}, {y}) screen={w}x{h}")
        try:
            self.driver.execute_script("mobile: clickGesture", {"x": x, "y": y})
        except Exception:
            try:
                self.driver.tap([(x, y)], 80)
            except Exception as e2:
                raise RuntimeError(f"点击评论占位条失败: {e2}")
        _human_sleep(1.0, 1.6)

    # 输入评论文字
    def type_comment(self, text: str) -> None:
        if not text:
            return
        # 输入框可能要再次定位（弹起后会变成新的输入控件）
        candidates = [
            (AppiumBy.ID, f"{self.session.app_package}:id/comment_edit_text"),
            (AppiumBy.CLASS_NAME, "android.widget.EditText"),
        ]
        el = _find_first(self.driver, candidates, timeout=8)
        try:
            el.click()
        except Exception:
            pass
        try:
            el.send_keys(text)
        except Exception:
            self.driver.execute_script("mobile: type", {"text": text})
        _human_sleep()

    # 在评论输入态打开“选图”入口并选第一张（push 到相册的图）
    def attach_first_album_image(self) -> None:
        """点击评论框旁的相册按钮，选中第一张相册图片。"""
        candidates_album_btn = [
            (AppiumBy.ID, f"{self.session.app_package}:id/iv_pic"),
            (AppiumBy.XPATH, "//*[@content-desc='图片' or @content-desc='添加图片' or @content-desc='相册']"),
        ]
        try:
            el = _find_first(self.driver, candidates_album_btn, timeout=6)
            _click_safe(el)
            _human_sleep(1.0, 1.6)
        except Exception as e:
            raise RuntimeError(f"未找到评论相册按钮（小红书版本可能不支持评论附图）: {e}")

        # 在相册里点第一张图
        candidates_first_img = [
            (AppiumBy.XPATH, "(//*[@class='android.widget.ImageView' and @clickable='true'])[1]"),
            (AppiumBy.XPATH, "(//android.widget.ImageView)[2]"),
        ]
        el = _find_first(self.driver, candidates_first_img, timeout=8)
        _click_safe(el)
        _human_sleep()

        # 部分版本要点“完成/确定”
        try:
            el_done = self.driver.find_element(
                AppiumBy.XPATH,
                "//*[@text='完成' or @text='确定' or @text='下一步']",
            )
            _click_safe(el_done)
            _human_sleep()
        except NoSuchElementException:
            pass

    # 点发送
    def send(self) -> None:
        candidates = [
            (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().text("发送")'),
            (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().text("发布")'),
            (AppiumBy.ID, f"{self.session.app_package}:id/comment_send"),
            (AppiumBy.XPATH, "//*[@text='发送' or @text='发布']"),
        ]
        el = _find_first(self.driver, candidates, timeout=8)
        _click_safe(el)
        _human_sleep(1.5, 2.5)

    # 整体一条流程
    def reply_with_image(
        self,
        note_id: str,
        text: str,
        local_image_paths: Optional[list[str]] = None,
        device_serial: Optional[str] = None,
        adb_path: Optional[str] = None,
    ) -> None:
        if local_image_paths:
            push_to_album(local_image_paths, device=device_serial, adb_path=adb_path)

        self.open_note_by_id(note_id)
        self.open_comment_input()
        self.type_comment(text)
        if local_image_paths:
            self.attach_first_album_image()
        self.send()
        logger.success(f"[App] 已发送评论 note_id={note_id}")
