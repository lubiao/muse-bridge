# 账号与隐私

每位部署者使用自己的 Muse、微信/飞书/QQ 账号。仓库没有共享账号、远程凭据收集接口或公共中转服务器。

## Muse Cookie

在自己已经登录的 Muse 网页中打开开发者工具 → Network，刷新网页，选择 `https://muse.ai/api/session` 请求，在 Request Headers 中复制 **Cookie 整行的值**。不要复制 Response Headers 的 `Set-Cookie`，不要把 Cookie 发到公开 issue、聊天或截图中。

在自己的部署主机执行所选渠道的配置命令：

```bash
sudo -u muse-bridge /opt/muse-domestic-bridge/venv/bin/muse-bridge configure weixin --config /etc/muse-domestic-bridge/config.json
```

把 `weixin` 替换为实际所选渠道。程序通过隐藏输入读取 Cookie；飞书还会询问 App ID / App Secret，QQ 会询问备用号、本人号和 OneBot 令牌。不要通过命令行参数或环境变量传递这些凭据。

配置文件由服务用户拥有，目录 0700，凭据文件 0600。Muse Cookie 具有账号访问能力，只放在自己控制的可信主机。失效后重新执行 `configure`，再重启服务；配置命令不会清除消息状态。

## 微信与 QQ 登录

微信通过 `login-weixin` 获取腾讯临时二维码，在本机终端展示，确认后保存自己的连接令牌。不要分享二维码；网络或扫码超时后重新运行。已有令牌时默认拒绝覆盖；确实需要重新登录可使用 `--replace`。重新登录另一账号需要新的独立状态，不能复用旧账号队列。

QQ 登录由外部 NapCat 管理，本项目只读取你配置的 OneBot 令牌和账号绑定。NapCat WebUI、QQ 登录数据及二维码不要暴露到公网或仓库。

## 本地保存什么

SQLite 保存消息正文、对端标识、文件引用、队列状态和去重记录，当前不自动删除。自己的临时附件缓存按任务状态清理，不能视为聊天历史删除功能；QQ 本身的缓存另外管理。

日志尽量只记录固定错误类别，但向维护者反馈前仍须检查第三方 SDK、系统日志、下载 URL 和截图。备份包含凭据与聊天记录，应自行加密并控制访问。移除安装时保留或删除这些数据应由操作者明确决定。
