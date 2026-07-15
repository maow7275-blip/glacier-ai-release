import sys
import os
import json
import base64
import math
from string import Template
import requests
from datetime import datetime
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QComboBox, QPushButton, QFileDialog,
    QScrollArea, QMessageBox, QDialog, QLineEdit, QFrame,
    QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QSizePolicy, QStackedWidget,
    QGridLayout, QSpacerItem, QMenu, QAction, QStyledItemDelegate, QStyle, QListView,
    QSlider, QListWidget, QCheckBox
)
import re
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSize, QPoint, QRect, QTimer, QMimeData, QEvent, QObject
from PyQt5.QtGui import QPixmap, QImage, QFont, QColor, QIcon, QPainter, QLinearGradient, QBrush, QPen, QPainterPath


class MentionPopup(QListWidget):
    """自定义弹窗，支持点击外部关闭"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setFocusPolicy(Qt.NoFocus)
        self.setStyleSheet(
            "QListWidget { background:#141c2e; border:1px solid rgba(125,211,252,0.3);"
            " color:#e0e8f0; padding:4px; font-size:13px; }"
            "QListWidget::item { padding:6px 12px; border-radius:4px; }"
            "QListWidget::item:selected { background:rgba(125,211,252,0.2); color:#bae6fd; }"
        )
        # 安装应用程序级别的事件过滤器
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, event):
        """过滤全局鼠标点击事件，点击弹窗外部时关闭"""
        if event.type() == QEvent.MouseButtonPress and self.isVisible():
            # 获取点击的全局坐标
            click_pos = event.globalPos()
            # 检查点击位置是否在弹窗区域内
            if not self.geometry().contains(self.mapFromGlobal(click_pos)):
                self.hide()
                return False
        return super().eventFilter(obj, event)

    def hideEvent(self, event):
        """弹窗隐藏时移除事件过滤器"""
        super().hideEvent(event)


class PlainPasteTextEdit(QTextEdit):
    """与 QTextEdit 一致，但粘贴时丢弃富文本格式，统一用当前编辑器字体显示。
    避免从浏览器/Word 复制带格式的文字粘进来后字体跟手动输入不一致。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._mention_provider = None
        self._mention_popup = None
        self._mention_anchor = -1

    def insertFromMimeData(self, source):
        if source.hasText():
            md = QMimeData()
            md.setText(source.text())
            super().insertFromMimeData(md)
        else:
            super().insertFromMimeData(source)

    def set_mention_provider(self, provider):
        self._mention_provider = provider

    def _ensure_mention_popup(self):
        if self._mention_popup is None:
            popup = MentionPopup()
            popup.itemClicked.connect(lambda it: self._insert_mention(it.text()))
            self._mention_popup = popup
        return self._mention_popup

    def _refresh_mention_popup(self):
        if not self._mention_provider:
            self._hide_mention_popup()
            return
        candidates = self._mention_provider() or []
        if not candidates:
            self._hide_mention_popup()
            return
        cursor = self.textCursor()
        typed = ""
        if self._mention_anchor >= 0 and cursor.position() >= self._mention_anchor:
            typed = self.toPlainText()[self._mention_anchor:cursor.position()]
        if typed:
            lowered = typed.lower()
            filtered = [c for c in candidates if lowered in c.lower()]
        else:
            filtered = list(candidates)
        if not filtered:
            self._hide_mention_popup()
            return
        popup = self._ensure_mention_popup()
        popup.clear()
        for c in filtered:
            popup.addItem(c)
        popup.setCurrentRow(0)
        rect = self.cursorRect()
        global_pos = self.mapToGlobal(rect.bottomLeft())
        popup.move(global_pos)
        popup.resize(220, min(len(filtered) * 32 + 12, 220))
        popup.show()

    def _hide_mention_popup(self):
        if self._mention_popup:
            self._mention_popup.hide()
        self._mention_anchor = -1

    def _insert_mention(self, text):
        if self._mention_anchor < 0:
            self._hide_mention_popup()
            return
        cursor = self.textCursor()
        cursor.setPosition(self._mention_anchor - 1)
        cursor.setPosition(self.textCursor().position(), cursor.KeepAnchor)
        cursor.removeSelectedText()
        cursor.insertText("@" + text)
        self._hide_mention_popup()
        self.setFocus()

    def keyPressEvent(self, event):
        popup_visible = self._mention_popup is not None and self._mention_popup.isVisible()
        if popup_visible:
            key = event.key()
            if key in (Qt.Key_Up, Qt.Key_Down):
                self._mention_popup.event(event)
                return
            if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
                item = self._mention_popup.currentItem()
                if item:
                    self._insert_mention(item.text())
                    return
            if key == Qt.Key_Escape:
                self._hide_mention_popup()
                return
        super().keyPressEvent(event)
        if event.text() == "@":
            self._mention_anchor = self.textCursor().position()
            self._refresh_mention_popup()
            return
        if self._mention_anchor >= 0:
            cursor = self.textCursor()
            doc = self.toPlainText()
            if (cursor.position() < self._mention_anchor or
                    self._mention_anchor - 1 < 0 or
                    self._mention_anchor - 1 >= len(doc) or
                    doc[self._mention_anchor - 1] != "@"):
                self._hide_mention_popup()
                return
            self._refresh_mention_popup()


API_URL = "https://www.hfsyapi.cn/v1/images/generations"
API_EDIT_URL = "https://www.hfsyapi.cn/v1/images/edits"
VIDEO_API_URL = "https://www.hfsyapi.cn/v1/video/create"
GEMINI_API_URL_TEMPLATE = "https://www.hfsyapi.cn/v1beta/models/{model}:generateContent"
FILE_UPLOAD_URL = "https://www.hfsyapi.cn/v1/files/image-upload"
BALANCE_URL = "https://www.hfsyapi.cn/api/usage/token/fund"

NO_PROXIES = {"http": None, "https": None}

RATIO_LIST = ["1:1", "5:4", "9:16", "16:9", "4:3", "3:2", "4:5", "3:4", "2:3", "21:9"]

GEMINI_RATIO_LIST = [
    "1:1", "2:1", "1:2", "3:2", "2:3", "4:3", "3:4",
    "5:4", "4:5", "16:9", "9:16", "21:9", "9:21",
]

SIZE_MAP = {
    "1:1":  {"1k": "1024x1024", "2k": "2048x2048", "4k": "2880x2880"},
    "5:4":  {"1k": "1040x832",  "2k": "2080x1664", "4k": "3200x2560"},
    "9:16": {"1k": "720x1280",  "2k": "1152x2048", "4k": "2160x3840"},
    "16:9": {"1k": "1280x720",  "2k": "2048x1152", "4k": "3840x2160"},
    "4:3":  {"1k": "1024x768",  "2k": "2048x1536", "4k": "3264x2448"},
    "3:2":  {"1k": "1008x672",  "2k": "2016x1344", "4k": "3504x2336"},
    "4:5":  {"1k": "832x1040",  "2k": "1664x2080", "4k": "2560x3200"},
    "3:4":  {"1k": "768x1024",  "2k": "1536x2048", "4k": "2448x3264"},
    "2:3":  {"1k": "672x1008",  "2k": "1344x2016", "4k": "2336x3504"},
    "21:9": {"1k": "1344x576",  "2k": "2016x864",  "4k": "3696x1584"},
}

GEMINI_SIZE_MAP = {
    "1:1":  {"1k": "1024x1024", "2k": "2048x2048", "4k": "4096x4096"},
    "2:1":  {"1k": "1408x704",  "2k": "2816x1408", "4k": "5632x2816"},
    "1:2":  {"1k": "704x1408",  "2k": "1408x2816", "4k": "2816x5632"},
    "3:2":  {"1k": "1248x832",  "2k": "2496x1664", "4k": "4992x3328"},
    "2:3":  {"1k": "832x1248",  "2k": "1664x2496", "4k": "3328x4992"},
    "4:3":  {"1k": "1024x768",  "2k": "2048x1536", "4k": "4096x3072"},
    "3:4":  {"1k": "768x1024",  "2k": "1536x2048", "4k": "3072x4096"},
    "5:4":  {"1k": "1120x896",  "2k": "2240x1792", "4k": "4480x3584"},
    "4:5":  {"1k": "896x1120",  "2k": "1792x2240", "4k": "3584x4480"},
    "16:9": {"1k": "1024x576",  "2k": "2048x1152", "4k": "4096x2304"},
    "9:16": {"1k": "576x1024",  "2k": "1152x2048", "4k": "2304x4096"},
    "21:9": {"1k": "1344x576",  "2k": "2688x1152", "4k": "5376x2304"},
    "9:21": {"1k": "576x1344",  "2k": "1152x2688", "4k": "2304x5376"},
}

MODEL_QUALITY = {
    "gpt-image-2": ["1k"],
    "gpt-image-2pro": ["2k", "4k"],
    "nano-banana-2": ["1k", "2k", "4k"],
    "nano-banana-pro": ["1k", "2k", "4k"],
}

GEMINI_MODELS = {"nano-banana-2", "nano-banana-pro"}
GEMINI_MAX_REFERENCE_IMAGES = 7

IMAGE_MODEL_MAX_REFS = {
    "gpt-image-2": 6,
    "gpt-image-2pro": 4,
    "nano-banana-2": 7,
    "nano-banana-pro": 7,
}

QUALITY_TO_API = {"1k": "low", "2k": "medium", "4k": "high"}

VIDEO_SIZES = {"横屏": "landscape", "竖屏": "portrait"}
VIDEO_DURATIONS = ["4", "8", "12"]


def extract_video_first_frame_bytes(video_bytes_or_path):
    """从视频字节或文件路径抽首帧，返回 PNG bytes，失败返回 None。"""
    try:
        import cv2
        import numpy as np
        import tempfile
    except Exception:
        return None
    tmp_path = None
    try:
        if isinstance(video_bytes_or_path, (bytes, bytearray)):
            tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
            tmp.write(video_bytes_or_path)
            tmp.close()
            tmp_path = tmp.name
            path = tmp_path
        else:
            path = video_bytes_or_path
        cap = cv2.VideoCapture(path)
        try:
            ok, frame = cap.read()
            if not ok or frame is None:
                return None
            ok2, buf = cv2.imencode(".png", frame)
            if not ok2:
                return None
            return bytes(buf)
        finally:
            cap.release()
    except Exception:
        return None
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def get_or_make_thumb_for_video(video_path):
    """对历史记录中的视频文件返回首帧 PNG 路径，缓存到同目录 .thumb.png。"""
    if not os.path.exists(video_path):
        return None
    thumb_path = video_path + ".thumb.png"
    fail_marker = video_path + ".thumb.fail"
    if os.path.exists(thumb_path) and os.path.getmtime(thumb_path) >= os.path.getmtime(video_path):
        return thumb_path
    # 之前已经抽帧失败过（可能 cv2 在此机器/此视频上有问题），跳过避免再次崩溃
    if os.path.exists(fail_marker):
        return None
    # 提前写失败标记：如果 cv2 native 层 segfault，下次进历史页至少不会再次崩
    try:
        with open(fail_marker, "wb") as f:
            f.write(b"")
    except Exception:
        pass
    png_bytes = extract_video_first_frame_bytes(video_path)
    if not png_bytes:
        return None
    try:
        with open(thumb_path, "wb") as f:
            f.write(png_bytes)
        try:
            os.remove(fail_marker)
        except Exception:
            pass
        return thumb_path
    except Exception:
        return None

VIDEO_MODELS = ["sora-2", "sd-2", "sd-2-fast", "sd-2-vip", "sd-2-vip-720", "kling-o3"]
VIDEO_MODEL_DURATIONS = {
    "sora-2": ["4", "8", "12"],
    "sd-2": [str(i) for i in range(5, 11)],
    "sd-2-fast": [str(i) for i in range(5, 11)],
    "sd-2-vip": [str(i) for i in range(5, 16)],
    "sd-2-vip-720": [str(i) for i in range(5, 16)],
    "kling-o3": [str(i) for i in range(5, 16)],
}
VIDEO_PROMPT_LIMIT = {"sd-2": 5000, "sd-2-fast": 5000, "sd-2-vip": 10000, "sd-2-vip-720": 10000, "kling-o3": 5000}
VIDEO_MODEL_ORIENTATIONS = {
    "sora-2": {"横屏": "landscape", "竖屏": "portrait"},
    "sd-2": {"横屏": "landscape", "竖屏": "portrait"},
    "sd-2-fast": {"横屏": "landscape", "竖屏": "portrait"},
    "sd-2-vip": {"横屏": "landscape", "竖屏": "portrait"},
    "sd-2-vip-720": {"横屏": "landscape", "竖屏": "portrait"},
    "kling-o3": {"横屏": "landscape", "竖屏": "portrait", "方屏": "square"},
}
VIDEO_MODEL_RATIOS = {
    "sd-2": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
    "sd-2-fast": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
    "sd-2-vip": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
    "sd-2-vip-720": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
}
VIDEO_MODEL_MAX_IMAGES_BASE = {"sora-2": 1, "sd-2": 9, "sd-2-fast": 9, "sd-2-vip": 9, "sd-2-vip-720": 9, "kling-o3": 8}
VIDEO_MODEL_MAX_IMAGES_WITH_VIDEO = {"sora-2": 1, "sd-2": 9, "sd-2-fast": 9, "sd-2-vip": 9, "sd-2-vip-720": 9, "kling-o3": 8}
VIDEO_MODEL_MAX_VIDEOS = {"sora-2": 0, "sd-2": 3, "sd-2-fast": 3, "sd-2-vip": 3, "sd-2-vip-720": 3, "kling-o3": 0}
VIDEO_MODEL_MAX_AUDIOS = {"sora-2": 0, "sd-2": 3, "sd-2-fast": 3, "sd-2-vip": 3, "sd-2-vip-720": 3, "kling-o3": 0}


# 注意：下拉项的灰色高光改用纯 QSS 实现（见 DARK_STYLE 中
# QComboBox QAbstractItemView::item:selected/hover），不再用自定义 delegate。
# 之前 delegate 在 QSS item:selected 样式优先级下不生效，改 QSS 直接控制更稳。
# 给所有 QComboBox 强制使用 QListView 作为下拉视图（默认是 native popup，
# 部分平台 ::item:selected/hover 不会生效）。

_orig_combo_init = QComboBox.__init__

def _patched_combo_init(self, *args, **kwargs):
    _orig_combo_init(self, *args, **kwargs)
    try:
        view = QListView(self)
        view.setMouseTracking(True)
        view.setSelectionMode(QListView.SingleSelection)
        self.setView(view)
        try:
            container = view.window()
            if container is not None:
                container.setContentsMargins(0, 0, 0, 0)
        except Exception:
            pass
    except Exception:
        pass

QComboBox.__init__ = _patched_combo_init


# ============================================================
# 主题系统（P1）
# 三套主题：暗夜蓝（dark_blue）、简洁明亮（light）、爱马仕橙（hermes）
# 每个 token 都必须三套都填，模板里通过 {key} 引用
# ============================================================
THEMES = {
    "dark_blue": {
        "name": "暗夜蓝",
        "is_dark": True,
        "bg_main": "#0a0e1a",
        "bg_titlebar": "rgba(2, 6, 20, 230)",
        "bg_sidebar": "rgba(5, 10, 25, 230)",
        "bg_card": "rgba(15, 21, 36, 153)",
        "bg_card_solid": "#0f1524",
        "bg_input": "rgba(15, 23, 42, 0.6)",
        "bg_input_strong": "rgba(15, 23, 42, 0.4)",
        "bg_combo_pop": "#000000",
        "bg_combo_pop_item_sel": "#4a4a4a",
        "border_soft": "rgba(125, 211, 252, 0.1)",
        "border_focus": "rgba(125, 211, 252, 0.4)",
        "border_top_alpha": "rgba(255, 255, 255, 0.1)",
        "text_primary": "#e0e8f0",
        "text_input": "#a0b4c4",
        "text_secondary": "#94a3b8",
        "text_muted": "#64748b",
        "text_dim": "#475569",
        "accent": "#7dd3fc",
        "accent_strong": "#bae6fd",
        "accent_soft": "rgba(125, 211, 252, 0.15)",
        "accent_softer": "rgba(125, 211, 252, 0.1)",
        "accent_hover": "rgba(125, 211, 252, 0.25)",
        "accent_press": "rgba(125, 211, 252, 0.35)",
        "accent_border": "rgba(125, 211, 252, 0.3)",
        "accent_selection": "rgba(125, 211, 252, 0.3)",
        "scrollbar_track": "rgba(15, 21, 36, 0.3)",
        "scrollbar_thumb": "rgba(125, 211, 252, 0.2)",
        "scrollbar_thumb_hover": "rgba(125, 211, 252, 0.35)",
        "preview_dash": "rgba(125, 211, 252, 0.2)",
        "btn_disabled_bg": "rgba(30, 41, 59, 0.5)",
        "btn_disabled_border": "rgba(100, 116, 139, 0.2)",
        "btn_disabled_text": "#475569",
        "save_disabled_text": "#334155",
        "section_badge_bg": "rgba(30, 41, 59, 0.5)",
        "down_arrow_color": "#64748b",
        "footer_bg": "rgba(15, 21, 36, 0.6)",
        "footer_brand": "rgba(125, 211, 252, 0.4)",
        "version_color": "#ff9f1c",
    },
    "light": {
        "name": "简洁明亮",
        "is_dark": False,
        "bg_main": "#faf8ff",
        "bg_titlebar": "rgba(255, 255, 255, 240)",
        "bg_sidebar": "#ffffff",
        "bg_card": "#ffffff",
        "bg_card_solid": "#ffffff",
        "bg_input": "#f2f3ff",
        "bg_input_strong": "#f2f3ff",
        "bg_combo_pop": "#ffffff",
        "bg_combo_pop_item_sel": "#dce1ff",
        "border_soft": "#c3c5d9",
        "border_focus": "#0043c8",
        "border_top_alpha": "rgba(19, 27, 46, 0.08)",
        "text_primary": "#131b2e",
        "text_input": "#131b2e",
        "text_secondary": "#434656",
        "text_muted": "#737688",
        "text_dim": "#9aa0b4",
        "accent": "#0043c8",
        "accent_strong": "#0057ff",
        "accent_soft": "#dce1ff",
        "accent_softer": "#eaedff",
        "accent_hover": "#c7d1f8",
        "accent_press": "#b0bef5",
        "accent_border": "#0043c8",
        "accent_selection": "#dce1ff",
        "scrollbar_track": "rgba(19, 27, 46, 0.04)",
        "scrollbar_thumb": "rgba(0, 67, 200, 0.25)",
        "scrollbar_thumb_hover": "rgba(0, 67, 200, 0.45)",
        "preview_dash": "#c3c5d9",
        "btn_disabled_bg": "#e7e9f3",
        "btn_disabled_border": "#c3c5d9",
        "btn_disabled_text": "#9aa0b4",
        "save_disabled_text": "#b0b6c8",
        "section_badge_bg": "#eaedff",
        "down_arrow_color": "#737688",
        "footer_bg": "#f2f3ff",
        "footer_brand": "rgba(0, 67, 200, 0.55)",
        "version_color": "#0043c8",
    },
    "hermes": {
        "name": "爱马仕橙",
        "is_dark": False,
        "bg_main": "#fbf9f9",
        "bg_titlebar": "rgba(255, 255, 255, 240)",
        "bg_sidebar": "#ffffff",
        "bg_card": "#ffffff",
        "bg_card_solid": "#ffffff",
        "bg_input": "#f5f3f3",
        "bg_input_strong": "#fbf9f9",
        "bg_combo_pop": "#ffffff",
        "bg_combo_pop_item_sel": "#ffdbc9",
        "border_soft": "rgba(28, 28, 28, 0.10)",
        "border_focus": "#ff7700",
        "border_top_alpha": "rgba(28, 28, 28, 0.08)",
        "text_primary": "#1b1c1c",
        "text_input": "#1b1c1c",
        "text_secondary": "#5f5e5e",
        "text_muted": "#8c7163",
        "text_dim": "#a19f9a",
        "accent": "#ff7700",
        "accent_strong": "#9b4600",
        "accent_soft": "#ffdbc9",
        "accent_softer": "#fff1e8",
        "accent_hover": "#ffc6a8",
        "accent_press": "#ffb68d",
        "accent_border": "#ff7700",
        "accent_selection": "#ffdbc9",
        "scrollbar_track": "rgba(28, 28, 28, 0.05)",
        "scrollbar_thumb": "rgba(255, 119, 0, 0.45)",
        "scrollbar_thumb_hover": "#ff7700",
        "preview_dash": "#e0c0b0",
        "btn_disabled_bg": "#efeded",
        "btn_disabled_border": "#dbdad9",
        "btn_disabled_text": "#a19f9a",
        "save_disabled_text": "#c9c6c1",
        "section_badge_bg": "#efeded",
        "down_arrow_color": "#8c7163",
        "footer_bg": "#f5f3f3",
        "footer_brand": "rgba(155, 70, 0, 0.65)",
        "version_color": "#9b4600",
    },
}

DEFAULT_THEME = "light"


def get_theme(name):
    return THEMES.get(name, THEMES[DEFAULT_THEME])


def build_main_style(theme_name):
    t = get_theme(theme_name)
    return Template(MAIN_STYLE_TEMPLATE).safe_substitute(t)


def build_login_style(theme_name):
    t = get_theme(theme_name)
    return Template(LOGIN_STYLE_TEMPLATE).safe_substitute(t)


def build_settings_dialog_style(theme_name):
    t = get_theme(theme_name)
    return Template(SETTINGS_DIALOG_STYLE_TEMPLATE).safe_substitute(t)


MAIN_STYLE_TEMPLATE = """
QWidget {
    font-family: "Segoe UI", "Microsoft YaHei";
    font-size: 14px;
    color: $text_primary;
}
QFrame#titleBar {
    background-color: $bg_titlebar;
    border-bottom: 1px solid $border_soft;
}
QLabel#titleBrand {
    color: $accent;
    font-size: 18px;
    font-weight: 700;
    font-style: italic;
}
QLabel#titleSub {
    color: $text_muted;
    font-size: 13px;
}
QPushButton#winBtn {
    background: transparent;
    border: none;
    color: $accent;
    font-size: 16px;
    padding: 6px 12px;
}
QPushButton#winBtn:hover {
    background-color: $accent_softer;
}
QPushButton#winBtnClose {
    background: transparent;
    border: none;
    color: $accent;
    font-size: 16px;
    padding: 6px 12px;
}
QPushButton#winBtnClose:hover {
    background-color: rgba(255, 107, 107, 0.2);
    color: #ff6b6b;
}
QFrame#sideNav {
    background-color: $bg_sidebar;
    border-right: 1px solid $border_top_alpha;
}
QLabel#navTitle {
    color: $accent;
    font-size: 12px;
    font-weight: 800;
    letter-spacing: 3px;
}
QLabel#navVersion {
    color: $text_muted;
    font-size: 11px;
    font-family: "Consolas", "Courier New";
}
QPushButton#navBtn {
    background: transparent;
    border: none;
    border-right: 2px solid transparent;
    color: $text_secondary;
    font-size: 14px;
    font-weight: 500;
    text-align: left;
    padding: 12px 18px;
}
QPushButton#navBtn:hover {
    background-color: $accent_softer;
    color: $text_primary;
}
QPushButton#navBtnActive {
    background-color: $accent_soft;
    border: none;
    border-right: 2px solid $accent;
    color: $accent_strong;
    font-size: 14px;
    font-weight: 500;
    text-align: left;
    padding: 12px 18px;
}
QLabel#navKeyLabel {
    color: $text_muted;
    font-size: 12px;
    font-family: "Consolas", "Courier New";
}
QFrame#glassPanel {
    background-color: $bg_card;
    border: 1px solid $border_soft;
    border-radius: 12px;
}
QLabel#sectionLabel {
    color: $text_primary;
    font-size: 15px;
    font-weight: 600;
}
QLabel#sectionBadge {
    color: $text_muted;
    font-size: 11px;
    font-family: "Consolas", "Courier New";
    background-color: $btn_disabled_bg;
    padding: 2px 6px;
    border-radius: 3px;
}
QLabel#paramLabel {
    color: $text_secondary;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 2px;
}
QTextEdit#promptInput {
    background-color: $bg_input_strong;
    border: 1px solid $border_soft;
    border-radius: 8px;
    padding: 12px;
    font-size: 14px;
    color: $text_input;
    selection-background-color: $accent_border;
}
QTextEdit#promptInput:focus {
    border: 1px solid $border_focus;
}
QComboBox {
    background-color: $bg_input;
    border: 1px solid $border_soft;
    border-radius: 8px;
    padding: 10px 14px;
    font-family: "Inter", "Source Han Sans SC", "思源黑体", "Microsoft YaHei";
    font-size: 15px;
    color: $text_input;
    min-width: 100px;
    min-height: 22px;
}
QComboBox:hover {
    border-color: $accent_border;
}
QComboBox:focus {
    border-color: $border_focus;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid $text_muted;
    margin-right: 8px;
}
QComboBox QAbstractItemView {
    background-color: $bg_combo_pop;
    border: 1px solid $border_soft;
    border-radius: 10px;
    selection-background-color: $bg_combo_pop_item_sel;
    selection-color: $text_primary;
    color: $text_input;
    padding: 6px;
    outline: none;
    font-family: "Inter", "Source Han Sans SC", "思源黑体", "Microsoft YaHei";
    font-size: 14px;
}
QComboBox QListView {
    background-color: $bg_combo_pop;
    border: none;
    outline: none;
}
QComboBox QFrame {
    border: none;
    background-color: $bg_combo_pop;
}
QComboBox QAbstractItemView::item {
    background-color: $bg_combo_pop;
    color: $text_input;
    padding: 8px 14px;
    min-height: 28px;
    border: none;
}
QComboBox QAbstractItemView::item:selected {
    background-color: $bg_combo_pop_item_sel;
    color: $text_primary;
}
QComboBox QAbstractItemView::item:hover {
    background-color: $bg_combo_pop_item_sel;
    color: $text_primary;
}
QPushButton#generateBtn {
    background-color: $accent_soft;
    border: 1px solid $accent_border;
    border-radius: 12px;
    color: $accent;
    font-size: 18px;
    font-weight: 800;
    padding: 16px;
}
QPushButton#generateBtn:hover {
    background-color: $accent_hover;
}
QPushButton#generateBtn:pressed {
    background-color: $accent_press;
}
QPushButton#generateBtn:disabled {
    background-color: $btn_disabled_bg;
    border-color: $btn_disabled_border;
    color: $text_dim;
}
QPushButton#saveBtn {
    background-color: $border_soft;
    border: 1px solid $scrollbar_thumb;
    border-radius: 8px;
    color: $accent;
    font-size: 15px;
    font-weight: 600;
    padding: 10px 24px;
}
QPushButton#saveBtn:hover {
    background-color: $scrollbar_thumb;
}
QPushButton#saveBtn:disabled {
    background-color: $btn_disabled_bg;
    border-color: $btn_disabled_border;
    color: $save_disabled_text;
}
QFrame#previewArea {
    background-color: $bg_card;
    border: 2px dashed $scrollbar_thumb;
    border-radius: 16px;
}
QLabel#previewPlaceholder {
    color: $text_dim;
    font-size: 15px;
}
QLabel#previewTitle {
    color: $text_primary;
    font-size: 18px;
    font-weight: 700;
}
QLabel#previewDesc {
    color: $text_secondary;
    font-size: 14px;
}
QFrame#footerBar {
    background-color: $footer_bg;
    border-top: 1px solid $border_soft;
}
QLabel#footerText {
    color: $text_muted;
    font-size: 11px;
    font-family: "Consolas", "Courier New";
}
QLabel#footerBrand {
    color: $border_focus;
    font-size: 11px;
    font-weight: 700;
    font-family: "Consolas", "Courier New";
}
QScrollArea {
    border: none;
    background: transparent;
}
QScrollBar:vertical {
    background: $scrollbar_track;
    width: 6px;
    border-radius: 3px;
}
QScrollBar::handle:vertical {
    background: $scrollbar_thumb;
    border-radius: 3px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: $accent_press;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar:horizontal {
    height: 0px;
}
QLineEdit#refUrlInput {
    background-color: $bg_input;
    border: 1px solid $border_soft;
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 14px;
    color: $text_input;
}
QLineEdit#refUrlInput:focus {
    border: 1px solid $border_focus;
}
QPushButton#refBtn {
    background-color: $border_soft;
    border: 1px solid $accent_soft;
    border-radius: 8px;
    color: $accent;
    font-size: 14px;
    font-weight: 600;
    padding: 9px 18px;
}
QPushButton#refBtn:hover {
    background-color: $scrollbar_thumb;
}
QLabel#thumbLabel {
    border: 2px solid transparent;
    border-radius: 8px;
    padding: 4px;
    background-color: $bg_input_strong;
}
QLabel#thumbLabel:hover {
    border-color: $accent_border;
    background-color: $accent_softer;
}
QFrame#videoThumb {
    border: 2px solid transparent;
    border-radius: 8px;
    padding: 8px;
    background-color: $bg_input_strong;
}
QFrame#videoThumb:hover {
    border-color: $accent_border;
    background-color: $accent_softer;
}
"""

LOGIN_STYLE_TEMPLATE = """
QDialog {
    background-color: $bg_main;
}
QFrame#loginCard {
    background-color: $bg_card;
    border: 1px solid $border_soft;
    border-radius: 16px;
}
QLabel#loginTitle {
    color: $accent;
    font-size: 24px;
    font-weight: 700;
    font-style: italic;
}
QLabel#loginSubtitle {
    color: $text_muted;
    font-size: 14px;
}
QLabel#loginVersion {
    color: $footer_brand;
    font-size: 12px;
    font-weight: 700;
    font-family: "Consolas", "Courier New";
    letter-spacing: 1px;
}
QLabel#loginHint {
    color: $text_secondary;
    font-size: 14px;
}
QLineEdit#keyInput {
    background-color: $bg_input;
    border: 1px solid $border_soft;
    border-radius: 8px;
    padding: 11px 14px;
    font-size: 15px;
    color: $text_input;
}
QLineEdit#keyInput:focus {
    border: 1px solid $border_focus;
}
QPushButton#loginBtn {
    background-color: $accent_soft;
    border: 1px solid $accent_border;
    color: $accent;
    border-radius: 8px;
    font-size: 16px;
    font-weight: 700;
    padding: 13px;
}
QPushButton#loginBtn:hover {
    background-color: $accent_hover;
}
QPushButton#loginBtn:pressed {
    background-color: $accent_press;
}
QPushButton#toggleBtn {
    border: none;
    color: $accent;
    font-size: 14px;
    background: transparent;
}
QPushButton#tutorialBtn {
    border: none;
    color: $text_muted;
    font-size: 14px;
    background: transparent;
}
QPushButton#tutorialBtn:hover {
    color: $accent;
}
QCheckBox#rememberChk {
    color: $text_secondary;
    font-size: 13px;
    spacing: 8px;
}
QCheckBox#rememberChk::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid $accent_border;
    border-radius: 3px;
    background-color: $bg_input;
}
QCheckBox#rememberChk::indicator:checked {
    background-color: $accent;
    border: 1px solid $accent;
}
"""


def get_logo_path():
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, 'logo.png')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logo.png')


def setup_combo(combo):
    combo.setMaxVisibleItems(20)
    combo.view().setMouseTracking(True)
    combo.wheelEvent = lambda e: e.ignore()



class TutorialDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("如何获取 API Key")
        self.setFixedSize(520, 520)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setStyleSheet("""
            QDialog { background-color: #0a0e1a; }
            QLabel { color: #e0e8f0; }
            QPushButton#closeBtn {
                background-color: rgba(125, 211, 252, 0.15);
                border: 1px solid rgba(125, 211, 252, 0.3);
                color: #7dd3fc; border-radius: 8px;
                font-size: 13px; font-weight: 600; padding: 10px 28px;
            }
            QPushButton#closeBtn:hover { background-color: rgba(125, 211, 252, 0.25); }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(16)

        title = QLabel("如何获取 API Key")
        title.setStyleSheet("color: #7dd3fc; font-size: 18px; font-weight: 700;")
        layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        content = QLabel()
        content.setWordWrap(True)
        content.setTextFormat(Qt.RichText)
        content.setAlignment(Qt.AlignTop)
        content.setStyleSheet("background: transparent; font-size: 13px; line-height: 1.6; padding: 4px;")
        content.setOpenExternalLinks(True)
        content.setText(
            '<p style="color:#94a3b8; margin-bottom:12px;">按照以下步骤获取您的 API Key：</p>'
            '<p style="color:#7dd3fc; font-weight:600; margin-bottom:4px;">第一步：注册账号</p>'
            '<p style="color:#a0b4c4; margin-bottom:12px;">'
            '访问 API 平台官网：<a href="https://www.hfsyapi.cn/" style="color:#7dd3fc;">https://www.hfsyapi.cn/</a><br>'
            '点击"注册"按钮创建账号，使用邮箱或手机号完成注册流程。</p>'
            '<p style="color:#7dd3fc; font-weight:600; margin-bottom:4px;">第二步：登录后台</p>'
            '<p style="color:#a0b4c4; margin-bottom:12px;">'
            '注册完成后登录 <a href="https://www.hfsyapi.cn/" style="color:#7dd3fc;">https://www.hfsyapi.cn/</a> 管理后台。<br>'
            '在左侧菜单中找到"API 管理"或"令牌管理"入口。</p>'
            '<p style="color:#7dd3fc; font-weight:600; margin-bottom:4px;">第三步：创建 API Key</p>'
            '<p style="color:#a0b4c4; margin-bottom:12px;">'
            '点击"创建新令牌"或"生成 API Key"按钮。<br>'
            '为令牌设置一个名称（如 "Glacier AI"）。<br>'
            '点击确认后，系统会生成一个以 <b style="color:#7dd3fc;">sk-</b> 开头的密钥。</p>'
            '<p style="color:#7dd3fc; font-weight:600; margin-bottom:4px;">第四步：复制并使用</p>'
            '<p style="color:#a0b4c4; margin-bottom:12px;">'
            '点击复制按钮，将 API Key 复制到剪贴板。<br>'
            '回到本软件，粘贴到输入框中，点击"开始使用"即可。</p>'
            '<p style="color:#ff6b6b; font-size:12px; margin-top:8px;">'
            '⚠ 请妥善保管您的 API Key，不要分享给他人。<br>'
            '如果密钥泄露，请立即到后台重新生成。</p>'
        )
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("我知道了")
        close_btn.setObjectName("closeBtn")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)


class KeyDialog(QDialog):
    def __init__(self, parent=None, prefill_key="", theme=None):
        super().__init__(parent)
        self.setWindowTitle("Glacier AI")
        self.setFixedSize(440, 460)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setStyleSheet(build_login_style(theme or DEFAULT_THEME))

        logo_path = get_logo_path()
        if os.path.exists(logo_path):
            self.setWindowIcon(QIcon(logo_path))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(30, 30, 30, 30)

        card = QFrame()
        card.setObjectName("loginCard")
        card_layout = QVBoxLayout(card)
        card_layout.setSpacing(16)
        card_layout.setContentsMargins(28, 32, 28, 28)

        title = QLabel("Glacier AI")
        title.setObjectName("loginTitle")
        card_layout.addWidget(title)

        subtitle = QLabel("New API - Image & Video Generator")
        subtitle.setObjectName("loginSubtitle")
        card_layout.addWidget(subtitle)

        version = QLabel("VERSION 3.3")
        version.setObjectName("loginVersion")
        card_layout.addWidget(version)

        card_layout.addSpacing(8)

        hint = QLabel("请输入您的 API Key")
        hint.setObjectName("loginHint")
        card_layout.addWidget(hint)

        self.key_input = QLineEdit()
        self.key_input.setObjectName("keyInput")
        self.key_input.setPlaceholderText("sk-xxxxxxxxxxxxxxxx")
        self.key_input.setMinimumHeight(42)
        self.key_input.setEchoMode(QLineEdit.Password)
        card_layout.addWidget(self.key_input)

        self.toggle_btn = QPushButton("显示")
        self.toggle_btn.setObjectName("toggleBtn")
        self.toggle_btn.setCursor(Qt.PointingHandCursor)
        self.toggle_btn.clicked.connect(self.toggle_visibility)
        card_layout.addWidget(self.toggle_btn, alignment=Qt.AlignRight)

        self.remember_chk = QCheckBox("记住 API Key（保存到本地，下次免输入）")
        self.remember_chk.setObjectName("rememberChk")
        self.remember_chk.setCursor(Qt.PointingHandCursor)
        self.remember_chk.setChecked(True)
        card_layout.addWidget(self.remember_chk)

        card_layout.addSpacing(4)

        self.login_btn = QPushButton("开始使用")
        self.login_btn.setObjectName("loginBtn")
        self.login_btn.setMinimumHeight(44)
        self.login_btn.setCursor(Qt.PointingHandCursor)
        self.login_btn.clicked.connect(self.on_login)
        card_layout.addWidget(self.login_btn)

        self.tutorial_btn = QPushButton("如何获取 API Key？")
        self.tutorial_btn.setObjectName("tutorialBtn")
        self.tutorial_btn.setCursor(Qt.PointingHandCursor)
        self.tutorial_btn.clicked.connect(self.show_tutorial)
        card_layout.addWidget(self.tutorial_btn, alignment=Qt.AlignCenter)

        outer.addWidget(card)

        if prefill_key:
            self.key_input.setText(prefill_key)
            self.remember_chk.setChecked(True)

    def toggle_visibility(self):
        if self.key_input.echoMode() == QLineEdit.Password:
            self.key_input.setEchoMode(QLineEdit.Normal)
            self.toggle_btn.setText("隐藏")
        else:
            self.key_input.setEchoMode(QLineEdit.Password)
            self.toggle_btn.setText("显示")

    def on_login(self):
        key = self.key_input.text().strip()
        if not key:
            mb = QMessageBox(self)
            mb.setIcon(QMessageBox.Warning)
            mb.setWindowTitle("提示")
            mb.setText("请输入正确的 API Key")
            mb.exec_()
            return
        if not key.startswith("sk-"):
            mb = QMessageBox(self)
            mb.setIcon(QMessageBox.Warning)
            mb.setWindowTitle("提示")
            mb.setText("请输入正确的 API Key")
            mb.setInformativeText("API Key 格式不正确，应以 sk- 开头")
            mb.exec_()
            return
        self.accept()

    def get_key(self):
        return self.key_input.text().strip()

    def should_remember(self):
        return self.remember_chk.isChecked()

    def show_tutorial(self):
        dlg = TutorialDialog(self)
        dlg.exec_()


class UploadRefImageThread(QThread):
    finished_ok = pyqtSignal(str, str, bytes)
    failed = pyqtSignal(str, str)

    def __init__(self, api_key, file_path, tag=""):
        super().__init__()
        self.api_key = api_key
        self.file_path = file_path
        self.tag = tag

    def run(self):
        try:
            with open(self.file_path, "rb") as f:
                img_bytes = f.read()
        except Exception as e:
            self.failed.emit(self.tag, f"读取图片失败: {e}")
            return

        filename = os.path.basename(self.file_path)
        ext = os.path.splitext(filename)[1].lower().lstrip(".") or "png"
        if ext == "jpg":
            ext = "jpeg"
        mime = f"image/{ext}"
        try:
            resp = requests.post(
                FILE_UPLOAD_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": (filename, img_bytes, mime)},
                timeout=120,
                proxies=NO_PROXIES,
            )
        except Exception as e:
            self.failed.emit(self.tag, f"上传请求失败: {e}")
            return

        try:
            with open("debug_output.log", "a", encoding="utf-8") as _f:
                _f.write(f"[UPLOAD] status={resp.status_code}, body={resp.text[:400]}\n")
        except Exception:
            pass

        if resp.status_code != 200:
            self.failed.emit(self.tag, f"上传失败 ({resp.status_code}): {resp.text[:200]}")
            return
        try:
            data = resp.json()
        except Exception:
            self.failed.emit(self.tag, f"上传响应非 JSON: {resp.text[:200]}")
            return
        if not data.get("success"):
            self.failed.emit(self.tag, f"上传失败: {data.get('message') or '未知错误'}")
            return
        url = (data.get("data") or {}).get("url") or ""
        if not url:
            self.failed.emit(self.tag, "上传响应缺少 url")
            return
        self.finished_ok.emit(self.tag, url, img_bytes)


class BalanceQueryThread(QThread):
    """查询账户余额。hfsyapi 提供的专用接口 /api/usage/token/fund 直接返回当前账号余额。"""
    finished_ok = pyqtSignal(float)
    failed = pyqtSignal(str)

    def __init__(self, api_key):
        super().__init__()
        self.api_key = api_key

    def run(self):
        try:
            resp = requests.get(
                BALANCE_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=15,
                proxies=NO_PROXIES,
            )
        except Exception as e:
            self.failed.emit(f"网络错误: {e}")
            return
        if resp.status_code != 200:
            self.failed.emit(f"HTTP {resp.status_code}")
            return
        try:
            data = resp.json()
        except Exception:
            self.failed.emit("响应解析失败")
            return
        if not data.get("success", False):
            self.failed.emit(data.get("message") or "查询失败")
            return

        amount = self._extract_amount(data.get("data"))
        if amount is None:
            self.failed.emit(f"未识别的余额字段: {repr(data.get('data'))[:80]}")
            return
        self.finished_ok.emit(amount)

    @staticmethod
    def _extract_amount(payload):
        """兼容 data 为数值/字符串/对象的多种返回格式。"""
        if payload is None:
            return None
        if isinstance(payload, (int, float)):
            return float(payload)
        if isinstance(payload, str):
            try:
                return float(payload.replace(",", "").replace("¥", "").replace("$", "").strip())
            except ValueError:
                return None
        if isinstance(payload, dict):
            for key in ("fund", "balance", "remain", "remain_quota", "quota"):
                if key in payload:
                    val = payload[key]
                    if isinstance(val, (int, float)):
                        return float(val)
                    if isinstance(val, str):
                        try:
                            return float(val)
                        except ValueError:
                            continue
        return None


class GenerateThread(QThread):
    one_finished = pyqtSignal(int, bytes)
    progress = pyqtSignal(str)
    error = pyqtSignal(int, str)
    all_done = pyqtSignal()

    def __init__(self, api_key, model, prompt, size, quality, output_format, count=1, image_url=None):
        super().__init__()
        self.api_key = api_key
        self.model = model
        self.prompt = prompt
        self.size = size
        self.quality = quality
        self.output_format = output_format
        self.count = count
        self.image_url = image_url

    def _generate_one(self, index):
        if self.model in GEMINI_MODELS:
            self._generate_one_gemini(index)
            return
        print(f"[DEBUG] _generate_one({index}) started, image_url={'YES' if self.image_url else 'NO'}, model={self.model}", flush=True)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "prompt": self.prompt,
            "n": 1,
            "size": self.size,
            "quality": self.quality,
            "response_format": "b64_json",
            "output_format": self.output_format
        }
        if self.image_url:
            payload["reference_images"] = list(self.image_url)
            print(f"[DEBUG] POST {API_URL} (json, {len(self.image_url)} reference_images)", flush=True)
        else:
            print(f"[DEBUG] POST {API_URL} (json, no image)", flush=True)

        import time as _time
        max_retries = 2
        resp = None
        for attempt in range(max_retries + 1):
            resp = requests.post(API_URL, headers=headers, json=payload, timeout=600, proxies=NO_PROXIES)
            try:
                with open("debug_output.log", "a", encoding="utf-8") as _f:
                    _f.write(f"[RESP] model={self.model}, status={resp.status_code}, body={resp.text[:500]}\n")
            except Exception:
                pass
            print(f"[DEBUG] Response status={resp.status_code}, body={resp.text[:300]}", flush=True)
            if resp.status_code == 200:
                break
            if resp.status_code == 429 and attempt < max_retries:
                try:
                    wait = max(1, int(resp.headers.get("Retry-After", "5")))
                except (TypeError, ValueError):
                    wait = 5
                print(f"[DEBUG] 429 limited, retry {attempt+1}/{max_retries} after {wait}s...", flush=True)
                self.progress.emit(f"第{index+1}张被限流，{wait}秒后重试...")
                _time.sleep(wait)
                continue
            if resp.status_code in (500, 502, 503) and attempt < max_retries:
                wait = (attempt + 1) * 3
                print(f"[DEBUG] Retry {attempt+1}/{max_retries} after {wait}s...", flush=True)
                self.progress.emit(f"第{index+1}张遇到服务器错误，{wait}秒后重试...")
                _time.sleep(wait)
            else:
                break

        if resp.status_code != 200:
            self.error.emit(index, f"第{index+1}张 API 错误 ({resp.status_code}): {resp.text[:200]}")
            return

        data = resp.json()
        if "data" not in data or len(data["data"]) == 0:
            self.error.emit(index, f"第{index+1}张 API 返回异常")
            return

        item = data["data"][0]
        if item.get("b64_json"):
            img_bytes = base64.b64decode(item["b64_json"])
            self.one_finished.emit(index, img_bytes)
        elif "url" in item or "file_id" in item:
            img_url = item.get("url", "")
            file_id = item.get("file_id", "")
            if img_url.startswith("http"):
                dl_url = img_url
            elif file_id:
                dl_url = f"https://www.hfsyapi.cn/v1/files/{file_id}/content"
            elif img_url.startswith("/"):
                dl_url = "https://www.hfsyapi.cn" + img_url
            else:
                dl_url = img_url
            print(f"[DEBUG] Downloading image from: {dl_url}", flush=True)
            dl_headers = {"Authorization": f"Bearer {self.api_key}"}
            img_resp = requests.get(dl_url, headers=dl_headers, timeout=120, proxies=NO_PROXIES)
            print(f"[DEBUG] Download status={img_resp.status_code}, content_length={len(img_resp.content)}, content_type={img_resp.headers.get('content-type','?')}", flush=True)
            if img_resp.status_code == 200 and len(img_resp.content) > 1000:
                content_type = img_resp.headers.get("content-type", "")
                if "image" in content_type or "octet-stream" in content_type or dl_url.endswith(".png") or dl_url.split("?")[0].endswith(".png") or dl_url.split("?")[0].endswith(".jpg"):
                    self.one_finished.emit(index, img_resp.content)
                else:
                    header = img_resp.content[:8]
                    is_png = header[:4] == b'\x89PNG'
                    is_jpg = header[:2] == b'\xff\xd8'
                    is_webp = header[8:12] == b'WEBP' if len(header) >= 12 else False
                    if is_png or is_jpg or is_webp:
                        self.one_finished.emit(index, img_resp.content)
                    elif file_id:
                        print(f"[DEBUG] Not an image, trying file_id fallback...", flush=True)
                        fb_url = f"https://www.hfsyapi.cn/v1/files/{file_id}/content"
                        fb_resp = requests.get(fb_url, headers=dl_headers, timeout=120, proxies=NO_PROXIES)
                        if fb_resp.status_code == 200 and len(fb_resp.content) > 2000:
                            self.one_finished.emit(index, fb_resp.content)
                        else:
                            self.error.emit(index, f"第{index+1}张下载失败")
                    else:
                        self.error.emit(index, f"第{index+1}张下载失败(非图片)")
            elif img_resp.status_code == 200:
                self.error.emit(index, f"第{index+1}张下载内容为空")
            else:
                print(f"[DEBUG] Download failed body={img_resp.text[:200]}", flush=True)
                self.error.emit(index, f"第{index+1}张下载失败 ({img_resp.status_code})")
        else:
            self.error.emit(index, f"第{index+1}张未知格式")

    def run(self):
        import threading, time
        threads = []
        for i in range(self.count):
            if i > 0:
                time.sleep(0.05)
            self.progress.emit(f"正在启动第 {i+1}/{self.count} 张生成...")
            t = threading.Thread(target=self._safe_generate, args=(i,))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        self.all_done.emit()

    def _generate_one_gemini(self, index):
        print(f"[DEBUG] _generate_one_gemini({index}) started, image_url={'YES' if self.image_url else 'NO'}, model={self.model}", flush=True)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        parts = [{"text": self.prompt}]
        if self.image_url:
            for url in list(self.image_url)[:GEMINI_MAX_REFERENCE_IMAGES]:
                parts.append({"fileData": {"fileUri": url}})

        ratio = "1:1"
        for r in GEMINI_RATIO_LIST:
            if GEMINI_SIZE_MAP.get(r, {}).get(self.quality) == self.size:
                ratio = r
                break

        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "imageConfig": {
                    "aspectRatio": ratio,
                    "imageSize": self.quality.upper()
                }
            }
        }

        api_url = GEMINI_API_URL_TEMPLATE.format(model=self.model)
        print(f"[DEBUG] POST {api_url} (gemini, ratio={ratio}, refs={len(self.image_url) if self.image_url else 0})", flush=True)

        import time as _time
        max_retries = 2
        resp = None
        for attempt in range(max_retries + 1):
            resp = requests.post(api_url, headers=headers, json=payload, timeout=600, proxies=NO_PROXIES)
            try:
                with open("debug_output.log", "a", encoding="utf-8") as _f:
                    _f.write(f"[RESP] model={self.model}, status={resp.status_code}, body={resp.text[:500]}\n")
            except Exception:
                pass
            print(f"[DEBUG] Gemini response status={resp.status_code}, body={resp.text[:300]}", flush=True)
            if resp.status_code == 200:
                break
            if resp.status_code == 429 and attempt < max_retries:
                try:
                    wait = max(1, int(resp.headers.get("Retry-After", "5")))
                except (TypeError, ValueError):
                    wait = 5
                print(f"[DEBUG] 429 limited, retry {attempt+1}/{max_retries} after {wait}s...", flush=True)
                self.progress.emit(f"第{index+1}张被限流，{wait}秒后重试...")
                _time.sleep(wait)
                continue
            if resp.status_code in (500, 502, 503) and attempt < max_retries:
                wait = (attempt + 1) * 3
                self.progress.emit(f"第{index+1}张遇到服务器错误，{wait}秒后重试...")
                _time.sleep(wait)
            else:
                break

        if resp.status_code != 200:
            self.error.emit(index, f"第{index+1}张 API 错误 ({resp.status_code}): {resp.text[:200]}")
            return

        try:
            data = resp.json()
        except Exception:
            self.error.emit(index, f"第{index+1}张 API 返回非 JSON")
            return

        img_url = self._extract_gemini_image_url(data)
        if not img_url:
            self.error.emit(index, f"第{index+1}张未找到图片地址")
            return

        if img_url.startswith("/"):
            img_url = "https://www.hfsyapi.cn" + img_url

        print(f"[DEBUG] Downloading gemini image from: {img_url}", flush=True)
        dl_headers = {"Authorization": f"Bearer {self.api_key}"}
        img_resp = requests.get(img_url, headers=dl_headers, timeout=120, proxies=NO_PROXIES)
        print(f"[DEBUG] Gemini download status={img_resp.status_code}, length={len(img_resp.content)}", flush=True)

        if img_resp.status_code == 200 and len(img_resp.content) > 1000:
            self.one_finished.emit(index, img_resp.content)
        else:
            self.error.emit(index, f"第{index+1}张下载失败 ({img_resp.status_code})")

    def _extract_gemini_image_url(self, data):
        try:
            candidates = data.get("candidates", [])
            for cand in candidates:
                content = cand.get("content", {})
                for p in content.get("parts", []):
                    fd = p.get("fileData") or p.get("file_data")
                    if fd:
                        uri = fd.get("fileUri") or fd.get("file_uri")
                        if uri:
                            return uri
                    inline = p.get("inlineData") or p.get("inline_data")
                    if inline and inline.get("data"):
                        return None
        except Exception as e:
            print(f"[DEBUG] _extract_gemini_image_url error: {e}", flush=True)
        return None

    def _safe_generate(self, index):
        try:
            self._generate_one(index)
        except requests.exceptions.Timeout:
            print(f"[DEBUG] _safe_generate({index}) Timeout", flush=True)
            self.error.emit(index, f"第{index+1}张请求超时")
        except requests.exceptions.ConnectionError:
            print(f"[DEBUG] _safe_generate({index}) ConnectionError", flush=True)
            self.error.emit(index, f"第{index+1}张网络连接失败")
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[DEBUG] _safe_generate({index}) Exception: {e}\n{tb}", flush=True)
            try:
                with open("debug_output.log", "a", encoding="utf-8") as _f:
                    _f.write(f"[EXCEPTION] index={index}: {e}\n{tb}\n")
            except Exception:
                pass
            self.error.emit(index, f"第{index+1}张错误: {str(e)}")


class VideoGenerateThread(QThread):
    one_finished = pyqtSignal(int, bytes)
    progress = pyqtSignal(str)
    error = pyqtSignal(int, str)
    all_done = pyqtSignal()

    def __init__(self, api_key, prompt, size, duration, count=1, image_url=None, model="sora-2", video_refs=None, audio_refs=None, sd_size=None, sd_ratio=None, start_image_url=None, end_image_url=None):
        super().__init__()
        self.api_key = api_key
        self.prompt = prompt
        self.size = size
        self.duration = duration
        self.count = count
        self.image_url = image_url
        self.model = model
        self.video_refs = video_refs
        self.audio_refs = audio_refs
        self.sd_size = sd_size
        self.sd_ratio = sd_ratio
        self.start_image_url = start_image_url
        self.end_image_url = end_image_url

    def _generate_one(self, index):
        import time
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "prompt": self.prompt,
            "orientation": self.size,
            "duration": int(self.duration),
            "watermark": False,
        }
        if self.model == "sora-2":
            payload["size"] = "1080p"
        elif self.sd_size:
            payload["size"] = self.sd_size
        if self.sd_ratio:
            payload["ratio"] = self.sd_ratio
        if self.image_url:
            if isinstance(self.image_url, str):
                payload["images"] = [self.image_url]
            else:
                payload["images"] = list(self.image_url)
        if self.video_refs:
            payload["videos"] = list(self.video_refs)
        if self.audio_refs:
            payload["audios"] = list(self.audio_refs)
        if self.start_image_url:
            payload["start_image_url"] = self.start_image_url
        if self.end_image_url:
            payload["end_image_url"] = self.end_image_url

        def _log(tag, content):
            try:
                with open("debug_output.log", "a", encoding="utf-8") as _f:
                    _f.write(f"[VIDEO {tag}] index={index}: {content}\n")
            except Exception:
                pass

        self.progress.emit(f"第{index+1}个视频: 正在提交请求...")
        resp = None
        for attempt in range(3):
            resp = requests.post(VIDEO_API_URL, headers=headers, json=payload, timeout=600, proxies=NO_PROXIES)
            _log("POST", f"attempt={attempt}, status={resp.status_code}, body={resp.text[:800]}")
            if resp.status_code == 200:
                break
            if resp.status_code == 429 and attempt < 2:
                try:
                    wait = max(1, int(resp.headers.get("Retry-After", "5")))
                except (TypeError, ValueError):
                    wait = 5
                self.progress.emit(f"第{index+1}个视频被限流，{wait}秒后重试...")
                time.sleep(wait)
                continue
            if resp.status_code in (500, 502, 503) and attempt < 2:
                wait = (attempt + 1) * 3
                self.progress.emit(f"第{index+1}个视频遇到服务器错误，{wait}秒后重试...")
                time.sleep(wait)
                continue
            break
        if resp.status_code != 200:
            self.error.emit(index, f"第{index+1}个 API 错误 ({resp.status_code}): {resp.text[:300]}")
            return

        data = resp.json()
        task_id = data.get("id") or data.get("task_id")
        if isinstance(data.get("data"), dict):
            task_id = task_id or data["data"].get("id")
        if not task_id:
            _log("NO_TASK_ID", f"data={json.dumps(data, ensure_ascii=False)[:600]}")
            self.error.emit(index, f"第{index+1}个 API 返回异常: 无法识别 task_id")
            return

        self.progress.emit(f"第{index+1}个视频: 任务已提交，等待生成...")
        query_url = f"https://www.hfsyapi.cn/v1/video/query?id={task_id}"

        for i in range(120):
            time.sleep(5)
            try:
                poll_resp = requests.get(query_url, headers=headers, timeout=30, proxies=NO_PROXIES)
            except Exception as e:
                _log("POLL_ERR", f"i={i}, err={e}")
                continue
            if poll_resp.status_code != 200:
                _log("POLL_HTTP", f"i={i}, status={poll_resp.status_code}, body={poll_resp.text[:300]}")
                if poll_resp.status_code == 429:
                    try:
                        extra = max(0, int(poll_resp.headers.get("Retry-After", "0")))
                    except (TypeError, ValueError):
                        extra = 0
                    if extra:
                        time.sleep(extra)
                continue
            try:
                poll_data = poll_resp.json()
            except Exception as e:
                _log("POLL_JSON_ERR", f"i={i}, err={e}, body={poll_resp.text[:300]}")
                continue

            inner = poll_data.get("data") if isinstance(poll_data.get("data"), dict) else {}
            status = str(inner.get("status") or poll_data.get("status") or "").lower()
            video_url = inner.get("video_url") or poll_data.get("video_url") or ""
            if not video_url:
                detail = inner.get("detail") if isinstance(inner.get("detail"), dict) else {}
                video_url = detail.get("url") or ""
            progress_pct = inner.get("progress", 0)

            if status in ("completed", "success"):
                if not video_url:
                    _log("NO_URL", f"poll_data={json.dumps(poll_data, ensure_ascii=False)[:600]}")
                    self.error.emit(index, f"第{index+1}个任务完成但未返回视频地址")
                    return
                _log("COMPLETED", f"poll_iter={i}, elapsed={(i+1)*5}s, url={video_url[:120]}")
                self.progress.emit(f"第{index+1}个视频: 正在下载...")
                dl_start = time.time()
                vid_resp = requests.get(video_url, timeout=300, proxies=NO_PROXIES)
                dl_elapsed = time.time() - dl_start
                _log("DOWNLOAD", f"status={vid_resp.status_code}, bytes={len(vid_resp.content)}, elapsed={dl_elapsed:.1f}s")
                if vid_resp.status_code == 200:
                    self.one_finished.emit(index, vid_resp.content)
                else:
                    self.error.emit(index, f"第{index+1}个视频下载失败 ({vid_resp.status_code})")
                return
            if status in ("failed", "failure"):
                err_msg = inner.get("error") or inner.get("message") or poll_data.get("message") or "未知错误"
                self.error.emit(index, f"第{index+1}个视频生成失败: {err_msg}")
                return
            self.progress.emit(f"第{index+1}个视频: 生成中... ({(i+1) * 5}s, {progress_pct}%, {status or 'pending'})")

        self.error.emit(index, f"第{index+1}个视频生成超时")

    def _safe_generate(self, index):
        try:
            self._generate_one(index)
        except requests.exceptions.Timeout:
            self.error.emit(index, f"第{index+1}个视频请求超时")
        except requests.exceptions.ConnectionError:
            self.error.emit(index, f"第{index+1}个视频网络连接失败")
        except Exception as e:
            self.error.emit(index, f"第{index+1}个视频错误: {str(e)}")

    def run(self):
        import threading, time
        threads = []
        for i in range(self.count):
            if i > 0:
                time.sleep(0.05)
            self.progress.emit(f"正在启动第 {i+1}/{self.count} 个视频生成...")
            t = threading.Thread(target=self._safe_generate, args=(i,))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        self.all_done.emit()


class Task:
    _id_counter = 0

    @classmethod
    def next_id(cls):
        cls._id_counter += 1
        return cls._id_counter

    def __init__(self, kind, params, summary):
        self.id = Task.next_id()
        self.kind = kind
        self.params = params
        self.summary = summary
        self.status = "pending"
        self.results = []
        self.error_msg = ""
        self.thread = None
        self.done_count = 0
        self.total = int(summary.get("count", 1))
        self.created_at = datetime.now()


class TaskScheduler(QObject):
    task_state_changed = pyqtSignal(int)
    queue_changed = pyqtSignal()

    def __init__(self, main_window, max_concurrent=3, max_queue=None):
        super().__init__()
        self.main_window = main_window
        self.max_concurrent = max(1, min(5, int(max_concurrent)))
        self.max_queue = max_queue  # None = 不限制
        self.pending = []
        self.running = {}
        self.all_tasks = {}

    def total_count(self):
        return len(self.pending) + len(self.running)

    def has_unfinished(self):
        return self.total_count() > 0

    def can_enqueue(self):
        if self.max_queue is None:
            return True
        return self.total_count() < self.max_queue

    def counts_by_kind(self):
        img_run = sum(1 for t in self.running.values() if t.kind == "image")
        vid_run = sum(1 for t in self.running.values() if t.kind == "video")
        return img_run, vid_run, len(self.pending)

    def enqueue(self, task):
        if not self.can_enqueue():
            return False
        task.status = "pending"
        self.pending.append(task)
        self.all_tasks[task.id] = task
        self.queue_changed.emit()
        self.task_state_changed.emit(task.id)
        self._try_run_next()
        return True

    def cancel(self, task_id):
        for i, t in enumerate(self.pending):
            if t.id == task_id:
                self.pending.pop(i)
                t.status = "canceled"
                self.all_tasks.pop(task_id, None)
                self.queue_changed.emit()
                self.task_state_changed.emit(task_id)
                return "removed"
        if task_id in self.running:
            return "running"
        return "not_found"

    def dismiss(self, task_id):
        for i, t in enumerate(self.pending):
            if t.id == task_id:
                self.pending.pop(i)
                break
        self.running.pop(task_id, None)
        self.all_tasks.pop(task_id, None)
        self.queue_changed.emit()

    def retry(self, task_id):
        t = self.all_tasks.get(task_id)
        if not t or t.status != "failed":
            return False
        if not self.can_enqueue():
            return False
        t.status = "pending"
        t.results = []
        t.error_msg = ""
        t.done_count = 0
        t.thread = None
        if t not in self.pending:
            self.pending.append(t)
        self.queue_changed.emit()
        self.task_state_changed.emit(task_id)
        self._try_run_next()
        return True

    def set_max_concurrent(self, n):
        self.max_concurrent = max(1, min(5, int(n)))
        self._try_run_next()

    def _try_run_next(self):
        while self.pending and len(self.running) < self.max_concurrent:
            task = self.pending.pop(0)
            self.running[task.id] = task
            task.status = "running"
            try:
                self.main_window._start_task(task)
            except Exception as e:
                task.status = "failed"
                task.error_msg = f"启动失败: {e}"
                self.running.pop(task.id, None)
            self.queue_changed.emit()
            self.task_state_changed.emit(task.id)

    def on_task_one_finished(self, task_id, index, data):
        t = self.all_tasks.get(task_id)
        if not t:
            return
        t.results.append(data)
        t.done_count += 1
        self.task_state_changed.emit(task_id)

    def on_task_error(self, task_id, index, msg):
        t = self.all_tasks.get(task_id)
        if not t:
            return
        t.done_count += 1
        if not t.error_msg:
            t.error_msg = msg
        self.task_state_changed.emit(task_id)

    def on_task_all_done(self, task_id):
        t = self.all_tasks.get(task_id)
        if not t:
            return
        if not t.results:
            t.status = "failed"
            if not t.error_msg:
                t.error_msg = "全部生成失败"
        else:
            t.status = "done"
        self.running.pop(task_id, None)
        self.task_state_changed.emit(task_id)
        self.queue_changed.emit()
        try:
            self.main_window._on_task_finished(t)
        except Exception as e:
            print(f"[DEBUG] _on_task_finished error: {e}", flush=True)
        self._try_run_next()


class TaskCard(QFrame):
    """任务卡片：标题 + 状态徽章 + 缩略图网格 + 操作按钮"""

    STATUS_TEXT = {
        "pending": "排队中",
        "running": "生成中",
        "done": "已完成",
        "failed": "失败",
    }

    def __init__(self, task, main_window, parent=None):
        super().__init__(parent)
        self.task = task
        self.main_window = main_window
        self.setObjectName("taskCard")
        theme = get_theme(main_window._theme)
        self.setStyleSheet(
            "QFrame#taskCard {"
            f" background: {theme['bg_card_solid']};"
            f" border: 1px solid {theme['border_soft']};"
            " border-radius: 10px;"
            "}"
            "QFrame#taskCard:hover {"
            f" border: 1px solid {theme['accent_border']};"
            "}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(10)

        kind_emoji = "⚡" if task.kind == "image" else "🎬"
        title_text = f"{kind_emoji}  #{task.id}  {task.summary.get('label','')}"
        self.title_lbl = QLabel(title_text)
        self.title_lbl.setStyleSheet(
            f"color: {theme['text_primary']}; font-size: 13px; font-weight: 700;"
            " border: none; background: transparent;"
        )
        head.addWidget(self.title_lbl, 1)

        self.status_lbl = QLabel("")
        self.status_lbl.setAlignment(Qt.AlignCenter)
        self.status_lbl.setMinimumWidth(72)
        self.status_lbl.setStyleSheet(self._status_style("pending", theme))
        head.addWidget(self.status_lbl)

        self.cancel_btn = QPushButton("✕")
        self.cancel_btn.setFixedSize(26, 26)
        self.cancel_btn.setCursor(Qt.PointingHandCursor)
        self.cancel_btn.setStyleSheet(
            "QPushButton { background: rgba(255,107,107,0.15); color: #ff6b6b;"
            " border: 1px solid rgba(255,107,107,0.35); border-radius: 4px; font-size: 14px; }"
            "QPushButton:hover { background: rgba(255,107,107,0.28); }"
        )
        self.cancel_btn.clicked.connect(self._on_cancel)
        head.addWidget(self.cancel_btn)

        outer.addLayout(head)

        meta = task.summary.get("meta", "")
        if meta:
            meta_lbl = QLabel(meta)
            meta_lbl.setStyleSheet(
                f"color: {theme['text_muted']}; font-size: 11px;"
                " border: none; background: transparent;"
            )
            outer.addWidget(meta_lbl)

        prompt = task.summary.get("prompt", "")
        if prompt:
            text = prompt if len(prompt) <= 120 else prompt[:120] + "..."
            prompt_lbl = QLabel(text)
            prompt_lbl.setWordWrap(True)
            prompt_lbl.setStyleSheet(
                f"color: {theme['text_secondary']}; font-size: 12px;"
                " border: none; background: transparent;"
            )
            outer.addWidget(prompt_lbl)

        self.thumbs_container = QWidget()
        self.thumbs_container.setStyleSheet("background: transparent;")
        self.thumbs_layout = QGridLayout(self.thumbs_container)
        self.thumbs_layout.setSpacing(8)
        self.thumbs_layout.setContentsMargins(0, 4, 0, 4)
        self.thumbs_layout.setAlignment(Qt.AlignLeft)
        outer.addWidget(self.thumbs_container)

        self.error_lbl = QLabel("")
        self.error_lbl.setWordWrap(True)
        self.error_lbl.setStyleSheet(
            "color: #ff6b6b; font-size: 11px; border: none; background: transparent;"
        )
        self.error_lbl.hide()
        outer.addWidget(self.error_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()

        self.retry_btn = QPushButton("重试")
        self.retry_btn.setObjectName("saveBtn")
        self.retry_btn.setCursor(Qt.PointingHandCursor)
        self.retry_btn.clicked.connect(self._on_retry)
        self.retry_btn.hide()
        btn_row.addWidget(self.retry_btn)

        self.select_all_btn = QPushButton("全选")
        self.select_all_btn.setObjectName("saveBtn")
        self.select_all_btn.setCursor(Qt.PointingHandCursor)
        self.select_all_btn.clicked.connect(self._on_select_all)
        self.select_all_btn.hide()
        btn_row.addWidget(self.select_all_btn)

        self.save_btn = QPushButton("保存选中")
        self.save_btn.setObjectName("saveBtn")
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.clicked.connect(self._on_save_selected)
        self.save_btn.hide()
        btn_row.addWidget(self.save_btn)

        outer.addLayout(btn_row)

        self.update_state()

    @staticmethod
    def _status_style(status, theme):
        bg, fg = {
            "pending": (theme.get('accent_softer', 'rgba(125,211,252,0.1)'), theme.get('accent', '#7dd3fc')),
            "running": ("rgba(96,165,250,0.18)", "#60a5fa"),
            "done": ("rgba(34,197,94,0.18)", "#22c55e"),
            "failed": ("rgba(239,68,68,0.18)", "#ef4444"),
        }.get(status, (theme.get('accent_softer', 'rgba(125,211,252,0.1)'), theme.get('accent', '#7dd3fc')))
        return (
            f"QLabel {{ background: {bg}; color: {fg};"
            " border-radius: 12px; padding: 3px 10px;"
            " font-size: 11px; font-weight: 700; }"
        )

    def update_state(self):
        t = self.task
        theme = get_theme(self.main_window._theme)
        status_key = t.status if t.status in self.STATUS_TEXT else "pending"
        text = self.STATUS_TEXT.get(status_key, status_key)
        if status_key == "running" and t.total > 1:
            text = f"生成中 {t.done_count}/{t.total}"
        self.status_lbl.setText(text)
        self.status_lbl.setStyleSheet(self._status_style(status_key, theme))

        self.cancel_btn.setVisible(status_key in ("pending", "running"))
        self.retry_btn.setVisible(status_key == "failed")
        has_results = bool(t.results)
        self.select_all_btn.setVisible(status_key in ("done",) and has_results)
        self.save_btn.setVisible(status_key in ("done",) and has_results)

        if status_key == "failed" and t.error_msg:
            self.error_lbl.setText(f"错误：{t.error_msg}")
            self.error_lbl.show()
        elif status_key == "done" and t.error_msg and len(t.results) < t.total:
            self.error_lbl.setText(f"部分失败：{t.error_msg}")
            self.error_lbl.show()
        else:
            self.error_lbl.hide()

        self._refresh_thumbs()

    def _refresh_thumbs(self):
        while self.thumbs_layout.count():
            w = self.thumbs_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not self.task.results:
            return
        is_video = self.task.kind == "video"
        ext = self.task.params.get("ext", "png") if not is_video else "mp4"
        cols = 3
        for i, data in enumerate(self.task.results):
            if is_video:
                lbl = ClickableLabel(data, file_ext="mp4", is_video=True)
                _t = get_theme(self.main_window._theme)
                lbl._vid_style_base = (
                    "QLabel { border: 2px solid transparent; border-radius: 8px; padding: 0px;"
                    f" background-color: {_t['bg_input_strong']}; color: {_t['text_input']}; font-size: 13px; }}"
                    "QLabel:hover { border-color: " + _t['accent_border'] + "; background-color: " + _t['accent_softer'] + "; }"
                )
                lbl._vid_style_checked = (
                    "QLabel { border: 3px solid " + _t['accent'] + "; border-radius: 8px; padding: 0px;"
                    f" background-color: {_t['accent_softer']}; color: {_t['text_input']}; font-size: 13px; }}"
                )
                lbl.setStyleSheet(lbl._vid_style_base)
            else:
                lbl = ClickableLabel(data, file_ext=ext)
                qimg = QImage.fromData(data)
                if not qimg.isNull():
                    pixmap = QPixmap.fromImage(qimg)
                    scaled = pixmap.scaled(140, 140, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    lbl.setPixmap(scaled)
            row, col = divmod(i, cols)
            self.thumbs_layout.addWidget(lbl, row, col)

    def _selected_labels(self):
        out = []
        for i in range(self.thumbs_layout.count()):
            w = self.thumbs_layout.itemAt(i).widget()
            if isinstance(w, ClickableLabel) and w.checked:
                out.append(w)
        return out

    def _all_labels(self):
        out = []
        for i in range(self.thumbs_layout.count()):
            w = self.thumbs_layout.itemAt(i).widget()
            if isinstance(w, ClickableLabel):
                out.append(w)
        return out

    def _on_select_all(self):
        labels = self._all_labels()
        if not labels:
            return
        all_checked = all(l.checked for l in labels)
        for l in labels:
            l.checked = not all_checked
            l._update_border()
        self.select_all_btn.setText("取消全选" if not all_checked else "全选")

    def _on_save_selected(self):
        labels = self._selected_labels()
        if not labels:
            mb = QMessageBox(self)
            mb.setWindowTitle("提示")
            mb.setIcon(QMessageBox.Information)
            mb.setText("请先单击选中要保存的项（蓝色边框表示选中）")
            mb.setStyleSheet(
                "QMessageBox { background-color: #ffffff; min-width: 380px; }"
                "QMessageBox QLabel { color: #000000; font-size: 16px; padding: 8px 4px; }"
                "QMessageBox QPushButton { color: #ffffff; background-color: #2563eb;"
                " border: none; border-radius: 6px; padding: 6px 18px; min-width: 64px; font-size: 15px; }"
                "QMessageBox QPushButton:hover { background-color: #1d4ed8; }"
            )
            mb.exec_()
            return
        is_video = self.task.kind == "video"
        if is_video:
            base = "sora_video"
            ext = "mp4"
            filters = "MP4 视频 (*.mp4);;所有文件 (*)"
            single_title = "保存视频"
        else:
            ext = self.task.params.get("ext", "png")
            base = "gpt_image"
            if ext == "png":
                filters = "PNG 图片 (*.png);;JPEG 图片 (*.jpg);;所有文件 (*)"
            else:
                filters = "JPEG 图片 (*.jpg);;PNG 图片 (*.png);;所有文件 (*)"
            single_title = "保存图片"
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        if len(labels) == 1:
            default_name = f"{base}_{ts}.{ext}"
            path, _ = QFileDialog.getSaveFileName(
                self, single_title,
                os.path.join(os.path.expanduser("~"), "Desktop", default_name),
                filters,
            )
            if path:
                with open(path, "wb") as f:
                    f.write(labels[0].raw_bytes)
                self.main_window.footer_status.setText(f"已保存到: {path}")
        else:
            folder = QFileDialog.getExistingDirectory(
                self, "选择保存文件夹",
                os.path.join(os.path.expanduser("~"), "Desktop"),
            )
            if folder:
                for i, lbl in enumerate(labels):
                    p = os.path.join(folder, f"{base}_{ts}_{i+1}.{ext}")
                    with open(p, "wb") as f:
                        f.write(lbl.raw_bytes)
                self.main_window.footer_status.setText(f"已保存 {len(labels)} 个到: {folder}")

    def _on_cancel(self):
        result = self.main_window.scheduler.cancel(self.task.id)
        if result == "running":
            self.main_window._show_transient_status(f"#{self.task.id} 生成中无法取消，请等待完成", 3000)
        elif result == "removed":
            self.main_window._show_transient_status(f"#{self.task.id} 已从队列移除", 2000)
            self.main_window._refresh_task_views()

    def _on_retry(self):
        if not self.main_window.scheduler.retry(self.task.id):
            QMessageBox.warning(self, "提示", "队列已满或无法重试")


class ClickableLabel(QLabel):
    def __init__(self, raw_bytes, file_ext="png", is_video=False, parent=None, file_path=None, skip_thumb=False):
        super().__init__(parent)
        self._raw_bytes = raw_bytes
        self.file_path = file_path
        self.file_ext = file_ext
        self.is_video = is_video
        self.checked = False
        self.setObjectName("thumbLabel")
        self.setCursor(Qt.PointingHandCursor)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(150, 150)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        if is_video and not skip_thumb and raw_bytes is not None:
            png_bytes = extract_video_first_frame_bytes(raw_bytes)
            if png_bytes:
                qimg = QImage.fromData(png_bytes)
                if not qimg.isNull():
                    pm = QPixmap.fromImage(qimg).scaled(150, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    self.setPixmap(pm)

    @property
    def raw_bytes(self):
        if self._raw_bytes is None and self.file_path and os.path.exists(self.file_path):
            try:
                with open(self.file_path, "rb") as f:
                    self._raw_bytes = f.read()
            except Exception:
                return b""
        return self._raw_bytes or b""

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.checked = not self.checked
            self._update_border()
        super().mousePressEvent(event)

    def _update_border(self):
        if hasattr(self, '_vid_style_base'):
            if self.checked:
                self.setStyleSheet(self._vid_style_checked)
            else:
                self.setStyleSheet(self._vid_style_base)
        else:
            if self.checked:
                self.setStyleSheet("border: 3px solid #7dd3fc; border-radius: 8px;")
            else:
                self.setStyleSheet("border: none;")

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            dlg = PreviewDialog(self.raw_bytes, self.file_ext, self.is_video, self.window())
            dlg.exec_()

    def _show_menu(self, pos):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #141c2e; border: 1px solid rgba(125,211,252,0.15); color: #e0e8f0; padding: 4px; }
            QMenu::item { padding: 6px 20px; }
            QMenu::item:selected { background-color: #2d3748; }
        """)
        save_action = menu.addAction("保存")
        action = menu.exec_(self.mapToGlobal(pos))
        if action == save_action:
            self._save_file()

    def _save_file(self):
        if self.is_video:
            name = f"sora_video_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
            filters = "MP4 视频 (*.mp4);;所有文件 (*)"
        else:
            name = f"gpt_image_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{self.file_ext}"
            if self.file_ext == "png":
                filters = "PNG 图片 (*.png);;JPEG 图片 (*.jpg);;所有文件 (*)"
            else:
                filters = "JPEG 图片 (*.jpg);;PNG 图片 (*.png);;所有文件 (*)"
        path, _ = QFileDialog.getSaveFileName(
            self, "保存文件",
            os.path.join(os.path.expanduser("~"), "Desktop", name),
            filters
        )
        if path:
            with open(path, "wb") as f:
                f.write(self.raw_bytes)


class PreviewDialog(QDialog):
    def __init__(self, raw_bytes, file_ext="png", is_video=False, parent=None):
        super().__init__(parent)
        self.raw_bytes = raw_bytes
        self.file_ext = file_ext
        self.is_video = is_video
        self.setWindowTitle("预览")
        self.setMinimumSize(600, 500)
        self.resize(900, 700)
        self.setStyleSheet("QDialog { background-color: #0a0e1a; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tmp_video_path = None
        self._media_player = None
        self._video_widget = None
        self._play_btn = None
        self._pos_slider = None
        self._slider_user_dragging = False

        if is_video:
            self._build_video_player(layout)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet("QScrollArea { border: none; background: #0a0e1a; }")
            img_label = QLabel()
            img_label.setAlignment(Qt.AlignCenter)
            img_label.setStyleSheet("background: transparent;")
            qimg = QImage.fromData(raw_bytes)
            if not qimg.isNull():
                pixmap = QPixmap.fromImage(qimg)
                screen_w = self.width() - 40
                if pixmap.width() > screen_w:
                    pixmap = pixmap.scaledToWidth(screen_w, Qt.SmoothTransformation)
                img_label.setPixmap(pixmap)
            scroll.setWidget(img_label)
            layout.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(16, 12, 16, 12)
        btn_row.addStretch()
        save_btn = QPushButton("保存")
        save_btn.setObjectName("refBtn")
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("refBtn")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _build_video_player(self, layout):
        # 使用 ffpyplayer 实现视频播放（内置 ffmpeg，支持音视频同步）
        try:
            from ffpyplayer.player import MediaPlayer
        except Exception as e:
            fallback = QLabel(f"需要安装 ffpyplayer: pip install ffpyplayer\n{e}")
            fallback.setAlignment(Qt.AlignCenter)
            fallback.setStyleSheet("color:#e0e8f0; font-size:14px; background: transparent;")
            layout.addWidget(fallback, 1)
            return

        try:
            tmp_dir = os.path.join(_APP_DIR, "history", "_preview_tmp")
            os.makedirs(tmp_dir, exist_ok=True)
            tmp_path = os.path.join(tmp_dir, f"preview_{datetime.now().strftime('%H%M%S_%f')}.mp4")
            with open(tmp_path, "wb") as f:
                f.write(self.raw_bytes)
            self._tmp_video_path = tmp_path
        except Exception as e:
            fallback = QLabel(f"临时文件写入失败: {e}")
            fallback.setAlignment(Qt.AlignCenter)
            fallback.setStyleSheet("color:#e0e8f0; font-size:14px; background: transparent;")
            layout.addWidget(fallback, 1)
            return

        # 初始化 ffpyplayer 状态
        self._ff_player = None
        self._ff_is_playing = False
        self._ff_duration = 0
        self._ff_current_pos = 0

        # 视频显示标签
        self._video_label = QLabel()
        self._video_label.setAlignment(Qt.AlignCenter)
        self._video_label.setStyleSheet("background:#000;")
        self._video_label.setScaledContents(False)
        layout.addWidget(self._video_label, 1)

        # 播放定时器（用于刷新视频帧）
        self._ff_timer = QTimer(self)
        self._ff_timer.timeout.connect(self._ff_refresh_frame)

        # 控制栏
        ctrl_row = QHBoxLayout()
        ctrl_row.setContentsMargins(16, 6, 16, 0)
        ctrl_row.setSpacing(10)

        self._play_btn = QPushButton("▶ 播放")
        self._play_btn.setObjectName("refBtn")
        self._play_btn.setCursor(Qt.PointingHandCursor)
        self._play_btn.setFixedWidth(96)
        self._play_btn.clicked.connect(self._ff_toggle_play)
        ctrl_row.addWidget(self._play_btn)

        self._pos_slider = QSlider(Qt.Horizontal)
        self._pos_slider.setRange(0, 1000)
        self._pos_slider.sliderPressed.connect(self._on_slider_press)
        self._pos_slider.sliderReleased.connect(self._ff_on_slider_release)
        ctrl_row.addWidget(self._pos_slider, 1)

        self._time_label = QLabel("00:00 / 00:00")
        self._time_label.setStyleSheet("color:#94a3b8; font-size:11px; font-family:Consolas;")
        ctrl_row.addWidget(self._time_label)

        layout.addLayout(ctrl_row)

        # 初始化 ffpyplayer（暂停状态）
        try:
            self._ff_player = MediaPlayer(self._tmp_video_path, ff_opts={'paused': True, 'sync': 'audio'})
            # 等待获取元数据
            import time as _time
            for _ in range(30):
                metadata = self._ff_player.get_metadata()
                if metadata.get('duration'):
                    self._ff_duration = metadata['duration']
                    break
                _time.sleep(0.05)
        except Exception as e:
            fallback = QLabel(f"播放器初始化失败: {e}")
            fallback.setAlignment(Qt.AlignCenter)
            fallback.setStyleSheet("color:#e0e8f0; font-size:14px; background: transparent;")
            layout.addWidget(fallback, 1)
            return

        # 启动定时器持续刷新
        self._ff_timer.start(10)  # 初始间隔，会根据 val 动态调整

    def _ff_refresh_frame(self):
        """刷新视频帧（由定时器调用）"""
        if not hasattr(self, "_ff_player") or not self._ff_player:
            return

        try:
            frame, val = self._ff_player.get_frame()

            if val == 'eof':
                # 播放结束
                self._ff_is_playing = False
                if hasattr(self, "_play_btn") and self._play_btn:
                    self._play_btn.setText("▶ 播放")
                # 重置到开头
                try:
                    self._ff_player.seek(0, relative=False)
                    self._ff_player.set_pause(True)
                except Exception:
                    pass
                return

            # 只在拿到有效帧时更新画面
            if frame is not None and val != 'paused':
                img, t = frame
                # 获取图像数据
                w, h = img.get_size()
                img_data = bytes(img.to_bytearray()[0])

                # 创建 QImage（ffpyplayer 默认输出 RGB24）
                qt_image = QImage(img_data, w, h, w * 3, QImage.Format_RGB888)

                # 缩放显示
                if hasattr(self, "_video_label") and self._video_label:
                    pixmap = QPixmap.fromImage(qt_image)
                    scaled_pixmap = pixmap.scaled(
                        self._video_label.size(),
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation
                    )
                    self._video_label.setPixmap(scaled_pixmap)

            # 根据 val 调整下次刷新时间（val 是建议的下次刷新延迟）
            if isinstance(val, (int, float)) and val > 0:
                # 至少 1ms，最多 50ms
                next_delay = max(1, min(50, int(val * 1000)))
                self._ff_timer.setInterval(next_delay)
            else:
                self._ff_timer.setInterval(10)

            # 始终更新进度
            try:
                pts = self._ff_player.get_pts()
                if pts is not None:
                    self._ff_current_pos = pts
                    if not self._slider_user_dragging and self._ff_duration > 0:
                        slider_value = int((pts / self._ff_duration) * 1000)
                        self._pos_slider.setValue(slider_value)
                    self._ff_update_time_label()
            except Exception:
                pass
        except Exception as e:
            pass

    def _ff_toggle_play(self):
        """切换播放/暂停"""
        if not self._ff_player:
            return

        if self._ff_is_playing:
            self._ff_is_playing = False
            self._ff_player.set_pause(True)
            self._play_btn.setText("▶ 播放")
        else:
            self._ff_is_playing = True
            self._ff_player.set_pause(False)
            self._play_btn.setText("⏸ 暂停")

    def _ff_on_slider_release(self):
        """滑块释放时跳转"""
        if self._pos_slider and self._ff_player and self._ff_duration > 0:
            target_pos = (self._pos_slider.value() / 1000) * self._ff_duration
            try:
                self._ff_player.seek(target_pos, relative=False, accurate=True)
            except Exception:
                pass
        self._slider_user_dragging = False

    def _ff_update_time_label(self):
        """更新时间标签"""
        if not hasattr(self, "_time_label"):
            return

        def fmt(sec):
            sec = max(0, int(sec))
            return f"{sec // 60:02d}:{sec % 60:02d}"

        self._time_label.setText(f"{fmt(self._ff_current_pos)} / {fmt(self._ff_duration)}")

    def _on_slider_press(self):
        self._slider_user_dragging = True

    def closeEvent(self, event):
        # 停止 ffpyplayer 播放器
        try:
            if hasattr(self, "_ff_timer") and self._ff_timer:
                self._ff_timer.stop()
            if hasattr(self, "_ff_player") and self._ff_player:
                self._ff_player.close_player()
                self._ff_player = None
        except Exception:
            pass

        # 清理临时文件
        if self._tmp_video_path and os.path.exists(self._tmp_video_path):
            try:
                os.remove(self._tmp_video_path)
            except Exception:
                pass
        super().closeEvent(event)

    def _save(self):
        if self.is_video:
            name = f"sora_video_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
            filters = "MP4 视频 (*.mp4);;所有文件 (*)"
        else:
            name = f"gpt_image_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{self.file_ext}"
            if self.file_ext == "png":
                filters = "PNG 图片 (*.png);;JPEG 图片 (*.jpg);;所有文件 (*)"
            else:
                filters = "JPEG 图片 (*.jpg);;PNG 图片 (*.png);;所有文件 (*)"
        path, _ = QFileDialog.getSaveFileName(
            self, "保存文件",
            os.path.join(os.path.expanduser("~"), "Desktop", name),
            filters
        )
        if path:
            with open(path, "wb") as f:
                f.write(self.raw_bytes)


if getattr(sys, 'frozen', False):
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))

HISTORY_DIR = os.path.join(_APP_DIR, "history")
HISTORY_JSON = os.path.join(HISTORY_DIR, "history.json")
UI_SETTINGS_JSON = os.path.join(_APP_DIR, "ui_settings.json")
API_KEY_FILE = os.path.join(_APP_DIR, "api_key.dat")

DEFAULT_UI_SETTINGS = {"font_scale": 100, "brightness": 0, "theme": DEFAULT_THEME, "concurrency": 3}


def save_api_key(key):
    """保存 API Key 到本地文件，使用 base64 编码（仅防止明文窥视，非加密）"""
    try:
        encoded = base64.b64encode(key.encode("utf-8")).decode("ascii")
        with open(API_KEY_FILE, "w", encoding="utf-8") as f:
            f.write(encoded)
    except Exception:
        pass


def load_api_key():
    if not os.path.exists(API_KEY_FILE):
        return ""
    try:
        with open(API_KEY_FILE, "r", encoding="utf-8") as f:
            encoded = f.read().strip()
        if not encoded:
            return ""
        return base64.b64decode(encoded.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def clear_api_key():
    try:
        if os.path.exists(API_KEY_FILE):
            os.remove(API_KEY_FILE)
    except Exception:
        pass


def load_ui_settings():
    if os.path.exists(UI_SETTINGS_JSON):
        try:
            with open(UI_SETTINGS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            theme = data.get("theme", DEFAULT_THEME)
            if theme not in THEMES:
                theme = DEFAULT_THEME
            try:
                concurrency = int(data.get("concurrency", 3))
            except (TypeError, ValueError):
                concurrency = 3
            concurrency = max(1, min(5, concurrency))
            return {
                "font_scale": int(data.get("font_scale", 100)),
                "brightness": int(data.get("brightness", 0)),
                "theme": theme,
                "concurrency": concurrency,
            }
        except Exception:
            pass
    return dict(DEFAULT_UI_SETTINGS)


def save_ui_settings(settings):
    try:
        with open(UI_SETTINGS_JSON, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _shift_channel(v, delta):
    return max(0, min(255, int(v) + delta))


def _shift_hex_color(match, delta):
    s = match.group(0)
    h = s[1:]
    if len(h) == 3:
        r, g, b = (int(c * 2, 16) for c in h)
    elif len(h) == 6:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    else:
        return s
    r, g, b = _shift_channel(r, delta), _shift_channel(g, delta), _shift_channel(b, delta)
    return f"#{r:02x}{g:02x}{b:02x}"


def _shift_rgba(match, delta):
    parts = [p.strip() for p in match.group(1).split(",")]
    if len(parts) < 3:
        return match.group(0)
    r = _shift_channel(parts[0], delta)
    g = _shift_channel(parts[1], delta)
    b = _shift_channel(parts[2], delta)
    if len(parts) == 3:
        return f"rgb({r}, {g}, {b})"
    return f"rgba({r}, {g}, {b}, {parts[3]})"


def apply_ui_adjustments(qss, font_scale=100, brightness=0):
    """缩放 QSS 中的 font-size，并整体偏移颜色亮度。
    font_scale: 80~140 百分比；brightness: -30~+40 通道偏移"""
    if font_scale != 100:
        def _scale(m):
            px = int(m.group(1))
            return f"font-size: {max(8, round(px * font_scale / 100))}px"
        qss = re.sub(r"font-size:\s*(\d+)px", _scale, qss)
    if brightness != 0:
        qss = re.sub(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b",
                     lambda m: _shift_hex_color(m, brightness), qss)
        qss = re.sub(r"rgba?\(([^)]+)\)",
                     lambda m: _shift_rgba(m, brightness), qss)
    return qss


def shift_inline_bg(color_hex, brightness):
    if brightness == 0:
        return color_hex
    return _shift_hex_color(re.match(r"#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3}", color_hex), brightness)


def ensure_history_dir():
    os.makedirs(HISTORY_DIR, exist_ok=True)


def load_history():
    ensure_history_dir()
    if os.path.exists(HISTORY_JSON):
        try:
            with open(HISTORY_JSON, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_history(records):
    ensure_history_dir()
    with open(HISTORY_JSON, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def add_history_record(prompt, model, quality, ratio, img_paths, kind="image"):
    records = load_history()
    records.insert(0, {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "prompt": prompt,
        "model": model,
        "quality": quality,
        "ratio": ratio,
        "kind": kind,
        "images": img_paths,
    })
    if len(records) > 200:
        records = records[:200]
    save_history(records)


def add_video_history_record(prompt, model, orientation, duration, video_paths):
    add_history_record(
        prompt=prompt,
        model=model,
        quality=f"{duration}s",
        ratio=orientation,
        img_paths=video_paths,
        kind="video",
    )


SETTINGS_DIALOG_STYLE_TEMPLATE = """
QDialog#settingsDlg {
    background-color: $bg_main;
    border: 1px solid $accent_border;
}
QLabel#settingsTitle {
    color: $accent;
    font-size: 18px;
    font-weight: 700;
}
QLabel#settingsHint {
    color: $text_muted;
    font-size: 12px;
}
QLabel#settingsRow {
    color: $text_primary;
    font-size: 14px;
    font-weight: 600;
}
QLabel#settingsValue {
    color: $accent;
    font-size: 13px;
    font-family: "Consolas", "Courier New";
    min-width: 56px;
}
QComboBox#themeCombo {
    background-color: $bg_input;
    border: 1px solid $border_soft;
    border-radius: 8px;
    padding: 8px 12px;
    color: $text_input;
    min-width: 180px;
    min-height: 28px;
    font-size: 13px;
}
QComboBox#themeCombo:hover {
    border-color: $accent_border;
}
QComboBox#themeCombo::drop-down {
    border: none;
    width: 24px;
}
QComboBox#themeCombo::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid $down_arrow_color;
    margin-right: 8px;
}
QComboBox#themeCombo QAbstractItemView {
    background-color: $bg_combo_pop;
    color: $text_input;
    selection-background-color: $bg_combo_pop_item_sel;
    selection-color: $text_primary;
    border: 1px solid $border_soft;
    border-radius: 6px;
    padding: 4px;
    outline: none;
}
QComboBox#themeCombo QAbstractItemView::item {
    background-color: $bg_combo_pop;
    color: $text_input;
    padding: 8px 14px;
    min-height: 26px;
    border: none;
}
QComboBox#themeCombo QAbstractItemView::item:selected,
QComboBox#themeCombo QAbstractItemView::item:hover {
    background-color: $bg_combo_pop_item_sel;
    color: $text_primary;
}
QSlider::groove:horizontal {
    background: $accent_soft;
    height: 4px;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: $accent;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: $accent;
    width: 14px;
    height: 14px;
    margin: -6px 0;
    border-radius: 7px;
}
QPushButton#settingsApply {
    background-color: $accent_soft;
    border: 1px solid $accent_border;
    border-radius: 8px;
    color: $accent;
    font-size: 14px;
    font-weight: 700;
    padding: 8px 22px;
}
QPushButton#settingsApply:hover {
    background-color: $accent_hover;
}
QPushButton#settingsReset {
    background: transparent;
    border: 1px solid $border_soft;
    border-radius: 8px;
    color: $text_secondary;
    font-size: 14px;
    padding: 8px 18px;
}
QPushButton#settingsReset:hover {
    color: $text_primary;
    border-color: $accent_border;
}
"""


class SettingsDialog(QDialog):
    def __init__(self, parent, font_scale, brightness, theme, concurrency, on_change):
        super().__init__(parent)
        self.setObjectName("settingsDlg")
        self.setWindowTitle("界面设置")
        self.setStyleSheet(build_settings_dialog_style(theme))
        self.setFixedWidth(460)
        self._on_change = on_change
        self._initial = (font_scale, brightness, theme, concurrency)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(14)

        title = QLabel("界面设置")
        title.setObjectName("settingsTitle")
        layout.addWidget(title)

        hint = QLabel("拖动滑条即时预览，确认后点击「应用」保存。")
        hint.setObjectName("settingsHint")
        layout.addWidget(hint)
        layout.addSpacing(6)

        theme_row = QHBoxLayout()
        theme_label = QLabel("主题")
        theme_label.setObjectName("settingsRow")
        theme_label.setFixedWidth(80)
        self.theme_combo = QComboBox()
        self.theme_combo.setObjectName("themeCombo")
        self.theme_combo.wheelEvent = lambda e: e.ignore()
        self._theme_keys = list(THEMES.keys())
        for k in self._theme_keys:
            self.theme_combo.addItem(THEMES[k]["name"], k)
        try:
            cur_idx = self._theme_keys.index(theme)
        except ValueError:
            cur_idx = self._theme_keys.index(DEFAULT_THEME)
        self.theme_combo.setCurrentIndex(cur_idx)
        self.theme_combo.currentIndexChanged.connect(self._on_theme)
        theme_row.addWidget(theme_label)
        theme_row.addWidget(self.theme_combo, 1)
        layout.addLayout(theme_row)

        font_row = QHBoxLayout()
        font_label = QLabel("字体大小")
        font_label.setObjectName("settingsRow")
        font_label.setFixedWidth(80)
        self.font_value = QLabel(f"{font_scale}%")
        self.font_value.setObjectName("settingsValue")
        self.font_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.font_slider = QSlider(Qt.Horizontal)
        self.font_slider.setRange(80, 140)
        self.font_slider.setSingleStep(5)
        self.font_slider.setPageStep(10)
        self.font_slider.setValue(font_scale)
        self.font_slider.valueChanged.connect(self._on_font)
        font_row.addWidget(font_label)
        font_row.addWidget(self.font_slider, 1)
        font_row.addWidget(self.font_value)
        layout.addLayout(font_row)

        bri_row = QHBoxLayout()
        bri_label = QLabel("界面亮度")
        bri_label.setObjectName("settingsRow")
        bri_label.setFixedWidth(80)
        self.bri_value = QLabel(self._fmt_brightness(brightness))
        self.bri_value.setObjectName("settingsValue")
        self.bri_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.bri_slider = QSlider(Qt.Horizontal)
        self.bri_slider.setRange(-20, 40)
        self.bri_slider.setSingleStep(5)
        self.bri_slider.setPageStep(10)
        self.bri_slider.setValue(brightness)
        self.bri_slider.valueChanged.connect(self._on_brightness)
        bri_row.addWidget(bri_label)
        bri_row.addWidget(self.bri_slider, 1)
        bri_row.addWidget(self.bri_value)
        layout.addLayout(bri_row)

        conc_row = QHBoxLayout()
        conc_label = QLabel("并发任务数")
        conc_label.setObjectName("settingsRow")
        conc_label.setFixedWidth(80)
        self.conc_combo = QComboBox()
        self.conc_combo.setObjectName("themeCombo")
        self.conc_combo.wheelEvent = lambda e: e.ignore()
        for n in range(1, 6):
            self.conc_combo.addItem(str(n), n)
        try:
            cur_c = max(1, min(5, int(concurrency)))
        except (TypeError, ValueError):
            cur_c = 3
        self.conc_combo.setCurrentIndex(cur_c - 1)
        self.conc_combo.currentIndexChanged.connect(self._on_concurrency)
        conc_row.addWidget(conc_label)
        conc_row.addWidget(self.conc_combo, 1)
        layout.addLayout(conc_row)

        conc_hint = QLabel("图片 + 视频共享并发额度，可选 1-5")
        conc_hint.setObjectName("settingsHint")
        layout.addWidget(conc_hint)

        layout.addSpacing(8)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        reset_btn = QPushButton("恢复默认")
        reset_btn.setObjectName("settingsReset")
        reset_btn.setCursor(Qt.PointingHandCursor)
        reset_btn.clicked.connect(self._reset)
        apply_btn = QPushButton("应用")
        apply_btn.setObjectName("settingsApply")
        apply_btn.setCursor(Qt.PointingHandCursor)
        apply_btn.clicked.connect(self.accept)
        btn_row.addWidget(reset_btn)
        btn_row.addSpacing(8)
        btn_row.addWidget(apply_btn)
        layout.addLayout(btn_row)

    @staticmethod
    def _fmt_brightness(v):
        return f"{v:+d}" if v else "0"

    def _current_theme(self):
        i = self.theme_combo.currentIndex()
        if 0 <= i < len(self._theme_keys):
            return self._theme_keys[i]
        return DEFAULT_THEME

    def _on_theme(self, _idx):
        theme = self._current_theme()
        # 设置弹窗自身也跟着切换样式
        self.setStyleSheet(build_settings_dialog_style(theme))
        self._on_change(self.font_slider.value(), self.bri_slider.value(), theme, self._current_concurrency())

    def _on_font(self, v):
        v = (v // 5) * 5
        if v != self.font_slider.value():
            self.font_slider.blockSignals(True)
            self.font_slider.setValue(v)
            self.font_slider.blockSignals(False)
        self.font_value.setText(f"{v}%")
        self._on_change(v, self.bri_slider.value(), self._current_theme(), self._current_concurrency())

    def _on_brightness(self, v):
        v = (v // 5) * 5
        if v != self.bri_slider.value():
            self.bri_slider.blockSignals(True)
            self.bri_slider.setValue(v)
            self.bri_slider.blockSignals(False)
        self.bri_value.setText(self._fmt_brightness(v))
        self._on_change(self.font_slider.value(), v, self._current_theme(), self._current_concurrency())

    def _on_concurrency(self, _idx):
        self._on_change(self.font_slider.value(), self.bri_slider.value(), self._current_theme(), self._current_concurrency())

    def _current_concurrency(self):
        return int(self.conc_combo.currentData() or 3)

    def _reset(self):
        try:
            default_idx = self._theme_keys.index(DEFAULT_THEME)
        except ValueError:
            default_idx = 0
        self.theme_combo.setCurrentIndex(default_idx)
        self.font_slider.setValue(100)
        self.bri_slider.setValue(0)
        self.conc_combo.setCurrentIndex(2)

    def reject(self):
        # 取消时还原回弹出前的设置
        self._on_change(*self._initial)
        super().reject()

    def values(self):
        return self.font_slider.value(), self.bri_slider.value(), self._current_theme(), self._current_concurrency()


class MainWindow(QMainWindow):
    def __init__(self, api_key):
        super().__init__()
        self.api_key = api_key
        self.image_data_list = []
        self.video_data_list = []
        self.thread = None
        self.video_thread = None
        self._drag_pos = None
        self._img_ref_data_list = []
        self._vid_img_ref_data_list = []
        self._vid_video_ref_url_list = []
        self._vid_audio_ref_url_list = []
        self._current_prompt = ""
        self._current_model = ""
        self._current_quality = ""
        self._current_ratio = ""
        ui = load_ui_settings()
        self._font_scale = ui["font_scale"]
        self._brightness = ui["brightness"]
        self._theme = ui.get("theme", DEFAULT_THEME)
        self._concurrency = max(1, min(5, int(ui.get("concurrency", 3))))
        self._bg_pages = []
        self._balance_thread = None
        self.scheduler = TaskScheduler(self, max_concurrent=self._concurrency)
        self.scheduler.task_state_changed.connect(self._on_task_state_changed)
        self.scheduler.queue_changed.connect(self._refresh_status_bar)
        self._task_cards = {}
        self.init_ui()
        self._apply_ui_settings()
        self._refresh_task_views()
        self._refresh_status_bar()
        QTimer.singleShot(300, self.refresh_balance)

    def init_ui(self):
        self.setWindowTitle("Glacier AI")
        self.setMinimumSize(1100, 750)
        self.resize(1200, 800)
        self.setStyleSheet(build_main_style(self._theme))
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)

        logo_path = get_logo_path()
        if os.path.exists(logo_path):
            self.setWindowIcon(QIcon(logo_path))

        central = QWidget()
        central.setStyleSheet(f"background-color: {get_theme(self._theme)['bg_main']};")
        self._bg_pages.append(central)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)

        self._build_title_bar(root)

        body = QHBoxLayout()
        body.setSpacing(0)
        body.setContentsMargins(0, 0, 0, 0)

        self._build_side_nav(body)

        right_area = QVBoxLayout()
        right_area.setSpacing(0)
        right_area.setContentsMargins(0, 0, 0, 0)

        self.content_stack = QStackedWidget()
        self._build_image_page()
        self._build_video_page()
        self._build_history_page()
        right_area.addWidget(self.content_stack)

        self._build_footer(right_area)

        body.addLayout(right_area)
        root.addLayout(body)

    def _build_title_bar(self, parent_layout):
        bar = QFrame()
        bar.setObjectName("titleBar")
        bar.setFixedHeight(40)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 0, 0)
        layout.setSpacing(0)

        brand = QLabel("Glacier AI")
        brand.setObjectName("titleBrand")
        layout.addWidget(brand)

        sep = QFrame()
        sep.setFixedSize(1, 16)
        sep.setStyleSheet("background-color: " + get_theme(self._theme)['accent_border'] + ";")
        layout.addSpacing(12)
        layout.addWidget(sep)
        layout.addSpacing(12)

        sub = QLabel("New API - 图片视频生成器")
        sub.setObjectName("titleSub")
        layout.addWidget(sub)

        layout.addStretch()

        btn_min = QPushButton("—")
        btn_min.setObjectName("winBtn")
        btn_min.setFixedSize(46, 40)
        btn_min.setCursor(Qt.PointingHandCursor)
        btn_min.clicked.connect(self.showMinimized)
        layout.addWidget(btn_min)

        self._btn_max = QPushButton("☐")
        self._btn_max.setObjectName("winBtn")
        self._btn_max.setFixedSize(46, 40)
        self._btn_max.setCursor(Qt.PointingHandCursor)
        self._btn_max.clicked.connect(self._toggle_maximize)
        layout.addWidget(self._btn_max)

        btn_close = QPushButton("✕")
        btn_close.setObjectName("winBtnClose")
        btn_close.setFixedSize(46, 40)
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(self.close)
        layout.addWidget(btn_close)

        bar.mousePressEvent = self._title_mouse_press
        bar.mouseMoveEvent = self._title_mouse_move
        bar.mouseDoubleClickEvent = lambda e: self._toggle_maximize()

        parent_layout.addWidget(bar)

    def _title_mouse_press(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()

    def _title_mouse_move(self, event):
        if self._drag_pos and event.buttons() & Qt.LeftButton:
            if self.isMaximized():
                self.showNormal()
            self.move(event.globalPos() - self._drag_pos)

    def _toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _build_side_nav(self, parent_layout):
        nav = QFrame()
        nav.setObjectName("sideNav")
        nav.setFixedWidth(200)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(0)

        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(20, 20, 20, 12)
        header_layout.setSpacing(2)
        title = QLabel("GLACIER ENGINE")
        title.setObjectName("navTitle")
        header_layout.addWidget(title)
        ver = QLabel("V3.3 Stable")
        ver.setObjectName("navVersion")
        ver.setStyleSheet("font-size: 16px; font-weight: bold; color: " + get_theme(self._theme)['version_color'] + ";")
        self._version_label = ver
        header_layout.addWidget(ver)
        nav_layout.addWidget(header)

        nav_layout.addSpacing(4)

        self.nav_img_btn = QPushButton("  ⚡  图片生成")
        self.nav_img_btn.setObjectName("navBtnActive")
        self.nav_img_btn.setCursor(Qt.PointingHandCursor)
        self.nav_img_btn.clicked.connect(lambda: self._switch_nav(0))
        nav_layout.addWidget(self.nav_img_btn)

        self.nav_vid_btn = QPushButton("  🎬  视频生成")
        self.nav_vid_btn.setObjectName("navBtn")
        self.nav_vid_btn.setCursor(Qt.PointingHandCursor)
        self.nav_vid_btn.clicked.connect(lambda: self._switch_nav(1))
        nav_layout.addWidget(self.nav_vid_btn)

        self.nav_hist_btn = QPushButton("  📋  历史记录")
        self.nav_hist_btn.setObjectName("navBtn")
        self.nav_hist_btn.setCursor(Qt.PointingHandCursor)
        self.nav_hist_btn.clicked.connect(lambda: self._switch_nav(2))
        nav_layout.addWidget(self.nav_hist_btn)

        nav_layout.addStretch()

        self.nav_settings_btn = QPushButton("  ⚙  设置")
        self.nav_settings_btn.setObjectName("navBtn")
        self.nav_settings_btn.setCursor(Qt.PointingHandCursor)
        self.nav_settings_btn.clicked.connect(self._open_settings)
        nav_layout.addWidget(self.nav_settings_btn)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(16, 12, 16, 16)
        bottom_layout.setSpacing(6)

        _t = get_theme(self._theme)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background-color: " + _t['border_top_alpha'] + ";")
        bottom_layout.addWidget(sep)
        bottom_layout.addSpacing(4)

        balance_row = QHBoxLayout()
        balance_row.setSpacing(6)
        balance_row.setContentsMargins(0, 0, 0, 0)
        self.balance_label = QLabel("余额: 加载中...")
        self.balance_label.setObjectName("navBalanceLabel")
        self.balance_label.setStyleSheet(
            f"color: {_t['accent']}; font-size: 16px; font-weight: 700; background: transparent;"
        )
        balance_row.addWidget(self.balance_label, 1)

        self.balance_refresh_btn = QPushButton("⟳")
        self.balance_refresh_btn.setObjectName("balanceRefreshBtn")
        self.balance_refresh_btn.setCursor(Qt.PointingHandCursor)
        self.balance_refresh_btn.setFixedSize(30, 28)
        self.balance_refresh_btn.setToolTip("刷新余额")
        self.balance_refresh_btn.setStyleSheet(
            "QPushButton { background: " + _t['accent_softer'] + "; color: " + _t['accent'] + ";"
            " border: 1px solid " + _t['accent_border'] + "; border-radius: 4px;"
            " font-size: 16px; padding: 0; }"
            "QPushButton:hover { background: " + _t['accent_soft'] + "; }"
            "QPushButton:disabled { color: " + _t['text_dim'] + "; border-color: " + _t['border_soft'] + "; }"
        )
        self.balance_refresh_btn.clicked.connect(self.refresh_balance)
        balance_row.addWidget(self.balance_refresh_btn)
        bottom_layout.addLayout(balance_row)

        switch_btn = QPushButton("切换账号")
        switch_btn.setCursor(Qt.PointingHandCursor)
        switch_btn.setStyleSheet(
            "QPushButton { background: transparent; color: " + _t['text_secondary'] + ";"
            " border: none; font-size: 13px; padding: 4px 2px; text-align: left; }"
            "QPushButton:hover { color: #cbd5e1; }"
        )
        switch_btn.clicked.connect(self._switch_account)
        bottom_layout.addWidget(switch_btn)

        key_display = self.api_key[:8] + "..." + self.api_key[-4:]
        key_label = QLabel(f"Key: {key_display}")
        key_label.setObjectName("navKeyLabel")
        key_label.setStyleSheet("color: " + _t['text_muted'] + "; font-size: 11px; background: transparent;")
        self._key_label = key_label
        bottom_layout.addWidget(key_label)

        nav_layout.addWidget(bottom)
        parent_layout.addWidget(nav)

    def _apply_ui_settings(self):
        base_qss = build_main_style(self._theme)
        adjusted = apply_ui_adjustments(base_qss, self._font_scale, self._brightness)
        self.setStyleSheet(adjusted)
        theme = get_theme(self._theme)
        page_bg = shift_inline_bg(theme["bg_main"], self._brightness)
        for w in self._bg_pages:
            try:
                w.setStyleSheet(f"background-color: {page_bg};")
            except Exception:
                pass
        ver_label = getattr(self, "_version_label", None)
        if ver_label is not None:
            ver_label.setStyleSheet(
                "font-size: 16px; font-weight: bold; color: " + theme['version_color'] + ";"
            )
        bal_label = getattr(self, "balance_label", None)
        if bal_label is not None:
            bal_label.setStyleSheet(
                f"color: {theme['accent']}; font-size: 16px; font-weight: 700; background: transparent;"
            )
        key_label = getattr(self, "_key_label", None)
        if key_label is not None:
            key_label.setStyleSheet(
                "color: " + theme['text_muted'] + "; font-size: 11px; background: transparent;"
            )
        refresh_btn = getattr(self, "balance_refresh_btn", None)
        if refresh_btn is not None:
            refresh_btn.setStyleSheet(
                "QPushButton { background: " + theme['accent_softer'] + "; color: " + theme['accent'] + ";"
                " border: 1px solid " + theme['accent_border'] + "; border-radius: 4px;"
                " font-size: 16px; padding: 0; }"
                "QPushButton:hover { background: " + theme['accent_soft'] + "; }"
                "QPushButton:disabled { color: " + theme['text_dim'] + "; border-color: " + theme['border_soft'] + "; }"
            )

    def _preview_ui_settings(self, font_scale, brightness, theme=None, concurrency=None):
        self._font_scale = font_scale
        self._brightness = brightness
        if theme is not None and theme in THEMES:
            self._theme = theme
        if concurrency is not None:
            try:
                self._concurrency = max(1, min(5, int(concurrency)))
            except (TypeError, ValueError):
                pass
        self._apply_ui_settings()

    def _open_settings(self):
        before = (self._font_scale, self._brightness, self._theme, self._concurrency)
        dlg = SettingsDialog(self, self._font_scale, self._brightness, self._theme, self._concurrency, self._preview_ui_settings)
        if dlg.exec_() == QDialog.Accepted:
            fs, bri, theme, conc = dlg.values()
            self._font_scale = fs
            self._brightness = bri
            self._theme = theme
            self._concurrency = conc
            self._apply_ui_settings()
            self.scheduler.set_max_concurrent(conc)
            self._refresh_status_bar()
            save_ui_settings({"font_scale": fs, "brightness": bri, "theme": theme, "concurrency": conc})
        else:
            self._font_scale, self._brightness, self._theme, self._concurrency = before
            self._apply_ui_settings()

    def refresh_balance(self):
        if self._balance_thread is not None and self._balance_thread.isRunning():
            return
        self.balance_label.setText("余额: 查询中...")
        self.balance_refresh_btn.setEnabled(False)
        thread = BalanceQueryThread(self.api_key)
        thread.finished_ok.connect(self._on_balance_ok)
        thread.failed.connect(self._on_balance_failed)
        thread.finished.connect(lambda: self.balance_refresh_btn.setEnabled(True))
        self._balance_thread = thread
        thread.start()

    def _on_balance_ok(self, amount):
        self.balance_label.setText(f"余额: ¥ {amount:.2f}")
        self.balance_label.setToolTip(f"账户剩余额度 ¥{amount:.4f}（点击 ⟳ 刷新）")

    def _on_balance_failed(self, msg):
        self.balance_label.setText("余额: 查询失败")
        self.balance_label.setToolTip(f"查询失败: {msg}")

    def _switch_account(self):
        if self.scheduler.has_unfinished():
            QMessageBox.information(
                self, "切换账号",
                f"当前还有 {self.scheduler.total_count()} 个任务未完成，请先取消或等待完成后再切换账号。",
            )
            return
        mb = QMessageBox(self)
        mb.setWindowTitle("切换账号")
        mb.setText("返回登录窗口选择/输入其他 API Key？")
        mb.setIcon(QMessageBox.Question)
        yes_btn = mb.addButton("是", QMessageBox.YesRole)
        no_btn = mb.addButton("否", QMessageBox.NoRole)
        mb.setDefaultButton(no_btn)
        mb.setStyleSheet(
            "QMessageBox { background-color: #ffffff; min-width: 380px; }"
            "QMessageBox QLabel { color: #000000; font-size: 17px; padding: 8px 4px; }"
        )
        from PyQt5.QtGui import QPalette
        for btn in (yes_btn, no_btn):
            btn.setStyleSheet(
                "QPushButton { color: #000000; background-color: #f1f5f9;"
                " border: 1px solid #94a3b8; border-radius: 4px;"
                " padding: 8px 26px; min-width: 96px; font-size: 16px; font-weight: 700; }"
                "QPushButton:hover { background-color: #cbd5e1; color: #000000; }"
                "QPushButton:pressed { background-color: #94a3b8; color: #000000; }"
            )
            pal = btn.palette()
            pal.setColor(QPalette.ButtonText, QColor("#000000"))
            pal.setColor(QPalette.WindowText, QColor("#000000"))
            pal.setColor(QPalette.Button, QColor("#f1f5f9"))
            btn.setPalette(pal)
            f = btn.font()
            f.setBold(True)
            f.setPointSize(12)
            btn.setFont(f)
            btn.setAutoFillBackground(True)
        mb.exec_()
        if mb.clickedButton() is yes_btn:
            QApplication.exit(1001)

    def closeEvent(self, event):
        if self.scheduler.has_unfinished():
            n = self.scheduler.total_count()
            mb = QMessageBox(self)
            mb.setWindowTitle("确认关闭")
            mb.setIcon(QMessageBox.Question)
            mb.setText(f"还有 {n} 个任务未完成，确认关闭？")
            yes_btn = mb.addButton("关闭", QMessageBox.YesRole)
            no_btn = mb.addButton("继续", QMessageBox.NoRole)
            mb.setDefaultButton(no_btn)
            mb.exec_()
            if mb.clickedButton() is not yes_btn:
                event.ignore()
                return
        super().closeEvent(event)

    def _switch_nav(self, index):
        self.content_stack.setCurrentIndex(index)
        btns = [self.nav_img_btn, self.nav_vid_btn, self.nav_hist_btn]
        for i, btn in enumerate(btns):
            btn.setObjectName("navBtnActive" if i == index else "navBtn")
            btn.setStyle(btn.style())
        if index == 2:
            self._refresh_history_page()

    def _build_image_page(self):
        page = QWidget()
        page.setStyleSheet(f"background-color: {get_theme(self._theme)['bg_main']};")
        self._bg_pages.append(page)
        layout = QHBoxLayout(page)
        layout.setSpacing(24)
        layout.setContentsMargins(24, 24, 24, 0)

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_inner = QWidget()
        left_inner.setStyleSheet("background: transparent;")
        left_layout = QVBoxLayout(left_inner)
        left_layout.setSpacing(16)
        left_layout.setContentsMargins(0, 0, 0, 0)

        prompt_card = QFrame()
        prompt_card.setObjectName("glassPanel")
        pc_layout = QVBoxLayout(prompt_card)
        pc_layout.setSpacing(10)
        pc_layout.setContentsMargins(20, 20, 20, 20)

        ph = QHBoxLayout()
        pl = QLabel("提示词 Prompt")
        pl.setObjectName("sectionLabel")
        ph.addWidget(pl)
        ph.addStretch()
        badge = QLabel("ENHANCED")
        badge.setObjectName("sectionBadge")
        ph.addWidget(badge)
        pc_layout.addLayout(ph)

        self.prompt_input = PlainPasteTextEdit()
        self.prompt_input.setObjectName("promptInput")
        self.prompt_input.setPlaceholderText("描述你想要生成的图片，例如：一只在月球上弹吉他的猫，赛博朋克风格...")
        self.prompt_input.setMinimumHeight(120)
        self.prompt_input.setMaximumHeight(160)
        self.prompt_input.setAcceptRichText(False)
        pc_layout.addWidget(self.prompt_input)
        left_layout.addWidget(prompt_card)

        ref_card = QFrame()
        ref_card.setObjectName("glassPanel")
        ref_layout = QVBoxLayout(ref_card)
        ref_layout.setSpacing(10)
        ref_layout.setContentsMargins(20, 20, 20, 20)

        ref_title = QLabel("参考图片（可选）")
        ref_title.setObjectName("sectionLabel")
        ref_layout.addWidget(ref_title)

        self.img_ref_hint = QLabel("提示：参考图最多 6 张")
        self.img_ref_hint.setStyleSheet("color: #f59e0b; font-size: 11px; background: transparent;")
        ref_layout.addWidget(self.img_ref_hint)

        self.img_ref_url = QLineEdit()
        self.img_ref_url.setObjectName("refUrlInput")
        self.img_ref_url.setPlaceholderText("粘贴图片 URL 或选择本地图片...")
        ref_layout.addWidget(self.img_ref_url)

        btn_row = QHBoxLayout()
        self.img_ref_pick_btn = QPushButton("选择本地图片")
        self.img_ref_pick_btn.setObjectName("refBtn")
        self.img_ref_pick_btn.setCursor(Qt.PointingHandCursor)
        self.img_ref_pick_btn.clicked.connect(self.on_pick_img_ref)
        btn_row.addWidget(self.img_ref_pick_btn)
        btn_row.addStretch()
        self.img_ref_clear_btn = QPushButton("清除")
        self.img_ref_clear_btn.setObjectName("refBtn")
        self.img_ref_clear_btn.setCursor(Qt.PointingHandCursor)
        self.img_ref_clear_btn.clicked.connect(self.on_clear_img_ref)
        btn_row.addWidget(self.img_ref_clear_btn)
        ref_layout.addLayout(btn_row)

        self.img_ref_preview_layout = QGridLayout()
        self.img_ref_preview_layout.setSpacing(6)
        self.img_ref_preview_layout.setAlignment(Qt.AlignLeft)
        ref_layout.addLayout(self.img_ref_preview_layout)

        self.img_ref_count_label = QLabel("")
        self.img_ref_count_label.setStyleSheet("color: " + get_theme(self._theme)['text_muted'] + "; font-size: 11px; background: transparent;")
        ref_layout.addWidget(self.img_ref_count_label)

        left_layout.addWidget(ref_card)

        param_card = QFrame()
        param_card.setObjectName("glassPanel")
        pm_layout = QVBoxLayout(param_card)
        pm_layout.setSpacing(14)
        pm_layout.setContentsMargins(20, 20, 20, 20)

        pm_title = QLabel("参数设置")
        pm_title.setObjectName("sectionLabel")
        pm_layout.addWidget(pm_title)

        grid = QGridLayout()
        grid.setSpacing(12)

        l1 = QLabel("模型")
        l1.setObjectName("paramLabel")
        grid.addWidget(l1, 0, 0)
        self.model_combo = QComboBox()
        setup_combo(self.model_combo)
        self.model_combo.addItems(["gpt-image-2", "gpt-image-2pro", "nano-banana-2", "nano-banana-pro"])
        self.model_combo.currentTextChanged.connect(self.on_model_changed)
        grid.addWidget(self.model_combo, 1, 0)

        l2 = QLabel("比例")
        l2.setObjectName("paramLabel")
        grid.addWidget(l2, 0, 1)
        self.size_combo = QComboBox()
        setup_combo(self.size_combo)
        self.size_combo.addItems(RATIO_LIST)
        grid.addWidget(self.size_combo, 1, 1)

        l3 = QLabel("质量")
        l3.setObjectName("paramLabel")
        grid.addWidget(l3, 2, 0)
        self.quality_combo = QComboBox()
        setup_combo(self.quality_combo)
        self.quality_combo.addItems(MODEL_QUALITY["gpt-image-2"])
        grid.addWidget(self.quality_combo, 3, 0)

        l4 = QLabel("生成格式")
        l4.setObjectName("paramLabel")
        grid.addWidget(l4, 2, 1)
        self.format_combo = QComboBox()
        setup_combo(self.format_combo)
        self.format_combo.addItems(["png", "jpeg"])
        grid.addWidget(self.format_combo, 3, 1)

        l5 = QLabel("生成数量")
        l5.setObjectName("paramLabel")
        grid.addWidget(l5, 4, 0)
        self.img_count_combo = QComboBox()
        setup_combo(self.img_count_combo)
        self.img_count_combo.addItems([str(i) for i in range(1, 11)])
        grid.addWidget(self.img_count_combo, 5, 0)

        pm_layout.addLayout(grid)
        pm_layout.addSpacing(8)

        self.gen_btn = QPushButton("⚡  生成图片")
        self.gen_btn.setObjectName("generateBtn")
        self.gen_btn.setMinimumHeight(56)
        self.gen_btn.setCursor(Qt.PointingHandCursor)
        self.gen_btn.clicked.connect(self.on_generate)
        pm_layout.addWidget(self.gen_btn)

        left_layout.addWidget(param_card)
        left_layout.addStretch()
        left_scroll.setWidget(left_inner)

        right = QVBoxLayout()
        right.setSpacing(0)
        right.setContentsMargins(0, 0, 0, 0)

        preview = QFrame()
        preview.setObjectName("previewArea")
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(24, 24, 24, 24)

        self.img_placeholder = QWidget()
        ph_layout = QVBoxLayout(self.img_placeholder)
        ph_layout.setAlignment(Qt.AlignCenter)
        ph_layout.setSpacing(12)
        img_icon_holder = QWidget()
        img_icon_holder.setFixedSize(200, 110)
        img_icon_holder.setStyleSheet("background: transparent;")
        self.img_search_icon = QLabel("🔍", img_icon_holder)
        self.img_search_icon.setAlignment(Qt.AlignCenter)
        self.img_search_icon.setStyleSheet("font-size: 48px; border: none; background: transparent; color: " + get_theme(self._theme)['accent'] + ";")
        self.img_search_icon.setFixedSize(70, 70)
        self.img_search_icon.move(65, 35)
        self._img_icon_home = (65, 35)
        self._img_icon_opacity = QGraphicsOpacityEffect(self.img_search_icon)
        self._img_icon_opacity.setOpacity(1.0)
        self.img_search_icon.setGraphicsEffect(self._img_icon_opacity)
        ph_layout.addWidget(img_icon_holder, alignment=Qt.AlignCenter)
        t1 = QLabel("等待生成")
        t1.setObjectName("previewTitle")
        t1.setAlignment(Qt.AlignCenter)
        ph_layout.addWidget(t1)
        t2 = QLabel("输入提示词并调整参数，生成高质量 AI 图片")
        t2.setObjectName("previewDesc")
        t2.setAlignment(Qt.AlignCenter)
        t2.setWordWrap(True)
        ph_layout.addWidget(t2)

        self.img_results_container = QWidget()
        self.img_results_container.setStyleSheet("background: transparent;")
        self.img_results_layout = QVBoxLayout(self.img_results_container)
        self.img_results_layout.setSpacing(10)
        self.img_results_layout.setContentsMargins(8, 8, 8, 8)
        self.img_results_layout.setAlignment(Qt.AlignTop)
        self.img_results_layout.addStretch()

        img_scroll = QScrollArea()
        img_scroll.setWidgetResizable(True)
        img_scroll.setWidget(self.img_results_container)

        self.img_stack = QStackedWidget()
        self.img_stack.addWidget(self.img_placeholder)
        self.img_stack.addWidget(img_scroll)

        preview_layout.addWidget(self.img_stack)

        self.img_status_bar = QLabel("● GPU Cluster: Online          Session: Frost-772")
        self.img_status_bar.setObjectName("footerText")
        self.img_status_bar.setStyleSheet("border-top: 1px solid " + get_theme(self._theme)['border_top_alpha'] + "; padding-top: 8px; color: " + get_theme(self._theme)['text_muted'] + "; font-size: 9px; font-family: Consolas; background: transparent;")
        preview_layout.addWidget(self.img_status_bar)

        right.addWidget(preview, 1)

        layout.addWidget(left_scroll, 4)
        layout.addLayout(right, 6)
        self.content_stack.addWidget(page)

    def _build_video_page(self):
        page = QWidget()
        page.setStyleSheet(f"background-color: {get_theme(self._theme)['bg_main']};")
        self._bg_pages.append(page)
        layout = QHBoxLayout(page)
        layout.setSpacing(24)
        layout.setContentsMargins(24, 24, 24, 0)

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_inner = QWidget()
        left_inner.setStyleSheet("background: transparent;")
        left_layout = QVBoxLayout(left_inner)
        left_layout.setSpacing(16)
        left_layout.setContentsMargins(0, 0, 0, 0)

        prompt_card = QFrame()
        prompt_card.setObjectName("glassPanel")
        pc_layout = QVBoxLayout(prompt_card)
        pc_layout.setSpacing(10)
        pc_layout.setContentsMargins(20, 20, 20, 20)

        ph = QHBoxLayout()
        pl = QLabel("提示词 Prompt")
        pl.setObjectName("sectionLabel")
        ph.addWidget(pl)
        ph.addStretch()
        badge = QLabel("VIDEO")
        badge.setObjectName("sectionBadge")
        ph.addWidget(badge)
        pc_layout.addLayout(ph)

        self.video_prompt_input = PlainPasteTextEdit()
        self.video_prompt_input.setObjectName("promptInput")
        self.video_prompt_input.setPlaceholderText("描述你想要生成的视频，例如：一只猫在海边奔跑，慢动作，电影质感...")
        self.video_prompt_input.setMinimumHeight(120)
        self.video_prompt_input.setMaximumHeight(160)
        self.video_prompt_input.setAcceptRichText(False)
        self.video_prompt_input.set_mention_provider(self._mention_candidates)
        pc_layout.addWidget(self.video_prompt_input)
        left_layout.addWidget(prompt_card)

        vid_ref_card = QFrame()
        vid_ref_card.setObjectName("glassPanel")
        vid_ref_layout = QVBoxLayout(vid_ref_card)
        vid_ref_layout.setSpacing(10)
        vid_ref_layout.setContentsMargins(20, 20, 20, 20)

        vid_ref_title = QLabel("参考图片（可选）")
        vid_ref_title.setObjectName("sectionLabel")
        vid_ref_layout.addWidget(vid_ref_title)

        self.vid_img_ref_hint = QLabel("")
        self.vid_img_ref_hint.setStyleSheet("color: #f59e0b; font-size: 11px; background: transparent;")
        self.vid_img_ref_hint.setWordWrap(True)
        vid_ref_layout.addWidget(self.vid_img_ref_hint)

        vid_btn_row = QHBoxLayout()
        self.vid_ref_pick_btn = QPushButton("选择本地图片")
        self.vid_ref_pick_btn.setObjectName("refBtn")
        self.vid_ref_pick_btn.setCursor(Qt.PointingHandCursor)
        self.vid_ref_pick_btn.clicked.connect(self.on_pick_vid_ref)
        vid_btn_row.addWidget(self.vid_ref_pick_btn)
        vid_btn_row.addStretch()
        self.vid_ref_clear_btn = QPushButton("清除")
        self.vid_ref_clear_btn.setObjectName("refBtn")
        self.vid_ref_clear_btn.setCursor(Qt.PointingHandCursor)
        self.vid_ref_clear_btn.clicked.connect(self.on_clear_vid_ref)
        vid_btn_row.addWidget(self.vid_ref_clear_btn)
        vid_ref_layout.addLayout(vid_btn_row)

        self.vid_img_ref_preview_layout = QGridLayout()
        self.vid_img_ref_preview_layout.setSpacing(6)
        self.vid_img_ref_preview_layout.setAlignment(Qt.AlignLeft)
        vid_ref_layout.addLayout(self.vid_img_ref_preview_layout)

        self.vid_img_ref_count_label = QLabel("")
        self.vid_img_ref_count_label.setStyleSheet("color: " + get_theme(self._theme)['text_muted'] + "; font-size: 11px; background: transparent;")
        vid_ref_layout.addWidget(self.vid_img_ref_count_label)

        left_layout.addWidget(vid_ref_card)

        self.vid_video_ref_card = QFrame()
        self.vid_video_ref_card.setObjectName("glassPanel")
        vvr_layout = QVBoxLayout(self.vid_video_ref_card)
        vvr_layout.setSpacing(10)
        vvr_layout.setContentsMargins(20, 20, 20, 20)

        self.vid_video_ref_title = QLabel("参考视频（可选）")
        self.vid_video_ref_title.setObjectName("sectionLabel")
        vvr_layout.addWidget(self.vid_video_ref_title)

        self.vid_video_ref_hint = QLabel("")
        self.vid_video_ref_hint.setStyleSheet("color: #f59e0b; font-size: 11px; background: transparent;")
        self.vid_video_ref_hint.setWordWrap(True)
        vvr_layout.addWidget(self.vid_video_ref_hint)

        vvr_input_row = QHBoxLayout()
        self.vid_video_ref_input = QLineEdit()
        self.vid_video_ref_input.setObjectName("refUrlInput")
        self.vid_video_ref_input.setPlaceholderText("粘贴视频 URL 后点击 + 添加")
        self.vid_video_ref_input.returnPressed.connect(self.on_add_vid_video_ref)
        vvr_input_row.addWidget(self.vid_video_ref_input)
        self.vid_video_ref_add_btn = QPushButton("添加")
        self.vid_video_ref_add_btn.setObjectName("refBtn")
        self.vid_video_ref_add_btn.setFixedWidth(60)
        self.vid_video_ref_add_btn.setStyleSheet("font-size: 13px;")
        self.vid_video_ref_add_btn.setCursor(Qt.PointingHandCursor)
        self.vid_video_ref_add_btn.clicked.connect(self.on_add_vid_video_ref)
        vvr_input_row.addWidget(self.vid_video_ref_add_btn)
        vvr_layout.addLayout(vvr_input_row)

        self.vid_video_ref_list_layout = QVBoxLayout()
        self.vid_video_ref_list_layout.setSpacing(4)
        vvr_layout.addLayout(self.vid_video_ref_list_layout)

        self.vid_video_ref_count_label = QLabel("")
        self.vid_video_ref_count_label.setStyleSheet("color: " + get_theme(self._theme)['text_muted'] + "; font-size: 11px; background: transparent;")
        vvr_layout.addWidget(self.vid_video_ref_count_label)

        left_layout.addWidget(self.vid_video_ref_card)

        self.vid_audio_ref_card = QFrame()
        self.vid_audio_ref_card.setObjectName("glassPanel")
        var_layout = QVBoxLayout(self.vid_audio_ref_card)
        var_layout.setSpacing(10)
        var_layout.setContentsMargins(20, 20, 20, 20)

        self.vid_audio_ref_title = QLabel("参考音频（可选）")
        self.vid_audio_ref_title.setObjectName("sectionLabel")
        var_layout.addWidget(self.vid_audio_ref_title)

        self.vid_audio_ref_hint = QLabel("提示：最多 3 条，单条 ≤15MB，总时长 ≤15s，必须公网 URL；prompt 中可用 @声音1 @声音2... 引用")
        self.vid_audio_ref_hint.setStyleSheet("color: #f59e0b; font-size: 11px; background: transparent;")
        self.vid_audio_ref_hint.setWordWrap(True)
        var_layout.addWidget(self.vid_audio_ref_hint)

        var_input_row = QHBoxLayout()
        self.vid_audio_ref_input = QLineEdit()
        self.vid_audio_ref_input.setObjectName("refUrlInput")
        self.vid_audio_ref_input.setPlaceholderText("粘贴音频 URL 后点击 + 添加")
        self.vid_audio_ref_input.returnPressed.connect(self.on_add_vid_audio_ref)
        var_input_row.addWidget(self.vid_audio_ref_input)
        self.vid_audio_ref_add_btn = QPushButton("添加")
        self.vid_audio_ref_add_btn.setObjectName("refBtn")
        self.vid_audio_ref_add_btn.setFixedWidth(60)
        self.vid_audio_ref_add_btn.setStyleSheet("font-size: 13px;")
        self.vid_audio_ref_add_btn.setCursor(Qt.PointingHandCursor)
        self.vid_audio_ref_add_btn.clicked.connect(self.on_add_vid_audio_ref)
        var_input_row.addWidget(self.vid_audio_ref_add_btn)
        var_layout.addLayout(var_input_row)

        self.vid_audio_ref_list_layout = QVBoxLayout()
        self.vid_audio_ref_list_layout.setSpacing(4)
        var_layout.addLayout(self.vid_audio_ref_list_layout)

        self.vid_audio_ref_count_label = QLabel("")
        self.vid_audio_ref_count_label.setStyleSheet("color: " + get_theme(self._theme)['text_muted'] + "; font-size: 11px; background: transparent;")
        var_layout.addWidget(self.vid_audio_ref_count_label)

        left_layout.addWidget(self.vid_audio_ref_card)

        self.vid_keyframe_card = QFrame()
        self.vid_keyframe_card.setObjectName("glassPanel")
        kf_layout = QVBoxLayout(self.vid_keyframe_card)
        kf_layout.setSpacing(10)
        kf_layout.setContentsMargins(20, 20, 20, 20)

        kf_title = QLabel("首尾帧（Kling 专属，可选）")
        kf_title.setObjectName("sectionLabel")
        kf_layout.addWidget(kf_title)

        kf_hint = QLabel("提示：首尾帧模式需同时上传首帧和尾帧")
        kf_hint.setStyleSheet("color: #f59e0b; font-size: 11px; background: transparent;")
        kf_hint.setWordWrap(True)
        kf_layout.addWidget(kf_hint)

        kf_slots_row = QHBoxLayout()
        kf_slots_row.setSpacing(12)

        self._vid_start_frame = {"url": None, "bytes": None, "uploading": False}
        self._vid_end_frame = {"url": None, "bytes": None, "uploading": False}

        for tag, label_text in (("start", "首帧"), ("end", "尾帧")):
            slot_col = QVBoxLayout()
            slot_col.setSpacing(4)
            lbl_text = QLabel(label_text)
            lbl_text.setStyleSheet("color: " + get_theme(self._theme)['text_secondary'] + "; font-size: 11px; background: transparent;")
            slot_col.addWidget(lbl_text, alignment=Qt.AlignCenter)
            thumb = QLabel("点击选择")
            thumb.setFixedSize(110, 110)
            thumb.setAlignment(Qt.AlignCenter)
            thumb.setStyleSheet("border: 1px dashed " + get_theme(self._theme)['accent_border'] + "; border-radius: 6px; color:" + get_theme(self._theme)['text_secondary'] + "; font-size:12px; background: " + get_theme(self._theme)['accent_softer'] + ";")
            thumb.setCursor(Qt.PointingHandCursor)
            thumb.mousePressEvent = lambda ev, t=tag: self.on_pick_keyframe(t)
            setattr(self, f"_vid_{tag}_frame_thumb", thumb)
            slot_col.addWidget(thumb, alignment=Qt.AlignCenter)
            clear_btn = QPushButton("清除")
            clear_btn.setFixedSize(110, 20)
            clear_btn.setCursor(Qt.PointingHandCursor)
            clear_btn.setStyleSheet("background: rgba(255,107,107,0.2); color: #ff6b6b; border: none; border-radius: 3px; font-size: 10px;")
            clear_btn.clicked.connect(lambda checked, t=tag: self.on_clear_keyframe(t))
            slot_col.addWidget(clear_btn, alignment=Qt.AlignCenter)
            kf_slots_row.addLayout(slot_col)
        kf_slots_row.addStretch()
        kf_layout.addLayout(kf_slots_row)

        left_layout.addWidget(self.vid_keyframe_card)

        param_card = QFrame()
        param_card.setObjectName("glassPanel")
        pm_layout = QVBoxLayout(param_card)
        pm_layout.setSpacing(14)
        pm_layout.setContentsMargins(20, 20, 20, 20)

        pm_title = QLabel("参数设置")
        pm_title.setObjectName("sectionLabel")
        pm_layout.addWidget(pm_title)

        grid = QGridLayout()
        grid.setSpacing(12)

        l1 = QLabel("模型")
        l1.setObjectName("paramLabel")
        grid.addWidget(l1, 0, 0)
        self.video_model_combo = QComboBox()
        setup_combo(self.video_model_combo)
        self.video_model_combo.addItems(VIDEO_MODELS)
        self.video_model_combo.currentTextChanged.connect(self.on_video_model_changed)
        grid.addWidget(self.video_model_combo, 1, 0)

        l2 = QLabel("画面方向")
        l2.setObjectName("paramLabel")
        grid.addWidget(l2, 0, 1)
        self.video_size_combo = QComboBox()
        setup_combo(self.video_size_combo)
        self.video_size_combo.addItems(list(VIDEO_SIZES.keys()))
        grid.addWidget(self.video_size_combo, 1, 1)

        l3 = QLabel("时长（秒）")
        l3.setObjectName("paramLabel")
        grid.addWidget(l3, 2, 0)
        self.video_duration_combo = QComboBox()
        setup_combo(self.video_duration_combo)
        self.video_duration_combo.addItems(VIDEO_DURATIONS)
        grid.addWidget(self.video_duration_combo, 3, 0)

        l4 = QLabel("生成数量")
        l4.setObjectName("paramLabel")
        grid.addWidget(l4, 2, 1)
        self.vid_count_combo = QComboBox()
        setup_combo(self.vid_count_combo)
        self.vid_count_combo.addItems([str(i) for i in range(1, 6)])
        grid.addWidget(self.vid_count_combo, 3, 1)

        self.vid_size_label = QLabel("画质")
        self.vid_size_label.setObjectName("paramLabel")
        grid.addWidget(self.vid_size_label, 4, 0)
        self.vid_size_combo = QComboBox()
        setup_combo(self.vid_size_combo)
        self.vid_size_combo.addItems(["自适应", "large", "small"])
        grid.addWidget(self.vid_size_combo, 5, 0)

        self.vid_ratio_label = QLabel("视频比例")
        self.vid_ratio_label.setObjectName("paramLabel")
        grid.addWidget(self.vid_ratio_label, 4, 1)
        self.vid_ratio_combo = QComboBox()
        setup_combo(self.vid_ratio_combo)
        self.vid_ratio_combo.addItems(["自动"])
        grid.addWidget(self.vid_ratio_combo, 5, 1)
        self.vid_ratio_label.setVisible(False)
        self.vid_ratio_combo.setVisible(False)

        pm_layout.addLayout(grid)
        pm_layout.addSpacing(8)

        self.video_gen_btn = QPushButton("⚡  生成视频")
        self.video_gen_btn.setObjectName("generateBtn")
        self.video_gen_btn.setMinimumHeight(56)
        self.video_gen_btn.setCursor(Qt.PointingHandCursor)
        self.video_gen_btn.clicked.connect(self.on_generate_video)
        pm_layout.addWidget(self.video_gen_btn)

        left_layout.addWidget(param_card)
        left_layout.addStretch()
        left_scroll.setWidget(left_inner)

        right = QVBoxLayout()
        right.setSpacing(0)
        right.setContentsMargins(0, 0, 0, 0)

        preview = QFrame()
        preview.setObjectName("previewArea")
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(24, 24, 24, 24)

        self.video_status_label = QLabel()
        self.video_status_label.setAlignment(Qt.AlignCenter)
        self.video_status_label.setStyleSheet("border: none; background: transparent;")

        self.vid_placeholder = QWidget()
        vph_layout = QVBoxLayout(self.vid_placeholder)
        vph_layout.setAlignment(Qt.AlignCenter)
        vph_layout.setSpacing(12)
        vid_icon_holder = QWidget()
        vid_icon_holder.setFixedSize(200, 110)
        vid_icon_holder.setStyleSheet("background: transparent;")
        self.vid_search_icon = QLabel("🎬", vid_icon_holder)
        self.vid_search_icon.setAlignment(Qt.AlignCenter)
        self.vid_search_icon.setStyleSheet("font-size: 48px; border: none; background: transparent; color: " + get_theme(self._theme)['accent'] + ";")
        self.vid_search_icon.setFixedSize(70, 70)
        self.vid_search_icon.move(65, 35)
        self._vid_icon_home = (65, 35)
        self._vid_icon_opacity = QGraphicsOpacityEffect(self.vid_search_icon)
        self._vid_icon_opacity.setOpacity(1.0)
        self.vid_search_icon.setGraphicsEffect(self._vid_icon_opacity)
        vph_layout.addWidget(vid_icon_holder, alignment=Qt.AlignCenter)
        self.vid_placeholder_title = QLabel("等待生成")
        self.vid_placeholder_title.setObjectName("previewTitle")
        self.vid_placeholder_title.setAlignment(Qt.AlignCenter)
        vph_layout.addWidget(self.vid_placeholder_title)
        self.vid_placeholder_desc = QLabel("输入提示词并调整参数，生成高质量 AI 视频")
        self.vid_placeholder_desc.setObjectName("previewDesc")
        self.vid_placeholder_desc.setAlignment(Qt.AlignCenter)
        self.vid_placeholder_desc.setWordWrap(True)
        vph_layout.addWidget(self.vid_placeholder_desc)

        self.vid_results_container = QWidget()
        self.vid_results_container.setStyleSheet("background: transparent;")
        self.vid_results_layout = QVBoxLayout(self.vid_results_container)
        self.vid_results_layout.setSpacing(10)
        self.vid_results_layout.setContentsMargins(8, 8, 8, 8)
        self.vid_results_layout.setAlignment(Qt.AlignTop)
        self.vid_results_layout.addStretch()

        vid_scroll = QScrollArea()
        vid_scroll.setWidgetResizable(True)
        vid_scroll.setWidget(self.vid_results_container)

        self.vid_stack = QStackedWidget()
        self.vid_stack.addWidget(self.vid_placeholder)
        self.vid_stack.addWidget(vid_scroll)
        self.vid_stack.addWidget(self.video_status_label)

        preview_layout.addWidget(self.vid_stack)

        self.vid_status_bar = QLabel("● GPU Cluster: Online          Session: Frost-772")
        self.vid_status_bar.setObjectName("footerText")
        self.vid_status_bar.setStyleSheet("border-top: 1px solid " + get_theme(self._theme)['border_top_alpha'] + "; padding-top: 8px; color: " + get_theme(self._theme)['text_muted'] + "; font-size: 9px; font-family: Consolas; background: transparent;")
        preview_layout.addWidget(self.vid_status_bar)

        right.addWidget(preview, 1)

        layout.addWidget(left_scroll, 4)
        layout.addLayout(right, 6)
        self.content_stack.addWidget(page)
        self.on_video_model_changed(self.video_model_combo.currentText())

    def _build_footer(self, parent_layout):
        footer = QFrame()
        footer.setObjectName("footerBar")
        footer.setFixedHeight(36)
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(20, 0, 20, 0)

        self.footer_status = QLabel("就绪")
        self.footer_status.setObjectName("footerText")
        fl.addWidget(self.footer_status)

        fl.addStretch()

        brand = QLabel("GLACIER-OS CORE V3.3")
        brand.setObjectName("footerBrand")
        fl.addWidget(brand)

        parent_layout.addWidget(footer)

    def _pick_ref_image(self, url_input, preview_label, attr_name):
        if not self.api_key:
            QMessageBox.warning(self, "提示", "请先在右上角设置 API Key")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择参考图片",
            os.path.join(os.path.expanduser("~"), "Desktop"),
            "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*)"
        )
        if not path:
            return
        url_input.setText(f"上传中: {os.path.basename(path)} ...")
        url_input.setEnabled(False)

        tag = attr_name
        thread = UploadRefImageThread(self.api_key, path, tag=tag)

        def _on_ok(_tag, url, img_bytes):
            setattr(self, attr_name, url)
            url_input.setEnabled(True)
            url_input.setText(url)
            qimg = QImage.fromData(img_bytes)
            if not qimg.isNull():
                pixmap = QPixmap.fromImage(qimg)
                scaled = pixmap.scaledToHeight(min(pixmap.height(), 150), Qt.SmoothTransformation)
                preview_label.setPixmap(scaled)
            self.footer_status.setText(f"参考图已上传")
            if thread in getattr(self, "_upload_threads", []):
                self._upload_threads.remove(thread)

        def _on_fail(_tag, msg):
            setattr(self, attr_name, None)
            url_input.setEnabled(True)
            url_input.clear()
            preview_label.clear()
            QMessageBox.warning(self, "上传失败", msg)
            if thread in getattr(self, "_upload_threads", []):
                self._upload_threads.remove(thread)

        thread.finished_ok.connect(_on_ok)
        thread.failed.connect(_on_fail)
        if not hasattr(self, "_upload_threads"):
            self._upload_threads = []
        self._upload_threads.append(thread)
        thread.start()

    def _clear_ref_image(self, url_input, preview_label, attr_name):
        setattr(self, attr_name, None)
        url_input.clear()
        preview_label.clear()

    def _current_max_img_refs(self):
        model = self.model_combo.currentText() if hasattr(self, "model_combo") else ""
        return IMAGE_MODEL_MAX_REFS.get(model, 4)

    def on_pick_img_ref(self):
        max_refs = self._current_max_img_refs()
        if len(self._img_ref_data_list) >= max_refs:
            QMessageBox.warning(self, "提示", f"最多支持 {max_refs} 张参考图片")
            return
        if not self.api_key:
            QMessageBox.warning(self, "提示", "请先在右上角设置 API Key")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, f"选择参考图片（最多{max_refs}张）",
            os.path.join(os.path.expanduser("~"), "Desktop"),
            "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*)"
        )
        if not paths:
            return
        slots_left = max_refs - len(self._img_ref_data_list)
        paths = paths[:slots_left]
        if not hasattr(self, "_upload_threads"):
            self._upload_threads = []

        for path in paths:
            placeholder = {"url": None, "bytes": None, "name": os.path.basename(path), "uploading": True, "error": None}
            self._img_ref_data_list.append(placeholder)
            tag = f"img_ref_{id(placeholder)}"

            thread = UploadRefImageThread(self.api_key, path, tag=tag)

            def _on_ok(_tag, url, img_bytes, slot=placeholder, th=thread):
                slot["url"] = url
                slot["bytes"] = img_bytes
                slot["uploading"] = False
                self._refresh_img_ref_preview()
                if th in self._upload_threads:
                    self._upload_threads.remove(th)

            def _on_fail(_tag, msg, slot=placeholder, th=thread):
                slot["uploading"] = False
                slot["error"] = msg
                if slot in self._img_ref_data_list:
                    self._img_ref_data_list.remove(slot)
                self._refresh_img_ref_preview()
                QMessageBox.warning(self, "上传失败", f"{slot['name']}: {msg}")
                if th in self._upload_threads:
                    self._upload_threads.remove(th)

            thread.finished_ok.connect(_on_ok)
            thread.failed.connect(_on_fail)
            self._upload_threads.append(thread)
            thread.start()

        self._refresh_img_ref_preview()

    def on_clear_img_ref(self):
        self._img_ref_data_list.clear()
        self._refresh_img_ref_preview()

    def _refresh_img_ref_preview(self):
        while self.img_ref_preview_layout.count():
            w = self.img_ref_preview_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        thumb_size = 90
        for i, slot in enumerate(self._img_ref_data_list):
            img_bytes = slot.get("bytes")
            uploading = slot.get("uploading")
            if img_bytes is None and not uploading:
                continue

            container = QWidget()
            container.setFixedSize(thumb_size + 4, thumb_size + 22)
            container.setStyleSheet("background: transparent;")
            cl = QVBoxLayout(container)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(2)
            img_lbl = QLabel()
            img_lbl.setFixedSize(thumb_size, thumb_size)
            img_lbl.setAlignment(Qt.AlignCenter)
            img_lbl.setStyleSheet("border: 1px solid " + get_theme(self._theme)['accent_border'] + "; border-radius: 4px; color:" + get_theme(self._theme)['text_secondary'] + "; font-size:11px; background: transparent;")

            if img_bytes:
                qimg = QImage.fromData(img_bytes)
                if not qimg.isNull():
                    pixmap = QPixmap.fromImage(qimg)
                    cropped = pixmap.scaled(thumb_size, thumb_size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                    x = (cropped.width() - thumb_size) // 2
                    y = (cropped.height() - thumb_size) // 2
                    cropped = cropped.copy(x, y, thumb_size, thumb_size)
                    img_lbl.setPixmap(cropped)
                if uploading:
                    img_lbl.setStyleSheet(img_lbl.styleSheet() + " ")
            else:
                img_lbl.setText("上传中...")
            cl.addWidget(img_lbl, alignment=Qt.AlignCenter)

            del_btn = QPushButton("删除")
            del_btn.setFixedSize(thumb_size, 18)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setStyleSheet("background: rgba(255,107,107,0.2); color: #ff6b6b; border: none; border-radius: 3px; font-size: 10px;")
            del_btn.clicked.connect(lambda checked, x=i: self._remove_img_ref(x))
            cl.addWidget(del_btn, alignment=Qt.AlignCenter)
            row, col = divmod(i, 3)
            self.img_ref_preview_layout.addWidget(container, row, col)
        count = len(self._img_ref_data_list)
        uploading_count = sum(1 for s in self._img_ref_data_list if s.get("uploading"))
        max_refs = self._current_max_img_refs()
        if uploading_count:
            self.img_ref_count_label.setText(f"已选择 {count}/{max_refs} 张（上传中 {uploading_count}）")
        else:
            self.img_ref_count_label.setText(f"已选择 {count}/{max_refs} 张" if count > 0 else "")
        self.img_ref_url.setText(f"已选择 {count} 张参考图" if count > 0 else "")

    def _remove_img_ref(self, index):
        if 0 <= index < len(self._img_ref_data_list):
            self._img_ref_data_list.pop(index)
        self._refresh_img_ref_preview()
        if not self._img_ref_data_list:
            self.model_combo.setEnabled(True)

    def on_pick_vid_ref(self):
        max_imgs = self._current_max_images()
        if len(self._vid_img_ref_data_list) >= max_imgs:
            QMessageBox.warning(self, "提示", f"当前模型最多支持 {max_imgs} 张参考图片")
            return
        if not self.api_key:
            QMessageBox.warning(self, "提示", "请先在右上角设置 API Key")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, f"选择参考图片（最多{max_imgs}张）",
            os.path.join(os.path.expanduser("~"), "Desktop"),
            "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*)"
        )
        if not paths:
            return
        slots_left = max_imgs - len(self._vid_img_ref_data_list)
        paths = paths[:slots_left]
        if not hasattr(self, "_upload_threads"):
            self._upload_threads = []

        for path in paths:
            placeholder = {"url": None, "bytes": None, "name": os.path.basename(path), "uploading": True, "error": None}
            self._vid_img_ref_data_list.append(placeholder)
            tag = f"vid_img_ref_{id(placeholder)}"
            thread = UploadRefImageThread(self.api_key, path, tag=tag)

            def _on_ok(_tag, url, img_bytes, slot=placeholder, th=thread):
                slot["url"] = url
                slot["bytes"] = img_bytes
                slot["uploading"] = False
                self._refresh_vid_img_ref_preview()
                if th in self._upload_threads:
                    self._upload_threads.remove(th)

            def _on_fail(_tag, msg, slot=placeholder, th=thread):
                slot["uploading"] = False
                slot["error"] = msg
                if slot in self._vid_img_ref_data_list:
                    self._vid_img_ref_data_list.remove(slot)
                self._refresh_vid_img_ref_preview()
                QMessageBox.warning(self, "上传失败", f"{slot['name']}: {msg}")
                if th in self._upload_threads:
                    self._upload_threads.remove(th)

            thread.finished_ok.connect(_on_ok)
            thread.failed.connect(_on_fail)
            self._upload_threads.append(thread)
            thread.start()

        self._refresh_vid_img_ref_preview()

    def on_clear_vid_ref(self):
        self._vid_img_ref_data_list.clear()
        self._refresh_vid_img_ref_preview()

    def _refresh_vid_img_ref_preview(self):
        while self.vid_img_ref_preview_layout.count():
            w = self.vid_img_ref_preview_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        thumb_size = 90
        is_sd = self.video_model_combo.currentText().startswith("sd-2")
        for i, slot in enumerate(self._vid_img_ref_data_list):
            img_bytes = slot.get("bytes")
            uploading = slot.get("uploading")
            if img_bytes is None and not uploading:
                continue

            container = QWidget()
            container.setFixedSize(thumb_size + 4, thumb_size + (40 if is_sd else 22))
            container.setStyleSheet("background: transparent;")
            cl = QVBoxLayout(container)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(2)
            img_lbl = QLabel()
            img_lbl.setFixedSize(thumb_size, thumb_size)
            img_lbl.setAlignment(Qt.AlignCenter)
            img_lbl.setStyleSheet("border: 1px solid " + get_theme(self._theme)['accent_border'] + "; border-radius: 4px; color:" + get_theme(self._theme)['text_secondary'] + "; font-size:11px; background: transparent;")

            if img_bytes:
                qimg = QImage.fromData(img_bytes)
                if not qimg.isNull():
                    pixmap = QPixmap.fromImage(qimg)
                    cropped = pixmap.scaled(thumb_size, thumb_size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                    x = (cropped.width() - thumb_size) // 2
                    y = (cropped.height() - thumb_size) // 2
                    cropped = cropped.copy(x, y, thumb_size, thumb_size)
                    img_lbl.setPixmap(cropped)
            else:
                img_lbl.setText("上传中...")
            cl.addWidget(img_lbl, alignment=Qt.AlignCenter)

            if is_sd:
                ins_btn = QPushButton(f"插入 @图片{i+1}")
                ins_btn.setFixedSize(thumb_size, 18)
                ins_btn.setCursor(Qt.PointingHandCursor)
                ins_btn.setStyleSheet("background: " + get_theme(self._theme)['accent_soft'] + "; color: " + get_theme(self._theme)['accent'] + "; border: none; border-radius: 3px; font-size: 10px;")
                ins_btn.clicked.connect(lambda checked, n=i+1: self._insert_ref_token(f"@图片{n}"))
                cl.addWidget(ins_btn, alignment=Qt.AlignCenter)

            del_btn = QPushButton("删除")
            del_btn.setFixedSize(thumb_size, 18)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setStyleSheet("background: rgba(255,107,107,0.2); color: #ff6b6b; border: none; border-radius: 3px; font-size: 10px;")
            del_btn.clicked.connect(lambda checked, x=i: self._remove_vid_img_ref(x))
            cl.addWidget(del_btn, alignment=Qt.AlignCenter)
            row, col = divmod(i, 3)
            self.vid_img_ref_preview_layout.addWidget(container, row, col)
        count = len(self._vid_img_ref_data_list)
        uploading_count = sum(1 for s in self._vid_img_ref_data_list if s.get("uploading"))
        max_imgs = 1 if self.video_model_combo.currentText() == "sora-2" else 4
        if uploading_count:
            self.vid_img_ref_count_label.setText(f"已选择 {count}/{max_imgs} 张（上传中 {uploading_count}）")
        else:
            self.vid_img_ref_count_label.setText(f"已选择 {count}/{max_imgs} 张" if count > 0 else "")

    def _remove_vid_img_ref(self, index):
        if 0 <= index < len(self._vid_img_ref_data_list):
            self._vid_img_ref_data_list.pop(index)
        self._refresh_vid_img_ref_preview()

    def on_add_vid_video_ref(self):
        url = self.vid_video_ref_input.text().strip()
        if not url:
            return
        if len(self._vid_video_ref_url_list) >= 3:
            QMessageBox.warning(self, "提示", "最多支持 3 条参考视频")
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            QMessageBox.warning(self, "提示", "请填写完整的 http(s) URL")
            return
        self._vid_video_ref_url_list.append(url)
        self.vid_video_ref_input.clear()
        self._refresh_vid_video_ref_list()

    def _remove_vid_video_ref(self, index):
        if 0 <= index < len(self._vid_video_ref_url_list):
            self._vid_video_ref_url_list.pop(index)
        self._refresh_vid_video_ref_list()

    def on_add_vid_audio_ref(self):
        url = self.vid_audio_ref_input.text().strip()
        if not url:
            return
        if len(self._vid_audio_ref_url_list) >= 3:
            QMessageBox.warning(self, "提示", "最多支持 3 条参考音频")
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            QMessageBox.warning(self, "提示", "请填写完整的 http(s) URL")
            return
        self._vid_audio_ref_url_list.append(url)
        self.vid_audio_ref_input.clear()
        self._refresh_vid_audio_ref_list()

    def _remove_vid_audio_ref(self, index):
        if 0 <= index < len(self._vid_audio_ref_url_list):
            self._vid_audio_ref_url_list.pop(index)
        self._refresh_vid_audio_ref_list()

    def _refresh_vid_audio_ref_list(self):
        while self.vid_audio_ref_list_layout.count():
            w = self.vid_audio_ref_list_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        for i, url in enumerate(self._vid_audio_ref_url_list):
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(6)
            tag = QLabel(f"@声音{i+1}")
            tag.setStyleSheet("color: " + get_theme(self._theme)['accent'] + "; font-size: 11px; min-width: 50px; background: transparent;")
            rl.addWidget(tag)
            url_lbl = QLabel(url)
            url_lbl.setStyleSheet("color: " + get_theme(self._theme)['text_secondary'] + "; font-size: 11px; background: transparent;")
            url_lbl.setToolTip(url)
            url_lbl.setMaximumWidth(220)
            url_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            rl.addWidget(url_lbl, 1)
            ins_btn = QPushButton("插入")
            ins_btn.setFixedSize(40, 20)
            ins_btn.setCursor(Qt.PointingHandCursor)
            ins_btn.setStyleSheet("background: " + get_theme(self._theme)['accent_soft'] + "; color: " + get_theme(self._theme)['accent'] + "; border: none; border-radius: 3px; font-size: 10px;")
            ins_btn.clicked.connect(lambda checked, n=i+1: self._insert_ref_token(f"@声音{n}"))
            rl.addWidget(ins_btn)
            del_btn = QPushButton("×")
            del_btn.setFixedSize(20, 20)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setStyleSheet("background: rgba(255,107,107,0.2); color: #ff6b6b; border: none; border-radius: 3px; font-size: 12px;")
            del_btn.clicked.connect(lambda checked, x=i: self._remove_vid_audio_ref(x))
            rl.addWidget(del_btn)
            self.vid_audio_ref_list_layout.addWidget(row)
        count = len(self._vid_audio_ref_url_list)
        self.vid_audio_ref_count_label.setText(f"已添加 {count}/3 条" if count > 0 else "")

    def _refresh_vid_video_ref_list(self):
        while self.vid_video_ref_list_layout.count():
            w = self.vid_video_ref_list_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        for i, url in enumerate(self._vid_video_ref_url_list):
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(6)
            tag = QLabel(f"@视频{i+1}")
            tag.setStyleSheet("color: " + get_theme(self._theme)['accent'] + "; font-size: 11px; min-width: 50px; background: transparent;")
            rl.addWidget(tag)
            url_lbl = QLabel(url)
            url_lbl.setStyleSheet("color: " + get_theme(self._theme)['text_secondary'] + "; font-size: 11px; background: transparent;")
            url_lbl.setToolTip(url)
            url_lbl.setMaximumWidth(220)
            url_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            rl.addWidget(url_lbl, 1)
            ins_btn = QPushButton("插入")
            ins_btn.setFixedSize(40, 20)
            ins_btn.setCursor(Qt.PointingHandCursor)
            ins_btn.setStyleSheet("background: " + get_theme(self._theme)['accent_soft'] + "; color: " + get_theme(self._theme)['accent'] + "; border: none; border-radius: 3px; font-size: 10px;")
            ins_btn.clicked.connect(lambda checked, n=i+1: self._insert_ref_token(f"@视频{n}"))
            rl.addWidget(ins_btn)
            del_btn = QPushButton("×")
            del_btn.setFixedSize(20, 20)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setStyleSheet("background: rgba(255,107,107,0.2); color: #ff6b6b; border: none; border-radius: 3px; font-size: 12px;")
            del_btn.clicked.connect(lambda checked, x=i: self._remove_vid_video_ref(x))
            rl.addWidget(del_btn)
            self.vid_video_ref_list_layout.addWidget(row)
        count = len(self._vid_video_ref_url_list)
        self.vid_video_ref_count_label.setText(f"已添加 {count}/3 条" if count > 0 else "")

    def _insert_ref_token(self, token):
        cursor = self.video_prompt_input.textCursor()
        cursor.insertText(token)
        self.video_prompt_input.setFocus()

    def _mention_candidates(self):
        model = self.video_model_combo.currentText() if hasattr(self, "video_model_combo") else ""
        if model == "sora-2":
            return []
        cands = []
        n_imgs = sum(1 for s in self._vid_img_ref_data_list if s.get("url"))
        for i in range(1, n_imgs + 1):
            cands.append(f"图片{i}")
        n_vids = len(self._vid_video_ref_url_list)
        for i in range(1, n_vids + 1):
            cands.append(f"视频{i}")
        n_auds = len(self._vid_audio_ref_url_list)
        for i in range(1, n_auds + 1):
            cands.append(f"声音{i}")
        return cands

    def on_pick_keyframe(self, tag):
        if not self.api_key:
            QMessageBox.warning(self, "提示", "请先在右上角设置 API Key")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, f"选择{('首帧' if tag == 'start' else '尾帧')}图片",
            os.path.join(os.path.expanduser("~"), "Desktop"),
            "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*)"
        )
        if not path:
            return
        slot = self._vid_start_frame if tag == "start" else self._vid_end_frame
        slot["uploading"] = True
        slot["name"] = os.path.basename(path)
        self._refresh_keyframe_thumb(tag)

        if not hasattr(self, "_upload_threads"):
            self._upload_threads = []
        thread = UploadRefImageThread(self.api_key, path, tag=f"kf_{tag}_{id(slot)}")

        def _on_ok(_tag, url, img_bytes, s=slot, th=thread, t=tag):
            s["url"] = url
            s["bytes"] = img_bytes
            s["uploading"] = False
            self._refresh_keyframe_thumb(t)
            if th in self._upload_threads:
                self._upload_threads.remove(th)

        def _on_fail(_tag, msg, s=slot, th=thread, t=tag):
            s["uploading"] = False
            s["url"] = None
            s["bytes"] = None
            self._refresh_keyframe_thumb(t)
            QMessageBox.warning(self, "上传失败", f"{s.get('name','')}: {msg}")
            if th in self._upload_threads:
                self._upload_threads.remove(th)

        thread.finished_ok.connect(_on_ok)
        thread.failed.connect(_on_fail)
        self._upload_threads.append(thread)
        thread.start()

    def on_clear_keyframe(self, tag):
        slot = self._vid_start_frame if tag == "start" else self._vid_end_frame
        slot["url"] = None
        slot["bytes"] = None
        slot["uploading"] = False
        self._refresh_keyframe_thumb(tag)

    def _refresh_keyframe_thumb(self, tag):
        slot = self._vid_start_frame if tag == "start" else self._vid_end_frame
        thumb = getattr(self, f"_vid_{tag}_frame_thumb")
        if slot.get("uploading"):
            thumb.setText("上传中...")
            thumb.setPixmap(QPixmap())
            return
        img_bytes = slot.get("bytes")
        if img_bytes:
            qimg = QImage.fromData(img_bytes)
            if not qimg.isNull():
                pm = QPixmap.fromImage(qimg).scaled(110, 110, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                x = max(0, (pm.width() - 110) // 2)
                y = max(0, (pm.height() - 110) // 2)
                thumb.setPixmap(pm.copy(x, y, 110, 110))
                thumb.setText("")
                return
        thumb.setText("点击选择")
        thumb.setPixmap(QPixmap())

    def on_video_model_changed(self, model):
        prev_dur = self.video_duration_combo.currentText()
        self.video_duration_combo.clear()
        durations = VIDEO_MODEL_DURATIONS.get(model, VIDEO_DURATIONS)
        self.video_duration_combo.addItems(durations)
        if prev_dur in durations:
            self.video_duration_combo.setCurrentText(prev_dur)

        prev_orient = self.video_size_combo.currentText()
        self.video_size_combo.clear()
        orientations = VIDEO_MODEL_ORIENTATIONS.get(model, VIDEO_SIZES)
        self.video_size_combo.addItems(list(orientations.keys()))
        if prev_orient in orientations:
            self.video_size_combo.setCurrentText(prev_orient)

        is_sd = model.startswith("sd-2")
        is_kling = model == "kling-o3"

        is_sd_new = model in ("sd-2", "sd-2-fast")
        if is_sd_new:
            self.vid_video_ref_card.setVisible(True)
            self.vid_video_ref_title.setText("参考视频（可选）")
            self.vid_video_ref_hint.setText("提示：最多 3 条，单段 2–10s、≤50MB，总时长 ≤10s，必须公网 URL")
            self.vid_audio_ref_card.setVisible(True)
        elif is_sd:
            self.vid_video_ref_card.setVisible(True)
            self.vid_video_ref_title.setText("参考视频（可选）")
            self.vid_video_ref_hint.setText("提示：最多 3 条，分辨率 480p–720p，单段 2–15s、≤50MB，总时长 ≤15s，必须公网 URL")
            self.vid_audio_ref_card.setVisible(True)
        else:
            self.vid_video_ref_card.setVisible(False)
            self.vid_audio_ref_card.setVisible(False)

        self.vid_size_label.setVisible(is_sd or is_kling)
        self.vid_size_combo.setVisible(is_sd or is_kling)
        self.vid_keyframe_card.setVisible(False)

        ratios = VIDEO_MODEL_RATIOS.get(model)
        if ratios:
            prev_ratio = self.vid_ratio_combo.currentText()
            self.vid_ratio_combo.clear()
            self.vid_ratio_combo.addItems(["自动"] + ratios)
            if prev_ratio in ratios:
                self.vid_ratio_combo.setCurrentText(prev_ratio)
            self.vid_ratio_label.setVisible(True)
            self.vid_ratio_combo.setVisible(True)
        else:
            self.vid_ratio_label.setVisible(False)
            self.vid_ratio_combo.setVisible(False)

        if model == "sora-2":
            self.vid_img_ref_hint.setText("提示：sora-2 仅支持 1 张参考图片")
        elif is_sd_new:
            self.vid_img_ref_hint.setText("提示：最多 9 张，单图 ≤30MB，总和 ≤270MB；prompt 中可用 @1 @2... 引用")
        elif is_sd:
            self.vid_img_ref_hint.setText("提示：最多 9 张，单图 ≤30MB，所有素材总 ≤64MB；prompt 中可用 @1 @2... 引用")
        elif is_kling:
            self.vid_img_ref_hint.setText("提示：最多 8 张参考图片，不支持视频/音频")

        max_imgs = self._current_max_images()
        max_vids = self._current_max_videos()
        max_auds = VIDEO_MODEL_MAX_AUDIOS.get(model, 0)
        if len(self._vid_img_ref_data_list) > max_imgs:
            self._vid_img_ref_data_list[:] = self._vid_img_ref_data_list[:max_imgs]
        if len(self._vid_video_ref_url_list) > max_vids:
            self._vid_video_ref_url_list[:] = self._vid_video_ref_url_list[:max_vids]
        if len(self._vid_audio_ref_url_list) > max_auds:
            self._vid_audio_ref_url_list[:] = self._vid_audio_ref_url_list[:max_auds]

        self._refresh_vid_img_ref_preview()
        self._refresh_vid_video_ref_list()
        self._refresh_vid_audio_ref_list()
        self._refresh_keyframe_thumb("start")
        self._refresh_keyframe_thumb("end")

    def _current_max_images(self):
        model = self.video_model_combo.currentText()
        has_video = len(self._vid_video_ref_url_list) > 0
        if has_video:
            return VIDEO_MODEL_MAX_IMAGES_WITH_VIDEO.get(model, 4)
        return VIDEO_MODEL_MAX_IMAGES_BASE.get(model, 4)

    def _current_max_videos(self):
        model = self.video_model_combo.currentText()
        return VIDEO_MODEL_MAX_VIDEOS.get(model, 0)

    def on_model_changed(self, model):
        self.quality_combo.clear()
        qualities = MODEL_QUALITY.get(model, ["1k"])
        self.quality_combo.addItems(qualities)

        current_ratio = self.size_combo.currentText()
        self.size_combo.blockSignals(True)
        self.size_combo.clear()
        if model in GEMINI_MODELS:
            self.size_combo.addItems(GEMINI_RATIO_LIST)
            if current_ratio in GEMINI_RATIO_LIST:
                self.size_combo.setCurrentText(current_ratio)
            else:
                self.size_combo.setCurrentText("1:1")
        else:
            self.size_combo.addItems(RATIO_LIST)
            if current_ratio in RATIO_LIST:
                self.size_combo.setCurrentText(current_ratio)
        self.size_combo.blockSignals(False)

        max_refs = IMAGE_MODEL_MAX_REFS.get(model, 4)
        if hasattr(self, "img_ref_hint"):
            self.img_ref_hint.setText(f"提示：参考图最多 {max_refs} 张")
        if len(self._img_ref_data_list) > max_refs:
            self._img_ref_data_list[:] = self._img_ref_data_list[:max_refs]
            if hasattr(self, "_refresh_img_ref_preview"):
                self._refresh_img_ref_preview()

    def on_generate(self):
        prompt = self.prompt_input.toPlainText().strip()
        print(f"[DEBUG] on_generate called, prompt='{prompt[:50]}', api_key='{self.api_key[:8] if self.api_key else 'NONE'}...'")
        if not prompt:
            QMessageBox.warning(self, "提示", "请输入提示词")
            return
        if any(s.get("uploading") for s in self._img_ref_data_list):
            QMessageBox.warning(self, "提示", "参考图正在上传，请稍候")
            return

        count = int(self.img_count_combo.currentText())
        model = self.model_combo.currentText()
        img_ref_urls = [s["url"] for s in self._img_ref_data_list if s.get("url")]

        if model in GEMINI_MODELS and len(img_ref_urls) > GEMINI_MAX_REFERENCE_IMAGES:
            QMessageBox.warning(self, "提示", f"{model} 最多支持 {GEMINI_MAX_REFERENCE_IMAGES} 张参考图，请删除多余的参考图后重试")
            return

        ratio = self.size_combo.currentText()
        quality = self.quality_combo.currentText()
        if model in GEMINI_MODELS:
            actual_size = GEMINI_SIZE_MAP.get(ratio, {}).get(quality, "1024x1024")
        else:
            actual_size = SIZE_MAP.get(ratio, {}).get(quality, "1024x1024")

        fmt = self.format_combo.currentText()
        ext = fmt if fmt == "png" else "jpg"

        params = {
            "model": model,
            "prompt": prompt,
            "size": actual_size,
            "quality": quality,
            "output_format": fmt,
            "ext": ext,
            "count": count,
            "image_url": img_ref_urls if img_ref_urls else None,
            "ratio": ratio,
        }
        summary = {
            "label": f"{model} · {count}张 · {ratio} · {quality}",
            "meta": f"{actual_size} · {fmt}",
            "prompt": prompt,
            "count": count,
        }
        task = Task("image", params, summary)
        if not self.scheduler.enqueue(task):
            QMessageBox.warning(self, "提示", f"队列已满（{self.scheduler.max_queue}/{self.scheduler.max_queue}），请等待")
            return
        self.footer_status.setText(f"任务 #{task.id} 已加入队列")

    def on_generate_video(self):
        prompt = self.video_prompt_input.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(self, "提示", "请输入提示词")
            return

        model = self.video_model_combo.currentText()
        limit = VIDEO_PROMPT_LIMIT.get(model)
        if limit and len(prompt) > limit:
            QMessageBox.warning(self, "提示", f"{model} 提示词不能超过 {limit} 字（当前 {len(prompt)} 字）")
            return

        if any(s.get("uploading") for s in self._vid_img_ref_data_list):
            QMessageBox.warning(self, "提示", "参考图正在上传，请稍候")
            return

        count = int(self.vid_count_combo.currentText())
        size_label = self.video_size_combo.currentText()
        size_value = VIDEO_SIZES.get(size_label, "landscape")

        vid_img_urls = [s["url"] for s in self._vid_img_ref_data_list if s.get("url")]
        vid_video_urls = list(self._vid_video_ref_url_list)
        vid_audio_urls = list(self._vid_audio_ref_url_list)
        is_kling = model == "kling-o3"
        size_choice = self.vid_size_combo.currentText() if (model.startswith("sd-2") or is_kling) else ""
        sd_size = size_choice if size_choice in ("large", "small") else None

        sd_ratio = None
        if VIDEO_MODEL_RATIOS.get(model):
            ratio_choice = self.vid_ratio_combo.currentText()
            if ratio_choice and ratio_choice != "自动":
                sd_ratio = ratio_choice

        start_url = None
        end_url = None

        duration = self.video_duration_combo.currentText()
        params = {
            "prompt": prompt,
            "size": size_value,
            "duration": duration,
            "count": count,
            "image_url": vid_img_urls if vid_img_urls else None,
            "model": model,
            "video_refs": vid_video_urls if vid_video_urls else None,
            "audio_refs": vid_audio_urls if vid_audio_urls else None,
            "sd_size": sd_size,
            "sd_ratio": sd_ratio,
            "start_image_url": start_url,
            "end_image_url": end_url,
            "orientation_label": size_label,
        }
        meta_parts = []
        if model.startswith("sd-2") or is_kling:
            meta_parts.append(sd_size or "自适应")
        if sd_ratio:
            meta_parts.append(sd_ratio)
        summary = {
            "label": f"{model} · {count}个 · {size_label} · {duration}s",
            "meta": " · ".join(meta_parts),
            "prompt": prompt,
            "count": count,
        }
        task = Task("video", params, summary)
        if not self.scheduler.enqueue(task):
            QMessageBox.warning(self, "提示", f"队列已满（{self.scheduler.max_queue}/{self.scheduler.max_queue}），请等待")
            return
        self.footer_status.setText(f"视频任务 #{task.id} 已加入队列")

    def _start_task(self, task):
        """由 TaskScheduler 在调度时调用。启动对应 QThread。"""
        tid = task.id
        if task.kind == "image":
            p = task.params
            thread = GenerateThread(
                self.api_key, p["model"], p["prompt"], p["size"], p["quality"],
                p["output_format"], p["count"], image_url=p.get("image_url"),
            )
        else:
            p = task.params
            thread = VideoGenerateThread(
                self.api_key, p["prompt"], p["size"], p["duration"], p["count"],
                image_url=p.get("image_url"), model=p["model"],
                video_refs=p.get("video_refs"), audio_refs=p.get("audio_refs"),
                sd_size=p.get("sd_size"),
                sd_ratio=p.get("sd_ratio"),
                start_image_url=p.get("start_image_url"),
                end_image_url=p.get("end_image_url"),
            )
        task.thread = thread
        thread.one_finished.connect(lambda idx, data, tid=tid: self.scheduler.on_task_one_finished(tid, idx, data))
        thread.error.connect(lambda idx, msg, tid=tid: self.scheduler.on_task_error(tid, idx, msg))
        thread.progress.connect(lambda msg, tid=tid: self._on_task_progress(tid, msg))
        thread.all_done.connect(lambda tid=tid: self.scheduler.on_task_all_done(tid))
        thread.start()

    def _on_task_progress(self, task_id, msg):
        if self._is_transient_status_active():
            return
        self.footer_status.setText(f"#{task_id} {msg}")

    def _show_transient_status(self, text, duration_ms=2500):
        self._transient_status_until = (datetime.now().timestamp() * 1000) + duration_ms
        self.footer_status.setText(text)
        QTimer.singleShot(duration_ms + 50, self._refresh_status_bar)

    def _is_transient_status_active(self):
        until = getattr(self, "_transient_status_until", 0)
        return datetime.now().timestamp() * 1000 < until

    def _on_task_state_changed(self, task_id):
        task = self.scheduler.all_tasks.get(task_id)
        card = self._task_cards.get(task_id)
        if task is None:
            if card is not None:
                card.setParent(None)
                card.deleteLater()
                self._task_cards.pop(task_id, None)
                self._refresh_task_views()
            return
        if card is None:
            self._refresh_task_views()
        else:
            card.update_state()
        self._refresh_status_bar()

    def _refresh_task_views(self):
        """重排所有 TaskCard，按 tab 分别显示。"""
        # 清空两个 layout
        for layout in (self.img_results_layout, self.vid_results_layout):
            while layout.count():
                item = layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None)
        # 重新生成
        self._task_cards = {}
        all_tasks = sorted(self.scheduler.all_tasks.values(), key=lambda t: t.id)
        for t in all_tasks:
            card = TaskCard(t, self)
            self._task_cards[t.id] = card
            if t.kind == "image":
                self.img_results_layout.addWidget(card)
            else:
                self.vid_results_layout.addWidget(card)
        self.img_results_layout.addStretch()
        self.vid_results_layout.addStretch()
        # 切换 stack 显示
        has_img = any(t.kind == "image" for t in all_tasks)
        has_vid = any(t.kind == "video" for t in all_tasks)
        self.img_stack.setCurrentIndex(1 if has_img else 0)
        self.vid_stack.setCurrentIndex(1 if has_vid else 0)

    def _refresh_status_bar(self):
        if not hasattr(self, "footer_status"):
            return
        if self._is_transient_status_active():
            return
        img_run, vid_run, queued = self.scheduler.counts_by_kind()
        text = f"图片 {img_run} 跑 · 视频 {vid_run} 跑 · 排队 {queued}"
        self.footer_status.setText(text)
        # tab stack: 没任务时回到 placeholder
        has_img = any(t.kind == "image" for t in self.scheduler.all_tasks.values())
        has_vid = any(t.kind == "video" for t in self.scheduler.all_tasks.values())
        if hasattr(self, "img_stack"):
            self.img_stack.setCurrentIndex(1 if has_img else 0)
        if hasattr(self, "vid_stack"):
            self.vid_stack.setCurrentIndex(1 if has_vid else 0)

    def _on_task_finished(self, task):
        """每个任务完成后保存历史 + 刷新余额。"""
        try:
            if task.kind == "image":
                self._save_image_task_to_history(task)
            else:
                self._save_video_task_to_history(task)
        except Exception as e:
            print(f"[DEBUG] save history failed: {e}", flush=True)
        self.refresh_balance()

    def _start_search_anim(self):
        if not hasattr(self, "_search_anim_phase"):
            self._search_anim_phase = 0.0
            self._search_timer = QTimer(self)
            self._search_timer.timeout.connect(self._tick_search_anim)
        self._search_anim_phase = 0.0
        self._search_timer.start(33)

    def _stop_search_anim(self):
        if hasattr(self, "_search_timer"):
            self._search_timer.stop()
        if hasattr(self, "img_search_icon") and hasattr(self, "_img_icon_home"):
            hx, hy = self._img_icon_home
            self.img_search_icon.move(hx, hy)
            self._img_icon_opacity.setOpacity(1.0)
        if hasattr(self, "vid_search_icon") and hasattr(self, "_vid_icon_home"):
            hx, hy = self._vid_icon_home
            self.vid_search_icon.move(hx, hy)
            self._vid_icon_opacity.setOpacity(1.0)

    def _tick_search_anim(self):
        period_ms = 2200.0
        self._search_anim_phase += (33.0 / period_ms) * 2 * math.pi
        a = self._search_anim_phase
        radius_x = 30
        radius_y = 14
        x_off = -math.sin(a) * radius_x
        y_off = -(1 - math.cos(a)) * radius_y
        opacity = 0.82 + 0.18 * (1 - abs(math.sin(a)))
        if hasattr(self, "img_search_icon"):
            hx, hy = self._img_icon_home
            self.img_search_icon.move(int(hx + x_off), int(hy + y_off))
            self._img_icon_opacity.setOpacity(opacity)
        if hasattr(self, "vid_search_icon"):
            hx, hy = self._vid_icon_home
            self.vid_search_icon.move(int(hx + x_off), int(hy + y_off))
            self._vid_icon_opacity.setOpacity(opacity)

    def _save_image_task_to_history(self, task):
        ensure_history_dir()
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        ext = task.params.get("ext", "png")
        img_paths = []
        for i, data in enumerate(task.results):
            filename = f"{ts}_t{task.id}_{i+1}.{ext}"
            filepath = os.path.join(HISTORY_DIR, filename)
            with open(filepath, "wb") as f:
                f.write(data)
            img_paths.append(filename)
        record_kind = "image" if img_paths else "image_failed"
        records = load_history()
        records.insert(0, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "task_id": task.id,
            "prompt": task.params.get("prompt", ""),
            "model": task.params.get("model", ""),
            "quality": task.params.get("quality", ""),
            "ratio": task.params.get("ratio", ""),
            "kind": record_kind,
            "images": img_paths,
            "status": task.status,
            "error": task.error_msg,
            "expected": task.total,
        })
        if len(records) > 200:
            records = records[:200]
        save_history(records)

    def _save_video_task_to_history(self, task):
        ensure_history_dir()
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        video_paths = []
        for i, data in enumerate(task.results):
            filename = f"video_{ts}_t{task.id}_{i+1}.mp4"
            filepath = os.path.join(HISTORY_DIR, filename)
            with open(filepath, "wb") as f:
                f.write(data)
            video_paths.append(filename)
            try:
                thumb_bytes = extract_video_first_frame_bytes(data)
                if thumb_bytes:
                    with open(filepath + ".thumb.png", "wb") as tf:
                        tf.write(thumb_bytes)
            except Exception:
                pass
        record_kind = "video" if video_paths else "video_failed"
        records = load_history()
        records.insert(0, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "task_id": task.id,
            "prompt": task.params.get("prompt", ""),
            "model": task.params.get("model", ""),
            "quality": f"{task.params.get('duration','')}s",
            "ratio": task.params.get("orientation_label", ""),
            "kind": record_kind,
            "images": video_paths,
            "status": task.status,
            "error": task.error_msg,
            "expected": task.total,
        })
        if len(records) > 200:
            records = records[:200]
        save_history(records)

    def _build_history_page(self):
        page = QWidget()
        page.setStyleSheet(f"background-color: {get_theme(self._theme)['bg_main']};")
        self._bg_pages.append(page)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("生成历史记录")
        title.setObjectName("sectionLabel")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: " + get_theme(self._theme)['text_primary'] + "; background: transparent;")
        header.addWidget(title)
        header.addStretch()

        open_dir_btn = QPushButton("打开历史文件夹")
        open_dir_btn.setCursor(Qt.PointingHandCursor)
        _t = get_theme(self._theme)
        open_dir_btn.setStyleSheet(
            "QPushButton { background: " + _t['accent_softer'] + "; color: " + _t['accent'] + ";"
            " border: 1px solid " + _t['accent_border'] + "; border-radius: 6px;"
            " padding: 6px 16px; font-size: 12px; }"
            "QPushButton:hover { background: " + _t['accent_soft'] + "; }"
        )
        open_dir_btn.clicked.connect(self._open_history_dir)
        header.addWidget(open_dir_btn)

        clear_btn = QPushButton("清空历史")
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255,60,60,0.15); color: #ff6b6b;
                border: 1px solid rgba(255,60,60,0.3); border-radius: 6px;
                padding: 6px 16px; font-size: 12px;
            }
            QPushButton:hover { background: rgba(255,60,60,0.25); }
        """)
        clear_btn.clicked.connect(self._clear_history)
        header.addWidget(clear_btn)
        layout.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self.history_container = QWidget()
        self.history_container.setStyleSheet("background: transparent;")
        self.history_layout = QVBoxLayout(self.history_container)
        self.history_layout.setSpacing(12)
        self.history_layout.setContentsMargins(0, 0, 0, 0)
        self.history_layout.addStretch()

        scroll.setWidget(self.history_container)
        layout.addWidget(scroll)
        self.content_stack.addWidget(page)

    def _refresh_history_page(self):
        while self.history_layout.count():
            item = self.history_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        records = load_history()
        if not records:
            empty = QLabel("暂无历史记录")
            empty.setStyleSheet(f"color: {get_theme(self._theme)['text_muted']}; font-size: 14px; padding: 40px; background: transparent;")
            empty.setAlignment(Qt.AlignCenter)
            self.history_layout.addWidget(empty)
            self.history_layout.addStretch()
            return

        for record in records:
            theme = get_theme(self._theme)
            card = QFrame()
            card.setStyleSheet(f"""
                QFrame {{
                    background: {theme['bg_card_solid']};
                    border: 1px solid {theme['border_soft']};
                    border-radius: 10px;
                }}
                QFrame:hover {{
                    border: 1px solid {theme['accent_border']};
                }}
            """)
            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(12, 12, 12, 12)
            card_layout.setSpacing(12)

            img_container = QHBoxLayout()
            img_container.setSpacing(6)
            thumb_labels = []
            is_video_record = record.get("kind") == "video"
            for img_file in record.get("images", [])[:4]:
                img_path = os.path.join(HISTORY_DIR, img_file)
                if not os.path.exists(img_path):
                    continue
                ext = os.path.splitext(img_file)[1].lstrip(".").lower()
                if is_video_record or ext == "mp4":
                    lbl = ClickableLabel(None, file_ext="mp4", is_video=True, file_path=img_path, skip_thumb=True)
                    thumb_pixmap = QPixmap()
                    try:
                        thumb_path = get_or_make_thumb_for_video(img_path)
                        if thumb_path:
                            thumb_pixmap = QPixmap(thumb_path)
                    except Exception as e:
                        print(f"[DEBUG] history thumb failed: {e}", flush=True)
                    if not thumb_pixmap.isNull():
                        scaled = thumb_pixmap.scaled(80, 80, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                        x = max(0, (scaled.width() - 80) // 2)
                        y = max(0, (scaled.height() - 80) // 2)
                        lbl.setPixmap(scaled.copy(x, y, 80, 80))
                        lbl.setStyleSheet(
                            "background:" + theme['accent_softer'] + ";"
                            "border:1px solid " + theme['accent_border'] + "; border-radius:6px;"
                        )
                    else:
                        lbl.setText("🎬")
                        lbl.setStyleSheet(
                            "color:" + theme['accent'] + "; font-size:32px; background:" + theme['accent_softer'] + ";"
                            "border:1px solid " + theme['accent_border'] + "; border-radius:6px;"
                        )
                    lbl.setFixedSize(80, 80)
                    lbl._hist_file = img_file
                    thumb_labels.append(lbl)
                    img_container.addWidget(lbl)
                else:
                    pixmap = QPixmap(img_path)
                    if not pixmap.isNull():
                        lbl = ClickableLabel(None, file_ext=ext, file_path=img_path)
                        scaled = pixmap.scaled(80, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                        lbl.setPixmap(scaled)
                        lbl.setFixedSize(80, 80)
                        lbl._hist_file = img_file
                        thumb_labels.append(lbl)
                        img_container.addWidget(lbl)
            card_layout.addLayout(img_container)

            info_layout = QVBoxLayout()
            info_layout.setSpacing(4)

            prompt_text = record.get("prompt", "")
            if len(prompt_text) > 80:
                prompt_text = prompt_text[:80] + "..."
            prompt_lbl = QLabel(prompt_text)
            prompt_lbl.setWordWrap(True)
            prompt_lbl.setStyleSheet(f"color: {theme['text_primary']}; font-size: 12px; border: none; background: transparent;")
            info_layout.addWidget(prompt_lbl)

            unit = "个视频" if is_video_record else "张"
            meta = f"{record.get('time', '')}  |  {record.get('model', '')}  |  {record.get('quality', '')}  |  {record.get('ratio', '')}  |  {len(record.get('images', []))} {unit}"
            meta_lbl = QLabel(meta)
            meta_lbl.setStyleSheet(f"color: {theme['text_muted']}; font-size: 11px; border: none; background: transparent;")
            info_layout.addWidget(meta_lbl)

            info_layout.addStretch()
            card_layout.addLayout(info_layout, 1)

            dl_btn = QPushButton("下载")
            dl_btn.setCursor(Qt.PointingHandCursor)
            dl_btn.setStyleSheet(
                "QPushButton {"
                " background: " + theme['accent_softer'] + "; color: " + theme['accent'] + ";"
                " border: 1px solid " + theme['accent_border'] + "; border-radius: 6px;"
                " padding: 8px 16px; font-size: 12px;"
                " }"
                "QPushButton:hover { background: " + theme['accent_soft'] + "; }"
            )
            img_files = record.get("images", [])
            dl_btn.clicked.connect(lambda checked, files=img_files, labels=thumb_labels: self._download_history_images(files, labels))
            card_layout.addWidget(dl_btn)

            self.history_layout.addWidget(card)

        self.history_layout.addStretch()

    def _download_history_images(self, img_files, labels=None):
        download_files = []
        if labels:
            selected = [lbl._hist_file for lbl in labels if lbl.checked]
            if selected:
                download_files = selected
            else:
                download_files = img_files
        else:
            download_files = img_files

        valid_files = [f for f in download_files if os.path.exists(os.path.join(HISTORY_DIR, f))]
        if not valid_files:
            QMessageBox.warning(self, "提示", "文件已不存在")
            return
        if len(valid_files) == 1:
            src = os.path.join(HISTORY_DIR, valid_files[0])
            ext = os.path.splitext(valid_files[0])[1]
            default_name = f"history_{valid_files[0]}"
            is_video = ext.lower() == ".mp4"
            path, _ = QFileDialog.getSaveFileName(
                self, "保存视频" if is_video else "保存图片",
                os.path.join(os.path.expanduser("~"), "Desktop", default_name),
                f"{'视频文件' if is_video else '图片文件'} (*{ext});;所有文件 (*)"
            )
            if path:
                import shutil
                shutil.copy2(src, path)
                self.footer_status.setText(f"已保存到: {path}")
        else:
            folder = QFileDialog.getExistingDirectory(
                self, "选择保存文件夹",
                os.path.join(os.path.expanduser("~"), "Desktop")
            )
            if folder:
                import shutil
                for f in valid_files:
                    src = os.path.join(HISTORY_DIR, f)
                    shutil.copy2(src, os.path.join(folder, f))
                self.footer_status.setText(f"已保存 {len(valid_files)} 张到: {folder}")

    def _clear_history(self):
        reply = QMessageBox.question(
            self, "确认", "确定要清空所有历史记录吗？图片文件也会被删除。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            import shutil
            if os.path.exists(HISTORY_DIR):
                shutil.rmtree(HISTORY_DIR)
            ensure_history_dir()
            save_history([])
            self._refresh_history_page()
            self.footer_status.setText("历史记录已清空")

    def _open_history_dir(self):
        ensure_history_dir()
        try:
            if sys.platform.startswith("win"):
                os.startfile(HISTORY_DIR)
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", HISTORY_DIR])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", HISTORY_DIR])
            self.footer_status.setText(f"已打开: {HISTORY_DIR}")
        except Exception as e:
            QMessageBox.warning(self, "提示", f"无法打开文件夹：{e}\n路径：{HISTORY_DIR}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 9))

    while True:
        saved = load_api_key()
        ui = load_ui_settings()
        dlg = KeyDialog(prefill_key=saved, theme=ui.get("theme", DEFAULT_THEME))
        if dlg.exec_() != QDialog.Accepted:
            sys.exit(0)
        api_key = dlg.get_key()
        if dlg.should_remember():
            save_api_key(api_key)
        else:
            clear_api_key()

        window = MainWindow(api_key)
        window.show()
        exit_code = app.exec_()
        if exit_code == 1001:
            window.deleteLater()
            continue
        sys.exit(exit_code)
