import asyncio
import time
import re
from astrbot.api import logger

class MessageFetcher:
    def __init__(self, bot_client):
        self.client = bot_client

    async def fetch_history(self, group_id: int, target_user_id: str, max_duration: int):
        """
        基于时间限制的纯净回溯
        """
        collected_texts = []
        message_seq = 0
        BATCH_SIZE = 200 
        
        start_time = time.time()
        
        # [修改] 移除 Info 日志，保持控制台清爽
        # logger.info(f"Portrayal: 开始纯净抓取 (限时: {max_duration}s)...")

        while True:
            elapsed = time.time() - start_time
            if elapsed >= max_duration:
                # [修改] 只有在超时且什么都没抓到时才打印 Warning，正常结束不打印
                if not collected_texts:
                    logger.debug(f"Portrayal: 时间耗尽 ({elapsed:.1f}s)，停止回溯。")
                break

            payloads = {
                "group_id": int(group_id),
                "count": BATCH_SIZE,
                "reverseOrder": True,
            }
            if message_seq > 0:
                payloads["message_seq"] = int(message_seq)

            try:
                result = await self.client.api.call_action("get_group_msg_history", **payloads)
                
                if not result or not isinstance(result, dict):
                    break
                    
                round_messages = result.get("messages", [])
                if not round_messages:
                    break

                message_seq = round_messages[0]["message_id"]

                for msg in round_messages:
                    if str(msg["sender"]["user_id"]) != target_user_id:
                        continue
                    
                    text = self._parse_content_clean(msg.get("message", []))
                    if text:
                        collected_texts.append(text)

                await asyncio.sleep(0.05)

            except Exception as e:
                # 只在 Debug 模式记录小故障
                logger.debug(f"Portrayal Fetch Error: {e}")
                await asyncio.sleep(0.5)
                continue

        return collected_texts[::-1], (time.time() - start_time)

    def _parse_content_clean(self, msg_content):
        """
        纯净解析
        """
        if isinstance(msg_content, str): 
            if re.search(r"http[s]?://", msg_content, re.I):
                return None
            return msg_content.strip()
        
        text_parts = []
        
        for seg in msg_content:
            type_ = seg.get("type")
            data = seg.get("data", {})
            
            if type_ in ["json", "xml", "miniprogram", "share", "file"]:
                return None
            
            if type_ == "text":
                t = data.get("text", "")
                if re.search(r"http[s]?://", t, re.I):
                    return None
                text_parts.append(t)
            
            elif type_ in ["image", "face", "mface", "record", "video", "marketface"]:
                continue
                
            else:
                continue
            
        final_text = "".join(text_parts).strip()
        return final_text if final_text else None