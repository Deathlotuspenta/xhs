"""真机执行端：通过 Appium 控制手机操作小红书 App。

模块边界：本模块只负责「在手机上做事」，不负责挑帖子/挑模板/风控统计。
具体业务调用：由现有 replier/auto_replier 决策后，把
(post.note_id, content, image_paths) 交给本模块执行。
"""
