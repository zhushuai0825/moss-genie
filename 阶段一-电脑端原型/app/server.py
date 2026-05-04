#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "moss_stage1.db"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
AUDIO_DIR = DATA_DIR / "audio"


def load_env_file() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def json_response(handler: SimpleHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def cache_buster() -> str:
    candidates = [WEB_DIR / "app.js", WEB_DIR / "styles.css", Path(__file__)]
    latest = max(path.stat().st_mtime for path in candidates if path.exists())
    return str(int(latest))


def read_json(handler: SimpleHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 格式错误：{exc}") from exc


def connect_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db() -> None:
    with connect_db() as db:
        db.executescript(
            """
            create table if not exists memories (
              id integer primary key autoincrement,
              kind text not null default 'note',
              content text not null,
              tags text not null default '',
              created_at text not null
            );

            create table if not exists wishes (
              id integer primary key autoincrement,
              title text not null,
              reason text not null default '',
              stage text not null default 'seed',
              energy integer not null default 0,
              next_action text not null default '',
              status text not null default 'active',
              created_at text not null,
              updated_at text not null
            );

            create table if not exists audit_logs (
              id integer primary key autoincrement,
              action text not null,
              args_json text not null,
              result text not null,
              created_at text not null
            );

            create table if not exists conversations (
              id integer primary key autoincrement,
              user_text text not null,
              assistant_text text not null,
              source text not null,
              memory_ids text not null default '',
              created_at text not null
            );
            """
        )
    MemoryStore.repair_existing()


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def search_terms(text: str) -> list[str]:
    chunks = re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]+", text.lower())
    terms: list[str] = []
    for chunk in chunks:
        if re.fullmatch(r"[a-zA-Z0-9_]+", chunk):
            if len(chunk) >= 2:
                terms.append(chunk)
            continue
        if 2 <= len(chunk) <= 6:
            terms.append(chunk)
        for size in (2, 3, 4):
            for index in range(0, max(0, len(chunk) - size + 1)):
                terms.append(chunk[index : index + size])
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        if term not in seen:
            unique.append(term)
            seen.add(term)
    return unique[:80]


class MemoryStore:
    @staticmethod
    def list(q: str = "") -> list[dict[str, Any]]:
        with connect_db() as db:
            if q:
                rows = db.execute(
                    """
                    select * from memories
                    where content like ? or tags like ? or kind like ?
                    order by id desc
                    """,
                    (f"%{q}%", f"%{q}%", f"%{q}%"),
                ).fetchall()
            else:
                rows = db.execute("select * from memories order by id desc").fetchall()
        return rows_to_dicts(rows)

    @staticmethod
    def normalize(content: str) -> tuple[str, str]:
        text = re.sub(r"\s+", " ", content.strip()).strip(" 。.!！")
        if text.startswith("称呼："):
            return text, "profile,name,voice,auto"
        if text.startswith("偏好："):
            return text, "profile,preference,voice,auto"
        nickname_patterns = [
            r"^(?:我叫|我是)\s*(.+)$",
            r"^(?:请)?(?:叫我|喊我)\s*(.+)$",
            r"^(.+?)[,， ]*你以后(?:可不可以|可以|就)?(?:叫我|喊我)\s*(.+)$",
            r"^以后(?:请)?(?:叫我|喊我)\s*(.+)$",
        ]
        for pattern in nickname_patterns:
            match = re.search(pattern, text)
            if match:
                name = (match.group(2) if match.lastindex and match.lastindex >= 2 else match.group(1)).strip(" 。.!！")
                if 1 <= len(name) <= 20:
                    return f"称呼：{name}", "profile,name,voice,auto"
        like_match = re.search(r"^我喜欢\s*(.+)$", text)
        if like_match:
            return f"偏好：喜欢{like_match.group(1).strip(' 。.!！')}", "profile,preference,voice,auto"
        dislike_match = re.search(r"^我不喜欢\s*(.+)$", text)
        if dislike_match:
            return f"偏好：不喜欢{dislike_match.group(1).strip(' 。.!！')}", "profile,preference,voice,auto"
        return text, "voice,auto"

    @staticmethod
    def add(content: str, kind: str = "note", tags: str = "") -> dict[str, Any]:
        content = content.strip()
        if not content:
            raise ValueError("记忆内容不能为空")
        if kind == "auto":
            content, normalized_tags = MemoryStore.normalize(content)
            tags = MemoryStore.merge_tags(normalized_tags, tags)
        with connect_db() as db:
            existing = db.execute(
                "select * from memories where content = ? order by id desc limit 1",
                (content,),
            ).fetchone()
            if existing:
                merged_tags = MemoryStore.merge_tags(str(existing["tags"]), tags)
                if merged_tags != str(existing["tags"]):
                    db.execute("update memories set tags = ? where id = ?", (merged_tags, existing["id"]))
                    existing = db.execute("select * from memories where id = ?", (existing["id"],)).fetchone()
                return dict(existing)
            cur = db.execute(
                "insert into memories(kind, content, tags, created_at) values (?, ?, ?, ?)",
                (kind.strip() or "note", content, tags.strip(), now_iso()),
            )
            row = db.execute("select * from memories where id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    @staticmethod
    def merge_tags(*values: str) -> str:
        tags: list[str] = []
        seen: set[str] = set()
        for value in values:
            for raw_tag in str(value or "").split(","):
                tag = raw_tag.strip()
                if tag and tag not in seen:
                    tags.append(tag)
                    seen.add(tag)
        return ",".join(tags)

    @staticmethod
    def repair_existing() -> None:
        with connect_db() as db:
            rows = db.execute("select * from memories where kind = 'auto'").fetchall()
            for row in rows:
                normalized, normalized_tags = MemoryStore.normalize(str(row["content"]))
                merged_tags = MemoryStore.merge_tags(normalized_tags, str(row["tags"]))
                if normalized != row["content"] or merged_tags != row["tags"]:
                    db.execute(
                        "update memories set content = ?, tags = ? where id = ?",
                        (normalized, merged_tags, row["id"]),
                    )

    @staticmethod
    def delete(memory_id: int) -> bool:
        with connect_db() as db:
            cur = db.execute("delete from memories where id = ?", (memory_id,))
        return cur.rowcount > 0

    @staticmethod
    def relevant(message: str, limit: int = 6) -> list[dict[str, Any]]:
        words = search_terms(message)
        all_items = MemoryStore.list()
        prioritized = MemoryStore.intent_matches(message, all_items)
        if not words:
            return MemoryStore.unique_items([*prioritized, *all_items], limit)
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in all_items:
            haystack = f"{item.get('kind', '')} {item.get('content', '')} {item.get('tags', '')}".lower()
            score = sum(1 for word in words if word in haystack)
            if score:
                scored.append((score, item))
        scored.sort(key=lambda pair: (pair[0], pair[1]["id"]), reverse=True)
        return MemoryStore.unique_items([*prioritized, *[item for _, item in scored]], limit)

    @staticmethod
    def intent_matches(message: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        text = re.sub(r"\s+", "", message)
        intents: list[str] = []
        if re.search(r"(我.*(叫什?么|名字|称呼)|你.*(叫我|喊我|称呼我)|该.*(叫我|喊我))", text):
            intents.append("name")
        if re.search(r"(我.*(喜欢|不喜欢|偏好)|记得.*(喜欢|偏好))", text):
            intents.append("preference")
        if not intents:
            return []

        matched: list[dict[str, Any]] = []
        for item in items:
            content = str(item.get("content", ""))
            tags = str(item.get("tags", ""))
            if "name" in intents and (content.startswith("称呼：") or "name" in tags):
                matched.append(item)
            elif "preference" in intents and (content.startswith("偏好：") or "preference" in tags):
                matched.append(item)
        return matched

    @staticmethod
    def unique_items(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[int] = set()
        for item in items:
            item_id = int(item["id"])
            if item_id not in seen:
                result.append(item)
                seen.add(item_id)
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def maybe_remember(message: str) -> dict[str, Any] | None:
        text = message.strip()
        normalized, normalized_tags = MemoryStore.normalize(text)
        if normalized != re.sub(r"\s+", " ", text).strip(" 。.!！") and normalized_tags.startswith("profile"):
            return MemoryStore.add(normalized, "auto", normalized_tags)
        patterns = [
            r"^(?:请)?记住[:：]?\s*(.+)$",
            r"^你要记得[:：]?\s*(.+)$",
            r"^我(?:叫|是)\s*(.+)$",
            r"^我喜欢\s*(.+)$",
            r"^我不喜欢\s*(.+)$",
            r"^以后(?:都)?\s*(.+)$",
            r"^.*?(?:叫我|喊我)\s*(.+)$",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                content = match.group(1).strip(" 。.!！")
                if len(content) >= 2:
                    return MemoryStore.add(content, "auto")
        return None


class ConversationStore:
    @staticmethod
    def add(user_text: str, assistant_text: str, source: str, memories: list[dict[str, Any]]) -> dict[str, Any]:
        memory_ids = ",".join(str(item["id"]) for item in memories)
        with connect_db() as db:
            cur = db.execute(
                """
                insert into conversations(user_text, assistant_text, source, memory_ids, created_at)
                values (?, ?, ?, ?, ?)
                """,
                (user_text, assistant_text, source, memory_ids, now_iso()),
            )
            row = db.execute("select * from conversations where id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    @staticmethod
    def list(limit: int = 20) -> list[dict[str, Any]]:
        with connect_db() as db:
            rows = db.execute("select * from conversations order by id desc limit ?", (limit,)).fetchall()
        return rows_to_dicts(rows)


class WishStore:
    STAGES = ["seed", "sprout", "leaf", "flower", "done"]

    @classmethod
    def stage_for_energy(cls, energy: int, status: str = "active") -> str:
        if status == "done" or energy >= 100:
            return "done"
        if energy >= 70:
            return "flower"
        if energy >= 35:
            return "leaf"
        if energy >= 10:
            return "sprout"
        return "seed"

    @staticmethod
    def list() -> list[dict[str, Any]]:
        with connect_db() as db:
            rows = db.execute("select * from wishes order by status asc, id desc").fetchall()
        return rows_to_dicts(rows)

    @classmethod
    def add(cls, title: str, reason: str = "", next_action: str = "") -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise ValueError("心愿标题不能为空")
        ts = now_iso()
        with connect_db() as db:
            cur = db.execute(
                """
                insert into wishes(title, reason, stage, energy, next_action, status, created_at, updated_at)
                values (?, ?, 'seed', 0, ?, 'active', ?, ?)
                """,
                (title, reason.strip(), next_action.strip(), ts, ts),
            )
            row = db.execute("select * from wishes where id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    @classmethod
    def progress(cls, wish_id: int, delta: int, note: str = "") -> dict[str, Any]:
        delta = max(-100, min(100, int(delta)))
        with connect_db() as db:
            row = db.execute("select * from wishes where id = ?", (wish_id,)).fetchone()
            if not row:
                raise ValueError("没有找到这个心愿")
            energy = max(0, min(100, int(row["energy"]) + delta))
            status = "done" if energy >= 100 else row["status"]
            stage = cls.stage_for_energy(energy, status)
            db.execute(
                "update wishes set energy = ?, stage = ?, status = ?, updated_at = ? where id = ?",
                (energy, stage, status, now_iso(), wish_id),
            )
            if note.strip():
                db.execute(
                    "insert into memories(kind, content, tags, created_at) values (?, ?, ?, ?)",
                    ("wish_progress", f"心愿进展：{row['title']}。{note.strip()}", f"wish:{wish_id}", now_iso()),
                )
            updated = db.execute("select * from wishes where id = ?", (wish_id,)).fetchone()
        return dict(updated)


class AuditLog:
    @staticmethod
    def add(action: str, args: dict[str, Any], result: str) -> None:
        with connect_db() as db:
            db.execute(
                "insert into audit_logs(action, args_json, result, created_at) values (?, ?, ?, ?)",
                (action, json.dumps(args, ensure_ascii=False), result, now_iso()),
            )

    @staticmethod
    def list(limit: int = 50) -> list[dict[str, Any]]:
        with connect_db() as db:
            rows = db.execute("select * from audit_logs order by id desc limit ?", (limit,)).fetchall()
        return rows_to_dicts(rows)


class ModelAdapter:
    DEFAULT_MINIMAX_MODEL = "MiniMax-M1"

    @staticmethod
    def status() -> dict[str, Any]:
        host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
        model = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
        available, models = ModelAdapter._ollama_status(host)
        return {
            "ollama_host": host,
            "ollama_model": model,
            "ollama_available": available,
            "ollama_models": models,
            "minimax_configured": bool(os.getenv("MINIMAX_API_KEY")),
            "minimax_model": os.getenv("MINIMAX_TEXT_MODEL", ModelAdapter.DEFAULT_MINIMAX_MODEL),
        }

    @classmethod
    def reply(cls, message: str, memories: list[dict[str, Any]], api_key: str = "") -> tuple[str, str]:
        minimax_reply = cls._try_minimax(message, memories, api_key)
        if minimax_reply:
            return minimax_reply, "minimax"
        ollama_reply = cls._try_ollama(message, memories)
        if ollama_reply:
            return ollama_reply, "ollama"
        return cls._fallback_reply(message, memories), "local-fallback"

    @staticmethod
    def _ollama_status(host: str) -> tuple[bool, list[str]]:
        try:
            with urllib.request.urlopen(f"{host}/api/tags", timeout=1.2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            models = [str(item.get("name", "")) for item in data.get("models", []) if item.get("name")]
            return True, models[:12]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return False, []

    @staticmethod
    def _try_ollama(message: str, memories: list[dict[str, Any]]) -> str | None:
        host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
        model = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
        memory_text = "\n".join(f"- {m['content']}" for m in memories[:5]) or "暂无相关记忆。"
        payload = {
            "model": model,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是心愿 Moss 精灵的电脑端大脑。回复要温柔、简洁、可执行。"
                        "遇到危险电脑控制、删除、付款、发送消息等请求时提醒需要二次确认。"
                    ),
                },
                {"role": "system", "content": f"相关本地记忆：\n{memory_text}"},
                {"role": "user", "content": message},
            ],
        }
        req = urllib.request.Request(
            f"{host}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data.get("message", {}).get("content") or None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _try_minimax(message: str, memories: list[dict[str, Any]], api_key: str = "") -> str | None:
        api_key = api_key.strip() or os.getenv("MINIMAX_API_KEY", "").strip()
        if not api_key:
            return None
        endpoint = os.getenv("MINIMAX_TEXT_ENDPOINT", "https://api.minimax.io/v1/text/chatcompletion_v2")
        model = os.getenv("MINIMAX_TEXT_MODEL", ModelAdapter.DEFAULT_MINIMAX_MODEL)
        memory_text = "\n".join(f"- {m['content']}" for m in memories[:6]) or "暂无相关记忆。"
        name_memories = [m["content"].replace("称呼：", "", 1) for m in memories if str(m.get("content", "")).startswith("称呼：")]
        name_rule = ""
        if name_memories:
            name_rule = (
                "\n如果用户问自己的名字或你该怎么称呼用户，必须依据本地记忆回答。"
                f"当前可见称呼记忆按新到旧是：{'、'.join(name_memories)}。"
                "如果有多个称呼，不要说不知道，要说明我记得这些称呼，并优先说最近的一条。"
            )
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是心愿 Moss 精灵的电脑端大脑，像一个陪伴型桌面娃娃。"
                        "你要先回应用户刚说的话，再给一个很小、可执行的下一步。"
                        "回复中文，简短自然。不要假装你做了没做的电脑操作。"
                        f"{name_rule}"
                        f"\n\n你可以参考这些本地记忆：\n{memory_text}"
                    ),
                },
                {"role": "user", "content": message},
            ],
        }
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return None
        base_resp = data.get("base_resp") or {}
        if base_resp.get("status_code") not in (0, "0", None):
            return None
        choices = data.get("choices") or []
        if choices:
            message_obj = choices[0].get("message") or {}
            content = message_obj.get("content")
            if content:
                return str(content).strip()
        reply = data.get("reply") or data.get("text")
        return str(reply).strip() if reply else None

    @staticmethod
    def _fallback_reply(message: str, memories: list[dict[str, Any]]) -> str:
        lower = message.lower()
        if memories:
            memory_hint = f"我想起了 {len(memories)} 条以前的记忆，会参考它们来回你。"
        else:
            memory_hint = "我这次没有命中旧记忆。"
        if any(word in message for word in ["心愿", "我想", "希望", "目标"]):
            return f"我听到了这个心愿。{memory_hint} 我先帮你把它变成一个很小的下一步：今天只做 10 分钟。"
        if any(word in message for word in ["记住", "记忆", "喜欢"]):
            return "可以，我已经把能识别出的偏好放进本地记忆。你右侧可以看到现在存了哪些。"
        if re.search(r"(叫什?么|名字|称呼|叫我|喊我)", message):
            names = [m["content"].replace("称呼：", "", 1) for m in memories if str(m.get("content", "")).startswith("称呼：")]
            if names:
                return f"我记得你最近的称呼是：{names[0]}。我还看到这些称呼记忆：{'、'.join(names)}。"
        if any(word in lower for word in ["open", "打开", "搜索", "截图", "音量"]):
            return "电脑控制已经接好第一版安全指令。请在控制面板里预览动作，再确认执行。"
        return f"我听见了：{message}。{memory_hint} 现在大模型还没连上时，我会用本地规则先陪你跑通语音和记忆流程。"


class TTSAdapter:
    DEFAULT_MODEL = "speech-2.8-hd"
    DEFAULT_VOICE = "Chinese (Mandarin)_Cute_Spirit"

    @classmethod
    def status(cls) -> dict[str, Any]:
        return {
            "provider": "minimax",
            "configured": bool(os.getenv("MINIMAX_API_KEY")),
            "model": os.getenv("MINIMAX_TTS_MODEL", cls.DEFAULT_MODEL),
            "voice_id": os.getenv("MINIMAX_TTS_VOICE", cls.DEFAULT_VOICE),
            "endpoint": os.getenv("MINIMAX_TTS_ENDPOINT", "https://api.minimax.io/v1/t2a_v2"),
        }

    @classmethod
    def synthesize(cls, payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("text", "")).strip()
        if not text:
            raise ValueError("合成文本不能为空")
        if len(text) > 1000:
            raise ValueError("阶段一预览限制 1000 字以内")

        api_key = str(payload.get("api_key", "")).strip() or os.getenv("MINIMAX_API_KEY", "").strip()
        if not api_key:
            raise ValueError("没有配置 MiniMax API Key。可在 .env 设置 MINIMAX_API_KEY，或在本次请求里临时填写。")

        model = str(payload.get("model", "")).strip() or os.getenv("MINIMAX_TTS_MODEL", cls.DEFAULT_MODEL)
        voice_id = str(payload.get("voice_id", "")).strip() or os.getenv("MINIMAX_TTS_VOICE", cls.DEFAULT_VOICE)
        speed = cls._json_number(payload.get("speed", 1), 0.5, 2.0, 1)
        volume = cls._json_number(payload.get("volume", 1), 0.1, 10.0, 1)
        pitch = cls._integer(payload.get("pitch", 0), -12, 12, 0)
        endpoint = os.getenv("MINIMAX_TTS_ENDPOINT", "https://api.minimax.io/v1/t2a_v2")

        req_payload = {
            "model": model,
            "text": text,
            "stream": False,
            "language_boost": "Chinese",
            "output_format": "hex",
            "voice_setting": {
                "voice_id": voice_id,
                "speed": speed,
                "vol": volume,
                "pitch": pitch,
            },
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": "mp3",
                "channel": 1,
            },
        }

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(req_payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ValueError(f"MiniMax 请求失败：HTTP {exc.code} {body[:300]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"MiniMax 请求失败：{exc}") from exc

        base_resp = data.get("base_resp", {})
        if base_resp.get("status_code") not in (0, "0", None):
            raise ValueError(f"MiniMax 返回错误：{base_resp.get('status_msg', 'unknown error')}")
        audio_hex = data.get("data", {}).get("audio")
        if not audio_hex:
            raise ValueError("MiniMax 没有返回音频内容")
        try:
            audio_bytes = bytes.fromhex(audio_hex)
        except ValueError as exc:
            raise ValueError("MiniMax 音频不是有效 hex 格式") from exc

        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", voice_id).strip("-")[:48] or "voice"
        filename = f"tts-{int(time.time())}-{safe_id}.mp3"
        target = AUDIO_DIR / filename
        target.write_bytes(audio_bytes)
        return {
            "ok": True,
            "provider": "minimax",
            "model": model,
            "voice_id": voice_id,
            "audio_url": f"/api/audio/{filename}",
            "path": str(target),
            "extra_info": data.get("extra_info", {}),
            "trace_id": data.get("trace_id"),
        }

    @staticmethod
    def _number(value: Any, low: float, high: float, fallback: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = fallback
        return max(low, min(high, number))

    @classmethod
    def _json_number(cls, value: Any, low: float, high: float, fallback: float) -> int | float:
        number = cls._number(value, low, high, fallback)
        if number.is_integer():
            return int(number)
        return round(number, 2)

    @classmethod
    def _integer(cls, value: Any, low: int, high: int, fallback: int) -> int:
        return int(round(cls._number(value, low, high, fallback)))


class CommandController:
    COMMANDS = {
        "open_url": {
            "name": "打开网页",
            "level": "L1",
            "fields": [{"name": "url", "label": "网页地址", "placeholder": "https://example.com"}],
            "warning": "会在本机默认浏览器打开网页。",
        },
        "search_web": {
            "name": "搜索内容",
            "level": "L1",
            "fields": [{"name": "query", "label": "搜索词", "placeholder": "ESP32-S3 I2S 麦克风"}],
            "warning": "会把搜索词发送给默认搜索引擎。",
        },
        "open_app": {
            "name": "打开应用",
            "level": "L1",
            "fields": [{"name": "app", "label": "应用名", "placeholder": "Calculator"}],
            "warning": "只允许打开白名单里的常见应用。",
        },
        "set_volume": {
            "name": "调整音量",
            "level": "L1",
            "fields": [{"name": "volume", "label": "音量 0-100", "placeholder": "35"}],
            "warning": "会修改本机系统输出音量。",
        },
        "take_screenshot": {
            "name": "保存截图",
            "level": "L2",
            "fields": [],
            "warning": "会保存当前屏幕截图到本地 data/screenshots，截图可能包含屏幕上的隐私内容。",
        },
    }

    SAFE_APPS = {"Calculator", "TextEdit", "Notes", "Safari", "Google Chrome", "Finder", "Preview"}

    @classmethod
    def list(cls) -> list[dict[str, Any]]:
        return [{"id": key, **value} for key, value in cls.COMMANDS.items()]

    @classmethod
    def preview(cls, action: str, args: dict[str, Any]) -> dict[str, Any]:
        spec = cls.COMMANDS.get(action)
        if not spec:
            raise ValueError("不支持的指令")
        summary = cls._summary(action, args)
        return {"action": action, "summary": summary, "level": spec["level"], "warning": spec["warning"]}

    @classmethod
    def run(cls, action: str, args: dict[str, Any], confirm: bool) -> dict[str, Any]:
        if not confirm:
            raise ValueError("执行电脑控制前需要在控制台确认")
        cls.preview(action, args)
        if action == "open_url":
            result = cls._open_url(str(args.get("url", "")))
        elif action == "search_web":
            result = cls._search_web(str(args.get("query", "")))
        elif action == "open_app":
            result = cls._open_app(str(args.get("app", "")))
        elif action == "set_volume":
            result = cls._set_volume(args.get("volume", ""))
        elif action == "take_screenshot":
            result = cls._take_screenshot()
        else:
            raise ValueError("不支持的指令")
        AuditLog.add(action, args, result["message"])
        return result

    @classmethod
    def _summary(cls, action: str, args: dict[str, Any]) -> str:
        if action == "open_url":
            return f"打开网页：{args.get('url', '')}"
        if action == "search_web":
            return f"搜索：{args.get('query', '')}"
        if action == "open_app":
            return f"打开应用：{args.get('app', '')}"
        if action == "set_volume":
            return f"设置系统音量为：{args.get('volume', '')}"
        if action == "take_screenshot":
            return "保存当前屏幕截图到本地"
        return action

    @staticmethod
    def _open_url(url: str) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("只允许打开 http/https 网页地址")
        subprocess.Popen(["open", url.strip()])
        return {"ok": True, "message": f"已打开网页：{url.strip()}"}

    @staticmethod
    def _search_web(query: str) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("搜索词不能为空")
        url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
        subprocess.Popen(["open", url])
        return {"ok": True, "message": f"已打开搜索：{query}"}

    @classmethod
    def _open_app(cls, app: str) -> dict[str, Any]:
        app = app.strip()
        if app not in cls.SAFE_APPS:
            allowed = "、".join(sorted(cls.SAFE_APPS))
            raise ValueError(f"应用不在白名单中。当前允许：{allowed}")
        subprocess.Popen(["open", "-a", app])
        return {"ok": True, "message": f"已尝试打开应用：{app}"}

    @staticmethod
    def _set_volume(volume: Any) -> dict[str, Any]:
        try:
            value = int(volume)
        except (TypeError, ValueError) as exc:
            raise ValueError("音量必须是 0-100 的整数") from exc
        value = max(0, min(100, value))
        subprocess.run(["osascript", "-e", f"set volume output volume {value}"], check=True)
        return {"ok": True, "message": f"已设置系统音量为 {value}"}

    @staticmethod
    def _take_screenshot() -> dict[str, Any]:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"screenshot-{int(time.time())}.png"
        target = SCREENSHOT_DIR / filename
        subprocess.run(["screencapture", "-x", str(target)], check=True)
        return {"ok": True, "message": f"截图已保存：{target}", "path": str(target)}


def handle_chat(payload: dict[str, Any]) -> dict[str, Any]:
    message = str(payload.get("message", "")).strip()
    if not message:
        raise ValueError("消息不能为空")
    api_key = str(payload.get("api_key", "")).strip()
    memories = MemoryStore.relevant(message)
    saved_memory = MemoryStore.maybe_remember(message)
    if saved_memory:
        memories = [saved_memory, *memories]
    wish_created = None
    if any(message.startswith(prefix) for prefix in ["我想", "希望", "心愿：", "心愿:"]):
        title = message.replace("心愿：", "").replace("心愿:", "").strip()
        if len(title) >= 2:
            wish_created = WishStore.add(title=title, reason="从聊天中识别", next_action="写下第一步行动")
    deterministic_reply = deterministic_memory_reply(message, memories)
    if deterministic_reply:
        reply, source = deterministic_reply, "memory"
    else:
        reply, source = ModelAdapter.reply(message, memories, api_key)
    conversation = ConversationStore.add(message, reply, source, memories[:6])
    return {
        "reply": reply,
        "source": source,
        "memory_hits": memories[:5],
        "saved_memory": saved_memory,
        "wish_created": wish_created,
        "conversation": conversation,
        "all_memories": MemoryStore.list()[:20],
    }


def deterministic_memory_reply(message: str, memories: list[dict[str, Any]]) -> str | None:
    text = re.sub(r"\s+", "", message)
    if re.search(r"(我.*(叫什?么|名字|称呼)|你.*(叫我|喊我|称呼我)|该.*(叫我|喊我))", text):
        names = [str(m["content"]).replace("称呼：", "", 1) for m in memories if str(m.get("content", "")).startswith("称呼：")]
        if not names:
            return None
        if len(names) == 1:
            return f"我记得你叫 {names[0]}。"
        return f"我记得你有这些称呼：{'、'.join(names)}。最新一条是 {names[0]}，之前你也让我叫过你 {names[-1]}。"
    return None


class MossHandler(SimpleHTTPRequestHandler):
    server_version = "MossStageOne/0.1"

    def translate_path(self, path: str) -> str:
        parsed = urllib.parse.urlparse(path)
        clean = parsed.path
        if clean == "/":
            return str(WEB_DIR / "index.html")
        candidate = (WEB_DIR / clean.lstrip("/")).resolve()
        if not str(candidate).startswith(str(WEB_DIR.resolve())):
            return str(WEB_DIR / "index.html")
        return str(candidate)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (now_iso(), fmt % args))

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/status":
                payload = {
                    "app": "心愿 Moss 精灵阶段一",
                    "time": now_iso(),
                    "python": platform.python_version(),
                    "db_path": str(DB_PATH),
                    "model": ModelAdapter.status(),
                    "tts": TTSAdapter.status(),
                }
                json_response(self, payload)
            elif parsed.path == "/api/app-version":
                json_response(self, {"version": cache_buster()})
            elif parsed.path == "/api/memories":
                q = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
                json_response(self, {"items": MemoryStore.list(q)})
            elif parsed.path == "/api/conversations":
                json_response(self, {"items": ConversationStore.list()})
            elif parsed.path == "/api/wishes":
                json_response(self, {"items": WishStore.list()})
            elif parsed.path == "/api/commands":
                json_response(self, {"items": CommandController.list()})
            elif parsed.path == "/api/tts/status":
                json_response(self, TTSAdapter.status())
            elif parsed.path.startswith("/api/audio/"):
                self._serve_audio(parsed.path.split("/")[-1])
            elif parsed.path == "/api/audit":
                json_response(self, {"items": AuditLog.list()})
            elif parsed.path == "/api/export":
                json_response(
                    self,
                    {
                        "memories": MemoryStore.list(),
                        "wishes": WishStore.list(),
                        "conversations": ConversationStore.list(500),
                        "audit": AuditLog.list(500),
                    },
                )
            elif parsed.path == "/":
                html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
                version = cache_buster()
                html = html.replace("styles.css", f"styles.css?v={version}")
                html = html.replace("app.js", f"app.js?v={version}")
                data = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            else:
                super().do_GET()
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 500)

    def do_HEAD(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/audio/"):
            self._serve_audio(parsed.path.split("/")[-1], head_only=True)
        else:
            super().do_HEAD()

    def do_POST(self) -> None:
        try:
            payload = read_json(self)
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/chat":
                json_response(self, handle_chat(payload))
            elif parsed.path == "/api/memories":
                item = MemoryStore.add(
                    str(payload.get("content", "")),
                    str(payload.get("kind", "note")),
                    str(payload.get("tags", "")),
                )
                json_response(self, {"item": item}, 201)
            elif parsed.path == "/api/wishes":
                item = WishStore.add(
                    str(payload.get("title", "")),
                    str(payload.get("reason", "")),
                    str(payload.get("next_action", "")),
                )
                json_response(self, {"item": item}, 201)
            elif parsed.path.startswith("/api/wishes/") and parsed.path.endswith("/progress"):
                wish_id = int(parsed.path.split("/")[3])
                item = WishStore.progress(wish_id, int(payload.get("delta", 10)), str(payload.get("note", "")))
                json_response(self, {"item": item})
            elif parsed.path == "/api/commands/preview":
                result = CommandController.preview(str(payload.get("action", "")), dict(payload.get("args", {})))
                json_response(self, result)
            elif parsed.path == "/api/commands/run":
                result = CommandController.run(
                    str(payload.get("action", "")),
                    dict(payload.get("args", {})),
                    bool(payload.get("confirm", False)),
                )
                json_response(self, result)
            elif parsed.path == "/api/tts/synthesize":
                result = TTSAdapter.synthesize(payload)
                json_response(self, result, 201)
            elif parsed.path == "/api/tts/speak":
                result = TTSAdapter.synthesize(payload)
                json_response(self, result, 201)
            else:
                json_response(self, {"error": "未知 POST 接口"}, 404)
        except ValueError as exc:
            json_response(self, {"error": str(exc)}, 400)
        except subprocess.CalledProcessError as exc:
            json_response(self, {"error": f"系统指令执行失败：{exc}"}, 500)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 500)

    def _serve_audio(self, filename: str, head_only: bool = False) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
            json_response(self, {"error": "非法音频文件名"}, 400)
            return
        target = (AUDIO_DIR / filename).resolve()
        if not str(target).startswith(str(AUDIO_DIR.resolve())) or not target.exists():
            json_response(self, {"error": "音频不存在"}, 404)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def do_DELETE(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.startswith("/api/memories/"):
                memory_id = int(parsed.path.split("/")[-1])
                json_response(self, {"ok": MemoryStore.delete(memory_id)})
            else:
                json_response(self, {"error": "未知 DELETE 接口"}, 404)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 500)


def main() -> None:
    load_env_file()
    init_db()
    port = int(os.getenv("MOSS_PORT", "8787"))
    server = ThreadingHTTPServer(("127.0.0.1", port), MossHandler)
    print(f"心愿 Moss 精灵阶段一已启动：http://127.0.0.1:{port}")
    print(f"数据库：{DB_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
