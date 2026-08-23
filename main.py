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

    def _write_records(self, path: Path, records: list, expect: int = 0) -> None:
        try:
            with open(path, "a", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            # 写后验证
            real = path.resolve()
            try:
                size = real.stat().st_size
            except Exception:
                size = -1
            logger.info(f"[NapCatExporter] 写入文件成功: {real}"
                        f"（{len(records)}条, 文件大小 {size} 字节）")
        except Exception as e:
            logger.error(f"[NapCatExporter] 写入文件失败: {path.resolve()} - {e}",
                         exc_info=True)
            raise

    async def _fetch(self, action: str, key: str, target_id: str,
                     start_seq: int, count: int) -> list:
        """调用 NapCat 扩展 API 拉取一页历史消息。

        注意：NapCat 的 get_group_msg_history / get_friend_msg_history
        要求 group_id / user_id / message_seq 均为字符串类型。

        aiocqhttp 的 call_action 已自动解包，resp 可能是：
          a) {"messages": [...]}                    —— NapCat 实际返回（解包后）
          b) {"data": {"messages": [...]}}          —— 标准 OneBot 包装
          c) {"data": [...]}                        —— data 直接是列表
          d) {"status": "failed", "retcode": 1400}  —— 调用失败
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
        # 失败检测
        retcode = resp.get("retcode")
        if resp.get("status") == "failed" or (retcode is not None and retcode != 0):
            logger.warning(
                f"{action}({target_id}) 调用失败: retcode={retcode} "
                f"message={resp.get('message')!r} wording={resp.get('wording')!r}")
            return []
        # 解析消息列表（兼容三种结构）
        msgs = None
        if "messages" in resp:
            msgs = resp.get("messages")
        else:
            data = resp.get("data")
            if isinstance(data, dict):
                msgs = data.get("messages")
            elif isinstance(data, list):
                msgs = data
        if msgs is None:
            logger.warning(f"{action}({target_id}) 返回格式无法解析: {str(resp)[:200]}")
            return []
        if not msgs:
            logger.warning(
                f"{action}({target_id}) 返回空消息列表"
                f"（NapCat 本地可能无该会话的消息记录/AIO 缓存）")
            return []
        return msgs

    @staticmethod
    def _seq_of(msg: dict) -> int:
        return msg.get("message_seq") or msg.get("message_id") or 0

    @staticmethod
    def _mid_of(msg: dict) -> str:
        """消息唯一 ID（用于去重）。"""
        return str(msg.get("message_id") or msg.get("message_seq") or "")

    @staticmethod
    def _time_of(msg: dict) -> int:
        try:
            return int(msg.get("time") or 0)
        except Exception:
            return 0

    async def _export_target(self, chat: str, target_id: str,
                             limit: int = 0) -> int:
        """导出单个目标。

        limit > 0：按需导出最近 limit 条（去重追加，同时更新游标）；
        limit = 0：增量导出（以 time 时间戳为边界 + message_id 去重）。
        返回本次新增写入的条数。

        注意：NapCat 返回的 message_seq = message_id（全局消息 ID），
        并非单调递增，不能用作增量游标；因此使用 time 做边界，
        并用已写 message_id 集合做去重（避免同秒消息重复/遗漏）。
        """
        action = "get_group_msg_history" if chat == "group" \
            else "get_friend_msg_history"
        key = "group_id" if chat == "group" else "user_id"
        # 游标结构: {"t": 最后导出消息的time, "ids": [已写message_id, 最多5000]}
        st = self._state.get(chat, {}).get(target_id)
        if isinstance(st, dict):
            last_t = int(st.get("t") or 0)
            seen = set(st.get("ids") or [])
        else:
            if st:  # 旧格式 int 游标（message_id 不单调，不可用）
                logger.info(f"[NapCatExporter] {target_id} 检测到旧格式游标 "
                            f"{st!r}（message_id 不单调，已重置为 0，将全量补导）")
            last_t = 0
            seen = set()
        # NapCat 扩展 API：message_seq=0 表示从最新消息开始，只能往回翻页
        start = 0
        fetched: list = []
        guard = 0
        while True:
            guard += 1
            if guard > 100:  # 防御：单次最多翻 100 页
                logger.warning(f"[NapCatExporter] {target_id} 翻页超过 100 页，强制停止")
                break
            msgs = await self._fetch(action, key, target_id, start, self.batch)
            if not msgs:
                break
            if limit > 0:
                # 按需导出：收集未写过的消息，达到 limit 条为止
                fresh = [m for m in msgs if self._mid_of(m) not in seen]
                fetched.extend(fresh)
                if len(fetched) >= limit:
                    break
                # 本页没有新消息且包含旧消息 → 没有更多可导出的了
                if not fresh and any(self._time_of(m) <= last_t for m in msgs):
                    break
            else:
                # 增量：只保留 time > last_t 且未写过的消息
                fresh = [m for m in msgs
                         if self._time_of(m) > last_t
                         and self._mid_of(m) not in seen]
                fetched.extend(fresh)
                # 本页出现 time <= last_t 的消息 → 已到导出边界，停止
                if any(self._time_of(m) <= last_t for m in msgs):
                    logger.info(f"[NapCatExporter] {target_id} 第{guard}页到达边界 "
                                f"(页内{len(msgs)}条, fresh {len(fresh)}条)")
                    break
            # 向前翻页（拿更早的）：用最小 message_seq-1 定位
            start = min(self._seq_of(m) for m in msgs) - 1
            if start < 1 or len(fetched) >= 5000:
                break
        if not fetched:
            logger.info(f"[NapCatExporter] {target_id} 无新消息"
                        f"（游标 t={last_t}，本轮 fetched=0）")
            return 0
        # 按 time 排序
        new = sorted(fetched[:limit] if limit > 0 else fetched,
                     key=self._time_of)
        # 按天分文件追加
        written = 0
        by_date: dict = {}
        for m in new:
            by_date.setdefault(self._date_of(m), []).append(m)
        for date, msgs in by_date.items():
            path = self._target_path(chat, target_id, date)
            records = [self._fmt_record(m, chat, target_id) for m in msgs]
            self._write_records(path, records, expect=len(records))
            written += len(records)
        logger.info(f"[NapCatExporter] {target_id} 写入 {written} 条"
                    f"（文件: {self.export_dir.resolve()}）")
        # 更新游标：time 边界 + 最近 5000 个已写 message_id
        max_t = max(self._time_of(m) for m in new)
        new_ids = [self._mid_of(m) for m in new if self._mid_of(m)]
        seen |= set(new_ids)
        cur = self._state.setdefault(chat, {})
        cur[target_id] = {"t": max_t, "ids": list(seen)[-5000:]}
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
        # 启动时打印关键路径，便于核对写入/统计目录是否一致
        import os as _os
        logger.info(f"[NapCatExporter] 进程 CWD: {_os.getcwd()}")
        logger.info(f"[NapCatExporter] export_dir(配置): {self.export_dir!r}")
        logger.info(f"[NapCatExporter] export_dir(绝对): {self.export_dir.resolve()}")
        logger.info(f"[NapCatExporter] state_file: {self.state_file.resolve()}")
        logger.info(f"[NapCatExporter] 当前游标: {json.dumps(self._state, ensure_ascii=False)}")
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

    @filter.llm_tool("debug_group_history_api")
    async def debug_group_history_api(self, event: AstrMessageEvent,
                                      group_id: str):
        '''
        诊断工具：直接调用 NapCat get_group_msg_history 接口并返回原始响应。
        用于排查"定时导出 0 条/无文件"问题——可以看到 NapCat 到底返回了什么
        （retcode、message、消息数量等）。

        Args:
          group_id(string): 目标 QQ 群号（纯数字，必填）

        返回: NapCat 原始响应 JSON（截断 1500 字符）
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        gid = group_id.strip()
        if not gid.isdigit():
            return f"❌ 群号格式错误：{group_id}。群号应为纯数字。"
        client = self._get_client()
        if client is None:
            return "❌ 未获取到 aiocqhttp 客户端，请检查适配器配置。"
        try:
            resp = await client.call_action(
                "get_group_msg_history", group_id=gid,
                message_seq="0", count=5)
            # 第二页：从第一页最小 message_seq-1 往回翻，验证序列单调性
            page1 = None
            if isinstance(resp, dict):
                page1 = resp.get("messages")
            elif isinstance(resp, list):
                page1 = resp
            resp2 = None
            if page1:
                seqs = [int(m.get("message_seq") or 0) for m in page1 if isinstance(m, dict)]
                if seqs:
                    nxt = min(seqs) - 1
                    if nxt >= 1:
                        resp2 = await client.call_action(
                            "get_group_msg_history", group_id=gid,
                            message_seq=str(nxt), count=5)
        except Exception as e:
            return f"❌ 调用 get_group_msg_history 异常: {e}"
        s = json.dumps(resp, ensure_ascii=False)[:1500]
        out = [f"get_group_msg_history({gid}) 原始响应:\n{s}"]
        # 单调性分析
        def brief(msgs, tag):
            if not msgs:
                return f"{tag}: (空)"
            lines = []
            for m in msgs[:5]:
                if isinstance(m, dict):
                    lines.append(f"  seq={m.get('message_seq')} id={m.get('message_id')} "
                                 f"time={m.get('time')} type={m.get('message_type')}")
            return f"{tag}（{len(msgs)}条）:\n" + "\n".join(lines)
        out.append(brief(page1, "第1页(最新)"))
        if isinstance(resp2, dict):
            out.append(brief(resp2.get("messages"), "第2页(更早)"))
        elif isinstance(resp2, list):
            out.append(brief(resp2, "第2页(更早)"))
        return "\n".join(out)

    @filter.llm_tool("get_export_status")
    async def get_export_status(self, event: AstrMessageEvent):
        '''
        查看导出器状态：模式、导出目录、各群/私聊的最新导出游标与文件数。

        返回: 状态摘要
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        exp = self.export_dir.resolve()
        files = sorted(p.name for p in exp.glob("*.jsonl"))
        lines = [
            f"模式: {self.mode}（定时间隔 {self.interval}s）",
            f"导出目录(配置值): {self.export_dir!r}",
            f"导出目录(绝对路径): {exp}",
            f"state.json 路径: {self.state_file.resolve()}",
            f"JSONL 文件数: {len(files)}",
        ]
        if files:
            lines.append("JSONL 文件列表:\n  " + "\n  ".join(files[:20]))
        # 列出导出目录内的所有内容（含 state.json）
        try:
            items = sorted(p.name for p in exp.iterdir())
            lines.append(f"导出目录内容({len(items)}项): "
                         + (", ".join(items[:30]) if items else "(空)"))
        except Exception as e:
            lines.append(f"读取导出目录失败: {e}")
        # state.json 实际内容摘要
        try:
            raw = self.state_file.read_text(encoding="utf-8")
            lines.append(f"state.json 内容: {raw[:400]}")
        except Exception as e:
            lines.append(f"读取 state.json 失败: {e}")
        for chat in ("group", "private"):
            st = self._state.get(chat, {})
            if st:
                def _fmt(v):
                    if isinstance(v, dict):
                        return f"t={v.get('t')}(ids={len(v.get('ids') or [])})"
                    return f"旧格式seq={v}"
                lines.append(f"{chat}: " + ", ".join(
                    f"{k}@{_fmt(v)}" for k, v in list(st.items())[:10]))
        return "\n".join(lines)
