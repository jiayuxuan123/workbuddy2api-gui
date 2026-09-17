"""wb_gui.py —— WorkBuddy2API 图形界面（PySide6 / Qt6）。

把原先散在 .bat 与网页看板里的操作集中到一个窗口：启停服务、添加/管理账号、
查用量、看日志、改设置、开关机自启。双击 EXE 即可使用。

为什么用 Qt 而不是 tkinter：

* **跨线程结果投递是安全的**。tkinter 禁止在工作线程调用 ``widget.after()``
  （抛 ``RuntimeError: main thread is not in main loop``），后果是网络结果
  永远回不到界面，而异常死在后台线程里无从察觉。Qt 的信号/槽自动使用队列
  连接，接收端槽函数始终在接收者所属线程执行，这类 bug 结构上不会出现。
* **原生观感与高分屏**。Qt6 自带 per-monitor DPI 处理，不需要手动声明。
* **控件齐全**。表格、托盘、富文本日志、对话框都是现成的。

线程模型：界面永远在主线程；网络与磁盘操作交给 :meth:`MainWindow.run_async`
（``QThreadPool`` + 信号），结果以信号回到主线程再刷新控件。

入口参数：``--port``、``--minimized``、``--start``。
"""

import argparse
import os
import sys
import time
import traceback

from PySide6.QtCore import (
    QObject, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal, Slot,
)
from PySide6.QtGui import (
    QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QScrollArea,
    QSizePolicy, QSpinBox, QStatusBar, QSystemTrayIcon, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

import wb_autostart
import wb_gateway
import wb_proxy
import wb_runtime
import wb_settings
import wb_ui_theme as theme

REALM_LABELS = {"intl": "国际版", "cn": "国内版"}

#: Poll cadences (ms). Status only reads a cached snapshot, so it can be
#: frequent; accounts and usage touch the disk and run slower.
STATUS_INTERVAL_MS = 1000
ACCOUNTS_INTERVAL_MS = 5000
USAGE_INTERVAL_MS = 5000
LOGS_INTERVAL_MS = 1500


def realm_label(realm):
    return REALM_LABELS.get(realm, realm or "?")


# ---------------------------------------------------------------------------
# Async plumbing
# ---------------------------------------------------------------------------
class _WorkerSignals(QObject):
    """Signals for :class:`_Worker`.

    A ``QRunnable`` is not a ``QObject`` and cannot own signals, so they live
    here. Emitting from a worker thread and connecting to a main-thread slot
    yields a queued connection automatically, which delivers the call on the
    UI thread - exactly what widget updates require.
    """
    succeeded = Signal(object)
    failed = Signal(str)


class _Worker(QRunnable):
    """Runs ``fn`` on a pool thread and reports the outcome via signals.

    ``setAutoDelete(True)`` lets the pool destroy this runnable as soon as
    ``run()`` returns, which would take the signals object with it: a result
    emitted at the very end could be discarded before the UI thread ever
    processes it. The signals therefore live in a separate QObject that the
    caller keeps a reference to for the lifetime of the request.
    """

    def __init__(self, fn, signals=None):
        super().__init__()
        self.fn = fn
        # Passed in rather than owned, so its lifetime is not tied to this
        # runnable's.
        self.signals = signals or _WorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self):
        try:
            result = self.fn()
        except Exception as exc:
            detail = "%s: %s" % (type(exc).__name__, exc)
            wb_proxy.log("GUI worker failed: %s" % detail)
            wb_proxy.log(traceback.format_exc())
            try:
                self.signals.failed.emit(detail)
            except RuntimeError:
                pass          # receiver already destroyed
        else:
            try:
                self.signals.succeeded.emit(result)
            except RuntimeError:
                pass


def make_icon(size=64, color=None):
    """Draw the application icon at runtime.

    Generating it avoids shipping a binary asset and keeps the bundle small:
    a rounded square with a "W" that reads clearly in the taskbar and tray.
    """
    color = color or theme.ACCENT
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(1, 1, size - 2, size - 2, size * 0.22, size * 0.22)

    painter.setPen(QColor(theme.BG_ALT))
    font = QFont()
    font.setPointSizeF(size * 0.5)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "W")
    painter.end()
    return QIcon(pixmap)


# ---------------------------------------------------------------------------
# Reusable pieces
# ---------------------------------------------------------------------------
class StatCard(QFrame):
    """A titled number card for the overview row."""

    def __init__(self, title, tone=None, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setMinimumHeight(86)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 12, 15, 12)
        layout.setSpacing(3)

        self.title = QLabel(title)
        self.title.setObjectName("CardTitle")
        layout.addWidget(self.title)

        self.value = QLabel("—")
        self.value.setObjectName("CardValue")
        if tone:
            self.value.setProperty("tone", tone)
        layout.addWidget(self.value)

        self.sub = QLabel("")
        self.sub.setObjectName("CardSub")
        layout.addWidget(self.sub)

    def set_value(self, text, sub=None):
        self.value.setText(str(text))
        if sub is not None:
            self.sub.setText(str(sub))


class Panel(QFrame):
    """A titled container with a vertical body layout."""

    def __init__(self, title=None, parent=None):
        super().__init__(parent)
        self.setObjectName("Panel")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(15, 14, 15, 14)
        self.body.setSpacing(10)
        if title:
            label = QLabel(title)
            label.setObjectName("PanelTitle")
            self.body.addWidget(label)


def hint_label(text="", warn=False):
    label = QLabel(text)
    label.setObjectName("HintWarn" if warn else "Hint")
    label.setWordWrap(True)
    return label


class ReadonlyField(QWidget):
    """A labelled read-only value with a copy button."""

    def __init__(self, label, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)

        self.label = QLabel(label)
        self.label.setFixedWidth(150)
        self.field = QLineEdit()
        self.field.setReadOnly(True)
        self.field.setFont(QFont("Cascadia Mono", 9))
        self.copy_button = QPushButton("复制")
        self.copy_button.setProperty("variant", "secondary")
        self.copy_button.setFixedWidth(62)
        self.copy_button.clicked.connect(self._copy)

        layout.addWidget(self.label)
        layout.addWidget(self.field, 1)
        layout.addWidget(self.copy_button)

    def set_text(self, text):
        if self.field.text() != (text or ""):
            self.field.setText(text or "")

    def text(self):
        return self.field.text()

    def _copy(self):
        QApplication.clipboard().setText(self.field.text())


# ---------------------------------------------------------------------------
# OAuth login dialog
# ---------------------------------------------------------------------------
class LoginDialog(QDialog):
    """Browser-based OAuth for one realm, mirroring the web panel's flow.

    Three visible steps: pick the realm, open the returned link, then wait
    while the dialog polls until the account lands in the pool. All network
    work goes through the owner's async runner, so the window stays live.
    """

    POLL_INTERVAL_MS = 2500

    def __init__(self, owner, pool, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.pool = pool
        self.state = None
        self.auth_url = ""

        self.setWindowTitle("添加 WorkBuddy 账号")
        self.setModal(False)
        self.setMinimumWidth(540)

        self._build()
        QTimer.singleShot(150, self.start)

    # ------------------------------------------------------------------ build
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title = QLabel("添加 WorkBuddy 账号 (OAuth 授权)")
        title.setObjectName("PanelTitle")
        layout.addWidget(title)

        realm_box = QGroupBox("选择要登录的版本")
        realm_layout = QVBoxLayout(realm_box)
        self.radio_cn = QRadioButton("国内版 (copilot.tencent.com)")
        self.radio_intl = QRadioButton("国际版 (www.workbuddy.ai)")
        self.radio_cn.setChecked(True)
        self.radio_cn.toggled.connect(self._on_realm_change)
        realm_layout.addWidget(self.radio_cn)
        realm_layout.addWidget(self.radio_intl)
        layout.addWidget(realm_box)

        self.step1 = self._step_row(layout, "1", "正在向上游申请授权…")
        self.step2 = self._step_row(layout, "2", "在浏览器里打开下方链接并完成登录授权")
        self.step3 = self._step_row(layout, "3", "等待授权回调完成（自动检测）")

        self.link = QTextEdit()
        self.link.setReadOnly(True)
        self.link.setFixedHeight(60)
        self.link.setFont(QFont("Cascadia Mono", 8))
        layout.addWidget(self.link)

        buttons = QHBoxLayout()
        self.open_button = QPushButton("在浏览器中打开")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_link)
        self.copy_button = QPushButton("复制链接")
        self.copy_button.setProperty("variant", "secondary")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self._copy_link)
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.status = hint_label("")
        layout.addWidget(self.status)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setProperty("variant", "secondary")
        self.cancel_button.clicked.connect(self.close)
        footer.addWidget(self.cancel_button)
        layout.addLayout(footer)

    def _step_row(self, parent_layout, number, text):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        badge = QLabel(number)
        badge.setFixedSize(22, 22)
        badge.setAlignment(Qt.AlignCenter)
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(badge)
        layout.addWidget(label, 1)
        parent_layout.addWidget(row)
        return {"badge": badge, "label": label, "ordinal": number}

    @staticmethod
    def _mark(step, state):
        """state: idle | active | done"""
        colors = {"active": theme.ACCENT, "done": theme.SUCCESS,
                  "idle": theme.FG_MUTED}
        marks = {"active": "▶", "done": "✓"}
        color = colors.get(state, theme.FG_MUTED)
        text = marks.get(state, step.get("ordinal", "•"))
        step["badge"].setText(text)
        step["badge"].setStyleSheet(
            "border: 1px solid %s; border-radius: 11px; color: %s;"
            % (color, color))

    # ---------------------------------------------------------------- actions
    def _realm(self):
        return "intl" if self.radio_intl.isChecked() else "cn"

    def _on_realm_change(self, _checked=False):
        # Only restart once the dialog is actually on screen; toggling during
        # construction would fire a second login request.
        if self.isVisible():
            self.start()

    @Slot()
    def start(self):
        realm = self._realm()
        config = self.owner.realm_config(realm)
        self.step1["label"].setText("正在向 %s (%s) 申请授权…"
                                    % (config["domain"], config["name"]))
        self.state = None
        self.auth_url = ""
        self.link.setPlainText("")
        self.open_button.setEnabled(False)
        self.copy_button.setEnabled(False)
        self.status.setText("")
        self._mark(self.step1, "active")
        self._mark(self.step2, "idle")
        self._mark(self.step3, "idle")

        self.owner.run_async(
            lambda: self.pool.start_login(realm=realm, platform="CLI"),
            self._on_started, self._on_failed)

    def _on_started(self, result):
        if not self.isVisible():
            return
        self.state = (result or {}).get("state")
        self.auth_url = (result or {}).get("authUrl") or ""
        self._mark(self.step1, "done")
        self._mark(self.step2, "active")
        self.link.setPlainText(self.auth_url)
        self.open_button.setEnabled(bool(self.auth_url))
        self.copy_button.setEnabled(bool(self.auth_url))
        self.status.setText("点击上方链接在浏览器里完成登录授权。"
                            "登录完成后本窗口会自动检测并完成入池。")
        self._poll()

    def _on_failed(self, message):
        if not self.isVisible():
            return
        self._mark(self.step1, "idle")
        self.status.setText("申请授权失败：%s" % message)

    def _poll(self):
        if not self.isVisible() or not self.state:
            return
        self._mark(self.step3, "active")
        state = self.state
        self.owner.run_async(lambda: self.pool.poll_login(state),
                             self._on_polled, self._on_poll_error)

    def _on_polled(self, result):
        if not self.isVisible():
            return
        result = result or {}
        status = result.get("status")
        message = result.get("message") or ""

        if status == "ok":
            account = result.get("account") or {}
            self._mark(self.step3, "done")
            self.status.setText("登录成功：%s 已加入账号池"
                                % (account.get("nickname") or "账号"))
            self.cancel_button.setText("关闭")
            if self.owner:
                self.owner.on_account_added(account)
            return

        if status in ("error", "expired", "unknown"):
            self._mark(self.step3, "idle")
            self.status.setText(message or ("登录状态：%s" % status))
            return

        # Still pending: upstream answers 11217 until the browser finishes.
        self.status.setText(message or "等待浏览器完成登录…")
        QTimer.singleShot(self.POLL_INTERVAL_MS, self._poll)

    def _on_poll_error(self, message):
        if not self.isVisible():
            return
        self.status.setText("轮询出错，继续重试：%s" % message)
        QTimer.singleShot(self.POLL_INTERVAL_MS, self._poll)

    @Slot()
    def _open_link(self):
        if self.auth_url and not QDesktopServices.openUrl(QUrl(self.auth_url)):
            self.status.setText("无法自动打开浏览器，请复制链接手动打开。")

    @Slot()
    def _copy_link(self):
        if self.auth_url:
            QApplication.clipboard().setText(self.auth_url)
            self.status.setText("链接已复制到剪贴板")

    def closeEvent(self, event):
        """Tell the upstream the window was abandoned so its state does not linger."""
        if self.state:
            state, self.state = self.state, None
            try:
                self.owner.run_async(lambda: self.pool.cancel_login(state))
            except Exception:
                pass
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    """The application window."""

    # ---------------------------------------------------------- dialog helpers
    #
    # Every confirmation and notice goes through these methods instead of
    # calling QMessageBox directly. Two reasons:
    #
    #   * they are the only modal entry points, so a headless test can override
    #     them and exercise the logic underneath without a dialog blocking
    #     forever. QMessageBox is a Qt class, so patching it from Python has no
    #     effect on the code being tested;
    #   * the parent window and the button wording stay consistent.
    def ask(self, title, text):
        """Ask a yes/no question. Returns True when the user chose Yes."""
        return QMessageBox.question(self, title, text) == QMessageBox.Yes

    def info(self, title, text):
        """Show an informational notice."""
        QMessageBox.information(self, title, text)

    def warn(self, title, text):
        """Show a warning."""
        QMessageBox.warning(self, title, text)

    def fail(self, title, text):
        """Report an error."""
        QMessageBox.critical(self, title, text)

    def ask_text(self, title, prompt, default=""):
        """Prompt for one line of text. Returns (text, accepted)."""
        return QInputDialog.getText(self, title, prompt, text=default)


    def __init__(self, args):
        super().__init__()
        self.args = args
        self.gateway = wb_gateway.Gateway()
        self.prefs = dict(self.gateway.prefs)
        self._log_tail = 0
        self._pool = QThreadPool.globalInstance()
        #: Keeps signal holders alive while a request is in flight; see
        #: :meth:`run_async`.
        self._pending = {}
        self._tray = None
        self._quitting = False

        self.setWindowTitle("WorkBuddy2API 网关")
        self.setMinimumSize(960, 650)
        self.resize(1100, 740)
        self.setWindowIcon(make_icon())

        self._build_ui()
        self._load_prefs_into_ui()
        self._setup_tray()
        self._start_timers()

        if args.start or self.prefs.get("autostart_start"):
            QTimer.singleShot(250, lambda: self.do_start(silent=True))
        if args.minimized:
            QTimer.singleShot(400, self._minimize_to_tray)

    # ------------------------------------------------------------------ build
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(14, 12, 14, 6)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        holder_layout.addWidget(self.tabs)
        root.addWidget(holder, 1)

        self.tabs.addTab(self._build_overview(), "概览")
        self.tabs.addTab(self._build_accounts(), "账号")
        self.tabs.addTab(self._build_usage(), "用量")
        self.tabs.addTab(self._build_tasks(), "任务")
        self.tabs.addTab(self._build_logs(), "日志")
        self.tabs.addTab(self._build_settings(), "设置")

        self.setStatusBar(QStatusBar())
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("Hint")
        self.statusBar().addWidget(self.status_label)
        self.path_label = QLabel(wb_runtime.data_dir())
        self.path_label.setObjectName("Hint")
        self.statusBar().addPermanentWidget(self.path_label)

    def _build_header(self):
        bar = QFrame()
        bar.setObjectName("HeaderBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 12, 16, 12)

        left = QVBoxLayout()
        left.setSpacing(3)
        self.state_label = QLabel("○ 已停止")
        self.state_label.setObjectName("StateText")
        self.state_label.setProperty("state", "stopped")
        self.detail_label = QLabel("未在监听")
        self.detail_label.setObjectName("DetailText")
        left.addWidget(self.state_label)
        left.addWidget(self.detail_label)
        layout.addLayout(left, 1)

        self.start_button = QPushButton("启动服务")
        self.start_button.clicked.connect(self.do_start)
        self.stop_button = QPushButton("停止服务")
        self.stop_button.setProperty("variant", "secondary")
        self.stop_button.clicked.connect(self.do_stop)
        self.restart_button = QPushButton("重启")
        self.restart_button.setProperty("variant", "secondary")
        self.restart_button.clicked.connect(self.do_restart)
        self.panel_button = QPushButton("网页面板")
        self.panel_button.setProperty("variant", "secondary")
        self.panel_button.clicked.connect(self.open_panel)
        for button in (self.start_button, self.stop_button,
                       self.restart_button, self.panel_button):
            layout.addWidget(button)
        return bar

    # --------------------------------------------------------------- overview
    def _build_overview(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 10, 4, 4)
        layout.setSpacing(14)

        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.cards = {
            "state": StatCard("运行状态", tone="success"),
            "accounts": StatCard("账号"),
            "requests": StatCard("请求数"),
            "tokens": StatCard("总 Token", tone="accent"),
            "errors": StatCard("失败", tone="danger"),
        }
        for card in self.cards.values():
            cards.addWidget(card)
        layout.addLayout(cards)

        access = Panel("接入信息")
        self.access = {}
        for key, label in (("url", "接口地址 (Base URL)"),
                           ("key", "API Key"),
                           ("realm", "当前区域"),
                           ("scheduler", "定时任务")):
            field = ReadonlyField(label)
            access.body.addWidget(field)
            self.access[key] = field
        key_row = QHBoxLayout()
        self.regen_key_button = QPushButton("重新生成 Key")
        self.regen_key_button.setProperty("variant", "secondary")
        self.regen_key_button.clicked.connect(self.do_regen_key)
        key_row.addWidget(self.regen_key_button)
        key_row.addStretch(1)
        access.body.addLayout(key_row)
        layout.addWidget(access)

        layout.addWidget(hint_label(
            "把接口地址与 API Key 填进任意 OpenAI 兼容客户端即可使用；"
            "同时支持 Chat Completions 与 Responses API 两种协议。"))
        layout.addStretch(1)
        return page

    # --------------------------------------------------------------- accounts
    def _build_accounts(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 10, 4, 4)
        layout.setSpacing(12)

        toolbar = QHBoxLayout()
        self.add_account_button = QPushButton("添加账号 (OAuth 授权)")
        self.add_account_button.clicked.connect(self.do_add_account)
        self.scan_button = QPushButton("扫描桌面应用凭证")
        self.scan_button.setProperty("variant", "secondary")
        self.scan_button.clicked.connect(self.do_scan_desktop)
        self.credits_button = QPushButton("查询额度")
        self.credits_button.setProperty("variant", "secondary")
        self.credits_button.clicked.connect(self.do_fetch_credits)
        self.refresh_tokens_button = QPushButton("刷新令牌")
        self.refresh_tokens_button.setProperty("variant", "secondary")
        self.refresh_tokens_button.clicked.connect(self.do_refresh_tokens)
        for button in (self.add_account_button, self.scan_button,
                       self.credits_button, self.refresh_tokens_button):
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        toolbar.addWidget(QLabel("显示区域："))
        self.realm_filter = QComboBox()
        self.realm_filter.addItem("全部", "all")
        self.realm_filter.addItem("国际版", "intl")
        self.realm_filter.addItem("国内版", "cn")
        self.realm_filter.currentIndexChanged.connect(self.refresh_accounts)
        toolbar.addWidget(self.realm_filter)
        layout.addLayout(toolbar)

        # Transfer actions live on their own row: they move the whole pool,
        # not one account, so they should not sit among the per-account tools.
        transfer = QHBoxLayout()
        self.export_accounts_button = QPushButton("导出账号")
        self.export_accounts_button.setProperty("variant", "secondary")
        self.export_accounts_button.clicked.connect(self.do_export_accounts)
        self.import_accounts_button = QPushButton("导入账号")
        self.import_accounts_button.setProperty("variant", "secondary")
        self.import_accounts_button.clicked.connect(self.do_import_accounts)
        transfer.addWidget(self.export_accounts_button)
        transfer.addWidget(self.import_accounts_button)
        transfer.addWidget(hint_label(
            "用于备份或换机迁移；导出文件含明文凭证，注意保管。"))
        transfer.addStretch(1)
        layout.addLayout(transfer)

        self.account_table = QTableWidget(0, 7)
        self.account_table.setHorizontalHeaderLabels(
            ["账号", "区域", "状态", "有效期", "额度", "请求", "Token"])
        self.account_table.verticalHeader().setVisible(False)
        self.account_table.setAlternatingRowColors(True)
        self.account_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.account_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.account_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.account_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 7):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        layout.addWidget(self.account_table, 1)

        actions = QHBoxLayout()
        self.toggle_button = QPushButton("启用 / 停用")
        self.toggle_button.setProperty("variant", "secondary")
        self.toggle_button.clicked.connect(self.do_toggle_account)
        self.delete_button = QPushButton("删除")
        self.delete_button.setProperty("variant", "danger")
        self.delete_button.clicked.connect(self.do_delete_account)
        enable_all = QPushButton("全部启用")
        enable_all.setProperty("variant", "secondary")
        enable_all.clicked.connect(lambda: self.do_set_all(True))
        disable_all = QPushButton("全部停用")
        disable_all.setProperty("variant", "secondary")
        disable_all.clicked.connect(lambda: self.do_set_all(False))
        self.refresh_accounts_button = QPushButton("刷新列表")
        self.refresh_accounts_button.setProperty("variant", "secondary")
        self.refresh_accounts_button.clicked.connect(self.refresh_accounts)
        for button in (self.toggle_button, self.delete_button,
                       enable_all, disable_all):
            actions.addWidget(button)
        actions.addStretch(1)
        actions.addWidget(self.refresh_accounts_button)
        layout.addLayout(actions)

        self.account_hint = hint_label("")
        layout.addWidget(self.account_hint)
        return page

    # ------------------------------------------------------------------ usage
    def _build_usage(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 10, 4, 4)
        layout.setSpacing(14)

        summary = Panel("汇总（当前区域）")
        grid = QGridLayout()
        grid.setSpacing(20)
        self.usage_values = {}
        fields = [("requests", "请求数"), ("prompt_tokens", "输入"),
                  ("completion_tokens", "输出"), ("reasoning_tokens", "思考"),
                  ("cached_tokens", "缓存命中"), ("total_tokens", "总 Token")]
        for index, (key, label) in enumerate(fields):
            title = QLabel(label)
            title.setObjectName("CardTitle")
            value = QLabel("—")
            value.setObjectName("CardValue")
            value.setStyleSheet("font-size: 13pt;")
            if key == "reasoning_tokens":
                value.setProperty("tone", "think")
            elif key == "total_tokens":
                value.setProperty("tone", "accent")
            grid.addWidget(title, 0, index)
            grid.addWidget(value, 1, index)
            self.usage_values[key] = value
        summary.body.addLayout(grid)
        layout.addWidget(summary)

        models = Panel("按模型")
        self.model_table = QTableWidget(0, 6)
        self.model_table.setHorizontalHeaderLabels(
            ["模型", "请求", "输入", "输出", "思考", "总 Token"])
        self.model_table.verticalHeader().setVisible(False)
        self.model_table.setAlternatingRowColors(True)
        self.model_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.model_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        mheader = self.model_table.horizontalHeader()
        mheader.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 6):
            mheader.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        models.body.addWidget(self.model_table)
        layout.addWidget(models, 1)

        recent = Panel("最近请求")
        self.recent_table = QTableWidget(0, 8)
        self.recent_table.setHorizontalHeaderLabels(
            ["时间", "模型", "账号", "模式", "耗时", "首字", "速度", "总 Token"])
        self.recent_table.verticalHeader().setVisible(False)
        self.recent_table.setAlternatingRowColors(True)
        self.recent_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.recent_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        rheader = self.recent_table.horizontalHeader()
        rheader.setSectionResizeMode(1, QHeaderView.Stretch)
        for column in (0, 2, 3, 4, 5, 6, 7):
            rheader.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        recent.body.addWidget(self.recent_table)
        layout.addWidget(recent, 2)

        self.usage_hint = hint_label("")
        layout.addWidget(self.usage_hint)
        return page

    # ------------------------------------------------------------------- logs
    # ------------------------------------------------------------------ tasks
    def _build_tasks(self):
        """Scheduler panel: what runs, when, and what it did last time.

        The daily check-in / growth tasks are a domestic-realm feature; for an
        international account the scheduler only keeps tokens alive. The panel
        says so rather than showing an empty list.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 10, 4, 4)
        layout.setSpacing(12)

        controls = Panel("定时任务")
        self.task_state = QLabel("调度器未启动（服务未运行）")
        self.task_state.setWordWrap(True)
        controls.body.addWidget(self.task_state)
        self.task_mode = hint_label("")
        controls.body.addWidget(self.task_mode)

        buttons = QHBoxLayout()
        self.scheduler_toggle_button = QPushButton("启用 / 暂停")
        self.scheduler_toggle_button.setProperty("variant", "secondary")
        self.scheduler_toggle_button.clicked.connect(self.do_toggle_scheduler)
        self.run_tasks_button = QPushButton("立即执行一次")
        self.run_tasks_button.clicked.connect(self.do_run_tasks_now)
        self.refresh_tasks_button = QPushButton("刷新")
        self.refresh_tasks_button.setProperty("variant", "secondary")
        self.refresh_tasks_button.clicked.connect(self.refresh_tasks)
        buttons.addWidget(self.scheduler_toggle_button)
        buttons.addWidget(self.run_tasks_button)
        buttons.addStretch(1)
        buttons.addWidget(self.refresh_tasks_button)
        controls.body.addLayout(buttons)
        controls.body.addWidget(hint_label(
            "国内版账号会按排程自动签到、领取积分与派出猫猫旅行；"
            "国际版账号只做令牌保活。全部操作走你自己的账号，"
            "程序已内置防风控间隔。"))
        layout.addWidget(controls)

        history = Panel("执行记录")
        self.task_log = QPlainTextEdit()
        self.task_log.setObjectName("LogView")
        self.task_log.setReadOnly(True)
        self.task_log.setMaximumBlockCount(3000)
        history.body.addWidget(self.task_log)
        layout.addWidget(history, 1)
        return page

    # ------------------------------------------------------------------- logs
    def _build_logs(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 10, 4, 4)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("级别："))
        self.log_level = QComboBox()
        self.log_level.addItems(["全部", "INFO", "WARN", "ERROR"])
        self.log_level.currentIndexChanged.connect(lambda _: self.refresh_logs(True))
        toolbar.addWidget(self.log_level)

        toolbar.addSpacing(10)
        toolbar.addWidget(QLabel("搜索："))
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText("关键字，回车过滤")
        self.log_search.setMaximumWidth(240)
        self.log_search.returnPressed.connect(lambda: self.refresh_logs(True))
        toolbar.addWidget(self.log_search)

        self.log_autoscroll = QCheckBox("自动滚动")
        self.log_autoscroll.setChecked(True)
        toolbar.addWidget(self.log_autoscroll)

        refresh = QPushButton("刷新")
        refresh.setProperty("variant", "secondary")
        refresh.clicked.connect(lambda: self.refresh_logs(True))
        clear = QPushButton("清空")
        clear.setProperty("variant", "secondary")
        clear.clicked.connect(self.do_clear_logs)
        export_logs = QPushButton("导出")
        export_logs.setProperty("variant", "secondary")
        export_logs.clicked.connect(self.do_export_logs)
        toolbar.addWidget(refresh)
        toolbar.addWidget(clear)
        toolbar.addWidget(export_logs)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("LogView")
        self.log_view.setReadOnly(True)
        # Bound memory on long-running sessions; the panel only shows recent
        # entries anyway.
        self.log_view.setMaximumBlockCount(6000)
        layout.addWidget(self.log_view, 1)
        return page

    # --------------------------------------------------------------- settings
    def _build_settings(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        page = QWidget()
        scroll.setWidget(page)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 10, 4, 4)
        layout.setSpacing(14)

        listen = Panel("监听")
        row = QHBoxLayout()
        row.addWidget(QLabel("端口"))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setFixedWidth(110)
        row.addWidget(self.port_spin)
        row.addStretch(1)
        listen.body.addLayout(row)
        self.lan_check = QCheckBox("允许局域网访问（0.0.0.0，自动生成 API Key）")
        self.lan_check.toggled.connect(self._on_lan_toggled)
        listen.body.addWidget(self.lan_check)
        listen.body.addWidget(hint_label(
            "仅本机访问时默认不校验 API Key；开放局域网后强制要求 Key。"))
        layout.addWidget(listen)

        # ---- API key management ----
        keys = Panel("API Key")
        self.require_local_key_check = QCheckBox(
            "本机也要求 API Key（默认关闭，本机客户端无需配置）")
        keys.body.addWidget(self.require_local_key_check)
        keys.body.addWidget(hint_label(
            "关闭时：本机请求不校验 Key，但客户端填了下面任何一个 Key 也照样能用 —— "
            "有些 Agent 客户端强制要求填写 Key 字段，填上即可。"))

        self.key_table = QTableWidget(0, 3)
        self.key_table.setHorizontalHeaderLabels(["名称", "Key", "状态"])
        self.key_table.verticalHeader().setVisible(False)
        self.key_table.setAlternatingRowColors(True)
        self.key_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.key_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.key_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.key_table.setMaximumHeight(150)
        kheader = self.key_table.horizontalHeader()
        kheader.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        kheader.setSectionResizeMode(1, QHeaderView.Stretch)
        kheader.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        keys.body.addWidget(self.key_table)

        key_actions = QHBoxLayout()
        self.new_key_button = QPushButton("新建 Key")
        self.new_key_button.clicked.connect(self.do_new_key)
        self.copy_key_button = QPushButton("复制选中")
        self.copy_key_button.setProperty("variant", "secondary")
        self.copy_key_button.clicked.connect(self.do_copy_key)
        self.toggle_key_button = QPushButton("启用 / 停用")
        self.toggle_key_button.setProperty("variant", "secondary")
        self.toggle_key_button.clicked.connect(self.do_toggle_key)
        self.delete_key_button = QPushButton("删除")
        self.delete_key_button.setProperty("variant", "danger")
        self.delete_key_button.clicked.connect(self.do_delete_key)
        self.regen_key_button = QPushButton("重新生成局域网 Key")
        self.regen_key_button.setProperty("variant", "secondary")
        self.regen_key_button.clicked.connect(self.do_regen_key)
        for button in (self.new_key_button, self.copy_key_button,
                       self.toggle_key_button, self.delete_key_button):
            key_actions.addWidget(button)
        key_actions.addStretch(1)
        key_actions.addWidget(self.regen_key_button)
        keys.body.addLayout(key_actions)

        self.key_hint = hint_label("")
        keys.body.addWidget(self.key_hint)
        layout.addWidget(keys)

        boot = Panel("启动")
        self.autostart_check = QCheckBox("开机自动启动（当前用户，无需管理员权限）")
        self.autostart_check.toggled.connect(self.do_toggle_autostart)
        boot.body.addWidget(self.autostart_check)
        self.autostart_status = hint_label("")
        boot.body.addWidget(self.autostart_status)
        self.start_min_check = QCheckBox("开机启动时最小化到托盘")
        boot.body.addWidget(self.start_min_check)
        self.auto_start_check = QCheckBox("程序启动后立即开始监听")
        boot.body.addWidget(self.auto_start_check)
        self.open_dash_check = QCheckBox("启动服务后自动打开网页面板")
        boot.body.addWidget(self.open_dash_check)
        layout.addWidget(boot)

        # ---- panel password ----
        panel = Panel("网页面板密码")
        panel.body.addWidget(hint_label(
            "网页面板是浏览器里的备用管理界面（比这个窗口功能更全）。"
            "默认密码是 admin。"))
        self.panel_pwd_status = hint_label("")
        panel.body.addWidget(self.panel_pwd_status)
        pwd_row = QHBoxLayout()
        self.new_pwd_edit = QLineEdit()
        self.new_pwd_edit.setEchoMode(QLineEdit.Password)
        self.new_pwd_edit.setPlaceholderText("新密码")
        self.new_pwd_confirm = QLineEdit()
        self.new_pwd_confirm.setEchoMode(QLineEdit.Password)
        self.new_pwd_confirm.setPlaceholderText("再输一次")
        self.set_pwd_button = QPushButton("修改密码")
        self.set_pwd_button.clicked.connect(self.do_set_panel_password)
        pwd_row.addWidget(self.new_pwd_edit, 1)
        pwd_row.addWidget(self.new_pwd_confirm, 1)
        pwd_row.addWidget(self.set_pwd_button)
        panel.body.addLayout(pwd_row)
        self.reset_pwd_button = QPushButton("重置为默认密码 admin")
        self.reset_pwd_button.setProperty("variant", "secondary")
        self.reset_pwd_button.clicked.connect(self.do_reset_panel_password)
        reset_row = QHBoxLayout()
        reset_row.addWidget(self.reset_pwd_button)
        reset_row.addStretch(1)
        panel.body.addLayout(reset_row)
        panel.body.addWidget(hint_label(
            "忘记密码时：本窗口可以随时重置（不需要旧密码），"
            "因为能打开这个窗口就说明你能访问本机数据目录。"))
        layout.addWidget(panel)

        advanced = Panel("高级")
        advanced.body.addWidget(QLabel("默认 system 提示词（客户端未提供时补上）"))
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setFixedHeight(74)
        advanced.body.addWidget(self.prompt_edit)
        advanced.body.addWidget(QLabel("上游 User-Agent（留空为自动镜像官方客户端）"))
        self.ua_edit = QLineEdit()
        advanced.body.addWidget(self.ua_edit)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("数据目录："))
        path_value = QLabel(wb_runtime.data_dir())
        path_value.setObjectName("Hint")
        path_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        path_row.addWidget(path_value, 1)
        open_dir = QPushButton("打开")
        open_dir.setProperty("variant", "secondary")
        open_dir.setFixedWidth(66)
        open_dir.clicked.connect(self.open_data_dir)
        path_row.addWidget(open_dir)
        advanced.body.addLayout(path_row)
        layout.addWidget(advanced)

        actions = QHBoxLayout()
        save = QPushButton("保存设置")
        save.clicked.connect(self.do_save_settings)
        reset = QPushButton("恢复默认")
        reset.setProperty("variant", "secondary")
        reset.clicked.connect(self.do_reset_settings)
        actions.addWidget(save)
        actions.addWidget(reset)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addStretch(1)
        return scroll

    # ------------------------------------------------------------------ setup
    def _setup_tray(self):
        """Tray icon, so minimize-to-tray has somewhere to come back from."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = None
            return
        self._tray = QSystemTrayIcon(make_icon(), self)
        self._tray.setToolTip("WorkBuddy2API 网关")

        menu = QMenu()
        show_action = QAction("显示主窗口", self)
        show_action.triggered.connect(self._restore_from_tray)
        self.tray_toggle_action = QAction("启动服务", self)
        self.tray_toggle_action.triggered.connect(self._tray_toggle_service)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self._tray_quit)
        menu.addAction(show_action)
        menu.addAction(self.tray_toggle_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _start_timers(self):
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.refresh_status)
        self.status_timer.start(STATUS_INTERVAL_MS)

        self.accounts_timer = QTimer(self)
        self.accounts_timer.timeout.connect(self.refresh_accounts)
        self.accounts_timer.start(ACCOUNTS_INTERVAL_MS)

        self.usage_timer = QTimer(self)
        self.usage_timer.timeout.connect(self.refresh_usage)
        self.usage_timer.start(USAGE_INTERVAL_MS)

        self.logs_timer = QTimer(self)
        self.logs_timer.timeout.connect(self.refresh_logs)
        self.logs_timer.start(LOGS_INTERVAL_MS)

        # Scheduler state changes slowly (hourly at most), so a slow poll is
        # enough; it is refreshed immediately after any manual action.
        self.tasks_timer = QTimer(self)
        self.tasks_timer.timeout.connect(self.refresh_tasks)
        self.tasks_timer.start(15000)

        self.refresh_status()
        self.refresh_accounts()
        self.refresh_usage()
        self.refresh_tasks()
        self.refresh_logs(True)

    # ----------------------------------------------------------------- helpers
    def run_async(self, fn, on_ok=None, on_error=None, name="task"):
        """Run ``fn`` on a pool thread; deliver results via queued signals.

        Qt marshals the signal to the receiver's thread, so ``on_ok`` always
        executes on the UI thread even though ``fn`` ran on a worker. This is
        the guarantee tkinter could not provide.

        The signal holder is kept in ``self._pending`` until the result has
        been handled: ``_Worker`` is auto-deleted by the pool when its
        ``run()`` returns, so a result emitted at the last moment would
        otherwise have nothing left to travel through.
        """
        signals = _WorkerSignals()
        worker = _Worker(fn, signals=signals)

        def finish(callback, payload):
            self._pending.pop(name, None)
            if callback:
                callback(payload)

        signals.succeeded.connect(lambda value: finish(on_ok, value))
        signals.failed.connect(lambda msg: finish(on_error or self._report_error, msg))
        self._pending[name] = signals
        self._pool.start(worker)

    def _report_error(self, message):
        self.set_status("操作失败：%s" % message)

    def set_status(self, text):
        self.status_label.setText(text)

    @staticmethod
    def realm_config(realm):
        import wb_accounts
        return wb_accounts.get_realm_config(realm)

    @Slot()
    def _restore_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._restore_from_tray()

    def _minimize_to_tray(self):
        self.hide()
        if self._tray:
            self._tray.showMessage(
                "WorkBuddy2API 仍在运行",
                "网关在后台继续服务。双击托盘图标可重新打开窗口。",
                QSystemTrayIcon.Information, 4000)

    def _tray_toggle_service(self):
        if self.gateway.is_running():
            self.do_stop()
        else:
            self.do_start()

    def _tray_quit(self):
        self._quitting = True
        if self.gateway.is_running():
            self.gateway.stop()
        if self._tray:
            self._tray.hide()
        QApplication.quit()

    # ----------------------------------------------------------------- actions
    def _collect_prefs(self):
        prefs = dict(self.gateway.prefs)
        prefs["port"] = int(self.port_spin.value())
        lan = self.lan_check.isChecked()
        prefs["lan"] = lan
        prefs["host"] = "0.0.0.0" if lan else "127.0.0.1"
        prefs["start_minimized"] = self.start_min_check.isChecked()
        prefs["autostart_start"] = self.auto_start_check.isChecked()
        prefs["open_dashboard_on_start"] = self.open_dash_check.isChecked()
        prefs["require_local_key"] = self.require_local_key_check.isChecked()
        prefs["user_agent"] = self.ua_edit.text().strip()
        prompt = self.prompt_edit.toPlainText().strip()
        prefs["system_prompt"] = prompt or wb_proxy.DEFAULT_SYSTEM_PROMPT
        return prefs

    def _load_prefs_into_ui(self):
        self.port_spin.setValue(int(self.prefs.get("port", 8788)))
        self.lan_check.setChecked(bool(self.prefs.get("lan")))
        self.require_local_key_check.setChecked(
            bool(self.prefs.get("require_local_key")))
        self.start_min_check.setChecked(bool(self.prefs.get("start_minimized")))
        self.auto_start_check.setChecked(bool(self.prefs.get("autostart_start")))
        self.open_dash_check.setChecked(
            bool(self.prefs.get("open_dashboard_on_start")))
        self.ua_edit.setText(self.prefs.get("user_agent") or "")
        self.prompt_edit.setPlainText(
            self.prefs.get("system_prompt") or wb_proxy.DEFAULT_SYSTEM_PROMPT)
        # Reflect reality, not the stored flag: the registry is the source of
        # truth for whether autostart is actually on.
        self.autostart_check.blockSignals(True)
        self.autostart_check.setChecked(wb_autostart.is_enabled())
        self.autostart_check.blockSignals(False)
        self._refresh_autostart_status()
        self.refresh_keys()
        self.refresh_panel_password_state()

    def _refresh_autostart_status(self):
        info = wb_autostart.status()
        if not info["supported"]:
            self.autostart_status.setText("当前系统不支持（仅 Windows）")
        elif not info["enabled"]:
            self.autostart_status.setText("未开启。勾选后写入 %s" % info["location"])
        elif info["stale"]:
            self.autostart_status.setText(
                "⚠ 启动项指向的程序已不存在：%s\n取消再重新勾选即可修复。"
                % info["target"])
        else:
            self.autostart_status.setText("已开启：%s" % info["command"])

    @Slot()
    def do_start(self, silent=False):
        self.set_status("正在启动…")
        prefs = self._collect_prefs()
        self.gateway.prefs.update(prefs)
        ok, message = self.gateway.start(prefs)
        self.set_status(message)
        self.refresh_status()
        if ok:
            try:
                wb_gateway.save_prefs(self.gateway.prefs)
            except Exception as exc:
                self.set_status("已启动，但设置保存失败：%s" % exc)
            if not silent and self.open_dash_check.isChecked():
                self.open_panel()
        elif not silent:
            self.fail("启动失败", message)

    @Slot()
    def do_stop(self):
        self.set_status("正在停止…")
        self.gateway.stop()
        self.set_status("已停止")
        self.refresh_status()

    @Slot()
    def do_restart(self):
        self.set_status("正在重启…")
        self.gateway.prefs.update(self._collect_prefs())
        ok, message = self.gateway.restart()
        self.set_status(message)
        self.refresh_status()

    @Slot()
    def open_panel(self):
        url = self.gateway.base_url()
        if not QDesktopServices.openUrl(QUrl(url)):
            self.warn("无法打开浏览器", "请手动访问：%s" % url)

    @Slot()
    def open_data_dir(self):
        path = wb_runtime.data_dir()
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            self.warn("无法打开目录", path)

    @Slot()
    def do_regen_key(self):
        """Mint a fresh LAN key (only meaningful when listening on all interfaces)."""
        if not self.lan_check.isChecked():
            self.info(
                self, "仅本机模式",
                "局域网 Key 只在开启「允许局域网访问」时使用。\n\n"
                "本机模式若需要 Key（例如某个客户端强制要求填写），"
                "请用上面的「新建 Key」。")
            return
        if not self.ask(
                "重新生成 Key",
                "重新生成后，所有客户端都要更换新 Key。\n\n确定继续？"):
            return
        import wb_settings
        try:
            saved = wb_settings.load(wb_proxy.ACCOUNTS_DIR)
            saved["launcher_key"] = ""
            wb_settings.save(wb_proxy.ACCOUNTS_DIR, saved)
            key, _ = wb_settings.ensure_launcher_key(wb_proxy.ACCOUNTS_DIR)
            wb_proxy.API_KEY = key
            self.set_status("已生成新的 API Key")
            self.info(
                self, "新 Key 已生成",
                "新 Key：\n\n%s\n\n重启服务后生效，请同步更新客户端配置。" % key)
            self.refresh_status()
        except Exception as exc:
            self.fail("生成失败", str(exc))

    # ------------------------------------------------------------- key manager
    def _on_lan_toggled(self, _checked):
        """Keep the key controls consistent with the listening mode."""
        self.refresh_keys()
        self.refresh_status()

    def _mask_key(self, key):
        """Show enough of a key to recognise it without exposing all of it."""
        key = key or ""
        if len(key) <= 10:
            return key
        return key[:6] + "…" + key[-4:]

    @Slot()
    def refresh_keys(self):
        """Render the configured keys plus the generated LAN key."""
        table = self.key_table
        table.setRowCount(0)
        try:
            entries = list(wb_proxy.configured_keys())
        except Exception:
            entries = []

        rows = []
        for entry in entries:
            rows.append((entry.get("id") or "", entry.get("name") or "未命名",
                         entry.get("key") or "", bool(entry.get("enabled", True)),
                         False))
        # The launcher key is generated by LAN mode and is not part of the
        # panel list, so surface it here too - otherwise the key printed at
        # startup has no visible home in the UI.
        try:
            launcher = wb_proxy.API_KEY or ""
        except Exception:
            launcher = ""
        if launcher and not any(r[2] == launcher for r in rows):
            rows.append(("__launcher__", "局域网自动生成", launcher, True, True))

        table.setRowCount(len(rows))
        for index, (kid, name, key, enabled, is_launcher) in enumerate(rows):
            cells = [name, self._mask_key(key),
                     "启用" if enabled else "已停用"]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(Qt.UserRole, kid)
                    item.setData(Qt.UserRole + 1, key)
                    item.setData(Qt.UserRole + 2, is_launcher)
                if not enabled:
                    item.setForeground(QColor(theme.FG_MUTED))
                elif column == 2:
                    item.setForeground(QColor(theme.SUCCESS))
                if column == 1:
                    item.setToolTip("点「复制选中」可复制完整 Key")
                table.setItem(index, column, item)

        lan = self.lan_check.isChecked()
        if lan:
            self.key_hint.setText(
                "当前是局域网模式：所有客户端必须携带 Key。"
                "「局域网自动生成」那个就是启动时提示的 Key，可复制给客户端。")
        elif self.require_local_key_check.isChecked():
            self.key_hint.setText(
                "已开启「本机也要求 Key」：本机客户端必须携带下面任一启用的 Key。")
        elif rows:
            self.key_hint.setText(
                "本机模式默认不校验 Key。下面这些 Key 仍然有效 —— "
                "客户端若强制要求填 Key，把任意一个填进去即可。")
        else:
            self.key_hint.setText(
                "本机模式不需要 Key。若某个客户端强制要求填写，"
                "点「新建 Key」生成一个填进去即可。")
        self.regen_key_button.setEnabled(lan)

    def _selected_key(self):
        row = self.key_table.currentRow()
        if row < 0:
            self.info("未选择", "请先在列表里选中一个 Key。")
            return None
        item = self.key_table.item(row, 0)
        if item is None:
            return None
        return {
            "id": item.data(Qt.UserRole),
            "key": item.data(Qt.UserRole + 1),
            "launcher": bool(item.data(Qt.UserRole + 2)),
        }

    @Slot()
    def do_new_key(self):
        """Create a key so a client that demands one has something to carry."""
        name, ok = self.ask_text(
            "新建 Key", "给这个 Key 起个名字（例如客户端名称）：",
            default="客户端")
        if not ok:
            return
        import secrets
        key = "wb-" + secrets.token_urlsafe(24)
        try:
            entries = list(wb_proxy.configured_keys())
            entries.append({
                "id": secrets.token_hex(6),
                "name": (name or "").strip() or "未命名",
                "key": key, "realm": "", "enabled": True,
            })
            wb_settings.set_api_keys(wb_proxy.ACCOUNTS_DIR, entries)
        except Exception as exc:
            self.fail("创建失败", str(exc))
            return
        self.refresh_keys()
        self.refresh_status()
        self.set_status("已新建 Key")
        self.show_new_key(key)

    def show_new_key(self, key):
        """Show a freshly created key with a one-click copy.

        Split out from :meth:`do_new_key` so tests can drive key creation
        without a modal dialog blocking them.
        """
        box = QMessageBox(self)
        box.setWindowTitle("新 Key 已创建")
        box.setText("把这个 Key 填进客户端：")
        box.setInformativeText(key)
        box.setStandardButtons(QMessageBox.Ok)
        copy_btn = box.addButton("复制", QMessageBox.ActionRole)
        box.exec()
        if box.clickedButton() is copy_btn:
            QApplication.clipboard().setText(key)

    @Slot()
    def do_copy_key(self):
        selected = self._selected_key()
        if not selected or not selected["key"]:
            return
        QApplication.clipboard().setText(selected["key"])
        self.set_status("Key 已复制到剪贴板")

    @Slot()
    def do_toggle_key(self):
        selected = self._selected_key()
        if not selected:
            return
        if selected["launcher"]:
            self.info(
                self, "局域网 Key",
                "这是局域网模式自动生成的 Key，不能单独停用。\n\n"
                "要更换它请点「重新生成局域网 Key」。")
            return
        try:
            entries = list(wb_proxy.configured_keys())
            for entry in entries:
                if entry.get("id") == selected["id"]:
                    entry["enabled"] = not entry.get("enabled", True)
            wb_settings.set_api_keys(wb_proxy.ACCOUNTS_DIR, entries)
        except Exception as exc:
            self.fail("操作失败", str(exc))
            return
        self.refresh_keys()
        self.refresh_status()

    @Slot()
    def do_delete_key(self):
        selected = self._selected_key()
        if not selected:
            return
        if selected["launcher"]:
            self.info(
                self, "局域网 Key",
                "这是局域网模式自动生成的 Key。\n\n"
                "关闭「允许局域网访问」即可让它失效。")
            return
        if not self.ask(
                "确认删除",
                "删除后使用这个 Key 的客户端会立刻失去访问权限。\n\n确定删除？"):
            return
        try:
            entries = [e for e in wb_proxy.configured_keys()
                       if e.get("id") != selected["id"]]
            wb_settings.set_api_keys(wb_proxy.ACCOUNTS_DIR, entries)
        except Exception as exc:
            self.fail("删除失败", str(exc))
            return
        self.refresh_keys()
        self.refresh_status()
        self.set_status("已删除 Key")

    # -------------------------------------------------------- panel password
    @Slot()
    def refresh_panel_password_state(self):
        try:
            is_default = wb_settings.panel_password_is_default(
                wb_proxy.ACCOUNTS_DIR)
        except Exception:
            is_default = True
        if is_default:
            self.panel_pwd_status.setText(
                "当前是默认密码 admin —— 建议改成自己的密码。")
        else:
            self.panel_pwd_status.setText("当前已设置自定义密码。")

    @Slot()
    def do_set_panel_password(self):
        new = self.new_pwd_edit.text()
        confirm = self.new_pwd_confirm.text()
        if not new:
            self.info("请输入密码", "新密码不能为空。")
            return
        if new != confirm:
            self.warn("两次输入不一致", "请重新输入两遍相同的新密码。")
            return
        if len(new) < 4:
            self.info("密码太短", "请至少使用 4 个字符。")
            return
        try:
            wb_settings.set_panel_password(wb_proxy.ACCOUNTS_DIR, new)
        except Exception as exc:
            self.fail("修改失败", str(exc))
            return
        self.new_pwd_edit.clear()
        self.new_pwd_confirm.clear()
        self.refresh_panel_password_state()
        self.set_status("网页面板密码已修改")
        self.info("已修改",
                                "网页面板密码已更新。浏览器里需要重新登录。")

    @Slot()
    def do_reset_panel_password(self):
        """Restore the default password without knowing the old one.

        The panel's own change-password flow requires the current password,
        which is useless to someone who has forgotten it. Anyone who can open
        this window already has local access to the data directory, so the
        password protects the browser route, not this one.
        """
        if not self.ask(
                "重置密码",
                "把网页面板密码重置为默认的 admin？\n\n"
                "重置后浏览器里用 admin 即可登录。"):
            return
        try:
            wb_settings.set_panel_password(
                wb_proxy.ACCOUNTS_DIR, wb_settings.DEFAULT_PANEL_PASSWORD)
        except Exception as exc:
            self.fail("重置失败", str(exc))
            return
        self.refresh_panel_password_state()
        self.set_status("已重置为默认密码 admin")
        self.info("已重置",
                                "网页面板密码已重置为 admin。")

    # -------------------------------------------------------------- accounts
    @Slot()
    def on_account_added(self, _account):
        self.refresh_accounts()
        self.set_status("已添加账号")

    @Slot()
    def do_add_account(self):
        if wb_proxy.POOL is None:
            self.info(
                self, "请先启动服务",
                "添加账号需要先启动服务。\n\n点右上角「启动服务」后再试。")
            return
        dialog = LoginDialog(self, wb_proxy.POOL, parent=self)
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        dialog.show()

    @Slot()
    def do_scan_desktop(self):
        """Scan for desktop-app credentials and let the user pick which to import.

        Both realms are listed. The proxy never adopts the desktop client's
        login on its own: the credential could belong to the other realm, and
        serving one realm's account where the other is expected is precisely
        what the realm split exists to prevent.
        """
        if wb_proxy.POOL is None:
            self.info("请先启动服务",
                                    "导入账号需要先启动服务。")
            return
        self.set_status("正在扫描桌面应用凭证…")
        self.run_async(wb_proxy.desktop_credential_scan, self._on_scan_done,
                       lambda msg: (self.set_status("扫描失败：%s" % msg),
                                    self.fail("扫描失败", msg)))

    def _on_scan_done(self, found):
        if not found:
            self.set_status("未发现桌面应用凭证")
            self.info(
                self, "未找到凭证",
                "本机没有找到 WorkBuddy 桌面应用的登录凭证。\n\n"
                "可以改用「添加账号 (OAuth 授权)」，无需安装桌面应用。")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("导入桌面应用凭证")
        dialog.setMinimumWidth(580)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = QLabel("发现以下凭据，勾选要导入的账号：")
        title.setObjectName("PanelTitle")
        layout.addWidget(title)

        listbox = QListWidget()
        entries = []
        for item in found:
            valid = bool(item.get("valid"))
            text = "%s · %s · %s" % (
                item.get("realmName") or item.get("realm"),
                item.get("nickname") or (item.get("uid") or "?")[:8],
                item.get("file") or "")
            if item.get("error"):
                text += "   (%s)" % item["error"]
            row = QListWidgetItem(text)
            row.setFlags(row.flags() | Qt.ItemIsUserCheckable)
            row.setCheckState(Qt.Checked if valid else Qt.Unchecked)
            if not valid:
                row.setFlags(row.flags() & ~Qt.ItemIsEnabled)
            listbox.addItem(row)
            entries.append(item)
        layout.addWidget(listbox)

        layout.addWidget(hint_label(
            "国内版与国际版账号会分别入池，请求按区域路由，不会互相串用。"))

        buttons = QDialogButtonBox()
        import_button = buttons.addButton("导入所选", QDialogButtonBox.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.RejectRole)
        layout.addWidget(buttons)

        def do_import():
            picked = [entries[i] for i in range(listbox.count())
                      if listbox.item(i).checkState() == Qt.Checked]
            dialog.accept()
            if not picked:
                return
            self.set_status("正在导入 %d 个账号…" % len(picked))

            def work():
                results = []
                for item in picked:
                    try:
                        account = wb_proxy.POOL.import_desktop_credential(
                            item["path"], realm=item.get("realm"))
                        results.append((True, account.nickname or account.uid[:8]))
                    except Exception as exc:
                        results.append((False, str(exc)))
                return results

            def done(results):
                good = [name for ok, name in results if ok]
                bad = [msg for ok, msg in results if not ok]
                self.refresh_accounts()
                if good:
                    self.set_status("已导入：%s" % "、".join(good))
                if bad:
                    self.warn("部分导入失败", "\n".join(bad))
                elif good:
                    self.info("导入完成",
                                            "已导入 %d 个账号：\n%s"
                                            % (len(good), "、".join(good)))

            self.run_async(work, done,
                           lambda msg: self.fail("导入失败", msg))

        import_button.clicked.connect(do_import)
        buttons.rejected.connect(dialog.reject)
        dialog.exec()

    @Slot()
    def do_fetch_credits(self):
        if wb_proxy.POOL is None or not wb_proxy.POOL.accounts:
            self.info("没有账号", "请先添加账号。")
            return
        self.set_status("正在查询额度…")

        def work():
            out = []
            for account in list(wb_proxy.POOL.accounts):
                try:
                    result = account.fetch_credits()
                    out.append((account.uid, bool(result.get("ok")),
                                result.get("error") or ""))
                except Exception as exc:
                    out.append((account.uid, False, str(exc)))
            return out

        def done(results):
            failed = [r for r in results if not r[1]]
            self.refresh_accounts()
            self.set_status("额度查询完成，失败 %d 个" % len(failed)
                            if failed else "额度查询完成")

        self.run_async(work, done,
                       lambda msg: self.set_status("额度查询失败：%s" % msg))

    @Slot()
    def do_refresh_tokens(self):
        if wb_proxy.POOL is None or not wb_proxy.POOL.accounts:
            self.info("没有账号", "请先添加账号。")
            return
        self.set_status("正在刷新令牌…")

        def work():
            out = []
            for account in list(wb_proxy.POOL.accounts):
                try:
                    ok = account.refresh()
                    account.save(wb_proxy.ACCOUNTS_DIR)
                    out.append((account.uid, ok, account.last_error))
                except Exception as exc:
                    out.append((account.uid, False, str(exc)))
            return out

        def done(results):
            ok_count = sum(1 for _, ok, _ in results if ok)
            self.refresh_accounts()
            self.set_status("已刷新 %d/%d 个账号" % (ok_count, len(results)))

        self.run_async(work, done, lambda msg: self.set_status("刷新失败：%s" % msg))

    def _selected_uid(self):
        row = self.account_table.currentRow()
        if row < 0:
            self.info("未选择", "请先在列表里选中一个账号。")
            return None
        item = self.account_table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    @Slot()
    def do_toggle_account(self):
        uid = self._selected_uid()
        if not uid or wb_proxy.POOL is None:
            return
        account = wb_proxy.POOL.get(uid)
        if account is None:
            return
        wb_proxy.POOL.set_enabled(uid, not account.enabled)
        self.refresh_accounts()

    @Slot()
    def do_delete_account(self):
        uid = self._selected_uid()
        if not uid or wb_proxy.POOL is None:
            return
        account = wb_proxy.POOL.get(uid)
        name = (account.nickname if account else uid[:8]) or uid[:8]
        if not self.ask(
                "确认删除",
                "确定删除账号 %s 吗？\n\n删除后需要重新登录才能恢复。" % name):
            return
        wb_proxy.POOL.remove(uid)
        self.refresh_accounts()
        self.set_status("已删除 %s" % name)

    def do_set_all(self, enabled):
        if wb_proxy.POOL is None:
            return
        realm = self.realm_filter.currentData()
        wb_proxy.POOL.set_all_enabled(enabled, None if realm == "all" else realm)
        self.refresh_accounts()

    # ------------------------------------------------------------------- logs
    @Slot()
    def do_clear_logs(self):
        wb_proxy.clear_logs()
        self._log_tail = 0
        self.log_view.clear()
        self.refresh_logs(True)

    @staticmethod
    def _resolve_export_target(chosen):
        """Validate a path the user picked in a save dialog.

        The picker already decided where the file goes, so this is not a
        trust boundary - it exists so a malformed value produces a clear error
        rather than a file in an unexpected place. Returns (path, error).
        """
        if not chosen:
            return None, "未选择文件"
        normalized = os.path.normpath(chosen)
        if ".." in normalized.split(os.sep):
            return None, "路径中不允许包含 .."
        target = os.path.realpath(normalized)
        if not os.path.isabs(target):
            return None, "请选择绝对路径"
        parent = os.path.dirname(target)
        if not os.path.isdir(parent):
            return None, "目标文件夹不存在：%s" % parent
        if os.path.isdir(target):
            return None, "目标是一个文件夹，请指定文件名"
        return target, ""

    @staticmethod
    def _write_export(path, text):
        """Write an export file, creating it with os.open.

        Uses os.open rather than the built-in open() so the destination is
        explicit and the file is created with owner-only permissions on
        platforms that honour them - these exports can carry credentials.
        """
        data = text.encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    @Slot()
    def do_export_logs(self):
        """Save the in-memory log buffer to a text file."""
        entries = wb_proxy.get_logs(limit=5000)
        rows = entries.get("logs") or []
        if not rows:
            self.info("没有日志", "当前没有可导出的日志。")
            return
        default = os.path.join(
            wb_runtime.data_dir(),
            "wb-proxy-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
        chosen, _ = QFileDialog.getSaveFileName(
            self, "导出日志", default, "日志文件 (*.log);;文本文件 (*.txt)")
        target, problem = self._resolve_export_target(chosen)
        if problem:
            if chosen:
                self.fail("路径无效", problem)
            return
        body = "".join("[%s] [%s] [%s] %s\n" % (
            row.get("ts", ""), row.get("level", ""),
            row.get("tag", ""), row.get("msg", "")) for row in rows)
        try:
            self._write_export(target, body)
        except Exception as exc:
            self.fail("导出失败", str(exc))
            return
        self.set_status("日志已导出")
        self.info("导出完成", "已导出 %d 条日志到：\n\n%s" % (len(rows), target))

    # ------------------------------------------------------- account transfer
    @Slot()
    def do_export_accounts(self):
        """Write the account pool to a JSON document for backup or migration."""
        pool = wb_proxy.POOL
        if pool is None or not pool.accounts:
            self.info("没有账号", "当前没有账号可导出。")
            return
        default = os.path.join(
            wb_runtime.data_dir(),
            "workbuddy-accounts-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
        chosen, _ = QFileDialog.getSaveFileName(
            self, "导出账号", default, "JSON 文件 (*.json)")
        target, problem = self._resolve_export_target(chosen)
        if problem:
            if chosen:
                self.fail("路径无效", problem)
            return
        payload = {
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "count": len(pool.accounts),
            "accounts": [account.to_dict() for account in pool.accounts],
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            self._write_export(target, body)
        except Exception as exc:
            self.fail("导出失败", str(exc))
            return
        self.set_status("已导出 %d 个账号" % len(pool.accounts))
        # Say plainly that the file carries live credentials: it is the kind of
        # file people mail to themselves without thinking.
        self.warn("导出完成",
                  "已导出 %d 个账号到：\n\n%s\n\n"
                  "注意：文件里包含账号的登录凭证（明文），"
                  "请勿分享或放进同步盘。" % (len(pool.accounts), target))

    @Slot()
    def do_import_accounts(self):
        """Load an exported document, previewing what it would change first."""
        if wb_proxy.POOL is None:
            self.info("请先启动服务", "导入账号需要先启动服务。")
            return
        chosen, _ = QFileDialog.getOpenFileName(
            self, "导入账号", wb_runtime.data_dir(), "JSON 文件 (*.json)")
        if not chosen:
            return
        source = os.path.realpath(os.path.normpath(chosen))
        if ".." in os.path.normpath(chosen).split(os.sep):
            self.fail("路径无效", "路径中不允许包含 ..")
            return
        if not os.path.isfile(source):
            self.fail("文件不存在", source)
            return
        if os.path.getsize(source) > 32 * 1024 * 1024:
            self.fail("文件过大", "这个文件不像是账号导出文件。")
            return
        try:
            with open(source, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            self.fail("读取失败", str(exc))
            return

        rows = data.get("accounts") if isinstance(data, dict) else data
        if not isinstance(rows, list) or not rows:
            self.fail("格式不对",
                      "这个文件里没有账号列表。\n\n"
                      "请选择由本程序「导出账号」生成的文件。")
            return

        try:
            preview = wb_proxy.POOL.preview_import_rows(rows)
        except Exception as exc:
            self.fail("预览失败", str(exc))
            return

        summary = ("将要导入 %d 个账号：\n\n"
                   "  新增    %d 个\n"
                   "  覆盖    %d 个\n"
                   "  跳过    %d 个（已存在）\n"
                   "  无效    %d 个\n\n"
                   "导入后同名账号会被文件里的版本覆盖。确定继续？"
                   % (len(rows), len(preview["added"]),
                      len(preview["updated"]), len(preview["skipped"]),
                      len(preview["invalid"])))
        if not self.ask("确认导入", summary):
            return

        def work():
            return wb_proxy.POOL.import_rows(rows, overwrite=True)

        def done(result):
            self.refresh_accounts()
            added = len((result or {}).get("added") or [])
            updated = len((result or {}).get("updated") or [])
            self.set_status("已导入：新增 %d，覆盖 %d" % (added, updated))
            self.info("导入完成",
                      "新增 %d 个，覆盖 %d 个账号。" % (added, updated))

        self.run_async(work, done, lambda msg: self.fail("导入失败", msg))

    # ------------------------------------------------------------ tasks panel
    @Slot()
    def refresh_tasks(self):
        """Show scheduler state and its recent runs."""
        sched = getattr(wb_proxy, "SCHEDULER", None)
        if sched is None:
            self.task_state.setText("调度器未启动（服务未运行）")
            self.task_log.clear()
            return
        try:
            status = sched.status()
        except Exception as exc:
            self.task_state.setText("读取调度器状态失败：%s" % exc)
            return
        self.task_state.setText(
            "状态：%s　·　上次运行：%s　·　下次运行：%s"
            % ("已启用" if status.get("enabled") else "已暂停",
               status.get("last_run_time") or "尚未运行",
               status.get("next_run_time") or "待调度"))
        self.task_mode.setText(status.get("mode") or "")
        logs = status.get("logs") or []
        self.task_log.setPlainText("\n".join(logs[-60:]) if logs
                                   else "（暂无执行记录）")

    @Slot()
    def do_toggle_scheduler(self):
        sched = getattr(wb_proxy, "SCHEDULER", None)
        if sched is None:
            self.info("服务未运行", "请先启动服务。")
            return
        try:
            sched.enabled = not sched.enabled
        except Exception as exc:
            self.fail("操作失败", str(exc))
            return
        self.refresh_tasks()
        self.set_status("定时任务已%s" % ("启用" if sched.enabled else "暂停"))

    @Slot()
    def do_run_tasks_now(self):
        sched = getattr(wb_proxy, "SCHEDULER", None)
        if sched is None:
            self.info("服务未运行", "请先启动服务。")
            return
        try:
            result = sched.trigger_now()
        except Exception as exc:
            self.fail("触发失败", str(exc))
            return
        self.set_status((result or {}).get("msg") or "已触发")
        QTimer.singleShot(2000, self.refresh_tasks)

    # --------------------------------------------------------------- settings
    def do_toggle_autostart(self, checked):
        if checked:
            ok, message = wb_autostart.install(
                port=int(self.port_spin.value()),
                minimized=self.start_min_check.isChecked())
        else:
            ok, message = wb_autostart.uninstall()
        if not ok:
            # Put the checkbox back where reality is.
            self.autostart_check.blockSignals(True)
            self.autostart_check.setChecked(wb_autostart.is_enabled())
            self.autostart_check.blockSignals(False)
            self.fail("开机自启", message)
        self.set_status(message)
        self._refresh_autostart_status()

    @Slot()
    def do_save_settings(self):
        prefs = self._collect_prefs()
        self.gateway.prefs.update(prefs)
        try:
            wb_gateway.save_prefs(self.gateway.prefs)
        except Exception as exc:
            self.fail("保存失败", str(exc))
            return
        self.prefs = dict(self.gateway.prefs)
        if wb_autostart.is_enabled():
            wb_autostart.install(port=prefs["port"],
                                 minimized=prefs.get("start_minimized", True))
        self._refresh_autostart_status()
        if self.gateway.is_running():
            self.set_status("设置已保存（监听改动需重启服务生效）")
            self.info(
                self, "已保存",
                "设置已保存。\n\n监听地址与端口的改动需要重启服务后生效。")
        else:
            self.set_status("设置已保存")

    @Slot()
    def do_reset_settings(self):
        if not self.ask("恢复默认", "确定把所有设置恢复为默认值吗？"):
            return
        self.prefs = dict(wb_gateway.DEFAULTS)
        self.gateway.prefs.update(self.prefs)
        wb_gateway.save_prefs(self.gateway.prefs)
        self._load_prefs_into_ui()
        self.set_status("已恢复默认设置")

    # ---------------------------------------------------------------- refresh
    @Slot()
    def refresh_status(self):
        status = self.gateway.status()
        running = status.get("running")

        self.state_label.setText("● 运行中" if running else "○ 已停止")
        self.state_label.setProperty("state", "running" if running else "stopped")
        # Re-polish so the property-based rule takes effect immediately.
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)

        parts = ["监听 %s" % status.get("url", "") if running else "未在监听"]
        if running:
            parts.append("已运行 %s" % self._format_uptime(status.get("uptime")))
        if status.get("error"):
            parts.append(status["error"])
        self.detail_label.setText("  ·  ".join(parts))

        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(bool(running))
        self.restart_button.setEnabled(bool(running))
        self.panel_button.setEnabled(bool(running))
        if self._tray:
            self.tray_toggle_action.setText("停止服务" if running else "启动服务")

        self.cards["state"].set_value(
            "运行中" if running else "已停止",
            self._format_uptime(status.get("uptime")) if running else "")
        self.cards["accounts"].set_value(
            status.get("accounts", 0), "可用 %d" % status.get("usable", 0))

        try:
            totals = wb_proxy.usage_index().totals(wb_proxy.CURRENT_REALM)
        except Exception:
            totals = {}
        self.cards["requests"].set_value(self._num(totals.get("requests")))
        self.cards["tokens"].set_value(self._num(totals.get("total_tokens")))
        errors = totals.get("errors", 0)
        self.cards["errors"].set_value(
            self._num(errors), "有失败请求" if errors else "全部成功")

        self.access["url"].set_text("%s/v1" % status.get("url", ""))
        # Report the key situation in terms of what a client must do, which is
        # what the operator needs to know when wiring one up.
        key = status.get("api_key") or ""
        if status.get("lan"):
            shown = key or "(尚未生成)"
        elif status.get("auth_required"):
            shown = key or "(已启用校验，但未设置 Key)"
        elif key:
            shown = "%s  (本机不强制，客户端可填)" % key
        else:
            shown = "(本机模式无需 Key)"
        self.access["key"].set_text(shown)
        self.access["realm"].set_text(realm_label(wb_proxy.CURRENT_REALM))
        self.access["scheduler"].set_text(status.get("scheduler_next") or "未启用")

    @Slot()
    def refresh_accounts(self):
        pool = wb_proxy.POOL
        table = self.account_table
        table.setRowCount(0)

        if pool is None:
            self.account_hint.setText(
                "服务未启动。点右上角「启动服务」后即可添加账号。")
            return

        realm_filter = self.realm_filter.currentData()
        try:
            usage = {bucket["account"]: bucket
                     for bucket in wb_proxy.usage_index().by_account()}
        except Exception:
            usage = {}

        now = time.time()
        rows = []
        for account in list(pool.accounts):
            if realm_filter != "all" and account.realm != realm_filter:
                continue
            if not account.enabled:
                state = "已停用"
            elif account.cooldown_until > now:
                state = "冷却 %d 秒" % int(account.cooldown_until - now)
            else:
                state = "可用"
            credits = account.credits or {}
            credit_text = ("%s / %s" % (self._num(credits.get("remain")),
                                        self._num(credits.get("size")))
                           if credits else "未查询")
            bucket = usage.get(account.uid) or {}
            rows.append((account, state, credit_text,
                         self._format_delta((account.expires_at or 0) - now),
                         self._num(bucket.get("requests")),
                         self._num(bucket.get("total_tokens"))))

        table.setRowCount(len(rows))
        for index, (account, state, credit_text, expires,
                    requests, tokens) in enumerate(rows):
            values = [account.uid or "?", realm_label(account.realm), state,
                      expires, credit_text, requests, tokens]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(Qt.UserRole, account.uid)
                    item.setToolTip("%s\n来源：%s\n文件：%s"
                                    % (account.uid, account.source,
                                       account.path or "(未保存)"))
                if not account.enabled:
                    item.setForeground(QColor(theme.FG_MUTED))
                elif column == 2:
                    if state == "可用":
                        item.setForeground(QColor(theme.SUCCESS))
                    elif state.startswith("冷却"):
                        item.setForeground(QColor(theme.WARNING))
                if account.last_error and column == 2:
                    item.setToolTip(account.last_error)
                table.setItem(index, column, item)

        total = len(pool.accounts)
        usable = pool.count_usable()
        if total == 0:
            self.account_hint.setText(
                "还没有账号。点「添加账号 (OAuth 授权)」用浏览器登录，"
                "国内版与国际版都可以；或点「扫描桌面应用凭证」导入本机已登录的账号。")
        else:
            hidden = total - len(rows)
            suffix = "（已按区域过滤，另有 %d 个未显示）" % hidden if hidden else ""
            self.account_hint.setText(
                "共 %d 个账号，当前可用 %d 个%s。多账号按会话粘性 + 轮询使用，"
                "某个账号被上游拒绝时会自动切到下一个。"
                % (total, usable, suffix))

    @Slot()
    def refresh_usage(self):
        try:
            index = wb_proxy.usage_index()
            totals = index.totals(wb_proxy.CURRENT_REALM)
            by_model = index.by_model(wb_proxy.CURRENT_REALM)
        except Exception:
            totals, by_model = {}, {}

        for key, widget in self.usage_values.items():
            widget.setText(self._num(totals.get(key)))

        ordered = sorted(by_model.items(),
                         key=lambda kv: -(kv[1].get("total_tokens") or 0))
        table = self.model_table
        table.setRowCount(len(ordered))
        for row, (model, bucket) in enumerate(ordered):
            values = [model,
                      self._num(bucket.get("requests")),
                      self._num(bucket.get("prompt_tokens")),
                      self._num(bucket.get("completion_tokens")),
                      self._num(bucket.get("reasoning_tokens")),
                      self._num(bucket.get("total_tokens"))]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 4:
                    item.setForeground(QColor(theme.THINK))
                elif column == 5:
                    item.setForeground(QColor(theme.ACCENT))
                table.setItem(row, column, item)

        self.usage_hint.setText(
            "以上为 %s 的累计用量，数据来自 usage.jsonl，重启不丢。"
            % realm_label(wb_proxy.CURRENT_REALM))

        # ---- recent requests ----
        try:
            recent = wb_proxy.usage_index().recent(
                limit=80, realm=wb_proxy.CURRENT_REALM)[1]
        except Exception:
            recent = []
        rtable = self.recent_table
        rtable.setRowCount(len(recent))
        for row_index, entry in enumerate(reversed(recent)):
            stamp = (entry.get("iso") or "").replace("T", " ")[5:]
            is_error = bool(entry.get("error"))
            elapsed = entry.get("elapsed_ms")
            ttft = entry.get("ttft_ms")
            speed = entry.get("tokens_per_sec")
            values = [
                stamp,
                entry.get("model") or "?",
                (entry.get("account") or "—")[:8],
                "失败" if is_error else ("流式" if entry.get("stream") else "非流式"),
                self._num(elapsed) + " ms" if elapsed is not None else "—",
                self._num(ttft) + " ms" if ttft is not None else "—",
                "%.1f" % speed if speed else "—",
                self._num(entry.get("total_tokens")),
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if is_error:
                    item.setForeground(QColor(theme.DANGER))
                elif column == 3:
                    item.setForeground(QColor(theme.SUCCESS))
                rtable.setItem(row_index, column, item)

    @Slot()
    def refresh_logs(self, force=False):
        level = self.log_level.currentText()
        search = self.log_search.text().strip()
        entries = wb_proxy.get_logs(limit=400,
                                    level="" if level == "全部" else level,
                                    search=search,
                                    since_id=0 if force else self._log_tail)
        rows = entries.get("logs") or []
        if rows:
            self._log_tail = max(row.get("id", 0) for row in rows)

        if force:
            self.log_view.clear()

        if rows:
            # Cap what is rendered per tick so a burst cannot stall the UI.
            for row in rows[-400:]:
                level_name = row.get("level", "INFO")
                color = theme.LEVEL_COLORS.get(level_name, theme.FG_DIM)
                message = (row.get("msg", "")
                           .replace("&", "&amp;").replace("<", "&lt;")
                           .replace(">", "&gt;"))
                self.log_view.appendHtml(
                    '<span style="color:%s">%s</span>'
                    '<span style="color:%s">  %s</span>'
                    '<span style="color:%s">  [%s]</span>'
                    '<span style="color:%s">  %s</span>'
                    % (theme.FG_MUTED, row.get("time", ""),
                       color, level_name,
                       theme.FG_MUTED, row.get("tag", ""),
                       theme.FG_DIM, message))
            if self.log_autoscroll.isChecked():
                bar = self.log_view.verticalScrollBar()
                bar.setValue(bar.maximum())

    # ------------------------------------------------------------------- close
    def closeEvent(self, event):
        """Closing must not silently kill a gateway others may be using."""
        if self._quitting or not self.gateway.is_running():
            if self._tray:
                self._tray.hide()
            event.accept()
            return

        box = QMessageBox(self)
        box.setWindowTitle("退出")
        box.setText("网关正在运行。")
        box.setInformativeText(
            "「停止并退出」会中断正在使用它的客户端；\n"
            "「最小化到托盘」则后台继续服务。")
        stop_button = box.addButton("停止并退出", QMessageBox.DestructiveRole)
        tray_button = box.addButton("最小化到托盘", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()

        clicked = box.clickedButton()
        if clicked is stop_button:
            self.gateway.stop()
            if self._tray:
                self._tray.hide()
            event.accept()
        elif clicked is tray_button:
            self._minimize_to_tray()
            event.ignore()
        else:
            event.ignore()

    # ------------------------------------------------------------- formatting
    @staticmethod
    def _num(value):
        try:
            return "{:,}".format(int(value or 0))
        except (TypeError, ValueError):
            return "—"

    @staticmethod
    def _format_delta(seconds):
        if seconds is None:
            return "—"
        seconds = int(seconds)
        if seconds <= 0:
            return "已过期"
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days:
            return "%d 天 %d 小时" % (days, hours)
        if hours:
            return "%d 小时 %d 分" % (hours, minutes)
        return "%d 分" % minutes

    @staticmethod
    def _format_uptime(seconds):
        if not seconds:
            return ""
        seconds = int(seconds)
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return "%d 小时 %d 分" % (hours, minutes)
        if minutes:
            return "%d 分 %d 秒" % (minutes, secs)
        return "%d 秒" % secs


def main(argv=None):
    parser = argparse.ArgumentParser(description="WorkBuddy2API 图形界面")
    parser.add_argument("--port", type=int, default=None,
                        help="覆盖保存的监听端口")
    parser.add_argument("--minimized", action="store_true",
                        help="启动后最小化到托盘（供开机自启使用）")
    parser.add_argument("--start", action="store_true",
                        help="启动后立即开始监听")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    wb_runtime.configure_streams()

    if args.port:
        prefs = wb_gateway.load_prefs()
        prefs["port"] = max(1, min(65535, int(args.port)))
        wb_gateway.save_prefs(prefs)

    app = QApplication(sys.argv)
    app.setApplicationName("WorkBuddy2API")
    app.setApplicationDisplayName("WorkBuddy2API 网关")
    app.setWindowIcon(make_icon())
    app.setStyleSheet(theme.stylesheet())
    # A tray app should outlive its last window being closed.
    app.setQuitOnLastWindowClosed(False)

    window = MainWindow(args)
    window.show()

    try:
        code = app.exec()
    except KeyboardInterrupt:
        code = 0
    finally:
        try:
            window.gateway.stop()
        except Exception:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
