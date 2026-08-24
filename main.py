import asyncio
import json
from datetime import datetime, timedelta
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
    - 自动归档开关（auto_export）：开启后定时循环增量导出（默认 120s 一次）
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
        self._split_merged_files()  # v1.2.0: v1.1.0 单文件自动拆回按天文件
        self.auto_export = bool(config.get("auto_export", True))
        self.interval = max(30, int(config.get("interval_seconds", 120)))
        self.batch = max(1, min(int(config.get("count_per_batch", 50)), 200))
        self.auto_friends = bool(config.get("auto_export_friends", False))
        self.admin_only = bool(config.get("admin_only", True))
        # v1.3.0: 自动归档白名单 / 历史自动清理（仅循环导出模式生效）
        self.whitelist = [str(x).strip() for x in (config.get("whitelist") or [])
                          if str(x).strip()]
        self.auto_clean = bool(config.get("auto_clean", True))
        self.clean_days = max(1, int(config.get("clean_days", 14)))
        self.state_file = self.export_dir / "state.json"
        self._state: dict = self._load_state()
        self._client = None
        self._task: asyncio.Task | None = None
        self._last_clean: datetime | None = None  # v1.3.1: 上次自动清理时间（每12h一次）

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
        # 按天分文件：napcat_<群号>_YYYY-MM-DD.jsonl（私聊 napcat_private_<QQ>_*）
        if chat == "group":
            return self.export_dir / f"napcat_{target_id}_{date}.jsonl"
        return self.export_dir / f"napcat_private_{target_id}_{date}.jsonl"

    def _split_merged_files(self) -> None:
        """v1.2.0 迁移：若存在 v1.1.0 单文件 napcat_<群号>.jsonl，
        按行内 t 字段拆回按天文件 napcat_<群号>_YYYY-MM-DD.jsonl，
        随后删除单文件（避免与按天分文件模式冲突）。
        """
        try:
            if not self.export_dir.is_dir():
                return
            import re as _re
            for p in sorted(self.export_dir.glob("napcat_*.jsonl")):
                if p.name == "state.json":
                    continue
                m = _re.match(r"napcat_(private_)?(\d+)\.jsonl$", p.name)
                if not m:
                    continue  # 已是按天文件，跳过
                prefix = m.group(1) or ""
                gid = m.group(2)
                try:
                    lines = [ln for ln in
                             p.read_text(encoding="utf-8", errors="replace")
                             .splitlines() if ln.strip()]
                except Exception as e:
                    logger.error(f"[NapCatExporter] 拆分读取失败 {p.name}: {e}")
                    continue
                if not lines:
                    p.unlink(missing_ok=True)
                    continue
                # 按行内 t 字段的前 10 位（YYYY-MM-DD）分组
                by_date: dict = {}
                for ln in lines:
                    d = ""
                    try:
                        d = str(json.loads(ln).get("t", ""))[:10]
                    except Exception:
                        pass
                    by_date.setdefault(d or "unknown", []).append(ln)
                for date, day_lines in by_date.items():
                    target = self.export_dir / \
                        f"napcat_{prefix}{gid}_{date}.jsonl"
                    try:
                        with open(target, "a", encoding="utf-8") as f:
                            f.write("\n".join(day_lines) + "\n")
                        logger.info(f"[NapCatExporter] 拆分: {p.name} → "
                                    f"{target.name}（{len(day_lines)}行）")
                    except Exception as e:
                        logger.error(f"[NapCatExporter] 拆分写入失败 "
                                     f"{target.name}: {e}")
                p.unlink(missing_ok=True)
                logger.info(f"[NapCatExporter] 单文件已删除: {p.name}")
        except Exception as e:
            logger.error(f"[NapCatExporter] 单文件拆分迁移异常: {e}")

    def _merge_write(self, path: Path, records: list) -> int:
        """v1.3.3: 合并写入——读现有文件行 + 新记录，按 seq 去重、
        按 t 排序后整体重写。文件自愈：不依赖内存去重，永不重复、有序。"""
        merged: dict = {}
        if path.exists():
            for ln in path.read_text(encoding="utf-8",
                                     errors="replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    r = json.loads(ln)
                    key = str(r.get("seq") or
                              (r.get("user_id", "") + r.get("t", "")))
                    merged[key] = r
                except Exception:
                    continue
        for r in records:
            key = str(r.get("seq") or
                      (r.get("user_id", "") + r.get("t", "")))
            merged[key] = r  # 新记录覆盖旧记录
        ordered = sorted(merged.values(),
                         key=lambda r: str(r.get("t", "")))
        with open(path, "w", encoding="utf-8") as f:
            for r in ordered:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return len(ordered)

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
            # v1.3.2: NapCat 翻页锚点消息不存在（retcode=1200，如 message_seq 传了
            # 不存在的 id）属预期行为 → 降级为 warning，停止翻页（已获取的消息保留）
            msg = str(e)
            if "不存在" in msg or getattr(e, "retcode", None) == 1200:
                logger.warning(f"{action}({target_id}) 翻页停止: {msg}")
            else:
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
    def _anchor_of(msg: dict) -> int | None:
        """v1.3.4: 翻页定位锚点——优先 real_seq（会话内序号，单调递增，
        NapCat 内部用其定位），否则回退 message_id。"""
        rs = msg.get("real_seq")
        if rs is not None:
            try:
                return int(str(rs).strip())
            except Exception:
                pass
        return None

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
                             limit: int = 0, today_only: bool = False,
                             start_ts: int = 0, end_ts: int = 0) -> int:
        """导出单个目标。

        limit > 0：按需导出最近 limit 条（去重追加，同时更新游标）；
        limit = 0：增量导出（以 time 时间戳为边界 + message_id 去重）。
        today_only=True（自动归档）：只归档当天消息，不导历史。
        start_ts/end_ts > 0：回溯导出 [start_ts, end_ts] 时间段消息
        （手动归档，不推进游标，不影响后续增量）。
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
        # 循环导出模式：只归档当天（本地时区 0 点起）
        today_start = 0
        if today_only:
            today_start = int(datetime.combine(
                datetime.now().date(), datetime.min.time()).timestamp())
        # NapCat 扩展 API：message_seq=0 表示从最新消息开始，只能往回翻页
        start = 0
        fetched: list = []
        guard = 0
        local_seen = set(seen)  # v1.3.3: 本轮已收集 id，防页间重叠
        consecutive_empty = 0  # v1.3.4: 连续无新增页数，防翻页死循环
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
                fresh = [m for m in msgs
                         if self._mid_of(m) not in local_seen]
                fetched.extend(fresh)
                local_seen.update(self._mid_of(m) for m in fresh)
                if len(fetched) >= limit:
                    break
                # 本页没有新消息且包含旧消息 → 没有更多可导出的了
                if not fresh and any(self._time_of(m) <= last_t for m in msgs):
                    break
                if not fresh:
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        break
                else:
                    consecutive_empty = 0
            else:
                # 回溯时间段导出（手动归档历史，不推进游标）
                if start_ts > 0:
                    zone = [m for m in msgs
                            if end_ts <= 0 or self._time_of(m) <= end_ts]
                    fresh = [m for m in zone
                             if self._time_of(m) >= start_ts
                             and self._mid_of(m) not in local_seen]
                    fetched.extend(fresh)
                    local_seen.update(self._mid_of(m) for m in fresh)
                    if any(self._time_of(m) < start_ts for m in msgs):
                        break  # 已到时间段起点
                    if zone and not fresh:
                        consecutive_empty += 1
                        if consecutive_empty >= 3:
                            break
                    elif zone and fresh:
                        consecutive_empty = 0
                    # zone 为空（页内全是 end_ts 之后的消息）→ 不计数，继续往前翻
                else:
                    # 增量：只保留 time > last_t 且未写过的消息
                    fresh = [m for m in msgs
                             if self._time_of(m) > last_t
                             and self._mid_of(m) not in local_seen]
                    if today_only:
                        # 自动归档只归档当天
                        fresh = [m for m in fresh
                                 if self._time_of(m) >= today_start]
                    fetched.extend(fresh)
                    local_seen.update(self._mid_of(m) for m in fresh)
                    if today_only:
                        # 遇当天之前（含昨天及更早）的消息 → 本页已到当天边界
                        if any(self._time_of(m) < today_start for m in msgs):
                            break
                    else:
                        if any(self._time_of(m) <= last_t for m in msgs):
                            break
                    if not fresh:
                        consecutive_empty += 1
                        if consecutive_empty >= 3:
                            break
                    else:
                        consecutive_empty = 0
            # 向前翻页（拿更早的）：v1.3.4 优先用 real_seq（单调递增，
            # 传 min-1 精确定位更早消息）；无 real_seq 时回退页内最小
            # message_id（真实存在，NapCat 返回其之前更早的消息）
            anchors = [a for a in (self._anchor_of(m) for m in msgs)
                       if a is not None]
            if anchors:
                start = min(anchors) - 1
            else:
                start = min(self._seq_of(m) for m in msgs)
            if start < 1 or len(fetched) >= 5000:
                break
        if not fetched:
            return 0
        # 按 time 排序
        new = sorted(fetched[:limit] if limit > 0 else fetched,
                     key=self._time_of)
        # 按天分文件写入（v1.3.3: 统一合并-去重-排序-重写，文件自愈）
        written = 0
        by_date: dict = {}
        for m in new:
            by_date.setdefault(self._date_of(m), []).append(m)
        for date, msgs in by_date.items():
            path = self._target_path(chat, target_id, date)
            records = [self._fmt_record(m, chat, target_id) for m in msgs]
            self._merge_write(path, records)
            written += len(records)
        # 更新游标：time 边界 + 最近 5000 个已写 message_id
        max_t = max(self._time_of(m) for m in new)
        new_ids = [self._mid_of(m) for m in new if self._mid_of(m)]
        seen |= set(new_ids)
        cur = self._state.setdefault(chat, {})
        if today_only:
            # v1.3.2: 当天模式游标不推进（固定为今天 0 点 -1 秒），
            # 每轮都尝试拉取当天全部消息，配合 message_id 去重：
            # 翻页失败时也不漏"最新"消息，翻页可用时能补全当天更早的
            cur[target_id] = {"t": today_start - 1,
                              "ids": list(seen)[-5000:]}
        elif start_ts > 0:
            # v1.4.0: 回溯时间段归档不推进游标（不影响后续增量），仅更新去重 ids
            cur[target_id] = {"t": last_t, "ids": list(seen)[-5000:]}
        else:
            cur[target_id] = {"t": max_t, "ids": list(seen)[-5000:]}
        self._save_state()
        return written

    async def _auto_export_once(self, apply_rules: bool = True) -> int:
        """定时一轮：导出所有群（可选项私聊）的增量消息。

        apply_rules=True（循环导出模式）：应用群白名单、只归档当天消息；
        apply_rules=False（用户/LLM 手动触发全量增量）：不过滤，可导历史。
        """
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
        if apply_rules and self.whitelist:
            before = len(groups)
            groups = [g for g in groups
                      if str(g.get("group_id", "")) in self.whitelist]
            if len(groups) != before:
                logger.info(f"白名单过滤：{before} 个群 → {len(groups)} 个（仅白名单内导出）")
            if not groups:
                logger.info("白名单内没有可导出的群，本轮跳过")
                return 0
        logger.info(f"定时导出开始：共 {len(groups)} 个群")
        for g in groups:
            gid = str(g.get("group_id", ""))
            if not gid:
                continue
            try:
                n = await self._export_target("group", gid,
                                              today_only=apply_rules)
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
                    n = await self._export_target("private", uid,
                                                  today_only=apply_rules)
                    if n:
                        logger.info(f"私聊 {uid} 新增 {n} 条")
                except Exception as e:
                    logger.error(f"导出私聊 {uid} 失败: {e}")
                total += n
        logger.info(f"定时导出完成，本轮新增 {total} 条（目录: {self.export_dir.resolve()}）")
        return total

    def _cleanup_old_files(self) -> None:
        """v1.3.0: 自动清理过期历史文件（仅循环导出模式调用）。

        删除文件名日期早于（今天 - clean_days）的 napcat_*_YYYY-MM-DD.jsonl；
        手动归档（protected）过的目标文件不清理。
        """
        if not self.auto_clean:
            return
        cutoff = (datetime.now().date()
                  - timedelta(days=self.clean_days)).strftime("%Y-%m-%d")
        protected = set()
        for g in self._state.get("protected", {}).get("group", []) or []:
            protected.add(f"napcat_{g}_")
        for u in self._state.get("protected", {}).get("private", []) or []:
            protected.add(f"napcat_private_{u}_")
        import re as _re
        removed = 0
        for f in self.export_dir.glob("napcat_*_????-??-??.jsonl"):
            m = _re.match(
                r"napcat_(private_)?(\d+)_(\d{4}-\d{2}-\d{2})\.jsonl$", f.name)
            if not m:
                continue
            prefix = f"napcat_{m.group(1) or ''}{m.group(2)}_"
            if prefix in protected:
                continue  # 手动归档过的目标，不自动清理
            fdate = m.group(3)
            if fdate < cutoff:
                try:
                    f.unlink(missing_ok=True)
                    logger.info(f"[NapCatExporter] 自动清理过期文件: "
                                f"{f.name}（早于 {cutoff}）")
                    removed += 1
                except Exception as e:
                    logger.error(f"[NapCatExporter] 清理文件失败 {f.name}: {e}")
        if removed:
            logger.info(f"[NapCatExporter] 本轮自动清理 {removed} 个过期文件")

    async def _auto_loop(self) -> None:
        logger.info(f"定时导出循环已启动，间隔 {self.interval}s，"
                    f"导出目录: {self.export_dir.resolve()}"
                    + (f"，白名单: {self.whitelist}" if self.whitelist else "")
                    + (f"，自动清理: {self.clean_days}天前"
                       if self.auto_clean else "，自动清理: 关闭"))
        while True:
            try:
                await self._auto_export_once()
            except Exception as e:
                logger.error(f"定时导出异常: {e}")
            # v1.3.1: 自动清理每 12 小时执行一次（非每轮）
            if self._last_clean is None or \
                    datetime.now() - self._last_clean >= timedelta(hours=12):
                try:
                    self._cleanup_old_files()
                    self._last_clean = datetime.now()
                except Exception as e:
                    logger.error(f"自动清理异常: {e}")
            await asyncio.sleep(self.interval)

    # ---------------------------------------------------------------
    # 生命周期
    # ---------------------------------------------------------------

    async def initialize(self) -> None:
        if self.auto_export:
            self._task = asyncio.create_task(self._auto_loop())
            logger.info("NapCat 历史导出器已启动自动归档（每 %ss 增量导出一次，"
                        "导出目录: %s）", self.interval, self.export_dir.resolve())
        else:
            logger.info("NapCat 历史导出器自动归档已关闭，"
                        "仅在被 LLM 工具触发时归档（导出目录: %s）",
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
        if written > 0:
            # v1.3.0: 手动归档过的目标加入保护名单，自动清理不删除其文件
            self._state.setdefault("protected", {}).setdefault("group", [])
            if gid not in self._state["protected"]["group"]:
                self._state["protected"]["group"].append(gid)
                self._save_state()
                logger.info(f"[NapCatExporter] 群 {gid} 已加入手动归档保护名单")
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
        if written > 0:
            # v1.3.0: 手动归档过的目标加入保护名单，自动清理不删除其文件
            self._state.setdefault("protected", {}).setdefault("private", [])
            if uid not in self._state["protected"]["private"]:
                self._state["protected"]["private"].append(uid)
                self._save_state()
                logger.info(f"[NapCatExporter] 私聊 {uid} 已加入手动归档保护名单")
        return (f"✅ 与 {uid} 的私聊导出完成：新增 {written} 条"
                f"（导出目录: {self.export_dir}）")

    @filter.llm_tool("export_all_incremental")
    async def export_all_incremental(self, event: AstrMessageEvent,
                                     group_id: str = "",
                                     start_date: str = "",
                                     end_date: str = ""):
        '''
        立即归档（手动触发，不受自动归档开关/白名单/当天限制）：
        默认增量归档全部群；可指定群号与时间段回溯归档历史消息。

        Args:
          group_id(string): 目标群号（可选，留空=全部群）
          start_date(string): 开始日期 YYYY-MM-DD（可选，留空=不限起点）
          end_date(string): 结束日期 YYYY-MM-DD（可选，留空=不限终点）

        返回: 归档摘要（新增条数、文件路径）
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        start_ts = 0
        end_ts = 0
        try:
            if start_date:
                start_ts = int(datetime.strptime(
                    start_date.strip(), "%Y-%m-%d").timestamp())
            if end_date:
                end_ts = int(datetime.strptime(
                    end_date.strip() + " 23:59:59",
                    "%Y-%m-%d %H:%M:%S").timestamp())
        except Exception:
            return ("❌ 日期格式错误：应为 YYYY-MM-DD，例如 2026-08-20。")
        if start_ts and end_ts and start_ts > end_ts:
            return "❌ 开始日期不能晚于结束日期。"
        # 指定群
        if group_id:
            gid = group_id.strip()
            if not gid.isdigit():
                return f"❌ 群号格式错误：{group_id}。群号应为纯数字。"
            written = await self._export_target(
                "group", gid, start_ts=start_ts, end_ts=end_ts)
            return (f"✅ 群 {gid} 归档完成：新增 {written} 条"
                    f"（导出目录: {self.export_dir}）")
        # 全部群
        client = self._get_client()
        if client is None:
            return "❌ 未获取到 aiocqhttp 客户端，请检查适配器配置。"
        try:
            groups = await client.call_action("get_group_list")
        except Exception as e:
            logger.error(f"获取群列表失败: {e}")
            groups = []
        total = 0
        for g in groups or []:
            gid = str(g.get("group_id", ""))
            if not gid:
                continue
            try:
                n = await self._export_target("group", gid,
                                              start_ts=start_ts,
                                              end_ts=end_ts)
                total += n
            except Exception as e:
                logger.error(f"归档群 {gid} 失败: {e}")
        return (f"✅ 全部群归档完成，新增 {total} 条"
                f"（导出目录: {self.export_dir}）")

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
            f"自动归档: {'开启' if self.auto_export else '关闭'}"
            f"（间隔 {self.interval}s）",
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

    # ---------------------------------------------------------------
    # 归档读取（v1.4.0 整合自特殊版：查看/搜索/群列表）
    # ---------------------------------------------------------------

    def _read_group_files(self, group_id: str) -> list:
        """返回某群全部归档文件路径（按文件名倒序 = 新 → 旧）。"""
        gid = group_id.strip()
        files = [p for p in self.export_dir.iterdir()
                 if p.is_file()
                 and (p.name.startswith(f"napcat_{gid}_")
                      or p.name.startswith(f"napcat_{gid}."))]
        files.sort(key=lambda p: p.name, reverse=True)
        return files

    def _parse_archived_line(self, raw: str) -> dict | None:
        """解析 JSONL 归档行。"""
        raw = raw.strip()
        if not raw.startswith("{"):
            return None
        try:
            d = json.loads(raw)
        except Exception:
            return None
        return {
            "t": d.get("t", ""),
            "user_id": str(d.get("user_id", "") or ""),
            "nickname": d.get("nickname", ""),
            "content": d.get("content", ""),
        }

    @filter.llm_tool("get_group_message_history")
    async def get_group_message_history(self, event: AstrMessageEvent,
                                        group_id: str, count: int = 20):
        '''
        读取指定群已归档的聊天记录（跨天文件合并，按时间正序返回最近 N 条）。
        适用于查看本插件导出的历史消息，无需依赖 LLM 推理。

        Args:
          group_id(string): 目标 QQ 群号（纯数字，必填）
          count(number): 返回条数上限（默认 20，最大 200）

        返回: 最近 N 条消息文本（时间正序）
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        gid = group_id.strip()
        if not gid.isdigit():
            return f"❌ 群号格式错误：{group_id}。群号应为纯数字。"
        count = max(1, min(int(count), 200))
        files = self._read_group_files(gid)
        if not files:
            return f"📭 群 {gid} 暂无归档记录（目录: {self.export_dir}）"
        all_msgs: list[dict] = []
        for fpath in files:
            try:
                raw_lines = fpath.read_text(encoding="utf-8",
                                            errors="replace").splitlines()
            except Exception as e:
                logger.error(f"读取归档文件失败 {fpath}: {e}")
                continue
            for raw in raw_lines:
                info = self._parse_archived_line(raw)
                if info:
                    all_msgs.append(info)
        all_msgs.sort(key=lambda m: m["t"])
        recent = all_msgs[-count:]
        lines = [f"[{m['t']}] {m['nickname']}: {m['content']}"
                 for m in recent]
        return (f"📋 群 {gid} 最近 {len(recent)} 条消息:\n"
                + "\n".join(lines))

    @filter.llm_tool("search_archived_messages")
    async def search_archived_messages(self, event: AstrMessageEvent,
                                       group_id: str, keyword: str = "",
                                       date: str = "",
                                       user_id: str = "",
                                       nickname: str = "",
                                       count: int = 20):
        '''
        在指定群已归档记录中搜索消息（纯程序过滤，不依赖 LLM 推理）。
        条件可任意组合，全部满足才命中。

        Args:
          group_id(string): 目标 QQ 群号（纯数字，必填）
          keyword(string): 消息内容关键词（子串匹配，不区分大小写，可选）
          date(string): 日期过滤 YYYY-MM-DD（可选）
          user_id(string): QQ 号精确匹配（可选）
          nickname(string): 昵称包含该字符串（可选）
          count(number): 返回条数上限（默认 20，最大 200）

        返回: 命中的消息文本（时间正序）
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        gid = group_id.strip()
        if not gid.isdigit():
            return f"❌ 群号格式错误：{group_id}。群号应为纯数字。"
        count = max(1, min(int(count), 200))
        kw = keyword.strip() if keyword else None
        dt = date.strip() if date else None
        uid = user_id.strip() if user_id else None
        nick = nickname.strip() if nickname else None
        files = self._read_group_files(gid)
        if not files:
            return f"📭 群 {gid} 暂无归档记录（目录: {self.export_dir}）"
        hits: list[dict] = []
        for fpath in files:
            try:
                raw_lines = fpath.read_text(encoding="utf-8",
                                            errors="replace").splitlines()
            except Exception as e:
                logger.error(f"读取归档文件失败 {fpath}: {e}")
                continue
            for raw in raw_lines:
                info = self._parse_archived_line(raw)
                if not info:
                    continue
                if dt and not str(info["t"]).startswith(dt):
                    continue
                if kw and kw.lower() not in info["content"].lower():
                    continue
                if uid and info["user_id"] != uid:
                    continue
                if nick and nick.lower() not in info["nickname"].lower():
                    continue
                hits.append(info)
        hits.sort(key=lambda m: m["t"])
        result = hits[-count:]
        lines = [f"[{m['t']}] {m['nickname']}({m['user_id']}): {m['content']}"
                 for m in result]
        return (f"🔍 群 {gid} 搜索到 {len(hits)} 条"
                f"（显示最近 {len(result)} 条）:\n" + "\n".join(lines))

    @filter.llm_tool("list_archived_groups")
    async def list_archived_groups(self, event: AstrMessageEvent):
        '''
        列出已有归档记录的群号列表（按文件名提取，去重排序）。

        返回: 群号列表
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        import re as _re
        groups = set()
        for p in self.export_dir.glob("napcat_*.jsonl"):
            m = _re.match(r"napcat_(\d+)_\d{4}-\d{2}-\d{2}\.jsonl$", p.name)
            if m:
                groups.add(m.group(1))
                continue
            m2 = _re.match(r"napcat_(\d+)\.jsonl$", p.name)
            if m2:
                groups.add(m2.group(1))
        if not groups:
            return "📭 暂无任何归档记录（目录: {})".format(self.export_dir)
        lines = sorted(groups)
        return "📂 已有归档的群:\n" + "\n".join(lines)
