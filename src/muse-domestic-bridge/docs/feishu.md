# 飞书：单渠道接入

在飞书开放平台创建自己的企业自建应用，启用机器人。把本人加入应用可用范围。

## 应用权限与事件

- `im:message.p2p_msg:readonly`：接收本人私聊。
- `im:message:send_as_bot`：机器人发送。
- `im:resource`：图片/文件资源上传。
- `im:message:readonly`：下载消息中的附件资源；按平台实际接口允许的等效权限配置。

选择长连接接收事件，订阅 `im.message.receive_v1`，发布应用版本。无需公网回调 URL。长连接配置可能要求进程先建立连接，下面先配置并启动进程，再保存事件和发布。

## 配置与配对

```bash
sudo bash deploy/install.sh --channel feishu --check
sudo bash deploy/install.sh --channel feishu
sudo -u muse-bridge /opt/muse-domestic-bridge/venv/bin/muse-bridge configure feishu --config /etc/muse-domestic-bridge/config.json
sudo -u muse-bridge /opt/muse-domestic-bridge/venv/bin/muse-bridge check feishu --config /etc/muse-domestic-bridge/config.json
sudo systemctl start muse-bridge.service
```

配置命令询问 App ID、Muse Cookie 和 App Secret。`check` 验证 Muse 连接与凭据字段，不代表飞书权限和事件配置已完成。进程连接后回到开放平台保存事件设置、发布应用。

```bash
sudo -u muse-bridge /opt/muse-domestic-bridge/venv/bin/muse-bridge pair feishu --config /etc/muse-domestic-bridge/config.json
```

用准备接收消息的飞书账号打开机器人私聊，发送命令打印的 `/bind ...`。配对码 30 分钟有效，绑定成功后只接收该用户在该私聊发来的消息。

完成一条独特文字往返、普通图片识别及文件往返后：

```bash
sudo systemctl enable muse-bridge.service
```

前台部署可把安装步骤改为 `pip install '.[feishu]'`、`muse-bridge init feishu --directory ./config`，其他命令使用对应配置路径；保持启动进程后完成应用事件和配对的顺序。

支持普通图片消息；富文本 `post` 内嵌图片暂不处理。每个普通文件上限 30 MiB。已有应用若正由其他服务消费，不要让多个长连接程序竞争同一应用事件。

参考：[接收消息](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)、[发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)。
