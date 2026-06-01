#!/usr/bin/env python3
"""Build Shields-compatible translation contribution statistics.

The profile README cannot sum GitHub PR fields directly from a badge URL, so this
script searches merged translation-related PRs, fetches their diff metadata, and
writes small JSON files that Shields can render as endpoint badges.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


USER = os.environ.get("GITHUB_PROFILE_USER", "sampple-korea")
ROOT = Path(__file__).resolve().parents[1]
METRICS_DIR = ROOT / "metrics"
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
API_VERSION = "2022-11-28"
WORD_RE = re.compile(r"[\w가-힣]+", re.UNICODE)
TEXT_SIGNAL_RE = re.compile(
    r"\b(korean|ko[-_ ]?kr|translation|translations|locali[sz]ation|"
    r"i18n|l10n|locale|language)\b|한국어|한글",
    re.IGNORECASE,
)
STRONG_LOCALE_PATH_RE = re.compile(
    r"(^|/)"
    r"("
    r"values-ko(?:-rkr)?|"
    r"ko|"
    r"ko[-_]kr|"
    r"ko-rkr"
    r")"
    r"(/|$|\.)|"
    r"(^|/)(?:intl_|messages_|strings[-_])?ko(?:[-_]kr)?"
    r"\.(?:arb|json|ya?ml|po|pot|ts|tsx|js|jsx|xml|xlf|xliff|properties|strings)$|"
    r"(^|/)readme[._-]ko(?:[-_]kr)?\.md$",
    re.IGNORECASE,
)
WEAK_TRANSLATION_PATH_RE = re.compile(
    r"(^|/)(?:i18n|l10n|locale|locales|lang|langs|language|languages|"
    r"translation|translations|crowdin|weblate)(/|$)|"
    r"locales_config\.xml$|LocaleUtils\.(?:kt|java|swift|ts|tsx|js|jsx)$",
    re.IGNORECASE,
)


def api_get(url: str) -> Any:
    text = api_get_text(url, "application/vnd.github+json")
    return json.loads(text)


def api_get_text(url: str, accept: str) -> str:
    headers = {
        "Accept": accept,
        "User-Agent": "sampple-korea-profile-translation-stats",
        "X-GitHub-Api-Version": API_VERSION,
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"

    for attempt in range(4):
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            if exc.code in {403, 429, 500, 502, 503, 504} and attempt < 3:
                time.sleep(2**attempt)
                continue
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API request failed: {exc.code} {url}\n{body}") from exc


def paged(url: str) -> list[Any]:
    items: list[Any] = []
    page = 1
    while True:
        separator = "&" if "?" in url else "?"
        data = api_get(f"{url}{separator}per_page=100&page={page}")
        page_items = data["items"] if isinstance(data, dict) and "items" in data else data
        if not page_items:
            break
        items.extend(page_items)
        if len(page_items) < 100:
            break
        page += 1
    return items


def search_merged_prs() -> dict[str, dict[str, Any]]:
    pulls: dict[str, dict[str, Any]] = {}
    query = f"author:{USER} is:pr is:merged"
    url = "https://api.github.com/search/issues?" + urlencode(
        {"q": query, "sort": "updated", "order": "desc"}
    )
    for item in paged(url):
        pull = item.get("pull_request") or {}
        pull_url = pull.get("url")
        if not pull_url:
            continue
        pulls[pull_url] = {
            "title": item.get("title", ""),
            "body": item.get("body") or "",
            "html_url": item.get("html_url", ""),
        }

    return pulls


def count_patch_words(patch: str | None) -> int:
    if not patch:
        return 0

    words = 0
    for line in patch.splitlines():
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith(("+", "-")):
            words += len(WORD_RE.findall(line[1:]))
    return words


def classify_translation_pr(
    pull: dict[str, Any], files: list[dict[str, Any]], seed: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    title = seed.get("title") or pull.get("title") or ""
    body = seed.get("body") or pull.get("body") or ""
    text_signal = TEXT_SIGNAL_RE.search(f"{title}\n{body}") is not None
    filenames = [file.get("filename") or "" for file in files]
    strong_files = [name for name in filenames if STRONG_LOCALE_PATH_RE.search(name)]
    weak_files = [name for name in filenames if WEAK_TRANSLATION_PATH_RE.search(name)]
    is_translation = bool(strong_files or text_signal)

    signals: list[str] = []
    if text_signal:
        signals.append("title/body")
    if strong_files:
        signals.append("ko-locale-path")
    if weak_files:
        signals.append("translation-support-path")

    return is_translation, {
        "signals": signals,
        "ko_locale_files": strong_files,
        "translation_support_files": weak_files,
    }


def fetch_pull_stats(pull_url: str, seed: dict[str, Any]) -> dict[str, Any]:
    pull = api_get(pull_url)
    files = paged(f"{pull_url}/files?")
    diff_words = sum(count_patch_words(file.get("patch")) for file in files)
    diff_url = pull.get("diff_url")
    if diff_url:
        try:
            diff_words = count_patch_words(
                api_get_text(diff_url, "application/vnd.github.v3.diff")
            )
        except RuntimeError:
            pass
    is_translation, classification = classify_translation_pr(pull, files, seed)

    additions = int(pull.get("additions") or 0)
    deletions = int(pull.get("deletions") or 0)
    return {
        "repo": pull.get("base", {}).get("repo", {}).get("full_name"),
        "number": pull.get("number"),
        "title": seed.get("title") or pull.get("title"),
        "url": pull.get("html_url") or seed.get("html_url"),
        "merged_at": pull.get("merged_at"),
        "additions": additions,
        "deletions": deletions,
        "changed_lines": additions + deletions,
        "changed_files": int(pull.get("changed_files") or 0),
        "diff_words": diff_words,
        "is_public": not bool(pull.get("base", {}).get("repo", {}).get("private")),
        "is_translation": is_translation,
        **classification,
    }


def compact_number(value: int) -> str:
    if value >= 1_000_000:
        compact = f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{compact}M"
    if value >= 1_000:
        compact = f"{value / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{compact}k"
    return str(value)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def with_stable_updated_at(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    existing_payload: dict[str, Any] | None = None
    if path.exists():
        try:
            existing_payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing_payload = None

    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if existing_payload:
        existing_without_timestamp = dict(existing_payload)
        existing_without_timestamp.pop("updated_at", None)
        if existing_without_timestamp == payload:
            updated_at = existing_payload.get("updated_at") or updated_at

    return {"updated_at": updated_at, **payload}


def main() -> int:
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    pulls = search_merged_prs()
    all_pull_stats = [fetch_pull_stats(url, seed) for url, seed in sorted(pulls.items())]
    pull_stats = [
        item for item in all_pull_stats if item["is_translation"] and item["is_public"]
    ]
    pull_stats.sort(key=lambda item: item.get("merged_at") or "", reverse=True)

    additions = sum(item["additions"] for item in pull_stats)
    deletions = sum(item["deletions"] for item in pull_stats)
    changed_lines = additions + deletions
    diff_words = sum(item["diff_words"] for item in pull_stats)
    changed_files = sum(item["changed_files"] for item in pull_stats)

    stats = with_stable_updated_at(
        METRICS_DIR / "translation-stats.json",
        {
            "user": USER,
            "classification": {
                "mode": "public merged PRs by author, then classify by Korean locale paths and translation text signals",
                "text_signal_pattern": TEXT_SIGNAL_RE.pattern,
                "strong_locale_path_pattern": STRONG_LOCALE_PATH_RE.pattern,
                "weak_translation_path_pattern": WEAK_TRANSLATION_PATH_RE.pattern,
            },
            "merged_translation_prs": len(pull_stats),
            "additions": additions,
            "deletions": deletions,
            "changed_lines": changed_lines,
            "changed_files": changed_files,
            "diff_words": diff_words,
            "pulls": pull_stats,
        },
    )

    write_json(METRICS_DIR / "translation-stats.json", stats)
    write_json(
        METRICS_DIR / "translation-lines-badge.json",
        {
            "schemaVersion": 1,
            "label": "translation changes",
            "message": f"{compact_number(changed_lines)} lines",
            "color": "2ea44f",
            "namedLogo": "github",
            "style": "flat-square",
        },
    )
    write_json(
        METRICS_DIR / "translation-words-badge.json",
        {
            "schemaVersion": 1,
            "label": "translation diff words",
            "message": f"{compact_number(diff_words)} words",
            "color": "0969da",
            "namedLogo": "github",
            "style": "flat-square",
        },
    )

    print(
        f"Updated translation stats: {len(pull_stats)} PRs, "
        f"{changed_lines} changed lines, {diff_words} diff words."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
