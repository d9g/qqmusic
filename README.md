# qqmusic

QQ 音乐评论抓取与 AI 情感分析服务。基于 FastAPI + SQLAlchemy，提供评论抓取（断点续传 / 增量更新）、水评打标、AI 情感分析、浏览发现（歌单 / 榜单）、QQ 扫码登录等功能。`netease163` 的姊妹项目。

## ✨ 功能

### 💬 评论抓取
- 无需登录，单歌评论约 2 万条；支持断点续传与增量更新，崩溃不丢进度
- 热评 + 普通评论双通道，`(song_id, comment_id)` 唯一约束去重
- 后台定时调度器：自动增量更新已入库歌曲，风控安全节奏（页间隔 0.35s / 歌曲间隔 3~5s 随机抖动）

### 🏷️ 水评打标
- 短评 / 空文本 / 广告链接 / 系统占位自动打标保留（`is_trivial` + `trivial_reason`）
- 高赞豁免：点赞 ≥ 20 的短评不打标
- 阈值可环境变量覆盖，`recheck` 命令全库重算，无需重抓

### 🤖 AI 情感分析
- 26 类情感标签体系（与 netease163 一致）
- 聚焦高赞与高质量评论，跳过水评省 LLM token
- 自动回写 `ai_emotion` 字段，情感分布可视化

### 🧭 浏览发现
- 歌单分类树（语种 / 风格 / 主题 / 心情 / 场景）→ 歌单列表 → 歌曲清单
- QQ 官方榜单：热歌 / 新歌 / 网络歌曲 / K 歌 / 飙升 / 国风 / ACG / 公告牌等 18 个
- 每首歌可跳转 QQ 音乐播放页
- 登录后可读取「我的歌单」

### 🔐 QQ 扫码登录
- OAuth 授权流扫码登录 + cookie 粘贴兜底
- 登录态校验 / 退出，未登录接口返回 401 + 前端自动跳转登录页

### 📊 排行中心
- 热门歌曲（按评论数 / 点赞数）
- 神评论（按点赞数）
- 高质量评论（按 AI 标签）

### 🎨 WebUI
- 单页应用：概览 / 浏览 / 排行榜 / 搜歌 / 评论检索 / 情感分析 / 登录 / 系统管理
- 支持桌面端 + 移动端

## 🛠️ 技术栈

| 模块 | 技术 |
|------|------|
| 后端 | FastAPI + SQLAlchemy 2.0 |
| 前端 | 单页 WebUI（HTML + 原生 JS + Chart.js） |
| 数据库 | SQLite (WAL) |
| LLM | OpenAI / Anthropic 兼容 API |
| 调度 | 后台线程调度器（随服务自启） |

## 🚀 快速开始

### 安装

```bash
# 1. 克隆
git clone https://github.com/d9g/qqmusic.git
cd qqmusic

# 2. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. 配置环境变量 (可选)
cp .env.example .env 2>/dev/null || true
# 编辑 .env: OPENAI_API_KEY=*** (AI 情感分析用, 不配则跳过该功能)

# 4. 启动 (默认端口 8020)
python -m qqmusic.cli serve
```

### CLI 用法

```bash
# 搜歌, 拿 songid
python -m qqmusic.cli search 孤勇者

# 抓评论 (预览)
python -m qqmusic.cli comments 331839675 --pages 5

# 抓评论并入库
python -m qqmusic.cli comments 331839675 --save

# 看统计
python -m qqmusic.cli stats
```

### systemd 部署

```ini
# /etc/systemd/system/qqmusic.service 示例
[Unit]
Description=qqmusic - QQ music comment service
After=network.target

[Service]
WorkingDirectory=/opt/qqmusic
Environment="DATABASE_URL=sqlite:////opt/qqmusic/data/qqmusic.db"
ExecStart=/opt/qqmusic/.venv/bin/uvicorn qqmusic.api.app:app --host 127.0.0.1 --port 8020
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now qqmusic.service
```

nginx 反代：根路径指向 `webui/` 静态目录，`/api/` 与 `/docs` 反代后端。

## 📦 项目结构

```
qqmusic/
├── qqmusic/
│   ├── api/            # FastAPI 路由
│   ├── ai/             # LLM 情感分析 (26 标签)
│   ├── spiders/        # base / comment / search / browse / auth
│   ├── storage/        # SQLAlchemy 模型 + DB
│   ├── utils/          # 常量 / 日志 / 清洗 / 水评判定
│   ├── scheduler.py    # 后台定时增量抓取调度器
│   └── cli.py          # 命令行入口
├── webui/              # 单页 WebUI 静态文件
├── tests/              # pytest 测试
└── data/               # SQLite DB + 日志 (gitignore)
```

## 📊 数据模型

- `song` — 歌曲元数据（songid / songmid / 歌手 / 专辑 / 标称评论数）
- `comment` — 评论（含 `raw_content` / `is_trivial` / `trivial_reason` / `ai_emotion`）
- `song_crawl_status` — 单曲抓取状态 + 断点续传游标（`next_pagenum` / `stop_reason`）

## ⚙️ 自动调度

| 配置项 | 值 |
|--------|-----|
| 间隔 | 12 小时（`QQMUSIC_SCHEDULER_INTERVAL`） |
| 每轮预算 | 30 首 / 500 页 |
| 页间隔 | 0.35s |
| 歌曲间隔 | 3~5s 随机 |
| 策略 | 最久未更新优先；已抓全走增量（连续 2 页无新增即收工） |

管理入口：`/api/v1/admin/scheduler/{status,start,stop,run-now}` 及 WebUI 系统管理页。

## 🔌 主要 API

| 端点 | 说明 |
|------|------|
| `GET /api/v1/search` | 搜歌 |
| `POST /api/v1/crawl/comment/{songid}` | 抓评论（mode=auto/restart/once） |
| `GET /api/v1/crawl/status/{songid}` | 抓取状态与续传游标 |
| `GET /api/v1/comments/search` | 评论检索（关键词 / 情感 / 点赞数 / 剔除水评） |
| `GET /api/v1/comments/emotions` | 情感分布统计 |
| `GET /api/v1/browse/{categories,playlists,playlist/{id}}` | 浏览发现 |
| `GET /api/v1/browse/{toplists,toplist/{id}}` | QQ 官方榜单 |
| `POST /api/v1/browse/my-playlists` | 我的歌单（需登录） |
| `POST /api/v1/auth/{qr/new,qr/poll,cookie,status,logout}` | QQ 扫码登录 |
| `GET /api/v1/rankings/{hot-songs,hot-comments,high-quality}` | 排行中心 |
| `POST /api/v1/admin/comments/{recheck,analyze}` | 水评重算 / AI 分析 |

## ⚙️ 环境变量

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` | 数据库连接，默认项目内 SQLite |
| `QQMUSIC_API_KEY` | 设置后 API 需带 `X-API-Key` 请求头 |
| `QQMUSIC_SCHEDULER` | `0` 关闭后台调度器，默认开启 |
| `QQMUSIC_SCHEDULER_INTERVAL` | 调度间隔（秒），默认 43200 (12h) |
| `QQMUSIC_PROXY` | 评论抓取代理出口（应对机房 IP 风控） |
| `TRIVIAL_MIN_LEN` / `TRIVIAL_MIN_LIKED_KEEP` | 水评阈值 |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | AI 情感分析 |
| `LOG_LEVEL` | 日志级别，默认 INFO |

## 📝 接口实测要点（2026-09）

改爬虫代码前先看，都是踩过坑的：

1. **评论接口无需登录**，真 / 假 / 无 cookie 返回完全一致
2. **`pagesize` 硬上限 25**：传大值不报错而是静默退化成 10 条
3. **单歌可翻深度约 2 万条**：超深后返回空列表且 `code=0`，非风控
4. **歌单列表**参数名必须用 `categoryId`（驼峰）且**不能带 `outCharset`**，否则 list 恒空；且该接口返回 **GBK 编码** JSON，需双编码兼容解码
5. **榜单接口只返回 songId 不返回歌曲 mid**，播放链接需用 `music.pf_song_detail_svr` 批量反查 songmid
6. **扫码登录须走 OAuth 授权流**（daid=383 + pt_3rd_aid），直连流已失效（23013 应用版本过低）
7. 评论接口对机房 IP 有定向风控（搜索 / 分类 / 榜单不受影响），可用 `QQMUSIC_PROXY` 走代理出口

## 🧪 测试

```bash
python -m pytest tests/ -q
```

## 📄 License

仅供学习交流，请勿用于商业用途。
