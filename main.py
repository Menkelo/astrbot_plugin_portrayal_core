import asyncio
import base64
import unicodedata
import re
from collections import OrderedDict

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

        # 1. 获取触发ID
        trigger_id = None
        try:
            if hasattr(event, "message_obj") and hasattr(event.message_obj, "message_id"):
                trigger_id = str(event.message_obj.message_id)
            if not trigger_id and hasattr(event, "raw_data") and isinstance(event.raw_data, dict):
                trigger_id = str(event.raw_data.get("message_id"))
        except:
            pass

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
        sys_prompt = self.config.get("system_prompt_template", "你是一位侧写师。").format(
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
            try:
                img_bytes = await self.renderer.render(result_text, nickname, str(target_id))
                b64_img = base64.b64encode(img_bytes).decode()

                payload = []
                if trigger_id:
                    payload.append({"type": "reply", "data": {"id": str(trigger_id)}})
                payload.append({"type": "image", "data": {"file": f"base64://{b64_img}"}})

                await event.bot.api.call_action(
                    "send_group_msg",
                    group_id=int(group_id),
                    message=payload
                )
            except Exception as e:
                logger.error(f"Render Error: {e}")
                yield event.plain_result(result_text)
        else:
            yield event.plain_result(result_text)
