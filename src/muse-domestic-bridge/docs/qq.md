# QQ：单渠道接入

需要一个备用 QQ 登录外部 NapCat，日常 QQ 与备用号成为好友并私聊。本仓库只提供 OneBot 适配器，不分发 NapCat/QQ、不替其登录或修改其源码。

## 先准备外部运行时

自行按 [NapCat 上游说明](https://napneko.github.io/guide/boot/Shell)安装和登录。先阅读所用版本的许可证及账号风险说明。已有验收使用 NapCat 4.18.41；其他版本需要自行验证兼容性。

在 NapCat 的配置界面启用：

- OneBot HTTP：`127.0.0.1:3300`。
- OneBot WebSocket 服务：`127.0.0.1:3301`。
- 两者使用同一个自己生成的随机访问令牌；不开放公网。
- 关闭 `enableLocalFile2Url`，避免下载接口在 JSON 中返回大文件 Base64。

## 安装桥接

```bash
sudo bash deploy/install.sh --channel qq --check
sudo bash deploy/install.sh --channel qq
sudo -u muse-bridge /opt/muse-domestic-bridge/venv/bin/muse-bridge configure qq --config /etc/muse-domestic-bridge/config.json
```

隐藏输入自己的 Muse Cookie、OneBot 令牌，并填入备用 QQ 和日常 QQ。只接受日常号私聊；不自动加好友、不处理群聊。

## 文件目录权限

图片从该原私聊对应的 QQ 官方 CDN 下载，无需授权 QQ 图片目录。普通文件使用 NapCat 本机下载路径，需要额外配置；否则文字和图片可用，文件下载会被拒绝。

在配置的 `download_roots` 中填入本机实际的两个**窄目录**：

1. QQ 账号数据的 `nt_data/File`。
2. NapCat 的附件 `temp` 目录。

不要填账号数据根目录、用户主目录、`/var/lib` 或 `/`。桥接会逐层 `openat`，拒绝符号链接、越界路径、非普通文件及超限附件。

systemd 模板启用了 `ProtectHome=true`；普通文件下载目录应放在 `/var/lib` 下自己的专用运行时数据目录中，不能依赖桥接读取 `/home` 内的 QQ 数据。不会为了访问 QQ 下载而关闭整个服务的主目录保护。

通过系统 ACL 给 `muse-bridge`：上述目录和现有下载文件的读取权限、目录继承 ACL、各父目录的穿越权限。只对下载目录授权；不要授予 QQ 登录文件读取权限。目录位置、父目录层数和 NapCat 运行用户随安装方式变化，Beta 安装器不会猜测或递归授权整个 QQ 数据目录。没有 `setfacl` 时先自行安装系统的 `acl` 工具。

机器人回传文件使用安装器创建的 `/var/lib/muse-domestic-bridge-media`，所有者 `muse-bridge`、组 `muse-bridge-media`、目录 2750、暂存文件 0640。将你自己的 NapCat 运行用户加入该组，再重启**自己的 NapCat 进程**以载入组成员关系；给该用户父目录穿越权限，保持对共享目录只读。不要用 root 运行桥接来绕过权限。

若普通下载文件仍被 QQ 强制设为 0600，继承 ACL 可能无效；需要检查实际文件有效 ACL。该情况不会通过放宽整个账号目录权限自动修复。

## 启动与验收

```bash
sudo -u muse-bridge /opt/muse-domestic-bridge/venv/bin/muse-bridge check qq --config /etc/muse-domestic-bridge/config.json
sudo systemctl enable --now muse-bridge.service
```

`check` 验证 Muse 访问和备用号身份，不会替 NapCat 扫码。用日常 QQ 发送一条独特文字，确认在原 Muse 主聊天出现并收到回复；再分别试一张相册图片、一个小文件，最后再尝试大文件。

不明确的发送结果保留 `unknown`，不自动重放。QQ 历史补漏只读取绑定用户最近 50 条私聊，无法保证长时间断线后的完整恢复。单文件代码上限及既有验收达到 200 MiB，不代表平台理论上限。
