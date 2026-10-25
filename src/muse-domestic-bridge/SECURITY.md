# 安全反馈

此 Beta 面向自己主机上的单用户私聊桥接，不提供公网多租户入口。

发现凭据泄露、跨用户收件或任意路径读取问题时，请使用仓库已启用的 [GitHub 私密漏洞报告](https://github.com/norlingdumar-arch/muse-domestic-bridge/security/advisories/new)。不要在公开 issue 上传漏洞利用细节、Cookie、二维码、聊天数据库或 OneBot/CDN 令牌。

可以公开报告不含敏感数据的症状和错误类别。尚未设立漏洞响应时限或完成独立安全审计。

发布前及贡献时：运行 `python scripts/check_public_tree.py`，检查 staged diff 和构建产物，只提交空凭据示例。该脚本是基本检查，不能代替完整秘密扫描与人工审阅。泄露凭据后应先在对应服务撤销/更新，不要只删除文件。
