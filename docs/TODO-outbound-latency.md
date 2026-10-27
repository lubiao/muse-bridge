# 待优化：文件消息出站延迟

> 状态：**待实施**（2026-10-27 记录，用户当时正在使用，约定稍后改进）
> 方案：**A + B**（增大分块 + 连接复用）

## 现象

从飞书收到 Muse 发回的文件（图片、文档、视频）比纯文本明显慢。

实测最近 25 条出站消息的「入队 → 首次发送」延迟：

| 类型 | 延迟 |
|---|---|
| 纯文本 | 0.4 秒（中位数） |
| 文件 / 图片 | 4 ~ 17 秒（最大 17.2s） |

三条同时到达的图片最能说明问题——`created` 时间戳完全相同，但首次发送时间依次延后：

```
created 1793091491.1（三条一样）
  第 1 张首次发送：+5.3s
  第 2 张首次发送：+11.9s   ← 排队等第 1 张
  第 3 张首次发送：+17.2s   ← 排队等前两张
```

## 各环节实测耗时

样本：172,365 字节的图片（`显示四位数函数积木图`）

| 环节 | 耗时 |
|---|---|
| `Muse read_file`（从 Muse 工作区拉取） | **3.59s** |
| `Feishu upload`（上传到飞书） | 1.16s |

瓶颈在第一步。

## 根因

### 1. 每次读文件都新建连接（主因）

`src/muse_feishu_bridge/muse.py` → `MainChatClient.read_file()`

```python
def read_file(self, path, expected_size=None):
    gateway = self._connect()    # 每次重新做 Noise 协议握手
    ...
    finally:
        gateway.close()          # 用完就断
```

Muse 网关用的是 Noise_XX 加密握手，建立连接本身就要几秒。一个文件读一次、断一次，握手成本占了绝大部分时间。

### 2. 分块 256KB 偏小

```python
'len': min(256 * 1024, MAX_FILE_BYTES + 1 - offset)
```

每块一次完整网络往返，大文件往返次数多。

### 3. 出站单线程串行

`src/muse_feishu_bridge/bridge.py` → `Bridge._outgoing()`

```python
while not self.stop_event.is_set():
    row = self.state.claim_outgoing()   # 一次只领一条
    # 上传 + 发送，全程阻塞，期间无法处理下一条
```

多条文件只能排队，总耗时 = 条数 × 单条耗时。

### 4. 轮询间隔 0.5s

```python
if row is None:
    self.stop_event.wait(0.5)
```

队列空闲时每 0.5 秒才检查一次。影响较小，但也是白等。

## 约定方案：A + B

### A. 增大分块

256KB → 1MB（或找到一个既减少往返、又不超过服务端单次响应上限的值）。

需要先确认 Muse 网关对 `fs.read` 单次 `len` 的上限，不能盲改。

### B. 连接复用

让 `read_file` 复用同一个 gateway 连接，而不是每次 `_connect()` / `close()`。

要点：
- 连接要保持可用性检测，失效时重建（否则一次断连会拖垮后续所有读取）
- 要处理并发安全：`read_file` 可能被多个线程调用
- 复用失败的降级路径必须保留——读取失败仍然要能回退到「重新建连再试一次」

## 暂缓项

### C. 并行上传（未采纳）

出站 worker 改多线程可让多文件总耗时除以并发数，但：

- 飞书对同一用户有 **5 QPS** 限制（官方文档），并发需要自己限流
- 与现有 SQLite 队列的 `claim_outgoing` 租约机制耦合，改动面大

### D. 预取（未采纳）

收到 Muse 文件事件时立刻拉取并缓存，用户感知延迟可归零。但要处理缓存一致性、磁盘占用、以及用户根本不需要该文件时的浪费。

## 实施时注意

1. **先跑现有测试**：`pytest tests`（上游 305 个通过），确认改动不破坏行为
2. **改完实测对比**：用同一个 172KB 样本重跑 `read_file`，对比 3.59s 这个基线
3. **注意 `file_payload` 的持久化**：`feishu_image_key` / `feishu_file_key` 写入是为了重试时跳过重复上传，改连接逻辑时别破坏它
4. **失败语义**：`DeliveryUnknown` 表示「可能已送达」，优化后仍不能自动重发

## 相关代码位置

| 文件 | 位置 |
|---|---|
| `src/muse_feishu_bridge/muse.py` | `read_file()`, `_connect()`, `_BoundedGateway` |
| `src/muse_feishu_bridge/bridge.py` | `_outgoing()`, `_send_video()` |
| `src/muse_feishu_bridge/feishu.py` | `upload_file()`, `send_file()` |
