import subprocess
import requests
import os
import json
import re
import shutil
import tempfile

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

    def _split_oversized_section(self, section, max_len):
        """Split one section into line-bounded chunks each ≤ max_len chars.

        A section starts with a "<b>HEADER</b>" line followed by bullet lines.
        Each output chunk repeats the section header so readers always see what
        topic the bullets belong to. Single bullet lines longer than max_len
        are still emitted (HTML send_one's plain-text fallback strips tags and
        usually fits) rather than truncated.
        """
        if len(section) <= max_len:
            return [section]

        lines = section.split("\n")
        header = ""
        body = lines
        if lines and lines[0].lstrip().startswith("<b>"):
            header = lines[0]
            body = lines[1:]

        def joined_len(parts):
            return sum(len(p) for p in parts) + max(0, len(parts) - 1)

        chunks = []
        current = [header] if header else []
        # When header is present, current always starts with header — only flush
        # when there's at least one body line in addition to the header.
        body_floor = 1 if header else 0

        for line in body:
            if joined_len(current + [line]) > max_len and len(current) > body_floor:
                chunks.append("\n".join(current))
                current = [header] if header else []
            current.append(line)

        if len(current) > body_floor:
            chunks.append("\n".join(current))
        return chunks

    def _channel_footer(self, chat_id):
        """Footer with a t.me link to the target channel.

        Appended to every split message so a forwarded fragment still shows
        where it came from. Returns '' for non-public targets (numeric IDs,
        private chats) where t.me URLs don't resolve.
        """
        if not chat_id:
            return ""
        s = str(chat_id)
        if not s.startswith("@"):
            return ""
        handle = s.lstrip("@")
        return f'\n\n—\n📡 <a href="https://t.me/{handle}">@{handle}</a>'

    def _send_one(self, url, text, chat_id=None):
        """Send one message, fallback to plain text if HTML fails.

        Returns True if Telegram accepted either the HTML or the plain-text
        fallback; False if both attempts failed, the HTTP call raised, or the
        response body wasn't decodable JSON (caught as ValueError, which
        json.JSONDecodeError and requests.JSONDecodeError both subclass).
        """
        target_chat = chat_id or self.chat_id
        try:
            resp = requests.post(url, data={
                "chat_id": target_chat,
                "text": text,
                "parse_mode": "HTML",
            }, timeout=30)
            result = resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  TG HTML request error: {e}")
            result = {"ok": False, "description": str(e)}

        if result.get("ok"):
            return True

        print(f"  TG HTML error: {result.get('description', 'unknown')}")
        # Fallback: strip tags and send as plain text
        plain = re.sub(r'<[^>]+>', '', text)
        try:
            resp = requests.post(url, data={
                "chat_id": target_chat,
                "text": plain,
            }, timeout=30)
            result = resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  TG plain request error: {e}")
            return False

        if result.get("ok"):
            print("  TG fallback OK (plain text)")
            return True
        print(f"  TG plain error: {result.get('description', 'unknown')}")
        return False

    def send_tg(self, text, title="INTEL", chat_id=None):
        """Post the digest to Telegram, splitting long output by topic section.

        Returns True iff every message part was delivered (HTML or plain-text
        fallback). main.py uses this to gate post-send actions like
        Collectors.commit_seen() — see GitHub issue #2.
        """
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
        footer = self._channel_footer(target_chat)
        # Reserve room for the footer in every split message so a shared
        # fragment still shows where it came from.
        effective_limit = self.TG_LIMIT - len(footer)

        print(f"Sending to Telegram ({len(full_text) + len(footer)} chars) to {target_chat}...")

        if len(full_text) <= effective_limit:
            return self._send_one(url, full_text + footer, chat_id=target_chat)

        # Split by topic sections and group into messages
        sections = self._split_by_sections(formatted_content)
        if not sections:
            # No sections found, hard split as last resort
            all_ok = True
            for i in range(0, len(full_text), effective_limit):
                if not self._send_one(url, full_text[i:i + effective_limit] + footer, chat_id=target_chat):
                    all_ok = False
            return all_ok

        # Pre-expand: any single section longer than what fits with the header
        # gets line-split here so the grouping loop below stays simple.
        section_budget = effective_limit - len(header)
        expanded = []
        for section in sections:
            if len(section) > section_budget:
                expanded.extend(self._split_oversized_section(section, section_budget))
            else:
                expanded.append(section)
        sections = expanded

        messages = []
        current = header
        for section in sections:
            # If adding this section exceeds limit, flush current message
            if len(current) + len(section) + 2 > effective_limit:
                if current.strip():
                    messages.append(current.strip())
                current = section + "\n\n"
            else:
                current += section + "\n\n"
        if current.strip():
            messages.append(current.strip())

        print(f"  Split into {len(messages)} messages")
        all_ok = True
        for i, msg in enumerate(messages):
            print(f"  Sending part {i+1}/{len(messages)} ({len(msg) + len(footer)} chars)...")
            if not self._send_one(url, msg + footer, chat_id=target_chat):
                all_ok = False
        return all_ok

    def ask_llm(self, prompt):
        """Run an LLM CLI (gemini/codex) in cron-safe mode.

        Cron runs with a minimal PATH, so we try absolute paths first.
        Codex prints a decorated transcript to stdout, so we redirect its
        final-message output to a temp file and read it back.
        """

        # Extend PATH for subprocesses (cron runs with a minimal PATH)
        extra_paths = [
            os.path.expanduser("~/.npm-global/bin"),
            "/home/linuxbrew/.linuxbrew/bin",
            os.path.expanduser("~/.local/bin"),
        ]
        env = os.environ.copy()
        env["PATH"] = ":".join(extra_paths + [env.get("PATH", "")])

        # Each entry: (name, path hints, runner). Runner returns clean text or None.
        candidates = [
            ("gemini", [os.environ.get("GEMINI_BIN", ""), "gemini"], self._run_gemini),
            ("codex", [os.environ.get("CODEX_BIN", ""), "codex"], self._run_codex),
        ]

        # Dedupe by resolved binary: env-var + PATH lookup often point at the
        # same path, and re-running a hanging CLI just burns another timeout.
        tried = set()
        for name, hints, runner in candidates:
            for cli in hints:
                if not cli:
                    continue
                resolved = cli if os.path.isabs(cli) else shutil.which(cli, path=env["PATH"])
                if not resolved or resolved in tried:
                    continue
                tried.add(resolved)
                print(f"Trying {name} ({resolved})...")
                try:
                    out = runner(resolved, prompt, env, timeout=180)
                except Exception as e:
                    print(f"{name} failed: {e}")
                    continue
                if out and out.strip():
                    return out.strip()

        return None

    def _run_subprocess(self, argv, prompt, env, timeout, stdout):
        """Run argv with prompt on stdin; return (returncode, stdout_text, stderr_text).

        Kills the child on timeout so we don't leak orphaned CLI processes that
        keep holding network sockets when the next cron tick arrives.
        """
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            out, err = process.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        return process.returncode, out, err

    def _run_gemini(self, resolved, prompt, env, timeout):
        rc, out, err = self._run_subprocess(
            [resolved], prompt, env, timeout, stdout=subprocess.PIPE,
        )
        if rc == 0 and out and out.strip():
            return out.strip()
        if err and err.strip():
            print(f"gemini stderr: {err.strip()[:500]}")
        return None

    def _run_codex(self, resolved, prompt, env, timeout):
        fd, out_path = tempfile.mkstemp(prefix="codex_out_", suffix=".txt")
        os.close(fd)
        try:
            rc, _, err = self._run_subprocess(
                [resolved, "exec", "--skip-git-repo-check", "-o", out_path, "-"],
                prompt, env, timeout, stdout=subprocess.DEVNULL,
            )
            if rc != 0:
                if err and err.strip():
                    print(f"codex stderr: {err.strip()[:500]}")
                return None
            try:
                with open(out_path, "r", encoding="utf-8") as f:
                    text = f.read().strip()
            except OSError as e:
                print(f"codex output read failed: {e}")
                return None
            return text or None
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
