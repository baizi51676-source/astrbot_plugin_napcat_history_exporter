import asyncio
import json
from datetime import datetime
from pathlib import Path

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star

# 消息段类型 → 占位符（不导出媒体文件）
_SEG_PLACEHOLDER = {
    "image": "[图片]",
    "face": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "reply": "[引用消息]",
    "forward": "[合并转发]",
    "json": "[卡片消息]",
    "xml": "[卡片消息]",
}


class NapcatHistoryExporter(Star):
    """NapCat 历史聊天记录导出插件（OneBot v11 / aiocqhttp 适配）。

    通过 NapCat 扩展 API（get_group_msg_history / get_friend_msg_history）
    将历史聊天记录导出为 JSONL 文件（每行一条消息），供其他插件（如
    astrbot_plugin_group_forwarder_special）搜索联动。

    特性：
    - 两种模式：auto 定时循环增量导出（默认 120s 一次）/ manual 仅按需触发
    - 图片、表情、语音等媒体不导出，使用 [图片]/[表情]/[语音] 等占位符
    - 按天分文件：napcat_<群号>_YYYY-MM-DD.jsonl（私聊 napcat_private_<QQ>_*.jsonl）
    - 增量导出：记录每个目标的最新 message_seq，只拉新消息，不重复写入
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.export_dir = Path(config.get(
            "export_dir", "data/workspaces/napcat_exports"))
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.mode = str(config.get("mode", "auto"))
        self.interval = max(30, int(config.get("interval_seconds", 120)))
        self.batch = max(1, min(int(config.get("count_per_batch", 50)), 200))
        self.auto_friends = bool(config.get("auto_export_friends", False))
        self.admin_only = bool(config.get("admin_only", True))
        self.state_file = self.export_dir / "state.json"
        self._state: dict = self._load_state()
        self._client = None
        self._task: asyncio.Task | None = None

    # ---------------------------------------------------------------
    # 内部工具
    # ---------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {"group": {}, "private": {}}

    def _save_state(self) -> None:
        try:
            self.state_file.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            logger.error(f"保存导出状态失败: {e}")

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        if not self.admin_only:
            return True
        return event.is_admin()

    def _get_client(self):
        """获取 aiocqhttp 平台的 CQHttp 客户端（用于调用 NapCat 扩展 API）。"""
        if self._client is not None:
            return self._client
        try:
            platform = self.context.get_platform("aiocqhttp")
            if platform is None:
                logger.warning(
                    "未找到 aiocqhttp 平台实例（get_platform('aiocqhttp') 返回 None），"
                    "无法进行定时导出。请确认 AstrBot 已启用 aiocqhttp 适配器连接 NapCat。")
                return None
            self._client = platform.get_client()
            logger.info("已获取 aiocqhttp 客户端（CQHttp），可用于调用 NapCat API")
        except Exception as e:
            logger.error(f"获取 aiocqhttp 客户端失败: {e}")
            return None
        return self._client

    def _segments_to_text(self, segments) -> str:
        """消息段 → 文本。图片/表情等替换为占位符，不导出媒体。"""
        if isinstance(segments, str):
            return segments
        parts = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            t = seg.get("type", "")
            data = seg.get("data") or {}
            if t == "text":
                parts.append(data.get("text", ""))
            elif t == "at":
                parts.append(f"[At:{data.get('qq', '')}]")
            elif t == "file":
                parts.append(f"[文件:{data.get('name', '')}]")
            elif t in _SEG_PLACEHOLDER:
                parts.append(_SEG_PLACEHOLDER[t])
            else:
                parts.append(f"[{t}]")
        return "".join(parts)

    def _fmt_record(self, msg: dict, chat: str, target_id: str) -> dict:
        """OneBot 消息 → JSONL 行。"""
        sender = msg.get("sender") or {}
        try:
            ts = int(msg.get("time") or 0)
            t = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            t = ""
        return {
            "t": t,
            "chat": chat,
            "group_id": target_id,
            "user_id": str(sender.get("user_id", "")),
            "nickname": (sender.get("card") or sender.get("nickname") or "").strip(),
            "seq": msg.get("message_seq") or msg.get("message_id") or 0,
            "content": self._segments_to_text(msg.get("message", "")),
        }

    def _date_of(self, msg: dict) -> str:
        try:
            return datetime.fromtimestamp(int(msg.get("time") or 0)) \
                .strftime("%Y-%m-%d")
        except Exception:
            return "unknown"

    def _target_path(self, chat: str, target_id: str, date: str) -> Path:
        if chat == "group":
            return self.export_dir / f"napcat_{target_id}_{date}.jsonl"
        return self.export_dir / f"napcat_private_{target_id}_{date}.jsonl"

    def _write_records(self, path: Path, records: list) -> None:
        with open(path, "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    async def _fetch(self, action: str, key: str, target_id: str,
                     start_seq: int, count: int) -> list:
        """调用 NapCat 扩展 API 拉取一页历史消息。

        注意：NapCat 的 get_group_msg_history / get_friend_msg_history
        要求 group_id / user_id / message_seq 均为字符串类型。
        """
        client = self._get_client()
        if client is None:
            return []
        try:
            resp = await client.call_action(
                action, **{key: str(target_id), "message_seq": str(start_seq),
                           "count": count})
        except Exception as e:
            logger.error(f"调用 {action}({target_id}) 失败: {e}")
            return []
        if not isinstance(resp, dict):
            logger.warning(f"{action}({target_id}) 返回异常: {resp!r}")
            return []
        # 检查 NapCat 返回状态（retcode != 0 表示失败，data 可能为 null）
        retcode = resp.get("retcode", 0)
        if resp.get("status") == "failed" or (retcode is not None and retcode != 0):
            logger.warning(
                f"{action}({target_id}) 调用失败: retcode={retcode} "
                f"message={resp.get('message')!r} wording={resp.get('wording')!r}")
            return []
        data = resp.get("data")
        if isinstance(data, dict):
            return data.get("messages") or []
        if isinstance(data, list):
            return data
        return []

    @staticmethod
    def _seq_of(msg: dict) -> int:
        return msg.get("message_seq") or msg.get("message_id") or 0

    async def _export_target(self, chat: str, target_id: str,
                             limit: int = 0) -> int:
        """导出单个目标。

        limit > 0：按需导出最近 limit 条（覆盖式快照，同时更新游标）；
        limit = 0：增量导出（从上次游标续拉新消息）。
        返回本次新增写入的条数。
        """
        action = "get_group_msg_history" if chat == "group" \
            else "get_friend_msg_history"
        key = "group_id" if chat == "group" else "user_id"
        last_seq = self._state.get(chat, {}).get(target_id, 0)
        # NapCat 扩展 API：message_seq=0 表示从最新消息开始。
        # 因此无论按需还是增量，都从最新往回翻页；
        # 增量模式过滤 seq > last_seq 的新消息，遇到含旧消息的页即停止。
        start = 0
        fetched: list = []
        guard = 0
        while True:
            guard += 1
            if guard > 100:  # 防御：单次最多翻 100 页
                break
            msgs = await self._fetch(action, key, target_id, start, self.batch)
            if not msgs:
                break
            if limit > 0:
                fetched.extend(msgs)
                if len(fetched) >= limit:
                    break
            else:
                # 增量：只保留比游标新的消息
                fresh = [m for m in msgs if self._seq_of(m) > last_seq]
                fetched.extend(fresh)
                if len(fresh) < len(msgs):
                    break  # 本页已含游标或更早消息，说明新消息已全部取到
            # 向前翻页（拿更早的）
            start = min(self._seq_of(m) for m in msgs) - 1
            if start < 1 or len(fetched) >= 5000:
                break
        if not fetched:
            return 0
        if limit > 0:
            new = sorted(fetched[:limit], key=self._seq_of)
        else:
            new = sorted(fetched, key=self._seq_of)
        # 按天分文件追加
        written = 0
        by_date: dict = {}
        for m in new:
            by_date.setdefault(self._date_of(m), []).append(m)
        for date, msgs in by_date.items():
            path = self._target_path(chat, target_id, date)
            records = [self._fmt_record(m, chat, target_id) for m in msgs]
            self._write_records(path, records)
            written += len(records)
        # 更新游标
        max_seq = max(self._seq_of(m) for m in new)
        cur = self._state.setdefault(chat, {})
        if max_seq > cur.get(target_id, 0):
            cur[target_id] = max_seq
        self._save_state()
        return written

    async def _auto_export_once(self) -> int:
        """定时一轮：导出所有群（可选项私聊）的增量消息。"""
        client = self._get_client()
        if client is None:
            logger.warning("未获取到 aiocqhttp 客户端，本轮跳过")
            return 0
        total = 0
        try:
            groups = await client.call_action("get_group_list")
        except Exception as e:
            logger.error(f"获取群列表失败: {e}")
            groups = []
        groups = groups or []
        logger.info(f"定时导出开始：共 {len(groups)} 个群")
        for g in groups:
            gid = str(g.get("group_id", ""))
            if not gid:
                continue
            try:
                n = await self._export_target("group", gid)
                if n:
                    logger.info(f"群 {gid} 新增 {n} 条")
            except Exception as e:
                logger.error(f"导出群 {gid} 失败: {e}")
            total += n
        if self.auto_friends:
            try:
                friends = await client.call_action("get_friend_list")
            except Exception as e:
                logger.error(f"获取好友列表失败: {e}")
                friends = []
            for f in friends or []:
                uid = str(f.get("user_id", ""))
                if not uid:
                    continue
                try:
                    n = await self._export_target("private", uid)
                    if n:
                        logger.info(f"私聊 {uid} 新增 {n} 条")
                except Exception as e:
                    logger.error(f"导出私聊 {uid} 失败: {e}")
                total += n
        logger.info(f"定时导出完成，本轮新增 {total} 条（目录: {self.export_dir.resolve()}）")
        return total

    async def _auto_loop(self) -> None:
        logger.info(f"定时导出循环已启动，间隔 {self.interval}s，"
                    f"导出目录: {self.export_dir.resolve()}")
        while True:
            try:
                await self._auto_export_once()
            except Exception as e:
                logger.error(f"定时导出异常: {e}")
            await asyncio.sleep(self.interval)

    # ---------------------------------------------------------------
    # 生命周期
    # ---------------------------------------------------------------

    async def initialize(self) -> None:
        if self.mode == "auto":
            self._task = asyncio.create_task(self._auto_loop())
            logger.info("NapCat 历史导出器已启动定时模式（每 %ss 增量导出一次，"
                        "导出目录: %s）", self.interval, self.export_dir.resolve())
        else:
            logger.info("NapCat 历史导出器当前为 manual 模式，"
                        "仅在被 LLM 工具触发时导出（导出目录: %s）",
                        self.export_dir.resolve())

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ---------------------------------------------------------------
    # LLM 工具
    # ---------------------------------------------------------------

    @filter.llm_tool("export_group_history")
    async def export_group_history(self, event: AstrMessageEvent,
                                   group_id: str, count: int = 200):
        '''
        按需导出指定 QQ 群的历史聊天记录为 JSONL 文件（图片/表情等用占位符）。
        适合需要把某群聊天记录保存成文件、供后续搜索/分析的场景。
        与定时模式共用同一套增量游标，重复导出不会产生大量重复数据。

        Args:
          group_id(string): 目标 QQ 群号（纯数字，必填）
          count(number): 导出的消息条数上限（默认 200，最大 5000）

        返回: 导出摘要（新增条数、文件路径）
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        gid = group_id.strip()
        if not gid.isdigit():
            return f"❌ 群号格式错误：{group_id}。群号应为纯数字。"
        count = max(1, min(int(count), 5000))
        written = await self._export_target("group", gid, limit=count)
        path = self.export_dir
        return (f"✅ 群 {gid} 导出完成：新增 {written} 条"
                f"（导出目录: {path}）")

    @filter.llm_tool("export_private_history")
    async def export_private_history(self, event: AstrMessageEvent,
                                     user_id: str, count: int = 200):
        '''
        按需导出指定 QQ 好友的私聊历史记录为 JSONL 文件（图片/表情用占位符）。
        注意：NapCat 需保存有与该好友的聊天记录。

        Args:
          user_id(string): 目标 QQ 号（纯数字，必填）
          count(number): 导出的消息条数上限（默认 200，最大 5000）

        返回: 导出摘要（新增条数、文件路径）
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        uid = user_id.strip()
        if not uid.isdigit():
            return f"❌ QQ 号格式错误：{user_id}。应为纯数字。"
        count = max(1, min(int(count), 5000))
        written = await self._export_target("private", uid, limit=count)
        return (f"✅ 与 {uid} 的私聊导出完成：新增 {written} 条"
                f"（导出目录: {self.export_dir}）")

    @filter.llm_tool("export_all_incremental")
    async def export_all_incremental(self, event: AstrMessageEvent):
        '''
        手动触发一轮全量增量导出：所有群（及配置开启时的私聊）的新消息。
        与定时模式行为一致，适合 manual 模式或想立即同步时调用。

        返回: 本轮新增条数
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        written = await self._auto_export_once()
        return f"✅ 全量增量导出完成，本轮新增 {written} 条（目录: {self.export_dir}）"

    @filter.llm_tool("get_export_status")
    async def get_export_status(self, event: AstrMessageEvent):
        '''
        查看导出器状态：模式、导出目录、各群/私聊的最新导出游标与文件数。

        返回: 状态摘要
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        files = list(self.export_dir.glob("*.jsonl"))
        lines = [f"模式: {self.mode}"
                 f"（定时间隔 {self.interval}s）", f"导出目录: {self.export_dir}",
                 f"JSONL 文件数: {len(files)}"]
        for chat in ("group", "private"):
            st = self._state.get(chat, {})
            if st:
                lines.append(f"{chat}: " + ", ".join(
                    f"{k}@seq{v}" for k, v in list(st.items())[:10]))
        return "\n".join(lines)
