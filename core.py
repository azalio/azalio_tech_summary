import subprocess
import requests
import os
import json
import re
import shutil

class VibeCore:
    def __init__(self):
        self.tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not self.tg_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN env var is required (see env.example)")
        self.chat_id = os.environ.get("TELEGRAM_DEFAULT_CHAT_ID", "")
        self.TG_LIMIT = 4000

    def format_to_html(self, text):
        """Converts basic Markdown to Telegram-compatible HTML."""
        # 1. Escape HTML special chars first
        text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        # 2. Bold: **text** or __text__
        text = re.sub(r'(\*\*|__)(.*?)\1', r'<b>\2</b>', text)

        # 3. Bold: single *text* (after ** already handled)
        text = re.sub(r'(?<!\w)\*([^\*\n]+?)\*(?!\w)', r'<b>\1</b>', text)

        # 4. Italic: _text_
        text = re.sub(r'(?<!\w)_([^_\n]+?)_(?!\w)', r'<i>\1</i>', text)

        # 5. Markdown links [text](url)
        text = re.sub(r'\[([^\]]+?)\]\(([^)]+?)\)', r'<a href="\2">\1</a>', text)

        # 6. Headers: #, ##, ### → bold
        text = re.sub(r'^#+\s+(.*)$', r'<b>\1</b>', text, flags=re.M)

        # 7. Lists: *, - → •
        text = re.sub(r'^\s*[\*\-]\s+', '• ', text, flags=re.M)

        return text

    def _split_by_sections(self, text):
        """Split text into sections by bold headers (<b>🔥, <b>💰, etc.)."""
        # Split on lines that start with <b> and contain emoji section headers
        parts = re.split(r'(?=\n<b>[🔥💰🌍🤖⚠️])', text)
        return [p.strip() for p in parts if p.strip()]

    def _send_one(self, url, text, chat_id=None):
        """Send one message, fallback to plain text if HTML fails."""
        target_chat = chat_id or self.chat_id
        resp = requests.post(url, data={
            "chat_id": target_chat,
            "text": text,
            "parse_mode": "HTML",
        }, timeout=30)
        result = resp.json()
        if not result.get("ok"):
            print(f"  TG HTML error: {result.get('description', 'unknown')}")
            # Fallback: strip tags and send as plain text
            plain = re.sub(r'<[^>]+>', '', text)
            resp = requests.post(url, data={
                "chat_id": target_chat,
                "text": plain,
            }, timeout=30)
            result = resp.json()
            if not result.get("ok"):
                print(f"  TG plain error: {result.get('description', 'unknown')}")
            else:
                print("  TG fallback OK (plain text)")
        return result

    def send_tg(self, text, title="INTEL", chat_id=None):
        target_chat = chat_id or self.chat_id
        url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"

        clean_text = text
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                clean_text = data.get("response", data.get("text", text))
        except:
            if "```" in text:
                m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
                if m:
                    try:
                        inner = m.group(1)
                        data = json.loads(inner)
                        clean_text = data.get("response", data.get("text", inner))
                    except:
                        clean_text = m.group(1)

        formatted_content = self.format_to_html(clean_text)
        header = f"⚡️ <b>{title}</b>\n\n"
        full_text = header + formatted_content

        print(f"Sending to Telegram ({len(full_text)} chars) to {target_chat}...")

        if len(full_text) <= self.TG_LIMIT:
            self._send_one(url, full_text, chat_id=target_chat)
            return

        # Split by topic sections and group into messages
        sections = self._split_by_sections(formatted_content)
        if not sections:
            # No sections found, hard split as last resort
            for i in range(0, len(full_text), self.TG_LIMIT):
                self._send_one(url, full_text[i:i + self.TG_LIMIT], chat_id=target_chat)
            return

        messages = []
        current = header
        for section in sections:
            # If adding this section exceeds limit, flush current message
            if len(current) + len(section) + 2 > self.TG_LIMIT:
                if current.strip():
                    messages.append(current.strip())
                current = section + "\n\n"
            else:
                current += section + "\n\n"
        if current.strip():
            messages.append(current.strip())

        print(f"  Split into {len(messages)} messages")
        for i, msg in enumerate(messages):
            print(f"  Sending part {i+1}/{len(messages)} ({len(msg)} chars)...")
            self._send_one(url, msg, chat_id=target_chat)

    def ask_llm(self, prompt):
        """Run an LLM CLI (gemini/codex) in cron-safe mode.

        Cron runs with a minimal PATH, so we try absolute paths first.
        """

        # CLI lookup: env override → command on PATH
        candidates = [
            ("gemini", [os.environ.get("GEMINI_BIN", ""), "gemini"]),
            ("codex", [os.environ.get("CODEX_BIN", ""), "codex"]),
        ]

        # Extend PATH for subprocesses (cron runs with a minimal PATH)
        extra_paths = [
            os.path.expanduser("~/.npm-global/bin"),
            "/home/linuxbrew/.linuxbrew/bin",
            os.path.expanduser("~/.local/bin"),
        ]
        env = os.environ.copy()
        env["PATH"] = ":".join(extra_paths + [env.get("PATH", "")])

        for name, paths in candidates:
            for cli in paths:
                resolved = cli if os.path.isabs(cli) else shutil.which(cli, path=env["PATH"])
                if not resolved:
                    continue
                try:
                    print(f"Trying {name} ({resolved})...")
                    process = subprocess.Popen(
                        [resolved],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=env,
                    )
                    stdout, stderr = process.communicate(input=prompt, timeout=180)
                    if process.returncode == 0 and stdout and stdout.strip():
                        return stdout.strip()
                    if stderr and stderr.strip():
                        print(f"{name} stderr: {stderr.strip()[:500]}")
                except Exception as e:
                    print(f"{name} failed: {e}")
                    continue

        return None
