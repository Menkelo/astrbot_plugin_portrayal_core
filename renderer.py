import re
import base64
import html
import hashlib

# 渲染依赖探测：缺失时插件仍可加载（降级为纯文本输出）
try:
    import markdown
    import aiohttp
    from playwright.async_api import async_playwright
    HAS_RENDER_DEPS = True
except ImportError:
    markdown = None
    aiohttp = None
    async_playwright = None
    HAS_RENDER_DEPS = False


class ProfileRenderer:
    DEFAULT_AVATAR = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="

    async def _fetch_avatar_b64(self, qq: str) -> str:
        """
        [移植版] 强力头像抓取逻辑 (QuoteCore同款 - 最稳策略)
        """
        if not qq or not str(qq).isdigit():
            return self.DEFAULT_AVATAR

        # 强制使用 s=100 (高清图接口经常 403，s=100 最稳定)
        urls = [
            f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=100",
            f"https://q2.qlogo.cn/headimg_dl?dst_uin={qq}&spec=100",
            f"https://thirdqq.qlogo.cn/g?b=qq&nk={qq}&s=100",
        ]

        async with aiohttp.ClientSession() as session:
            for url in urls:
                try:
                    async with session.get(url, timeout=2.5) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            if len(data) > 500:
                                b64 = base64.b64encode(data).decode()
                                return f"data:image/jpg;base64,{b64}"
                except Exception:
                    continue

        return self.DEFAULT_AVATAR

    def _avatar_html(self, avatar_b64: str, nickname: str) -> str:
        """有真实头像则用图片，否则用昵称首字母的纯色字母头像兜底"""
        if avatar_b64 and avatar_b64 != self.DEFAULT_AVATAR:
            return f'<img class="avatar" src="{avatar_b64}">'

        initial = html.escape(nickname.strip()[:1]) if nickname and nickname.strip() else "?"
        hue = int(hashlib.md5((nickname or "?").encode()).hexdigest(), 16) % 360
        return (
            f'<div class="avatar avatar-mono" '
            f'style="background:hsl({hue},42%,46%)">{initial}</div>'
        )

    def _normalize_md(self, text: str) -> str:
        """规整模型输出的标题，保证自动编号(01/02)稳定生效：
        1) 兼容任意级别(#~######)、可被 ** 加粗包裹、可带行首缩进的标题
        2) 兼容“整行纯加粗”当小节标题的写法(无 # 时也能识别)
        3) 去除标题中自带的人工编号(如 一、/ 1. / （1）)，避免与 CSS 自动编号叠加
        4) 统一抽取为纯净的二级标题 `## 标题`，前后补空行使其独立成块
        代码围栏 ``` 内的内容原样保留。
        """
        # 带 # 的标题
        HEAD = re.compile(r"^\s*\*{0,3}\s*#{1,6}\s+(.+?)\s*\**\s*$")
        # 整行被 ** 包裹、且内部不含其它 ** 的，视为小节标题
        BOLD_HEAD = re.compile(r"^\s*\*\*([^*]+?)\*\*\s*$")
        # 标题前的人工编号：1. / 1、/ 1) / （1）/ (1) / 一、/ 01. 等
        LEAD_NUM = re.compile(
            r"^\s*[(（]?\s*(?:\d{1,3}|[一二三四五六七八九十]+)\s*[)）.、:：]\s*"
        )
        out, in_fence = [], False
        for line in text.split("\n"):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                out.append(line)
                continue

            title = None
            if not in_fence:
                m = HEAD.match(line)
                if m:
                    title = m.group(1)
                else:
                    mb = BOLD_HEAD.match(line)
                    if mb:
                        title = mb.group(1)

            if title is not None:
                title = title.strip().strip("*").strip().rstrip("#").strip()
                title = LEAD_NUM.sub("", title).strip()
                if not title:          # 去编号后为空则按普通行处理
                    out.append(line)
                    continue
                if out and out[-1].strip() != "":
                    out.append("")
                out.append("## " + title)
                out.append("")
                continue

            out.append(line)
        return "\n".join(out)

    async def render(self, markdown_text: str, nickname: str, user_id: str = None) -> bytes:
        """
        极简杂志风渲染 (森绿 / Forest Green)
        user_id 可选：提供则抓取 QQ 头像，否则使用昵称首字母头像兜底
        """
        # 1. 头像
        avatar_b64 = self.DEFAULT_AVATAR
        if user_id:
            avatar_b64 = await self._fetch_avatar_b64(str(user_id))
        avatar_html = self._avatar_html(avatar_b64, nickname)

        # 1.5 规整 Markdown（去标题缩进）
        markdown_text = self._normalize_md(markdown_text)

        # 2. Token 注入：昵称加粗 + 【标签】转药丸
        if nickname and nickname.strip():
            safe_nick_re = re.escape(nickname)
            markdown_text = re.sub(
                f"(?i){safe_nick_re}",
                f'**{nickname}**',
                markdown_text
            )

        markdown_text = re.sub(
            r'【\**([^\*]+?)\**】',
            r'<span class="topic-pill">\1</span>',
            markdown_text
        )

        # 3. Markdown 转 HTML
        html_body = markdown.markdown(
            markdown_text,
            extensions=['tables', 'nl2br', 'sane_lists']
        )

        # 4. 页面数据
        safe_nick = html.escape(nickname) if nickname else "群友"

        # 5. CSS 样式 (森绿封面大刊)
        css = """
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=Noto+Serif+SC:wght@400;500;700;900&display=swap');

        :root {
            --bar: #1c4634;
            --accent: #2f7d5b;
            --soft: #e6f1ea;
            --line: #bfe0cd;
            --quote-bg: #f1f7f3;
        }

        body {
            margin: 0; padding: 0;
            background: transparent;
            font-family: "Noto Serif SC", Georgia, "Songti SC", serif;
            -webkit-font-smoothing: antialiased;
        }

        .container {
            width: 1200px;
            background: #ffffff;
            position: relative;
            overflow: hidden;
            color: #161616;
            box-shadow: 0 0 2px rgba(0,0,0,0.1);
            padding-bottom: 4px;
        }

        /* === 刊头条 === */
        .masthead {
            background: var(--bar);
            color: #ffffff;
            font-family: "Inter", -apple-system, sans-serif;
            display: flex; align-items: center;
            padding: 28px 70px;
            font-size: 24px; letter-spacing: 3px;
        }
        .masthead .brand { font-weight: 800; }

        /* === 头部 === */
        .head {
            padding: 72px 80px 0;
            position: relative;
            min-height: 300px;
        }
        .avatar {
            position: absolute;
            width: 150px; height: 150px;
            top: 88px; right: 80px;
            object-fit: cover;
            border: 6px solid #ffffff;
            box-shadow: 0 12px 30px rgba(0,0,0,0.20);
            border-radius: 50%;
            background: #e6e6e6;
        }
        .avatar-mono {
            display: flex; align-items: center; justify-content: center;
            color: #ffffff; font-weight: 900;
            font-size: 76px; font-family: "Inter", "Noto Serif SC", sans-serif;
        }
        .headtext {
            display: flex; flex-direction: column; justify-content: center;
            min-height: 156px;
            padding-right: 220px;
        }
        .cat {
            font-family: "Inter", sans-serif;
            font-size: 24px; font-weight: 700; letter-spacing: 4px;
            color: var(--accent); text-transform: uppercase;
            margin-bottom: 24px;
        }
        .nm {
            font-size: 76px; font-weight: 900; line-height: 1.1;
            letter-spacing: -1px; margin: 0 0 20px;
            word-break: break-word;
        }
        .bio {
            font-family: "Inter", sans-serif;
            font-size: 24px; color: #8a8a8a;
            padding-bottom: 50px;
        }

        /* === 正文 === */
        .content {
            padding: 50px 80px 56px;
            font-size: 33px; line-height: 1.9;
            border-top: 6px solid var(--bar);
            counter-reset: h2c;
            text-align: justify;
        }
        .content p { margin: 0 0 32px; }
        .content h1 { font-size: 52px; font-weight: 900; margin: 50px 0 24px; }
        .content h2 { font-size: 46px; font-weight: 900; margin: 54px 0 22px; }
        .content h2::before {
            counter-increment: h2c;
            content: counter(h2c, decimal-leading-zero) "  ";
            font-family: "Inter", sans-serif;
            font-size: 30px; color: var(--accent); font-weight: 800;
        }
        .content h3 { font-size: 38px; font-weight: 800; margin: 36px 0 18px; }
        .content blockquote {
            margin: 40px 0; padding: 30px 40px;
            background: var(--quote-bg);
            border-left: 6px solid var(--accent);
            font-style: italic; color: #444;
        }
        .content ul, .content ol { padding-left: 48px; margin-bottom: 32px; }
        .content li { margin-bottom: 14px; }
        .content strong {
            color: #161616; font-weight: 700;
            border-bottom: 3px solid var(--line); padding-bottom: 1px;
        }
        .content code {
            font-family: "Menlo", "Monaco", "Courier New", monospace;
            background: #f3f3f0; color: #444;
            padding: 4px 10px; border-radius: 4px; font-size: 0.92em;
        }
        .topic-pill {
            display: inline-block;
            font-family: "Inter", sans-serif;
            background: var(--soft); color: var(--accent);
            padding: 2px 16px; border-radius: 6px;
            font-size: 28px; line-height: 1.5; font-weight: 600;
            vertical-align: middle; margin: 0 4px;
            border: none;
        }
        /* 标签即使被模型放进标题/加粗里，也保持小尺寸、不带下划线 */
        .content h1 .topic-pill,
        .content h2 .topic-pill,
        .content h3 .topic-pill { font-size: 28px; }
        .content strong .topic-pill { font-weight: 600; }
        .content strong:has(> .topic-pill) { border-bottom: none; }
        .content h2:has(> .topic-pill)::before,
        .content h2 strong:has(> .topic-pill) { border-bottom: none; }

        /* === 页脚 === */
        .foot {
            font-family: "Inter", sans-serif;
            padding: 30px 80px 60px;
            font-size: 22px; color: #9a9a9a;
        }
        .foot .cr { color: var(--accent); font-weight: 700; letter-spacing: 0.5px; }
        """

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>{css}</style>
        </head>
        <body>
            <div class="container" id="card">
                <div class="masthead">
                    <span class="brand">PORTRAYAL · CORE</span>
                </div>

                <div class="head">
                    {avatar_html}
                    <div class="headtext">
                        <div class="cat">心理侧写 / 群友档案</div>
                        <div class="nm">{safe_nick}</div>
                        <div class="bio">被观测对象 · 深度行为分析样本</div>
                    </div>
                </div>

                <div class="content">
                    {html_body}
                </div>

                <div class="foot">
                    <span class="cr">Menkelo/astrbot_plugin_portrayal_core</span>
                </div>
            </div>
        </body>
        </html>
        """

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page(device_scale_factor=3.0)
                await page.set_content(html_content)
                card = await page.query_selector("#card")
                if not card:
                    raise Exception("#card not found")
                return await card.screenshot(type="png", omit_background=True)
            finally:
                await browser.close()
