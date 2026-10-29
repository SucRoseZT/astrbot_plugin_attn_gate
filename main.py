import asyncio
import time

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import At, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

EXIT_SYSTEM_PROMPT = (
    "你是群聊里的一名 AI 成员。请根据窗口期间的聊天记录判断：此刻是否还有继续参与这段对话的必要。"
    "判断标准：最近的消息里有没有指向你的提问或回应、你未兑现的承诺、正在进行且你深度参与的话题。"
    "如果都没有，就应该退出把注意力还给别的群。只回答两个字：继续 或 退出。"
)


@register("attn_gate", "sugarcane", "群聊注意力硬闸门", "1.1.0")
class AttnGate(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._windows: dict[str, dict] = {}
        self._history: dict[str, list] = {}

    # ---------- 工具 ----------

    def _cfg(self, key: str, default):
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _enabled_group(self, group_id: str) -> bool:
        groups = self._cfg("groups", []) or []
        return not groups or str(group_id) in {str(g) for g in groups}

    @staticmethod
    def _self_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_self_id() or "")
        except Exception:
            return ""

    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        if bool(getattr(event, "is_at_or_wake_command", False)):
            return True
        sid = self._self_id(event)
        if sid:
            try:
                for comp in event.message_obj.message:
                    if isinstance(comp, At) and str(getattr(comp, "qq", "")) == sid:
                        return True
            except Exception:
                pass
        return False

    def _state(self, group_id: str) -> dict:
        return self._windows.setdefault(
            group_id,
            {"until": 0.0, "opened_at": 0.0, "transcript": [], "checking": False},
        )

    def _record(self, st: dict, sender: str, text: str) -> None:
        st["last_activity"] = time.time()
        text = (text or "").strip()
        if not text:
            return
        cap = int(self._cfg("transcript_max_messages", 30) or 30)
        st["transcript"].append(f"{sender}: {text}"[:300])
        del st["transcript"][:-cap]

    def _record_history(self, group_id: str, sender: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        cap = max(int(self._cfg("wake_history_count", 20) or 20) + 10, 30)
        buf = self._history.setdefault(str(group_id), [])
        buf.append(f"{sender}: {text}"[:300])
        del buf[:-cap]

    # ---------- 闸门主逻辑 ----------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=300000)
    async def gate(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id or not self._enabled_group(group_id):
            return
        text = event.message_str or ""
        stripped = text.strip()
        if stripped.startswith(("/闸门", "闸门 ")):
            return
        sender = event.get_sender_id()
        if sender and str(sender) == self._self_id(event):
            return

        now = time.time()
        st = self._state(group_id)
        sender_name = None
        try:
            sender_name = event.get_sender_name() or None
        except Exception:
            sender_name = None
        sender_label = sender_name or str(sender or "?")

        if self._is_at_bot(event):
            if now < st.get("until", 0.0):
                # 窗口已开：不重置计时、不重新触发，只标记“以最后一条 @ 为准”
                st["reat_pending"] = True
                self._record(st, sender_label, text)
                self._record_history(group_id, sender_label, text)
                logger.info(f"[注意力闸门] 群 {group_id} 窗口期间再次被 @，计时保持不重置")
                return
            window = float(self._cfg("wake_window_seconds", 30) or 30)
            st["until"] = now + window
            st["opened_at"] = now
            st["transcript"] = []
            st["checking"] = False
            st["wake_pending"] = True
            self._record(st, sender_label, text)
            self._record_history(group_id, sender_label, text)
            self._schedule_exit(group_id)
            logger.info(f"[注意力闸门] 群 {group_id} 被 @，窗口开启 {window:.0f}s")
            return

        self._record_history(group_id, sender_label, text)

        if now < st.get("until", 0.0):
            self._record(st, sender_label, text)
            try:
                event.is_at_or_wake_command = True
            except Exception:
                pass
            return

        event.stop_event()

    # ---------- 到期定时器（主动自查） ----------

    def _schedule_exit(self, group_id: str) -> None:
        st = self._state(group_id)
        old = st.get("timer_task")
        if old and not old.done():
            old.cancel()
        st["timer_task"] = asyncio.create_task(self._timer_loop(str(group_id)))
        logger.debug(f"[注意力闸门] 群 {group_id} 到期定时器已启动")

    async def _timer_loop(self, group_id: str) -> None:
        try:
            st = self._state(group_id)
            remain = st.get("until", 0.0) - time.time()
            if remain > 0:
                await asyncio.sleep(remain)
            if time.time() < st.get("until", 0.0):
                return  # 期间被续期，交给新的定时器
            now = time.time()
            opened_at = st.get("opened_at", 0.0)
            max_total = float(self._cfg("max_window_seconds", 600) or 600)
            over_cap = bool(opened_at) and (now - opened_at) >= max_total
            last_act = float(st.get("last_activity", 0.0) or 0.0)
            gap = (now - last_act) if last_act else 999.0
            if over_cap:
                st["until"] = 0.0
                st["transcript"] = []
                logger.info(f"[注意力闸门] 群 {group_id} 窗口关闭（超总上限 {max_total:.0f}s），重新封闭")
                return
            quiet = float(self._cfg("quiet_close_seconds", 10) or 0)
            if quiet > 0 and gap >= quiet:
                st["until"] = 0.0
                st["transcript"] = []
                logger.info(
                    f"[注意力闸门] 群 {group_id} 窗口关闭（到期前 {gap:.0f}s 无人说话，跳过自查省 token），重新封闭"
                )
                return
            if self._cfg("exit_check_enabled", True) and st.get("transcript"):
                logger.info(f"[注意力闸门] 群 {group_id} 窗口到期（最后活动 {gap:.0f}s 前），开始自查")
                stay = await self._exit_check(group_id, st)
                if stay:
                    window = float(self._cfg("wake_window_seconds", 30) or 30)
                    st["until"] = time.time() + window
                    logger.info(f"[注意力闸门] 群 {group_id} 自查=继续，窗口续期 {window:.0f}s")
                    self._schedule_exit(group_id)
                    return
            st["until"] = 0.0
            st["transcript"] = []
            logger.info(f"[注意力闸门] 群 {group_id} 窗口关闭（自查=退出或禁用），重新封闭")
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[注意力闸门] 到期定时器异常")

    # ---------- 退出自查 ----------

    async def _exit_check(self, group_id: str, st: dict) -> bool:
        if st.get("checking"):
            return False
        st["checking"] = True
        try:
            provider = self.context.get_using_provider()
            if provider is None:
                return False
            transcript = "\n".join(st["transcript"][-int(self._cfg("transcript_max_messages", 30) or 30):])
            prompt = f"以下是注意力窗口期间的群聊记录（你的发言已标为【你】）：\n\n{transcript}\n\n窗口时间到了。继续参与还是退出？"
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(prompt=prompt, system_prompt=EXIT_SYSTEM_PROMPT), timeout=30
                )
            except asyncio.TimeoutError:
                logger.warning("[注意力闸门] 退出自查 LLM 超时（30s），默认退出")
                return False
            except Exception as exc:
                logger.warning(f"[注意力闸门] 退出自查 LLM 调用失败，默认退出: {type(exc).__name__} {exc}")
                return False
            answer = ""
            try:
                answer = (getattr(resp, "completion_text", None) or "").strip()
            except Exception:
                answer = str(resp)
            logger.info(f"[注意力闸门] 群 {group_id} 自查回答: {answer[:40]!r}")
            if "退出" in answer:
                return False
            return "继续" in answer
        finally:
            st["checking"] = False

    # ---------- 窗口纪律：静默标记 ----------

    @filter.on_llm_request()
    async def inject_window_discipline(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            group_id = event.get_group_id()
            if not group_id or not self._enabled_group(group_id):
                return
            st = self._windows.get(group_id)
            if not st or time.time() >= st.get("until", 0.0):
                return
            marker = str(self._cfg("silent_marker", "<silent/>") or "<silent/>")
            if st.pop("wake_pending", False):
                n = int(self._cfg("wake_history_count", 20) or 20)
                recent = (self._history.get(str(group_id)) or [])[-n:]
                hist_block = "\n".join(recent) if recent else "（暂无近期记录）"
                note = (
                    f"\n\n【群聊参与规则】你被 @ 唤醒，正在参与群聊。"
                    f"以下是本群最近 {len(recent)} 条聊天记录（发生在你被 @ 之前，此前你不可见这些消息，"
                    f"它们不是你的记忆，也不属于上下文历史）：\n\n{hist_block}\n\n"
                    f"只针对最后一条 @ 你的消息进行回复，历史消息仅作参考。"
                    f"如果你认为当前话题不需要你插嘴，或者对话已经自然结束，"
                    f"只输出静默标记 {marker}，不要输出其他任何内容。"
                )
            elif st.pop("reat_pending", False):
                note = (
                    f"\n\n【群聊参与规则】窗口期间你又收到一条新的 @。"
                    f"只针对最后一条 @ 你的消息进行回复，历史消息仅作参考。"
                    f"若这条 @ 没有实质内容或不需要你插嘴，只输出静默标记 {marker}。"
                )
            else:
                note = (
                    f"\n\n【注意力窗口纪律】你正在参与群聊。如果你认为当前话题不需要你插嘴，"
                    f"或者对话已经自然结束，请退出，不要输出其他任何内容——只输出静默标记 {marker}。"
                    f"只有当这条消息明确涉及你（提问你、@你、接续你正在参与的话题、你需要兑现的事）时才正常回复。"
                    f"宁可沉默，不要硬聊。"
                )
            req.system_prompt = (req.system_prompt or "") + note
        except Exception:
            pass

    @filter.on_decorating_result(priority=290000)
    async def swallow_silent(self, event: AstrMessageEvent, *args, **kwargs):
        try:
            group_id = event.get_group_id()
            if not group_id:
                return
            marker = str(self._cfg("silent_marker", "<silent/>") or "<silent/>")
            result = event.get_result()
            if result is None:
                return
            chain = list(getattr(result, "chain", []) or [])
            text = "".join(
                str(getattr(c, "text", "") or "") for c in chain if isinstance(c, Plain)
            )
            if marker in text or text.strip() == marker:
                result.chain = []
                event.stop_event()
                st = self._windows.get(group_id)
                if st and st.get("transcript"):
                    st["transcript"].append("【你】（选择沉默）")
                logger.debug(f"[注意力闸门] 群 {group_id} 静默回复已拦截")
        except Exception:
            logger.exception("[注意力闸门] 静默拦截异常")

    @filter.on_llm_response()
    async def record_own_reply(self, event: AstrMessageEvent, resp):
        try:
            group_id = event.get_group_id()
            if not group_id:
                return
            st = self._windows.get(group_id)
            if not st:
                return
            text = str(getattr(resp, "completion_text", "") or "")
            marker = str(self._cfg("silent_marker", "<silent/>") or "<silent/>")
            if text and marker not in text:
                self._record(st, "【你】", text)
        except Exception:
            pass

    # ---------- 管理指令 ----------

    @filter.command("闸门")
    async def gate_cmd(self, event: AstrMessageEvent):
        """注意力闸门管理：/闸门 status|open [秒]|close"""
        if not event.is_admin():
            return
        group_id = event.get_group_id()
        parts = (event.message_str or "").strip().split()
        sub = parts[1] if len(parts) > 1 else "status"
        st = self._windows.get(group_id or "", {"until": 0.0, "opened_at": 0.0, "transcript": []})
        now = time.time()
        if sub == "open":
            secs = float(parts[2]) if len(parts) > 2 else float(self._cfg("wake_window_seconds", 30) or 30)
            st["until"] = now + secs
            st["opened_at"] = now
            st["transcript"] = []
            st["wake_pending"] = False
            if group_id:
                self._schedule_exit(group_id)
            yield event.plain_result(f"闸门已手动开启 {secs:.0f} 秒")
        elif sub == "close":
            st["until"] = 0.0
            old = st.get("timer_task")
            if old and not old.done():
                old.cancel()
            yield event.plain_result("闸门已关闭，回到封闭状态")
        else:
            if now < st.get("until", 0.0):
                remain = st["until"] - now
                yield event.plain_result(
                    f"闸门状态：开启中（剩 {remain:.0f}s）｜窗口内消息 {len(st.get('transcript', []))} 条"
                )
            else:
                yield event.plain_result(
                    f"闸门状态：封闭｜窗口时长 {self._cfg('wake_window_seconds', 30)}s｜"
                    f"自查={'开' if self._cfg('exit_check_enabled', True) else '关'}｜"
                    f"总上限 {self._cfg('max_window_seconds', 600)}s"
                )
