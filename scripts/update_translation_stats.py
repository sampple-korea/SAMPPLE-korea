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
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


USER = os.environ.get("GITHUB_PROFILE_USER", "sampple-korea")
ROOT = Path(__file__).resolve().parents[1]
METRICS_DIR = ROOT / "metrics"
README_PATH = ROOT / "README.md"
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
API_VERSION = "2022-11-28"
XML_WORD_RE = re.compile(r"[A-Za-z가-힣]+(?:[-'][A-Za-z가-힣]+)*|\d+(?:[.,]\d+)*")
IGNORED_XML_TOKEN_RE = re.compile(
    r"https?://\S+|"
    r"%(\d+\$)?[-#+ 0,(]*\d*(?:\.\d+)?[A-Za-z%]|"
    r"\\[nrt\"']|"
    r"@\w+/[\w.]+|"
    r"\{[^{}]*\}|\$\{[^{}]*\}"
)
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
XML_LOCALE_PATH_RE = re.compile(
    r"(^|/)values-ko(?:-rkr)?/.*\.xml$|"
    r"(^|/)(?:intl_|messages_|strings[-_])?ko(?:[-_]kr)?\.xml$",
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


def api_get_text_optional(url: str, accept: str = "text/plain") -> str | None:
    try:
        return api_get_text(url, accept)
    except RuntimeError as exc:
        if "failed: 404" in str(exc):
            return None
        raise


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


def raw_github_url(repo: str, ref: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{ref}/{quote(path, safe='/')}"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def element_text(element: ET.Element) -> str:
    return "".join(element.itertext())


def count_xml_words(text: str) -> int:
    cleaned = IGNORED_XML_TOKEN_RE.sub(" ", text)
    cleaned = cleaned.replace("\\n", " ").replace("\\t", " ")
    return len(XML_WORD_RE.findall(cleaned))


def parse_android_xml_entries(content: str) -> dict[str, str]:
    root = ET.fromstring(content.encode("utf-8"))
    entries: dict[str, str] = {}

    for child in root:
        tag = local_name(child.tag)
        name = child.attrib.get("name")
        if not name or child.attrib.get("translatable") == "false":
            continue

        if tag == "string":
            entries[f"string:{name}"] = element_text(child)
            continue

        if tag in {"array", "string-array", "integer-array"}:
            index = 0
            for item in child:
                if local_name(item.tag) != "item":
                    continue
                entries[f"{tag}:{name}:{index}"] = element_text(item)
                index += 1
            continue

        if tag == "plurals":
            for item in child:
                if local_name(item.tag) != "item":
                    continue
                quantity = item.attrib.get("quantity", str(len(entries)))
                entries[f"plurals:{name}:{quantity}"] = element_text(item)
            continue

        if tag == "item":
            item_type = child.attrib.get("type", "item")
            entries[f"{item_type}:{name}"] = element_text(child)

    return entries


def changed_xml_stats(
    pull: dict[str, Any], files: list[dict[str, Any]], ko_locale_files: list[str]
) -> tuple[int, int, list[dict[str, Any]]]:
    base_repo = pull.get("base", {}).get("repo", {}).get("full_name")
    base_sha = pull.get("base", {}).get("sha")
    total_words = 0
    total_resources = 0
    xml_files: list[dict[str, Any]] = []

    for file in files:
        filename = file.get("filename") or ""
        if filename not in ko_locale_files or not XML_LOCALE_PATH_RE.search(filename):
            continue
        if file.get("status") == "removed":
            continue

        head_text = api_get_text_optional(file.get("raw_url") or "")
        if not head_text:
            continue

        base_text = None
        if base_repo and base_sha:
            base_text = api_get_text_optional(raw_github_url(base_repo, base_sha, filename))

        after_entries = parse_android_xml_entries(head_text)
        before_entries = parse_android_xml_entries(base_text) if base_text else {}
        changed_keys = [
            key for key, value in after_entries.items() if before_entries.get(key) != value
        ]
        file_words = sum(count_xml_words(after_entries[key]) for key in changed_keys)

        total_words += file_words
        total_resources += len(changed_keys)
        xml_files.append(
            {
                "filename": filename,
                "changed_resources": len(changed_keys),
                "xml_words": file_words,
            }
        )

    return total_words, total_resources, xml_files


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
    is_translation, classification = classify_translation_pr(pull, files, seed)
    is_public = not bool(pull.get("base", {}).get("repo", {}).get("private"))
    xml_words, xml_resources, xml_files = (0, 0, [])
    if is_public and is_translation:
        xml_words, xml_resources, xml_files = changed_xml_stats(
            pull, files, classification["ko_locale_files"]
        )

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
        "xml_words": xml_words,
        "xml_changed_resources": xml_resources,
        "xml_files": xml_files,
        "is_public": is_public,
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


def format_int(value: int) -> str:
    return f"{value:,}"


def translation_table(stats: dict[str, Any]) -> str:
    rows = [
        "| Project | XML words | Resources | PR |",
        "| --- | ---: | ---: | --- |",
    ]
    pulls = sorted(
        stats["pulls"],
        key=lambda item: (item["xml_words"], item["xml_changed_resources"]),
        reverse=True,
    )
    for pull in pulls:
        project = pull["repo"].split("/", 1)[-1]
        rows.append(
            f"| {project} | {format_int(pull['xml_words'])} | "
            f"{format_int(pull['xml_changed_resources'])} | "
            f"[#{pull['number']}]({pull['url']}) |"
        )
    return "\n".join(rows)


def update_readme_table(stats: dict[str, Any]) -> None:
    if not README_PATH.exists():
        return

    start = "<!-- translation-stats:start -->"
    end = "<!-- translation-stats:end -->"
    block = f"{start}\n{translation_table(stats)}\n{end}"
    content = README_PATH.read_text(encoding="utf-8")

    if start in content and end in content:
        before, rest = content.split(start, 1)
        _, after = rest.split(end, 1)
        new_content = before.rstrip() + "\n\n" + block + after
    else:
        new_content = content.rstrip() + "\n\n" + block + "\n"

    README_PATH.write_text(new_content, encoding="utf-8")


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
    changed_files = sum(item["changed_files"] for item in pull_stats)
    xml_words = sum(item["xml_words"] for item in pull_stats)
    xml_changed_resources = sum(item["xml_changed_resources"] for item in pull_stats)
    xml_files = sum(len(item["xml_files"]) for item in pull_stats)

    stats = with_stable_updated_at(
        METRICS_DIR / "translation-stats.json",
        {
            "user": USER,
            "classification": {
                "mode": "public merged PRs by author, then classify by Korean locale paths and count changed Android XML resource text",
                "text_signal_pattern": TEXT_SIGNAL_RE.pattern,
                "strong_locale_path_pattern": STRONG_LOCALE_PATH_RE.pattern,
                "weak_translation_path_pattern": WEAK_TRANSLATION_PATH_RE.pattern,
            },
            "merged_translation_prs": len(pull_stats),
            "additions": additions,
            "deletions": deletions,
            "changed_lines": changed_lines,
            "changed_files": changed_files,
            "xml_words": xml_words,
            "xml_changed_resources": xml_changed_resources,
            "xml_files": xml_files,
            "pulls": pull_stats,
        },
    )

    write_json(METRICS_DIR / "translation-stats.json", stats)
    update_readme_table(stats)
    write_json(
        METRICS_DIR / "translation-xml-words-badge.json",
        {
            "schemaVersion": 1,
            "label": "translated XML",
            "message": f"{compact_number(xml_words)} words",
            "color": "0969da",
            "namedLogo": "github",
            "style": "flat-square",
        },
    )

    print(
        f"Updated translation stats: {len(pull_stats)} PRs, "
        f"{xml_words} XML words across {xml_changed_resources} resources."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
