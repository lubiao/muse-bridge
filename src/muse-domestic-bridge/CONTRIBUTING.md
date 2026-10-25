# 贡献说明

先选择你要修改的一个渠道，安装 `.[weixin,test]`、`.[feishu,test]` 或 `.[qq,test]`。跨渠道开发才需要 `.[all,dev]`。

测试必须离线运行，使用假凭据，不能读取维护者或 CI 的真实账号。行为改动应覆盖真正的边界：授权、持久事务、原生消息格式、大小限制或不确定发送恢复；不要只重复实现本身。

统一运行：

```bash
python -m pytest -q
python scripts/check_public_tree.py
python -m build
```

不要提交本机配置、Cookie、App Secret、QQ/微信连接令牌、数据库、下载 URL、二维码、用户头像或验收聊天截图。讨论大文件问题时用生成的无私密内容样本。

提交说明描述问题触发条件、最终行为和验证范围。涉及上游代码改写时保留许可与来源声明。不要向本仓库加入 NapCat/QQ 的源码、修改包或可执行程序。

普通问题可在 [Issues](https://github.com/norlingdumar-arch/muse-domestic-bridge/issues) 反馈，安全问题请按 SECURITY.md 私密报告。
