"""Central look of the operator GUI, ported from the former web dashboard (dark theme).

One QSS string built from PALETTE; widgets pick styles through object names and dynamic properties
(`QPushButton[primary="true"]`, `QLabel#badge[state="playing"]`, `QFrame#card[selected="true"]`) instead of inline colours.
"""

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QWidget

PALETTE = {
    "bg": "#111418", "surface": "#1a1f25", "surface2": "#232a32", "border": "#2f3842",
    "text": "#e6eaef", "muted": "#8a96a3", "accent": "#4f9dff", "accent_text": "#0b1420",
    "ok": "#3fbf7f", "warn": "#e0a83a", "bad": "#e5566a", "selected": "#264466",
    "ok_bg": "#1d3d33", "bad_bg": "#43242d", "warn_bg": "#43391f", "accent_bg": "#1d3350",
}
SEVERITY_COLORS = {"good": PALETTE["ok"], "meh": PALETTE["warn"], "poor": PALETTE["bad"]}
LEVEL_COLORS = {"ok": PALETTE["ok"], "info": PALETTE["accent"], "warn": PALETTE["warn"], "error": PALETTE["bad"],
                "pending": PALETTE["muted"]}
FONT_FAMILIES = ["Segoe UI", "SF Pro Text", "Helvetica Neue", "Roboto", "Noto Sans", "DejaVu Sans", "Arial"]
FONT_SIZE = 10

# state -> (text colour, pill background); anything else is shown like "paused"
_STATES = {"playing": ("ok", "ok_bg"), "paused": ("accent", "accent_bg"), "ready": ("accent", "accent_bg"),
           "syncing": ("warn", "warn_bg"), "loading": ("warn", "warn_bg"), "error": ("bad", "bad_bg"),
           "offline": ("muted", "surface2"), "connecting": ("muted", "surface2")}

_QSS = """
QMainWindow, QDialog, QMessageBox { background: %(bg)s; }
QWidget#tabPage, QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }
QLabel { background: transparent; }
QLabel[muted="true"], QLabel#muted { color: %(muted)s; }
QLabel#fieldHelp { color: %(muted)s; font-size: 9pt; padding-top: 2px; line-height: 150%%; }
QToolTip { background: %(surface2)s; color: %(text)s; border: 1px solid %(border)s; padding: 4px; }

QFrame#topbar { background: %(surface)s; border: 0; border-bottom: 1px solid %(border)s; }
QLabel#brand { font-size: 13pt; font-weight: 700; }
QLabel#serverName { color: %(muted)s; font-size: 11pt; }
QLabel#stats { color: %(muted)s; }
QLabel#conn { border-radius: 10px; padding: 3px 12px; font-size: 9pt; }
QLabel#conn[ok="true"] { background: %(ok_bg)s; color: %(ok)s; }
QLabel#conn[ok="false"] { background: %(bad_bg)s; color: %(bad)s; }

QMenuBar { background: %(surface)s; border-bottom: 1px solid %(border)s; }
QMenuBar::item { padding: 5px 10px; background: transparent; }
QMenuBar::item:selected { background: %(surface2)s; }
QMenu { background: %(surface)s; border: 1px solid %(border)s; padding: 4px; }
QMenu::item { padding: 6px 22px; border-radius: 4px; }
QMenu::item:selected { background: %(selected)s; }

QStatusBar { background: %(surface)s; border-top: 1px solid %(border)s; color: %(muted)s; }
QStatusBar::item { border: 0; }
QStatusBar QLabel { color: %(muted)s; padding: 0 8px; }

QTabWidget::pane { border: 0; border-top: 1px solid %(border)s; top: -1px; background: transparent; }
QTabBar { background: transparent; }
QTabBar::tab { background: transparent; color: %(muted)s; padding: 9px 16px; margin-right: 4px;
               border: 0; border-bottom: 2px solid transparent; }
QTabBar::tab:hover { color: %(text)s; }
QTabBar::tab:selected { color: %(text)s; border-bottom: 2px solid %(accent)s; }

QPushButton { background: %(surface2)s; border: 1px solid %(border)s; border-radius: 8px; padding: 7px 14px;
              min-height: 20px; }
QPushButton:hover { border-color: %(accent)s; }
QPushButton:pressed { background: %(surface)s; padding-top: 8px; }
QPushButton:disabled { color: %(muted)s; border-color: %(border)s; background: %(surface)s; }
QPushButton:default { border-color: %(accent)s; }
QPushButton[primary="true"] { background: %(accent)s; border-color: %(accent)s; color: %(accent_text)s;
                              font-weight: 600; }
QPushButton[primary="true"]:hover { background: #6aaeff; border-color: #6aaeff; }
QPushButton[primary="true"]:disabled { background: %(accent_bg)s; border-color: %(accent_bg)s; color: %(muted)s; }
QPushButton[danger="true"] { color: %(bad)s; }
QPushButton[danger="true"]:hover { border-color: %(bad)s; }
QPushButton[small="true"] { padding: 2px 10px; min-height: 16px; font-size: 9pt; }

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: %(surface2)s; border: 1px solid %(border)s;
    border-radius: 8px; padding: 5px 10px; min-height: 20px; selection-background-color: %(accent)s;
    selection-color: %(accent_text)s; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: %(accent)s; }
QComboBox::drop-down { border: 0; width: 24px; }
QComboBox QAbstractItemView { background: %(surface)s; border: 1px solid %(border)s; outline: 0;
    selection-background-color: %(selected)s; selection-color: %(text)s; }
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width: 16px;
    border: 0; background: transparent; }

QCheckBox { spacing: 8px; color: %(muted)s; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid %(border)s; border-radius: 4px;
                       background: %(surface2)s; }
QCheckBox::indicator:checked { background: %(accent)s; border-color: %(accent)s; }

QSlider::groove:horizontal { height: 4px; background: %(surface2)s; border-radius: 2px; }
QSlider::sub-page:horizontal { background: %(accent)s; border-radius: 2px; }
QSlider::handle:horizontal { background: %(accent)s; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }

QProgressBar { background: %(surface2)s; border: 0; border-radius: 2px; }
QProgressBar::chunk { background: %(accent)s; border-radius: 2px; }
QProgressBar[download="true"]::chunk { background: %(warn)s; }

QFrame#card { background: %(surface)s; border: 1px solid %(border)s; border-radius: 12px; }
QFrame#card:hover { border-color: #3e4a57; }
QFrame#card[selected="true"] { border: 1px solid %(accent)s; background: %(selected)s; }
QFrame#card[online="false"] QLabel { color: %(muted)s; }
QLabel#cardName { font-weight: 600; font-size: 11pt; }
QLabel#chip { background: %(surface2)s; color: %(muted)s; border-radius: 9px; padding: 1px 8px; font-size: 9pt; }
QLabel#badge { font-size: 8pt; font-weight: 700; border-radius: 8px; padding: 3px 10px;
               background: %(surface2)s; color: %(muted)s; }
%(badges)s
QLabel#error { color: %(bad)s; }
QLabel#mono { font-family: "DejaVu Sans Mono", Menlo, Consolas, monospace; }
QLabel[drift="good"] { color: %(ok)s; } QLabel[drift="meh"] { color: %(warn)s; } QLabel[drift="poor"] { color: %(bad)s; }

QGroupBox#playback { background: %(surface)s; border: 1px solid %(border)s; border-radius: 12px;
                     margin-top: 0; padding: 18px 12px 12px 12px; }
QGroupBox#playback::title { subcontrol-origin: padding; subcontrol-position: top left; left: 14px; top: 4px;
                            color: %(muted)s; font-size: 9pt; font-weight: 700; }
QLabel#target { font-weight: 600; }
QLabel#now { color: %(muted)s; }
QLabel#time { color: %(muted)s; }

QTableView { background: %(surface)s; alternate-background-color: %(surface)s; border: 1px solid %(border)s;
             border-radius: 10px; gridline-color: %(border)s; selection-background-color: %(selected)s;
             selection-color: %(text)s; outline: 0; }
QTableView::item { padding: 4px 8px; border-bottom: 1px solid %(border)s; }
QHeaderView { background: transparent; }
QHeaderView::section { background: %(surface)s; color: %(muted)s; border: 0; border-bottom: 1px solid %(border)s;
                       padding: 7px 8px; font-size: 8pt; font-weight: 600; }
QTableCornerButton::section { background: %(surface)s; border: 0; }

QListWidget { background: %(surface)s; border: 1px solid %(border)s; border-radius: 10px; outline: 0;
              font-family: "DejaVu Sans Mono", Menlo, Consolas, monospace; font-size: 9pt; padding: 4px; }
QListWidget::item { padding: 5px 6px; border-bottom: 1px solid %(border)s; }

QScrollBar:vertical { background: transparent; width: 12px; margin: 0; }
QScrollBar:horizontal { background: transparent; height: 12px; margin: 0; }
QScrollBar::handle { background: %(border)s; border-radius: 5px; min-height: 30px; min-width: 30px; margin: 2px; }
QScrollBar::handle:hover { background: %(muted)s; }
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page { background: none; border: 0;
    height: 0; width: 0; }
"""


def _badge_rules() -> str:
    rules = []
    for state, (fg, bg) in _STATES.items():
        rules.append('QLabel#badge[state="%s"] { background: %s; color: %s; }' % (state, PALETTE[bg], PALETTE[fg]))
    return "\n".join(rules)


def build_stylesheet(palette: dict = None) -> str:
    values = dict(PALETTE if palette is None else palette)
    values["badges"] = _badge_rules()
    return _QSS % values


def state_style(state: str) -> str:
    """Badge property value: known states keep their own colour, anything else looks like 'paused'."""
    return state if state in _STATES else "paused"


def set_property(widget: QWidget, name: str, value) -> None:
    """Set a dynamic property and re-evaluate the stylesheet for the widget, only when it changed."""
    value = value if isinstance(value, str) else str(bool(value)).lower()
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def mark(widget: QWidget, **props) -> QWidget:
    """Set several dynamic properties before the widget is shown (no re-polish needed)."""
    for name, value in props.items():
        widget.setProperty(name, value if isinstance(value, str) else str(bool(value)).lower())
    return widget


def apply_theme(app: QApplication) -> None:
    """Fusion base style, dark palette, clean font and the central stylesheet."""
    app.setStyle("Fusion")
    p = PALETTE
    pal = QPalette()
    for role, key in ((QPalette.Window, "bg"), (QPalette.Base, "surface"), (QPalette.AlternateBase, "surface"),
                      (QPalette.Button, "surface2"), (QPalette.ToolTipBase, "surface2"),
                      (QPalette.Highlight, "accent"), (QPalette.WindowText, "text"), (QPalette.Text, "text"),
                      (QPalette.ButtonText, "text"), (QPalette.ToolTipText, "text"),
                      (QPalette.HighlightedText, "accent_text"), (QPalette.PlaceholderText, "muted")):
        pal.setColor(role, QColor(p[key]))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(p["muted"]))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(p["muted"]))
    app.setPalette(pal)
    font = QFont()
    font.setFamilies(FONT_FAMILIES)
    font.setPointSize(FONT_SIZE)
    app.setFont(font)
    app.setStyleSheet(build_stylesheet())
