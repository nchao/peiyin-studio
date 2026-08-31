# 配音工作台

本地部署的视频解说配音工具。粘贴解说稿 → LLM 智能分段 → 逐段配音 → 导出音频和字幕。基于小米 MiMo TTS，音色与语气分离控制。

面向单人自用，Docker 一键起停，随用随开。

## 功能

- **长文本配音**：粘稿后一键合成，段落级独立控制音色、语气、停顿
- **LLM 智能预处理**：语义分段、插入语气/停顿标签、数字英文转中文读法（`3000` → `三千`，`JSON` → `J S O N`）。字幕文本保持原文一字不改，程序自动校验，不一致则退回规则分段
- **音色 / 语气分离**：4 个中文预置音色（苏打、白桦、冰糖、茉莉）× 6 种解说语气预设，也支持自定义语气词。段落可覆盖整篇默认值
- **声音克隆音色**：上传一段人声样本（3–30s，wav/mp3）即可克隆成专属音色，做角色音或个人声线。全局共享，像预置音色一样选用。走 MiMo voiceclone 模型，**按次付费**，UI 有「付费」标注
- **在线试听**：单段试听、全篇试听，改动前先听效果
- **导出**：MP3 / WAV 音频，SRT 字幕（时间轴按段级对齐）
- **内容缓存**：改一段只重合成那一段，命中缓存秒出
- **过期检测**：改音色/语气后自动标记受影响的段落，避免导出与界面不符的旧音频

## 快速开始

需要 Docker 和 Docker Compose。

```bash
# 1. 准备配置
cp .env.example .env
# 编辑 .env，填入你的 MiMo API Key 和 LLM 网关信息（见下）

# 2. 启动
docker compose up -d

# 3. 打开
# 浏览器访问 http://localhost:8756
# 手机同局域网访问 http://<你的电脑IP>:8756
```

停止用 `docker compose stop`，再次启动 `docker compose start`。

## 部署到群晖 NAS

NAS 的低功耗 CPU（如 Celeron J4125）本地构建很慢，尤其前端 `npm build` 和 `pip` 编译扩展要几分钟。方案是**在开发机上构建好镜像，再传到 NAS 直接运行**，NAS 不做任何编译。开发机与 NAS 同为 amd64 架构，镜像可直接通用。

```bash
# 开发机上一键部署（构建 → save → 传 NAS → load → 起容器 → 健康检查）
./deploy.sh

# 只改了代码、镜像已构建过时，跳过构建直接传+起
./deploy.sh --no-build
```

`deploy.sh` 顶部有配置项（NAS 地址/端口/目录、代理），按自己环境改。前提：已配好到 NAS 的 SSH 免密与 `sudo docker` 免密。Docker 分层缓存让改代码后只重传变化的层。

NAS 上手动启停用部署专用 compose（`docker-compose.deploy.yml` 用现成镜像，不触发构建）：

```bash
cd /volume1/docker/peiyin
sudo docker compose -f docker-compose.yml -f docker-compose.deploy.yml up -d     # 起
sudo docker compose -f docker-compose.yml -f docker-compose.deploy.yml stop      # 停
```

对外访问建议：域名走 HTTPS（反向代理套证书），并在 NAS 的 `.env` 设 `APP_PASSWORD` 开启访问密码。

## 配置

`.env` 里的关键项：

| 变量 | 说明 |
|------|------|
| `MIMO_API_KEY` | 小米 MiMo TTS 的 API Key，[控制台](https://mimo.mi.com)用小米账号登录后创建 |
| `MIMO_BASE_URL` | MiMo API 地址，按量付费用 `https://api.xiaomimimo.com/v1` |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | LLM 预处理服务，任何 OpenAI 兼容接口均可 |
| `LLM_DISABLE_THINKING` | 关闭模型推理模式。分段是结构化任务，开推理慢 30 倍且更易出错，默认 `true` |
| `LLM_CHUNK_CHARS` | 每块送 LLM 的字符数（默认 400）。LLM 输出有 token 上限，长稿必须切块并行 |
| `TTS_CONCURRENCY` | 并发合成段数（默认 4）。MiMo 限流 RPM 100 且按账号聚合，别调太高 |
| `PORT` | 对外端口（默认 8756），改这里需同步改 `docker-compose.yml` 的映射 |
| `APP_PASSWORD` | 访问密码。留空=不鉴权（本地/局域网直接用）；设值=打开页面需登录。用于对外域名场景，务必同时让域名走 HTTPS |

完整配置见 `.env.example`。

## 数据存储

所有状态在 `data/` 目录（挂载为 Docker volume）：

- `data/app.db` —— SQLite，存项目、段落、参数、克隆音色元信息
- `data/audio/<hash>.wav` —— 合成音频，按内容哈希命名
- `data/samples/<hash>.<ext>` —— 克隆音色的样本音频（与合成缓存隔离，不被自动清理）

备份就是拷走整个 `data/` 目录。删除项目时会自动清理不再被引用的音频文件；内容相同的段落跨项目共享同一文件，仍被引用的不会误删。

## 技术栈

- 后端：Python + FastAPI，同时托管前端静态文件
- 前端：Vue 3 + Vite，响应式，桌面和手机浏览器通用
- 音频：ffmpeg（导出 MP3），WAV 在 PCM 层直接拼接
- 存储：SQLite

## 开发

```bash
# 后端
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest          # 跑测试
.venv/bin/uvicorn app.main:app --reload --port 8756

# 前端（另开终端，开发时代理 /api 到后端）
cd frontend
npm install
npm run dev
```

## 说明

- 微信小程序无法本地部署（需已备案 HTTPS 域名），所以做成响应式 Web，手机浏览器同样可用
- 预置音色是 MiMo 的 4 个中文音色；想要角色音/网红音，用声音克隆上传样本（按次付费）
- 字幕时间轴为段级对齐（MiMo 不返回字级时间戳），切段越细字幕越准
