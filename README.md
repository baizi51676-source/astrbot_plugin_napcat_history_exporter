# NapCat 历史记录导出器 (astrbot_plugin_napcat_history_exporter)

通过 NapCat（OneBot v11）扩展 API 将历史聊天记录导出为 **JSONL 文件**，图片/表情/语音等媒体**不导出**，统一使用 `[图片]` `[表情]` 等**占位符**替换。适合做聊天记录存档、归档检索与离线分析。

> ## ⚠️ 联动机制已移除（v1.4.0 起）
>
> 从 **v1.4.0** 开始，本插件**不再支持任何联动机制**，不再与 `astrbot_plugin_group_forwarder_special` 等外部插件联动搜索。跨对话查看消息、归档搜索、群列表已内置为本插件自身的 LLM 工具（`get_group_message_history` / `search_archived_messages` / `list_archived_groups`），直接调用即可。
>
> 注意：特殊版插件（`astrbot_plugin_group_forwarder_special`）的联动兼容性仅支持本插件 **v1.3.3** 及之前版本；使用 v1.4.0+ 时两者不再互通。

## 特性

- 🕐 **自动归档开关**（`auto_export`，默认开启）：开启后定时循环**增量导出**，每 120s 检查一次各群新消息；关闭后仅在被 LLM 工具触发时归档
- 📄 **JSONL 格式**（每行一条消息），按天分文件：
  - 群聊：`napcat_<群号>_YYYY-MM-DD.jsonl`
  - 私聊：`napcat_private_<QQ号>_YYYY-MM-DD.jsonl`
- 🧩 **媒体占位符**：图片 `[图片]`、表情 `[表情]`、语音 `[语音]`、视频 `[视频]`、引用 `[引用消息]`、@ `[At:QQ]`、文件 `[文件:名称]` 等
- ⚡ **增量游标**：记录每个群/好友的最新时间戳，重复运行只追加新消息；文件自动去重排序，不会重复

## 输出格式

每行一条 JSON：

```json
{"t": "2026-08-23 12:00:00", "chat": "group", "group_id": "123456789", "user_id": "987654321", "nickname": "张三", "seq": 12345, "content": "今晚聚餐吗 [图片]"}
```

| 字段 | 说明 |
|---|---|
| `t` | 消息时间（本地时间，YYYY-MM-DD HH:MM:SS）|
| `chat` | `group` 群聊 / `private` 私聊 |
| `group_id` | 群聊=群号；私聊=对方 QQ 号 |
| `user_id` | 发送者 QQ 号 |
| `nickname` | 发送者昵称（优先群名片）|
| `seq` | NapCat 消息序号（message_seq，用于增量去重）|
| `content` | 文本内容（媒体已替换为占位符）|

## LLM 工具

| 工具 | 功能 |
|---|---|
| `export_group_history(group_id, count)` | 按需导出指定群最近 N 条消息（默认 200，最大 5000）|
| `export_private_history(user_id, count)` | 按需导出指定好友私聊最近 N 条消息 |
| `export_all_incremental(group_id, start_date, end_date)` | 立即归档：默认全部群增量；可指定群号 + 起止日期回溯归档历史 |
| `get_group_message_history(group_id, count)` | 读取指定群已归档记录（最近 N 条，时间正序）|
| `search_archived_messages(group_id, keyword, date, user_id, nickname, count)` | 在归档记录中搜索（关键词/日期/QQ/昵称，可组合）|
| `list_archived_groups()` | 列出已有归档记录的群号 |
| `get_export_status()` | 查看自动归档开关、导出目录、游标状态 |

## 配置（WebUI 可视化）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `export_dir` | `data/workspaces/napcat_exports` | 导出目录（JSONL 文件与游标状态存放位置，位于 AstrBot 工作目录下）|
| `auto_export` | `true` | 自动归档开关：开启后定时循环增量归档 |
| `interval_seconds` | `120` | 定时循环间隔（最小 30s）|
| `count_per_batch` | `50` | 单次 API 拉取条数（1-200）|
| `auto_export_friends` | `false` | 自动归档是否同时导出私聊 |
| `admin_only` | `true` | 仅管理员可调用 LLM 工具 |
| `whitelist` | `[]`（全部）| **自动归档群白名单**：仅名单内的群会被定时循环导出；留空=全部群。用户/LLM 手动归档不受此限制 |
| `auto_clean` | `true` | **历史文件自动清理开关**：定时循环导出时自动删除超过保留天数的历史 JSONL（手动归档过的目标除外）|
| `clean_days` | `14` | 历史文件保留天数（默认 14 天，更早的自动删除）|

## 使用前提

- AstrBot 使用 `aiocqhttp` 适配器连接 NapCat
- NapCat 本地保存有需要导出的聊天记录（OneBot v11 消息缓存）
- 需要调用 NapCat 扩展 API：`get_group_msg_history` / `get_friend_msg_history`（Go-CQHTTP 兼容接口）

## 安装

```bash
# AstrBot 中执行（插件市场命令）
plugin i https://github.com/baizi51676-source/astrbot_plugin_napcat_history_exporter
```

或下载 Release 附件 zip，解压到 `AstrBot/data/plugins/`，然后在 WebUI 启用。
