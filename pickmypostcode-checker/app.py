#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import os
import re
import signal
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


DEFAULT_URL_TEMPLATE = "https://pickmypostcode.com/api/index.php/entry/current/{entry_id}"
DEFAULT_MATCH_TEXT = "No results found"
DEFAULT_ENTRY_ID = "27079"
DEFAULT_SURVEY_URL = "https://pickmypostcode.com/survey-draw/"
DEFAULT_SURVEY_ANSWERS_JSON = json.dumps({"radio-1": "neither"})
DEFAULT_HTTP_PORT = 8080
DEFAULT_REQUEST_TIMEOUT = 20
PUSHOVER_ENDPOINT = "https://api.pushover.net/1/messages.json"
STATE_PATH = Path(os.environ.get("STATE_PATH", "/data/state.json"))


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def normalize_postcode(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def pretty_postcode(value: str) -> str:
    value = re.sub(r"[^A-Z0-9]", "", value.upper())
    if len(value) <= 3:
        return value
    return f"{value[:-3]} {value[-3:]}"


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            text = " ".join(data.split())
            if text:
                self._parts.append(text)

    def text(self) -> str:
        return " ".join(self._parts)


def html_to_text(document: str) -> str:
    parser = TextExtractor()
    parser.feed(document)
    parser.close()
    return html.unescape(parser.text())


def parse_check_time(value: str) -> tuple[int, int]:
    value = value.strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", value)
    if not match:
        raise SystemExit("CHECK_TIME must be in HH:MM or HH:MM:SS format")

    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or "0")
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise SystemExit("CHECK_TIME is out of range")
    return hour, minute, second


def local_now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def next_run_at(now: datetime, hour: int, minute: int, second: int) -> datetime:
    candidate = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def sleep_until(target: datetime, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        remaining = (target - datetime.now(target.tzinfo)).total_seconds()
        if remaining <= 0:
            return
        stop_event.wait(timeout=min(remaining, 60))


@dataclass
class Config:
    postcode: str
    check_time: str
    timezone: str
    check_url_template: str
    entry_id: str
    match_text: str
    request_timeout: int
    http_port: int
    state_path: Path
    pushover_app_token: str
    pushover_user_key: str
    pushover_device: str
    pushover_sound: str
    pushover_title: str
    pushover_url: str
    pushover_url_title: str
    survey_url: str
    survey_answers: dict[str, str]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def schedule(self) -> tuple[int, int, int]:
        return parse_check_time(self.check_time)


@dataclass
class CheckSnapshot:
    checked_at: str
    url: str
    postcode: str
    pretty_postcode: str
    found: bool
    matched_text: str
    title: str
    excerpt: str
    status: str
    http_status: int | None
    next_check_at: str | None
    error: str | None = None


@dataclass
class SurveySnapshot:
    attempted_at: str
    url: str
    answers: dict[str, str]
    submitted_url: str
    title: str
    excerpt: str
    status: str
    http_status: int | None
    error: str | None = None


class AppState:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.lock = threading.Lock()
        self.snapshot: CheckSnapshot | None = None
        self.survey_snapshot: SurveySnapshot | None = None
        self.last_raw_body: str = ""
        self.last_notification_signature: str = ""
        self.last_notification_at: str = ""
        self.last_notification_error: str = ""
        self.last_survey_raw_body: str = ""
        self.last_survey_signature: str = ""
        self.last_survey_at: str = ""
        self.last_survey_error: str = ""
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.config.state_path.read_text())
        except FileNotFoundError:
            return
        except Exception:
            return

        try:
            self.snapshot = CheckSnapshot(**data["snapshot"])
            survey_snapshot = data.get("survey_snapshot")
            self.survey_snapshot = SurveySnapshot(**survey_snapshot) if survey_snapshot else None
            self.last_raw_body = data.get("last_raw_body", "")
            self.last_notification_signature = data.get("last_notification_signature", "")
            self.last_notification_at = data.get("last_notification_at", "")
            self.last_notification_error = data.get("last_notification_error", "")
            self.last_survey_raw_body = data.get("last_survey_raw_body", "")
            self.last_survey_signature = data.get("last_survey_signature", "")
            self.last_survey_at = data.get("last_survey_at", "")
            self.last_survey_error = data.get("last_survey_error", "")
        except Exception:
            self.snapshot = None
            self.survey_snapshot = None
            self.last_raw_body = ""
            self.last_notification_signature = ""
            self.last_notification_at = ""
            self.last_notification_error = ""
            self.last_survey_raw_body = ""
            self.last_survey_signature = ""
            self.last_survey_at = ""
            self.last_survey_error = ""

    def save(self) -> None:
        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "snapshot": asdict(self.snapshot) if self.snapshot else None,
            "survey_snapshot": asdict(self.survey_snapshot) if self.survey_snapshot else None,
            "last_raw_body": self.last_raw_body[-20000:],
            "last_notification_signature": self.last_notification_signature,
            "last_notification_at": self.last_notification_at,
            "last_notification_error": self.last_notification_error,
            "last_survey_raw_body": self.last_survey_raw_body[-20000:],
            "last_survey_signature": self.last_survey_signature,
            "last_survey_at": self.last_survey_at,
            "last_survey_error": self.last_survey_error,
        }
        tmp = self.config.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(self.config.state_path)

    def update(self, snapshot: CheckSnapshot, raw_body: str) -> None:
        with self.lock:
            self.snapshot = snapshot
            self.last_raw_body = raw_body
            self.save()

    def update_survey(
        self,
        snapshot: SurveySnapshot,
        raw_body: str,
        *,
        successful: bool = False,
    ) -> None:
        with self.lock:
            self.survey_snapshot = snapshot
            if successful:
                self.last_survey_signature = survey_signature(snapshot)
                self.last_survey_at = snapshot.attempted_at
                self.last_survey_error = ""
            if raw_body:
                self.last_survey_raw_body = raw_body
            self.save()

    def current(self) -> CheckSnapshot | None:
        with self.lock:
            return self.snapshot

    def current_survey(self) -> SurveySnapshot | None:
        with self.lock:
            return self.survey_snapshot


def build_check_url(template: str, postcode: str, entry_id: str) -> str:
    url = template.replace("{entry_id}", quote_plus(entry_id))
    encoded = quote_plus(pretty_postcode(postcode))
    if "{postcode_raw}" in template:
        url = url.replace("{postcode_raw}", quote_plus(postcode))
    return url.replace("{postcode}", encoded)


def build_survey_url(base_url: str, answers: dict[str, str]) -> str:
    parts = urlsplit(base_url)
    query_items = dict(parse_qsl(parts.query, keep_blank_values=True))
    query_items.update(answers)
    query = urlencode(query_items)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def fetch_url(url: str, timeout: int) -> tuple[int, str]:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; PickMyPostcodeChecker/1.0)",
            "Accept": "application/json,text/plain,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.8",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
        status = getattr(response, "status", 200)
        return status, body


def post_form(url: str, form: dict[str, str], timeout: int) -> tuple[int, str]:
    data = urlencode(form).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; PickMyPostcodeChecker/1.0)",
            "Accept": "application/json,text/plain,*/*",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
        status = getattr(response, "status", 200)
        return status, body


def parse_json_document(body: str) -> Any | None:
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def parse_json_map(value: str, env_name: str) -> dict[str, str]:
    value = value.strip()
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{env_name} must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"{env_name} must be a JSON object")
    result: dict[str, str] = {}
    for key, item in payload.items():
        if not isinstance(key, str) or not key.strip():
            raise SystemExit(f"{env_name} must contain string keys")
        if not isinstance(item, str):
            raise SystemExit(f"{env_name} must contain string values")
        result[key.strip()] = item.strip()
    return result


def postcode_pattern(postcode: str) -> re.Pattern[str]:
    normalized = normalize_postcode(postcode)
    pattern = r"[\s-]*".join(re.escape(ch) for ch in normalized)
    return re.compile(pattern, re.I)


def make_excerpt(text: str, postcode: str, limit: int = 240) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    match = postcode_pattern(postcode).search(compact)
    if match:
        start = max(0, match.start() - 80)
        end = min(len(compact), start + limit)
        return compact[start:end]
    return compact[:limit]


def collect_current_results(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []

    data = payload.get("data")
    if not isinstance(data, dict):
        return []

    draw_results = data.get("drawResults")
    if not isinstance(draw_results, dict):
        return []

    results: list[str] = []

    def add_value(value: Any) -> None:
        if isinstance(value, str):
            compact = value.strip()
            if compact:
                results.append(compact)
            return
        if isinstance(value, list):
            for item in value:
                add_value(item)
            return
        if isinstance(value, dict):
            for item in value.values():
                add_value(item)

    for key in ("main", "survey", "video", "mini"):
        entry = draw_results.get(key)
        if isinstance(entry, dict):
            add_value(entry.get("result"))

    stackpot = draw_results.get("stackpot")
    if isinstance(stackpot, dict):
        add_value(stackpot.get("result"))
        add_value(stackpot.get("winningresult"))

    bonus = draw_results.get("bonus")
    if isinstance(bonus, dict):
        for key in ("five", "ten", "twenty"):
            entry = bonus.get(key)
            if isinstance(entry, dict):
                add_value(entry.get("result"))

    return results


def render_json_excerpt(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""

    data = payload.get("data")
    if not isinstance(data, dict):
        return ""

    draw_results = data.get("drawResults")
    if not isinstance(draw_results, dict):
        return ""

    summary: dict[str, Any] = {}
    for key in ("main", "survey", "video", "mini"):
        entry = draw_results.get(key)
        if isinstance(entry, dict):
            summary[key] = {
                "result": entry.get("result"),
                "status": entry.get("status"),
                "updated": entry.get("updated"),
            }

    stackpot = draw_results.get("stackpot")
    if isinstance(stackpot, dict):
        summary["stackpot"] = {
            "result": stackpot.get("result"),
            "winningresult": stackpot.get("winningresult"),
        }

    bonus = draw_results.get("bonus")
    if isinstance(bonus, dict):
        summary["bonus"] = {}
        for key in ("five", "ten", "twenty"):
            entry = bonus.get(key)
            if isinstance(entry, dict):
                summary["bonus"][key] = {"result": entry.get("result")}

    return json.dumps(summary, indent=2, sort_keys=True)


def render_html_excerpt(body: str, limit: int = 240) -> str:
    text = html_to_text(body)
    return make_excerpt(text, "", limit)


def notification_signature(snapshot: CheckSnapshot) -> str:
    return "|".join(
        [
            snapshot.postcode,
            snapshot.matched_text,
            snapshot.url,
            snapshot.status,
        ]
    )


def survey_signature(snapshot: SurveySnapshot) -> str:
    return "|".join(
        [
            snapshot.url,
            json.dumps(snapshot.answers, sort_keys=True),
        ]
    )


def send_pushover_notification(config: Config, snapshot: CheckSnapshot, state: AppState) -> None:
    if not config.pushover_app_token or not config.pushover_user_key:
        return
    if not snapshot.found:
        return

    signature = notification_signature(snapshot)
    if state.last_notification_signature == signature:
        return

    message = (
        f"{pretty_postcode(snapshot.postcode)} matched the current Pick My Postcode draw.\n"
        f"Current result: {snapshot.matched_text}\n"
        f"Checked at: {snapshot.checked_at}"
    )
    form = {
        "token": config.pushover_app_token,
        "user": config.pushover_user_key,
        "message": message,
        "title": config.pushover_title or "Pick My Postcode",
    }
    if config.pushover_device:
        form["device"] = config.pushover_device
    if config.pushover_sound:
        form["sound"] = config.pushover_sound
    if config.pushover_url:
        form["url"] = config.pushover_url
    if config.pushover_url_title:
        form["url_title"] = config.pushover_url_title

    try:
        status, body = post_form(PUSHOVER_ENDPOINT, form, config.request_timeout)
        payload = parse_json_document(body)
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        if isinstance(payload, dict) and payload.get("status") != 1:
            raise RuntimeError(payload.get("errors") or payload.get("error") or "unknown error")
        state.last_notification_signature = signature
        state.last_notification_at = snapshot.checked_at
        state.last_notification_error = ""
        state.save()
        print(f"[pushover] notification sent for {snapshot.pretty_postcode}", flush=True)
    except Exception as exc:
        state.last_notification_error = str(exc)
        state.save()
        print(f"[pushover] notification failed: {exc}", flush=True)


def run_survey(config: Config, state: AppState) -> SurveySnapshot:
    attempted_at = local_now(config.tz)
    submitted_url = build_survey_url(config.survey_url, config.survey_answers)
    title = "Pick My Postcode survey"
    excerpt = ""
    status = "disabled"
    http_status: int | None = None
    error: str | None = None
    body = ""

    if not config.survey_answers:
        snapshot = SurveySnapshot(
            attempted_at=attempted_at.isoformat(),
            url=config.survey_url,
            answers={},
            submitted_url=submitted_url,
            title=title,
            excerpt="",
            status=status,
            http_status=http_status,
            error=error,
        )
        state.update_survey(snapshot, "")
        return snapshot

    existing = state.current_survey()
    if existing is not None and state.last_survey_at:
        try:
            last_attempt = datetime.fromisoformat(state.last_survey_at)
        except ValueError:
            last_attempt = None
        if last_attempt is not None and last_attempt.date() == attempted_at.date() and state.last_survey_signature == survey_signature(config.survey_url, config.survey_answers):
            snapshot = SurveySnapshot(
                attempted_at=attempted_at.isoformat(),
                url=config.survey_url,
                answers=dict(config.survey_answers),
                submitted_url=submitted_url,
                title=existing.title,
                excerpt=existing.excerpt,
                status="already_submitted_today",
                http_status=existing.http_status,
                error=None,
            )
            state.update_survey(snapshot, state.last_survey_raw_body)
            return snapshot

    try:
        http_status, body = fetch_url(config.survey_url, config.request_timeout)
        page_title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
        if page_title_match:
            title = html.unescape(page_title_match.group(1)).strip()
        excerpt = render_html_excerpt(body)

        if http_status >= 400:
            raise RuntimeError(f"HTTP {http_status}")

        http_status, body = fetch_url(submitted_url, config.request_timeout)
        page_title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
        if page_title_match:
            title = html.unescape(page_title_match.group(1)).strip()
        excerpt = render_html_excerpt(body)
        if http_status >= 400:
            raise RuntimeError(f"HTTP {http_status}")

        status = "submitted"
        snapshot = SurveySnapshot(
            attempted_at=attempted_at.isoformat(),
            url=config.survey_url,
            answers=dict(config.survey_answers),
            submitted_url=submitted_url,
            title=title,
            excerpt=excerpt,
            status=status,
            http_status=http_status,
            error=None,
        )
        state.update_survey(snapshot, body, successful=True)
        print(f"[survey] submitted answers for {config.survey_url}", flush=True)
        return snapshot
    except HTTPError as exc:
        error = f"HTTP error {exc.code}: {exc.reason}"
        status = "error"
        http_status = exc.code
    except URLError as exc:
        error = f"Network error: {exc.reason}"
        status = "error"
    except Exception as exc:
        error = f"Unexpected error: {exc}"
        status = "error"

    snapshot = SurveySnapshot(
        attempted_at=attempted_at.isoformat(),
        url=config.survey_url,
        answers=dict(config.survey_answers),
        submitted_url=submitted_url,
        title=title,
        excerpt=excerpt,
        status=status,
        http_status=http_status,
        error=error,
    )
    state.update_survey(snapshot, body if body else "")
    print(f"[survey] submission failed: {error}", flush=True)
    return snapshot


def run_check(config: Config, state: AppState) -> CheckSnapshot:
    checked_at = local_now(config.tz)
    url = build_check_url(config.check_url_template, config.postcode, config.entry_id)
    found = False
    matched_text = config.match_text
    title = ""
    excerpt = ""
    status = "unknown"
    http_status: int | None = None
    error: str | None = None

    try:
        http_status, body = fetch_url(url, config.request_timeout)
        payload = parse_json_document(body)
        if isinstance(payload, dict):
            title = "Pick My Postcode current draw"
            candidates = collect_current_results(payload)
            normalized_postcode = normalize_postcode(config.postcode)
            matches = [candidate for candidate in candidates if normalize_postcode(candidate) == normalized_postcode]
            found = bool(matches)
            matched_text = matches[0] if matches else config.match_text
            status = "results_present" if found else "no_results"
            excerpt = render_json_excerpt(payload)
        else:
            text = html_to_text(body)
            title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
            title = html.unescape(title_match.group(1)).strip() if title_match else ""

            normalized_text = normalize_postcode(text)
            normalized_postcode = normalize_postcode(config.postcode)
            found = normalized_postcode in normalized_text
            matched_text = config.match_text
            if matched_text:
                status = "no_results" if matched_text.lower() in text.lower() else "results_present"
            else:
                status = "results_present" if found else "no_results"
            excerpt = make_excerpt(text, config.postcode)
            if found and status == "no_results":
                status = "possible_match"
    except HTTPError as exc:
        error = f"HTTP error {exc.code}: {exc.reason}"
        status = "error"
        http_status = exc.code
    except URLError as exc:
        error = f"Network error: {exc.reason}"
        status = "error"
    except Exception as exc:
        error = f"Unexpected error: {exc}"
        status = "error"

    snapshot = CheckSnapshot(
        checked_at=checked_at.isoformat(),
        url=url,
        postcode=config.postcode,
        pretty_postcode=pretty_postcode(config.postcode),
        found=found,
        matched_text=matched_text,
        title=title,
        excerpt=excerpt,
        status=status,
        http_status=http_status,
        next_check_at=None,
        error=error,
    )
    state.update(snapshot, "" if error else body)
    if error is None and snapshot.status == "results_present" and snapshot.found:
        send_pushover_notification(config, snapshot, state)
    return snapshot


def format_dashboard(
    check_snapshot: CheckSnapshot | None,
    survey_snapshot: SurveySnapshot | None,
    config: Config,
) -> dict[str, Any]:
    now = local_now(config.tz)
    next_check = next_run_at(now, *config.schedule)
    return {
        "status": check_snapshot.status if check_snapshot else "idle",
        "postcode": pretty_postcode(config.postcode),
        "check_time": config.check_time,
        "timezone": config.timezone,
        "check_url_template": config.check_url_template,
        "entry_id": config.entry_id,
        "pushover_enabled": bool(config.pushover_app_token and config.pushover_user_key),
        "survey_enabled": bool(config.survey_answers),
        "survey_url": config.survey_url,
        "next_check_at": (check_snapshot.next_check_at if check_snapshot and check_snapshot.next_check_at else next_check.isoformat()),
        "check": asdict(check_snapshot) if check_snapshot else None,
        "survey": asdict(survey_snapshot) if survey_snapshot else None,
    }


def render_html(snapshot: dict[str, Any]) -> str:
    check = snapshot.get("check") or {}
    survey = snapshot.get("survey") or {}
    safe = {
        "status": html.escape("" if snapshot.get("status") is None else str(snapshot.get("status"))),
        "postcode": html.escape("" if snapshot.get("postcode") is None else str(snapshot.get("postcode"))),
        "check_time": html.escape("" if snapshot.get("check_time") is None else str(snapshot.get("check_time"))),
        "timezone": html.escape("" if snapshot.get("timezone") is None else str(snapshot.get("timezone"))),
        "check_url_template": html.escape("" if snapshot.get("check_url_template") is None else str(snapshot.get("check_url_template"))),
        "entry_id": html.escape("" if snapshot.get("entry_id") is None else str(snapshot.get("entry_id"))),
        "pushover_enabled": html.escape("" if snapshot.get("pushover_enabled") is None else str(snapshot.get("pushover_enabled"))),
        "survey_enabled": html.escape("" if snapshot.get("survey_enabled") is None else str(snapshot.get("survey_enabled"))),
        "survey_url": html.escape("" if snapshot.get("survey_url") is None else str(snapshot.get("survey_url"))),
        "next_check_at": html.escape("" if snapshot.get("next_check_at") is None else str(snapshot.get("next_check_at"))),
    }
    badge_class = {
        "error": "bad",
        "no_results": "neutral",
        "results_present": "good",
        "possible_match": "good",
        "idle": "neutral",
    }.get(snapshot.get("status", "idle"), "neutral")

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pick My Postcode Checker</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #08111f;
      --panel: rgba(11, 20, 37, 0.92);
      --panel-border: rgba(148, 163, 184, 0.18);
      --text: #e5eefb;
      --muted: #93a4bf;
      --good: #6ee7b7;
      --bad: #fda4af;
      --neutral: #fde68a;
      --accent: #7dd3fc;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at top left, rgba(56, 189, 248, 0.16), transparent 30%),
        radial-gradient(circle at bottom right, rgba(16, 185, 129, 0.12), transparent 28%),
        linear-gradient(160deg, #050914 0%, #0b1220 45%, #101826 100%);
      color: var(--text);
      font: 16px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    }}
    .wrap {{
      max-width: 1100px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    .hero {{
      display: grid;
      gap: 20px;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      align-items: stretch;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--panel-border);
      border-radius: 24px;
      padding: 24px;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.35);
      backdrop-filter: blur(12px);
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: clamp(2rem, 4vw, 3.5rem);
      line-height: 1.05;
      letter-spacing: -0.04em;
    }}
    .lede {{ color: var(--muted); max-width: 62ch; }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border-radius: 999px;
      padding: 8px 14px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-size: 0.78rem;
      margin-bottom: 16px;
    }}
    .bad {{ background: rgba(253, 164, 175, 0.12); color: var(--bad); }}
    .good {{ background: rgba(110, 231, 183, 0.12); color: var(--good); }}
    .neutral {{ background: rgba(253, 230, 138, 0.12); color: var(--neutral); }}
    dl {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin: 0;
    }}
    .stat {{
      padding: 16px;
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid rgba(255, 255, 255, 0.06);
    }}
    dt {{
      color: var(--muted);
      font-size: 0.85rem;
      margin-bottom: 6px;
    }}
    dd {{
      margin: 0;
      word-break: break-word;
      font-size: 1rem;
    }}
    code {{
      background: rgba(15, 23, 42, 0.92);
      padding: 2px 6px;
      border-radius: 8px;
    }}
    pre {{
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      background: rgba(2, 6, 23, 0.85);
      border: 1px solid rgba(148, 163, 184, 0.14);
      border-radius: 18px;
      padding: 16px;
      margin: 0;
      color: #dbeafe;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1.2fr 0.8fr;
      gap: 20px;
      margin-top: 20px;
    }}
    @media (max-width: 860px) {{
      .grid {{ grid-template-columns: 1fr; }}
    }}
    a {{ color: var(--accent); }}
    .foot {{ color: var(--muted); font-size: 0.9rem; margin-top: 14px; }}
  </style>
</head>
<body>
  <main class="wrap">
    <section class="hero">
      <div class="panel">
        <div class="badge {badge_class}">{safe['status']}</div>
        <h1>Pick My Postcode checker</h1>
        <p class="lede">This container checks the live Pick My Postcode current-draw API for <code>{safe['postcode']}</code> and submits the daily survey on the schedule you set in the stack.</p>
        <p class="foot">Next run: <code>{safe.get('next_check_at', '')}</code></p>
      </div>
      <div class="panel">
        <dl>
          <div class="stat"><dt>Postcode</dt><dd>{safe['postcode']}</dd></div>
          <div class="stat"><dt>Schedule</dt><dd>{safe['check_time']} {safe['timezone']}</dd></div>
          <div class="stat"><dt>Target URL</dt><dd>{safe['check_url_template']}</dd></div>
          <div class="stat"><dt>Entry ID</dt><dd>{safe['entry_id']}</dd></div>
          <div class="stat"><dt>Pushover</dt><dd>{safe['pushover_enabled']}</dd></div>
          <div class="stat"><dt>Survey enabled</dt><dd>{safe['survey_enabled']}</dd></div>
        </dl>
      </div>
    </section>
    <section class="grid">
      <div class="panel">
        <h2 style="margin-top:0">Latest check</h2>
        <pre>{html.escape(json.dumps(check, indent=2, sort_keys=True))}</pre>
      </div>
      <div class="panel">
        <h2 style="margin-top:0">Latest survey</h2>
        <pre>{html.escape(json.dumps(survey, indent=2, sort_keys=True))}</pre>
      </div>
    </section>
    <section class="grid">
      <div class="panel">
        <h2 style="margin-top:0">Health</h2>
        <pre>{html.escape(json.dumps({{"ok": True, "status": snapshot.get("status", "idle")}}, indent=2, sort_keys=True))}</pre>
        <p class="foot">Health endpoint: <code>/health</code></p>
      </div>
      <div class="panel">
        <h2 style="margin-top:0">Survey config</h2>
        <pre>{html.escape(json.dumps({{"survey_url": snapshot.get("survey_url"), "survey_answers": survey.get("answers")}}, indent=2, sort_keys=True))}</pre>
      </div>
    </section>
  </main>
</body>
</html>"""


def make_handler(state: AppState, config: Config):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _respond(self, code: int, content_type: str, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            snapshot = state.current()
            survey_snapshot = state.current_survey()
            data = format_dashboard(snapshot, survey_snapshot, config)
            if self.path in {"/", "/index.html"}:
                self._respond(200, "text/html; charset=utf-8", render_html(data))
                return
            if self.path == "/health":
                status = 200 if data.get("status") != "error" else 500
                self._respond(status, "application/json; charset=utf-8", json.dumps({"ok": status == 200, "snapshot": data}, indent=2))
                return
            if self.path == "/api/status":
                self._respond(200, "application/json; charset=utf-8", json.dumps(data, indent=2, sort_keys=True))
                return
            self._respond(404, "text/plain; charset=utf-8", "Not found")

    return Handler


def scheduler_loop(state: AppState, stop_event: threading.Event) -> None:
    config = state.config
    hour, minute, second = config.schedule
    while not stop_event.is_set():
        now = local_now(config.tz)
        target = next_run_at(now, hour, minute, second)
        snapshot = state.current()
        if snapshot is not None:
            snapshot.next_check_at = target.isoformat()
            state.save()
        print(f"[scheduler] next check at {target.isoformat()}", flush=True)
        sleep_until(target, stop_event)
        if stop_event.is_set():
            break
        print(f"[checker] running at {local_now(config.tz).isoformat()}", flush=True)
        snapshot = run_check(config, state)
        survey_snapshot = run_survey(config, state)
        next_check = next_run_at(local_now(config.tz), hour, minute, second).isoformat()
        snapshot.next_check_at = next_check
        state.save()
        print(
            f"[checker] status={snapshot.status} found={snapshot.found} "
            f"http_status={snapshot.http_status} url={snapshot.url}",
            flush=True,
        )
        print(
            f"[survey] status={survey_snapshot.status} http_status={survey_snapshot.http_status} "
            f"url={survey_snapshot.submitted_url}",
            flush=True,
        )


def main() -> None:
    postcode = env("POSTCODE").strip()
    if not postcode:
        raise SystemExit("POSTCODE cannot be empty")

    config = Config(
        postcode=postcode,
        check_time=os.environ.get("CHECK_TIME", "15:30"),
        timezone=os.environ.get("TZ", "Europe/London"),
        check_url_template=os.environ.get("CHECK_URL_TEMPLATE", DEFAULT_URL_TEMPLATE),
        entry_id=os.environ.get("ENTRY_ID", DEFAULT_ENTRY_ID),
        match_text=os.environ.get("MATCH_TEXT", DEFAULT_MATCH_TEXT),
        request_timeout=int(os.environ.get("REQUEST_TIMEOUT", str(DEFAULT_REQUEST_TIMEOUT))),
        http_port=int(os.environ.get("HTTP_PORT", str(DEFAULT_HTTP_PORT))),
        state_path=STATE_PATH,
        pushover_app_token=os.environ.get("PUSHOVER_APP_TOKEN", "").strip(),
        pushover_user_key=os.environ.get("PUSHOVER_USER_KEY", "").strip(),
        pushover_device=os.environ.get("PUSHOVER_DEVICE", "").strip(),
        pushover_sound=os.environ.get("PUSHOVER_SOUND", "").strip(),
        pushover_title=os.environ.get("PUSHOVER_TITLE", "Pick My Postcode").strip(),
        pushover_url=os.environ.get("PUSHOVER_URL", "").strip(),
        pushover_url_title=os.environ.get("PUSHOVER_URL_TITLE", "").strip(),
        survey_url=os.environ.get("SURVEY_URL", DEFAULT_SURVEY_URL).strip(),
        survey_answers=parse_json_map(os.environ.get("SURVEY_ANSWERS_JSON", DEFAULT_SURVEY_ANSWERS_JSON), "SURVEY_ANSWERS_JSON"),
    )

    state = AppState(config)
    stop_event = threading.Event()

    def handle_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    server = ThreadingHTTPServer(("0.0.0.0", config.http_port), make_handler(state, config))
    server.timeout = 1.0

    scheduler = threading.Thread(target=scheduler_loop, args=(state, stop_event), daemon=True)
    scheduler.start()

    print(
        f"[startup] postcode={pretty_postcode(config.postcode)} "
        f"check_time={config.check_time} timezone={config.timezone} "
        f"url_template={config.check_url_template} entry_id={config.entry_id} "
        f"survey={'on' if config.survey_answers else 'off'} "
        f"pushover={'on' if config.pushover_app_token and config.pushover_user_key else 'off'}",
        flush=True,
    )
    print(f"[startup] listening on :{config.http_port}", flush=True)

    try:
        while not stop_event.is_set():
            server.handle_request()
    finally:
        stop_event.set()
        server.server_close()
        scheduler.join(timeout=5)


if __name__ == "__main__":
    main()
