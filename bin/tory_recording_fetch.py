#!/usr/bin/env python3
"""Sync meeting-note-studio notes into Tory assistant local state.

This keeps raw transcripts out of Hermes memory feeds. Tory can read them as
local evidence through recording_read, while curated memory remains distilled.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    KST = timezone.utc

try:
    import tory_assistant_config as assistant_config
    PROFILE = assistant_config.load_profile()
except Exception:
    PROFILE = {}

HOME = os.path.expanduser("~")
BASE = PROFILE.get("base_dir") or os.path.join(HOME, ".torymemory")
STATE_DIR = PROFILE.get("state_dir") or os.path.join(BASE, "state")
OUT_DIR = os.path.join(STATE_DIR, "recordings")
INDEX_FILE = os.path.join(STATE_DIR, "recordings-index.json")
CURSOR_FILE = os.path.join(STATE_DIR, "recording-cursor.json")


def _source_candidates():
    raw = os.environ.get("TORY_MEETING_NOTES_FILE", "")
    items = [x.strip() for x in raw.split(",") if x.strip()]
    if PROFILE.get("meeting_notes_file"):
        items.append(PROFILE["meeting_notes_file"])
    items.extend([
        os.path.join(BASE, "meeting-note-studio-data", "notes.json"),
        "/root/.torymemory/meeting-note-studio-data/notes.json",
        "/Users/tory/Downloads/dev/meeting-note-studio/data/notes.json",
        os.path.expanduser("~/Downloads/dev/meeting-note-studio/data/notes.json"),
    ])
    out = []
    seen = set()
    for item in items:
        p = os.path.expanduser(item)
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _load_notes():
    tried = []
    for path in _source_candidates():
        tried.append(path)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("notes file is not a list: %s" % path)
        return path, data
    raise FileNotFoundError("meeting notes file not found. tried: " + ", ".join(tried))


def _safe_name(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value or "").strip("-")
    return value[:120] or "note"


def _atomic_write(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _fmt_time(seconds):
    try:
        seconds = max(0, int(float(seconds)))
    except Exception:
        seconds = 0
    return "%02d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _line(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "title", "task", "item", "content"):
            if value.get(key):
                return str(value.get(key)).strip()
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def _section(title, rows):
    rows = [_line(x) for x in _as_list(rows)]
    rows = [x for x in rows if x]
    if not rows:
        return []
    return ["## " + title, ""] + ["- " + x for x in rows] + [""]


def _topics(topics):
    out = []
    for topic in _as_list(topics):
        if isinstance(topic, dict):
            title = topic.get("title") or "주제"
            out.append("### " + str(title).strip())
            for bullet in _as_list(topic.get("bullets")):
                b = _line(bullet)
                if b:
                    out.append("- " + b)
            out.append("")
        else:
            t = _line(topic)
            if t:
                out.extend(["### 주제", "- " + t, ""])
    return ["## 주요 주제", ""] + out if out else []


def _segments_text(note):
    if isinstance(note.get("transcript"), str) and note["transcript"].strip():
        return note["transcript"].strip()
    rows = []
    for seg in note.get("segments") or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = _fmt_time(seg.get("start", 0))
        end = _fmt_time(seg.get("end", seg.get("start", 0)))
        speaker = seg.get("speaker") or "화자"
        rows.append("[%s-%s] %s: %s" % (start, end, speaker, text))
    return "\n".join(rows)


def _normalize_note(note, source_path):
    minutes = note.get("minutes") or {}
    note_id = str(note.get("id") or note.get("recordingId") or "")
    if not note_id:
        return None
    transcript = _segments_text(note)
    one_line = (minutes.get("oneLine") or "").strip()
    final_markdown = (minutes.get("finalMarkdown") or "").strip()
    summary = _as_list(minutes.get("summary"))
    topics = _as_list(minutes.get("topics"))
    has_content = bool(transcript or one_line or final_markdown or summary or topics)
    if not has_content:
        return None
    title = (note.get("title") or note.get("sourceName") or note_id).strip()
    created = note.get("createdAt") or ""
    updated = note.get("updatedAt") or created
    normalized = {
        "id": note_id,
        "recording_id": note.get("recordingId") or "",
        "title": title,
        "status": note.get("status") or "",
        "source_name": note.get("sourceName") or "",
        "source_notes_file": source_path,
        "created_at": created,
        "updated_at": updated,
        "duration_seconds": note.get("durationSeconds") or 0,
        "participants": _as_list(note.get("participants")),
        "one_line": one_line,
        "summary": summary,
        "topics": topics,
        "decisions": _as_list(minutes.get("decisions")),
        "action_items": _as_list(minutes.get("actionItems")),
        "risks": _as_list(minutes.get("risks")),
        "questions": _as_list(minutes.get("questions")),
        "keywords": _as_list(minutes.get("keywords")),
        "final_markdown": final_markdown,
        "transcript": transcript,
        "audio_url": note.get("audioUrl") or "",
        "audio_path": note.get("audioPath") or "",
    }
    search_parts = [
        title, normalized["source_name"], one_line, final_markdown,
        " ".join(_line(x) for x in summary),
        " ".join(_line(x) for x in normalized["decisions"]),
        " ".join(_line(x) for x in normalized["action_items"]),
        " ".join(_line(x) for x in normalized["keywords"]),
        transcript,
    ]
    normalized["search_text"] = "\n".join(x for x in search_parts if x)[:250000]
    return normalized


def _render_markdown(note):
    lines = [
        "# " + note["title"],
        "",
        "- note_id: `%s`" % note["id"],
        "- status: %s" % (note.get("status") or "?"),
        "- created_at: %s" % (note.get("created_at") or "?"),
        "- updated_at: %s" % (note.get("updated_at") or "?"),
        "- duration: %s" % _fmt_time(note.get("duration_seconds") or 0),
    ]
    if note.get("source_name"):
        lines.append("- source: %s" % note["source_name"])
    lines.append("")
    if note.get("one_line"):
        lines.extend(["## 한 줄 요약", "", note["one_line"], ""])
    lines.extend(_section("요약", note.get("summary")))
    lines.extend(_topics(note.get("topics")))
    lines.extend(_section("결정사항", note.get("decisions")))
    lines.extend(_section("할 일", note.get("action_items")))
    lines.extend(_section("리스크", note.get("risks")))
    lines.extend(_section("질문", note.get("questions")))
    lines.extend(_section("키워드", note.get("keywords")))
    if note.get("final_markdown"):
        lines.extend(["## 미팅노트 원문", "", note["final_markdown"], ""])
    if note.get("transcript"):
        lines.extend(["## 녹취록", "", note["transcript"], ""])
    return "\n".join(lines).rstrip() + "\n"


def _iso_now():
    return datetime.now(KST).isoformat(timespec="seconds")


def sync(dry_run=False):
    source, raw_notes = _load_notes()
    normalized = []
    for note in raw_notes:
        if isinstance(note, dict):
            n = _normalize_note(note, source)
            if n:
                normalized.append(n)
    normalized.sort(key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)

    index = {
        "updated_at": _iso_now(),
        "source_notes_file": source,
        "count": len(normalized),
        "notes": [],
    }
    for note in normalized:
        note_file = os.path.join(OUT_DIR, _safe_name(note["id"]) + ".json")
        md_file = os.path.join(OUT_DIR, _safe_name(note["id"]) + ".md")
        entry = {
            "id": note["id"],
            "title": note["title"],
            "status": note.get("status"),
            "created_at": note.get("created_at"),
            "updated_at": note.get("updated_at"),
            "duration_seconds": note.get("duration_seconds"),
            "one_line": note.get("one_line"),
            "keywords": note.get("keywords")[:12],
            "json_path": note_file,
            "markdown_path": md_file,
        }
        index["notes"].append(entry)
        if not dry_run:
            _atomic_write(note_file, json.dumps(note, ensure_ascii=False, indent=1))
            _atomic_write(md_file, _render_markdown(note))

    if not dry_run:
        _atomic_write(INDEX_FILE, json.dumps(index, ensure_ascii=False, indent=1))
        cursor = {
            "updated_at": _iso_now(),
            "source_notes_file": source,
            "source_mtime": os.path.getmtime(source),
            "count": len(normalized),
        }
        _atomic_write(CURSOR_FILE, json.dumps(cursor, ensure_ascii=False, indent=1))
    return {"ok": True, "source": source, "notes": len(raw_notes), "synced": len(normalized), "dry_run": dry_run}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    enabled = set(PROFILE.get("enabled_sources") or
                  ["slack", "gmail", "calendar", "drive", "notion", "memory", "local", "recordings"])
    if "recordings" not in enabled:
        print(json.dumps({"ok": True, "skip": "recordings_source_disabled"}, ensure_ascii=False))
        return
    try:
        out = sync(dry_run=args.dry_run)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
