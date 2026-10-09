"""Web UI (Home Assistant Ingress) of the add-on: tasks, llm_tasks.yaml editor, processors and audit archives.

Plain standard library on purpose: no extra dependencies in the image and nothing that can break on Alpine.
The runner module (run.py) is passed in, so storage paths and the task runner have exactly one implementation.
"""
import json
import logging
import mimetypes
import os
import re
import shutil
import string
import tarfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import yaml

logger = logging.getLogger("ha_llm_runner.web")

DEFAULT_PORT = 8099
# Only the Supervisor's Ingress proxy (which does the HA login and admin check) and local debugging may connect
ALLOWED_CLIENTS = {"172.30.32.2", "127.0.0.1", "::1", "::ffff:127.0.0.1", "::ffff:172.30.32.2"}
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_INLINE_BYTES = 256 * 1024
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

PROCESSOR_NAME = re.compile(r"^[A-Za-z0-9_\-]+\.py$")
AUDIT_NAME = re.compile(r"^audit_(?P<task>.+)_(?P<stamp>\d{8}_\d{6})\.tar\.gz$")
TEXT_SUFFIXES = (".txt", ".md", ".json", ".csv", ".yaml", ".yml")
KNOWN_TASK_KEYS = {
    "name", "icon", "state_template", "unit_of_measurement", "device_class", "state_class",
    "entities", "files", "urls", "hours", "resample", "data_processor", "processor",
    "prompt", "provider", "model", "temperature", "response_schema", "history_limit",
    "target_sensor", "friendly_name", "audit",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details or {}


class BlockDumper(yaml.SafeDumper):
    pass


def _represent_str(dumper, value):
    # Multi-line prompts read like in llm_tasks.yaml (`prompt: |`) instead of one long quoted string
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


BlockDumper.add_representer(str, _represent_str)


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def validate_tasks_text(runner, text: str) -> dict:
    """YAML syntax plus the structural checks that would otherwise only show up in the add-on log."""
    errors, warnings = [], []
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None) or getattr(e, "context_mark", None)
        errors.append({"line": mark.line + 1 if mark else None, "message": str(e)})
        return {"valid": False, "errors": errors, "warnings": warnings, "task_ids": []}

    data = {} if data is None else data
    if not isinstance(data, dict):
        errors.append({"line": 1, "message": "The file must be a mapping of task ids to task settings (optionally under a top-level 'tasks:' key)."})
        return {"valid": False, "errors": errors, "warnings": warnings, "task_ids": []}

    root_level = "tasks" not in data
    tasks = data if root_level else data["tasks"]
    if tasks is None:
        tasks = {}
    if not isinstance(tasks, dict):
        errors.append({"line": None, "message": "'tasks:' must be a mapping of task ids to task settings."})
        tasks = {}
    if root_level:
        for key in [k for k, v in tasks.items() if not isinstance(v, dict)]:
            warnings.append(f"Top-level entry '{key}' is not a task mapping and will be ignored.")
        tasks = {k: v for k, v in tasks.items() if isinstance(v, dict)}
    if not tasks:
        warnings.append("No tasks defined, so no sensors will be created.")

    for task_id, cfg in tasks.items():
        if not isinstance(cfg, dict):
            errors.append({"line": None, "message": f"Task '{task_id}' must be a mapping of settings."})
            continue
        unknown = sorted(set(map(str, cfg)) - KNOWN_TASK_KEYS)
        if unknown:
            warnings.append(f"Task '{task_id}': unknown key(s) {', '.join(unknown)} (typo?).")
        processor = cfg.get("data_processor") or cfg.get("processor")
        if processor and not os.path.isfile(runner.resolve_config_path(str(processor))):
            warnings.append(f"Task '{task_id}': processor '{processor}' not found, the default aggregation will be used.")
        prompt = cfg.get("prompt")
        if prompt is not None and not isinstance(prompt, str):
            errors.append({"line": None, "message": f"Task '{task_id}': 'prompt' must be text."})
        elif isinstance(prompt, str):
            try:
                list(string.Formatter().parse(prompt))
            except ValueError as e:
                warnings.append(f"Task '{task_id}': placeholders in the prompt can't be filled ({e}); "
                                "use {{ and }} for literal braces. The values are appended instead.")
        target = cfg.get("target_sensor")
        if target and "." not in str(target):
            warnings.append(f"Task '{task_id}': target_sensor '{target}' should look like 'sensor.my_name'.")
        for key in ("hours", "history_limit"):
            if key in cfg and not isinstance(cfg[key], int):
                errors.append({"line": None, "message": f"Task '{task_id}': '{key}' must be a whole number."})

    return {"valid": not errors, "errors": errors, "warnings": warnings, "task_ids": [str(t) for t in tasks]}


class WebApp:
    def __init__(self, runner):
        self.runner = runner
        self.write_lock = threading.Lock()
        self.routes = [
            ("GET", r"", self.index),
            ("GET", r"index\.html", self.index),
            ("GET", r"api/overview", self.overview),
            ("POST", r"api/run-all", self.run_all),
            ("GET", r"api/tasks/(?P<task_id>[^/]+)", self.task_detail),
            ("POST", r"api/tasks/(?P<task_id>[^/]+)/run", self.run_task),
            ("DELETE", r"api/tasks/(?P<task_id>[^/]+)/memory", self.clear_memory),
            ("GET", r"api/config", self.get_config),
            ("PUT", r"api/config", self.save_config),
            ("POST", r"api/config/validate", self.validate_config),
            ("GET", r"api/processors", self.list_processors),
            ("GET", r"api/processors/(?P<name>[^/]+)", self.get_processor),
            ("PUT", r"api/processors/(?P<name>[^/]+)", self.save_processor),
            ("DELETE", r"api/processors/(?P<name>[^/]+)", self.delete_processor),
            ("GET", r"api/audit", self.list_audit),
            ("GET", r"api/audit/(?P<name>[^/]+)", self.audit_detail),
            ("GET", r"api/audit/(?P<name>[^/]+)/download", self.audit_download),
            ("GET", r"api/audit/(?P<name>[^/]+)/files/(?P<member>[^/]+)", self.audit_member),
            ("DELETE", r"api/audit/(?P<name>[^/]+)", self.delete_audit),
        ]
        self.routes = [(method, re.compile(f"^{pattern}$"), handler) for method, pattern, handler in self.routes]

    def dispatch(self, method: str, path: str, query: dict, body):
        allowed = False
        for route_method, pattern, handler in self.routes:
            match = pattern.match(path)
            if not match:
                continue
            allowed = True
            if route_method == method:
                params = {k: unquote(v) for k, v in match.groupdict().items()}
                return handler(query=query, body=body, **params)
        raise ApiError(405 if allowed else 404, "Method not allowed" if allowed else "Not found")

    # --- tasks ------------------------------------------------------------------------------------

    def tasks(self) -> dict:
        return self.runner.load_tasks()

    def task_or_404(self, task_id: str) -> dict:
        tasks = self.tasks()
        if task_id not in tasks:
            raise ApiError(404, f"Task '{task_id}' not found in llm_tasks.yaml.")
        return tasks[task_id]

    def index(self, **_):
        return ("file", os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")

    def overview(self, **_):
        runner = self.runner
        config_text = read_text(runner.TASKS_CONFIG_PATH) if os.path.isfile(runner.TASKS_CONFIG_PATH) else ""
        validation = validate_tasks_text(runner, config_text)
        tasks = []
        for task_id, cfg in self.tasks().items():
            status = runner.task_status(task_id)
            status.pop("last_prompt", None)
            tasks.append({
                "id": task_id,
                "name": cfg.get("name", task_id),
                "icon": cfg.get("icon", "mdi:brain"),
                "llm": bool(cfg.get("prompt")),
                "provider": cfg.get("provider"),
                "model": cfg.get("model"),
                "target_sensor": cfg.get("target_sensor"),
                "memory_entries": len(runner.load_memory(task_id)),
                "status": status,
            })
        return {
            "tasks": tasks,
            "config_exists": bool(config_text),
            "config_errors": validation["errors"],
            "config_warnings": validation["warnings"],
            "mqtt": dict(runner.MQTT_STATUS),
        }

    def task_detail(self, task_id, **_):
        cfg = self.task_or_404(task_id)
        return {
            "id": task_id,
            "config": cfg,
            "config_yaml": yaml.dump({task_id: cfg}, Dumper=BlockDumper, allow_unicode=True, sort_keys=False, width=1000),
            "status": self.runner.task_status(task_id),
            "memory": self.runner.load_memory(task_id),
        }

    def run_task(self, task_id, **_):
        cfg = self.task_or_404(task_id)
        self.runner.run_task_async(task_id, cfg, self.runner._mqtt_client, self.runner.load_options())
        return 202, {"queued": [task_id]}

    def run_all(self, **_):
        self.runner.run_all_tasks_async(self.runner._mqtt_client, self.runner.load_options())
        return 202, {"queued": list(self.tasks())}

    def clear_memory(self, task_id, **_):
        self.task_or_404(task_id)
        self.runner.clear_memory(task_id)
        return {"cleared": task_id}

    # --- llm_tasks.yaml ---------------------------------------------------------------------------

    def get_config(self, **_):
        path = self.runner.TASKS_CONFIG_PATH
        return {"path": path, "exists": os.path.isfile(path), "content": read_text(path) if os.path.isfile(path) else ""}

    def validate_config(self, body, **_):
        return validate_tasks_text(self.runner, self.content_of(body))

    def save_config(self, body, **_):
        content = self.content_of(body)
        validation = validate_tasks_text(self.runner, content)
        if not validation["valid"]:
            raise ApiError(400, "llm_tasks.yaml was not saved because it contains errors.", validation)
        runner = self.runner
        with self.write_lock:
            old_tasks = runner.load_tasks()
            os.makedirs(os.path.dirname(runner.TASKS_CONFIG_PATH), exist_ok=True)
            if os.path.isfile(runner.TASKS_CONFIG_PATH):
                shutil.copy2(runner.TASKS_CONFIG_PATH, f"{runner.TASKS_CONFIG_PATH}.bak")
            runner.write_text_atomic(runner.TASKS_CONFIG_PATH, content)
            new_tasks = runner.load_tasks()
        # New, renamed and removed tasks show up in Home Assistant right away, no add-on restart needed
        runner.sync_task_discovery(old_tasks, new_tasks)
        return {**validation, "saved": True, "mqtt_synced": runner._mqtt_client is not None}

    # --- processors -------------------------------------------------------------------------------

    def processor_path(self, name: str) -> str:
        if not PROCESSOR_NAME.match(name):
            raise ApiError(400, "Processor names may only contain letters, digits, '_' and '-' and must end in '.py'.")
        return os.path.join(self.runner.PROCESSORS_DIR, name)

    def processor_users(self) -> dict:
        users = {}
        for task_id, cfg in self.tasks().items():
            ref = cfg.get("data_processor") or cfg.get("processor")
            if ref:
                users.setdefault(os.path.basename(self.runner.resolve_config_path(str(ref))), []).append(task_id)
        return users

    def list_processors(self, **_):
        folder = self.runner.PROCESSORS_DIR
        users = self.processor_users()
        items = []
        if os.path.isdir(folder):
            for name in sorted(os.listdir(folder)):
                path = os.path.join(folder, name)
                if PROCESSOR_NAME.match(name) and os.path.isfile(path):
                    stat = os.stat(path)
                    items.append({"name": name, "size": stat.st_size, "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                                  "used_by": users.get(name, [])})
        return {"folder": folder, "processors": items}

    def get_processor(self, name, **_):
        path = self.processor_path(name)
        if not os.path.isfile(path):
            raise ApiError(404, f"Processor '{name}' not found.")
        return {"name": name, "content": read_text(path), "used_by": self.processor_users().get(name, [])}

    def save_processor(self, name, body, **_):
        path = self.processor_path(name)
        content = self.content_of(body)
        try:
            compile(content, name, "exec")
        except SyntaxError as e:
            raise ApiError(400, f"Syntax error in line {e.lineno}: {e.msg}", {"line": e.lineno, "column": e.offset})
        warnings = []
        if not re.search(r"^def\s+process\s*\(", content, re.MULTILINE):
            warnings.append("No top-level 'def process(df, config)' found, the task would fail.")
        with self.write_lock:
            os.makedirs(self.runner.PROCESSORS_DIR, exist_ok=True)
            self.runner.write_text_atomic(path, content)
        return {"saved": True, "warnings": warnings}

    def delete_processor(self, name, **_):
        path = self.processor_path(name)
        if not os.path.isfile(path):
            raise ApiError(404, f"Processor '{name}' not found.")
        os.remove(path)
        return {"deleted": name}

    # --- audit archives ---------------------------------------------------------------------------

    def audit_path(self, name: str) -> str:
        if not AUDIT_NAME.match(name) or os.path.basename(name) != name:
            raise ApiError(400, "Invalid archive name.")
        path = os.path.join(self.runner.AUDIT_DIR, name)
        if not os.path.isfile(path):
            raise ApiError(404, f"Archive '{name}' not found.")
        return path

    def list_audit(self, query, **_):
        folder = self.runner.AUDIT_DIR
        wanted = (query.get("task") or [None])[0]
        items = []
        if os.path.isdir(folder):
            for name in os.listdir(folder):
                match = AUDIT_NAME.match(name)
                if not match or (wanted and match["task"] != self.runner.safe_name(wanted)):
                    continue
                stamp = datetime.strptime(match["stamp"], "%Y%m%d_%H%M%S")
                items.append({"name": name, "task": match["task"], "time": stamp.isoformat(),
                              "size": os.path.getsize(os.path.join(folder, name))})
        items.sort(key=lambda item: item["time"], reverse=True)
        return {"folder": folder, "archives": items}

    def audit_detail(self, name, **_):
        members = []
        with tarfile.open(self.audit_path(name), "r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                mime = mimetypes.guess_type(member.name)[0] or "application/octet-stream"
                entry = {"name": member.name, "size": member.size, "mime": mime}
                if member.name.endswith(TEXT_SUFFIXES):
                    raw = tar.extractfile(member).read(MAX_INLINE_BYTES + 1)
                    entry["content"] = raw[:MAX_INLINE_BYTES].decode("utf-8", errors="replace")
                    entry["truncated"] = len(raw) > MAX_INLINE_BYTES
                members.append(entry)
        order = {"prompt.txt": 0, "response.md": 1, "kpis.json": 2, "data.json": 3, "data.txt": 3}
        members.sort(key=lambda m: (order.get(m["name"], 9), m["name"]))
        return {"name": name, "members": members}

    def audit_member(self, name, member, **_):
        with tarfile.open(self.audit_path(name), "r:gz") as tar:
            for info in tar.getmembers():
                if info.isfile() and info.name == member:
                    mime = mimetypes.guess_type(member)[0] or "application/octet-stream"
                    return ("bytes", tar.extractfile(info).read(), mime, None)
        raise ApiError(404, f"'{member}' is not part of {name}.")

    def audit_download(self, name, **_):
        path = self.audit_path(name)
        with open(path, "rb") as f:
            return ("bytes", f.read(), "application/gzip", name)

    def delete_audit(self, name, **_):
        os.remove(self.audit_path(name))
        return {"deleted": name}

    @staticmethod
    def content_of(body) -> str:
        if not isinstance(body, dict) or not isinstance(body.get("content"), str):
            raise ApiError(400, "Expected a JSON body like {\"content\": \"...\"}.")
        return body["content"]


class Handler(BaseHTTPRequestHandler):
    app: WebApp = None
    server_version = "HALLMRunner"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        logger.debug("web: " + fmt, *args)

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")

    def do_PUT(self):
        self.handle_request("PUT")

    def do_DELETE(self):
        self.handle_request("DELETE")

    def handle_request(self, method: str):
        try:
            if self.client_address[0] not in ALLOWED_CLIENTS:
                raise ApiError(403, "Access is only allowed through Home Assistant (Ingress).")
            url = urlparse(self.path)
            path = url.path.strip("/")
            body = self.read_body(method)
            result = self.app.dispatch(method, path, parse_qs(url.query), body)
            self.send_result(result)
        except ApiError as e:
            self.close_connection = True
            self.send_json(e.status, {"error": e.message, **e.details})
        except Exception as e:
            logger.exception(f"Web UI request {method} {self.path} failed: {e}")
            self.close_connection = True
            self.send_json(500, {"error": f"{type(e).__name__}: {e}"})

    def read_body(self, method: str):
        length = int(self.headers.get("Content-Length") or 0)
        if method == "GET":
            return None
        # A JSON content type can't be sent cross-site without a CORS preflight, which this server never allows
        if self.headers.get_content_type() != "application/json":
            raise ApiError(415, "Requests that change something must use Content-Type: application/json.")
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "Request body too large.")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError(400, "Request body is not valid JSON.")

    def send_result(self, result):
        if isinstance(result, tuple) and result and result[0] == "file":
            _, path, mime = result
            with open(path, "rb") as f:
                self.send_bytes(200, f.read(), mime)
        elif isinstance(result, tuple) and result and result[0] == "bytes":
            _, data, mime, filename = result
            self.send_bytes(200, data, mime, filename)
        elif isinstance(result, tuple):
            status, payload = result
            self.send_json(status, payload)
        else:
            self.send_json(200, result)

    def send_json(self, status: int, payload):
        self.send_bytes(status, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def send_bytes(self, status: int, data: bytes, mime: str, filename: str | None = None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)


def create_server(runner, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": WebApp(runner)})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def start_web_server(runner, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    server = create_server(runner, host, port)
    threading.Thread(target=server.serve_forever, name="web-ui", daemon=True).start()
    logger.info(f"Web UI listening on port {server.server_address[1]} (open it via the add-on's sidebar entry).")
    return server
