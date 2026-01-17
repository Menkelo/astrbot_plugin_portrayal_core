import asyncio
from astrbot.api import logger

class MessageFetcher:
    def __init__(self, bot_client):
        self.client = bot_client

    async def fetch_history(self, group_id: int, target_user_id: str, max_rounds: int, batch_size: int = 100):
        """
        深度优先抓取指定用户的历史消息
        """
        collected_texts = []
        cursor_seq = 0
        error_strike = 0
        real_rounds = 0
        
        while real_rounds < max_rounds:
            batch, next_cursor, success, new_strike = await self._fetch_next_batch_robust(
                group_id, cursor_seq, error_strike, batch_size
            )
            error_strike = new_strike
            
            if not success:
                if next_cursor <= 0: break
                cursor_seq = next_cursor
                await asyncio.sleep(0.1)
                continue
            
            if not batch: break
                
            for msg in reversed(batch): 
                # 筛选目标用户
                if str(msg["sender"]["user_id"]) != target_user_id: continue
                try:
                    # 提取纯文本
                    msg_content = msg.get("message", [])
                    text = ""
                    if isinstance(msg_content, str): 
                        text = msg_content
                    else: 
                        text = "".join([s["data"]["text"] for s in msg_content if s.get("type") == "text"])
                    
                    if text.strip(): 
                        collected_texts.append(text.strip())
                except: continue

            cursor_seq = next_cursor
            real_rounds += 1
            await asyncio.sleep(0.2) 

        return collected_texts[::-1], real_rounds

    async def _fetch_next_batch_robust(self, group_id, cursor_seq, current_strike, batch_size):
        """[底层] 获取单批次消息 (带熔断与跳跃机制)"""
        MAX_RETRY_STRIKE = 15 
        if current_strike > MAX_RETRY_STRIKE:
            logger.error(f"Portrayal: 连续失败次数过多 ({current_strike}次)，触发熔断。")
            return [], 0, False, current_strike

        try:
            payload = {
                "group_id": int(group_id),
                "count": batch_size,
                "reverseOrder": True
            }
            if cursor_seq > 0:
                payload["message_seq"] = cursor_seq

            res = await self.client.api.call_action("get_group_msg_history", **payload)
            
            if not res or not isinstance(res, dict): 
                return [], 0, False, current_strike
            
            batch = res.get("messages", [])
            if not batch: 
                return [], 0, True, 0 
            
            oldest_msg = batch[0]
            next_cursor = int(oldest_msg.get("message_seq") or oldest_msg.get("message_id") or 0)
            return batch, next_cursor, True, 0

        except Exception as e:
            err_msg = str(e)
            # 处理 1200 错误或消息不存在的情况
            if "1200" in err_msg or "不存在" in err_msg:
                new_strike = current_strike + 1
                base_jump = max(50, batch_size) 
                jump_step = base_jump * (2 ** (min(new_strike, 8) - 1))
                if new_strike <= 5 or new_strike % 5 == 0:
                    logger.warning(f"Portrayal: 游标 {cursor_seq} 断层，跳跃 {jump_step} 条...")
                new_cursor = cursor_seq - jump_step
                return [], new_cursor, False, new_strike
            else:
                logger.warning(f"Portrayal: API请求中断: {e}")
                return [], 0, False, current_strike
