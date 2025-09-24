from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
import requests
import os
import hmac
import hashlib
import re
import base64
from typing import Iterable, Dict, Any

app = FastAPI()

OPENPROJECT_URL = os.getenv("OPENPROJECT_URL")
OPENPROJECT_API_KEY = os.getenv("OPENPROJECT_API_KEY")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")
GITLAB_WEBHOOK_SECRET = os.getenv("GITLAB_WEBHOOK_SECRET")

TASK_ID_PATTERN = re.compile(r"#(\d+)")

OPENPROJECT_AUTH_HEADER = ""
if OPENPROJECT_API_KEY:
    basic_token = base64.b64encode(f"apikey:{OPENPROJECT_API_KEY}".encode()).decode()
    OPENPROJECT_AUTH_HEADER = f"Basic {basic_token}"


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
    if not OPENPROJECT_URL or not OPENPROJECT_AUTH_HEADER:
        raise HTTPException(status_code=500, detail="Server misconfigured: missing OPENPROJECT_URL or OPENPROJECT_API_KEY")

    url = f"{OPENPROJECT_URL}/api/v3/work_packages/{task_id}/activities"
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
    for sid in dict.fromkeys(TASK_ID_PATTERN.findall(message or "")):
        yield int(sid)


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


async def process_commits(commits: Iterable[Dict[str, Any]], source: str):
    for commit in commits or []:
        message = (commit.get("message") or "").strip()
        if not message:
            continue

        url = commit.get("url") or commit.get("web_url") or ""
        author = resolve_author(commit)

        task_ids = list(iter_task_ids(message))
        if not task_ids:
            continue

        comment_lines = [f"💡 Новый коммит ({source}) от {author}:", "", message]
        if url:
            comment_lines.extend(["", f"🔗 {url}"])

        comment_text = "\n".join(comment_lines)

        for task_id in task_ids:
            await run_in_threadpool(add_comment_to_task, task_id, comment_text)


@app.post("/github-webhook")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str = Header(None),
    x_github_event: str = Header(None),
):
    body = await request.body()

    # Проверка подписи GitHub
    verify_signature(body, x_hub_signature_256)

    # Обработка типа события
    event = (x_github_event or "").lower()
    if event == "ping":
        return {"status": "ok"}
    if event and event != "push":
        return {"status": "ignored", "event": event}

    data = await request.json()
    await process_commits(data.get("commits", []), source="GitHub")

    return {"status": "ok"}


@app.post("/gitlab-webhook")
async def gitlab_webhook(
    request: Request,
    x_gitlab_token: str = Header(None),
    x_gitlab_event: str = Header(None),
):
    verify_gitlab_token(x_gitlab_token)

    event = (x_gitlab_event or "").lower()
    if event in {"ping", "system hook"}:
        return {"status": "ok"}
    if event and event not in {"push hook"}:
        return {"status": "ignored", "event": event}

    data = await request.json()
    object_kind = (data.get("object_kind") or "").lower()
    if object_kind and object_kind != "push":
        return {"status": "ignored", "object_kind": object_kind}

    await process_commits(data.get("commits", []), source="GitLab")

    return {"status": "ok"}
