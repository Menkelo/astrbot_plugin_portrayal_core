import re
from datetime import datetime
from astrbot.api import logger

# 尝试导入依赖
try:
    import markdown
    from playwright.async_api import async_playwright
    HAS_RENDER_DEPS = True
except ImportError:
    HAS_RENDER_DEPS = False

class ProfileRenderer:
    def __init__(self):
        if not HAS_RENDER_DEPS:
            logger.warning("Portrayal: 缺少 markdown 或 playwright 依赖，渲染功能不可用。")

    async def render(self, markdown_text: str, nickname: str) -> bytes:
        """
        将 Markdown 文本渲染为 [直角白玻璃风格] 的图片
        [修改] 卡片改为直角 (border-radius: 0)
        [修改] 时间格式调整为 28 Dec 2025 16:17
        [修改] 移除右上角名字的胶囊叠级背景，改为纯文本
        """
        if not HAS_RENDER_DEPS:
            raise ImportError("Missing dependencies: markdown, playwright")

        # ================= 1. 正则预处理 =================
        
        # A. 处理昵称 -> 蓝色 (user-token)
        if nickname and nickname.strip():
            safe_nick = re.escape(nickname)
            markdown_text = re.sub(
                f"(?i){safe_nick}", 
                f'<span class="user-token">{nickname}</span>', 
                markdown_text
            )

        # B. 处理标签/短语 (Tag) -> 绿色 (tag-token)
        markdown_text = re.sub(
            r'【\**([^\*]+?)\**】', 
            r'【<span class="tag-token">\1</span>】', 
            markdown_text
        )

        # ================= 2. 转换与资源准备 =================

        html_body = markdown.markdown(
            markdown_text, 
            extensions=['tables', 'fenced_code', 'nl2br', 'sane_lists']
        )

        # [修改] 时间格式: 28 Dec 2025 16:17
        time_str = datetime.now().strftime("%d %b %Y %H:%M")
        
        hljs_css = "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css"
        hljs_js = "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"

        # ================= 3. CSS 样式 =================
        css = """
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Noto+Sans+SC:wght@400;500;700&family=JetBrains+Mono:wght@400&display=swap');
        
        body {
            margin: 0; padding: 0;
            background-color: #f5f5f7;
            /* 柔和的淡彩光斑 */
            background-image: 
                radial-gradient(circle at 10% 20%, rgba(219, 234, 254, 1) 0%, transparent 40%),
                radial-gradient(circle at 90% 80%, rgba(254, 226, 226, 1) 0%, transparent 40%);
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", "Noto Sans SC", sans-serif;
            color: #1d1d1f;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh;
        }

        .container {
            width: 720px;
            padding: 50px; /* 稍微增加内边距 */
            margin: 0;     /* 截图时不需要外边距 */
            
            /* --- 白玻璃拟态 --- */
            background: rgba(255, 255, 255, 0.75);
            backdrop-filter: blur(60px) saturate(180%);
            -webkit-backdrop-filter: blur(60px) saturate(180%);
            
            /* [修改] 直角设计 */
            border-radius: 0; 
            
            border: 1px solid rgba(255, 255, 255, 0.8);
            box-shadow: 0 20px 60px -10px rgba(0, 0, 0, 0.08);
        }

        /* --- Header --- */
        .header {
            display: flex; justify-content: space-between; align-items: flex-start;
            margin-bottom: 36px;
            padding-bottom: 24px;
            border-bottom: 1px solid rgba(0, 0, 0, 0.06);
        }
        
        .title-block { display: flex; flex-direction: column; }
        .title { 
            font-size: 28px; font-weight: 800; 
            background: linear-gradient(135deg, #1a1a1a 0%, #4a4a4a 100%);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            letter-spacing: -0.5px;
        }
        .subtitle { font-size: 14px; color: rgba(0, 0, 0, 0.45); margin-top: 4px; font-weight: 500; }
        
        /* 右上角信息块 */
        .meta-block { display: flex; flex-direction: column; align-items: flex-end; }
        
        /* [修改] 纯文本名字，移除胶囊背景 */
        .meta-info { 
            font-size: 14px; color: rgba(0, 0, 0, 0.5); 
            font-weight: 500; 
            margin-bottom: 4px; /* 名字和时间的间距 */
            display: flex; align-items: center; gap: 6px;
        }
        
        /* [修改] 时间样式 */
        .time-label {
            font-family: 'Inter', sans-serif; /* 使用非衬线字体，更接近参考图 */
            font-size: 14px; 
            color: rgba(0, 0, 0, 0.4);
            letter-spacing: 0px;
        }

        /* --- Content --- */
        h1, h2, h3 { margin-top: 28px; margin-bottom: 16px; font-weight: 700; letter-spacing: -0.3px; color: #111; }
        h1 { font-size: 22px; display: flex; align-items: center; }
        h1::before {
            content: ''; display: inline-block; width: 4px; height: 20px;
            background: #8b5cf6; margin-right: 12px; border-radius: 0; /* 直角条 */
        }
        h2 { font-size: 19px; color: #334155; border-left: 3px solid #cbd5e1; padding-left: 10px; }
        h3 { font-size: 17px; color: #64748b; }
        
        p { 
            line-height: 2.0; font-size: 15px; 
            color: #374151; 
            margin-bottom: 20px; text-align: justify; 
        }

        /* --- Colors --- */

        /* 1. 蓝色：用户昵称 */
        .user-token { color: #2563eb; font-weight: 700; }
        /* 右上角名字颜色 */
        .meta-info .user-token { color: #3b82f6; font-size: 15px; }

        /* 2. 绿色：标签 */
        .tag-token {
            background: rgba(16, 185, 129, 0.12); color: #059669;
            padding: 0 4px; border-radius: 0; /* 直角 */
            font-weight: 600;
            border: 1px solid rgba(16, 185, 129, 0.2);
            box-decoration-break: clone; -webkit-box-decoration-break: clone;
        }

        /* 3. 金色：重点 */
        strong { 
            background: rgba(245, 158, 11, 0.12); color: #b45309;                       
            padding: 0 4px; border-radius: 0; /* 直角 */
            font-weight: 600; margin: 0 1px;
            box-decoration-break: clone; -webkit-box-decoration-break: clone;
        }

        /* 防止嵌套冲突 */
        strong:has(.user-token), strong:has(.tag-token) {
            background: transparent !important; padding: 0 !important; color: inherit !important;
        }

        /* --- Components --- */
        ul, ol { padding-left: 24px; color: #4b5563; }
        li { margin-bottom: 8px; }

        blockquote {
            background: rgba(243, 244, 246, 0.6);
            border-left: 3px solid #f43f5e;
            margin: 20px 0; padding: 14px 20px;
            color: #4b5563; font-style: italic;
            border-radius: 0; /* 直角 */
            border-top: 1px solid rgba(0,0,0,0.02);
            border-bottom: 1px solid rgba(0,0,0,0.02);
            border-right: 1px solid rgba(0,0,0,0.02);
        }

        p code, li code {
            background: #f1f5f9;
            border: 1px solid #e2e8f0;
            padding: 2px 6px; border-radius: 0; /* 直角 */
            font-family: 'JetBrains Mono', monospace;
            color: #ef4444; font-size: 0.9em;
        }
        
        pre {
            background: #f8fafc !important;
            border: 1px solid #e2e8f0;
            border-radius: 0; /* 直角 */
            padding: 16px; margin: 20px 0;
            overflow-x: hidden;
        }
        pre code { font-family: 'JetBrains Mono', monospace; font-size: 13px; background: transparent !important; }
        """

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <link rel="stylesheet" href="{hljs_css}">
            <script src="{hljs_js}"></script>
            <style>{css}</style>
        </head>
        <body>
            <div class="container" id="card">
                <div class="header">
                    <div class="title-block">
                        <div class="title">Personality Profile</div>
                        <div class="subtitle">AI-Powered Behavioral Analysis</div>
                    </div>
                    <div class="meta-block">
                        <div class="meta-info">
                            TARGET: <span class="user-token">{nickname}</span>
                        </div>
                        <div class="time-label">{time_str}</div>
                    </div>
                </div>
                <div class="content">{html_body}</div>
            </div>
            <script>hljs.highlightAll();</script>
        </body>
        </html>
        """

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page(device_scale_factor=2)
                await page.set_content(html_content)
                card = await page.query_selector("#card")
                if not card: raise Exception("Element #card not found")
                return await card.screenshot(type="png", omit_background=True)
            finally:
                await browser.close()
