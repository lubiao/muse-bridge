# muse-bridge

把 Meta Muse 接入飞书的自托管桥接服务。Docker Compose 部署，跑在自己的 NAS 或 VPS 上。

基于 [norlingdumar-arch/muse-domestic-bridge](https://github.com/norlingdumar-arch/muse-domestic-bridge)（Apache-2.0），在其飞书渠道上做了一组修复与增强。

---

## 它能做什么

- **双向对话**：在飞书私聊里跟你的 Muse 主聊天对话，回复实时回推
- **主动消息**：Muse 的提醒、定时任务结果会推送到飞书
- **富媒体**：图片、图文混排、文件、语音、视频都能转发给 Muse
- **视频抽帧**：视频自动拆成开头/中间/结尾三张静图，Muse 能真正看懂画面
- **收到回执**：需要处理的媒体消息一进桥就打个 ✅ 表情，不用干等
- **读飞书上下文**：`/read` 把最近的飞书对话喂给 Muse，让它基于真实聊天记录回答
- **Markdown 渲染**：Muse 的 Markdown 回复转成飞书卡片，标题/加粗/列表/链接正常显示

---

## 快速开始

### 0. 准备本机环境变量

```bash
cp .env.example .env
```

按需填 `HTTP_PROXY` / `HTTPS_PROXY`。**国内主机通常必须填**，因为 `hatch.metaaivm.com` 的 DNS 被污染，直连会超时。不需要代理就留空。

### 1. 构建并初始化

```bash
docker compose build
docker compose run --rm bridge init feishu --directory /config --data-dir /state
```

### 2. 写入凭据

```bash
docker compose run --rm -it bridge configure feishu --config /config/config.json
```

依次输入三项：

| 项 | 从哪来 |
|---|---|
| Feishu App ID | [open.feishu.cn](https://open.feishu.cn/) → 你的应用 → 凭证与基础信息，`cli_` 开头 |
| Full Muse Cookie | 登录 [muse.ai](https://muse.ai/) → F12 → Network → 刷新 → `session` 请求 → Request Headers 的 `Cookie` 整行，**必须含 `hatch_sess=`** |
| Feishu App Secret | 同应用凭证页，点「查看」 |

### 3. 启动

```bash
docker compose up -d
```

### 4. 配对

```bash
docker compose exec bridge muse-bridge pair feishu --config /config/config.json
```

会打印一个 `/bind <配对码>`，**30 分钟内**用你的飞书账号私聊机器人发送它。绑定成功后机器人会回复确认。

---

## 飞书应用配置

在 [open.feishu.cn](https://open.feishu.cn/) 创建**企业自建应用**：

**权限**（权限管理页搜索添加）

| 权限码 | 用途 |
|---|---|
| `im:message.p2p_msg:readonly` | 收私聊消息（必需） |
| `im:message:send_as_bot` | 机器人发消息（必需） |
| `im:resource` | 上传/下载图片和文件 |
| `im:message:readonly` | 读会话历史（`/read` 用） |
| `drive:drive` + `docx:document:readonly` | 读云文档（可选） |
| `calendar:calendar:readonly` | 读日历（可选） |

**机器人能力**：添加能力 → 机器人

**事件订阅**：事件与回调 → 事件配置 → 订阅方式选 **「使用长连接接收事件」** → 订阅 `im.message.receive_v1`

> 长连接不需要公网回调地址。但飞书可能要求先有连接才能保存事件配置，此时先启动服务再回来保存。

**发布**：版本管理与发布 → 创建版本 → 申请发布。权限变更必须重新发布才生效。

---

## 常用命令

```bash
# 运行状态
docker compose exec bridge muse-bridge status feishu --config /config/config.json

# 连通性自检（会真的连 Muse 拉历史）
docker compose run --rm bridge check feishu --config /config/config.json

# 日志（默认只输出 warning/error，正常运行是空的）
docker compose logs -f
```

`status` 里看这几个字段：

- `health_age_seconds` 几秒内 → 主循环在跑
- `health.muse` = `连接已校验` 或 `最近提交成功` → 上游正常
- `queues` 里有 `unknown` → 有投递结果待人工核对

---

## 在飞书里可用的命令

| 命令 | 作用 |
|---|---|
| `/bind <码>` | 首次配对绑定 |
| `/status` | 桥接进程与队列状态 |
| `/read` | 最近 20 条飞书对话喂给 Muse，让它概括 |
| `/read 50` | 指定条数（上限 50） |
| `/read 待办有哪些` | 拉最近 20 条 + 用这句话提问 |

---

## 相对上游的改动

### 修复

**1. 流式回复被截断**（`state.py`）
Muse 的回复是流式的，同一个 `message_id` 会先到片段、后到全文。上游按 id 去重，导致片段入队后全文被静默丢弃。现在识别「严格前缀扩展」的增量并原地升级，已发送的记录不改写。

**2. 图文消息被丢弃**（`feishu.py`）
飞书客户端发「图片+文字」用的是 `post` 富文本类型，上游白名单里没有，整条消息被静默丢弃。现在解析 `post` 结构，文字和图片一起转发。

**3. 语音/视频被丢弃**（`feishu.py`）
`audio` / `media` 类型同样不在白名单。现在走通用文件通道转发，并补齐了 Python 缺失的 mime 映射（`.opus` `.amr` `.m4a` `.aac` `.silk`）。

**4. 日志永不输出**（`cli.py`）
`basicConfig(WARNING)` 先执行后，各渠道的 `basicConfig(INFO)` 变成空操作。现在支持 `MUSEBRIDGE_LOG_LEVEL` 环境变量覆盖。

### 新增

**`markdown.py`** —— Muse 用 Markdown 写回复，飞书 `text` 消息不解析。现在转成飞书卡片，并按两边方言差异适配：

| Muse 写的 | 飞书卡片 | 处理 |
|---|---|---|
| `## 标题` | 不支持 | 降级为加粗行 |
| `\| 表格 \|` | 不支持 | 重排为列表 |
| 裸 URL | 会溢出 | 转为 `[域名](url)` 短链 |
| 加粗/列表/代码块 | 支持 | 原样透传 |

只有检测到 Markdown 结构时才用卡片，短对话仍是普通气泡。

**`reader.py`** —— 只读的飞书上下文模块（`/read` 的实现）：

- 所有方法只读，没有任何发送/编辑/删除
- 输出有界：单条 500 字、整段 12000 字、每次最多 50 条
- 权限不足时降级为提示，不影响正常聊天
- 能提取卡片消息里的正文（否则桥自己的回复在上下文里会变成空白）

**`Dockerfile` / `docker-compose.yml`** —— 容器化部署：

- 非 root 运行（uid 10001）
- `read_only` 根文件系统 + `tmpfs /tmp`
- `cap_drop: ALL`、`no-new-privileges`
- 不映射任何端口，纯出站
- 内存上限 2G，日志滚动 10M×3
- 代理经 `.env` 注入，不写死在编排文件里

**`video_frames.py`** —— 视频抽帧：

Muse 的附件通道能收任何文件，但只有图像通道的内容模型才看得见。一段 `mp4` 发过去是个不透明的二进制块，而三张静图能让它回答「这个视频里发生了什么」。

抽取策略刻意简单：开头（10%）、中间（50%）、结尾（90%）三点。解码用 **PyAV**，它自带静态链接的 ffmpeg 库：

| 方案 | 镜像体积 |
|---|---|
| Debian `ffmpeg` 包 | 736 MB |
| **PyAV wheel** | **395 MB** |
| 不加视频支持 | 287 MB |

Debian 的 `ffmpeg` 会通过 `libplacebo` 拉入 `libllvm19`（124MB）和 `mesa-libgallium`（41MB），那是硬件渲染用的，抽帧完全用不到。PyAV 净增约 108MB 就把这件事做了。

抽帧失败时降级为转发原视频文件——转发一个看不懂的视频，也远好过丢掉整条消息。

**`acknowledge()`** —— 收到回执：

媒体消息和 `/read` 一进来就在原消息上打一个 ✅ 反应。放在工作线程里执行，不阻塞飞书 SDK 的事件循环（它要求 3 秒内返回）；失败静默，不影响消息转发。纯文本消息不加，避免每句话都刷一个表情。

---

## 已知限制

- **协议不稳定**：走的是 `hatch.metaaivm.com` 网关，社区逆向所得，不是 Meta 官方 API，对方改动就可能失效
- **Muse Cookie 会过期**：失效后需要重新 `configure`
- **国内需代理**：`hatch.metaaivm.com` 的 DNS 被污染（解析到 `2001::1` 等假地址），容器需要通过 `HTTP_PROXY` / `HTTPS_PROXY` 出网。飞书域名建议放 `NO_PROXY` 直连
- **语音不转写**：语音作为文件原样转发，Muse 大概率听不懂内容。飞书的 `speech_to_text` 接口在部分租户上返回 `99991400 request trigger frequency limit`（配额问题，非权限问题），因此没有接入
- **视频只送静图**：三帧能覆盖大意，但动作、节奏、音频这些信息会丢失
- **单用户**：一个实例只服务一个 owner
- **上游项目很新**：2026-10-22 建仓，无第三方安全审计

---

## 安全

- 凭据只存在 `config/secrets.json`（0600，容器内 uid 10001），永远不会提交到仓库
- `.gitignore` 已排除 `config/`、`state/`、数据库和日志
- **Muse Cookie 等同于你的账号访问权**：拿到它的人能看到你全部对话、能让 Muse 执行操作。泄漏后去 muse.ai 重新登录即可让旧 Cookie 失效

---

## 许可证

本项目基于 [norlingdumar-arch/muse-domestic-bridge](https://github.com/norlingdumar-arch/muse-domestic-bridge)，遵循 **Apache-2.0**。

上游的第三方声明见 `src/muse-domestic-bridge/NOTICE` 与 `licenses/` 目录。
