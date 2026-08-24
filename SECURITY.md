# 安全策略 (Security Policy)

## 支持的版本 (Supported Versions)

仅**最新发布版本**提供安全修复。使用旧版本时，请升级到最新版后再反馈问题。

## 报告漏洞 (Reporting a Vulnerability)

发现安全问题，请通过以下任一渠道报告。**请勿在公开 Issue / 讨论区直接披露漏洞细节**，避免影响其他使用者。

1. **GitHub Issue**：在 [Issues](https://github.com/baizi51676-source/astrbot_plugin_napcat_history_exporter/issues) 新建，标题以 `[Security]` 开头
2. **邮箱**：`bz516i@outlook.com`

作者收到报告后会尽快处理并发布修复版本；**由于作者平时较忙，回复可能延迟，但一定会查看**。修复完成后会在 Release Notes 中说明。

## 报告时请包含

- 插件版本号（如 v1.4.0）
- AstrBot / NapCat 版本
- 复现步骤（尽量详细）
- 相关日志（注意对 QQ 号、token 等敏感信息脱敏）

## 安全建议

- 始终使用最新版本
- 生产环境请勿开启 DEBUG 日志
- 请勿将插件运行在不可信 / 公网直接暴露的环境中
