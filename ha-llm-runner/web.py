"""Web UI (Home Assistant Ingress) of the add-on: tasks, llm_tasks.yaml editor, processors and audit archives.

Plain standard library on purpose: no extra dependencies in the image and nothing that can break on Alpine.
The runner module (run.py) is passed in, so storage paths and the task runner have exactly one implementation.
"""
import ast
import copy
import importlib.util
import io
import json
import logging
import mimetypes
import os
import re
import shutil
import string
import tarfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import CommentMark
from ruamel.yaml.scalarstring import FoldedScalarString, LiteralScalarString
from ruamel.yaml.tokens import CommentToken

logger = logging.getLogger("ha_llm_runner.web")

DEFAULT_PORT = 8099
# Only the Supervisor's Ingress proxy (which does the HA login and admin check) and local debugging may connect
ALLOWED_CLIENTS = {"172.30.32.2", "127.0.0.1", "::1", "::ffff:127.0.0.1", "::ffff:172.30.32.2"}
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_INLINE_BYTES = 256 * 1024
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STATES_CACHE_SECONDS = 10
MAX_SEARCH_RESULTS = 50

PROCESSOR_NAME = re.compile(r"^[A-Za-z0-9_\-]+\.py$")
AUDIT_NAME = re.compile(r"^audit_(?P<task>.+)_(?P<stamp>\d{8}_\d{6})\.tar\.gz$")
TEXT_SUFFIXES = (".txt", ".md", ".json", ".csv", ".yaml", ".yml")
KNOWN_TASK_KEYS = {
    "icon", "state_template", "unit_of_measurement", "device_class", "state_class",
    "entities", "files", "urls", "hours", "resample", "data_processor", "processor",
    "prompt", "provider", "model", "temperature", "response_schema", "history_limit",
    "target_sensor", "friendly_name", "audit", "inputs", "validate_response",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details or {}


class BlockDumper(yaml.SafeDumper):
    pass


def task_entity_rows(cfg: dict) -> list[dict]:
    """Everything a task reads (entities, files, urls) with the kind the runner treats it as."""
    rows = []

    def add(section, alias, target):
        target = str(target)
        if target.startswith(("http://", "https://")):
            kind, entity_id, attribute = ("url" if section == "urls" else "file" if section == "files" else "camera"), None, None
        else:
            entity_id, _, attribute = target.partition(":")
            domain = entity_id.split(".", 1)[0]
            kind = domain if domain in ("camera", "calendar") else "entity"
        rows.append({"section": section, "alias": str(alias), "target": target, "kind": kind,
                     "entity_id": entity_id, "attribute": attribute or None})

    entities = cfg.get("entities") or {}
    if isinstance(entities, dict):
        for alias, target in entities.items():
            add("entities", alias, target)
    elif isinstance(entities, list):
        for index, target in enumerate(entities):
            add("entities", index, target)
    for section in ("files", "urls"):
        values = cfg.get(section) or {}
        if isinstance(values, dict):
            for alias, target in values.items():
                add(section, alias, target)
    return rows


def _represent_str(dumper, value):
    # Multi-line prompts read like in llm_tasks.yaml (`prompt: |`) instead of one long quoted string
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


BlockDumper.add_representer(str, _represent_str)


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# A leading digit is fine for str.format ({1og_temp}), only all-digit names ({1}) are positional
ALIAS_PATTERN = re.compile(r"^(?!\d+$)[A-Za-z0-9_]+$")
TASK_ID_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
ENTITY_ID_PATTERN = re.compile(r"^[a-z0-9_]+\.[A-Za-z0-9_]+(:[A-Za-z0-9_]+)?$")
# Names the runner fills itself; an entity with the same alias would silently replace them
RESERVED_ALIASES = {"weekday", "today", "date", "time", "month", "year", "now", "history", "timeseries", "data"}


def round_trip_yaml(text: str) -> YAML:
    """ruamel.yaml in round-trip mode, so comments, quotes, block prompts and the order of keys survive."""
    rt = YAML()
    rt.preserve_quotes = True
    rt.width = 4096
    # Keep the list style of the file: "key:\n  - item" (indented) or "key:\n- item"
    flat_lists = re.search(r"^( *)[^\s#][^\n]*:[ \t]*\n\1- ", text, re.MULTILINE)
    rt.indent(mapping=2, sequence=2 if flat_lists else 4, offset=0 if flat_lists else 2)
    return rt


def _last_leaf(node):
    """Deepest last (container, key) of a task. ruamel keeps blank lines and comments that follow a task there."""
    while isinstance(node, (CommentedMap, CommentedSeq)) and len(node):
        key = list(node.keys())[-1] if isinstance(node, CommentedMap) else len(node) - 1
        if not isinstance(node[key], (CommentedMap, CommentedSeq)) or not len(node[key]):
            return node, key
        node = node[key]
    return None, None


def _is_block(value) -> bool:
    return isinstance(value, (LiteralScalarString, FoldedScalarString))


def _leaf_comment(node, key, token=False):
    slot = 2 if isinstance(node, CommentedMap) else 0
    entry = node.ca.items.get(key)
    if token is not False:
        if entry is None:
            entry = node.ca.items[key] = [None, None, None, None]
        entry[slot] = token
        return None
    return entry[slot] if entry else None


def _take_task_tail(task) -> str:
    """Detaches the blank lines/comments behind the task (e.g. the header comment of the next task)."""
    node, key = _last_leaf(task)
    token = _leaf_comment(node, key) if node is not None else None
    if token is None:
        return ""
    # A block scalar (|, >) has already consumed its line end, everything else still owns it
    cut = 0 if _is_block(node[key]) else token.value.find("\n") + 1
    if cut == 0 and not _is_block(node[key]):
        return ""
    head, tail = token.value[:cut], token.value[cut:]
    if head.strip():
        token.value = head
    else:
        _leaf_comment(node, key, None)
    return tail


def _put_task_tail(task, tail: str):
    node, key = _last_leaf(task)
    if not tail or node is None:
        return
    token = _leaf_comment(node, key)
    if token is not None:
        token.value = (token.value if token.value.endswith("\n") else token.value + "\n") + tail
        return
    block = _is_block(node[key])
    _leaf_comment(node, key, CommentToken(tail if block else "\n" + tail, CommentMark(0), None))


def _load_tasks_document(text: str):
    rt = round_trip_yaml(text)
    try:
        data = rt.load(text) if text.strip() else None
    except Exception as e:
        raise ApiError(400, f"llm_tasks.yaml can't be read, fix it in the llm_tasks.yaml tab first: {e}")
    return rt, data


def _dump_document(rt: YAML, data) -> str:
    out = io.StringIO()
    rt.dump(data, out)
    return out.getvalue()


def new_task_skeleton(task_id: str) -> CommentedMap:
    task = CommentedMap()
    task["prompt"] = LiteralScalarString("Describe the current situation in one short sentence.\n")
    return task


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _task_block(text: str, task_id: str):
    """Where a task sits in llm_tasks.yaml: (lines, key line, first/end body line, body indent, flow value column).

    The body ends before blank lines and comments at the task key's indent (they belong to the next task)."""
    _, data = _load_tasks_document(text)
    tasks = data.get("tasks", data) if isinstance(data, dict) else None
    if not isinstance(tasks, dict) or task_id not in tasks:
        raise ApiError(404, f"Task '{task_id}' not found in llm_tasks.yaml.")
    lines = text.splitlines(keepends=True)
    key_line, key_col = tasks.lc.key(task_id)
    following = [tasks.lc.key(k)[0] for k in tasks if tasks.lc.key(k)[0] > key_line]
    if tasks is not data:
        following += [data.lc.key(k)[0] for k in data if data.lc.key(k)[0] > key_line]
    end = min(following, default=len(lines))
    while end > key_line + 1:
        line = lines[end - 1]
        if not line.strip() or (line.lstrip(" ").startswith("#") and _indent_of(line) <= key_col):
            end -= 1
        else:
            break
    value = tasks[task_id]
    flow_col = None
    if isinstance(value, (CommentedMap, CommentedSeq)) and value.lc.line == key_line:
        flow_col = value.lc.col
    content = [l for l in lines[key_line + 1:end] if l.strip() and not l.lstrip(" ").startswith("#")]
    indent = min((_indent_of(l) for l in content), default=key_col + 2)
    return lines, key_line, key_line + 1, end, indent, flow_col, value


def task_text(text: str, task_id: str) -> str:
    """Everything of a task below its `task_id:` line as written in llm_tasks.yaml, unindented."""
    lines, _, first, end, indent, flow_col, value = _task_block(text, task_id)
    if flow_col is not None:
        block = copy.deepcopy(value)
        block.fa.set_block_style()
        return _dump_document(round_trip_yaml(""), block)
    body = [l[indent:] if l[:indent].strip() == "" and len(l.rstrip("\r\n")) > indent else l.lstrip(" ") for l in lines[first:end]]
    out = "".join(body)
    return out if out.endswith("\n") or not out else out + "\n"


def _parse_task_text(task_text_value: str) -> CommentedMap:
    try:
        task = round_trip_yaml(task_text_value).load(task_text_value) if task_text_value.strip() else None
    except Exception as e:
        problem = _yaml_problem(e)
        where = f"line {problem['line']}: " if problem["line"] else ""
        raise ApiError(400, f"The task is no valid YAML ({where}{problem['message']}).", problem)
    if task is None:
        raise ApiError(400, "A task needs at least one setting or entity.")
    if not isinstance(task, CommentedMap):
        raise ApiError(400, "The task must be 'key: value' lines.")
    return task


def replace_task(text: str, task_id: str, task_text_value: str) -> str:
    """Replaces the lines of a task with the edited text; everything else of the file stays byte for byte."""
    # In the file the task always ends with a line break; "prompt: |" without one would otherwise compare as different
    task_text_value = task_text_value.replace("\r\n", "\n").rstrip("\n") + "\n"
    task = _parse_task_text(task_text_value)
    lines, key_line, first, end, indent, flow_col, _ = _task_block(text, task_id)
    if flow_col is not None:
        key = lines[key_line][:flow_col].rstrip()
        lines[key_line] = key + ("\r\n" if lines[key_line].endswith("\r\n") else "\n")
    newline = "\r\n" if lines[key_line].endswith("\r\n") else "\n"
    body = [(" " * indent + line if line else line) + newline for line in task_text_value.rstrip("\n").split("\n")]
    if end == len(lines) and not lines[key_line].endswith("\n"):
        lines[key_line] += newline
    # Blank separator lines are not part of the editor text; whitespace in them could still extend the last block scalar
    rest = lines[end:]
    for i, line in enumerate(rest):
        if line.strip():
            break
        rest[i] = line[len(line.rstrip("\r\n")):]
    result = "".join(lines[:first] + body + rest)
    _, data = _load_tasks_document(result)
    tasks = data.get("tasks", data) if isinstance(data, dict) else None
    if not isinstance(tasks, dict) or tasks.get(task_id) != task:
        raise ApiError(400, "The task could not be put back into llm_tasks.yaml unchanged, edit it in the llm_tasks.yaml tab.")
    return result


def edit_task_entities(task_text_value: str, change) -> str:
    """Lets change(task_mapping) edit the task text of the editor; comments and layout stay."""
    rt = round_trip_yaml(task_text_value)
    task = _parse_task_text(task_text_value)
    change(task)
    return _dump_document(rt, task)


def _yaml_problem(error) -> dict:
    mark = getattr(error, "problem_mark", None) or getattr(error, "context_mark", None)
    message = getattr(error, "problem", None) or str(error).split("\n")[0]
    return {"line": mark.line + 1 if mark else None, "column": mark.column + 1 if mark else None, "message": message}


def check_yaml_syntax(text: str) -> dict:
    """YAML syntax only (incl. duplicate keys), not the structure of llm_tasks.yaml."""
    try:
        round_trip_yaml(text).load(text)
    except Exception as e:
        return {"valid": False, **_yaml_problem(e)}
    return {"valid": True}

def add_task_to_text(text: str, task_id: str, task: CommentedMap) -> str:
    """Appends a task at the end of the tasks, with a blank line in front of it."""
    rt, data = _load_tasks_document(text)
    if data is None:
        data = CommentedMap()
    if not isinstance(data, dict):
        raise ApiError(400, "llm_tasks.yaml is not a mapping of tasks, fix it in the llm_tasks.yaml tab first.")
    if "tasks" in data and data["tasks"] is None:
        data["tasks"] = CommentedMap()
    tasks = data.get("tasks", data)
    if not isinstance(tasks, dict):
        raise ApiError(400, "'tasks:' is not a mapping, fix it in the llm_tasks.yaml tab first.")
    if task_id in tasks:
        raise ApiError(409, f"A task '{task_id}' already exists.")
    if tasks.fa.flow_style():
        tasks.fa.set_block_style()
    if len(tasks):
        last = tasks[list(tasks)[-1]]
        if isinstance(last, (CommentedMap, CommentedSeq)) and len(last):
            tail = _take_task_tail(last)
            _put_task_tail(last, tail if tail.endswith("\n\n") else tail + "\n")
    tasks[task_id] = task
    return _dump_document(rt, data)


def remove_task_from_text(text: str, task_id: str) -> str:
    """Removes a task including its header comment; the comment in front of the next task stays."""
    rt, data = _load_tasks_document(text)
    tasks = data.get("tasks", data) if isinstance(data, dict) else None
    if not isinstance(tasks, dict) or task_id not in tasks:
        raise ApiError(404, f"Task '{task_id}' not found in llm_tasks.yaml.")
    keys = list(tasks)
    index = keys.index(task_id)
    removed = tasks[task_id]
    # The removed task carries the blank lines and the header comment of the next task
    tail = _take_task_tail(removed) if isinstance(removed, (CommentedMap, CommentedSeq)) else ""
    del tasks[task_id]
    if index > 0:
        previous = tasks[keys[index - 1]]
        if isinstance(previous, (CommentedMap, CommentedSeq)) and len(previous):
            _take_task_tail(previous)
            _put_task_tail(previous, tail)
    elif len(tasks):
        # Comments in front of the first task: a group directly attached to it is its header and goes with it,
        # groups separated by a blank line (like a file header) stay
        before = tasks.ca.comment[1] if tasks.ca.comment and tasks.ca.comment[1] else []
        while before and not before[-1].value.endswith("\n\n"):
            before.pop()
        header = tail.lstrip("\n")
        if header.strip():
            text_part = header.lstrip(" ")
            if not tasks.ca.comment:
                tasks.ca.comment = [None, []]
            elif tasks.ca.comment[1] is None:
                tasks.ca.comment[1] = []
            before = tasks.ca.comment[1]
            before.append(CommentToken(text_part, CommentMark(len(header) - len(text_part)), None))
    return _dump_document(rt, data)


class _ImportCollector(ast.NodeVisitor):
    """Absolute imports outside try blocks (a try usually guards an optional import)."""

    def __init__(self):
        self.imports = []

    def visit_Try(self, node):
        for child in node.finalbody + getattr(node, "orelse", []):
            self.visit(child)

    visit_TryStar = visit_Try

    def visit_Import(self, node):
        self.imports += [(alias.name.split(".")[0], node.lineno) for alias in node.names]

    def visit_ImportFrom(self, node):
        if not node.level and node.module:
            self.imports.append((node.module.split(".")[0], node.lineno))


def check_processor(content: str, name: str) -> dict:
    """Compiles a processor and checks process(df, config) and its imports, without running any of it."""
    try:
        compile(content, name, "exec")
        tree = ast.parse(content, name)
    except SyntaxError as e:
        return {"valid": False, "syntax_error": True,
                "errors": [{"line": e.lineno, "column": e.offset, "message": f"Syntax error: {e.msg}"}]}
    errors = []
    process = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "process"), None)
    if process is None:
        errors.append({"line": None, "message": "No top-level 'def process(df, config)' found, the task would fail."})
    elif isinstance(process, ast.AsyncFunctionDef):
        errors.append({"line": process.lineno, "message": "'process' must be a plain function, not 'async def'."})
    elif len(process.args.posonlyargs) + len(process.args.args) < 2 and not process.args.vararg:
        errors.append({"line": process.lineno, "message": "'process' must accept two arguments: process(df, config)."})
    collector = _ImportCollector()
    collector.visit(tree)
    seen = set()
    for module, line in collector.imports:
        if module in seen:
            continue
        seen.add(module)
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            errors.append({"line": line, "message": f"Module '{module}' is not installed in the add-on."})
    errors.sort(key=lambda e: e["line"] or 0)
    return {"valid": not errors, "syntax_error": False, "errors": errors}


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
        try:
            runner.validate_inputs_config(cfg)
        except ValueError as e:
            errors.append({"line": None, "message": f"Task '{task_id}': invalid input/response policy: {e}"})
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
        if not prompt:
            entities = cfg.get("entities")
            pairs = entities.items() if isinstance(entities, dict) else (
                [(v, v) for v in entities] if isinstance(entities, list) else [])
            unused = [str(k) for k in (cfg.get("files") or {})] if isinstance(cfg.get("files"), dict) else []
            unused += [str(alias) for alias, v in pairs if str(v).startswith(("camera.", "http"))]
            if unused:
                warnings.append(f"Task '{task_id}': no prompt, so no LLM is called and the images/files "
                                f"{', '.join(unused)} are not used. Add a prompt or remove them.")
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
        # Read-modify-write of llm_tasks.yaml by Add/Remove; write_lock is taken inside it for the file write
        self.edit_lock = threading.Lock()
        self.states_lock = threading.Lock()
        self.states_cache = None
        self.routes = [
            ("GET", r"", self.index),
            ("GET", r"index\.html", self.index),
            ("GET", r"assets/(?P<name>[A-Za-z0-9_-]+\.(?:js|css))", self.frontend_asset),
            ("GET", r"api/overview", self.overview),
            ("POST", r"api/run-all", self.run_all),
            ("POST", r"api/tasks", self.create_task),
            ("GET", r"api/tasks/(?P<task_id>[^/]+)", self.task_detail),
            ("DELETE", r"api/tasks/(?P<task_id>[^/]+)", self.delete_task),
            ("PUT", r"api/tasks/(?P<task_id>[^/]+)/settings", self.save_task_settings),
            ("POST", r"api/yaml/check", self.check_yaml),
            ("POST", r"api/tasks/(?P<task_id>[^/]+)/run", self.run_task),
            ("POST", r"api/tasks/(?P<task_id>[^/]+)/preview", self.preview_task),
            ("DELETE", r"api/tasks/(?P<task_id>[^/]+)/memory", self.clear_memory),
            ("POST", r"api/yaml/entities", self.edit_entities),
            ("GET", r"api/entities", self.search_entities),
            ("GET", r"api/config", self.get_config),
            ("PUT", r"api/config", self.save_config),
            ("POST", r"api/config/validate", self.validate_config),
            ("GET", r"api/processors", self.list_processors),
            ("GET", r"api/processors/(?P<name>[^/]+)", self.get_processor),
            ("PUT", r"api/processors/(?P<name>[^/]+)", self.save_processor),
            ("POST", r"api/processors/(?P<name>[^/]+)/validate", self.validate_processor),
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

    def index(self, query=None, **_):
        path = os.path.join(STATIC_DIR, "svelte", "index.html")
        if not os.path.isfile(path):
            raise ApiError(503, "The Svelte interface has not been built. Run npm ci and npm run build in frontend.")
        return ("file", path, "text/html; charset=utf-8")

    def frontend_asset(self, name, **_):
        path = os.path.join(STATIC_DIR, "svelte", "assets", name)
        if not os.path.isfile(path):
            raise ApiError(404, "Frontend asset not found.")
        mime = "text/javascript" if name.endswith(".js") else "text/css"
        return ("file", path, mime + "; charset=utf-8")

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
            "settings_text": self.task_settings_as_written(task_id, cfg),
            "status": self.runner.task_status(task_id),
            "memory": self.runner.load_memory(task_id),
        }

    def task_settings_as_written(self, task_id: str, cfg: dict) -> str:
        """The whole task as written in llm_tasks.yaml (comments, block prompts, quotes), without the `task_id:` line."""
        try:
            return task_text(read_text(self.runner.TASKS_CONFIG_PATH), task_id)
        except Exception:
            return yaml.dump(cfg, Dumper=BlockDumper, allow_unicode=True, sort_keys=False, width=1000) if cfg else ""

    def save_task_settings(self, task_id, body, **_):
        if not os.path.isfile(self.runner.TASKS_CONFIG_PATH):
            raise ApiError(404, "llm_tasks.yaml does not exist yet.")
        content = self.content_of(body)
        return self.change_tasks_text(lambda text: replace_task(text, task_id, content))

    def check_yaml(self, body, **_):
        content = self.content_of(body)
        result = check_yaml_syntax(content)
        if body.get("entities"):
            cfg = yaml.safe_load(content) if result["valid"] and content.strip() else None
            result["entities"] = self.entity_rows(cfg) if isinstance(cfg, dict) else None
        return result

    def run_task(self, task_id, body=None, **_):
        cfg = self.task_or_404(task_id)
        try:
            thread = self.runner.run_task_async(task_id, cfg, self.runner._mqtt_client, self.runner.load_options(), body)
        except (ValueError, OSError) as e:
            raise ApiError(400, str(e)) from e
        if thread is None:
            return 200, {"queued": [], "duplicate": True}
        return 202, {"queued": [task_id]}

    def preview_task(self, task_id, body, **_):
        """Runs the unsaved task text once; the checks are the same as for saving, but nothing is written."""
        self.task_or_404(task_id)
        content = replace_task(read_text(self.runner.TASKS_CONFIG_PATH), task_id, self.content_of(body))
        validation = validate_tasks_text(self.runner, content)
        if not validation["valid"]:
            raise ApiError(400, "The task was not run because it contains errors.", validation)
        cfg = self.runner.tasks_from_config(yaml.safe_load(content))[task_id]
        return {**self.runner.preview_task(task_id, cfg, self.runner.load_options()), "warnings": validation["warnings"]}

    def run_all(self, **_):
        self.runner.run_all_tasks_async(self.runner._mqtt_client, self.runner.load_options())
        return 202, {"queued": list(self.tasks())}

    def clear_memory(self, task_id, **_):
        self.task_or_404(task_id)
        self.runner.clear_memory(task_id)
        return {"cleared": task_id}

    # --- Home Assistant entities ------------------------------------------------------------------

    def ha_states(self) -> dict:
        """entity_id -> state object, cached briefly so typing in the search does not hammer Home Assistant."""
        now = time.monotonic()
        with self.states_lock:
            if self.states_cache is None or now - self.states_cache[0] > STATES_CACHE_SECONDS:
                try:
                    states = self.runner.fetch_ha_states()
                except Exception as e:
                    raise ApiError(502, f"Home Assistant is not reachable: {e}")
                self.states_cache = (now, {s.get("entity_id"): s for s in states if s.get("entity_id")})
            return self.states_cache[1]

    def entity_rows(self, cfg: dict) -> dict:
        """The entities, files and urls of a task with name and current value from Home Assistant."""
        rows = task_entity_rows(cfg)
        error = None
        states = {}
        if any(row["entity_id"] for row in rows):
            try:
                states = self.ha_states()
            except ApiError as e:
                error = e.message
        for row in rows:
            if not row["entity_id"] or error:
                continue
            state = states.get(row["entity_id"])
            if state is None:
                row["missing"] = True
                continue
            attributes = state.get("attributes") or {}
            row["name"] = attributes.get("friendly_name")
            row["state"] = attributes.get(row["attribute"], state.get("state")) if row["attribute"] else state.get("state")
            row["unit"] = None if row["attribute"] else attributes.get("unit_of_measurement")
        return {"rows": rows, "error": error, "list_format": isinstance(cfg.get("entities"), list)}

    def search_entities(self, query, **_):
        words = " ".join(query.get("q") or []).lower().split()
        results = []
        for entity_id, state in sorted(self.ha_states().items()):
            attributes = state.get("attributes") or {}
            name = str(attributes.get("friendly_name") or "")
            haystack = f"{entity_id} {name}".lower()
            if all(word in haystack for word in words):
                results.append({
                    "entity_id": entity_id,
                    "name": name,
                    "state": state.get("state"),
                    "unit": attributes.get("unit_of_measurement"),
                })
        return {"entities": results[:MAX_SEARCH_RESULTS], "total": len(results)}

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
        self.write_tasks_file(content)
        return {**validation, "saved": True, "mqtt_synced": self.runner._mqtt_client is not None}

    def write_tasks_file(self, content: str):
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

    def change_tasks_text(self, transform) -> dict:
        """Read-modify-write of llm_tasks.yaml with a validation of the result and a .bak of the old file."""
        path = self.runner.TASKS_CONFIG_PATH
        with self.edit_lock:
            content = transform(read_text(path) if os.path.isfile(path) else "")
            validation = validate_tasks_text(self.runner, content)
            if not validation["valid"]:
                raise ApiError(400, "llm_tasks.yaml was not changed because the result would contain errors.", validation)
            self.write_tasks_file(content)
        return {"saved": True, "warnings": validation["warnings"]}

    def create_task(self, body, **_):
        task_id = str((body or {}).get("id") or "").strip()
        if not TASK_ID_PATTERN.match(task_id):
            raise ApiError(400, "The task ID may only contain lowercase letters, digits and _ (max. 64 characters).")
        result = self.change_tasks_text(lambda text: add_task_to_text(text, task_id, new_task_skeleton(task_id)))
        return 201, {**result, "id": task_id}

    def delete_task(self, task_id, **_):
        runner = self.runner
        self.task_or_404(task_id)
        if runner.task_busy(task_id) or runner.task_status(task_id).get("state") in ("queued", "running"):
            raise ApiError(409, f"Task '{task_id}' is running right now, try again when it is done.")
        result = self.change_tasks_text(lambda text: remove_task_from_text(text, task_id))
        runner.clear_memory(task_id)
        archives = 0
        if os.path.isdir(runner.AUDIT_DIR):
            for name in os.listdir(runner.AUDIT_DIR):
                match = AUDIT_NAME.match(name)
                if match and match["task"] == runner.safe_name(task_id):
                    os.remove(os.path.join(runner.AUDIT_DIR, name))
                    archives += 1
        runner.TASK_STATUS.pop(task_id, None)
        return {**result, "deleted": task_id, "audit_archives_deleted": archives}

    def edit_entities(self, body, **_):
        """Adds or removes an entity in the task text of the editor; saving is up to the user."""
        content = self.content_of(body)
        if isinstance(body.get("add"), dict):
            change = self.adding_entity(str(body["add"].get("alias") or "").strip(), str(body["add"].get("entity_id") or "").strip())
        elif body.get("remove") is not None:
            change = self.removing_entity(str(body["remove"]), str(body.get("section") or "entities"))
        else:
            raise ApiError(400, "Expected 'add' or 'remove'.")
        return {"content": edit_task_entities(content, change)}

    @staticmethod
    def adding_entity(alias: str, entity_id: str):
        if not ENTITY_ID_PATTERN.match(entity_id):
            raise ApiError(400, f"'{entity_id}' is not a valid entity ID (like sensor.my_sensor).")

        def change(task):
            entities = task.get("entities")
            if isinstance(entities, list):
                if entity_id in map(str, entities):
                    raise ApiError(409, f"{entity_id} is already used by this task.")
                entities.append(entity_id)
                return
            if not ALIAS_PATTERN.match(alias):
                raise ApiError(400, "The alias may only contain letters, digits and _ and must not be a plain number.")
            if alias in RESERVED_ALIASES:
                raise ApiError(400, f"'{alias}' is a built-in placeholder, choose another alias.")
            if entities is None:
                entities = CommentedMap()
                # Entities usually come before the prompt
                position = list(task).index("prompt") if "prompt" in task else len(task)
                if "entities" in task:
                    task["entities"] = entities
                else:
                    task.insert(position, "entities", entities)
            elif not isinstance(entities, dict):
                raise ApiError(400, "'entities' of this task is neither a mapping nor a list.")
            if alias in entities:
                raise ApiError(409, f"The alias '{alias}' is already used by this task.")
            if entity_id in map(str, entities.values()):
                raise ApiError(409, f"{entity_id} is already used by this task.")
            entities[alias] = entity_id

        return change

    @staticmethod
    def removing_entity(alias: str, section: str = "entities"):
        if section not in ("entities", "files", "urls"):
            raise ApiError(400, "'section' must be entities, files or urls.")

        def change(task):
            entities = task.get(section)
            if isinstance(entities, dict) and alias in entities:
                del entities[alias]
            elif section == "entities" and isinstance(entities, list) and alias.isdigit() and int(alias) < len(entities):
                del entities[int(alias)]
            else:
                raise ApiError(404, f"'{alias}' is not in the {section} of this task.")
            if not entities:
                del task[section]

        return change

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
        check = check_processor(content, name)
        if check["syntax_error"]:
            error = check["errors"][0]
            raise ApiError(400, f"Syntax error in line {error['line']}: {error['message'].removeprefix('Syntax error: ')}",
                           {"line": error["line"], "column": error["column"]})
        with self.write_lock:
            os.makedirs(self.runner.PROCESSORS_DIR, exist_ok=True)
            self.runner.write_text_atomic(path, content)
        # Saved anyway, so work in progress is not lost; the task would fail until these are fixed
        return {"saved": True, "warnings": [f"Line {e['line']}: {e['message']}" if e["line"] else e["message"] for e in check["errors"]]}

    def validate_processor(self, name, body, **_):
        self.processor_path(name)
        return check_processor(self.content_of(body), name)

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
