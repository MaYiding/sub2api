#!/usr/bin/env python3
"""Top up active Sub2API users whose wallet balance is below the threshold."""

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEPLOY_DIR = os.environ.get("SUB2API_DEPLOY_DIR", "/opt/sub2api/deploy")
STATE_FILE = Path(os.environ.get(
    "SUB2API_BLUE_GREEN_STATE_FILE",
    os.path.join(DEPLOY_DIR, ".blue-green-active"),
))
LEGACY_CONTAINER = os.environ.get("SUB2API_LEGACY_CONTAINER", "sub2api")
LEGACY_PORT = 8080
CONTAINER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
THRESHOLD = 100.0
TOP_UP_AMOUNT = 500.0
PAGE_SIZE = 200
REQUEST_TIMEOUT = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("sub2api-auto-recharge")


def read_state():
    if not STATE_FILE.is_file():
        return {}
    values = {}
    for line in STATE_FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def inspect_container(container, template):
    try:
        return subprocess.check_output(
            ["docker", "inspect", container, "--format", template],
            text=True,
            timeout=10,
        ).strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"active Sub2API container is unavailable: {container}") from error


def load_runtime():
    state = read_state()
    container = state.get("active_container", LEGACY_CONTAINER)
    port_text = state.get("active_port", str(LEGACY_PORT))
    if not CONTAINER_NAME_PATTERN.fullmatch(container):
        raise RuntimeError("active Sub2API container name is invalid")
    try:
        port = int(port_text)
    except ValueError as error:
        raise RuntimeError("active Sub2API port is invalid") from error
    if not 1 <= port <= 65535:
        raise RuntimeError("active Sub2API port is outside the valid range")

    status = inspect_container(container, "{{.State.Status}}")
    if status != "running":
        raise RuntimeError(f"active Sub2API container is not running: {container} ({status})")
    health = inspect_container(
        container,
        "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
    )
    if health not in {"healthy", "none"}:
        raise RuntimeError(f"active Sub2API container is not healthy: {container} ({health})")
    return f"http://127.0.0.1:{port}/api/v1", container


def load_admin_credentials(container):
    env_text = subprocess.check_output(
        [
            "docker",
            "inspect",
            container,
            "--format",
            "{{range .Config.Env}}{{println .}}{{end}}",
        ],
        text=True,
        timeout=10,
    )
    values = {}
    for line in env_text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"ADMIN_EMAIL", "ADMIN_PASSWORD"}:
            values[key] = value
    if not values.get("ADMIN_EMAIL") or not values.get("ADMIN_PASSWORD"):
        raise RuntimeError("Sub2API admin credentials are not available in the container environment")
    return values["ADMIN_EMAIL"], values["ADMIN_PASSWORD"]


def request_json(base_url, path, token, method="GET", payload=None, idempotency_key=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = Request(base_url + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read(512).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code} {method} {path}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"request failed {method} {path}: {error.reason}") from error


def unwrap(response):
    return response.get("data", response)


def login(base_url, container):
    email, password = load_admin_credentials(container)
    response = request_json(
        base_url,
        "/auth/login",
        token="",
        method="POST",
        payload={"email": email, "password": password},
    )
    data = unwrap(response)
    token = data.get("access_token") if isinstance(data, dict) else None
    if not token:
        raise RuntimeError("admin login response did not contain an access token")
    return token


def list_users(base_url, token):
    users = []
    page = 1
    while True:
        response = request_json(
            base_url,
            f"/admin/users?page={page}&page_size={PAGE_SIZE}",
            token,
        )
        data = unwrap(response)
        page_items = data.get("items") or data.get("users") or []
        users.extend(page_items)
        total = data.get("total")
        if not page_items or (total is not None and len(users) >= int(total)) or len(page_items) < PAGE_SIZE:
            return users
        page += 1


def user_balance(base_url, token, user_id):
    return unwrap(request_json(base_url, f"/admin/users/{user_id}", token))


def main():
    base_url, container = load_runtime()
    token = login(base_url, container)
    users = list_users(base_url, token)
    bucket = int(time.time()) // 600
    changed = 0
    errors = 0

    for user in users:
        if user.get("role") != "user" or user.get("status") != "active":
            continue
        try:
            balance = float(user.get("balance"))
        except (TypeError, ValueError):
            log.warning("skip user_id=%s: invalid balance", user.get("id"))
            errors += 1
            continue
        if balance >= THRESHOLD:
            continue

        user_id = user.get("id")
        try:
            current = user_balance(base_url, token, user_id)
            current_balance = float(current.get("balance"))
            if current_balance >= THRESHOLD:
                continue
            key = f"sub2api-auto-recharge-v1-{user_id}-{bucket}"
            updated = unwrap(
                request_json(
                    base_url,
                    f"/admin/users/{user_id}/balance",
                    token,
                    method="POST",
                    payload={
                        "balance": TOP_UP_AMOUNT,
                        "operation": "add",
                        "notes": "自动充值：余额低于100，补充500刀",
                    },
                    idempotency_key=key,
                )
            )
            changed += 1
            log.info(
                "recharged user_id=%s email=%s old_balance=%.6f new_balance=%s amount=%.2f",
                user_id,
                user.get("email", ""),
                current_balance,
                updated.get("balance", "unknown") if isinstance(updated, dict) else "unknown",
                TOP_UP_AMOUNT,
            )
        except Exception as error:  # Keep processing other users if one top-up fails.
            errors += 1
            log.error("recharge failed user_id=%s email=%s error=%s", user_id, user.get("email", ""), error)

    log.info("completed users=%d recharged=%d errors=%d", len(users), changed, errors)
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        log.error("run failed: %s", error)
        sys.exit(1)
