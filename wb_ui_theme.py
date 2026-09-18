"""wb_ui_theme.py —— 界面主题：配色常量与 Qt 样式表。

设计取向：像一个正常软件，而不是一块仪表盘。

早期版本的问题（用户直接指出的"AI 味"）：每个元素都套一个细边框，卡片里再套
输入框样式的只读字段，一屏里数字用了四种强调色，底色是深紫。线条密、颜色多、
层次靠边框而非排版 —— 这是生成式界面最典型的特征。

现在的做法：

* **浅色为主**：页面底色极浅灰，内容区纯白，靠留白与字号分层，而不是靠线。
* **边框克制**：只在真正需要分隔处用一道极浅的线；卡片用很淡的描边；表格行
  之间才允许有细线。
* **单一强调色**：蓝色只给可交互的主操作。数字默认是正文色，只有状态（运行中
  / 失败）才上色，一屏最多两处彩色。
* **只读值不像输入框**：改为"小号灰标签 + 正文"，一眼看出是展示不是编辑。

配色以语义化常量暴露（``BG``、``SURFACE``、``TEXT`` 等），界面代码只引用名字。
提供浅色与深色两套，默认浅色。
"""

import copy

# ---------------------------------------------------------------------------
# 浅色（默认）
# ---------------------------------------------------------------------------
LIGHT = {
    "BG": "#f4f5f7",           # 页面底色
    "SURFACE": "#ffffff",      # 卡片 / 面板
    "SURFACE_ALT": "#fafbfc",  # 次级区域（表头、只读行）
    "BORDER": "#e4e6ea",       # 主要分隔线
    "BORDER_SOFT": "#eef0f2",  # 极淡分隔

    "TEXT": "#1f2430",         # 正文
    "TEXT_DIM": "#5b6470",     # 次要说明
    "TEXT_MUTED": "#98a1ad",   # 标签、占位

    "ACCENT": "#2f6fed",       # 主操作
    "ACCENT_HOVER": "#2359d0",
    "ACCENT_SOFT": "#eaf1fe",  # 主操作的浅背景（选中行、菜单高亮）

    "SUCCESS": "#0f9d58",
    "WARNING": "#c77700",
    "DANGER": "#d92d20",
    "THINK": "#7c5cff",

    "CODE_BG": "#f7f8fa",
    "ON_ACCENT": "#ffffff",    # 强调色上的文字
}

# ---------------------------------------------------------------------------
# 深色（可选）
# ---------------------------------------------------------------------------
DARK = {
    "BG": "#1b1d22",
    "SURFACE": "#23262c",
    "SURFACE_ALT": "#282b32",
    "BORDER": "#33373f",
    "BORDER_SOFT": "#2b2f36",

    "TEXT": "#e8eaee",
    "TEXT_DIM": "#a8b0bc",
    "TEXT_MUTED": "#6f7885",

    "ACCENT": "#5b8def",
    "ACCENT_HOVER": "#6f9bf2",
    "ACCENT_SOFT": "#262f3f",

    "SUCCESS": "#4ec27f",
    "WARNING": "#e0a63a",
    "DANGER": "#f0665c",
    "THINK": "#a48cff",

    "CODE_BG": "#1a1c21",
    "ON_ACCENT": "#ffffff",
}

#: 兼容旧名：早期代码引用了 CARD / FG / FG_DIM / FG_MUTED。
#: 保留别名以免逐个改调用点，同时让新名字成为首选。
_ALIASES = {
    "CARD": "SURFACE",
    "CARD_HOVER": "SURFACE_ALT",
    "BG_ALT": "SURFACE",
    "FG": "TEXT",
    "FG_DIM": "TEXT_DIM",
    "FG_MUTED": "TEXT_MUTED",
    "SUCCESS_DIM": "SUCCESS",
}

_dark = False
_STYLESHEET = None


def _apply():
    """Copy the active palette (and aliases) into module-level names."""
    palette = DARK if _dark else LIGHT
    for key, value in palette.items():
        globals()[key] = value
    for old, new in _ALIASES.items():
        globals()[old] = palette[new]
    globals()["LEVEL_COLORS"] = {
        "INFO": palette["TEXT_DIM"],
        "WARN": palette["WARNING"],
        "ERROR": palette["DANGER"],
    }
    global _STYLESHEET
    _STYLESHEET = None


def palette():
    """A copy of the active palette, for widgets that need raw colours."""
    return copy.copy(DARK if _dark else LIGHT)


def set_dark(enabled):
    """Switch to the dark palette. Returns the new state."""
    global _dark
    _dark = bool(enabled)
    _apply()
    return _dark


def is_dark():
    return _dark


def level_color(level):
    """Colour for a log level in the active palette."""
    table = DARK if _dark else LIGHT
    return {
        "INFO": table["TEXT_DIM"],
        "WARN": table["WARNING"],
        "ERROR": table["DANGER"],
    }.get(level, table["TEXT_DIM"])


_apply()


def stylesheet():
    """The application style sheet for the active palette.

    Kept in one place so the whole look can be reviewed together. Selectors
    lean on object names and dynamic properties rather than deep descendant
    chains, so renaming a container does not silently drop its styling.
    """
    global _STYLESHEET
    if _STYLESHEET is not None:
        return _STYLESHEET

    p = DARK if _dark else LIGHT
    _STYLESHEET = """
/* ---------------------------------------------------------------- base */
QWidget {
    background: %(BG)s;
    color: %(TEXT)s;
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 9.5pt;
}
QMainWindow, QDialog { background: %(BG)s; }
QToolTip {
    background: %(SURFACE)s; color: %(TEXT)s;
    border: 1px solid %(BORDER)s; padding: 5px 7px;
}

/* --------------------------------------------------------------- header */
#HeaderBar {
    background: %(SURFACE)s;
    border-bottom: 1px solid %(BORDER)s;
}
#AppTitle { font-size: 12pt; font-weight: 600; }
#StateText { font-size: 10.5pt; font-weight: 600; color: %(TEXT)s; }
#StateText[state="running"] { color: %(SUCCESS)s; }
#StateText[state="stopped"] { color: %(TEXT_MUTED)s; }
#DetailText { color: %(TEXT_MUTED)s; font-size: 8.5pt; }

/* Labels inside a card must not paint their own background: Qt fills a
   QLabel with the window colour by default, which showed as a grey band
   behind every caption sitting on a white surface. */
QLabel { background: transparent; }

/* ---------------------------------------------------------------- cards */
/* A card is a white surface with a hairline edge. No shadow and no nested
   boxes: the separation comes from the surface sitting on the page. */
#Card, #Panel {
    background: %(SURFACE)s;
    border: 1px solid %(BORDER_SOFT)s;
    border-radius: 8px;
}
#CardTitle { color: %(TEXT_MUTED)s; font-size: 8.5pt; }
#CardValue { font-size: 16pt; font-weight: 600; color: %(TEXT)s; }
#CardSub { color: %(TEXT_MUTED)s; font-size: 8pt; }

#PanelTitle {
    font-size: 10pt;
    font-weight: 600;
    color: %(TEXT)s;
}
#Hint { color: %(TEXT_MUTED)s; font-size: 8.5pt; background: transparent; }
#HintWarn { color: %(WARNING)s; font-size: 8.5pt; background: transparent; }

/* Read-only values read as text, not as a disabled input. */
#FieldLabel { color: %(TEXT_MUTED)s; font-size: 8.5pt; background: transparent; }
#FieldValue {
    color: %(TEXT)s;
    font-size: 9.5pt;
    background: transparent;
    border: none;
    padding: 0;
}

/* A thin rule, used only where a visual break is genuinely needed. */
#Divider { background: %(BORDER_SOFT)s; max-height: 1px; border: none; }

/* -------------------------------------------------------------- buttons */
QPushButton {
    background: %(ACCENT)s;
    color: %(ON_ACCENT)s;
    border: 1px solid %(ACCENT)s;
    border-radius: 6px;
    padding: 6px 14px;
    font-weight: 500;
}
QPushButton:hover { background: %(ACCENT_HOVER)s; border-color: %(ACCENT_HOVER)s; }
QPushButton:pressed { background: %(ACCENT)s; }
QPushButton:disabled {
    background: %(SURFACE_ALT)s;
    color: %(TEXT_MUTED)s;
    border-color: %(BORDER)s;
}

/* Secondary: a plain button that does not compete with the primary action. */
QPushButton[variant="secondary"] {
    background: %(SURFACE)s;
    color: %(TEXT)s;
    border: 1px solid %(BORDER)s;
    font-weight: 400;
}
QPushButton[variant="secondary"]:hover {
    background: %(SURFACE_ALT)s;
    border-color: %(TEXT_MUTED)s;
}
QPushButton[variant="secondary"]:disabled {
    color: %(TEXT_MUTED)s;
    background: %(SURFACE_ALT)s;
}

QPushButton[variant="danger"] {
    background: %(SURFACE)s;
    color: %(DANGER)s;
    border: 1px solid %(BORDER)s;
    font-weight: 400;
}
QPushButton[variant="danger"]:hover {
    border-color: %(DANGER)s;
    background: %(SURFACE)s;
}

/* A link-style button for inline actions such as "copy". */
QPushButton[variant="link"] {
    background: transparent;
    border: none;
    color: %(ACCENT)s;
    padding: 1px 3px;
    font-weight: 400;
}
QPushButton[variant="link"]:hover { color: %(ACCENT_HOVER)s; }

/* ---------------------------------------------------------------- inputs */
QLineEdit, QSpinBox, QPlainTextEdit, QTextEdit, QComboBox {
    background: %(SURFACE)s;
    color: %(TEXT)s;
    border: 1px solid %(BORDER)s;
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: %(ACCENT)s;
    selection-color: %(ON_ACCENT)s;
}
QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus,
QTextEdit:focus, QComboBox:focus { border-color: %(ACCENT)s; }
QLineEdit:disabled, QSpinBox:disabled {
    color: %(TEXT_MUTED)s; background: %(SURFACE_ALT)s;
}

QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background: %(SURFACE)s;
    color: %(TEXT)s;
    border: 1px solid %(BORDER)s;
    selection-background-color: %(ACCENT_SOFT)s;
    selection-color: %(TEXT)s;
    outline: none;
}

QCheckBox, QRadioButton { spacing: 7px; color: %(TEXT)s; background: transparent; }
QCheckBox::indicator, QRadioButton::indicator {
    width: 15px; height: 15px;
    border: 1px solid %(BORDER)s;
    background: %(SURFACE)s;
}
QCheckBox::indicator { border-radius: 3px; }
QRadioButton::indicator { border-radius: 8px; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background: %(ACCENT)s; border-color: %(ACCENT)s;
}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {
    border-color: %(ACCENT)s;
}

/* ---------------------------------------------------------------- tables */
QTableWidget, QTableView {
    background: %(SURFACE)s;
    alternate-background-color: %(SURFACE)s;
    color: %(TEXT)s;
    border: 1px solid %(BORDER_SOFT)s;
    border-radius: 8px;
    gridline-color: transparent;
    selection-background-color: %(ACCENT_SOFT)s;
    selection-color: %(TEXT)s;
    outline: none;
}
QTableWidget::item, QTableView::item {
    padding: 6px 8px;
    border: none;
    border-bottom: 1px solid %(BORDER_SOFT)s;
}
QTableWidget::item:selected, QTableView::item:selected {
    background: %(ACCENT_SOFT)s;
    color: %(TEXT)s;
}

QHeaderView { background: transparent; }
QHeaderView::section {
    background: %(SURFACE_ALT)s;
    color: %(TEXT_MUTED)s;
    border: none;
    border-bottom: 1px solid %(BORDER)s;
    padding: 7px 8px;
    font-weight: 500;
    font-size: 8.5pt;
}
QTableCornerButton::section { background: %(SURFACE_ALT)s; border: none; }

/* --------------------------------------------------------------- tabs */
/* Flat tabs with an underline for the active one - the shape a normal
   desktop application uses, rather than a box around every tab. */
QTabWidget::pane { border: none; background: transparent; }
QTabBar { background: transparent; }
QTabBar::tab {
    background: transparent;
    color: %(TEXT_DIM)s;
    padding: 8px 14px;
    margin-right: 2px;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 400;
}
QTabBar::tab:selected {
    color: %(ACCENT)s;
    border-bottom: 2px solid %(ACCENT)s;
    font-weight: 500;
}
QTabBar::tab:hover:!selected { color: %(TEXT)s; }

/* ------------------------------------------------------------ scrollbar */
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical {
    background: %(BORDER)s; border-radius: 5px; min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: %(TEXT_MUTED)s; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }

QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:horizontal {
    background: %(BORDER)s; border-radius: 5px; min-width: 30px;
}
QScrollBar::handle:horizontal:hover { background: %(TEXT_MUTED)s; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }

/* ---------------------------------------------------------------- misc */
QStatusBar {
    background: %(SURFACE)s;
    color: %(TEXT_MUTED)s;
    border-top: 1px solid %(BORDER)s;
}
QStatusBar::item { border: none; }

QPlainTextEdit#LogView {
    background: %(CODE_BG)s;
    color: %(TEXT_DIM)s;
    font-family: "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace;
    font-size: 8.5pt;
    border: 1px solid %(BORDER_SOFT)s;
    border-radius: 8px;
    padding: 8px;
}

QMenu {
    background: %(SURFACE)s; color: %(TEXT)s;
    border: 1px solid %(BORDER)s; border-radius: 6px; padding: 4px;
}
QMenu::item { padding: 6px 20px 6px 12px; border-radius: 4px; }
QMenu::item:selected { background: %(ACCENT_SOFT)s; }
QMenu::separator { height: 1px; background: %(BORDER_SOFT)s; margin: 4px 6px; }

QListWidget {
    background: %(SURFACE)s;
    border: 1px solid %(BORDER_SOFT)s;
    border-radius: 8px;
    outline: none;
}
QListWidget::item { padding: 7px 9px; border-radius: 4px; }
QListWidget::item:selected { background: %(ACCENT_SOFT)s; color: %(TEXT)s; }

QProgressBar {
    background: %(SURFACE_ALT)s; border: none; border-radius: 3px;
    height: 5px; text-align: center; color: transparent;
}
QProgressBar::chunk { background: %(ACCENT)s; border-radius: 3px; }

QGroupBox {
    border: 1px solid %(BORDER_SOFT)s;
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 10px;
    background: %(SURFACE)s;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: %(TEXT_DIM)s;
    font-size: 8.5pt;
}

QMessageBox { background: %(SURFACE)s; }
QMessageBox QLabel { color: %(TEXT)s; }
""" % p
    return _STYLESHEET
