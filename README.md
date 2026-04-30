# 🍠 小红书自动化帖子监控与回复系统

## 功能概览

| 功能 | 说明 |
|------|------|
| 定时抓取 | 按 Cron 表达式定时搜索小红书帖子，支持多关键词 |
| 热度评分 | 综合点赞、评论、收藏、分享，计算 0-100 热度分 |
| 模板库 | 内置 14 条默认模板，支持评论/私信两种类型，可随时扩充 |
| 随机抽模板 | 支持按关键词优先匹配，避免短期重复使用同一模板 |
| 自动回复 | 定时批量回复热门帖子，模拟人工随机延迟 |
| 防重复 | 已回复帖子自动打标记，永不重复回复 |
| 分批定时 | 抓取和回复各自独立 Cron，支持每天多个时间段 |
| CLI 界面 | 丰富的命令行工具，查看统计、管理模板、查看日志 |
| 数据持久化 | SQLite 本地数据库，无需额外服务 |

---

## 快速开始


### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 获取小红书 Cookie

1. 用 Chrome/Edge 打开 [小红书网页版](https://www.xiaohongshu.com) 并登录
2. 按 `F12` 打开开发者工具 → `Network` 标签
3. 随意点击任意页面，找到一个 XHR 请求
4. 在 `Request Headers` 中找到 `Cookie` 字段，复制全部内容
5. 将复制的内容填入 `config.yaml` 的 `xhs.cookies` 字段，或设置环境变量：

```bash
# Windows PowerShell
$env:XHS_COOKIES = "a1=xxx; web_session=xxx; ..."

# 或直接写入 config.yaml
```

### 3. 配置关键词和时间

编辑 `config.yaml`：

```yaml
xhs:
  cookies: "你的Cookie内容"
  keywords:
    - "护肤"
    - "美妆"
    - "你的行业关键词"

scheduler:
  crawl_cron: "0 9,14,20 * * *"   # 每天 9:00 14:00 20:00 抓取
  reply_cron: "0 10,15,21 * * *"  # 每天 10:00 15:00 21:00 回复
  max_replies_per_batch: 5         # 每批最多回复5条
```

### 4. 启动系统

```bash
# 启动定时调度器
python main.py start

# 启动并立即执行一次（测试用）
python main.py start --run-now

# 干跑测试（不实际发送）
python main.py reply --dry-run
```

---

## 常用命令

```bash
# ── 抓取 ──
python main.py crawl                    # 抓取所有配置关键词
python main.py crawl -k 护肤 -p 2      # 抓取"护肤"关键词，2页

# ── 回复 ──
python main.py reply                    # 立即回复热门帖子
python main.py reply -n 10             # 最多回复10条
python main.py reply --dry-run         # 干跑（不实际发送）

# ── 统计 ──
python main.py status                  # 查看整体统计

# ── 帖子 ──
python main.py posts list              # 查看帖子列表（按热度排序）
python main.py posts pending           # 查看待回复热门帖子
python main.py posts list -s pending   # 按状态过滤

# ── 模板 ──
python main.py templates list          # 查看所有模板
python main.py templates list -t comment  # 只看评论模板
python main.py templates add           # 交互式添加模板
python main.py templates import path/to/templates.json  # 批量导入
python main.py templates toggle 3     # 启用/禁用 ID=3 的模板
python main.py templates delete 5     # 删除 ID=5 的模板

# ── 日志 ──
python main.py logs                    # 查看最近回复日志
python main.py logs -n 50             # 查看最近50条
```

---

## 自定义模板

模板 JSON 格式如下（可批量导入）：

```json
[
  {
    "name": "模板名称",
    "type": "comment",
    "tags": ["护肤", "通用"],
    "keywords": "护肤,面膜",
    "content": "哇，这个帖子太有共鸣了！感谢分享✨"
  },
  {
    "name": "私信模板",
    "type": "dm",
    "tags": ["通用"],
    "keywords": "",
    "content": "你好！看了你的帖子很受益，可以互相关注交流吗？"
  }
]
```

---

## 热度评分说明

热度评分 = 各指标对数归一化后加权求和 × 100

| 指标 | 默认权重 | 满分基准 |
|------|---------|---------|
| 点赞 | 40% | 1000 |
| 评论 | 30% | 100 |
| 收藏 | 20% | 500 |
| 分享 | 10% | 200 |

**默认门槛：热度分 ≥ 50 才会被回复**（可在 `config.yaml` 中调整）

---

## 项目结构

```
RAG/
├── main.py                  # CLI 主入口
├── config.py                # 配置加载
├── config.yaml              # 配置文件（填写Cookie和参数）
├── requirements.txt         # 依赖
├── database/
│   └── models.py            # SQLite 数据模型
├── crawler/
│   └── xhs_crawler.py       # 小红书爬虫
├── analyzer/
│   └── hot_analyzer.py      # 热度分析
├── templates/
│   ├── template_manager.py  # 模板管理
│   └── default_templates.json  # 默认模板库（14条）
├── replier/
│   └── auto_replier.py      # 自动回复执行器
├── scheduler/
│   └── task_scheduler.py    # APScheduler 定时任务
├── utils/
│   └── logger.py            # 日志工具
├── data/                    # SQLite 数据库（自动创建）
└── logs/                    # 日志文件（自动创建）
```

---

## 注意事项

1. **合规使用**：请遵守小红书平台规则，避免高频操作触发风控
2. **Cookie 有效期**：Cookie 通常7天左右失效，需定期更新
3. **回复频率**：默认每批最多5条，每条间隔30秒，可在配置中调整
4. **签名问题**：小红书有请求签名验证，若遇到 403/签名错误，参考 [xhs 库文档](https://github.com/ReaJason/xhs) 配置签名函数

