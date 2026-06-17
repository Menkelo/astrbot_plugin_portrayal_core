import asyncio
import base64
import os
import re
import tempfile
import unicodedata
import uuid
from collections import OrderedDict
from pathlib import Path

from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api import logger
from astrbot.api.message_components import At
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

from .renderer import ProfileRenderer
from .fetcher import MessageFetcher
from .llm_client import LLMClient


class _ConfigAdapter:
    """适配 LLMClient 所需配置接口"""
    def __init__(self, config: AstrBotConfig):
        self.config = config

    def get_llm_provider_id(self):
        return self.config.get("llm_provider_id")

    # 兼容 get_xxx_provider_id 的调用
    def __getattr__(self, name):
        if name.startswith("get_") and name.endswith("_provider_id"):
            return lambda: None
        raise AttributeError(name)


class PortrayalPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.texts_cache = OrderedDict()
        self.MAX_CACHE_SIZE = 50
        self.renderer = ProfileRenderer()
        self.config_adapter = _ConfigAdapter(config)

    def _is_valid_nickname(self, name: str) -> bool:
        if not name: return False
        if not name.strip(): return False
        for char in name:
            if char in ('\u3164', '\u2800', '\u115f', '\u1160', '\uffa0'):
                continue
            cat = unicodedata.category(char)
            if cat.startswith('C') or cat.startswith('Z'):
                continue
            return True
        return False

    @filter.command("画像")
    async def generate_portrayal(self, event: AiocqhttpMessageEvent):
        if not isinstance(event, AiocqhttpMessageEvent): return

        # 1. 获取触发消息ID（用于引用回复原指令）
        #    适配器固定执行 abm.message_id = str(event.message_id)，
        #    故优先取 message_obj.message_id；并过滤 None/0/空 等无效值，
        #    否则会带着无效 reply.id 发出，被协议端静默丢弃（图能发但不引用）。
        def _valid_id(v):
            if v is None:
                return None
            s = str(v).strip()
            if s in ("", "0", "None", "none", "null", "-1"):
                return None
            return s

        trigger_id = None
        try:
            mo = getattr(event, "message_obj", None)
            trigger_id = _valid_id(getattr(mo, "message_id", None)) if mo is not None else None
            if not trigger_id and mo is not None:
                raw = getattr(mo, "raw_message", None)
                # raw_message 是 aiocqhttp Event（类 dict），用 .get 兜底
                if raw is not None:
                    try:
                        trigger_id = _valid_id(raw.get("message_id"))
                    except Exception:
                        trigger_id = _valid_id(getattr(raw, "message_id", None))
            if not trigger_id:
                raw = getattr(event, "raw_data", None)
                if isinstance(raw, dict):
                    trigger_id = _valid_id(raw.get("message_id"))
        except Exception as e:
            logger.warning(f"Portrayal: 获取触发消息ID异常: {e}")

        logger.info(f"Portrayal: trigger_id={trigger_id!r}")
        if not trigger_id:
            logger.warning("Portrayal: 未获取到有效触发消息ID，本次将不带引用发送")

        group_id = event.get_group_id()
        sender_id = event.get_sender_id()

        # 2. 确定目标用户
        target_id = sender_id
        for seg in event.get_messages():
            if isinstance(seg, At) and str(seg.qq) != event.get_self_id():
                target_id = str(seg.qq)
                break

        # 3. 获取昵称
        nickname = str(target_id)
        gender = "unknown"
        try:
            info = await event.bot.get_group_member_info(
                group_id=int(group_id), user_id=int(target_id)
            )
            raw_name = info.get("card") or info.get("nickname") or ""
            if self._is_valid_nickname(raw_name):
                nickname = raw_name
            else:
                nickname = str(target_id)
            gender = info.get("sex", "unknown")
        except:
            pass

        # 4. 抓取逻辑
        args = event.message_str.split()
        duration = self.config.get("max_fetch_duration", 20)
        force_refresh = False
        for arg in args:
            if arg.isdigit(): duration = int(arg)
            if "更新" in arg or "刷新" in arg: force_refresh = True
        duration = min(300, max(5, duration))

        texts = []
        cache_key = f"{group_id}_{target_id}"

        if not force_refresh and cache_key in self.texts_cache:
            texts = self.texts_cache[cache_key]
            self.texts_cache.move_to_end(cache_key)
        else:
            yield event.plain_result(f"⏳ 全速回溯 {nickname} ({duration}s)...")
            fetcher = MessageFetcher(event.bot)
            texts, _ = await fetcher.fetch_history(int(group_id), str(target_id), duration)
            if texts:
                self.texts_cache[cache_key] = texts
                self.texts_cache.move_to_end(cache_key)
                if len(self.texts_cache) > self.MAX_CACHE_SIZE:
                    self.texts_cache.popitem(last=False)
            else:
                yield event.plain_result(f"⚠️ 未找到 {nickname} 的发言。")
                return

        if len(texts) < 3:
            yield event.plain_result(f"⚠️ 发言过少 ({len(texts)}条)。")
            return

        # 5. 上下文截断
        history_str = "\n".join(texts)
        MAX_CHARS = 12000
        if len(history_str) > MAX_CHARS:
            history_str = history_str[-MAX_CHARS:]
            idx = history_str.find('\n')
            if idx != -1:
                history_str = history_str[idx+1:]

        # 6. 生成 Prompt
        gender_cn = "他" if gender == "male" else ("她" if gender == "female" else "TA")
        default_sys_prompt = (
            "你是一位侧写师，请仅凭群聊记录为群友【{nickname}】做一份性格侧写，用「{gender}」指代本人。\n"
            "输出格式要求：\n"
            "- 每个板块标题统一用 `##` 开头，序号会自动生成，标题里不要手动写「一、」「1.」之类的编号。\n"
            "- 关键标签用「【】」包裹，核心结论用 **加粗**。\n"
            "- 直接输出报告正文，不要写思考过程、开场白或结束语。"
        )
        sys_prompt = self.config.get("system_prompt_template", default_sys_prompt).format(
            nickname=nickname, gender=gender_cn
        )
        user_prompt = (
            f"用户【{nickname}】的历史发言 (精选 {len(history_str)} 字符):\n\n"
            f"--- 记录开始 ---\n{history_str}\n--- 记录结束 ---\n\n请进行画像分析。"
        )
        final_prompt = f"{sys_prompt}\n\n{user_prompt}"

        # 7. 选择 Provider ID
        provider_id = await LLMClient.get_provider_id_with_fallback(
            context=self.context,
            config_manager=self.config_adapter,
            provider_id_key=None,
            umo=event.unified_msg_origin
        )

        if not provider_id:
            yield event.plain_result("❌ 未找到可用的 LLM 服务。")
            return

        # 8. 调用 LLM (增加重试机制)
        MAX_RETRY = 3
        resp = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=final_prompt,
                    max_tokens=1024,
                    temperature=0.7
                )
            except Exception as e:
                logger.error(f"Portrayal LLM Error (attempt {attempt}/{MAX_RETRY}): {e}")
                await asyncio.sleep(0.6 * attempt)
                continue

            if resp and getattr(resp, "completion_text", ""):
                break
            else:
                logger.warning(f"Portrayal LLM empty output (attempt {attempt}/{MAX_RETRY})")
                await asyncio.sleep(0.6 * attempt)

        if not resp or not getattr(resp, "completion_text", ""):
            yield event.plain_result("❌ 模型多次未返回有效内容，请稍后再试。")
            return

        result_text = resp.completion_text

        # 9. 输出
        if self.config.get("enable_image_output", True):
            tmp_path = None
            try:
                img_bytes = await self.renderer.render(result_text, nickname, str(target_id))

                # 图片源：AstrBot 与 NapCat 同机时优先用本地文件路径(file://)。
                # NapCat 对“引用段 + base64 大图”的关联存在偶发丢失（图能发但不引用），
                # 改用文件路径后引用稳定。写文件失败时回退 base64。
                image_file = None
                try:
                    tmp_path = os.path.join(
                        tempfile.gettempdir(), f"portrayal_{uuid.uuid4().hex}.png"
                    )
                    with open(tmp_path, "wb") as f:
                        f.write(img_bytes)
                    image_file = Path(tmp_path).as_uri()  # file:///...
                except Exception as e:
                    logger.warning(f"Portrayal: 写临时图片失败，回退 base64: {e}")
                    tmp_path = None
                    image_file = "base64://" + base64.b64encode(img_bytes).decode()

                payload = []
                if trigger_id:
                    payload.append({"type": "reply", "data": {"id": str(trigger_id)}})
                payload.append({"type": "image", "data": {"file": image_file}})

                # 多账号(self_id)路由：与 SDK 适配器一致，避免发到错误的连接
                routing = {}
                self_id = getattr(getattr(event, "message_obj", None), "self_id", None)
                if self_id:
                    routing["self_id"] = self_id

                logger.info(
                    f"Portrayal: send img with_reply={bool(trigger_id)} "
                    f"trigger_id={trigger_id!r} group={group_id} self_id={self_id!r} "
                    f"img_src={'file' if tmp_path else 'base64'}"
                )

                if group_id:
                    ret = await event.bot.api.call_action(
                        "send_group_msg", group_id=int(group_id), message=payload, **routing
                    )
                else:
                    ret = await event.bot.api.call_action(
                        "send_private_msg", user_id=int(sender_id), message=payload, **routing
                    )
                logger.info(f"Portrayal: send result={ret}")
            except Exception as e:
                logger.error(f"Render Error: {e}")
                yield event.plain_result(result_text)
            finally:
                # call_action 返回时 NapCat 已读取图片，可安全清理临时文件
                if tmp_path:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
        else:
            yield event.plain_result(result_text)
