# 单渠道部署与维护

默认每个实例只接一个聊天应用。三个适配器同仓库维护，可选依赖只安装所选渠道。不要同时运行多个使用同一微信连接或飞书应用的消费者。

## 目录

| 路径 | 用途 |
|---|---|
| `/opt/muse-domestic-bridge/releases/` | 独立版本代码与 venv，保留旧版本 |
| `/opt/muse-domestic-bridge/venv` | 当前版本 Python 环境链接 |
| `/etc/muse-domestic-bridge/` | 当前所选渠道的一份配置及私有凭据，0700 |
| `/var/lib/muse-domestic-bridge/` | SQLite、附件缓存和健康状态，0700 |
| `/var/lib/muse-domestic-bridge-media/` | 仅 QQ 使用的只读共享出站目录 |
| `muse-bridge.service` | 当前所选渠道的一个 systemd 服务 |

安装器目标为 Ubuntu 24.04 + Python 3.11+ + systemd。需要已有 `python3-venv` 和可访问 Python 包源的网络；它不会运行 apt、修改防火墙/代理、替外部应用登录。Python 依赖在版本目录的新 venv 内安装，不复用其他桥接环境。

## 查看状态

```bash
sudo systemctl status muse-bridge.service --no-pager
sudo journalctl -u muse-bridge.service -n 30 --no-pager
# 将 weixin 换成实际所选渠道
sudo -u muse-bridge /opt/muse-domestic-bridge/venv/bin/muse-bridge status weixin --config /etc/muse-domestic-bridge/config.json
```

`check` 是有限的连接和字段检查；完整验收仍需真实收发。首次运行只捕获基线之后的新助手消息和图片，不导入全部旧聊天。

## 升级与回退

先查看队列；尽量在没有 `preparing/sending/pending` 任务时升级。正在提交时强制中断可能产生 `unknown`，需要人工核对。

```bash
sudo systemctl stop muse-bridge.service
sudo bash deploy/install.sh --channel weixin
sudo systemctl start muse-bridge.service
```

安装器保留已有配置和凭据，拒绝直接把另一渠道装到同一个状态。旧版本 venv 的绝对路径保留；回退时将 `/opt/muse-domestic-bridge/code` 和 `venv` 两个链接一同切回已审阅的旧版本，并恢复匹配的 unit。不要移动 venv、删除 SQLite 或复制不明队列。

## 停止

```bash
sudo systemctl disable --now muse-bridge.service
```

停止不删除凭据、聊天记录、缓存和外部登录状态。卸载/清理应先核对这些路径，再由操作者明确选择保留哪些数据。

## 数据和交付语义

每个实例只绑定本人私聊。下载、解密和工作区上传在提交 Muse 之前完成，文件引用先持久化；开始提交后发生不确定结果则不自动再执行。微信显式拒绝的出站任务可进入 `blocked` 等待会话恢复，`unknown` 不能混同为普通重试。

会保留队列正文与附件引用。临时缓存清理仅处理程序自身创建的名字；活动任务保留，结果不明任务按实现保留七天缓存。外部 QQ 下载缓存不包含在桥接缓存上限内。

当前保留各适配器已有的独立内部模块，以降低首次公开整理时的运行回归风险；公共 Muse 传输存在重复代码，后续可在回归覆盖下抽取。该结构不要求用户部署三个应用。
