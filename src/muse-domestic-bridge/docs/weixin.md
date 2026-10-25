# 微信：单渠道接入

使用腾讯 iLink/ClawBot 私聊连接。准备自己可登录的微信账号、自己的 Muse Cookie 和能够访问两端服务的 Linux 主机。

## 前台配置

```bash
python3 -m venv .venv
.venv/bin/python -m pip install '.[weixin]'
.venv/bin/muse-bridge init weixin --directory ./config
.venv/bin/muse-bridge configure weixin --config ./config/config.json
.venv/bin/muse-bridge login-weixin --config ./config/config.json
.venv/bin/muse-bridge check weixin --config ./config/config.json
.venv/bin/muse-bridge run weixin --config ./config/config.json
```

`configure` 输入 Muse Cookie；`login-weixin` 显示临时二维码，用接收消息的手机微信扫描并确认。`check` 验证 Muse 访问及本地微信凭据格式，不单独消费微信消息，也不保证微信账号当前在线。

服务启动后，在手机出现的 ClawBot 私聊发送一句独特文字，核对 Muse 原主聊天出现该文字，再核对微信回复。图片直接从相册发送，文件作为普通附件发送。`/status` 由桥接本地回复队列状态。

## systemd

```bash
sudo bash deploy/install.sh --channel weixin --check
sudo bash deploy/install.sh --channel weixin
sudo -u muse-bridge /opt/muse-domestic-bridge/venv/bin/muse-bridge configure weixin --config /etc/muse-domestic-bridge/config.json
sudo -u muse-bridge /opt/muse-domestic-bridge/venv/bin/muse-bridge login-weixin --config /etc/muse-domestic-bridge/config.json
sudo -u muse-bridge /opt/muse-domestic-bridge/venv/bin/muse-bridge check weixin --config /etc/muse-domestic-bridge/config.json
sudo systemctl enable --now muse-bridge.service
```

只运行一个消费者。前台进程和 systemd 不能同时消费同一连接；服务进程有本地锁，但不同数据目录仍可能连接同一账号，应避免这种部署。

微信上下文和发送额度由腾讯控制；长期未交互后的主动提醒可能被拒绝。显式拒绝的任务保留为 `blocked`，收到本人新消息后可恢复；发送结果不明的 `unknown` 任务不会自动重放。不要靠删除 SQLite 重置问题。

参考：[腾讯上游协议说明](https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md)。
