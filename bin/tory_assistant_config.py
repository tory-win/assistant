#!/usr/bin/env python3
"""Assistant profile loader for Tory assistant scripts.

Default profile keeps the existing Tory paths untouched. Extra assistants can
run with TORY_ASSISTANT_ID=<id> and ~/.torymemory/assistants/<id>.json.
"""
import copy
import json
import os

HOME = os.path.expanduser("~")
ROOT = os.path.join(HOME, ".torymemory")
ASSISTANTS_DIR = os.path.join(ROOT, "assistants")


BASE_DEFAULT = {
    "id": "tory",
    "assistant_name": "토리",
    "assistant_slug": "tory",
    "company_name": "ASWEMAKE",
    "boss_name": "오승현",
    "boss_title": "전략본부장",
    "boss_user_id": "U03EQFWTD61",
    "assistant_channel_id": "C0B997W7KGS",
    "assistant_channel_name": "승현-비서",
    "slack_username": "토리",
    "slack_icon_emoji": ":card_index_dividers:",
    "env_file": os.path.join(HOME, ".hermes", ".env"),
    "base_dir": ROOT,
    "state_dir": os.path.join(ROOT, "state"),
    "slack_config_file": os.path.join(ROOT, "slack-config.json"),
    "outbox_dir": os.path.join(ROOT, "outbox"),
    "deep_briefs_dir": os.path.join(ROOT, "deep-briefs"),
    "workdir": os.path.join(ROOT, "claude-workdir"),
    "feed_dirs": {
        "slack": os.path.join(ROOT, "feeds", "slack"),
        "google": os.path.join(ROOT, "feeds", "google"),
        "notion": os.path.join(ROOT, "feeds", "notion"),
        "recordings": os.path.join(ROOT, "feeds", "recordings"),
    },
    "enabled_sources": ["slack", "gmail", "calendar", "drive", "notion", "memory", "local", "recordings"],
    "enabled_actions": ["slack", "gmail", "calendar", "notion"],
    "notion_default_parent_page_id": "17eea3ff-6c9b-815a-bf1e-ca6ccbbe5528",  # 업무
    "notion_task_db_id": "17eea3ff-6c9b-8102-b0cd-dd7ee2c52ee7",
    "notion_task_owner_id": "181ea3ff-6c9b-802c-aaa1-d8ffba796d18",
    "notion_task_owner_name": "오승현",
    "memory_user_id": "awm_confidential",
}


def _deep_update(base, override):
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def _expand_paths(profile):
    for key in ("env_file", "base_dir", "state_dir", "slack_config_file", "outbox_dir",
                "deep_briefs_dir", "workdir"):
        if profile.get(key):
            profile[key] = os.path.expanduser(profile[key])
    feeds = profile.get("feed_dirs") or {}
    for key, value in list(feeds.items()):
        feeds[key] = os.path.expanduser(value)
    return profile


def _generated_default(assistant_id):
    prof = copy.deepcopy(BASE_DEFAULT)
    assistant_id = assistant_id or "tory"
    prof["id"] = assistant_id
    if assistant_id != "tory":
        base = os.path.join(ASSISTANTS_DIR, assistant_id)
        prof.update({
            "assistant_slug": assistant_id,
            "assistant_name": assistant_id,
            "slack_username": assistant_id,
            "boss_name": assistant_id,
            "boss_title": "",
            "boss_user_id": "",
            "assistant_channel_id": "",
            "assistant_channel_name": assistant_id + "-assistant",
            "base_dir": base,
            "state_dir": os.path.join(base, "state"),
            "slack_config_file": os.path.join(base, "slack-config.json"),
            "env_file": os.path.join(base, ".env"),
            "outbox_dir": os.path.join(base, "outbox"),
            "deep_briefs_dir": os.path.join(base, "deep-briefs"),
            "workdir": os.path.join(base, "claude-workdir"),
            "feed_dirs": {
                "slack": os.path.join(base, "feeds", "slack"),
                "google": os.path.join(base, "feeds", "google"),
                "notion": os.path.join(base, "feeds", "notion"),
                "recordings": os.path.join(base, "feeds", "recordings"),
            },
            "notion_task_owner_id": "",
            "notion_task_owner_name": "",
            "notion_default_parent_page_id": "",
        })
    return prof


def load_profile():
    assistant_id = os.environ.get("TORY_ASSISTANT_ID", "tory").strip() or "tory"
    path = os.environ.get("TORY_ASSISTANT_CONFIG", "").strip()
    if not path:
        path = os.path.join(ASSISTANTS_DIR, assistant_id + ".json")
    profile = _generated_default(assistant_id)
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            _deep_update(profile, loaded)
    except FileNotFoundError:
        pass
    return _expand_paths(profile)


def ensure_profile_dirs(profile):
    paths = [profile.get("state_dir"), profile.get("outbox_dir"), profile.get("deep_briefs_dir"),
             profile.get("workdir")]
    paths.extend((profile.get("feed_dirs") or {}).values())
    for path in paths:
        if path:
            os.makedirs(path, exist_ok=True)
