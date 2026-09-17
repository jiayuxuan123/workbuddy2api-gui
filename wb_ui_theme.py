"""wb_ui_theme.py —— 界面主题：配色常量与 Qt 样式表。

集中放在一处，方便整体换肤；界面代码只引用语义化名字（``BG``、``ACCENT``
等），不出现十六进制字面量。

配色采用 Catppuccin Mocha 的明度关系（深底 + 柔和前景 + 低饱和强调色）：
长时间盯着看的工具用低对比底色比纯黑舒服，强调色也能看清状态而不刺眼。
"""

# ---------------------------------------------------------------- 调色板
BG = "#1e1e2e"           # 窗口底色
BG_ALT = "#181825"       # 更深的区域（侧栏、状态栏）
CARD = "#252537"         # 卡片
CARD_HOVER = "#2d2d44"
BORDER = "#313244"
BORDER_SOFT = "#292940"

FG = "#cdd6f4"           # 主文字
FG_DIM = "#a6adc8"       # 次要文字
FG_MUTED = "#6c7086"     # 极弱文字（时间戳等）

ACCENT = "#89b4fa"       # 主强调（蓝）
ACCENT_HOVER = "#9fc1fb"
SUCCESS = "#a6e3a1"      # 成功 / 运行中
SUCCESS_DIM = "#3f5f43"
WARNING = "#f9e2af"      # 警告 / 冷却
DANGER = "#f38ba8"       # 危险 / 失败
THINK = "#cba6f7"        # 思考 token / 推理

CODE_BG = "#11111b"      # 等宽文本背景（日志）

#: 状态色，供界面按语义取用。
LEVEL_COLORS = {
    "INFO": FG_DIM,
    "WARN": WARNING,
    "ERROR": DANGER,
}


def stylesheet():
    """The application-wide Qt style sheet.

    Kept as one string so the whole look can be reviewed in a single place.
    Selectors use object names and dynamic properties rather than deep
    descendant chains, so renaming a container does not silently drop styling.
    """
    return f"""
/* ---------------------------------------------------------------- base */
QWidget {{
    background: {BG};
    color: {FG};
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 9.5pt;
}}

QMainWindow, QDialog {{ background: {BG}; }}

QToolTip {{
    background: {CARD};
    color: {FG};
    border: 1px solid {BORDER};
    padding: 6px 8px;
}}

/* --------------------------------------------------------------- header */
#HeaderBar {{
    background: {BG_ALT};
    border-bottom: 1px solid {BORDER};
}}
#AppTitle {{
    font-size: 13pt;
    font-weight: 600;
    color: {FG};
}}
#StateText {{ font-size: 11pt; font-weight: 600; }}
#StateText[state="running"] {{ color: {SUCCESS}; }}
#StateText[state="stopped"] {{ color: {FG_DIM}; }}
#DetailText {{ color: {FG_MUTED}; font-size: 8.5pt; }}

/* ---------------------------------------------------------------- cards */
#Card {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
#CardTitle {{ color: {FG_DIM}; font-size: 8.5pt; }}
#CardValue {{
    font-size: 17pt;
    font-weight: 600;
    color: {FG};
}}
#CardValue[tone="accent"] {{ color: {ACCENT}; }}
#CardValue[tone="success"] {{ color: {SUCCESS}; }}
#CardValue[tone="think"] {{ color: {THINK}; }}
#CardValue[tone="danger"] {{ color: {DANGER}; }}
#CardSub {{ color: {FG_MUTED}; font-size: 8pt; }}

/* --------------------------------------------------------------- panels */
#Panel {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
#PanelTitle {{
    font-size: 10pt;
    font-weight: 600;
    color: {FG};
}}
#Hint {{ color: {FG_MUTED}; font-size: 8.5pt; }}
#HintWarn {{ color: {WARNING}; font-size: 8.5pt; }}

/* -------------------------------------------------------------- buttons */
QPushButton {{
    background: {ACCENT};
    color: {BG_ALT};
    border: none;
    border-radius: 7px;
    padding: 7px 15px;
    font-weight: 600;
}}
QPushButton:hover {{ background: {ACCENT_HOVER}; }}
QPushButton:pressed {{ background: {ACCENT}; }}
QPushButton:disabled {{
    background: {BORDER};
    color: {FG_MUTED};
}}

QPushButton[variant="secondary"] {{
    background: transparent;
    color: {FG};
    border: 1px solid {BORDER};
    font-weight: 500;
}}
QPushButton[variant="secondary"]:hover {{
    background: {CARD_HOVER};
    border-color: {ACCENT};
}}
QPushButton[variant="secondary"]:disabled {{
    color: {FG_MUTED};
    border-color: {BORDER_SOFT};
}}

QPushButton[variant="danger"] {{
    background: transparent;
    color: {DANGER};
    border: 1px solid {DANGER};
    font-weight: 500;
}}
QPushButton[variant="danger"]:hover {{ background: rgba(243,139,168,0.12); }}

QPushButton[variant="success"] {{ background: {SUCCESS}; color: {BG_ALT}; }}
QPushButton[variant="success"]:hover {{ background: #b6ebb2; }}

/* ---------------------------------------------------------------- inputs */
QLineEdit, QSpinBox, QPlainTextEdit, QTextEdit, QComboBox {{
    background: {BG_ALT};
    color: {FG};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 6px 9px;
    selection-background-color: {ACCENT};
    selection-color: {BG_ALT};
}}
QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus,
QTextEdit:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QLineEdit[readOnly="true"] {{ color: {FG_DIM}; }}
QLineEdit:disabled {{ color: {FG_MUTED}; }}

QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {CARD};
    color: {FG};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    selection-color: {BG_ALT};
    outline: none;
}}

QCheckBox, QRadioButton {{ spacing: 8px; color: {FG}; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {BORDER};
    background: {BG_ALT};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {ACCENT};
}}

/* ---------------------------------------------------------------- tables */
QTableWidget, QTableView {{
    background: {BG_ALT};
    alternate-background-color: {CARD};
    color: {FG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    gridline-color: {BORDER_SOFT};
    selection-background-color: {CARD_HOVER};
    selection-color: {FG};
    outline: none;
}}
QTableWidget::item, QTableView::item {{ padding: 5px 7px; }}
QTableWidget::item:selected, QTableView::item:selected {{
    background: {CARD_HOVER};
    color: {FG};
}}

QHeaderView {{ background: transparent; }}
QHeaderView::section {{
    background: {CARD};
    color: {FG_DIM};
    border: none;
    border-right: 1px solid {BORDER_SOFT};
    border-bottom: 1px solid {BORDER};
    padding: 7px 6px;
    font-weight: 600;
}}
QHeaderView::section:last {{ border-right: none; }}
QTableCornerButton::section {{ background: {CARD}; border: none; }}

/* --------------------------------------------------------------- tabs */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 10px;
    background: {BG};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {FG_DIM};
    padding: 9px 18px;
    margin-right: 4px;
    border: 1px solid transparent;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    font-weight: 500;
}}
QTabBar::tab:selected {{
    background: {CARD};
    color: {FG};
    border-color: {BORDER};
    border-bottom-color: {CARD};
}}
QTabBar::tab:hover:!selected {{ color: {FG}; background: {CARD_HOVER}; }}

/* ------------------------------------------------------------ scrollbar */
QScrollBar:vertical {{
    background: transparent; width: 11px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER}; border-radius: 5px; min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: {FG_MUTED}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}

QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {BORDER}; border-radius: 5px; min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{ background: {FG_MUTED}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: none; }}

/* ---------------------------------------------------------------- misc */
QStatusBar {{
    background: {BG_ALT};
    color: {FG_MUTED};
    border-top: 1px solid {BORDER};
}}
QStatusBar::item {{ border: none; }}

QSplitter::handle {{ background: {BORDER_SOFT}; }}

QPlainTextEdit#LogView {{
    background: {CODE_BG};
    color: {FG_DIM};
    font-family: "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace;
    font-size: 8.5pt;
    border: 1px solid {BORDER};
    border-radius: 8px;
}}

QMenu {{
    background: {CARD};
    color: {FG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 5px;
}}
QMenu::item {{ padding: 7px 22px 7px 14px; border-radius: 5px; }}
QMenu::item:selected {{ background: {CARD_HOVER}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 8px; }}

QListWidget {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    outline: none;
}}
QListWidget::item {{ padding: 8px 10px; border-radius: 6px; }}
QListWidget::item:selected {{ background: {CARD_HOVER}; color: {FG}; }}

QProgressBar {{
    background: {BG_ALT};
    border: none;
    border-radius: 4px;
    height: 6px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}

QMessageBox {{ background: {BG}; }}
QMessageBox QLabel {{ color: {FG}; }}
"""
