import json
import queue
import re
import shlex
import subprocess
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
LOG_DIR = ROOT / "logs"
TASK_DIR = ROOT / "tasks"
RUN_LOG_DIR = LOG_DIR / "runs"
MODEL_DIR = ROOT / "models"
SKILL_DIR = ROOT / "skills"
APP_LOG = LOG_DIR / "app.log"
IM_LOG = LOG_DIR / "im.log"
TASK_INDEX = TASK_DIR / "tasks_index.json"
PERM_FILE = CONFIG_DIR / "permissions.json"
IM_FILE = CONFIG_DIR / "im_config.json"
APP_CFG_FILE = CONFIG_DIR / "app.json"
SKILL_FILE = CONFIG_DIR / "skills.json"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_dirs() -> None:
    for path in [CONFIG_DIR, LOG_DIR, TASK_DIR, RUN_LOG_DIR, MODEL_DIR, SKILL_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def append_log(file_path: Path, message: str) -> None:
    ensure_dirs()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "a", encoding="utf-8") as handle:
        handle.write(f"{now_iso()} | {message}\n")


def read_tail(file_path: Path, lines: int = 100) -> str:
    if not file_path.exists():
        return ""
    with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
        all_lines = handle.readlines()
    return "".join(all_lines[-lines:])


def load_json(file_path: Path, default_value: Dict[str, Any]) -> Dict[str, Any]:
    if not file_path.exists():
        return default_value
    try:
        with open(file_path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except Exception:
        return default_value


def save_json(file_path: Path, payload: Dict[str, Any]) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


class SecurityPolicy:
    def __init__(self) -> None:
        self.default = {
            "allow_commands": ["echo", "dir", "python", "ipconfig", "ping", "type", "whoami"],
            "deny_keywords": ["rm -rf", "format", "shutdown", "reboot", "del /f /s", "rd /s /q", "mkfs"],
        }
        self.reload()

    def reload(self) -> None:
        self.policy = load_json(PERM_FILE, self.default)
        if not PERM_FILE.exists():
            save_json(PERM_FILE, self.policy)

    def check_command(self, command: str) -> None:
        cmd = command.lower().strip()
        for item in self.policy.get("deny_keywords", []):
            if item in cmd:
                raise RuntimeError(f"blocked command keyword: {item}")
        parts = shlex.split(command, posix=False)
        first = parts[0].lower() if parts else ""
        allow = [x.lower() for x in self.policy.get("allow_commands", [])]
        if allow and first not in allow:
            raise RuntimeError(f"command not in allowlist: {first}")


class ModelManager:
    def __init__(self) -> None:
        self.default_cfg = {
            "model_api_url": "http://127.0.0.1:11434/v1",
            "model_name": "local-model",
            "started": False,
            "active_model_file": "",
            "model_checks": {},
            "runtime_host": "127.0.0.1",
            "runtime_port": 11434,
            "llama_server_path": str((ROOT / "tools" / "llama-server.exe").resolve()),
        }
        self.cfg = load_json(APP_CFG_FILE, self.default_cfg)
        for k, v in self.default_cfg.items():
            self.cfg.setdefault(k, v)
        if not APP_CFG_FILE.exists():
            save_json(APP_CFG_FILE, self.cfg)
        self.lock = threading.Lock()
        self.downloads: Dict[str, Dict[str, Any]] = {}
        self.runtime_process: Optional[subprocess.Popen] = None
        self.catalog = [
            {
                "id": "qwen2.5-1.5b-q4",
                "name": "Qwen2.5 1.5B",
                "filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
                "url": "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf",
                "size_gb": 1.5,
                "vram_gb": 4,
                "recommended": "低配",
            },
            {
                "id": "phi-2-q4",
                "name": "Phi-2",
                "filename": "phi-2.Q4_K_M.gguf",
                "url": "https://huggingface.co/TheBloke/phi-2-GGUF/resolve/main/phi-2.Q4_K_M.gguf",
                "size_gb": 1.7,
                "vram_gb": 4,
                "recommended": "低配",
            },
            {
                "id": "tinyllama-1.1b-q4",
                "name": "TinyLlama 1.1B",
                "filename": "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
                "url": "https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF/resolve/main/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
                "size_gb": 0.8,
                "vram_gb": 2,
                "recommended": "低配",
            },
            {
                "id": "qwen2.5-7b-q4",
                "name": "Qwen2.5 7B",
                "filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
                "url": "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q4_k_m.gguf",
                "size_gb": 4.5,
                "vram_gb": 8,
                "recommended": "中配",
            },
            {
                "id": "llama-3.1-8b-q4",
                "name": "Llama 3.1 8B",
                "filename": "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
                "url": "https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
                "size_gb": 5.0,
                "vram_gb": 8,
                "recommended": "中配",
            },
        ]

    def _models_url(self) -> str:
        return f"{self.cfg['model_api_url'].rstrip('/')}/models"

    def _save_cfg(self) -> None:
        save_json(APP_CFG_FILE, self.cfg)

    def _local_path(self, filename: str) -> Path:
        return (MODEL_DIR / filename).resolve()

    def _catalog_item(self, model_id: str) -> Optional[Dict[str, Any]]:
        for item in self.catalog:
            if item["id"] == model_id:
                return dict(item)
        return None

    def health(self) -> bool:
        try:
            response = requests.get(self._models_url(), timeout=2)
            return response.status_code == 200
        except Exception:
            return False

    def local_models(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for f in sorted(MODEL_DIR.glob("*.gguf")):
            items.append(
                {
                    "filename": f.name,
                    "path": str(f),
                    "size_bytes": f.stat().st_size,
                    "is_active": f.name == self.cfg.get("active_model_file", ""),
                }
            )
        return items

    def catalog_with_state(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for item in self.catalog:
            row = dict(item)
            p = self._local_path(item["filename"])
            row["downloaded"] = p.exists()
            row["is_active"] = item["filename"] == self.cfg.get("active_model_file", "")
            out.append(row)
        return out

    def download_jobs(self) -> List[Dict[str, Any]]:
        with self.lock:
            return sorted(self.downloads.values(), key=lambda x: x.get("created_at", ""), reverse=True)

    def activate_model(self, filename: str, model_name: str = "") -> Dict[str, Any]:
        target = self._local_path(filename)
        if not target.exists():
            raise RuntimeError("model file not found")
        self.cfg["active_model_file"] = filename
        self.cfg["model_name"] = model_name or target.stem
        self._save_cfg()
        append_log(APP_LOG, f"model_activated | {filename}")
        return self.status()

    def _record_model_check(self, filename: str, ok: bool, message: str) -> None:
        checks = self.cfg.get("model_checks", {})
        if not isinstance(checks, dict):
            checks = {}
        checks[filename] = {"ok": bool(ok), "message": message, "checked_at": now_iso()}
        self.cfg["model_checks"] = checks
        self._save_cfg()

    def self_check_active_model(self) -> Dict[str, Any]:
        active = str(self.cfg.get("active_model_file", "")).strip()
        if not active:
            raise RuntimeError("no active model")
        try:
            if not self.cfg.get("started"):
                self.start()
            # Keep test minimal for speed and stability.
            payload = {
                "model": self.cfg.get("model_name", "local-model"),
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
                "temperature": 0.0,
            }
            response = requests.post(
                f"{self.cfg['model_api_url'].rstrip('/')}/chat/completions",
                json=payload,
                timeout=15,
            )
            ok = response.status_code == 200
            msg = "本地推理自检通过" if ok else f"推理接口异常: HTTP {response.status_code}"
            self._record_model_check(active, ok, msg)
            return {"ok": ok, "message": msg}
        except Exception as exc:
            msg = f"推理自检失败: {exc}"
            self._record_model_check(active, False, msg)
            return {"ok": False, "message": msg}

    def _download_worker(self, job_id: str, url: str, target: Path, auto_activate: bool, model_name: str) -> None:
        temp = target.with_suffix(target.suffix + ".part")
        with self.lock:
            self.downloads[job_id]["status"] = "downloading"
        try:
            response = requests.get(url, stream=True, timeout=30)
            if response.status_code >= 400:
                raise RuntimeError(f"http {response.status_code}")
            total = int(response.headers.get("content-length", "0") or 0)
            downloaded = 0
            t0 = time.time()
            temp.parent.mkdir(parents=True, exist_ok=True)
            with open(temp, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 512):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    progress = int(downloaded * 100 / total) if total > 0 else 0
                    elapsed = max(time.time() - t0, 0.001)
                    speed_bps = downloaded / elapsed
                    eta_seconds = int((total - downloaded) / speed_bps) if total > 0 and speed_bps > 1 else -1
                    with self.lock:
                        job = self.downloads.get(job_id, {})
                        job["downloaded_bytes"] = downloaded
                        job["total_bytes"] = total
                        job["progress"] = progress
                        job["speed_bps"] = int(speed_bps)
                        job["eta_seconds"] = eta_seconds
            temp.replace(target)
            with self.lock:
                job = self.downloads.get(job_id, {})
                job["status"] = "success"
                job["progress"] = 100
                job["speed_bps"] = 0
                job["eta_seconds"] = 0
            if auto_activate:
                self.activate_model(target.name, model_name=model_name)
                # Auto deploy: if model service is already started, reload it with the new model.
                self.stop()
                self.start()
                check = self.self_check_active_model()
                with self.lock:
                    job = self.downloads.get(job_id, {})
                    job["self_check"] = check
            append_log(APP_LOG, f"model_download_success | {target.name}")
        except Exception as exc:
            with self.lock:
                job = self.downloads.get(job_id, {})
                job["status"] = "failed"
                job["error"] = str(exc)
                job["speed_bps"] = 0
                job["eta_seconds"] = -1
            append_log(APP_LOG, f"model_download_error | {target.name} | {exc}")
            try:
                if temp.exists():
                    temp.unlink()
            except Exception:
                pass

    def start_download(
        self,
        url: str,
        filename: str,
        auto_activate: bool = True,
        model_name: str = "",
    ) -> Dict[str, Any]:
        if not url.startswith("http://") and not url.startswith("https://"):
            raise RuntimeError("download url must start with http/https")
        safe_name = Path(filename).name.strip()
        if not safe_name:
            raise RuntimeError("invalid filename")
        target = self._local_path(safe_name)
        job_id = f"dl_{uuid.uuid4().hex[:8]}"
        job = {
            "id": job_id,
            "url": url,
            "filename": safe_name,
            "status": "queued",
            "progress": 0,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "speed_bps": 0,
            "eta_seconds": -1,
            "retry_count": 0,
            "self_check": None,
            "error": "",
            "created_at": now_iso(),
        }
        with self.lock:
            self.downloads[job_id] = job
        worker = threading.Thread(
            target=self._download_worker,
            args=(job_id, url, target, auto_activate, model_name),
            daemon=True,
        )
        worker.start()
        return job

    def retry_download(self, job_id: str) -> Dict[str, Any]:
        with self.lock:
            old = self.downloads.get(job_id)
        if not old:
            raise RuntimeError("download job not found")
        new_job = self.start_download(
            url=str(old.get("url", "")),
            filename=str(old.get("filename", "")),
            auto_activate=True,
            model_name=Path(str(old.get("filename", ""))).stem,
        )
        with self.lock:
            self.downloads[new_job["id"]]["retry_count"] = int(old.get("retry_count", 0)) + 1
        return new_job

    def _start_local_runtime(self, model_path: Path) -> Dict[str, Any]:
        exe = Path(self.cfg.get("llama_server_path", "")).resolve()
        if not exe.exists():
            raise RuntimeError(f"llama-server not found: {exe}")
        self.stop()
        host = str(self.cfg.get("runtime_host", "127.0.0.1"))
        port = int(self.cfg.get("runtime_port", 11434))
        cmd = [str(exe), "-m", str(model_path), "--host", host, "--port", str(port)]
        log_file = LOG_DIR / "llama_server.log"
        out = open(log_file, "a", encoding="utf-8")
        self.runtime_process = subprocess.Popen(cmd, cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT)
        self.cfg["model_api_url"] = f"http://{host}:{port}/v1"
        self.cfg["started"] = True
        self._save_cfg()
        time.sleep(0.8)
        return self.status()

    def start(self) -> Dict[str, Any]:
        active = str(self.cfg.get("active_model_file", "")).strip()
        if not active:
            locals_ = self.local_models()
            if not locals_:
                raise RuntimeError("no local model file, please download first")
            active = locals_[0]["filename"]
            self.cfg["active_model_file"] = active
            self.cfg["model_name"] = Path(active).stem
            self._save_cfg()
        model_path = self._local_path(active)
        if model_path.exists():
            try:
                return self._start_local_runtime(model_path)
            except Exception as exc:
                append_log(APP_LOG, f"local_runtime_fallback | {exc}")
        self.cfg["started"] = True
        self._save_cfg()
        return self.status()

    def stop(self) -> Dict[str, Any]:
        if self.runtime_process and self.runtime_process.poll() is None:
            try:
                self.runtime_process.terminate()
                self.runtime_process.wait(timeout=3)
            except Exception:
                try:
                    self.runtime_process.kill()
                except Exception:
                    pass
        self.runtime_process = None
        self.cfg["started"] = False
        self._save_cfg()
        return self.status()

    def status(self) -> Dict[str, Any]:
        reachable = self.health() if self.cfg.get("started") else False
        active = str(self.cfg.get("active_model_file", "")).strip()
        check = self.cfg.get("model_checks", {}).get(active, None) if active else None
        return {
            "started": bool(self.cfg.get("started")),
            "reachable": reachable,
            "url": self.cfg["model_api_url"],
            "model_name": self.cfg.get("model_name", ""),
            "active_model_file": active,
            "active_model_exists": bool(active and self._local_path(active).exists()),
            "local_models_count": len(self.local_models()),
            "active_model_check": check,
        }


class TaskEngine:
    def __init__(self, policy: SecurityPolicy) -> None:
        self.policy = policy
        self.lock = threading.Lock()
        self.listeners: List[Callable[[Dict[str, Any]], None]] = []
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.runs: Dict[str, Dict[str, Any]] = {}
        self.stop_events: Dict[str, threading.Event] = {}
        self._load_tasks()

    def add_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        self.listeners.append(callback)

    def _notify(self, run_item: Dict[str, Any]) -> None:
        for callback in self.listeners:
            try:
                callback(run_item)
            except Exception as exc:
                append_log(APP_LOG, f"listener_error | {exc}")

    def _load_tasks(self) -> None:
        data = load_json(TASK_INDEX, {"items": []})
        for item in data.get("items", []):
            self.tasks[item["id"]] = item

    def _save_tasks(self) -> None:
        save_json(TASK_INDEX, {"items": list(self.tasks.values())})

    def list_tasks(self) -> List[Dict[str, Any]]:
        with self.lock:
            return sorted(self.tasks.values(), key=lambda x: x.get("created_at", ""), reverse=True)

    def list_runs(self) -> List[Dict[str, Any]]:
        with self.lock:
            return sorted(self.runs.values(), key=lambda x: x.get("started_at", ""), reverse=True)

    def recent_runs(self, limit: int = 5) -> List[Dict[str, Any]]:
        return self.list_runs()[:limit]

    def create_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        task_id = payload.get("id") or f"task_{uuid.uuid4().hex[:8]}"
        item = {
            "id": task_id,
            "name": payload.get("name") or task_id,
            "steps": payload.get("steps", []),
            "retry": int(payload.get("retry", 0)),
            "created_at": now_iso(),
        }
        with self.lock:
            self.tasks[task_id] = item
            self._save_tasks()
        return item

    def _log_run(self, run_id: str, message: str) -> None:
        append_log(RUN_LOG_DIR / f"{run_id}.log", message)

    def run_task(self, task_id: str, trigger: str = "ui") -> Dict[str, Any]:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                raise RuntimeError("task not found")
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        run_item = {
            "id": run_id,
            "task_id": task_id,
            "task_name": task["name"],
            "trigger": trigger,
            "status": "running",
            "started_at": now_iso(),
            "ended_at": None,
            "error": "",
            "log_file": str((RUN_LOG_DIR / f"{run_id}.log").relative_to(ROOT)),
        }
        stop_event = threading.Event()
        self.stop_events[run_id] = stop_event
        with self.lock:
            self.runs[run_id] = run_item

        worker = threading.Thread(target=self._execute_run, args=(run_id, task, stop_event), daemon=True)
        worker.start()
        return run_item

    def stop_run(self, run_id: str) -> Dict[str, Any]:
        event = self.stop_events.get(run_id)
        if not event:
            raise RuntimeError("run not found")
        event.set()
        with self.lock:
            run_item = self.runs.get(run_id)
        if not run_item:
            raise RuntimeError("run not found")
        return run_item

    def get_run_logs(self, run_id: str, lines: int = 120) -> str:
        return read_tail(RUN_LOG_DIR / f"{run_id}.log", lines=lines)

    def _step_command(self, command: str, stop_event: threading.Event, run_id: str) -> None:
        self.policy.check_command(command)
        self._log_run(run_id, f"command | {command}")
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None
        while True:
            if stop_event.is_set():
                process.terminate()
                raise RuntimeError("run stopped by user")
            line = process.stdout.readline()
            if line:
                self._log_run(run_id, line.rstrip())
            if process.poll() is not None:
                break
        for line in process.stdout.readlines():
            self._log_run(run_id, line.rstrip())
        if process.returncode != 0:
            raise RuntimeError(f"command failed with code {process.returncode}")

    def _step_file_write(self, step: Dict[str, Any], run_id: str) -> None:
        rel = step.get("path", "tasks/output.txt")
        target = (ROOT / rel).resolve()
        if not str(target).startswith(str(ROOT)):
            raise RuntimeError("write path out of project scope")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(step.get("content", ""), encoding="utf-8")
        self._log_run(run_id, f"file_write | {target}")

    def _step_file_copy(self, step: Dict[str, Any], run_id: str) -> None:
        src = (ROOT / step.get("src", "")).resolve()
        dst = (ROOT / step.get("dst", "")).resolve()
        if not str(src).startswith(str(ROOT)) or not str(dst).startswith(str(ROOT)):
            raise RuntimeError("copy path out of project scope")
        if not src.exists():
            raise RuntimeError("source not found")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
        self._log_run(run_id, f"file_copy | {src} -> {dst}")

    def _execute_run(self, run_id: str, task: Dict[str, Any], stop_event: threading.Event) -> None:
        retry = int(task.get("retry", 0))
        last_error = ""
        for attempt in range(retry + 1):
            try:
                self._log_run(run_id, f"run_start | task={task['name']} | attempt={attempt + 1}")
                for step in task.get("steps", []):
                    if stop_event.is_set():
                        raise RuntimeError("run stopped by user")
                    step_type = step.get("type")
                    if step_type == "command":
                        self._step_command(step.get("value", ""), stop_event, run_id)
                    elif step_type == "file_write":
                        self._step_file_write(step, run_id)
                    elif step_type == "file_copy":
                        self._step_file_copy(step, run_id)
                    elif step_type == "open_url":
                        url = step.get("value", "")
                        self._log_run(run_id, f"open_url | {url}")
                        webbrowser.open(url)
                    elif step_type == "sleep":
                        duration = float(step.get("value", 1))
                        t0 = time.time()
                        while time.time() - t0 < duration:
                            if stop_event.is_set():
                                raise RuntimeError("run stopped by user")
                            time.sleep(0.2)
                    else:
                        raise RuntimeError(f"unknown step type: {step_type}")
                with self.lock:
                    run_item = self.runs[run_id]
                    run_item["status"] = "success"
                    run_item["ended_at"] = now_iso()
                    run_item["error"] = ""
                self._log_run(run_id, "run_end | success")
                self._notify(self.runs[run_id])
                return
            except Exception as exc:
                last_error = str(exc)
                self._log_run(run_id, f"run_error | {last_error}")
                if attempt < retry:
                    self._log_run(run_id, "retrying...")
                    time.sleep(0.6)
                    continue
        with self.lock:
            run_item = self.runs[run_id]
            run_item["status"] = "stopped" if "stopped" in last_error.lower() else "failed"
            run_item["ended_at"] = now_iso()
            run_item["error"] = last_error
        self._notify(self.runs[run_id])


class LLMTaskGenerator:
    def __init__(self, model_manager: ModelManager) -> None:
        self.model_manager = model_manager

    def _fallback(self, instruction: str) -> Dict[str, Any]:
        text = instruction.lower()
        steps: List[Dict[str, Any]] = []
        if "http://" in text or "https://" in text or "打开" in text:
            match = re.search(r"https?://\S+", instruction)
            url = match.group(0) if match else "https://example.com"
            steps.append({"type": "open_url", "value": url})
        elif "写入" in text or "write" in text:
            steps.append({"type": "file_write", "path": "tasks/generated_output.txt", "content": instruction})
        else:
            safe = instruction.replace('"', "'")
            steps.append({"type": "command", "value": f'echo "{safe}"'})
        return {"name": f"NL任务_{uuid.uuid4().hex[:4]}", "steps": steps, "retry": 0}

    def _extract_json(self, content: str) -> Optional[Dict[str, Any]]:
        candidates = re.findall(r"\{[\s\S]*\}", content)
        for item in candidates:
            try:
                parsed = json.loads(item)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                continue
        return None

    def generate_task(self, instruction: str) -> Dict[str, Any]:
        prompt = (
            "你是自动化任务生成器。仅返回JSON对象，不要解释。"
            "格式: {\"name\":\"...\",\"retry\":0,\"steps\":[{\"type\":\"command|file_write|file_copy|open_url|sleep\", ...}]}"
        )
        body = {
            "model": self.model_manager.cfg.get("model_name", "local-model"),
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": instruction},
            ],
            "temperature": 0.2,
        }
        url = f"{self.model_manager.cfg['model_api_url'].rstrip('/')}/chat/completions"
        try:
            response = requests.post(url, json=body, timeout=15)
            if response.status_code == 200:
                result = response.json()
                content = result["choices"][0]["message"]["content"]
                parsed = self._extract_json(content)
                if parsed:
                    parsed.setdefault("name", f"NL任务_{uuid.uuid4().hex[:4]}")
                    parsed.setdefault("retry", 0)
                    parsed.setdefault("steps", [])
                    return parsed
        except Exception as exc:
            append_log(APP_LOG, f"llm_generate_error | {exc}")
        return self._fallback(instruction)


class IMManager:
    def __init__(self, engine: TaskEngine, llm: LLMTaskGenerator, model_manager: ModelManager) -> None:
        self.engine = engine
        self.llm = llm
        self.model_manager = model_manager
        self.running = False
        self.threads: List[threading.Thread] = []
        self.offset = 0
        self.discord_seen = set()
        self.notify_queue: queue.Queue = queue.Queue()
        self.default_cfg = {
            "telegram": {"enabled": False, "token": "", "chat_ids": [], "allowed_user_ids": []},
            "discord": {"enabled": False, "bot_token": "", "channel_id": "", "allowed_user_ids": []},
            "qq": {
                "enabled": False,
                "endpoint": "http://127.0.0.1:5700",
                "access_token": "",
                "group_ids": [],
                "allowed_user_ids": [],
            },
            "webhook": {"enabled": False, "secret": "", "allowed_user_ids": [], "allowed_channels": []},
            "notifications": {"enabled": True, "on_success": True, "on_failure": True},
        }
        self.config = self._normalize_config(load_json(IM_FILE, self.default_cfg))
        if not IM_FILE.exists():
            save_json(IM_FILE, self.config)
        self.engine.add_listener(self.on_run_finished)

    def _normalize_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for key, default_block in self.default_cfg.items():
            current = config.get(key, {})
            if isinstance(default_block, dict):
                block = dict(default_block)
                if isinstance(current, dict):
                    block.update(current)
                merged[key] = block
            else:
                merged[key] = current if key in config else default_block
        return merged

    def save_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        self.config = self._normalize_config(config)
        save_json(IM_FILE, self.config)
        return self.config

    def log_message(self, direction: str, source: str, text: str, extra: str = "") -> None:
        append_log(IM_LOG, f"{direction} | {source} | {text} | {extra}")

    def is_allowed(self, source: str, user_id: str, channel_id: str) -> bool:
        cfg = self.config.get(source, {})
        allowed_users = {str(x) for x in cfg.get("allowed_user_ids", [])}
        if allowed_users and str(user_id) not in allowed_users:
            return False
        if source == "telegram":
            chat_ids = {str(x) for x in cfg.get("chat_ids", [])}
            return (not chat_ids) or (str(channel_id) in chat_ids)
        if source == "discord":
            channel_cfg = str(cfg.get("channel_id", "")).strip()
            return (not channel_cfg) or (str(channel_id) == channel_cfg)
        if source == "webhook":
            allowed_channels = {str(x) for x in cfg.get("allowed_channels", [])}
            return (not allowed_channels) or (str(channel_id) in allowed_channels)
        if source == "qq":
            group_ids = {str(x) for x in cfg.get("group_ids", [])}
            return (not group_ids) or (str(channel_id) in group_ids)
        return True

    def _cmd_help(self) -> str:
        return "/start /stop <run_id> /status /logs [run_id] /task <自然语言任务>"

    def process_command(self, text: str, source: str, user_id: str, channel_id: str) -> str:
        self.log_message("in", source, text, f"user={user_id}|channel={channel_id}")
        if not self.is_allowed(source, user_id, channel_id):
            return "权限不足：当前用户或频道不在白名单中。"
        if not text.startswith("/"):
            return "请使用指令格式，示例: /task 打开example并等待3秒"

        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2:
                task_id = parts[1].strip()
                run = self.engine.run_task(task_id, trigger=f"im:{source}")
                return f"任务已启动: {run['id']}"
            return f"WcClaw 在线。可用指令: {self._cmd_help()}"

        if text.startswith("/stop"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                return "用法: /stop <run_id>"
            run = self.engine.stop_run(parts[1].strip())
            return f"已请求停止: {run['id']}"

        if text.startswith("/status"):
            model_state = self.model_manager.status()
            runs = self.engine.list_runs()
            running = len([x for x in runs if x["status"] == "running"])
            return f"模型: {'在线' if model_state['reachable'] else '离线'} | 运行中任务: {running} | 总任务: {len(self.engine.list_tasks())}"

        if text.startswith("/logs"):
            parts = text.split(maxsplit=1)
            run_id = parts[1].strip() if len(parts) == 2 else (self.engine.list_runs()[0]["id"] if self.engine.list_runs() else "")
            if not run_id:
                return "暂无日志"
            logs = self.engine.get_run_logs(run_id, lines=20)
            return logs if logs else "暂无日志内容"

        if text.startswith("/task"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                return "用法: /task <自然语言任务>"
            task_def = self.llm.generate_task(parts[1].strip())
            task = self.engine.create_task(task_def)
            run = self.engine.run_task(task["id"], trigger=f"im:{source}")
            return f"任务已生成并运行: {task['name']} / {run['id']}"

        return f"未知指令。可用指令: {self._cmd_help()}"

    def _send_telegram(self, text: str) -> None:
        cfg = self.config.get("telegram", {})
        token = cfg.get("token", "").strip()
        if not token:
            return
        for chat_id in cfg.get("chat_ids", []):
            try:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": str(chat_id), "text": text[:3500]},
                    timeout=10,
                )
            except Exception as exc:
                append_log(IM_LOG, f"out_error | telegram | {exc}")

    def _send_discord(self, text: str) -> None:
        cfg = self.config.get("discord", {})
        token = cfg.get("bot_token", "").strip()
        channel_id = str(cfg.get("channel_id", "")).strip()
        if not token or not channel_id:
            return
        try:
            requests.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
                json={"content": text[:1800]},
                timeout=10,
            )
        except Exception as exc:
            append_log(IM_LOG, f"out_error | discord | {exc}")

    def _send_qq(self, text: str) -> None:
        cfg = self.config.get("qq", {})
        endpoint = str(cfg.get("endpoint", "")).strip().rstrip("/")
        if not endpoint:
            return
        headers = {"Content-Type": "application/json"}
        token = str(cfg.get("access_token", "")).strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        group_ids = [str(x).strip() for x in cfg.get("group_ids", []) if str(x).strip()]
        for group_id in group_ids:
            try:
                requests.post(
                    f"{endpoint}/send_group_msg",
                    headers=headers,
                    json={"group_id": int(group_id), "message": text[:1500]},
                    timeout=10,
                )
            except Exception as exc:
                append_log(IM_LOG, f"out_error | qq | {exc}")

    def send_message(self, source: str, text: str) -> None:
        self.log_message("out", source, text)
        if source == "telegram":
            self._send_telegram(text)
        elif source == "discord":
            self._send_discord(text)
        elif source == "qq":
            self._send_qq(text)

    def on_run_finished(self, run_item: Dict[str, Any]) -> None:
        cfg = self.config.get("notifications", {})
        if not cfg.get("enabled", True):
            return
        if run_item["status"] == "success" and not cfg.get("on_success", True):
            return
        if run_item["status"] != "success" and not cfg.get("on_failure", True):
            return
        self.notify_queue.put(run_item)

    def _notify_worker(self) -> None:
        while self.running:
            try:
                run_item = self.notify_queue.get(timeout=1)
            except queue.Empty:
                continue
            text = f"任务 {run_item['task_name']} | {run_item['status']} | {run_item['id']}"
            if run_item.get("error"):
                text += f" | {run_item['error']}"
            if self.config.get("telegram", {}).get("enabled"):
                self.send_message("telegram", text)
            if self.config.get("discord", {}).get("enabled"):
                self.send_message("discord", text)
            if self.config.get("qq", {}).get("enabled"):
                self.send_message("qq", text)

    def _telegram_worker(self) -> None:
        while self.running:
            cfg = self.config.get("telegram", {})
            token = cfg.get("token", "").strip()
            if not cfg.get("enabled") or not token:
                time.sleep(2)
                continue
            try:
                response = requests.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"timeout": 20, "offset": self.offset + 1},
                    timeout=30,
                )
                data = response.json()
                for item in data.get("result", []):
                    self.offset = max(self.offset, int(item["update_id"]))
                    msg = item.get("message", {})
                    text = msg.get("text", "").strip()
                    if not text:
                        continue
                    user_id = str(msg.get("from", {}).get("id", ""))
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    result = self.process_command(text, "telegram", user_id, chat_id)
                    self.log_message("out", "telegram", result, f"user={user_id}|channel={chat_id}")
                    requests.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": result[:3500]},
                        timeout=10,
                    )
            except Exception as exc:
                append_log(IM_LOG, f"in_error | telegram | {exc}")
                time.sleep(2)

    def _discord_worker(self) -> None:
        while self.running:
            cfg = self.config.get("discord", {})
            token = cfg.get("bot_token", "").strip()
            channel_id = str(cfg.get("channel_id", "")).strip()
            if not cfg.get("enabled") or not token or not channel_id:
                time.sleep(2)
                continue
            try:
                response = requests.get(
                    f"https://discord.com/api/v10/channels/{channel_id}/messages",
                    headers={"Authorization": f"Bot {token}"},
                    params={"limit": 20},
                    timeout=10,
                )
                if response.status_code != 200:
                    time.sleep(3)
                    continue
                messages = response.json()
                for msg in reversed(messages):
                    msg_id = str(msg.get("id"))
                    if msg_id in self.discord_seen:
                        continue
                    self.discord_seen.add(msg_id)
                    if msg.get("author", {}).get("bot"):
                        continue
                    text = (msg.get("content") or "").strip()
                    if not text.startswith("/"):
                        continue
                    user_id = str(msg.get("author", {}).get("id", ""))
                    result = self.process_command(text, "discord", user_id, channel_id)
                    self.log_message("out", "discord", result, f"user={user_id}|channel={channel_id}")
                    requests.post(
                        f"https://discord.com/api/v10/channels/{channel_id}/messages",
                        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
                        json={"content": result[:1800]},
                        timeout=10,
                    )
            except Exception as exc:
                append_log(IM_LOG, f"in_error | discord | {exc}")
            time.sleep(2)

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.threads = [
            threading.Thread(target=self._notify_worker, daemon=True),
            threading.Thread(target=self._telegram_worker, daemon=True),
            threading.Thread(target=self._discord_worker, daemon=True),
        ]
        for worker in self.threads:
            worker.start()

    def stop(self) -> None:
        self.running = False

    def logs(self, lines: int = 120) -> str:
        return read_tail(IM_LOG, lines=lines)

    def test_all_channels(self) -> Dict[str, Any]:
        ts = now_iso()
        results: Dict[str, Any] = {}
        if self.config.get("telegram", {}).get("enabled"):
            try:
                self.send_message("telegram", f"[WcClaw 测试] Telegram 通道正常 {ts}")
                results["telegram"] = {"ok": True, "message": "已发送测试消息"}
            except Exception as exc:
                results["telegram"] = {"ok": False, "message": str(exc)}
        else:
            results["telegram"] = {"ok": False, "message": "未启用"}

        if self.config.get("discord", {}).get("enabled"):
            try:
                self.send_message("discord", f"[WcClaw 测试] Discord 通道正常 {ts}")
                results["discord"] = {"ok": True, "message": "已发送测试消息"}
            except Exception as exc:
                results["discord"] = {"ok": False, "message": str(exc)}
        else:
            results["discord"] = {"ok": False, "message": "未启用"}

        if self.config.get("qq", {}).get("enabled"):
            try:
                self.send_message("qq", f"[WcClaw 测试] QQ 通道正常 {ts}")
                results["qq"] = {"ok": True, "message": "已发送测试消息"}
            except Exception as exc:
                results["qq"] = {"ok": False, "message": str(exc)}
        else:
            results["qq"] = {"ok": False, "message": "未启用"}

        webhook_enabled = bool(self.config.get("webhook", {}).get("enabled"))
        results["webhook"] = {
            "ok": webhook_enabled,
            "message": "Webhook 需外部请求触发，配置有效" if webhook_enabled else "未启用",
        }
        return results


class SkillManager:
    def __init__(self) -> None:
        self.default_cfg = {
            "skills": [
                {
                    "id": "file-organizer",
                    "name": "文件整理",
                    "description": "按扩展名整理指定目录，输出整理清单。",
                    "entry": "builtin:file_organizer",
                    "enabled": True,
                    "builtin": True,
                },
                {
                    "id": "web-runner",
                    "name": "网页自动执行",
                    "description": "打开网页并执行等待、抓取等基础动作。",
                    "entry": "builtin:web_runner",
                    "enabled": True,
                    "builtin": True,
                },
                {
                    "id": "cmd-helper",
                    "name": "命令助手",
                    "description": "将自然语言转换为安全命令步骤。",
                    "entry": "builtin:cmd_helper",
                    "enabled": True,
                    "builtin": True,
                },
            ]
        }
        self.lock = threading.Lock()
        self.cfg = load_json(SKILL_FILE, self.default_cfg)
        if not isinstance(self.cfg.get("skills"), list):
            self.cfg = self.default_cfg
        if not SKILL_FILE.exists():
            save_json(SKILL_FILE, self.cfg)
        self._sync_fs()

    def _save(self) -> None:
        save_json(SKILL_FILE, self.cfg)

    def _skill_file(self, skill_id: str) -> Path:
        return SKILL_DIR / f"{skill_id}.json"

    def _sync_fs(self) -> None:
        SKILL_DIR.mkdir(parents=True, exist_ok=True)
        for item in self.cfg.get("skills", []):
            skill_id = str(item.get("id", "")).strip()
            if not skill_id:
                continue
            fp = self._skill_file(skill_id)
            payload = {
                "id": item.get("id", ""),
                "name": item.get("name", ""),
                "description": item.get("description", ""),
                "entry": item.get("entry", ""),
                "enabled": bool(item.get("enabled", False)),
                "builtin": bool(item.get("builtin", False)),
                "updated_at": now_iso(),
            }
            save_json(fp, payload)

    def list_skills(self) -> List[Dict[str, Any]]:
        with self.lock:
            out = []
            for item in self.cfg.get("skills", []):
                row = dict(item)
                row["config_path"] = str(self._skill_file(str(item.get("id", ""))))
                out.append(row)
            return out

    def set_enabled(self, skill_id: str, enabled: bool) -> Dict[str, Any]:
        with self.lock:
            found = None
            for item in self.cfg.get("skills", []):
                if item.get("id") == skill_id:
                    item["enabled"] = bool(enabled)
                    found = dict(item)
                    break
            if not found:
                raise RuntimeError("skill not found")
            self._save()
            self._sync_fs()
            append_log(APP_LOG, f"skill_toggle | {skill_id} | enabled={enabled}")
            return found

    def create_skill(self, name: str, description: str = "") -> Dict[str, Any]:
        clean_name = re.sub(r"\s+", " ", name).strip()
        if not clean_name:
            raise RuntimeError("name required")
        skill_id = re.sub(r"[^a-z0-9]+", "-", clean_name.lower()).strip("-")
        if not skill_id:
            skill_id = f"skill-{uuid.uuid4().hex[:8]}"
        with self.lock:
            ids = {x.get("id") for x in self.cfg.get("skills", [])}
            if skill_id in ids:
                skill_id = f"{skill_id}-{uuid.uuid4().hex[:4]}"
            item = {
                "id": skill_id,
                "name": clean_name,
                "description": description.strip() or "自定义 Skill",
                "entry": f"custom:{skill_id}",
                "enabled": True,
                "builtin": False,
            }
            self.cfg.setdefault("skills", []).append(item)
            self._save()
            self._sync_fs()
            append_log(APP_LOG, f"skill_create | {skill_id}")
            return item

    def delete_skill(self, skill_id: str) -> Dict[str, Any]:
        with self.lock:
            target = None
            remain = []
            for item in self.cfg.get("skills", []):
                if item.get("id") == skill_id:
                    target = dict(item)
                else:
                    remain.append(item)
            if not target:
                raise RuntimeError("skill not found")
            if target.get("builtin"):
                raise RuntimeError("builtin skill can not be deleted")
            self.cfg["skills"] = remain
            self._save()
            fp = self._skill_file(skill_id)
            if fp.exists():
                fp.unlink()
            append_log(APP_LOG, f"skill_delete | {skill_id}")
            return target


class TaskCreateReq(BaseModel):
    name: str
    steps: List[Dict[str, Any]]
    retry: int = 0


class GenerateTaskReq(BaseModel):
    instruction: str
    auto_run: bool = False


class IMWebhookReq(BaseModel):
    text: str
    user_id: str = ""
    channel_id: str = ""
    secret: str = ""


class ModelDownloadReq(BaseModel):
    model_id: str = ""
    url: str = ""
    filename: str = ""
    auto_activate: bool = True


class ModelActivateReq(BaseModel):
    filename: str
    model_name: str = ""


class SkillCreateReq(BaseModel):
    name: str
    description: str = ""


class SkillToggleReq(BaseModel):
    enabled: bool


class AppRuntime:
    def __init__(self) -> None:
        ensure_dirs()
        self.policy = SecurityPolicy()
        self.model = ModelManager()
        self.engine = TaskEngine(self.policy)
        self.llm = LLMTaskGenerator(self.model)
        self.im = IMManager(self.engine, self.llm, self.model)
        self.skills = SkillManager()
        self.im.start()
        append_log(APP_LOG, "runtime_started")

    def status(self) -> Dict[str, Any]:
        model_status = self.model.status()
        runs = self.engine.list_runs()
        return {
            "model": model_status,
            "task_engine": {
                "running": True,
                "tasks_total": len(self.engine.list_tasks()),
                "runs_total": len(runs),
                "runs_active": len([x for x in runs if x["status"] == "running"]),
            },
            "im": {
                "running": self.im.running,
                "telegram_enabled": bool(self.im.config.get("telegram", {}).get("enabled")),
                "discord_enabled": bool(self.im.config.get("discord", {}).get("enabled")),
                "qq_enabled": bool(self.im.config.get("qq", {}).get("enabled")),
                "webhook_enabled": bool(self.im.config.get("webhook", {}).get("enabled")),
            },
        }


RUNTIME = AppRuntime()
APP = FastAPI(title="WcClaw Local API")


@APP.get("/api/status")
def api_status() -> Dict[str, Any]:
    return {"ok": True, "data": RUNTIME.status(), "error": None}


@APP.post("/api/model/start")
def api_model_start() -> Dict[str, Any]:
    try:
        return {"ok": True, "data": RUNTIME.model.start(), "error": None}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@APP.post("/api/model/stop")
def api_model_stop() -> Dict[str, Any]:
    return {"ok": True, "data": RUNTIME.model.stop(), "error": None}


@APP.get("/api/models/catalog")
def api_models_catalog() -> Dict[str, Any]:
    return {"ok": True, "data": RUNTIME.model.catalog_with_state(), "error": None}


@APP.get("/api/models/local")
def api_models_local() -> Dict[str, Any]:
    return {"ok": True, "data": RUNTIME.model.local_models(), "error": None}


@APP.get("/api/models/downloads")
def api_models_downloads() -> Dict[str, Any]:
    return {"ok": True, "data": RUNTIME.model.download_jobs(), "error": None}


@APP.post("/api/models/activate")
def api_models_activate(req: ModelActivateReq) -> Dict[str, Any]:
    try:
        data = RUNTIME.model.activate_model(req.filename, req.model_name)
        return {"ok": True, "data": data, "error": None}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@APP.post("/api/models/download")
def api_models_download(req: ModelDownloadReq) -> Dict[str, Any]:
    try:
        source = None
        if req.model_id:
            source = RUNTIME.model._catalog_item(req.model_id)
            if not source:
                raise RuntimeError("model_id not found")
        url = req.url.strip() if req.url else (source["url"] if source else "")
        filename = req.filename.strip() if req.filename else (source["filename"] if source else Path(url.split("?")[0]).name)
        model_name = source["name"] if source else Path(filename).stem
        job = RUNTIME.model.start_download(url=url, filename=filename, auto_activate=req.auto_activate, model_name=model_name)
        return {"ok": True, "data": job, "error": None}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@APP.post("/api/models/downloads/{job_id}/retry")
def api_models_download_retry(job_id: str) -> Dict[str, Any]:
    try:
        job = RUNTIME.model.retry_download(job_id)
        return {"ok": True, "data": job, "error": None}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@APP.post("/api/models/self_check")
def api_models_self_check() -> Dict[str, Any]:
    try:
        data = RUNTIME.model.self_check_active_model()
        return {"ok": True, "data": data, "error": None}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@APP.get("/api/skills")
def api_skills_list() -> Dict[str, Any]:
    return {"ok": True, "data": RUNTIME.skills.list_skills(), "error": None}


@APP.post("/api/skills")
def api_skills_create(req: SkillCreateReq) -> Dict[str, Any]:
    try:
        item = RUNTIME.skills.create_skill(req.name, req.description)
        return {"ok": True, "data": item, "error": None}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@APP.post("/api/skills/{skill_id}/enable")
def api_skills_enable(skill_id: str, req: SkillToggleReq) -> Dict[str, Any]:
    try:
        item = RUNTIME.skills.set_enabled(skill_id, req.enabled)
        return {"ok": True, "data": item, "error": None}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@APP.post("/api/skills/{skill_id}/delete")
def api_skills_delete(skill_id: str) -> Dict[str, Any]:
    try:
        item = RUNTIME.skills.delete_skill(skill_id)
        return {"ok": True, "data": item, "error": None}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@APP.post("/api/llm/generate_task")
def api_generate_task(req: GenerateTaskReq) -> Dict[str, Any]:
    task_def = RUNTIME.llm.generate_task(req.instruction)
    task_item = RUNTIME.engine.create_task(task_def)
    run = RUNTIME.engine.run_task(task_item["id"], trigger="nl_auto") if req.auto_run else None
    return {"ok": True, "data": {"task": task_item, "run": run}, "error": None}


@APP.get("/api/tasks")
def api_tasks() -> Dict[str, Any]:
    return {"ok": True, "data": RUNTIME.engine.list_tasks(), "error": None}


@APP.post("/api/tasks")
def api_create_task(req: TaskCreateReq) -> Dict[str, Any]:
    task_item = RUNTIME.engine.create_task(req.model_dump())
    return {"ok": True, "data": task_item, "error": None}


@APP.post("/api/tasks/{task_id}/run")
def api_run_task(task_id: str) -> Dict[str, Any]:
    try:
        run_item = RUNTIME.engine.run_task(task_id, trigger="ui")
        return {"ok": True, "data": run_item, "error": None}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@APP.post("/api/runs/{run_id}/stop")
def api_stop_run(run_id: str) -> Dict[str, Any]:
    try:
        run_item = RUNTIME.engine.stop_run(run_id)
        return {"ok": True, "data": run_item, "error": None}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@APP.get("/api/runs")
def api_runs() -> Dict[str, Any]:
    return {"ok": True, "data": RUNTIME.engine.list_runs(), "error": None}


@APP.get("/api/runs/{run_id}/logs")
def api_run_logs(run_id: str, lines: int = 120) -> Dict[str, Any]:
    text = RUNTIME.engine.get_run_logs(run_id, lines=lines)
    return {"ok": True, "data": {"run_id": run_id, "logs": text}, "error": None}


@APP.get("/api/dashboard")
def api_dashboard() -> Dict[str, Any]:
    return {
        "ok": True,
        "data": {
            "status": RUNTIME.status(),
            "recent_runs": RUNTIME.engine.recent_runs(5),
            "logs": read_tail(APP_LOG, lines=10),
        },
        "error": None,
    }


@APP.get("/api/im/config")
def api_im_cfg() -> Dict[str, Any]:
    return {"ok": True, "data": RUNTIME.im.config, "error": None}


@APP.post("/api/im/config")
def api_im_cfg_set(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = RUNTIME.im.save_config(payload)
    return {"ok": True, "data": result, "error": None}


@APP.get("/api/im/logs")
def api_im_logs(lines: int = 120) -> Dict[str, Any]:
    return {"ok": True, "data": {"logs": RUNTIME.im.logs(lines)}, "error": None}


@APP.post("/api/im/test_all")
def api_im_test_all() -> Dict[str, Any]:
    return {"ok": True, "data": RUNTIME.im.test_all_channels(), "error": None}


@APP.post("/api/im/webhook")
def api_im_webhook(req: IMWebhookReq) -> Dict[str, Any]:
    cfg = RUNTIME.im.config.get("webhook", {})
    if not cfg.get("enabled"):
        raise HTTPException(status_code=403, detail="webhook disabled")
    secret = str(cfg.get("secret", "")).strip()
    if secret and req.secret != secret:
        raise HTTPException(status_code=403, detail="invalid secret")
    result = RUNTIME.im.process_command(req.text, "webhook", req.user_id, req.channel_id)
    RUNTIME.im.log_message("out", "webhook", result, f"user={req.user_id}|channel={req.channel_id}")
    return {"ok": True, "data": {"message": result}, "error": None}


@APP.post("/api/im/qq/webhook")
def api_im_qq_webhook(req: IMWebhookReq) -> Dict[str, Any]:
    cfg = RUNTIME.im.config.get("qq", {})
    if not cfg.get("enabled"):
        raise HTTPException(status_code=403, detail="qq disabled")
    token = str(cfg.get("access_token", "")).strip()
    if token and req.secret != token:
        raise HTTPException(status_code=403, detail="invalid qq token")
    channel_id = req.channel_id or "0"
    result = RUNTIME.im.process_command(req.text, "qq", req.user_id, channel_id)
    RUNTIME.im.log_message("out", "qq", result, f"user={req.user_id}|channel={channel_id}")
    return {"ok": True, "data": {"message": result}, "error": None}


def run_backend(host: str = "127.0.0.1", port: int = 8765) -> None:
    uvicorn.run(APP, host=host, port=port, log_level="error")


def start_backend_thread(host: str = "127.0.0.1", port: int = 8765) -> threading.Thread:
    thread = threading.Thread(target=run_backend, args=(host, port), daemon=True)
    thread.start()
    return thread
