"""真机执行端的最小冒烟脚本。

用法（PowerShell）:
  1) 手机开开发者模式 + USB 调试，连上电脑
  2) `adb devices` 能看到设备
  3) `appium` 已启动（默认 4723 端口）
  4) 把要发的图放到本机任意路径

  python -m device_runner.demo_run --device <serial> --version 12 \\
      --note <note_id> --text "测试评论" --image E:/path/to/a.jpg
"""
from __future__ import annotations

import argparse

from loguru import logger

from device_runner.appium_session import AppiumSession
from device_runner.xhs_actions import XhsAppActions


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True, help="adb devices 列出的设备 serial")
    parser.add_argument("--version", required=True, help="Android 版本，如 12 / 13 / 14")
    parser.add_argument("--note", required=True, help="目标帖子 note_id")
    parser.add_argument("--text", default="测试一下", help="评论文字")
    parser.add_argument("--image", action="append", default=[], help="本地图片路径（可多次传入）")
    parser.add_argument("--appium", default="http://127.0.0.1:4723")
    args = parser.parse_args()

    session = AppiumSession(
        device_name=args.device,
        platform_version=args.version,
        appium_url=args.appium,
    )
    session.start()
    try:
        actions = XhsAppActions(session)
        actions.reply_with_image(
            note_id=args.note,
            text=args.text,
            local_image_paths=args.image or None,
            device_serial=args.device,
        )
        logger.success("✅ 冒烟成功")
    finally:
        session.quit()


if __name__ == "__main__":
    main()
