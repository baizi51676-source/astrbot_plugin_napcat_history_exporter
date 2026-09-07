# 聊天记录归档与查看 (astrbot_plugin_chat_history_archiver)

![插件封面](banner.jpg)

> 如图片有侵权，请联系插件作者删除（作者平时很忙，不是故意不回！）

> 本插件由 **astrbot_plugin_napcat_history_exporter** 于 **v2.0.0** 更名而来，支持 **NapCat 与 SnowLuma 双平台**（OneBot v11）。旧仓库地址会自动重定向，升级方法见文末「如何更新」。

通过 **NapCat / SnowLuma**（OneBot v11）扩展 API 将历史聊天记录导出为 **JSONL 文件**，图片/表情/语音等媒体**不导出**，统一使用 `[图片]` `[表情]` 等**占位符**替换。适合做聊天记录存档、归档检索与离线分析。

## 特性

- 自动归档开关（auto_export）：定时循环增量导出，每 120s 检查一次各群新消息；关闭后仅在被 LLM 工具触发时归档
- JSONL 格式（每行一条消息），按天分文件：群聊 napcat_群号_YYYY-MM-DD.jsonl；私聊 napcat_private_QQ号_YYYY-MM-DD.jsonl
- 媒体占位符：图片/表情/语音/视频/引用/@/文件等统一替换为可读文本
- 增量游标：记录每个会话的最新消息，重复运行只追加新消息；文件自动去重，不会重复
- 内置查看/搜索：已归档记录可直接用 LLM 工具读取、搜索、回溯（无需任何外部插件联动）
- 多 bot 归档：一个 AstrBot 可挂多个 QQ 号，插件为每个 bot 独立归档；可用 archive_bots 指定范围
- 后端自动探测：NapCat / SnowLuma 自动识别（backend: auto），无需手动指定

## 支持平台与 SnowLuma 使用须知

插件通过两个后端共有的扩展 API 工作：get_group_msg_history / get_friend_msg_history。NapCat 与 SnowLuma 均已实现，但两者在历史数据来源与翻页协议上存在差异：

| 维度 | NapCat | SnowLuma |
|---|---|---|
| 历史消息来源 | 本地消息缓存 | 本地 SQLite 消息库（收到即持久化，不自动清理）|
| 翻页锚点 | message_seq（真实序号）| message_id（int32 哈希，可能为负值）|
| 翻到最旧的停止信号 | 返回 retcode=1200 | 返回空消息列表（日志 WARN 属正常）|
| 单页上限 | 200 | 200 |

### SnowLuma 平台的使用局限性（请务必阅读）

1. 历史深度受本地消息库限制：SnowLuma 只能导出其本地 SQLite 消息库中保存的消息（即 SnowLuma 运行以来收到并持久化的记录）。清理 SnowLuma 数据/存储、重装容器或更换账号后，本地历史随之消失，无法再导出。
2. 登录回填能力有限：SnowLuma 可配置登录时从 QQ 服务器回填（historySync.enabled），但受 QQ 协议限制：每次登录每类会话只处理少量会话、每个会话每轮最多约 20 条。想逐步补齐更早历史需保持回填开启并周期性重启 SnowLuma；不要指望一次补全全部老消息。
3. 建议定期备份：SnowLuma 的消息库（数据卷）与本插件导出的 JSONL 目录都应纳入备份。
4. 昵称特殊符号：个别用户群昵称携带不可见特殊符号（QQ 客户端不显示但真实存在），SnowLuma 原样返回并写入 nickname 字段，表现为夹杂不可见控制字符——属正常数据，不影响消息内容与检索。
5. int32 哈希锚点：SnowLuma 的 message_id 为 4 字节哈希，理论上存在极低概率碰撞（两条不同消息哈希相同可能被去重误判）；NapCat 的真实序号无此风险，日常归档未见异常。
6. 同秒消息顺序：SnowLuma 同秒内多条消息的返回顺序可能与序号不完全一致（罕见），消息内容完整，不影响使用。
7. 私聊归档建议先验证：SnowLuma 的 get_friend_msg_history 在无锚点时从 QQ 服务器获取最新双向记录；正式开启 auto_export_friends 前建议先手动触发一次确认覆盖深度。
8. 长回溯耗时：大规模回溯（数千至上万条）为顺序分页拉取，期间定时循环该轮会顺延（不丢消息，下一轮补齐）。

### 多 bot（多个 QQ 号挂在一个 AstrBot 上）

AstrBot 中每个 QQ 号 = 一个独立的 aiocqhttp 平台实例（WebUI 平台配置里每一项有唯一 id）。插件会：

- 自动遍历全部启用的 aiocqhttp 实例，每个 bot 归档自己可见的群与私聊；
- LLM 工具（导出/归档）按触发消息的 bot（self_id）自动路由——在 bot A 的群里触发导出，只会操作 bot A 的数据；
- 若多个 bot 在同一个群：同一轮只归档一次，文件与游标天然兼容，不会冲突或重复。

archive_bots 配置：留空 [] = 归档全部 aiocqhttp 实例（默认）；填一个或多个平台实例 id（WebUI 平台配置里自己起的 id）或登录 QQ 号 = 只归档指定的 bot。例如：

```yaml
archive_bots:
  - 123456789          # 按 QQ 号指定
  - my_bot_2           # 或按平台实例 id 指定
```

> 小提示：get_export_status 会显示当前「归档 bot 配置」与每个 bot 的情况。

## 输出格式

每行一条 JSON：

```json
{"t": "2026-08-23 12:00:00", "chat": "group", "group_id": "123456789", "user_id": "987654321", "nickname": "张三", "seq": 12345, "content": "今晚聚餐吗 [图片]"}
```

| 字段 | 说明 |
|---|---|
| t | 消息时间（本地时间，YYYY-MM-DD HH:MM:SS）|
| chat | group 群聊 / private 私聊 |
| group_id | 群聊=群号；私聊=对方 QQ 号 |
| user_id | 发送者 QQ 号 |
| nickname | 发送者昵称（优先群名片）|
| seq | 消息序号（NapCat 为 message_seq；SnowLuma 为会话序号，用于增量去重）|
| content | 文本内容（媒体已替换为占位符）|

## LLM 工具

| 工具 | 功能 |
|---|---|
| export_group_history(group_id, count) | 按需导出指定群最近 N 条消息（默认 200，最大 5000）|
| export_private_history(user_id, count) | 按需导出指定好友私聊最近 N 条消息 |
| export_all_incremental(group_id, start_date, end_date) | 立即归档：默认全部群增量；可指定群号 + 起止日期回溯归档历史 |
| get_group_message_history(group_id, count) | 读取指定群已归档记录（最近 N 条，时间正序）|
| search_archived_messages(group_id, keyword, date, user_id, nickname, count) | 在归档记录中搜索（关键词/日期/QQ/昵称，可组合）|
| list_archived_groups() | 列出已有归档记录的群号 |
| get_export_status() | 查看自动归档开关、导出目录、游标状态 |

## 配置（WebUI 可视化）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| backend | auto | 后端协议：自动探测 NapCat/SnowLuma，或强制指定 napcat/snowluma |
| export_dir | data/workspaces/napcat_exports | 导出目录（位于 AstrBot 工作目录下）|
| auto_export | true | 自动归档开关：开启后定时循环增量归档 |
| interval_seconds | 120 | 定时循环间隔（最小 30s）|
| whitelist | []（全部）| 自动归档群白名单：仅名单内的群会被定时循环导出；留空=全部群。手动归档不受限 |
| auto_export_friends | false | 定时模式是否同时导出私聊 |
| archive_bots | []（全部）| 多 bot 归档白名单：留空=全部 aiocqhttp 实例；填平台实例 id 或登录 QQ 号=只归档指定 bot |
| count_per_batch | 50 | 单次 API 拉取条数（1-200）|
| admin_only | true | 仅管理员可调用 LLM 工具 |
| auto_clean | true | 自动删除超过保留天数的历史 JSONL（手动归档过的目标除外）|
| clean_days | 14 | 历史文件保留天数 |

## 运行时日志说明（可忽略的提示）

插件正常运行时，AstrBot / 后端日志中可能出现以下 WARN / ERROR，均属正常现象：

| 日志内容 | 级别 | 出现时机 | 原因与说明 |
|---|---|---|---|
| get_group_msg_history(...) 翻页停止: retcode=1200 消息XXX不存在 | WARN | NapCat 每群每轮定时导出时，最多 1 条 | NapCat 翻到了该群历史最早期边界，插件将其作为正常停止信号（防死循环）|
| [账号1] [error] 发生错误 Error: 消息XXX不存在 | ERROR | 与上一条同时出现 | NapCat 自身对同一个 1200 响应的内部打印，与插件无关 |
| WARN 似乎是旧版客户端，尝试仅通过序号获取引用消息 | WARN | 偶发，解析含引用（reply）的消息时 | NapCat 自身的兼容提示；正常时偶发，不会刷屏 |
| get_group_msg_history(...) 返回空消息列表（...） | WARN | SnowLuma 后端每群每轮翻到底时 | SnowLuma 的正常停止信号：其历史接口基于本地消息库，翻到最早处返回空列表，插件随即停止本群翻页 |

> 判断标准：只要导出文件正常生成、内容无重复（可通过 get_export_status 查看），上述日志都无需处理。
> 例外：若"旧版客户端"提示大量刷屏（每分钟几十条），说明翻页出现异常循环，请反馈作者。

## 使用前提

- AstrBot 使用 aiocqhttp 适配器连接 NapCat 或 SnowLuma（均为 OneBot v11）
- 后端需实现扩展 API：get_group_msg_history / get_friend_msg_history（两个平台均已实现）
- 后端本地需保存有需要导出的聊天记录（NapCat 缓存 / SnowLuma 消息库），详见上文「SnowLuma 使用须知」
- 多 bot：一个 AstrBot 上启用多个 aiocqhttp 平台实例（每个实例连一个 QQ 号的 NapCat/SnowLuma）

## 安装

```bash
# AstrBot 中执行（插件市场命令）
plugin i https://github.com/baizi51676-source/astrbot_plugin_chat_history_archiver
```

或下载 Release 附件 zip，解压到 AstrBot/data/plugins/，然后在 WebUI 启用。

## 如何更新

1. 通过 plugin i <仓库地址> 安装（推荐方式）：AstrBot 插件页直接点「更新」即可；若使用旧地址（astrbot_plugin_napcat_history_exporter）安装，GitHub 会自动重定向到新仓库，通常仍可正常更新，失败时改用上方新地址执行一次即可。
2. 通过 Release zip 手动安装：下载最新 zip 解压后覆盖 AstrBot/data/plugins/ 下插件目录（v2.0.0 起目录名为 astrbot_plugin_chat_history_archiver，若旧目录仍存在请删除），然后在 WebUI 停用→启用一次。
3. 通过插件市场安装：若旧条目无法检测更新，请卸载旧插件并按新名字重新安装（配置项需重新填写一次）。

升级不影响你的数据：历史 JSONL 归档与游标状态存放在 AstrBot 数据目录的导出目录（默认 data/workspaces/napcat_exports）中，不在插件目录里，覆盖/重装插件不会丢失任何已归档记录。

> 从 v1.4.0 起插件不再与 astrbot_plugin_group_forwarder_special 等外部插件联动搜索；查看/搜索/回溯均已内置为 LLM 工具。
