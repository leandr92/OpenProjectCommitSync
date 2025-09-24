from fastapi import FastAPI, Request, Header, HTTPException
import requests
import os
import hmac
import hashlib
import re

app = FastAPI()

OPENPROJECT_URL = os.getenv("OPENPROJECT_URL")
OPENPROJECT_API_KEY = os.getenv("OPENPROJECT_API_KEY")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")


def verify_signature(payload_body: bytes, signature_header: str):
    if not signature_header:
        raise HTTPException(status_code=400, detail="Missing X-Hub-Signature-256 header")

    sha_name, signature = signature_header.split('=')
    if sha_name != 'sha256':
        raise HTTPException(status_code=400, detail="Unsupported signature method")

    mac = hmac.new(GITHUB_WEBHOOK_SECRET.encode(), msg=payload_body, digestmod=hashlib.sha256)
    if not hmac.compare_digest(mac.hexdigest(), signature):
        raise HTTPException(status_code=403, detail="Invalid signature")


def add_comment_to_task(task_id: int, comment: str):
    url = f"{OPENPROJECT_URL}/api/v3/work_packages/{task_id}/activities"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENPROJECT_API_KEY}"
    }
    payload = {"comment": {"raw": comment}}

    resp = requests.post(url, json=payload, headers=headers)
    if resp.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"OpenProject API error: {resp.text}")
    return resp.json()


@app.post("/github-webhook")
async def github_webhook(request: Request, x_hub_signature_256: str = Header(None)):
    body = await request.body()

    # проверка подписи GitHub
    verify_signature(body, x_hub_signature_256)

    data = await request.json()
    for commit in data.get("commits", []):
        message = commit.get("message", "")
        url = commit.get("url", "")
        author = commit.get("author", {}).get("name", "Unknown")

        match = re.search(r"#(\d+)", message)
        if match:
            task_id = int(match.group(1))
            comment_text = f"💡 Новый коммит от {author}:\n\n{message}\n\n🔗 {url}"
            add_comment_to_task(task_id, comment_text)

    return {"status": "ok"}