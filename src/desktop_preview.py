import json
import hashlib
import os
import socket
import shutil
import subprocess
import sys
import time
import ctypes
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


def resource_path(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base.joinpath(*parts)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DESKTOP_CFG_FILE = PROJECT_ROOT / "config" / "desktop_ui.json"
APP_VERSION = "0.1.2"
DEFAULT_UPDATE_MANIFEST_URL = "https://cuiwenchu.com/update.json"
DEFAULT_GITHUB_REPO = "cuiwenchu/wcclaw"


def load_desktop_cfg() -> Dict[str, Any]:
    if not DESKTOP_CFG_FILE.exists():
        return {"first_run_done": False}
    try:
        return json.loads(DESKTOP_CFG_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"first_run_done": False}


def save_desktop_cfg(cfg: Dict[str, Any]) -> None:
    DESKTOP_CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP_CFG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_version(v: str) -> List[int]:
    raw = str(v or "").strip().lower().lstrip("v")
    out: List[int] = []
    for part in raw.split("."):
        try:
            out.append(int(part))
        except Exception:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return out[:3]


def is_version_newer(remote_v: str, local_v: str) -> bool:
    return normalize_version(remote_v) > normalize_version(local_v)


def machine_grade() -> str:
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        mem = MEMORYSTATUSEX()
        mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
        gb = mem.ullTotalPhys / (1024 ** 3)
        if gb <= 8:
            return "low"
        if gb <= 16:
            return "mid"
        return "high"
    except Exception:
        return "mid"


class ApiClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765") -> None:
        self.base_url = base_url.rstrip("/")

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 8) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            if method == "GET":
                response = requests.get(url, timeout=timeout)
            else:
                response = requests.post(url, json=payload or {}, timeout=timeout)
            if response.status_code >= 400:
                return {"ok": False, "error": f"HTTP {response.status_code}: {response.text}", "data": None}
            data = response.json()
            if isinstance(data, dict):
                return data
            return {"ok": True, "data": data, "error": None}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "data": None}

    def get(self, path: str, timeout: int = 8) -> Dict[str, Any]:
        return self._request("GET", path, timeout=timeout)

    def post(self, path: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 8) -> Dict[str, Any]:
        return self._request("POST", path, payload=payload, timeout=timeout)


class ApiWorkerSignals(QObject):
    finished = Signal(str, object)


class ApiWorker(QRunnable):
    def __init__(
        self,
        client: ApiClient,
        key: str,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: int = 8,
    ) -> None:
        super().__init__()
        self.client = client
        self.key = key
        self.method = method
        self.path = path
        self.payload = payload
        self.timeout = timeout
        self.signals = ApiWorkerSignals()

    def run(self) -> None:
        if self.method == "GET":
            result = self.client.get(self.path, timeout=self.timeout)
        else:
            result = self.client.post(self.path, payload=self.payload, timeout=self.timeout)
        self.signals.finished.emit(self.key, result)


class FuncWorker(QRunnable):
    def __init__(self, key: str, fn: Callable[[], Dict[str, Any]]) -> None:
        super().__init__()
        self.key = key
        self.fn = fn
        self.signals = ApiWorkerSignals()

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:
            result = {"ok": False, "error": str(exc), "data": None}
        self.signals.finished.emit(self.key, result)


class StatusLamp(QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("statusLamp")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        self.dot = QLabel("●")
        self.dot.setObjectName("lampDot")
        self.value = QLabel("未知")
        self.value.setObjectName("lampValue")
        layout.addWidget(self.dot)
        layout.addWidget(QLabel(title))
        layout.addStretch(1)
        layout.addWidget(self.value)

    def set_state(self, online: bool, text: str) -> None:
        self.dot.setProperty("on", "true" if online else "false")
        self.dot.style().unpolish(self.dot)
        self.dot.style().polish(self.dot)
        self.value.setText(text)


class ProviderBlock(QFrame):
    def __init__(self, title: str, token_hint: str, target_hint: str):
        super().__init__()
        self.setObjectName("providerBlock")
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel(title))
        self.enabled = QCheckBox("启用")
        header.addStretch(1)
        header.addWidget(self.enabled)
        layout.addLayout(header)
        row1 = QHBoxLayout()
        self.token = QLineEdit()
        self.token.setPlaceholderText(token_hint)
        self.target = QLineEdit()
        self.target.setPlaceholderText(target_hint)
        row1.addWidget(self.token, 2)
        row1.addWidget(self.target, 2)
        layout.addLayout(row1)
        self.users = QLineEdit()
        self.users.setPlaceholderText("白名单用户 ID（逗号分隔）")
        layout.addWidget(self.users)


def split_csv(text: str) -> List[str]:
    return [x.strip() for x in text.split(",") if x.strip()]


def to_csv(values: List[Any]) -> str:
    return ",".join([str(x) for x in values])


class WcClawDesktop(QMainWindow):
    def __init__(self):
        super().__init__()
        self.api = ApiClient()
        self.thread_pool = QThreadPool.globalInstance()
        self.inflight_keys: set[str] = set()
        self.one_shot_handlers: Dict[str, Callable[[Dict[str, Any]], None]] = {}
        self.latest_app_logs = ""
        self.latest_im_logs = ""
        self.max_log_lines = 100
        self.task_digest = ""
        self.run_digest = ""
        self.last_generated_task_id = ""
        self.last_download_job_id = ""
        self.machine_profile = machine_grade()
        self.ui_cfg = load_desktop_cfg()
        self.ui_cfg.setdefault("update_manifest_url", DEFAULT_UPDATE_MANIFEST_URL)
        self.ui_cfg.setdefault("github_repo", DEFAULT_GITHUB_REPO)
        self.ui_cfg.setdefault("ignore_update_version", "")
        save_desktop_cfg(self.ui_cfg)
        self.pending_update_meta: Dict[str, Any] = {}
        self.setWindowTitle("WcClaw 文出龙虾")
        self.resize(1360, 900)

        self._try_start_backend()
        self._build_ui()
        self._apply_style()
        self.switch_page("home")
        self._init_timers()
        self.refresh_all()
        self.request_im_config()
        QTimer.singleShot(300, self._maybe_run_startup_wizard)
        QTimer.singleShot(2500, lambda: self.check_update(manual=False))

    def _try_start_backend(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.2)
        try:
            if sock.connect_ex(("127.0.0.1", 8765)) == 0:
                return
        finally:
            sock.close()
        try:
            from wcclaw_backend import start_backend_thread

            start_backend_thread("127.0.0.1", 8765)
            time.sleep(0.3)
        except Exception:
            return

    def _init_timers(self) -> None:
        self.task_timer = QTimer(self)
        self.task_timer.timeout.connect(self.request_task_and_runs)
        self.task_timer.start(2000)

        self.model_timer = QTimer(self)
        self.model_timer.timeout.connect(self.request_model_status)
        self.model_timer.start(3000)

        self.im_timer = QTimer(self)
        self.im_timer.timeout.connect(self.request_im_status)
        self.im_timer.start(5000)

        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self.request_logs)
        self.log_timer.start(1000)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        left = QFrame()
        left.setObjectName("leftPanel")
        left.setFixedWidth(290)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(14, 16, 14, 14)

        search = QLineEdit()
        search.setPlaceholderText("搜索")
        search.setObjectName("search")
        left_layout.addWidget(search)
        left_layout.addWidget(QLabel("暂无历史对话"))

        self.left_task_button = QPushButton("任务列表")
        self.left_task_button.setObjectName("navBtn")
        self.left_task_button.setCheckable(True)
        self.left_task_button.clicked.connect(lambda: self.switch_page("tasks"))
        left_layout.addWidget(self.left_task_button)
        left_layout.addStretch(1)
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(0)
        footer_logo = QLabel()
        footer_path = resource_path("assets", "wcclaw_logo.png")
        if footer_path.exists():
            footer_logo.setFixedSize(180, 52)
            footer_logo.setPixmap(QPixmap(str(footer_path)).scaled(180, 52, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        footer_logo.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        footer_logo.setObjectName("leftBrandLogo")
        footer.addWidget(footer_logo)
        footer.addStretch(1)
        left_layout.addLayout(footer)

        right = QFrame()
        right.setObjectName("rightPanel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(18, 14, 18, 14)
        top = QHBoxLayout()
        self.top_title = QLabel("首页总控制台")
        self.top_title.setObjectName("topTitle")
        top.addWidget(self.top_title)
        top.addStretch(1)
        self.version_label = QLabel(f"v{APP_VERSION}")
        top.addWidget(self.version_label)
        right_layout.addLayout(top)

        self.stack = QStackedWidget()
        self.page_home = self._build_home_page()
        self.page_nl = self._build_nl_page()
        self.page_tasks = self._build_tasks_page()
        self.page_model = self._build_model_page()
        self.page_skills = self._build_skills_page()
        self.page_im = self._build_im_page()
        self.page_settings = self._build_settings_page()
        for w in [self.page_home, self.page_nl, self.page_tasks, self.page_model, self.page_skills, self.page_im, self.page_settings]:
            self.stack.addWidget(w)
        right_layout.addWidget(self.stack, 1)

        bottom = QFrame()
        bottom.setObjectName("bottomNav")
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(4, 8, 4, 4)
        bottom_layout.setSpacing(8)
        self.bottom_nav_buttons: Dict[str, QPushButton] = {}
        for key, text in [
            ("home", "首页"),
            ("nl", "自然语言任务"),
            ("model", "模型管理"),
            ("skills", "Skills 管理"),
            ("im", "IM 管理"),
            ("settings", "设置"),
        ]:
            btn = QPushButton(text)
            btn.setObjectName("navBtn")
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked=False, k=key: self.switch_page(k))
            bottom_layout.addWidget(btn)
            self.bottom_nav_buttons[key] = btn
        bottom_layout.addStretch(1)
        right_layout.addWidget(bottom)

        self.page_index = {"home": 0, "nl": 1, "tasks": 2, "model": 3, "skills": 4, "im": 5, "settings": 6}

        root_layout.addWidget(left)
        root_layout.addWidget(right, 1)

    def _build_home_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("contentPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 10)
        layout.setSpacing(10)

        status_row = QFrame()
        status_row.setObjectName("panel")
        status_layout = QHBoxLayout(status_row)
        status_layout.setContentsMargins(12, 10, 12, 10)
        status_layout.setSpacing(8)
        self.lamp_model = StatusLamp("模型")
        self.lamp_task = StatusLamp("任务引擎")
        self.lamp_im = StatusLamp("IM 连接")
        status_layout.addWidget(self.lamp_model)
        status_layout.addWidget(self.lamp_task)
        status_layout.addWidget(self.lamp_im)
        layout.addWidget(status_row)

        guide_panel = QFrame()
        guide_panel.setObjectName("panel")
        g = QVBoxLayout(guide_panel)
        g.setContentsMargins(12, 10, 12, 10)
        g.setSpacing(8)
        g.addWidget(QLabel("新手 3 分钟上手"))
        g.addWidget(QLabel("1) 安装推荐模型  2) 输入一句话任务执行  3) 可选配置 QQ 远程控制"))
        grow = QHBoxLayout()
        g1 = QPushButton("步骤1：安装模型")
        g1.setObjectName("secondary")
        g1.clicked.connect(self.quickstart_step_model)
        g2 = QPushButton("步骤2：试跑任务")
        g2.setObjectName("secondary")
        g2.clicked.connect(self.quickstart_step_task)
        g3 = QPushButton("步骤3：配置 QQ")
        g3.setObjectName("secondary")
        g3.clicked.connect(self.quickstart_step_im)
        g4 = QPushButton("完整指南")
        g4.setObjectName("ghost")
        g4.clicked.connect(self.show_quickstart_guide)
        grow.addWidget(g1)
        grow.addWidget(g2)
        grow.addWidget(g3)
        grow.addWidget(g4)
        grow.addStretch(1)
        g.addLayout(grow)
        layout.addWidget(guide_panel)

        chat_panel = QFrame()
        chat_panel.setObjectName("panel")
        c = QVBoxLayout(chat_panel)
        c.setContentsMargins(12, 10, 12, 10)
        c.setSpacing(8)

        self.chat_view = QTextEdit()
        self.chat_view.setObjectName("chatView")
        self.chat_view.setReadOnly(True)
        self.chat_view.setPlaceholderText("输入任务后，这里显示执行结果")
        c.addWidget(self.chat_view, 1)

        input_row = QHBoxLayout()
        self.chat_input = QLineEdit()
        self.chat_input.setObjectName("homeInput")
        self.chat_input.setPlaceholderText("输入自然语言任务，例如：整理下载目录并汇总成清单")
        self.chat_input.returnPressed.connect(self._send_home_input)
        self.chat_send = QPushButton("执行")
        self.chat_send.setObjectName("primary")
        self.chat_send.clicked.connect(self._send_home_input)
        input_row.addWidget(self.chat_input, 1)
        input_row.addWidget(self.chat_send)
        c.addLayout(input_row)

        layout.addWidget(chat_panel, 1)
        return page

    def _build_nl_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("contentPage")
        layout = QVBoxLayout(page)

        market = QFrame(); market.setObjectName("panel")
        ml = QVBoxLayout(market)
        ml.addWidget(QLabel("任务模板市场（本地）"))
        self.market_templates: List[Dict[str, Any]] = [
            {
                "name": "下载目录清理+清单",
                "description": "整理 downloads 目录并输出清单到 tasks/downloads_report.txt",
                "task": {
                    "name": "下载目录清理+清单",
                    "retry": 1,
                    "steps": [
                        {"type": "command", "value": "dir"},
                        {"type": "file_write", "path": "tasks/downloads_report.txt", "content": "请在此填入自动整理输出"}
                    ],
                },
            },
            {
                "name": "系统信息采集",
                "description": "采集 whoami 与 ipconfig，写入 tasks/system_info.txt",
                "task": {
                    "name": "系统信息采集",
                    "retry": 0,
                    "steps": [
                        {"type": "command", "value": "whoami"},
                        {"type": "command", "value": "ipconfig"},
                        {"type": "file_write", "path": "tasks/system_info.txt", "content": "系统信息采集完成"}
                    ],
                },
            },
            {
                "name": "网页访问记录",
                "description": "打开示例网址并记录执行结果",
                "task": {
                    "name": "网页访问记录",
                    "retry": 0,
                    "steps": [
                        {"type": "open_url", "value": "https://example.com"},
                        {"type": "sleep", "value": 3},
                        {"type": "file_write", "path": "tasks/web_visit_result.txt", "content": "已完成网页访问流程"}
                    ],
                },
            },
        ]
        self.market_list = QListWidget(); self.market_list.setObjectName("list")
        for item in self.market_templates:
            self.market_list.addItem(f"{item.get('name','')} | {item.get('description','')}")
        ml.addWidget(self.market_list)
        mrow = QHBoxLayout()
        m1 = QPushButton("导入到输入框"); m1.setObjectName("secondary"); m1.clicked.connect(self.import_market_template)
        m2 = QPushButton("一键导入并运行"); m2.setObjectName("primary"); m2.clicked.connect(self.run_market_template)
        mrow.addWidget(m1); mrow.addWidget(m2); mrow.addStretch(1)
        ml.addLayout(mrow)
        self.market_result = QLabel("请选择模板")
        ml.addWidget(self.market_result)
        layout.addWidget(market)

        card = QFrame(); card.setObjectName("panel")
        c = QVBoxLayout(card)
        c.addWidget(QLabel("自然语言任务生成"))
        template_row = QHBoxLayout()
        template_row.addWidget(QLabel("任务模板"))
        self.template_combo = QComboBox()
        self.template_defs = {
            "下载目录清单": "扫描 downloads 目录，把文件名和大小写入 tasks/downloads_report.txt",
            "批量重命名示例": "把 tasks/input 下所有 .txt 文件重命名为带日期前缀",
            "网页打开与等待": "打开 https://example.com，等待 3 秒并记录日志",
            "系统基础信息": "执行 whoami 和 ipconfig，并写入 tasks/system_info.txt",
        }
        self.template_combo.addItems(list(self.template_defs.keys()))
        t1 = QPushButton("载入模板")
        t1.setObjectName("secondary")
        t1.clicked.connect(self.load_template_instruction)
        t2 = QPushButton("一键运行模板")
        t2.setObjectName("ghost")
        t2.clicked.connect(self.run_template_instruction)
        template_row.addWidget(self.template_combo, 1)
        template_row.addWidget(t1)
        template_row.addWidget(t2)
        c.addLayout(template_row)
        self.nl_input = QTextEdit()
        self.nl_input.setPlaceholderText("例如：打开 https://example.com，等待 3 秒")
        self.nl_input.setFixedHeight(130)
        c.addWidget(self.nl_input)
        row = QHBoxLayout()
        b1 = QPushButton("生成任务"); b1.setObjectName("secondary"); b1.clicked.connect(lambda: self.generate_task(False))
        b2 = QPushButton("生成并执行"); b2.setObjectName("primary"); b2.clicked.connect(lambda: self.generate_task(True))
        b3 = QPushButton("运行刚生成任务"); b3.setObjectName("ghost"); b3.clicked.connect(self.run_generated_task)
        row.addWidget(b1); row.addWidget(b2); row.addWidget(b3); row.addStretch(1)
        c.addLayout(row)
        self.nl_result = QLabel("等待输入")
        c.addWidget(self.nl_result)
        self.generated_json = QTextEdit(); self.generated_json.setReadOnly(True); self.generated_json.setObjectName("logView")
        c.addWidget(self.generated_json)
        layout.addWidget(card)
        return page

    def _build_tasks_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("contentPage")
        layout = QHBoxLayout(page)

        p1 = QFrame(); p1.setObjectName("panel")
        l1 = QVBoxLayout(p1)
        l1.addWidget(QLabel("任务列表"))
        self.task_list = QListWidget(); self.task_list.setObjectName("list")
        l1.addWidget(self.task_list)
        r1 = QHBoxLayout()
        br1 = QPushButton("刷新"); br1.setObjectName("secondary"); br1.clicked.connect(self.refresh_tasks_page)
        br2 = QPushButton("运行选中任务"); br2.setObjectName("primary"); br2.clicked.connect(self.run_selected_task)
        r1.addWidget(br1); r1.addWidget(br2)
        l1.addLayout(r1)

        p2 = QFrame(); p2.setObjectName("panel")
        l2 = QVBoxLayout(p2)
        l2.addWidget(QLabel("运行记录"))
        self.run_list = QListWidget(); self.run_list.setObjectName("list")
        self.run_list.itemSelectionChanged.connect(self.load_selected_run_logs)
        l2.addWidget(self.run_list)
        r2 = QHBoxLayout()
        bs1 = QPushButton("刷新"); bs1.setObjectName("secondary"); bs1.clicked.connect(self.refresh_tasks_page)
        bs2 = QPushButton("停止选中运行"); bs2.setObjectName("secondary"); bs2.clicked.connect(self.stop_selected_run)
        r2.addWidget(bs1); r2.addWidget(bs2)
        l2.addLayout(r2)

        p3 = QFrame(); p3.setObjectName("panel")
        l3 = QVBoxLayout(p3)
        l3.addWidget(QLabel("任务日志"))
        self.run_logs = QTextEdit(); self.run_logs.setReadOnly(True); self.run_logs.setObjectName("logView")
        l3.addWidget(self.run_logs)

        layout.addWidget(p1, 1); layout.addWidget(p2, 1); layout.addWidget(p3, 1)
        return page
    def _build_model_page(self) -> QWidget:
        page = QScrollArea()
        page.setObjectName("contentPageScroll")
        page.setWidgetResizable(True)
        page.setFrameShape(QFrame.NoFrame)
        wrap = QWidget()
        wrap.setObjectName("contentPage")
        page.setWidget(wrap)
        layout = QVBoxLayout(wrap)

        top = QFrame(); top.setObjectName("panel")
        t = QHBoxLayout(top)
        self.model_status_label = QLabel("模型状态：未知")
        self.model_status_label.setObjectName("topTitle")
        s1 = QPushButton("启动模型"); s1.setObjectName("primary"); s1.clicked.connect(self.start_model)
        s2 = QPushButton("停止模型"); s2.setObjectName("secondary"); s2.clicked.connect(self.stop_model)
        t.addWidget(self.model_status_label); t.addStretch(1); t.addWidget(s1); t.addWidget(s2)
        layout.addWidget(top)

        card = QFrame(); card.setObjectName("panel")
        c = QVBoxLayout(card)
        c.addWidget(QLabel("可选轻量模型"))
        self.model_row_status: Dict[str, QLabel] = {}
        rec_id = self._recommended_model_id()
        self.model_catalog = [
            {"id": "qwen2.5-1.5b-q4", "name": "Qwen2.5 1.5B", "filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf", "meta": "1.5G / 4G"},
            {"id": "phi-2-q4", "name": "Phi-2", "filename": "phi-2.Q4_K_M.gguf", "meta": "1.7G / 4G"},
            {"id": "tinyllama-1.1b-q4", "name": "TinyLlama 1.1B", "filename": "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf", "meta": "0.8G / 2G"},
            {"id": "qwen2.5-7b-q4", "name": "Qwen2.5 7B", "filename": "qwen2.5-7b-instruct-q4_k_m.gguf", "meta": "4.5G / 8G"},
            {"id": "llama-3.1-8b-q4", "name": "Llama 3.1 8B", "filename": "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf", "meta": "5.0G / 8G"},
        ]
        for model in self.model_catalog:
            row = QFrame(); row.setObjectName("providerBlock")
            rl = QHBoxLayout(row)
            rl.addWidget(QLabel(model["name"]))
            rl.addWidget(QLabel(model["meta"]))
            status = QLabel("推荐" if model["id"] == rec_id else "未检测")
            self.model_row_status[model["id"]] = status
            act = QPushButton("设为当前"); act.setObjectName("secondary")
            act.clicked.connect(lambda checked=False, m=model: self.activate_catalog_model(m["id"]))
            dl = QPushButton("下载一键安装"); dl.setObjectName("ghost")
            dl.clicked.connect(lambda checked=False, m=model: self.download_catalog_model(m["id"]))
            rl.addWidget(status)
            rl.addStretch(1)
            rl.addWidget(act)
            rl.addWidget(dl)
            c.addWidget(row)

        custom = QHBoxLayout()
        self.model_url_input = QLineEdit(); self.model_url_input.setPlaceholderText("自定义模型下载链接（GitHub 直链 / HTTP / HTTPS）")
        self.model_filename_input = QLineEdit(); self.model_filename_input.setPlaceholderText("文件名（可选）")
        add_btn = QPushButton("下载一键安装"); add_btn.setObjectName("secondary"); add_btn.clicked.connect(self.add_model_download_task)
        custom.addWidget(self.model_url_input, 2); custom.addWidget(self.model_filename_input, 1); custom.addWidget(add_btn)
        c.addLayout(custom)
        status_row = QHBoxLayout()
        self.model_download_status = QLabel("下载状态：暂无")
        self.model_retry_btn = QPushButton("重试下载")
        self.model_retry_btn.setObjectName("ghost")
        self.model_retry_btn.setEnabled(False)
        self.model_retry_btn.clicked.connect(self.retry_last_download)
        status_row.addWidget(self.model_download_status, 1)
        status_row.addWidget(self.model_retry_btn)
        c.addLayout(status_row)
        layout.addWidget(card)
        return page

    def _build_skills_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("contentPage")
        layout = QHBoxLayout(page)

        left = QFrame()
        left.setObjectName("panel")
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("Skills 列表"))
        self.skills_list = QListWidget()
        self.skills_list.setObjectName("list")
        self.skills_list.itemSelectionChanged.connect(self.on_skill_selected)
        ll.addWidget(self.skills_list, 1)
        r = QHBoxLayout()
        rb = QPushButton("刷新")
        rb.setObjectName("secondary")
        rb.clicked.connect(self.request_skills)
        tg = QPushButton("启用/停用")
        tg.setObjectName("primary")
        tg.clicked.connect(self.toggle_selected_skill)
        dl = QPushButton("删除自定义")
        dl.setObjectName("ghost")
        dl.clicked.connect(self.delete_selected_skill)
        r.addWidget(rb)
        r.addWidget(tg)
        r.addWidget(dl)
        ll.addLayout(r)

        right = QFrame()
        right.setObjectName("panel")
        rl = QVBoxLayout(right)
        rl.addWidget(QLabel("Skill 详情"))
        self.skill_detail = QTextEdit()
        self.skill_detail.setReadOnly(True)
        self.skill_detail.setObjectName("logView")
        rl.addWidget(self.skill_detail, 1)

        create_box = QFrame()
        create_box.setObjectName("providerBlock")
        cl = QVBoxLayout(create_box)
        cl.addWidget(QLabel("新建自定义 Skill"))
        self.skill_name_input = QLineEdit()
        self.skill_name_input.setPlaceholderText("例如：下载目录智能整理")
        self.skill_desc_input = QLineEdit()
        self.skill_desc_input.setPlaceholderText("描述（可选）")
        crow = QHBoxLayout()
        cb = QPushButton("创建 Skill")
        cb.setObjectName("secondary")
        cb.clicked.connect(self.create_skill)
        crow.addWidget(cb)
        crow.addStretch(1)
        cl.addWidget(self.skill_name_input)
        cl.addWidget(self.skill_desc_input)
        cl.addLayout(crow)
        rl.addWidget(create_box)

        self.skill_result = QLabel("Skill 管理就绪")
        rl.addWidget(self.skill_result)
        layout.addWidget(left, 1)
        layout.addWidget(right, 1)
        return page

    def _build_im_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("contentPage")
        layout = QVBoxLayout(page)

        cfg = QFrame(); cfg.setObjectName("panel")
        c = QVBoxLayout(cfg)
        c.addWidget(QLabel("IM 管理（默认展示 QQ / Webhook）"))
        qq_guide = QLabel(
            "QQ 接入指南（推荐个人开发者）：\n"
            "1) 先启动 OneBot / NapCat / LLOneBot 等 QQ 网关；\n"
            "2) 在下方“QQ 机器人（OneBot）”填写群号、可选 Access Token、HTTP 地址；\n"
            "3) 勾选“启用”并保存配置；\n"
            "4) 使用“一键测试全部通道”测试 /status、/task 指令。\n"
            "说明：微信通道当前未内置。"
        )
        qq_guide.setWordWrap(True)
        qq_guide.setObjectName("imGuide")
        c.addWidget(qq_guide)
        self.im_notify_success = QCheckBox("任务成功通知")
        self.im_notify_fail = QCheckBox("任务失败通知")
        self.im_notify_success.setChecked(True)
        self.im_notify_fail.setChecked(True)
        nrow = QHBoxLayout(); nrow.addWidget(self.im_notify_success); nrow.addWidget(self.im_notify_fail); nrow.addStretch(1)
        c.addLayout(nrow)

        self.telegram_block = ProviderBlock("Telegram", "Bot Token", "Chat ID（逗号分隔）")
        self.discord_block = ProviderBlock("Discord", "Bot Token", "Channel ID")
        self.qq_block = ProviderBlock("QQ 机器人（OneBot）", "Access Token（可选）", "群号（逗号分隔）")
        self.webhook_block = ProviderBlock("Webhook", "Secret（签名密钥）", "允许频道 ID（逗号分隔）")

        self.im_advanced_toggle = QCheckBox("显示高级通道（Telegram / Discord）")
        self.im_advanced_toggle.setChecked(False)
        c.addWidget(self.im_advanced_toggle)
        self.im_advanced_wrap = QFrame()
        self.im_advanced_wrap.setObjectName("providerBlock")
        aw = QVBoxLayout(self.im_advanced_wrap)
        aw.setContentsMargins(8, 8, 8, 8)
        aw.setSpacing(8)
        aw.addWidget(self.telegram_block)
        aw.addWidget(self.discord_block)
        c.addWidget(self.im_advanced_wrap)
        self.im_advanced_wrap.setVisible(False)
        self.im_advanced_toggle.toggled.connect(self.im_advanced_wrap.setVisible)

        qq_group = QFrame(); qq_group.setObjectName("providerBlock")
        qq_layout = QVBoxLayout(qq_group)
        qq_layout.setContentsMargins(8, 8, 8, 8)
        qq_layout.setSpacing(8)
        qq_layout.addWidget(self.qq_block)
        self.qq_endpoint_input = QLineEdit()
        self.qq_endpoint_input.setPlaceholderText("QQ OneBot HTTP 地址（默认 http://127.0.0.1:5700）")
        qq_layout.addWidget(self.qq_endpoint_input)
        c.addWidget(qq_group)

        webhook_group = QFrame(); webhook_group.setObjectName("providerBlock")
        webhook_layout = QVBoxLayout(webhook_group)
        webhook_layout.setContentsMargins(8, 8, 8, 8)
        webhook_layout.setSpacing(8)
        webhook_layout.addWidget(self.webhook_block)
        c.addWidget(webhook_group)

        ar = QHBoxLayout()
        sv = QPushButton("保存 IM 配置"); sv.setObjectName("primary"); sv.clicked.connect(self.save_im_config)
        rl = QPushButton("重新加载"); rl.setObjectName("secondary"); rl.clicked.connect(self.load_im_config)
        ta = QPushButton("一键测试全部通道"); ta.setObjectName("ghost"); ta.clicked.connect(self.test_all_im_channels)
        ar.addWidget(sv); ar.addWidget(rl); ar.addWidget(ta); ar.addStretch(1)
        c.addLayout(ar)
        self.im_result = QLabel("IM 配置未保存")
        c.addWidget(self.im_result)

        logs = QFrame(); logs.setObjectName("panel")
        l = QVBoxLayout(logs)
        l.addWidget(QLabel("IM 消息日志（logs/im.log）"))
        self.im_logs = QTextEdit(); self.im_logs.setReadOnly(True); self.im_logs.setObjectName("logView")
        l.addWidget(self.im_logs)

        layout.addWidget(cfg)
        layout.addWidget(logs, 1)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QScrollArea()
        page.setObjectName("contentPageScroll")
        page.setWidgetResizable(True)
        page.setFrameShape(QFrame.NoFrame)
        wrap = QWidget()
        wrap.setObjectName("contentPage")
        page.setWidget(wrap)
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(8, 8, 8, 10)
        layout.setSpacing(10)

        hero = QFrame()
        hero.setObjectName("homeHero")
        h = QVBoxLayout(hero)
        h.setContentsMargins(20, 18, 20, 18)
        h.setSpacing(8)
        t = QLabel("WcClaw 设置中心")
        t.setObjectName("qclawTitle")
        t.setAlignment(Qt.AlignCenter)
        s = QLabel("服务状态、快捷操作、日志和系统参数")
        s.setObjectName("qclawSub")
        s.setAlignment(Qt.AlignCenter)
        h.addWidget(t)
        h.addWidget(s)
        layout.addWidget(hero)

        quick = QFrame()
        quick.setObjectName("panel")
        q = QHBoxLayout(quick)
        q.setContentsMargins(12, 10, 12, 10)
        q.setSpacing(8)
        for text, fn, oid in [
            ("启动模型", self.start_model, "primary"),
            ("一句话建任务", lambda: self.switch_page("nl"), "secondary"),
            ("任务与日志", lambda: self.switch_page("tasks"), "secondary"),
            ("Skills 管理", lambda: self.switch_page("skills"), "secondary"),
            ("IM 管理", lambda: self.switch_page("im"), "ghost"),
        ]:
            btn = QPushButton(text)
            btn.setObjectName(oid)
            btn.clicked.connect(fn)
            q.addWidget(btn)
        q.addStretch(1)
        layout.addWidget(quick)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        p1 = QFrame(); p1.setObjectName("panel")
        p1l = QVBoxLayout(p1)
        p1l.setContentsMargins(12, 10, 12, 10)
        p1l.addWidget(QLabel("最近运行任务（5 条）"))
        self.home_runs = QListWidget(); self.home_runs.setObjectName("list")
        p1l.addWidget(self.home_runs)
        p2 = QFrame(); p2.setObjectName("panel")
        p2l = QVBoxLayout(p2)
        p2l.setContentsMargins(12, 10, 12, 10)
        p2l.addWidget(QLabel("实时日志（最新）"))
        self.home_logs = QTextEdit(); self.home_logs.setReadOnly(True); self.home_logs.setObjectName("logView")
        p2l.addWidget(self.home_logs)
        grid.addWidget(p1, 0, 0)
        grid.addWidget(p2, 0, 1)
        layout.addLayout(grid)

        base_panel = QFrame()
        base_panel.setObjectName("panel")
        p = QVBoxLayout(base_panel)
        p.setContentsMargins(12, 10, 12, 10)
        p.setSpacing(8)
        p.addWidget(QLabel("基础设置"))
        row = QHBoxLayout()
        row.addWidget(QLabel("本地 API"))
        self.api_url_input = QLineEdit(self.api.base_url)
        ab = QPushButton("应用地址")
        ab.setObjectName("secondary")
        ab.clicked.connect(self.apply_api_url)
        row.addWidget(self.api_url_input, 1)
        row.addWidget(ab)
        p.addLayout(row)
        self.setting_openai_api = QCheckBox("启动时暴露 OpenAI 兼容接口")
        self.setting_access_token = QCheckBox("启用本地访问控制令牌")
        p.addWidget(self.setting_openai_api)
        p.addWidget(self.setting_access_token)
        p.addWidget(QLabel("项目名称：WcClaw 文出龙虾"))
        layout.addWidget(base_panel)

        update_panel = QFrame()
        update_panel.setObjectName("panel")
        up = QVBoxLayout(update_panel)
        up.setContentsMargins(12, 10, 12, 10)
        up.setSpacing(8)
        up.addWidget(QLabel("版本更新"))
        urow = QHBoxLayout()
        urow.addWidget(QLabel("更新清单地址"))
        self.update_manifest_input = QLineEdit(str(self.ui_cfg.get("update_manifest_url", DEFAULT_UPDATE_MANIFEST_URL)))
        self.update_manifest_input.setPlaceholderText("https://cuiwenchu.com/update.json")
        urow.addWidget(self.update_manifest_input, 1)
        up.addLayout(urow)
        grow = QHBoxLayout()
        grow.addWidget(QLabel("GitHub 仓库"))
        self.github_repo_input = QLineEdit(str(self.ui_cfg.get("github_repo", DEFAULT_GITHUB_REPO)))
        self.github_repo_input.setPlaceholderText("例如：cuiwenchu/wcclaw")
        grow.addWidget(self.github_repo_input, 1)
        up.addLayout(grow)
        ub = QHBoxLayout()
        check_btn = QPushButton("检查更新")
        check_btn.setObjectName("secondary")
        check_btn.clicked.connect(lambda: self.check_update(manual=True))
        apply_url_btn = QPushButton("保存地址")
        apply_url_btn.setObjectName("ghost")
        apply_url_btn.clicked.connect(self.save_update_manifest_url)
        update_btn = QPushButton("立即更新")
        update_btn.setObjectName("primary")
        update_btn.clicked.connect(self.start_update_install)
        self.update_install_btn = update_btn
        ub.addWidget(check_btn)
        ub.addWidget(apply_url_btn)
        ub.addWidget(update_btn)
        ub.addStretch(1)
        up.addLayout(ub)
        self.update_status_label = QLabel(f"当前版本：v{APP_VERSION}")
        up.addWidget(self.update_status_label)
        layout.addWidget(update_panel)
        return page

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            #appRoot { background: #f3f7fb; }
            QWidget { color: #26384e; font-family: 'Microsoft YaHei UI'; font-size: 14px; }
            #leftPanel { background: #ffffff; border-right: 1px solid #dde7f1; }
            #rightPanel { background: #f3f7fb; }
            #contentPage { background: #f3f7fb; }
            #contentPageScroll { background: transparent; border: none; }
            #contentPageScroll > QWidget > QWidget { background: #f3f7fb; }
            #bottomNav { background: transparent; border-top: 1px solid #dce8f3; }
            #search, QLineEdit { background: #ffffff; border: 1px solid #d6e2ee; border-radius: 12px; padding: 8px 10px; }
            QTextEdit { border: 1px solid #d6e2ee; border-radius: 12px; background: #ffffff; padding: 8px; }
            #navBtn { text-align: left; background: #f7fafe; border: 1px solid #dce8f3; border-radius: 11px; padding: 9px 12px; }
            #navBtn:hover { background: #eef6ff; border: 1px solid #bdd6ed; }
            #navBtn:checked { background: #e9f4ff; border: 1px solid #aac9ea; color: #1f5c97; font-weight: 600; }
            #topTitle { font-size: 19px; font-weight: 700; color: #203349; }
            #heroTitle { font-size: 36px; font-weight: 700; color: #21354b; }
            #homeHero { background: #ffffff; border: 1px solid #dce8f3; border-radius: 16px; }
            #qclawTitle { font-size: 56px; font-weight: 700; color: #2b3650; letter-spacing: 1px; }
            #qclawSub { font-size: 18px; color: #6f8297; }
            #abilityCard { background: #f8fbff; border: 1px solid #dce8f3; border-radius: 12px; }
            #abilityTitle { font-size: 21px; font-weight: 700; color: #293d56; }
            #abilityDesc { font-size: 13px; color: #7b8fa4; }
            #statusLamp { background: #ffffff; border: 1px solid #dce8f3; border-radius: 12px; }
            #lampDot { font-size: 16px; color: #ea6d76; }
            #lampDot[on="true"] { color: #1f9f74; }
            #lampValue { color: #5f748a; font-size: 12px; }
            #panel { background: #ffffff; border: 1px solid #dce8f3; border-radius: 14px; padding: 6px; }
            #list { border: 1px solid #d8e4f0; border-radius: 12px; background: #fbfdff; padding: 4px; }
            #logView { border: 1px solid #d8e4f0; border-radius: 12px; background: #fbfdff; padding: 8px; }
            #chatView { border: 1px solid #d8e4f0; border-radius: 16px; background: #fbfdff; padding: 10px; }
            QPushButton#primary { background: #2f88c8; color: #ffffff; border: 1px solid #549fd5; border-radius: 10px; padding: 8px 12px; }
            QPushButton#secondary { background: #edf5fc; color: #33506d; border: 1px solid #c8dcef; border-radius: 10px; padding: 8px 12px; }
            QPushButton#ghost { background: #ffffff; color: #3d607e; border: 1px solid #d1e0ef; border-radius: 10px; padding: 8px 12px; }
            #providerBlock { background: #f9fcff; border: 1px solid #dbe8f4; border-radius: 12px; }
            #imGuide { background: #f7fbff; border: 1px solid #d5e5f4; border-radius: 10px; padding: 10px 12px; color: #45617c; }
            #homeInput { border-radius: 18px; padding: 10px 12px; }
            #leftBrandLogo { background: transparent; }
            QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px; border: 1px solid #b6cade; background: #ffffff; }
            QCheckBox::indicator:checked { background: #2f88c8; border: 1px solid #5ba6db; }
            QScrollBar:vertical { border: none; background: transparent; width: 8px; margin: 8px 2px 8px 2px; }
            QScrollBar::handle:vertical { background: #c3d5e8; border-radius: 4px; min-height: 36px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
            """
        )

    def switch_page(self, key: str) -> None:
        self.stack.setCurrentIndex(self.page_index.get(key, 0))
        titles = {
            "home": "",
            "nl": "自然语言任务",
            "tasks": "任务列表",
            "model": "模型管理",
            "skills": "Skills 管理",
            "im": "IM 管理",
            "settings": "设置",
        }
        self.top_title.setText(titles.get(key, "WcClaw"))
        self.top_title.setVisible(key != "home")
        self.left_task_button.setChecked(key == "tasks")
        for k, btn in self.bottom_nav_buttons.items():
            btn.setChecked(k == key)
        if key in ["tasks", "nl"]:
            self.refresh_tasks_page()
        if key == "skills":
            self.request_skills()
        if key == "im":
            self.refresh_im_logs()

    def show_error(self, text: str) -> None:
        QMessageBox.warning(self, "WcClaw", self._friendly_error(text))

    def _friendly_error(self, text: str) -> str:
        raw = (text or "").strip()
        lower = raw.lower()
        if "llama-server not found" in lower:
            return "缺少本地模型运行组件：tools/llama-server.exe。请先放入该文件后再启动模型。"
        if "no local model file" in lower:
            return "未检测到本地模型文件。请先在模型页点击“下载一键安装”。"
        if "download url must start with http/https" in lower:
            return "下载链接格式不正确，请使用 http/https 直链。"
        if "model file not found" in lower:
            return "模型文件不存在，可能未下载完成。请重新下载或重试。"
        if "http 4" in lower or "http 5" in lower:
            return f"网络请求失败：{raw}。请检查网络/代理后重试。"
        return raw

    def _recommended_model_id(self) -> str:
        if self.machine_profile == "low":
            return "tinyllama-1.1b-q4"
        if self.machine_profile == "mid":
            return "qwen2.5-1.5b-q4"
        return "qwen2.5-7b-q4"

    def _network_ok_for_model_download(self) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.5)
        try:
            return sock.connect_ex(("huggingface.co", 443)) == 0
        except Exception:
            return False
        finally:
            sock.close()

    def _maybe_run_startup_wizard(self) -> None:
        dlg = QDialog(self)
        dlg.setObjectName("startupWizard")
        dlg.setWindowTitle("WcClaw 新手 3 分钟上手")
        dlg.resize(580, 260)
        dlg.setAttribute(Qt.WA_StyledBackground, True)
        dlg.setStyleSheet(
            """
            QDialog#startupWizard {
                background: #f7fbff;
            }
            QFrame#startupCard {
                background: #ffffff;
                border: 1px solid #d4e3f1;
                border-radius: 12px;
            }
            QFrame#startupCard QLabel {
                color: #1f3044;
                background: transparent;
                font-size: 15px;
            }
            QFrame#startupCard QPushButton {
                color: #2a4058;
                background: #edf5fc;
                border: 1px solid #c8dcef;
                border-radius: 8px;
                padding: 6px 12px;
                font-size: 14px;
                font-weight: 600;
            }
            QFrame#startupCard QPushButton:hover {
                background: #e6f0fa;
            }
            """
        )
        l = QVBoxLayout(dlg)
        l.setContentsMargins(12, 12, 12, 12)
        card = QFrame()
        card.setObjectName("startupCard")
        l.addWidget(card)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 14, 14, 14)
        card_layout.setSpacing(10)
        grade_map = {"low": "低配", "mid": "中配", "high": "高配"}
        rec_id = self._recommended_model_id()
        rec_name = next((x["name"] for x in self.model_catalog if x["id"] == rec_id), "TinyLlama 1.1B")
        llama_exists = (PROJECT_ROOT / "tools" / "llama-server.exe").exists()
        net_ok = self._network_ok_for_model_download()
        checks = [
            f"机器档位：{grade_map.get(self.machine_profile, '中配')}",
            f"推荐模型：{rec_name}",
            f"模型下载网络：{'可访问' if net_ok else '不可访问（可稍后配置代理）'}",
            f"本地运行组件：{'已检测到' if llama_exists else '未检测到 tools/llama-server.exe'}",
            f"项目目录：{PROJECT_ROOT}",
        ]
        for line in checks:
            lb = QLabel(line)
            lb.setStyleSheet("color:#1f3044; background:#ffffff;")
            card_layout.addWidget(lb)
        hint = QLabel("建议：点击“安装推荐模型”可直接下载、安装、激活并自动自检。")
        hint.setStyleSheet("color:#1f3044; background:#ffffff;")
        card_layout.addWidget(hint)
        buttons = QDialogButtonBox()
        install_btn = buttons.addButton("安装推荐模型", QDialogButtonBox.AcceptRole)
        guide_btn = buttons.addButton("查看完整指南", QDialogButtonBox.ActionRole)
        skip_btn = buttons.addButton("稍后再说", QDialogButtonBox.RejectRole)
        card_layout.addWidget(buttons)

        def do_install() -> None:
            self.download_catalog_model(rec_id)
            dlg.accept()
            self.switch_page("model")

        install_btn.clicked.connect(do_install)
        guide_btn.clicked.connect(self.show_quickstart_guide)
        skip_btn.clicked.connect(dlg.reject)
        dlg.exec()

    def show_quickstart_guide(self) -> None:
        text = (
            "WcClaw 新手 3 分钟上手\n\n"
            "第 1 分钟：安装模型\n"
            "1. 点击底部“模型管理”。\n"
            "2. 直接点“下载一键安装”（推荐模型）。\n"
            "3. 下载完成后点“启动模型”。\n\n"
            "第 2 分钟：执行第一条任务\n"
            "1. 回到首页聊天框。\n"
            "2. 输入：整理 tasks 目录并输出文件清单。\n"
            "3. 点击“执行”，系统会自动生成并运行任务。\n\n"
            "第 3 分钟：可选配置 QQ 远控\n"
            "1. 打开“IM 管理”，在 QQ 区填写 OneBot 地址和群号。\n"
            "2. 启用后保存配置，使用“一键测试全部通道”验证连通。\n\n"
            "提示：如果模型启动失败，请先确认 tools/llama-server.exe 已存在。"
        )
        QMessageBox.information(self, "WcClaw 新手指南", text)

    def quickstart_step_model(self) -> None:
        self.switch_page("model")
        self.model_download_status.setText("新手引导：先点击推荐模型“下载一键安装”，完成后再点“启动模型”。")

    def quickstart_step_task(self) -> None:
        self.switch_page("home")
        self.chat_input.setText("整理 tasks 目录并输出文件清单")
        self.chat_input.setFocus()
        self._set_text_limited(
            self.chat_view,
            self.chat_view.toPlainText() + "\n系统：已填入示例任务，点击“执行”即可体验。",
        )

    def quickstart_step_im(self) -> None:
        self.switch_page("im")
        self.im_result.setText("新手引导：优先配置 QQ（OneBot）并发送 /status 测试连通性。")

    def _send_home_input(self) -> None:
        text = self.chat_input.text().strip()
        if not text:
            self.show_error("请输入任务或问题")
            return
        self.chat_input.clear()
        self._set_text_limited(self.chat_view, self.chat_view.toPlainText() + f"\n你：{text}")
        self._submit_one_shot(
            "home_generate_run",
            "POST",
            "/api/llm/generate_task",
            payload={"instruction": text, "auto_run": True},
            timeout=25,
            on_done=self._after_home_generate_run,
        )

    def _after_home_generate_run(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self._set_text_limited(
                self.chat_view,
                self.chat_view.toPlainText() + f"\n系统：任务生成失败：{result.get('error', '未知错误')}",
            )
            return
        data = result.get("data", {})
        task = data.get("task", {})
        run = data.get("run", {}) or {}
        msg = f"\n系统：已创建并开始执行任务 {task.get('name', '')}（{run.get('id', '')}）"
        self._set_text_limited(self.chat_view, self.chat_view.toPlainText() + msg)

    def _submit_request(
        self,
        key: str,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: int = 6,
        skip_if_running: bool = True,
    ) -> None:
        if skip_if_running and key in self.inflight_keys:
            return
        self.inflight_keys.add(key)
        worker = ApiWorker(self.api, key, method, path, payload=payload, timeout=timeout)
        worker.signals.finished.connect(self._on_worker_result)
        self.thread_pool.start(worker)

    def _submit_one_shot(
        self,
        tag: str,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: int = 8,
        on_done: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        req_key = f"{tag}_{time.time_ns()}"
        if on_done:
            self.one_shot_handlers[req_key] = on_done
        self._submit_request(req_key, method, path, payload=payload, timeout=timeout, skip_if_running=False)

    def _submit_func_one_shot(
        self,
        tag: str,
        fn: Callable[[], Dict[str, Any]],
        on_done: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        req_key = f"{tag}_{time.time_ns()}"
        if on_done:
            self.one_shot_handlers[req_key] = on_done
        worker = FuncWorker(req_key, fn)
        worker.signals.finished.connect(self._on_worker_result)
        self.thread_pool.start(worker)

    def _on_worker_result(self, key: str, result: Dict[str, Any]) -> None:
        self.inflight_keys.discard(key)
        one_shot = self.one_shot_handlers.pop(key, None)
        if one_shot:
            one_shot(result)
            return
        if key == "poll_tasks":
            self._handle_tasks_result(result)
            return
        if key == "poll_runs":
            self._handle_runs_result(result)
            return
        if key == "poll_model_status":
            self._handle_model_status_result(result)
            return
        if key == "poll_model_catalog":
            self._handle_model_catalog_result(result)
            return
        if key == "poll_model_downloads":
            self._handle_model_downloads_result(result)
            return
        if key == "poll_im_status":
            self._handle_im_status_result(result)
            return
        if key == "poll_dashboard_logs":
            self._handle_dashboard_logs_result(result)
            return
        if key == "poll_im_logs":
            self._handle_im_logs_result(result)
            return
        if key == "poll_run_logs":
            self._handle_run_logs_result(result)
            return
        if key == "poll_skills":
            self._handle_skills_result(result)
            return
        if key == "load_im_config":
            self._handle_load_im_config_result(result)
            return

    def _trim_lines(self, text: str, max_lines: int) -> str:
        lines = text.splitlines()
        if len(lines) <= max_lines:
            return text
        return "\n".join(lines[-max_lines:])

    def _set_text_limited(self, widget: QTextEdit, text: str, max_lines: Optional[int] = None) -> None:
        limit = max_lines if max_lines is not None else self.max_log_lines
        trimmed = self._trim_lines(text, limit)
        if widget.toPlainText() != trimmed:
            widget.setPlainText(trimmed)

    def _merge_home_logs(self) -> None:
        merged = "[APP]\n" + (self.latest_app_logs or "") + "\n[IM]\n" + (self.latest_im_logs or "")
        self._set_text_limited(self.home_logs, merged)

    def request_task_and_runs(self) -> None:
        self._submit_request("poll_tasks", "GET", "/api/tasks", timeout=4)
        self._submit_request("poll_runs", "GET", "/api/runs", timeout=4)

    def request_model_status(self) -> None:
        self._submit_request("poll_model_status", "GET", "/api/status", timeout=4)
        self._submit_request("poll_model_catalog", "GET", "/api/models/catalog", timeout=6)
        self._submit_request("poll_model_downloads", "GET", "/api/models/downloads", timeout=6)

    def request_im_status(self) -> None:
        self._submit_request("poll_im_status", "GET", "/api/status", timeout=4)

    def request_logs(self) -> None:
        self._submit_request("poll_dashboard_logs", "GET", "/api/dashboard", timeout=4)
        self._submit_request("poll_im_logs", "GET", "/api/im/logs?lines=150", timeout=4)
        item = self.run_list.currentItem()
        if item and self.stack.currentIndex() == self.page_index["tasks"]:
            run_id = item.data(Qt.UserRole)
            self._submit_request("poll_run_logs", "GET", f"/api/runs/{run_id}/logs?lines=150", timeout=4)

    def request_im_config(self) -> None:
        self._submit_request("load_im_config", "GET", "/api/im/config", timeout=5)

    def request_skills(self) -> None:
        self._submit_request("poll_skills", "GET", "/api/skills", timeout=5)

    def _handle_tasks_result(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            return
        tasks = result.get("data", [])[:300]
        digest = "|".join([f"{x.get('id','')}:{x.get('created_at','')}" for x in tasks])
        if digest == self.task_digest:
            return
        self.task_digest = digest
        selected_id = self.task_list.currentItem().data(Qt.UserRole) if self.task_list.currentItem() else None
        self.task_list.clear()
        for task in tasks:
            item = QListWidgetItem(f"{task.get('name', '')} [{task.get('id', '')}]")
            item.setData(Qt.UserRole, task.get("id", ""))
            self.task_list.addItem(item)
            if selected_id and selected_id == task.get("id"):
                self.task_list.setCurrentItem(item)

    def _handle_runs_result(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            return
        runs = result.get("data", [])[:300]
        digest = "|".join([f"{x.get('id','')}:{x.get('status','')}:{x.get('ended_at','')}" for x in runs])
        if digest == self.run_digest:
            active = len([r for r in runs if r.get("status") == "running"])
            self.lamp_task.set_state(True, f"运行中 {active}")
            return
        self.run_digest = digest
        selected_id = self.run_list.currentItem().data(Qt.UserRole) if self.run_list.currentItem() else None
        self.run_list.clear()
        for run in runs:
            text = f"{run.get('id', '')} | {run.get('task_name', '')} | {run.get('status', '')}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, run.get("id", ""))
            self.run_list.addItem(item)
            if selected_id and selected_id == run.get("id"):
                self.run_list.setCurrentItem(item)

        self.home_runs.clear()
        for run in runs[:5]:
            self.home_runs.addItem(f"{run.get('status', '')} | {run.get('task_name', '')} | {run.get('started_at', '')}")

        active = len([r for r in runs if r.get("status") == "running"])
        self.lamp_task.set_state(True, f"运行中 {active}")

    def _handle_model_status_result(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.lamp_model.set_state(False, "离线")
            self.model_status_label.setText("模型状态：后端未连接")
            return
        data = result.get("data", {})
        model = data.get("model", {})
        model_on = bool(model.get("reachable"))
        self.lamp_model.set_state(model_on, "在线" if model_on else "离线")
        active = model.get("active_model_file", "") or "未选择"
        self.model_status_label.setText(f"模型状态：{'在线' if model_on else '离线'} | 当前: {active} | URL: {model.get('url', '')}")

    def _handle_model_catalog_result(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            return
        catalog = result.get("data", []) or []
        for item in catalog:
            model_id = item.get("id", "")
            label = self.model_row_status.get(model_id)
            if not label:
                continue
            if item.get("is_active"):
                label.setText("当前模型")
            elif item.get("downloaded"):
                label.setText("已下载")
            else:
                label.setText("未下载")

    def _handle_model_downloads_result(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            return
        jobs = result.get("data", []) or []
        if not jobs:
            self.model_download_status.setText("下载状态：暂无")
            self.model_retry_btn.setEnabled(False)
            return
        latest = jobs[0]
        self.last_download_job_id = str(latest.get("id", ""))
        text = (
            f"下载状态：{latest.get('filename', '')} | {latest.get('status', '')} | "
            f"{latest.get('progress', 0)}%"
        )
        speed_bps = int(latest.get("speed_bps", 0) or 0)
        if speed_bps > 0:
            text += f" | {round(speed_bps / 1024 / 1024, 2)} MB/s"
        eta = int(latest.get("eta_seconds", -1) or -1)
        if eta >= 0:
            text += f" | 预计剩余 {eta}s"
        check = latest.get("self_check")
        if isinstance(check, dict):
            text += f" | 自检: {'通过' if check.get('ok') else '失败'}"
        if latest.get("error"):
            text += f" | {latest.get('error')}"
        self.model_retry_btn.setEnabled(latest.get("status") == "failed")
        self.model_download_status.setText(text)

    def _handle_im_status_result(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.lamp_im.set_state(False, "离线")
            return
        data = result.get("data", {})
        im = data.get("im", {})
        im_on = bool(im.get("running")) and (
            bool(im.get("telegram_enabled"))
            or bool(im.get("discord_enabled"))
            or bool(im.get("qq_enabled"))
            or bool(im.get("webhook_enabled"))
        )
        self.lamp_im.set_state(im_on, "已连接" if im_on else "未连接")

    def _handle_dashboard_logs_result(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            return
        self.latest_app_logs = result.get("data", {}).get("logs", "")
        self._merge_home_logs()

    def _handle_im_logs_result(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            return
        logs = result.get("data", {}).get("logs", "") or "暂无 IM 日志"
        self.latest_im_logs = logs
        self._set_text_limited(self.im_logs, logs)
        self._merge_home_logs()

    def _handle_run_logs_result(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            return
        logs = result.get("data", {}).get("logs", "") or "暂无日志"
        self._set_text_limited(self.run_logs, logs)

    def _handle_load_im_config_result(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.im_result.setText("IM 配置读取失败")
            return
        cfg = result.get("data", {})
        tg = cfg.get("telegram", {})
        self.telegram_block.enabled.setChecked(bool(tg.get("enabled")))
        self.telegram_block.token.setText(str(tg.get("token", "")))
        self.telegram_block.target.setText(to_csv(tg.get("chat_ids", [])))
        self.telegram_block.users.setText(to_csv(tg.get("allowed_user_ids", [])))

        dc = cfg.get("discord", {})
        self.discord_block.enabled.setChecked(bool(dc.get("enabled")))
        self.discord_block.token.setText(str(dc.get("bot_token", "")))
        self.discord_block.target.setText(str(dc.get("channel_id", "")))
        self.discord_block.users.setText(to_csv(dc.get("allowed_user_ids", [])))
        advanced_on = bool(tg.get("enabled")) or bool(dc.get("enabled"))
        self.im_advanced_toggle.setChecked(advanced_on)

        qq = cfg.get("qq", {})
        self.qq_block.enabled.setChecked(bool(qq.get("enabled")))
        self.qq_block.token.setText(str(qq.get("access_token", "")))
        self.qq_block.target.setText(to_csv(qq.get("group_ids", [])))
        self.qq_block.users.setText(to_csv(qq.get("allowed_user_ids", [])))
        self.qq_endpoint_input.setText(str(qq.get("endpoint", "http://127.0.0.1:5700")))

        wb = cfg.get("webhook", {})
        self.webhook_block.enabled.setChecked(bool(wb.get("enabled")))
        self.webhook_block.token.setText(str(wb.get("secret", "")))
        self.webhook_block.target.setText(to_csv(wb.get("allowed_channels", [])))
        self.webhook_block.users.setText(to_csv(wb.get("allowed_user_ids", [])))

        n = cfg.get("notifications", {})
        self.im_notify_success.setChecked(bool(n.get("on_success", True)))
        self.im_notify_fail.setChecked(bool(n.get("on_failure", True)))
        self.im_result.setText("IM 配置已加载")

    def _handle_skills_result(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            return
        items = result.get("data", []) or []
        selected_id = self.skills_list.currentItem().data(Qt.UserRole) if self.skills_list.currentItem() else None
        self.skills_list.clear()
        for skill in items:
            state = "启用" if skill.get("enabled") else "停用"
            tag = "内置" if skill.get("builtin") else "自定义"
            text = f"{skill.get('name', '')} | {state} | {tag}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, skill.get("id", ""))
            item.setData(Qt.UserRole + 1, skill)
            self.skills_list.addItem(item)
            if selected_id and selected_id == skill.get("id"):
                self.skills_list.setCurrentItem(item)
        if self.skills_list.count() and not self.skills_list.currentItem():
            self.skills_list.setCurrentRow(0)
        if not items:
            self.skill_detail.setPlainText("暂无 Skills")

    def on_skill_selected(self) -> None:
        item = self.skills_list.currentItem()
        if not item:
            self.skill_detail.setPlainText("请选择一个 Skill")
            return
        skill = item.data(Qt.UserRole + 1) or {}
        text = (
            f"名称：{skill.get('name', '')}\n"
            f"ID：{skill.get('id', '')}\n"
            f"状态：{'启用' if skill.get('enabled') else '停用'}\n"
            f"类型：{'内置' if skill.get('builtin') else '自定义'}\n"
            f"入口：{skill.get('entry', '')}\n"
            f"配置：{skill.get('config_path', '')}\n\n"
            f"说明：{skill.get('description', '')}"
        )
        self.skill_detail.setPlainText(text)

    def toggle_selected_skill(self) -> None:
        item = self.skills_list.currentItem()
        if not item:
            self.show_error("请先选择 Skill")
            return
        skill = item.data(Qt.UserRole + 1) or {}
        target = not bool(skill.get("enabled"))
        skill_id = str(skill.get("id", "")).strip()
        if not skill_id:
            self.show_error("Skill ID 无效")
            return
        self._submit_one_shot(
            "toggle_skill",
            "POST",
            f"/api/skills/{skill_id}/enable",
            payload={"enabled": target},
            on_done=self._after_toggle_skill,
        )

    def _after_toggle_skill(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.show_error(f"切换 Skill 状态失败：{result.get('error', '未知错误')}")
            return
        item = result.get("data", {}) or {}
        self.skill_result.setText(
            f"Skill 状态已更新：{item.get('name', '')} -> {'启用' if item.get('enabled') else '停用'}"
        )
        self.request_skills()

    def create_skill(self) -> None:
        name = self.skill_name_input.text().strip()
        if not name:
            self.show_error("请输入 Skill 名称")
            return
        desc = self.skill_desc_input.text().strip()
        self._submit_one_shot(
            "create_skill",
            "POST",
            "/api/skills",
            payload={"name": name, "description": desc},
            on_done=self._after_create_skill,
        )

    def _after_create_skill(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.show_error(f"创建 Skill 失败：{result.get('error', '未知错误')}")
            return
        item = result.get("data", {}) or {}
        self.skill_result.setText(f"Skill 已创建：{item.get('name', '')}")
        self.skill_name_input.clear()
        self.skill_desc_input.clear()
        self.request_skills()

    def delete_selected_skill(self) -> None:
        item = self.skills_list.currentItem()
        if not item:
            self.show_error("请先选择 Skill")
            return
        skill = item.data(Qt.UserRole + 1) or {}
        if skill.get("builtin"):
            self.show_error("内置 Skill 不可删除")
            return
        skill_id = str(skill.get("id", "")).strip()
        if not skill_id:
            self.show_error("Skill ID 无效")
            return
        self._submit_one_shot(
            "delete_skill",
            "POST",
            f"/api/skills/{skill_id}/delete",
            payload={},
            on_done=self._after_delete_skill,
        )

    def _after_delete_skill(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.show_error(f"删除 Skill 失败：{result.get('error', '未知错误')}")
            return
        item = result.get("data", {}) or {}
        self.skill_result.setText(f"Skill 已删除：{item.get('name', '')}")
        self.request_skills()

    def start_model(self) -> None:
        self._submit_one_shot(
            "start_model",
            "POST",
            "/api/model/start",
            on_done=lambda result: self._after_action_result(result, "启动模型失败"),
        )

    def stop_model(self) -> None:
        self._submit_one_shot(
            "stop_model",
            "POST",
            "/api/model/stop",
            on_done=lambda result: self._after_action_result(result, "停止模型失败"),
        )

    def _after_action_result(self, result: Dict[str, Any], fail_title: str) -> None:
        if not result.get("ok"):
            self.show_error(f"{fail_title}：{result.get('error', '未知错误')}")
            return
        self.refresh_all()

    def add_model_download_task(self) -> None:
        url = self.model_url_input.text().strip()
        if not url:
            self.show_error("请输入模型下载链接")
            return
        filename = self.model_filename_input.text().strip()
        self._submit_one_shot(
            "model_download_task_manual",
            "POST",
            "/api/models/download",
            payload={"url": url, "filename": filename, "auto_activate": True},
            timeout=20,
            on_done=self._after_model_download_submit,
        )

    def _after_model_download_submit(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.show_error(f"提交下载失败：{result.get('error', '未知错误')}")
            return
        self.last_download_job_id = str(result.get("data", {}).get("id", ""))
        self.model_retry_btn.setEnabled(False)
        self.model_download_status.setText("下载状态：任务已提交，正在下载...")
        self.request_model_status()

    def retry_last_download(self) -> None:
        if not self.last_download_job_id:
            self.show_error("没有可重试的下载任务")
            return
        self._submit_one_shot(
            "model_retry_download",
            "POST",
            f"/api/models/downloads/{self.last_download_job_id}/retry",
            payload={},
            timeout=12,
            on_done=self._after_model_download_submit,
        )

    def download_catalog_model(self, model_id: str) -> None:
        self._submit_one_shot(
            "model_download_catalog",
            "POST",
            "/api/models/download",
            payload={"model_id": model_id, "auto_activate": True},
            timeout=20,
            on_done=self._after_model_download_submit,
        )

    def activate_catalog_model(self, model_id: str) -> None:
        m = next((x for x in self.model_catalog if x["id"] == model_id), None)
        if not m:
            self.show_error("模型不存在")
            return
        self._submit_one_shot(
            "model_activate",
            "POST",
            "/api/models/activate",
            payload={"filename": m["filename"], "model_name": m["name"]},
            timeout=8,
            on_done=self._after_model_activate,
        )

    def _after_model_activate(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.show_error(f"设为当前失败：{result.get('error', '未知错误')}")
            return
        self._submit_one_shot(
            "model_self_check",
            "POST",
            "/api/models/self_check",
            payload={},
            timeout=20,
            on_done=self._after_model_self_check,
        )
        self.request_model_status()

    def _after_model_self_check(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.model_download_status.setText(f"下载状态：模型自检失败 {result.get('error', '未知错误')}")
            return
        data = result.get("data", {})
        self.model_download_status.setText(
            f"下载状态：模型自检{'通过' if data.get('ok') else '失败'} | {data.get('message', '')}"
        )

    def load_template_instruction(self) -> None:
        key = self.template_combo.currentText().strip()
        text = self.template_defs.get(key, "")
        if text:
            self.nl_input.setPlainText(text)

    def run_template_instruction(self) -> None:
        self.load_template_instruction()
        self.generate_task(True)

    def import_market_template(self) -> None:
        idx = self.market_list.currentRow()
        if idx < 0 or idx >= len(self.market_templates):
            self.show_error("请先选择模板")
            return
        item = self.market_templates[idx]
        task = item.get("task", {})
        pretty = json.dumps(task, ensure_ascii=False, indent=2)
        self.nl_input.setPlainText(
            f"模板：{item.get('name', '')}\n"
            f"说明：{item.get('description', '')}\n\n"
            f"{pretty}"
        )
        self.market_result.setText(f"已导入模板：{item.get('name', '')}")

    def run_market_template(self) -> None:
        idx = self.market_list.currentRow()
        if idx < 0 or idx >= len(self.market_templates):
            self.show_error("请先选择模板")
            return
        item = self.market_templates[idx]
        payload = item.get("task", {})
        self._submit_one_shot(
            "create_task_from_market",
            "POST",
            "/api/tasks",
            payload=payload,
            timeout=12,
            on_done=self._after_create_market_task,
        )

    def _after_create_market_task(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.show_error(f"模板导入失败：{result.get('error', '未知错误')}")
            return
        task = result.get("data", {}) or {}
        task_id = str(task.get("id", "")).strip()
        if not task_id:
            self.show_error("模板导入失败：任务 ID 缺失")
            return
        self._submit_one_shot(
            "run_task_from_market",
            "POST",
            f"/api/tasks/{task_id}/run",
            timeout=12,
            on_done=self._after_run_market_task,
        )

    def _after_run_market_task(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.show_error(f"模板运行失败：{result.get('error', '未知错误')}")
            return
        run = result.get("data", {}) or {}
        self.market_result.setText(f"模板已导入并运行：{run.get('id', '')}")
        self.switch_page("tasks")

    def generate_task(self, auto_run: bool) -> None:
        text = self.nl_input.toPlainText().strip()
        if not text:
            self.show_error("请输入自然语言指令")
            return
        self._submit_one_shot(
            "generate_task",
            "POST",
            "/api/llm/generate_task",
            payload={"instruction": text, "auto_run": auto_run},
            timeout=20,
            on_done=self._after_generate_task,
        )

    def _after_generate_task(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.show_error(f"生成任务失败：{result.get('error', '未知错误')}")
            return
        data = result.get("data", {})
        task = data.get("task", {})
        run = data.get("run")
        self.last_generated_task_id = task.get("id", "")
        self.generated_json.setPlainText(json.dumps(task, ensure_ascii=False, indent=2))
        if run:
            self.nl_result.setText(f"任务已生成并执行：{task.get('name', '')} / {run.get('id', '')}")
        else:
            self.nl_result.setText(f"任务已生成：{task.get('name', '')}（可直接运行）")
        self.switch_page("tasks")

    def run_generated_task(self) -> None:
        if not self.last_generated_task_id:
            self.show_error("还没有可运行的生成任务")
            return
        self._submit_one_shot(
            "run_generated_task",
            "POST",
            f"/api/tasks/{self.last_generated_task_id}/run",
            on_done=lambda result: self._after_run_action(result, "运行失败"),
        )

    def refresh_tasks_page(self) -> None:
        self.request_task_and_runs()

    def run_selected_task(self) -> None:
        item = self.task_list.currentItem()
        if not item:
            self.show_error("请先选择任务")
            return
        self._submit_one_shot(
            "run_selected_task",
            "POST",
            f"/api/tasks/{item.data(Qt.UserRole)}/run",
            on_done=lambda result: self._after_run_action(result, "运行任务失败"),
        )

    def _after_run_action(self, result: Dict[str, Any], fail_title: str) -> None:
        if not result.get("ok"):
            self.show_error(f"{fail_title}：{result.get('error', '未知错误')}")
            return
        self.switch_page("tasks")
        self.refresh_tasks_page()

    def stop_selected_run(self) -> None:
        item = self.run_list.currentItem()
        if not item:
            self.show_error("请先选择运行记录")
            return
        self._submit_one_shot(
            "stop_selected_run",
            "POST",
            f"/api/runs/{item.data(Qt.UserRole)}/stop",
            on_done=lambda result: self._after_run_action(result, "停止运行失败"),
        )

    def load_selected_run_logs(self) -> None:
        self.request_logs()

    def collect_im_payload(self) -> Dict[str, Any]:
        return {
            "telegram": {
                "enabled": self.telegram_block.enabled.isChecked(),
                "token": self.telegram_block.token.text().strip(),
                "chat_ids": split_csv(self.telegram_block.target.text()),
                "allowed_user_ids": split_csv(self.telegram_block.users.text()),
            },
            "discord": {
                "enabled": self.discord_block.enabled.isChecked(),
                "bot_token": self.discord_block.token.text().strip(),
                "channel_id": self.discord_block.target.text().strip(),
                "allowed_user_ids": split_csv(self.discord_block.users.text()),
            },
            "qq": {
                "enabled": self.qq_block.enabled.isChecked(),
                "endpoint": self.qq_endpoint_input.text().strip() or "http://127.0.0.1:5700",
                "access_token": self.qq_block.token.text().strip(),
                "group_ids": split_csv(self.qq_block.target.text()),
                "allowed_user_ids": split_csv(self.qq_block.users.text()),
            },
            "webhook": {
                "enabled": self.webhook_block.enabled.isChecked(),
                "secret": self.webhook_block.token.text().strip(),
                "allowed_channels": split_csv(self.webhook_block.target.text()),
                "allowed_user_ids": split_csv(self.webhook_block.users.text()),
            },
            "notifications": {
                "enabled": True,
                "on_success": self.im_notify_success.isChecked(),
                "on_failure": self.im_notify_fail.isChecked(),
            },
        }

    def save_im_config(self) -> None:
        self._submit_one_shot(
            "save_im_config",
            "POST",
            "/api/im/config",
            payload=self.collect_im_payload(),
            on_done=self._after_save_im_config,
        )

    def _after_save_im_config(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.show_error(f"保存失败：{result.get('error', '未知错误')}")
            return
        self.im_result.setText("IM 配置已保存")
        self.request_im_status()

    def load_im_config(self) -> None:
        self.request_im_config()

    def test_webhook_command(self) -> None:
        cmd = self.webhook_cmd_input.text().strip()
        if not cmd:
            self.show_error("请输入 Webhook 测试指令")
            return
        payload = {
            "text": cmd,
            "user_id": "ui-test-user",
            "channel_id": "ui-test-channel",
            "secret": self.webhook_block.token.text().strip(),
        }
        self._submit_one_shot(
            "test_webhook",
            "POST",
            "/api/im/webhook",
            payload=payload,
            on_done=self._after_test_webhook,
        )

    def _after_test_webhook(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.show_error(f"Webhook 测试失败：{result.get('error', '未知错误')}")
            return
        self.im_result.setText("Webhook 返回：" + result.get("data", {}).get("message", ""))
        self.refresh_im_logs()

    def test_qq_command(self) -> None:
        cmd = self.qq_cmd_input.text().strip()
        if not cmd:
            self.show_error("请输入 QQ 测试指令")
            return
        payload = {
            "text": cmd,
            "user_id": "ui-test-user",
            "channel_id": (split_csv(self.qq_block.target.text()) or ["0"])[0],
            "secret": self.qq_block.token.text().strip(),
        }
        self._submit_one_shot(
            "test_qq",
            "POST",
            "/api/im/qq/webhook",
            payload=payload,
            on_done=self._after_test_qq,
        )

    def _after_test_qq(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.show_error(f"QQ 测试失败：{result.get('error', '未知错误')}")
            return
        self.im_result.setText("QQ 返回：" + result.get("data", {}).get("message", ""))
        self.refresh_im_logs()

    def test_all_im_channels(self) -> None:
        self._submit_one_shot(
            "im_test_all",
            "POST",
            "/api/im/test_all",
            payload={},
            timeout=10,
            on_done=self._after_test_all_im_channels,
        )

    def _after_test_all_im_channels(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.show_error(f"通道测试失败：{result.get('error', '未知错误')}")
            return
        data = result.get("data", {}) or {}
        parts = []
        for k in ["telegram", "discord", "qq", "webhook"]:
            row = data.get(k, {})
            flag = "OK" if row.get("ok") else "未通过"
            parts.append(f"{k}:{flag}")
        self.im_result.setText("全通道测试结果：" + " | ".join(parts))
        self.refresh_im_logs()

    def refresh_im_logs(self) -> None:
        self._submit_request("poll_im_logs", "GET", "/api/im/logs?lines=150", timeout=4)

    def apply_api_url(self) -> None:
        text = self.api_url_input.text().strip()
        if not text.startswith("http"):
            self.show_error("API 地址必须是 http/https")
            return
        self.api.base_url = text.rstrip("/")
        self.refresh_all()

    def save_update_manifest_url(self) -> None:
        url = self.update_manifest_input.text().strip()
        repo = self.github_repo_input.text().strip()
        if not url.startswith("http"):
            self.show_error("更新清单地址必须是 http/https")
            return
        if "/" not in repo:
            self.show_error("GitHub 仓库格式错误，应为 owner/repo")
            return
        self.ui_cfg["update_manifest_url"] = url
        self.ui_cfg["github_repo"] = repo
        save_desktop_cfg(self.ui_cfg)
        self.update_status_label.setText(f"已保存更新配置：{url} / {repo}")

    def check_update(self, manual: bool = False) -> None:
        url = str(
            self.update_manifest_input.text().strip()
            if hasattr(self, "update_manifest_input")
            else self.ui_cfg.get("update_manifest_url", DEFAULT_UPDATE_MANIFEST_URL)
        )
        repo = str(
            self.github_repo_input.text().strip()
            if hasattr(self, "github_repo_input")
            else self.ui_cfg.get("github_repo", DEFAULT_GITHUB_REPO)
        )
        if hasattr(self, "update_status_label"):
            self.update_status_label.setText("正在检查更新...")

        def _work() -> Dict[str, Any]:
            candidates: List[Dict[str, Any]] = []
            errors: List[str] = []

            if url.startswith("http"):
                try:
                    full = url + ("&" if "?" in url else "?") + f"t={int(time.time())}"
                    resp = requests.get(full, timeout=8)
                    if resp.status_code < 400 and resp.text.strip():
                        payload = resp.json()
                        ver = str(payload.get("version", "")).strip()
                        if ver:
                            payload["source"] = "manifest"
                            payload["has_update"] = is_version_newer(ver, APP_VERSION)
                            candidates.append(payload)
                        else:
                            errors.append("manifest: 缺少 version")
                    else:
                        errors.append(f"manifest: HTTP {resp.status_code}")
                except Exception as exc:
                    errors.append(f"manifest: {exc}")

            if "/" in repo:
                try:
                    gh_api = f"https://api.github.com/repos/{repo}/releases/latest"
                    gh_resp = requests.get(gh_api, timeout=8, headers={"Accept": "application/vnd.github+json"})
                    if gh_resp.status_code < 400:
                        rel = gh_resp.json()
                        tag = str(rel.get("tag_name", "")).strip().lstrip("v")
                        if tag:
                            asset_url = ""
                            asset_sha = ""
                            for a in rel.get("assets", []) or []:
                                name = str(a.get("name", "")).lower()
                                if name.endswith(".zip") and "wcclaw" in name:
                                    asset_url = str(a.get("browser_download_url", "")).strip()
                                    break
                            item = {
                                "app": "wcclaw",
                                "version": tag,
                                "notes": str(rel.get("body", "") or "").strip(),
                                "package_url": asset_url,
                                "package_sha256": asset_sha,
                                "published_at": str(rel.get("published_at", "")).strip(),
                                "source": "github",
                            }
                            item["has_update"] = is_version_newer(tag, APP_VERSION)
                            candidates.append(item)
                        else:
                            errors.append("github: 缺少 tag_name")
                    else:
                        errors.append(f"github: HTTP {gh_resp.status_code}")
                except Exception as exc:
                    errors.append(f"github: {exc}")

                try:
                    raw_url = f"https://raw.githubusercontent.com/{repo}/main/update.json"
                    raw_resp = requests.get(raw_url, timeout=8)
                    if raw_resp.status_code < 400 and raw_resp.text.strip():
                        raw_payload = raw_resp.json()
                        raw_ver = str(raw_payload.get("version", "")).strip()
                        if raw_ver:
                            raw_payload["source"] = "github-update-json"
                            raw_payload["has_update"] = is_version_newer(raw_ver, APP_VERSION)
                            candidates.append(raw_payload)
                except Exception:
                    pass

            if not candidates:
                return {"ok": False, "error": " | ".join(errors) or "没有可用更新源", "data": None}

            best = candidates[0]
            for c in candidates[1:]:
                if is_version_newer(str(c.get("version", "")), str(best.get("version", ""))):
                    best = c
            best["debug_sources"] = candidates
            return {"ok": True, "data": best, "error": None}

        self._submit_func_one_shot(
            "check_update",
            _work,
            on_done=lambda result: self._after_check_update(result, manual),
        )

    def _after_check_update(self, result: Dict[str, Any], manual: bool) -> None:
        if not result.get("ok"):
            msg = f"检查更新失败：{result.get('error', '未知错误')}"
            if hasattr(self, "update_status_label"):
                self.update_status_label.setText(msg)
            if manual:
                self.show_error(msg)
            return
        data = result.get("data", {}) or {}
        remote_v = str(data.get("version", "")).strip()
        has_update = bool(data.get("has_update"))
        source = str(data.get("source", "")).strip() or "unknown"
        if hasattr(self, "update_status_label"):
            self.update_status_label.setText(f"当前 v{APP_VERSION}，远端 v{remote_v}（来源：{source}）")
        if not has_update:
            if manual:
                QMessageBox.information(self, "WcClaw", "当前已是最新版本")
            return
        if str(self.ui_cfg.get("ignore_update_version", "")) == remote_v and not manual:
            return
        self.pending_update_meta = data
        notes = str(data.get("notes", "")).strip()
        package_url = str(data.get("package_url", "") or data.get("url", "")).strip()
        if not package_url:
            if manual:
                QMessageBox.information(
                    self,
                    "发现新版本",
                    f"检测到新版本 v{remote_v}（来源：{source}），但未提供可下载的 zip 包链接。",
                )
            return
        btn = QMessageBox.question(
            self,
            "发现新版本",
            f"检测到新版本 v{remote_v}（来源：{source}）\n\n更新说明：{notes or '无'}\n\n是否现在下载并安装？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if btn == QMessageBox.Yes:
            self.start_update_install()

    def start_update_install(self) -> None:
        meta = self.pending_update_meta or {}
        package_url = str(meta.get("package_url", "") or meta.get("url", "")).strip()
        remote_v = str(meta.get("version", "")).strip()
        if not package_url:
            self.show_error("更新信息缺少 package_url/url")
            return
        if not package_url.lower().endswith(".zip"):
            self.show_error("请使用 zip 更新包（包含 exe + tools）")
            return
        if hasattr(self, "update_install_btn"):
            self.update_install_btn.setEnabled(False)
        if hasattr(self, "update_status_label"):
            self.update_status_label.setText("正在下载更新包...")

        def _work() -> Dict[str, Any]:
            updates_dir = PROJECT_ROOT / "updates"
            updates_dir.mkdir(parents=True, exist_ok=True)
            version_tag = remote_v or str(int(time.time()))
            zip_path = updates_dir / f"wcclaw-{version_tag}.zip"
            stage_dir = updates_dir / f"stage-{version_tag}"
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
            with requests.get(package_url, stream=True, timeout=30) as resp:
                if resp.status_code >= 400:
                    return {"ok": False, "error": f"下载失败 HTTP {resp.status_code}", "data": None}
                with open(zip_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
            expected_sha = str(meta.get("package_sha256", "") or meta.get("sha256", "")).strip().lower()
            if expected_sha:
                h = hashlib.sha256()
                with open(zip_path, "rb") as f:
                    for b in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(b)
                got = h.hexdigest().lower()
                if got != expected_sha:
                    return {"ok": False, "error": "更新包校验失败（sha256 不匹配）", "data": None}
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(stage_dir)
            main_exe = stage_dir / "WcClawDesktopPreview.exe"
            if not main_exe.exists():
                candidates = list(stage_dir.rglob("WcClawDesktopPreview.exe"))
                if not candidates:
                    return {"ok": False, "error": "更新包内未找到 WcClawDesktopPreview.exe", "data": None}
                stage_dir = candidates[0].parent
                main_exe = candidates[0]
            updater_bat = updates_dir / "apply_update.bat"
            bat = (
                "@echo off\r\n"
                "setlocal\r\n"
                f"set \"SRC={str(stage_dir)}\"\r\n"
                f"set \"DST={str(PROJECT_ROOT)}\"\r\n"
                "for /L %%i in (1,1,20) do (\r\n"
                "  ping 127.0.0.1 -n 2 >nul\r\n"
                ")\r\n"
                "xcopy \"%SRC%\\*\" \"%DST%\\\" /E /Y /I /Q >nul\r\n"
                "start \"\" \"%DST%\\WcClawDesktopPreview.exe\"\r\n"
                "exit /b 0\r\n"
            )
            updater_bat.write_text(bat, encoding="utf-8")
            return {"ok": True, "data": {"updater_bat": str(updater_bat)}, "error": None}

        self._submit_func_one_shot("download_update", _work, on_done=self._after_update_ready)

    def _after_update_ready(self, result: Dict[str, Any]) -> None:
        if hasattr(self, "update_install_btn"):
            self.update_install_btn.setEnabled(True)
        if not result.get("ok"):
            msg = f"更新失败：{result.get('error', '未知错误')}"
            if hasattr(self, "update_status_label"):
                self.update_status_label.setText(msg)
            self.show_error(msg)
            return
        updater_bat = str(result.get("data", {}).get("updater_bat", "")).strip()
        if not updater_bat:
            self.show_error("更新器启动失败：缺少 updater.bat")
            return
        if hasattr(self, "update_status_label"):
            self.update_status_label.setText("更新包已就绪，正在重启安装...")
        subprocess.Popen(
            ["cmd", "/c", updater_bat],
            cwd=str(PROJECT_ROOT),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        )
        QApplication.instance().quit()

    def refresh_all(self) -> None:
        self.request_task_and_runs()
        self.request_model_status()
        self.request_im_status()
        self.request_logs()
        self.request_skills()


def main() -> None:
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei", 10))
    icon_path = resource_path("assets", "wcclaw_claw.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    w = WcClawDesktop()
    if icon_path.exists():
        w.setWindowIcon(QIcon(str(icon_path)))
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()

