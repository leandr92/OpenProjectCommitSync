from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
import requests
import os
import hmac
import hashlib
import re
import base64
import json
import logging
from pathlib import Path
from typing import Iterable, Dict, Any, List, Optional

app = FastAPI()

OPENPROJECT_URL = os.getenv("OPENPROJECT_URL")
OPENPROJECT_API_KEY = os.getenv("OPENPROJECT_API_KEY")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")
GITLAB_WEBHOOK_SECRET = os.getenv("GITLAB_WEBHOOK_SECRET")
DEFAULT_MAPPING_PATH = Path(__file__).resolve().parent / "status_mapping.json"
STATUS_MAPPING_FILE = Path(os.getenv("STATUS_MAPPING_FILE", str(DEFAULT_MAPPING_PATH)))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

TASK_ID_PATTERN = re.compile(r"#(\d+)")
BRANCH_TASK_PATTERN = re.compile(r"(?:^|[-_/])(\d+)")
MERGE_PR_PATTERN = re.compile(r"Merge pull request #\d+ from [^/\s]+/(?P<branch>[\w\-./]+)", re.IGNORECASE)
MERGE_BRANCH_PATTERN = re.compile(r"Merge branch '([^']+)' into '([^']+)'", re.IGNORECASE)
MERGE_REMOTE_PATTERN = re.compile(r"Merge remote-tracking branch '([^']+)'", re.IGNORECASE)

OPENPROJECT_AUTH_HEADER = ""
if OPENPROJECT_API_KEY:
    basic_token = base64.b64encode(f"apikey:{OPENPROJECT_API_KEY}".encode()).decode()
    OPENPROJECT_AUTH_HEADER = f"Basic {basic_token}"

OPENPROJECT_BASE_URL = (OPENPROJECT_URL or "").rstrip("/")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("openproject_commit_sync")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))


def log_event(level: int, message: str, **context):
    if context:
        context_str = ", ".join(f"{key}={value!r}" for key, value in context.items())
        logger.log(level, f"{message} | {context_str}")
    else:
        logger.log(level, message)


def normalize_status_value(value: Any) -> Optional[str]:
    if isinstance(value, int):
        return f"/api/v3/statuses/{value}"
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.isdigit():
            return f"/api/v3/statuses/{raw}"
        if raw.startswith("http") or raw.startswith("/api/"):
            return raw
        if raw.startswith("/"):
            return raw
    return None


def load_status_mapping() -> Dict[str, str]:
    path = STATUS_MAPPING_FILE
    if not path.exists():
        log_event(logging.WARNING, "Status mapping file not found", path=str(path))
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        log_event(logging.ERROR, "Failed to load status mapping", path=str(path))
        return {}

    mapping: Dict[str, str] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            normalized = normalize_status_value(value)
            if normalized:
                mapping[key] = normalized
    log_event(logging.INFO, "Status mapping loaded", entries=len(mapping), path=str(path))
    return mapping


STATUS_MAPPING = load_status_mapping()


def build_api_url(path: str) -> str:
    if not OPENPROJECT_BASE_URL:
        return path
    if path.startswith("http"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return f"{OPENPROJECT_BASE_URL}{path}"


def status_href(status_key: str) -> Optional[str]:
    href = STATUS_MAPPING.get(status_key)
    if not href:
        log_event(logging.DEBUG, "No status href for key", status_key=status_key)
        return None
    return href if href.startswith("http") else href


def extract_branch_name(ref: str) -> str:
    if not ref:
        return ""
    if "/" in ref:
        return ref.split("/", 2)[-1]
    return ref


def derive_status_key_from_branch(branch_name: str) -> Optional[str]:
    name = (branch_name or "").strip().lower()
    if not name:
        log_event(logging.DEBUG, "Branch name empty for status derivation")
        return None
    short = name.split("/")[-1]
    if short in {"main", "master"}:
        return "completed"
    if short == "dev":
        return "testing"
    return None


def extract_source_branch_from_message(message: str, target_branch: str = "") -> Optional[str]:
    first_line = (message or "").splitlines()[0]

    match = MERGE_PR_PATTERN.search(first_line)
    if match:
        branch = match.group("branch")
        branch = branch.split("/", 1)[-1] if "/" in branch else branch
        if branch and branch != target_branch:
            return branch

    match = MERGE_BRANCH_PATTERN.search(first_line)
    if match:
        branch = match.group(1).strip()
        branch = branch.replace("origin/", "", 1)
        if branch and branch != target_branch:
            return branch

    match = MERGE_REMOTE_PATTERN.search(first_line)
    if match:
        branch = match.group(1).strip()
        branch = branch.replace("origin/", "", 1)
        if branch and branch != target_branch:
            return branch

    return None


def update_task_status(task_id: int, status_key: str):
    href = status_href(status_key)
    if not href:
        log_event(logging.DEBUG, "Status href missing, skipping update", task_id=task_id, status_key=status_key)
        return
    if not OPENPROJECT_BASE_URL or not OPENPROJECT_AUTH_HEADER:
        raise HTTPException(status_code=500, detail="Server misconfigured: missing OPENPROJECT_URL or OPENPROJECT_API_KEY")

    task_url = build_api_url(f"/api/v3/work_packages/{task_id}")
    headers = {
        "Authorization": OPENPROJECT_AUTH_HEADER,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        resp = requests.get(task_url, headers=headers, timeout=10)
    except requests.RequestException as exc:
        log_event(logging.ERROR, "Failed to fetch work package", task_id=task_id, error=str(exc))
        raise HTTPException(status_code=502, detail=f"OpenProject request failed: {exc}")

    if resp.status_code == 404:
        log_event(logging.WARNING, "Work package not found when fetching", task_id=task_id)
        return
    if resp.status_code >= 300:
        log_event(logging.ERROR, "OpenProject returned error on fetch", task_id=task_id, status_code=resp.status_code)
        raise HTTPException(status_code=500, detail=f"OpenProject API error: {resp.text}")

    lock_version = resp.json().get("lockVersion")
    if lock_version is None:
        log_event(logging.ERROR, "Missing lockVersion in OpenProject response", task_id=task_id)
        raise HTTPException(status_code=500, detail="OpenProject response missing lockVersion")

    payload = {
        "lockVersion": lock_version,
        "_links": {
            "status": {
                "href": href,
            }
        }
    }

    try:
        patch_resp = requests.patch(task_url, json=payload, headers=headers, timeout=10)
    except requests.RequestException as exc:
        log_event(logging.ERROR, "Failed to update work package status", task_id=task_id, error=str(exc))
        raise HTTPException(status_code=502, detail=f"OpenProject request failed: {exc}")

    if patch_resp.status_code == 404:
        log_event(logging.WARNING, "Work package not found when updating status", task_id=task_id)
        return
    if patch_resp.status_code >= 300:
        log_event(logging.ERROR, "OpenProject returned error on status update", task_id=task_id, status_code=patch_resp.status_code)
        raise HTTPException(status_code=500, detail=f"OpenProject API error: {patch_resp.text}")

    log_event(logging.INFO, "Task status updated", task_id=task_id, status_key=status_key)

async def set_status_for_tasks(task_ids: Iterable[int], status_key: Optional[str]):
    unique_ids = list(dict.fromkeys(task_ids))
    if not status_key or status_key not in STATUS_MAPPING:
        log_event(logging.DEBUG, "No status change requested", status_key=status_key, tasks=unique_ids)
        return
    if not unique_ids:
        log_event(logging.DEBUG, "No tasks to update status for", status_key=status_key)
        return

    log_event(logging.INFO, "Updating status for tasks", status_key=status_key, tasks=unique_ids)
    for task_id in unique_ids:
        await run_in_threadpool(update_task_status, task_id, status_key)


def verify_signature(payload_body: bytes, signature_header: str):
    if not signature_header:
        raise HTTPException(status_code=400, detail="Missing X-Hub-Signature-256 header")

    try:
        sha_name, signature = signature_header.split('=', 1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Bad signature header format")

    if sha_name.lower() != 'sha256':
        raise HTTPException(status_code=400, detail="Unsupported signature method")

    if not GITHUB_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Server misconfigured: missing GITHUB_WEBHOOK_SECRET")

    mac = hmac.new(GITHUB_WEBHOOK_SECRET.encode(), msg=payload_body, digestmod=hashlib.sha256)
    if not hmac.compare_digest(mac.hexdigest(), signature):
        raise HTTPException(status_code=403, detail="Invalid signature")


def verify_gitlab_token(token_header: str):
    if not GITLAB_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Server misconfigured: missing GITLAB_WEBHOOK_SECRET")
    if not token_header:
        raise HTTPException(status_code=400, detail="Missing X-Gitlab-Token header")
    if not hmac.compare_digest(token_header.strip(), GITLAB_WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Invalid token")


def add_comment_to_task(task_id: int, comment: str):
    if not OPENPROJECT_BASE_URL or not OPENPROJECT_AUTH_HEADER:
        raise HTTPException(status_code=500, detail="Server misconfigured: missing OPENPROJECT_URL or OPENPROJECT_API_KEY")

    url = build_api_url(f"/api/v3/work_packages/{task_id}/activities")
    headers = {
        "Content-Type": "application/json",
        "Authorization": OPENPROJECT_AUTH_HEADER
    }
    payload = {"comment": {"raw": comment}}

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"OpenProject request failed: {exc}")

    if resp.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"OpenProject API error: {resp.text}")
    return resp.json()


def iter_task_ids(message: str) -> Iterable[int]:
    seen: Dict[str, None] = {}
    text = message or ""
    for match in TASK_ID_PATTERN.finditer(text):
        raw = match.group(1)
        before = text[: match.start()].lower()
        # Игнорируем ID из системной строки мержа PR
        if before.endswith("pull request ") or before.endswith("pull-request "):
            log_event(logging.DEBUG, "Skipped PR number in commit message", candidate=raw, commit_message=message)
            continue
        if raw not in seen:
            seen[raw] = None
            yield int(raw)


def iter_branch_task_ids(branch_name: str) -> Iterable[int]:
    ids = list(dict.fromkeys(TASK_ID_PATTERN.findall(branch_name or "")))
    if ids:
        for sid in ids:
            yield int(sid)
        return

    for match in BRANCH_TASK_PATTERN.finditer(branch_name or ""):
        yield int(match.group(1))


def resolve_author(commit: Dict[str, Any]) -> str:
    author_data = commit.get("author")
    if isinstance(author_data, dict):
        return (
            author_data.get("name")
            or author_data.get("username")
            or author_data.get("email")
            or "Unknown"
        )

    if isinstance(author_data, str):
        return author_data

    return (
        commit.get("author_name")
        or commit.get("author_username")
        or "Unknown"
    )


def format_changed_files(commit: Dict[str, Any]) -> str:
    sections: List[str] = []
    change_map = [
        ("added", "+"),
        ("modified", "~"),
        ("removed", "-"),
    ]
    for key, marker in change_map:
        files = [f for f in commit.get(key, []) if isinstance(f, str) and f]
        if not files:
            continue

        display = files[:5]
        suffix = ""
        if len(files) > len(display):
            suffix = f" и ещё {len(files) - len(display)}"

        sections.append(f"{marker} {', '.join(display)}{suffix}")

    if sections:
        return "Файлы: " + "; ".join(sections)
    return ""


async def process_commits(commits: Iterable[Dict[str, Any]], source: str, branch_name: str = "") -> List[int]:
    collected_ids: List[int] = []
    for commit in commits or []:
        message = (commit.get("message") or "").strip()
        if not message:
            continue

        url = commit.get("url") or commit.get("web_url") or ""
        author = resolve_author(commit)
        source_branch = extract_source_branch_from_message(message, branch_name)

        log_event(
            logging.DEBUG,
            "Processing commit",
            source=source,
            author=author,
            branch=branch_name or None,
            source_branch=source_branch or None,
            url=url or None,
        )

        task_ids = list(iter_task_ids(message))
        if not task_ids:
            continue

        branch_fragment = f" в `{branch_name}`" if branch_name else ""
        comment_lines = [f"💡 Новый коммит ({source}){branch_fragment} от {author}:"]
        if source_branch:
            comment_lines.append(f"↪️ Из ветки `{source_branch}`")
        comment_lines.extend(["", message])
        if url:
            comment_lines.extend(["", f"🔗 {url}"])

        files_line = format_changed_files(commit)
        if files_line:
            comment_lines.extend(["", files_line])

        comment_text = "\n".join(comment_lines)

        for task_id in task_ids:
            collected_ids.append(task_id)
            await run_in_threadpool(add_comment_to_task, task_id, comment_text)
        log_event(
            logging.INFO,
            "Comment posted for commit",
            tasks=task_ids,
            source=source,
            branch=branch_name or None,
            url=url or None,
        )

    return list(dict.fromkeys(collected_ids))


async def notify_branch_creation(task_ids: Iterable[int], branch_name: str, source: str, branch_url: str = ""):
    task_ids = list(dict.fromkeys(task_ids))
    if not task_ids:
        return

    log_event(logging.INFO, "Recording branch creation", source=source, branch=branch_name, tasks=task_ids, branch_url=branch_url or None)

    comment_lines = [f"🌱 Создана ветка ({source}): `{branch_name}`"]
    if branch_url:
        comment_lines.extend(["", f"🔗 {branch_url}"])

    comment_text = "\n".join(comment_lines)

    for task_id in task_ids:
        await run_in_threadpool(add_comment_to_task, task_id, comment_text)

    await set_status_for_tasks(task_ids, "in_progress")


@app.post("/github-webhook")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str = Header(None),
    x_github_event: str = Header(None),
):
    body = await request.body()

    # Проверка подписи GitHub
    verify_signature(body, x_hub_signature_256)

    data = await request.json()
    branch_name = extract_branch_name(data.get("ref") or "")

    # Обработка типа события
    event = (x_github_event or "").lower()
    log_event(logging.INFO, "GitHub webhook received", event=event or None, branch=branch_name or None)
    if event == "ping":
        log_event(logging.DEBUG, "GitHub ping event acknowledged")
        return {"status": "ok"}
    if event == "create":
        if (data.get("ref_type") or "").lower() != "branch":
            return {"status": "ignored", "event": event}

        task_ids = list(iter_branch_task_ids(branch_name))
        if not task_ids:
            return {"status": "ignored", "event": event}

        repo = data.get("repository") or {}
        repo_url = repo.get("html_url") or repo.get("url") or ""
        branch_url = f"{repo_url}/tree/{branch_name}" if repo_url else ""
        log_event(logging.INFO, "GitHub branch create", branch=branch_name, tasks=task_ids, branch_url=branch_url or None)
        await notify_branch_creation(task_ids, branch_name, source="GitHub", branch_url=branch_url)
        return {"status": "ok"}

    if event and event != "push":
        return {"status": "ignored", "event": event}

    if data.get("created"):
        task_ids = list(iter_branch_task_ids(branch_name))
        if task_ids:
            repo = data.get("repository") or {}
            repo_url = repo.get("html_url") or repo.get("url") or ""
            branch_url = f"{repo_url}/tree/{branch_name}" if repo_url else ""
            log_event(logging.INFO, "GitHub branch detected in push", branch=branch_name, tasks=task_ids, branch_url=branch_url or None)
            await notify_branch_creation(task_ids, branch_name, source="GitHub", branch_url=branch_url)

    task_ids_from_commits = await process_commits(data.get("commits", []), source="GitHub", branch_name=branch_name)

    status_key = derive_status_key_from_branch(branch_name)
    if status_key:
        log_event(logging.INFO, "GitHub derived status from branch", branch=branch_name or None, status_key=status_key, tasks=task_ids_from_commits)
    await set_status_for_tasks(task_ids_from_commits, status_key)

    return {"status": "ok"}


@app.post("/gitlab-webhook")
async def gitlab_webhook(
    request: Request,
    x_gitlab_token: str = Header(None),
    x_gitlab_event: str = Header(None),
):
    verify_gitlab_token(x_gitlab_token)

    event = (x_gitlab_event or "").lower()
    log_event(logging.INFO, "GitLab webhook received", event=event or None)
    if event in {"ping", "system hook"}:
        log_event(logging.DEBUG, "GitLab ping/system hook acknowledged", event=event)
        return {"status": "ok"}
    if event and event not in {"push hook"}:
        return {"status": "ignored", "event": event}

    data = await request.json()
    object_kind = (data.get("object_kind") or "").lower()
    if object_kind and object_kind != "push":
        return {"status": "ignored", "object_kind": object_kind}

    # Обработка создания ветки (новый push с нуля)
    before = data.get("before") or ""
    branch_name = extract_branch_name(data.get("ref") or "")
    is_new_branch = before.strip("0") == "" and before != ""
    if is_new_branch:
        task_ids = list(iter_branch_task_ids(branch_name))
        if task_ids:
            project = data.get("project") or {}
            project_url = project.get("web_url") or ""
            branch_url = f"{project_url}/-/tree/{branch_name}" if project_url else ""
            log_event(logging.INFO, "GitLab branch detected", branch=branch_name, tasks=task_ids, branch_url=branch_url or None)
            await notify_branch_creation(task_ids, branch_name, source="GitLab", branch_url=branch_url)

    task_ids_from_commits = await process_commits(data.get("commits", []), source="GitLab", branch_name=branch_name)

    status_key = derive_status_key_from_branch(branch_name)
    if status_key:
        log_event(logging.INFO, "GitLab derived status from branch", branch=branch_name or None, status_key=status_key, tasks=task_ids_from_commits)
    await set_status_for_tasks(task_ids_from_commits, status_key)

    return {"status": "ok"}
