import asyncio
import base64
from collections import OrderedDict
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api import logger
from astrbot.api.message_components import At, Reply, Image, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

# 导入分层模块
from .renderer import ProfileRenderer, HAS_RENDER_DEPS
from .fetcher import MessageFetcher

class PortrayalPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.texts_cache: OrderedDict[str, list[str]] = OrderedDict()
        self.MAX_CACHE_SIZE = 50 
        self.renderer = ProfileRenderer()

    # ================= 辅助方法 =================

    def _get_target_info(self, event: AiocqhttpMessageEvent):
        """解析目标用户ID"""
        for seg in event.get_messages():
            if isinstance(seg, At) and str(seg.qq) != event.get_self_id():
                return str(seg.qq)
        return event.get_sender_id()

    async def _get_user_nickname_gender(self, event: AiocqhttpMessageEvent, user_id: str):
        """获取昵称和性别"""
        try:
            info = await event.bot.get_group_member_info(
                group_id=int(event.get_group_id()), user_id=int(user_id)
            )
            return info.get("card") or info.get("nickname") or "群友", info.get("sex", "unknown")
        except Exception:
            return "群友", "unknown"

    def _force_find_provider(self, target_id: str):
        """查找指定ID的Provider"""
        if not target_id: return None
        target_id_lower = target_id.lower()
        all_providers = []
        if hasattr(self.context, "register"):
            reg_providers = getattr(self.context.register, "providers", None)
            if isinstance(reg_providers, dict): all_providers.extend(reg_providers.values())
            elif isinstance(reg_providers, list): all_providers.extend(reg_providers)
        if hasattr(self.context, "get_all_providers"):
            try: all_providers.extend(self.context.get_all_providers())
            except Exception: pass

        seen = set()
        for p in all_providers:
            if not p or id(p) in seen: continue
            seen.add(id(p))
            p_ids = []
            if hasattr(p, "id") and p.id: p_ids.append(str(p.id))
            if hasattr(p, "provider_id") and p.provider_id: p_ids.append(str(p.provider_id))
            if hasattr(p, "config") and isinstance(p.config, dict) and p.config.get("id"): 
                p_ids.append(str(p.config["id"]))
            if hasattr(p, "provider_config") and isinstance(p.provider_config, dict) and p.provider_config.get("id"): 
                p_ids.append(str(p.provider_config["id"]))

            for pid in p_ids:
                if pid.lower() == target_id_lower: return p
        return None

    # ================= 主指令逻辑 =================

    @filter.command("画像")
    async def generate_portrayal(self, event: AiocqhttpMessageEvent):
        """指令入口"""
        if not isinstance(event, AiocqhttpMessageEvent):
            yield event.plain_result("本插件依赖 OneBot 协议获取历史消息，当前适配器不支持")
            return

        # 1. 准备 LLM Provider
        provider = None
        cfg_provider_id = self.config.get("llm_provider_id")
        if cfg_provider_id: provider = self._force_find_provider(cfg_provider_id)
        if not provider:
            if hasattr(event, "unified_msg_origin"): provider = self.context.get_using_provider(event.unified_msg_origin)
            else: provider = self.context.get_using_provider()
            
        if not provider:
            yield event.plain_result("未找到可用的 LLM 服务")
            return

        # 2. 解析参数
        target_id = self._get_target_info(event)
        nickname, gender = await self._get_user_nickname_gender(event, target_id)
        
        args = event.message_str.split()
        custom_rounds = None
        force_refresh = False
        for arg in args:
            if arg.isdigit(): custom_rounds = int(arg)
            if "更新" in arg or "刷新" in arg: force_refresh = True
            
        max_rounds = custom_rounds if custom_rounds else self.config.get("max_query_rounds", 20)
        max_rounds = min(100, max(1, max_rounds))

        # 3. 获取历史消息
        texts = []
        completion_text = ""

        if not force_refresh and target_id in self.texts_cache:
            texts = self.texts_cache[target_id]
            self.texts_cache.move_to_end(target_id)
            completion_text = f"从缓存加载：找到 {len(texts)} 条有效发言"
        else:
            yield event.plain_result(f"正在回溯 (深度: {max_rounds}轮)...")
            
            fetcher = MessageFetcher(event.bot)
            batch_size = self.config.get("batch_size", 100)
            texts, rounds_done = await fetcher.fetch_history(
                int(event.get_group_id()), target_id, max_rounds, batch_size
            )
            
            if texts:
                self.texts_cache[target_id] = texts
                self.texts_cache.move_to_end(target_id)
                if len(self.texts_cache) > self.MAX_CACHE_SIZE:
                    self.texts_cache.popitem(last=False)
                completion_text = f"回溯结束：找到 {len(texts)} 条有效发言"

        if not texts or len(texts) < 3:
            yield event.plain_result(f"发言太少（仅 {len(texts)} 条），无法生成准确画像")
            return

        # 4. 调用 LLM 生成画像
        gender_cn = "他" if gender == "male" else ("她" if gender == "female" else "TA")
        system_prompt = self.config.get("system_prompt_template", "").format(
            nickname=nickname, gender=gender_cn
        )
        
        history_str = "\n".join([f"[{i+1}] {t}" for i, t in enumerate(texts)])
        final_prompt = (
            f"以下是目标用户 {nickname} 的最近聊天记录（共 {len(texts)} 条）：\n\n"
            f"{history_str}\n\n"
            f"请根据 System Prompt 的要求，对该用户进行深度性格侧写与分析。"
        )
        
        try:
            response = await provider.text_chat(
                prompt=final_prompt,
                system_prompt=system_prompt,
                contexts=[]
            )
            
            if not response or not response.completion_text:
                raise ValueError("LLM 返回内容为空")

            result_text = response.completion_text
            enable_image = self.config.get("enable_image_output", False)
            sent_success = False
            
            # 5. 渲染并发送
            if enable_image:
                if HAS_RENDER_DEPS:
                    try:
                        img_bytes = await self.renderer.render(result_text, nickname)
                        
                        # [稳健获取消息 ID]
                        msg_id = None
                        try:
                            # 优先尝试 raw_data (最底层数据，最准确)
                            if hasattr(event, "raw_data") and isinstance(event.raw_data, dict):
                                msg_id = event.raw_data.get("message_id")
                            
                            # 备选尝试 message_obj
                            if not msg_id and hasattr(event, "message_obj") and hasattr(event.message_obj, "message_id"):
                                msg_id = event.message_obj.message_id
                            
                            if msg_id: msg_id = int(msg_id)
                        except Exception as e:
                            logger.warning(f"Portrayal: 获取消息ID异常: {e}")
                            msg_id = None
                        
                        # [直接构建底层 API 数据包]
                        b64_img = base64.b64encode(img_bytes).decode()
                        payload_msg = []
                        
                        # 1. 引用回复
                        if msg_id:
                            payload_msg.append({
                                "type": "reply",
                                "data": {"id": str(msg_id)}
                            })
                        
                        # 2. 图片 (无任何文本节点)
                        payload_msg.append({
                            "type": "image",
                            "data": {"file": f"base64://{b64_img}"}
                        })
                        
                        # [调用 API 发送]
                        group_id = event.get_group_id()
                        if group_id:
                            await event.bot.api.call_action(
                                "send_group_msg",
                                group_id=int(group_id),
                                message=payload_msg
                            )
                        else:
                            await event.bot.api.call_action(
                                "send_private_msg",
                                user_id=int(event.get_sender_id()),
                                message=payload_msg
                            )
                            
                        sent_success = True
                        
                    except Exception as e:
                        logger.error(f"Portrayal: 渲染或发送失败: {e}")
                else:
                    logger.warning("Portrayal: 开启了图片输出但缺少依赖，请安装 markdown 和 playwright")

            if not sent_success:
                final_msg = f"{completion_text}\n\n{result_text}" if completion_text else result_text
                yield event.plain_result(final_msg)

        except Exception as e:
            err_str = str(e)
            if "completion 无法解析" in err_str or "content=None" in err_str:
                logger.error(f"Portrayal: LLM 拒绝生成。Err: {err_str}")
                yield event.plain_result("生成失败：LLM 拒绝生成内容（可能是聊天记录包含敏感内容触发了模型的安全过滤）。")
            else:
                logger.error(f"画像生成失败: {e}")
                yield event.plain_result(f"分析过程中发生错误: {e}")
