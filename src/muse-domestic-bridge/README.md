# Muse Domestic Bridge

将已有 **Meta Muse 主聊天**接入国内聊天软件。选择 **微信、飞书或 QQ 其中一个渠道**，在自己的 Linux 服务器运行；手机通过所选应用收发消息。

**0.1.0b1 · Beta · 非官方项目**。各服务商没有为本项目提供稳定性承诺。适合个人、自托管、单用户使用。

[下载 Beta](https://github.com/norlingdumar-arch/muse-domestic-bridge/releases/tag/v0.1.0b1) · [CI 运行记录](https://github.com/norlingdumar-arch/muse-domestic-bridge/actions/workflows/test.yml) · [问题反馈](https://github.com/norlingdumar-arch/muse-domestic-bridge/issues)

## 能做什么

| 功能 | 微信 | 飞书 | QQ |
|---|---|---|---|
| 文字双向收发 | 支持 | 支持 | 支持 |
| 消息进入原 Muse 主聊天 | 支持 | 支持 | 支持 |
| 普通图片双向发送、Muse 识图 | 支持 | 支持 | 支持 |
| 普通文件双向传输 | 200 MiB | 30 MiB | 200 MiB |
| 接收 Muse 主聊天的主动消息 | 有会话/额度限制 | 支持 | 支持，需机器人在线 |
| 绑定本人私聊、持久去重、重启恢复 | 支持 | 支持 | 支持 |

文件容量是已有真实账号往返验收达到的大小，不能视为平台理论上限。图片代码上限为每张 10,000,000 字节、2400 万像素、最长边 12000 像素；静态 WebP/GIF 转 PNG，动画请作为文件发送。微信、QQ 支持同一消息最多 10 张图片及说明文字，同批最多 30,000,000 字节。

一次安装只启用一个渠道。QQ 需要另一个登录 NapCat 的备用号；微信使用腾讯 iLink/ClawBot 连接；飞书需要自建企业应用。无需为了桥接安装完整 OpenClaw。

## 选一个渠道安装

Python 3.11+。守护进程部署目标为 Ubuntu 24.04；Windows 可用于开发和离线测试。

下载并解压源码后，在项目目录执行**所选渠道的一条命令**：

```bash
python3 -m venv .venv
# 三选一：只安装使用的渠道，不必安装另外两个 SDK
.venv/bin/python -m pip install '.[weixin]'
# .venv/bin/python -m pip install '.[feishu]'
# .venv/bin/python -m pip install '.[qq]'
```

前台试运行（以微信为例）：

```bash
.venv/bin/muse-bridge init weixin --directory ./config
.venv/bin/muse-bridge configure weixin --config ./config/config.json
.venv/bin/muse-bridge login-weixin --config ./config/config.json
.venv/bin/muse-bridge check weixin --config ./config/config.json
.venv/bin/muse-bridge run weixin --config ./config/config.json
```

`configure` 使用隐藏输入保存你的 Muse Cookie；微信还需扫描终端二维码。凭据仅写入自己的主机，文件权限为 0600。首次运行先读取 Muse 主聊天建立历史基线，再接收新消息。

详细配置与账号步骤：[微信](docs/weixin.md) · [飞书](docs/feishu.md) · [QQ](docs/qq.md)。

## 常驻运行

在 Ubuntu 24.04、Python/venv 和 systemd 已准备好的服务器上：

```bash
# --channel 三选一；这里只安装微信
sudo bash deploy/install.sh --channel weixin --check
sudo bash deploy/install.sh --channel weixin
sudo -u muse-bridge /opt/muse-domestic-bridge/venv/bin/muse-bridge configure weixin --config /etc/muse-domestic-bridge/config.json
sudo -u muse-bridge /opt/muse-domestic-bridge/venv/bin/muse-bridge login-weixin --config /etc/muse-domestic-bridge/config.json
# 按所选渠道文档完成 check / 配对后再启动
sudo systemctl enable --now muse-bridge.service
```

只创建所选渠道的一份配置和一个服务。安装程序不安装系统包，不安装 NapCat/QQ，不启动服务，不填入任何凭据。已有活动服务会阻止覆盖安装；更换渠道应使用另一份独立部署及状态，不能直接复用原账号队列。目录与维护方式见 [部署说明](docs/deployment.md)。

## 需要了解的边界

- Muse 使用内部接口和你自己的网页登录 Cookie；失效后需要更新。读取 Cookie 的操作见 [账号与隐私](docs/credentials.md)。
- 消息同步到已有主聊天，其他 Muse 客户端发出的助手回复也可能被转发；网页里你自己发送的消息不会额外镜像。
- Muse 定时任务自身可能延迟，桥接无法保证其执行准时。微信长期不交互后的主动发送可能被拒绝。
- 网络中断时若已开始提交而回执不明，任务标为 `unknown`，不自动再执行；请核对 Muse 主聊天。历史补漏窗口有限，不保证长期停机后的完整回补。
- 群聊、多用户接入、独立语音/视频、飞书富文本 `post` 嵌图不在此版本范围。
- QQ 使用第三方客户端连接，可能需要重新扫码或处理账号验证；使用 NapCat 前自行查看其许可和平台规则。

## 开发与验证

```bash
# 维护者才需要全部渠道依赖
python -m pip install '.[all,dev]'
python -m pytest -q
python scripts/check_public_tree.py
python -m build
```

单渠道开发可使用 `.[weixin,test]` 后执行 `python -m pytest tests/weixin tests/public -q`，其他渠道同理。测试使用假凭据和假网络，不要求真实账号。

已有功能验收与候选包验证分开记录，见 [验证范围](docs/validation.md)。GitHub Actions 按渠道验证 Python 3.11、3.12、3.13；具体提交结果以仓库的 CI 运行记录为准。

## 许可证与来源

本项目采用 [Apache-2.0](LICENSE)，第三方部分保留原许可和版权声明，见 [来源说明](THIRD_PARTY_NOTICES.md)。依赖 muse-cli，参考 Meta Muse Gadget SDK 协议和腾讯微信协议实现。与 Meta、腾讯、飞书及相关上游作者没有官方隶属或背书关系。

QQ 适配器通过 OneBot 接口通信，仓库不包含 NapCat 源码、QQ 程序或登录数据；它们的许可不由本项目的 Apache-2.0 覆盖。
