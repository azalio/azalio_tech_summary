#!/usr/bin/env python3
import praw
import json
import os
import requests
import sqlite3
import subprocess
import html
from datetime import datetime
import tempfile

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# --- CONFIG ---
WORKSPACE = os.path.expanduser(os.environ.get("VIBE_WORKSPACE", "./workspace"))
DB_PATH = os.path.join(WORKSPACE, "memory", "reddit_sent.db")
CREDENTIALS_PATH = os.path.expanduser(
    os.environ.get("REDDIT_CREDENTIALS", "~/.config/reddit/credentials.json")
)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_REDDIT_CHAT_ID", "")
if not TELEGRAM_TOKEN or not CHAT_ID:
    raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_REDDIT_CHAT_ID env vars are required")
MIN_SCORE = 1000
# Subreddits with image/video content. Comma-separated list in env var.
# Empty list = media reposts disabled.
MEDIA_SUBS = [s.strip() for s in os.environ.get("REDDIT_MEDIA_SUBS", "").split(",") if s.strip()]
TEXT_SUBS = ["ClaudeAI", "kimi", "interesting", "ThinkingDeeplyAI", "KafkaFPS",
             "ClaudeCode", "AgentsOfAI", "Anthropic", "artificial",
             "ArtificialInteligence", "AskReddit", "books",
             "ChatGPT", "ChatGPTCoding", "ChatGPTPro", "ChatGPTPromptGenius",
             "codex", "comfyui", "dadjokes", "datascience", "devops",
             "explainlikeimfive", "stocks",
             "UpliftingNews", "nottheonion",
             # Tech / Programming
             "programming", "ExperiencedDevs", "golang", "rust", "Python", "cpp",
             "opensource", "linux", "sysadmin", "PostgreSQL",
             # Cloud / K8s / CNCF / Infra
             "kubernetes", "aws", "azure", "googlecloud", "docker", "Terraform",
             "selfhosted", "homelab",
             # AI / ML Engineering
             "MachineLearning", "LocalLLaMA", "StableDiffusion", "MLOps",
             "singularity",
             # Security
             "netsec", "cybersecurity", "ReverseEngineering", "privacy",
             # Crypto / DeFi Tech
             "ethereum", "CryptoTechnology", "ethdev", "defi",
             # Hardware
             "hardware", "nvidia", "gadgets", "tech"]
MAX_SIZE_MB = 16

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sent_posts (
            url TEXT PRIMARY KEY,
            subreddit TEXT,
            sent_at TIMESTAMP
        )
    ''')
    conn.commit()
    return conn

def is_already_sent(conn, url):
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM sent_posts WHERE url = ?', (url,))
    return cursor.fetchone() is not None

def mark_as_sent(conn, url, subreddit):
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()
    cursor.execute('INSERT OR IGNORE INTO sent_posts (url, subreddit, sent_at) VALUES (?, ?, ?)', 
                   (url, subreddit, now_str))
    conn.commit()

def get_reddit_client():
    with open(CREDENTIALS_PATH, "r") as f:
        creds = json.load(f)
    return praw.Reddit(
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        user_agent=creds["user_agent"]
    )

def send_telegram(method, data, files=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    try:
        r = requests.post(url, data=data, files=files, timeout=60)
        return r.json()
    except Exception as e:
        print(f"Telegram error: {e}")
        return None

def convert_gif_to_mp4(input_path, output_path):
    """Converts GIF to H.264 MP4 using ffmpeg, optimized for Telegram."""
    try:
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-movflags", "faststart",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264",
            "-crf", "23",
            "-preset", "medium",
            output_path
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"FFmpeg conversion error: {e}")
        return False

def process_heavy_gif(url, caption, spoiler=False):
    """Downloads a GIF, and if it's heavy, converts it to MP4 before sending."""
    with tempfile.TemporaryDirectory() as tmpdir:
        input_gif = os.path.join(tmpdir, "input.gif")
        output_mp4 = os.path.join(tmpdir, "output.mp4")
        
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(input_gif, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            
            size_mb = os.path.getsize(input_gif) / (1024 * 1024)
            
            if size_mb > MAX_SIZE_MB:
                print(f"GIF is heavy ({size_mb:.1f}MB), converting...")
                if convert_gif_to_mp4(input_gif, output_mp4):
                    with open(output_mp4, "rb") as f:
                        return send_telegram("sendVideo", {"chat_id": CHAT_ID, "caption": caption, "has_spoiler": spoiler}, {"video": f})
            else:
                with open(input_gif, "rb") as f:
                    return send_telegram("sendAnimation", {"chat_id": CHAT_ID, "caption": caption, "has_spoiler": spoiler}, {"animation": f})
        except Exception as e:
            print(f"Error processing heavy gif: {e}")
    return None

def process_media_subs(reddit, conn):
    for sub_name in MEDIA_SUBS:
        print(f"Processing r/{sub_name}...")
        try:
            subreddit = reddit.subreddit(sub_name)
            for post in subreddit.hot(limit=25):
                if post.stickied or post.score < MIN_SCORE or is_already_sent(conn, post.url):
                    continue
                
                media_url = post.url
                caption = f"{post.title}\n\nhttps://reddit.com{post.permalink}"
                
                success = False
                if any(media_url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png']):
                    res = send_telegram("sendPhoto", {"chat_id": CHAT_ID, "photo": media_url, "caption": caption, "has_spoiler": True})
                    success = res and res.get("ok")
                elif any(media_url.lower().endswith(ext) for ext in ['.gif', '.gifv']):
                    res = send_telegram("sendAnimation", {"chat_id": CHAT_ID, "animation": media_url, "caption": caption, "has_spoiler": True})
                    if res and not res.get("ok"):
                        print(f"URL send failed for {media_url}, trying local conversion...")
                        res = process_heavy_gif(media_url, caption, spoiler=True)
                    success = True
                elif 'v.redd.it' in media_url or post.is_video:
                    print(f"Processing video {media_url}...")
                    video_success = False
                    try:
                        hls_url = None
                        if hasattr(post, 'media') and post.media and 'reddit_video' in post.media:
                            hls_url = post.media['reddit_video'].get('hls_url')
                        
                        if hls_url:
                            with tempfile.TemporaryDirectory() as tmpdir:
                                out_path = os.path.join(tmpdir, "video.mp4")
                                subprocess.run([
                                    "ffmpeg", "-y", "-user_agent", "MyBot/0.1",
                                    "-i", hls_url, "-c", "copy", "-movflags", "faststart",
                                    out_path
                                ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                                    with open(out_path, "rb") as f:
                                        send_telegram("sendVideo", {"chat_id": CHAT_ID, "caption": caption, "has_spoiler": True}, {"video": f})
                                    video_success = True
                    except Exception as e:
                        print(f"FFmpeg HLS failed: {e}")

                    if not video_success:
                        with tempfile.TemporaryDirectory() as tmpdir:
                            out_tmpl = os.path.join(tmpdir, "vid.%(ext)s")
                            ytdlp = os.environ.get("YT_DLP_PATH", "yt-dlp")
                            if os.path.isabs(ytdlp) and not os.path.exists(ytdlp):
                                ytdlp = "yt-dlp"
                            try:
                                subprocess.run([
                                    ytdlp, "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best", 
                                    "-o", out_tmpl, media_url
                                ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                for f in os.listdir(tmpdir):
                                    if f.startswith("vid."):
                                        with open(os.path.join(tmpdir, f), "rb") as vf:
                                            send_telegram("sendVideo", {"chat_id": CHAT_ID, "caption": caption, "has_spoiler": True}, {"video": vf})
                                        video_success = True
                                        break
                            except Exception as e:
                                print(f"yt-dlp failed: {e}")

                    if not video_success:
                        send_telegram("sendMessage", {"chat_id": CHAT_ID, "text": f"🎥 Video r/{sub_name}: {caption}\nDirect: {media_url}"})
                    success = True
                
                if success:
                    mark_as_sent(conn, post.url, sub_name)
        except Exception as e:
            print(f"Error r/{sub_name}: {e}")

def get_page_snippet(url):
    """Tries to get a meta description or initial text from an external page."""
    if not url or "reddit.com" in url:
        return ""
    
    binary_exts = ['.png', '.jpg', '.jpeg', '.gif', '.gifv', '.mp4', '.mov', '.webm']
    if any(url.lower().endswith(ext) for ext in binary_exts) or "i.redd.it" in url or "v.redd.it" in url:
        return ""

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        }
        with requests.get(url, timeout=15, headers=headers) as r:
            r.raise_for_status()
            content_type = r.headers.get('Content-Type', '').lower()
            if 'text/html' not in content_type:
                return ""
            html_content = r.text
        
        import re
        m = re.search(r'<meta\s+(?:name|property)="[^"]*description"\s+content="([^"]+)"', html_content, re.I)
        if not m:
            m = re.search(r'<meta\s+content="([^"]+)"\s+(?:name|property)="[^"]*description"', html_content, re.I)
        
        res = ""
        if m:
            res = m.group(1).strip()
        else:
            text = re.sub(r'<script.*?</script>', '', html_content, flags=re.S|re.I)
            text = re.sub(r'<style.*?</style>', '', text, flags=re.S|re.I)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            res = text[:600]
            
        return html.unescape(res) if res else ""
    except Exception:
        return ""

def get_top_comments(post, limit=3):
    """Fetches the top X comments for a given post."""
    comments = []
    try:
        # Sort comments by top
        post.comment_sort = "top"
        post.comments.replace_more(limit=0) # Only fetch initial top comments
        for comment in post.comments[:limit]:
            if hasattr(comment, "body"):
                comments.append({
                    "author": str(comment.author),
                    "score": comment.score,
                    "body": html.unescape(comment.body[:500])
                })
    except Exception as e:
        print(f"  [WARN] Failed to fetch comments: {e}")
    return comments

def process_text_subs(reddit, conn):
    raw_data = []
    for sub_name in TEXT_SUBS:
        try:
            subreddit = reddit.subreddit(sub_name)
            for post in subreddit.hot(limit=50):
                if post.stickied or post.score < MIN_SCORE or is_already_sent(conn, post.url):
                    continue
                
                print(f"  [OK] Processing text post: {post.title[:50]}...")
                text = post.selftext[:1200] if hasattr(post, 'selftext') and post.selftext else ""
                external_url = post.url if not post.is_self else ""
                
                if not text and external_url:
                    text = get_page_snippet(external_url)
                
                # Enrich with top comments
                top_comments = get_top_comments(post)
                
                raw_data.append({
                    "subreddit": sub_name,
                    "title": post.title,
                    "url": f"https://reddit.com{post.permalink}",
                    "external_url": external_url,
                    "score": post.score,
                    "text": html.unescape(text) if text else "",
                    "top_comments": top_comments
                })
                mark_as_sent(conn, post.url, sub_name)
        except Exception as e:
            print(f"Error r/{sub_name}: {e}")
    
    RAW_JSON_PATH = os.path.join(WORKSPACE, "memory", "reddit_ai_raw.json")
    os.makedirs(os.path.dirname(RAW_JSON_PATH), exist_ok=True)
    with open(RAW_JSON_PATH, "w") as f:
        json.dump(raw_data, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    db_conn = init_db()
    reddit_client = get_reddit_client()
    process_text_subs(reddit_client, db_conn)
    process_media_subs(reddit_client, db_conn)
    db_conn.close()
