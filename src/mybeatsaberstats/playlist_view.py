"""Playlist 画面 — ScoreSaber / BeatLeader / AccSaber / AccSaber Reloaded の
ランクマップ、または任意の .bplist ファイルを一覧表示してフィルタ・ソート・一括出力を行う画面。

AccSaber / AccSaber Reloaded は API から取得するためネットワーク接続が必要。
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import os
import re
import shutil
import tempfile
import threading
from urllib.parse import quote
import zipfile
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

from PySide6.QtCore import Qt, QObject, Signal, QUrl, QSize, QDate, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QFont, QPixmap, QDesktopServices, QIcon, QPalette
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QDateEdit,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QButtonGroup,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QProgressDialog,
    QApplication,
    QCheckBox,
    QMenu,
    QProxyStyle,
    QSlider,
    QStackedWidget,
    QStyle,
    QStyleOptionHeader,
    QStyleOptionViewItem,
    QTextBrowser,
    QStyledItemDelegate,
)

from .snapshot import BASE_DIR, RESOURCES_DIR
from .accsaber_reloaded import is_pending_difficulty as _is_rl_pending_difficulty
from .beatsaver_cache import load_beatsaver_meta_cache, update_beatsaver_meta_cache, upsert_beatsaver_meta_cache, _has_full_beatsaver_meta
from .settings_store import (
    load_beatsaber_dir as _load_beatsaber_dir_setting,
    load_playlist_export_dir as _load_playlist_export_dir_setting,
    save_beatsaber_dir as _save_beatsaber_dir_setting,
    save_playlist_export_dir as _save_playlist_export_dir_setting,
)
from .theme import detect_system_dark, is_dark, table_stylesheet


class _SwapSortArrowStyle(QProxyStyle):
    """ソート矢印を昇順=↑、降順=↓ の直感的表示にするプロキシスタイル。

    Qt デフォルトは昇順=↓、降順=↑ となっているため逆転する。
    """

    def drawPrimitive(self, element, option, painter, widget=None) -> None:
        if (
            element == QStyle.PrimitiveElement.PE_IndicatorHeaderArrow
            and isinstance(option, QStyleOptionHeader)
        ):
            opt = QStyleOptionHeader(option)
            si = opt.sortIndicator  # type: ignore[attr-defined]
            SortDown = QStyleOptionHeader.SortIndicator.SortDown  # type: ignore[attr-defined]
            SortUp = QStyleOptionHeader.SortIndicator.SortUp  # type: ignore[attr-defined]
            if si == SortDown:
                opt.sortIndicator = SortUp  # type: ignore[attr-defined]
            elif si == SortUp:
                opt.sortIndicator = SortDown  # type: ignore[attr-defined]
            super().drawPrimitive(element, opt, painter, widget)
        else:
            super().drawPrimitive(element, option, painter, widget)


class _PlaylistTableWidget(QTableWidget):
    @staticmethod
    def _blend_colors(base: QColor, overlay: QColor, alpha_override: Optional[float] = None) -> QColor:
        alpha = overlay.alphaF() if alpha_override is None else max(0.0, min(1.0, alpha_override))
        inv = 1.0 - alpha
        return QColor(
            round(base.red() * inv + overlay.red() * alpha),
            round(base.green() * inv + overlay.green() * alpha),
            round(base.blue() * inv + overlay.blue() * alpha),
        )

    def _selected_row_mask_color(self, row: int, active: bool) -> QColor:
        fill_color, _text_color = _NoFocusItemDelegate._selection_colors(active)
        if self.alternatingRowColors() and row % 2 == 1:
            base_color = self.palette().color(QPalette.ColorRole.AlternateBase)
        else:
            base_color = self.palette().color(QPalette.ColorRole.Base)
        effective_alpha = max(fill_color.alphaF() * 1.35, 0.18 if is_dark() else 0.14)
        blended = self._blend_colors(base_color, fill_color, effective_alpha)
        blended.setAlpha(255)
        return blended

    def paintEvent(self, event) -> None:  # type: ignore[override]
        selection_model = self.selectionModel()
        if selection_model is not None:
            rows = selection_model.selectedRows()
            if rows:
                active = self.window().isActiveWindow() if self.window() is not None else False
                fill_color, _text_color = _NoFocusItemDelegate._selection_colors(active)
                painter = QPainter(self.viewport())
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fill_color)
                max_x = max(0, self.viewport().width() - 1)
                for model_index in rows:
                    row = model_index.row()
                    if row < 0:
                        continue
                    top = self.rowViewportPosition(row)
                    if top < 0:
                        continue
                    painter.drawRect(0, top, max_x, self.rowHeight(row))
                painter.end()
        super().paintEvent(event)
        if selection_model is None:
            return
        rows = selection_model.selectedRows()
        if not rows:
            return
        line_color = QColor("#7fc7f3") if is_dark() else QColor("#67b7ee")
        max_x = max(0, self.viewport().width() - 1)
        painter = QPainter(self.viewport())
        painter.setPen(line_color)
        active = self.window().isActiveWindow() if self.window() is not None else False
        for model_index in rows:
            row = model_index.row()
            if row < 0:
                continue
            top = self.rowViewportPosition(row)
            if top < 0:
                continue
            bottom = top + self.rowHeight(row) - 1
            mask_default = self._selected_row_mask_color(row, active)
            mask_height = max(0, self.rowHeight(row) - 2)
            if mask_height > 0:
                for column in range(self.columnCount()):
                    if self.isColumnHidden(column):
                        continue
                    cell_left = self.columnViewportPosition(column)
                    cell_width = self.columnWidth(column)
                    mask_left = cell_left + 1
                    mask_width = min(4, max(0, cell_width - 2))
                    if mask_width <= 0 or mask_left >= max_x:
                        continue
                    mask_color = mask_default
                    item = self.item(row, column)
                    if item is not None:
                        background_brush = item.data(Qt.ItemDataRole.BackgroundRole)
                        if hasattr(background_brush, "style") and background_brush.style() != Qt.BrushStyle.NoBrush:
                            mask_color = background_brush.color()
                    painter.fillRect(mask_left, top + 1, mask_width, mask_height, mask_color)
            painter.drawLine(0, top, max_x, top)
            painter.drawLine(0, bottom, max_x, bottom)
        painter.end()


class _NoFocusItemDelegate(QStyledItemDelegate):
    @staticmethod
    def _selection_colors(active: bool) -> tuple[QColor, QColor]:
        if is_dark():
            if active:
                return QColor(127, 199, 243, 36), QColor("#e6f4ff")
            return QColor(142, 207, 246, 46), QColor("#f2f8ff")
        if active:
            return QColor(103, 183, 238, 31), QColor("#0f172a")
        return QColor(123, 196, 246, 41), QColor("#0f172a")

    @staticmethod
    def _paint_cell_text(
        painter: QPainter,
        option: QStyleOptionViewItem,
        index,
        text_color: QColor,
    ) -> None:
        font_data = index.data(Qt.ItemDataRole.FontRole)
        if isinstance(font_data, QFont):
            painter.setFont(font_data)
        else:
            painter.setFont(painter.font())

        painter.setPen(text_color)
        display_text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        alignment_data = index.data(Qt.ItemDataRole.TextAlignmentRole)
        if alignment_data is None:
            alignment = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        else:
            alignment = int(alignment_data)
        text_rect = option.rect.adjusted(4, 0, -4, 0)  # type: ignore[attr-defined]
        painter.drawText(text_rect, alignment, display_text)

    def paint(self, painter, option, index):  # type: ignore[override]
        opt = QStyleOptionViewItem(option)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)  # type: ignore[attr-defined]
        active = bool(opt.state & QStyle.StateFlag.State_Active)  # type: ignore[attr-defined]
        background_brush = index.data(Qt.ItemDataRole.BackgroundRole)
        foreground_brush = index.data(Qt.ItemDataRole.ForegroundRole)
        text_color = opt.palette.color(QPalette.ColorRole.Text)  # type: ignore[attr-defined]
        if selected:
            fill, text_color = self._selection_colors(active)
        else:
            fill = None
            if hasattr(foreground_brush, "color"):
                text_color = foreground_brush.color()

        painter.save()
        if hasattr(background_brush, "style") and background_brush.style() != Qt.BrushStyle.NoBrush:
            painter.fillRect(opt.rect, background_brush)  # type: ignore[arg-type]
        if fill is not None:
            painter.fillRect(opt.rect, fill)  # type: ignore[attr-defined]
        self._paint_cell_text(painter, opt, index, text_color)
        painter.restore()


class _PercentageBarDelegate(QStyledItemDelegate):
    def __init__(
        self,
        parent: Optional[QWidget] = None,
        max_value: float = 100.0,
        gradient_min: float = 0.0,
        dark_text_on_bar: Optional[str] = "#3333FF",
        dark_text_off_bar: Optional[str] = "#4499FF",
        light_text_on_bar: Optional[str] = "#2222FF",
        light_text_off_bar: Optional[str] = "#111199",
    ) -> None:
        super().__init__(parent)
        self._max_value = max_value
        self._min_value = gradient_min
        self._dark_text_on_bar = dark_text_on_bar
        self._dark_text_off_bar = dark_text_off_bar
        self._light_text_on_bar = light_text_on_bar
        self._light_text_off_bar = light_text_off_bar

    def _parse_value(self, value_str) -> Optional[float]:
        try:
            return float(str(value_str)) if value_str not in (None, "") else None
        except ValueError:
            return None

    def _bar_value(self, index) -> Optional[float]:
        user_val = index.data(Qt.ItemDataRole.UserRole)
        if user_val is not None:
            try:
                return float(user_val)
            except (TypeError, ValueError):
                pass
        return self._parse_value(index.data())

    def initStyleOption(self, option, index) -> None:  # type: ignore[override]
        super().initStyleOption(option, index)
        option.displayAlignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        value = self._bar_value(index)
        if value is not None and value >= self._max_value - 1e-3:
            option.font.setBold(True)

    def paint(self, painter, option, index):  # type: ignore[override]
        opt = QStyleOptionViewItem(option)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)  # type: ignore[attr-defined]
        active = bool(opt.state & QStyle.StateFlag.State_Active)  # type: ignore[attr-defined]
        background_brush = index.data(Qt.ItemDataRole.BackgroundRole)
        if selected:
            fill, text_color = _NoFocusItemDelegate._selection_colors(active)
        else:
            fill = None
            foreground_brush = index.data(Qt.ItemDataRole.ForegroundRole)
            if hasattr(foreground_brush, "color"):
                text_color = foreground_brush.color()
            else:
                text_color = opt.palette.color(QPalette.ColorRole.Text)  # type: ignore[attr-defined]

        value = self._bar_value(index)
        painter.save()
        if hasattr(background_brush, "style") and background_brush.style() != Qt.BrushStyle.NoBrush:
            painter.fillRect(opt.rect, background_brush)  # type: ignore[arg-type]
        if fill is not None:
            painter.fillRect(opt.rect, fill)  # type: ignore[attr-defined]
        if value is None or not (self._max_value > 0):
            _NoFocusItemDelegate._paint_cell_text(painter, opt, index, text_color)
            painter.restore()
            return

        if value <= self._min_value:
            ratio = 0.0
        else:
            span = self._max_value - self._min_value
            ratio = 0.0 if span <= 0 else (value - self._min_value) / span
        ratio = max(0.0, min(1.0, ratio))

        painter.save()
        rect = opt.rect.adjusted(1, 1, -1, -1)  # type: ignore[attr-defined]
        bar_width = int(rect.width() * ratio)
        bar_rect = rect.adjusted(0, 0, bar_width - rect.width(), 0)

        if ratio <= 0.5:
            t = ratio / 0.5 if ratio > 0 else 0.0
            r = 255
            g = int(255 * t)
            b = 0
        elif ratio <= 0.8:
            t = (ratio - 0.5) / 0.3
            r = int(255 * (1.0 - t))
            g = 255
            b = 0
        else:
            t = (ratio - 0.8) / 0.2
            r = 0
            g = 255
            b = int(255 * t / 2)
        painter.fillRect(bar_rect, QColor(r, g, b if ratio > 0.8 else 0, 180))
        painter.restore()

        bar_lum = 0.299 * r + 0.587 * g + 0.114 * b
        use_dark_text = ratio >= 0.3 and bar_lum > 140
        dark = is_dark()
        text_color_str = (
            self._dark_text_on_bar if dark else self._light_text_on_bar
        ) if use_dark_text else (
            self._dark_text_off_bar if dark else self._light_text_off_bar
        )
        if text_color_str is not None:
            text_color = QColor(text_color_str)
        font_data = index.data(Qt.ItemDataRole.FontRole)
        if isinstance(font_data, QFont):
            font = QFont(font_data)
        else:
            font = QFont(painter.font())
        if value >= self._max_value - 1e-3:
            font.setBold(True)
        painter.setFont(font)
        _NoFocusItemDelegate._paint_cell_text(painter, opt, index, text_color)
        painter.restore()

# ──────────────────────────────────────────────────────────────────────────────
# ソース定数
# ──────────────────────────────────────────────────────────────────────────────
SOURCE_SS = "ScoreSaber"
SOURCE_BL = "BeatLeader"
SOURCE_ACC = "AccSaber"
SOURCE_ACC_RL = "AccSaber RL"
SOURCE_BS = "BeatSaver"
SOURCE_OPEN = "Open File"
def _secondary_button_stylesheet() -> str:
    if is_dark():
        return (
            "QPushButton { background-color: #2a2a2a; color: #f0f0f0; "
            "border: 1px solid #5a5a5a; border-radius: 4px; padding: 1px 10px; }"
            "QPushButton:hover { background-color: #353535; border-color: #7a7a7a; }"
            "QPushButton:pressed { background-color: #232323; }"
            "QPushButton:disabled { background-color: #222222; color: #888888; border-color: #4a4a4a; }"
        )
    return (
        "QPushButton { background-color: #f7f7f7; color: #111111; "
        "border: 1px solid #cfcfcf; border-radius: 8px; padding: 1px 10px; }"
        "QPushButton:hover { background-color: #ffffff; border-color: #b9cbea; }"
        "QPushButton:pressed { background-color: #ececec; }"
        "QPushButton:disabled { background-color: #f1f1f1; color: #9a9a9a; border-color: #dddddd; }"
    )


def _is_windows_light_app_light() -> bool:
    return not is_dark() and not detect_system_dark()

# ステータス表示
STATUS_CLEARED = "✔"
STATUS_NF = "⚠NF"
STATUS_WARN = "⚠"    # NF 以外のモディファイアによる未公認クリア
STATUS_UNPLAYED = "✖"

# 難易度の表示順
_DIFF_ORDER: Dict[str, int] = {"Easy": 1, "Normal": 2, "Hard": 3, "Expert": 4, "ExpertPlus": 5}

_CACHE_DIR = BASE_DIR / "cache"
_COVER_CACHE_DIR = _CACHE_DIR / "covers"
_BATCH_CONFIG_PATH = _CACHE_DIR / "batch_configs.json"
_PLAYLIST_WINDOW_PATH = _CACHE_DIR / "playlist_window.json"


def _cover_cache_path(url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return _COVER_CACHE_DIR / f"{digest}.img"


def _read_cover_cache(url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        path = _cover_cache_path(url)
        if not path.exists():
            return None
        data = path.read_bytes()
        return data or None
    except Exception:
        return None


def _write_cover_cache(url: str, data: bytes) -> None:
    if not url or not data:
        return
    try:
        _COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cover_cache_path(url)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_bytes(data)
        tmp_path.replace(path)
    except Exception:
        pass


def load_playlist_export_dir() -> str:
    """前回のプレイリスト出力先フォルダを読み込む。"""
    return _load_playlist_export_dir_setting()


def save_playlist_export_dir(folder: str) -> None:
    """プレイリスト出力先フォルダを保存する。"""
    _save_playlist_export_dir_setting(folder)


def load_beatsaber_dir() -> str:
    """前回保存した Beat Saber フォルダを読み込む。"""
    return _load_beatsaber_dir_setting()


def save_beatsaber_dir(folder: str) -> None:
    """Beat Saber フォルダを保存する。"""
    _save_beatsaber_dir_setting(folder)


def load_playlist_batch_configs() -> List["_BatchConfig"]:
    """保存済みの Batch Export 設定を読み込む。"""
    try:
        if _BATCH_CONFIG_PATH.exists():
            data = json.loads(_BATCH_CONFIG_PATH.read_text(encoding="utf-8"))
            return [_BatchConfig.from_dict(item) for item in data]
    except Exception:
        pass
    return []


def load_enabled_playlist_batch_configs() -> List["_BatchConfig"]:
    """有効化されている Batch Export 設定だけを返す。"""
    return [config for config in load_playlist_batch_configs() if config.enabled]


def export_playlist_configs(
    steam_id: Optional[str],
    configs: List["_BatchConfig"],
    folder_path: Path,
    progress: Optional[Callable[[int, int, str], None]] = None,
    covers: Optional[Dict[str, str]] = None,
) -> Tuple[List[str], List[str]]:
    """指定された Batch Export 設定を使って同期的にプレイリストを書き出す。"""
    has_ss = any(config.source == "ss" for config in configs)
    has_bl = any(config.source == "bl" for config in configs)
    has_rl = any(config.source == "rl" for config in configs)
    has_acc = any(config.source == "acc" for config in configs)
    bs_configs = [config for config in configs if config.source == "bs"]

    ss_maps: List[MapEntry] = []
    bl_maps: List[MapEntry] = []
    rl_maps: List[MapEntry] = []
    acc_maps: List[MapEntry] = []

    n_load = (1 if has_ss else 0) + (1 if has_bl else 0) + (1 if has_rl else 0) + (1 if has_acc else 0) + len(bs_configs)
    total = n_load + len(configs)
    step = 0

    def _emit(label: str) -> None:
        if progress is not None:
            progress(step, total, label)

    if has_ss:
        _emit("Loading SS ranked maps...")
        ss_maps = load_ss_maps(steam_id)
        step += 1
    if has_bl:
        _emit("Loading BL ranked maps...")
        bl_maps = load_bl_maps(steam_id)
        step += 1
    if has_rl:
        _emit("Fetching AccSaber RL maps...")

        def _rl_prog(_done: int, _total: int, label: str) -> None:
            if progress is not None:
                progress(step, total, label)

        rl_maps = load_accsaber_reloaded_maps(steam_id, "all", on_progress=_rl_prog)
        step += 1
    if has_acc:
        _emit("Fetching AccSaber maps...")

        def _acc_prog(_done: int, _total: int, label: str) -> None:
            if progress is not None:
                progress(step, total, label)

        acc_maps = load_accsaber_maps(steam_id, "all", on_progress=_acc_prog)
        step += 1

    folder_path.mkdir(parents=True, exist_ok=True)
    resolved_covers = covers if covers is not None else _pregenerate_covers(configs)
    saved_files: List[str] = []
    errors: List[str] = []

    for config in configs:
        _emit(f"Exporting: {config.label}...")
        try:
            if config.source == "bs":
                _emit(f"Loading BeatSaver: {config.label}...")
                bs_from_dt = _parse_local_date_filter(config.bs_from_date)
                bs_to_dt = _parse_local_date_filter(config.bs_to_date, end_of_day=True)
                base_maps = load_beatsaver_maps(
                    steam_id=steam_id,
                    query=config.bs_query,
                    days=config.bs_days,
                    min_rating=config.bs_min_rating / 100.0,
                    min_votes=config.bs_min_votes,
                    max_maps=config.bs_max_maps,
                    from_dt=bs_from_dt,
                    to_dt=bs_to_dt,
                    unranked_only=config.bs_unranked_only,
                    exclude_ai=config.bs_exclude_ai,
                )
                step += 1
            else:
                base_maps = {"ss": ss_maps, "bl": bl_maps, "rl": rl_maps, "acc": acc_maps}.get(config.source, [])
            maps = _apply_config_filter(list(base_maps), config)
            _write_config_files(maps, config, folder_path, saved_files, errors, resolved_covers)
        except Exception as exc:
            errors.append(f"{config.label}: {exc}")
        step += 1

    return saved_files, errors


def export_all_playlist_batches(
    steam_id: Optional[str],
    folder_path: Path,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> Tuple[List[str], List[str]]:
    """有効な Batch Export 設定をすべて使って同期的にプレイリストを書き出す。"""
    configs = load_enabled_playlist_batch_configs()
    if not configs:
        return [], []
    return export_playlist_configs(steam_id, configs, folder_path, progress=progress)


def show_bplist_covers_dialog(
    parent: Optional[QWidget],
    title: str,
    folder: str,
    filenames: List[str],
    errors: List[str],
) -> None:
    """保存済み .bplist ファイルのカバー画像をサムネイルグリッドで表示する。"""
    import base64 as _b64

    dlg = QDialog(parent)
    dlg.setWindowTitle("Export Complete" if title.startswith("Export Complete") else title)
    dlg.resize(720, 540)

    outer = QVBoxLayout(dlg)
    outer.setSpacing(8)
    outer.setContentsMargins(12, 12, 12, 12)

    title_lbl = QLabel(title)
    title_lbl.setStyleSheet("font-weight: bold; font-size: 13px;")
    outer.addWidget(title_lbl)

    folder_row = QHBoxLayout()
    summary_lbl = QLabel(f"{len(filenames)} file(s) saved to:  {folder}")
    summary_lbl.setWordWrap(True)
    folder_row.addWidget(summary_lbl, 1)
    btn_open_folder = QPushButton("Open Folder")
    btn_open_folder.setFixedWidth(100)
    btn_open_folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)))
    folder_row.addWidget(btn_open_folder)
    outer.addLayout(folder_row)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    grid_widget = QWidget()
    grid = QGridLayout(grid_widget)
    grid.setSpacing(10)
    grid.setContentsMargins(8, 8, 8, 8)
    scroll.setWidget(grid_widget)

    cols = 5
    thumb = 100

    for idx, fname in enumerate(filenames):
        fpath = Path(folder) / fname
        pm = QPixmap()
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            img_data = data.get("image", "")
            if img_data.startswith("data:"):
                raw = _b64.b64decode(img_data.split(",", 1)[1])
                pm.loadFromData(raw)
        except Exception:
            pass

        img_lbl = QLabel()
        if not pm.isNull():
            img_lbl.setPixmap(
                pm.scaled(
                    thumb,
                    thumb,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            img_lbl.setText("(no image)")
        img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        img_lbl.setFixedSize(thumb + 4, thumb + 4)
        img_lbl.setStyleSheet("border: 1px solid #555; background: #1a1a1a;")

        short = fname if len(fname) <= 16 else fname[:7] + "…" + fname[-7:]
        name_lbl = QLabel(short)
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_lbl.setWordWrap(True)
        name_lbl.setMaximumWidth(thumb + 4)
        name_lbl.setToolTip(fname)

        cell = QWidget()
        cell_layout = QVBoxLayout(cell)
        cell_layout.setSpacing(2)
        cell_layout.setContentsMargins(0, 0, 0, 0)
        cell_layout.addWidget(img_lbl)
        cell_layout.addWidget(name_lbl)

        row, col = divmod(idx, cols)
        grid.addWidget(cell, row, col)

    outer.addWidget(scroll, 1)

    if errors:
        err_lbl = QLabel("Errors:\n" + "\n".join(errors[:10]))
        err_lbl.setStyleSheet("color: #ff6666;")
        err_lbl.setWordWrap(True)
        outer.addWidget(err_lbl)

    btn_row = QHBoxLayout()
    btn_row.addStretch()
    btn_ok = QPushButton("OK")
    btn_ok.setDefault(True)
    btn_ok.clicked.connect(dlg.accept)
    btn_row.addWidget(btn_ok)
    outer.addLayout(btn_row)

    dlg.exec()


# ──────────────────────────────────────────────────────────────────────────────
# データクラス
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class MapEntry:
    """プレイリスト画面の 1 行を表す。"""
    song_name: str
    song_author: str
    mapper: str
    song_hash: str          # 大文字ハッシュ
    difficulty: str         # ExpertPlus など
    mode: str               # Standard など
    stars: float
    max_pp: float           # SS: maxPP (多くは 0), BL: 0 (starsを代替使用)
    player_pp: float        # プレイヤーの取得 PP (0 = 未プレイ)
    cleared: bool           # NF/SS/NA なしクリア済み
    nf_clear: bool          # NF 付きクリアあり (cleared=False の場合)
    player_acc: float       # プレイヤーの精度 % (0 = 未プレイ)
    player_rank: int        # プレイヤーのランク (0 = 未プレイ)
    leaderboard_id: str     # SS: leaderboard id, BL: map id
    source: str             # "scoresaber" | "beatleader" | "open"
    acc_category: str = ""  # AccSaber / AccSaber Reloaded のカテゴリ (true/standard/tech)
    acc_rl_ap: float = 0.0  # AccSaber Reloaded AP (0 = 未取得 / 未プレイ)
    acc_complexity: float = 0.0  # AccSaber / AccSaber Reloaded の Complexity
    player_mods: str = ""   # 実際に使用したモディファイア文字列 (例: "NF", "SC", "NF,SC")
    full_combo: bool = False  # フルコンボ達成済み
    score_source: str = ""  # スコア表示元: "BL" | "SS" | "AS" | ""
    duration_seconds: int = 0  # 譜面時間 (秒)
    played_at_ts: int = 0  # プレイ日時 (Unix秒)
    source_date_ts: int = 0  # ソース側の日付 (Ranked / Published など)
    pending: bool = False
    beatsaver_key: str = ""
    beatsaver_cover_url: str = ""
    beatsaver_preview_url: str = ""
    beatsaver_page_url: str = ""
    beatsaver_download_url: str = ""
    beatsaver_rating: float = 0.0
    beatsaver_votes: int = 0
    beatsaver_upvotes: int = 0
    beatsaver_downvotes: int = 0
    beatsaver_uploaded_ts: int = 0
    beatsaver_description: str = ""
    beatleader_page_url: str = ""
    beatleader_replay_url: str = ""
    beatleader_global1_replay_url: str = ""
    beatleader_local1_replay_url: str = ""
    beatleader_attempts: int = 0
    beatleader_plays: int = 0

    @property
    def status_str(self) -> str:
        queued_suffix = "[Q]" if self.pending else ""
        if self.cleared:
            return f"{STATUS_CLEARED}{queued_suffix}"
        if self.nf_clear:
            mods_upper = self.player_mods.upper()
            if not self.player_mods or "NF" in mods_upper:
                return f"{STATUS_NF}{queued_suffix}"
            # SC 等の NF 以外のモディファイアで無効化されている場合
            return f"{STATUS_WARN}{self.player_mods}{queued_suffix}"
        return f"{STATUS_UNPLAYED}{queued_suffix}"

    @property
    def sort_stars(self) -> float:
        return self.stars

    @property
    def played(self) -> bool:
        return self.cleared or self.nf_clear

    @property
    def display_song_name(self) -> str:
        return self.song_name

    @property
    def beatleader_success_rate(self) -> float:
        if self.beatleader_attempts <= 0:
            return 0.0
        return self.beatleader_plays / self.beatleader_attempts * 100.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MapEntry":
        known = {field_name for field_name in cls.__dataclass_fields__.keys()}  # type: ignore[attr-defined]
        return cls(**{key: value for key, value in data.items() if key in known})


# ──────────────────────────────────────────────────────────────────────────────
# ヘルパー
# ──────────────────────────────────────────────────────────────────────────────

def _diff_from_raw(raw_str: str, diff_num: int = 0) -> str:
    """SS の difficultyRaw / difficulty 番号から表示名へ変換。"""
    if raw_str:
        for pat, name in [
            ("_ExpertPlus_", "ExpertPlus"),
            ("_Expert_", "Expert"),
            ("_Hard_", "Hard"),
            ("_Normal_", "Normal"),
            ("_Easy_", "Easy"),
        ]:
            if pat in raw_str:
                return name
    _num_map = {1: "Easy", 3: "Normal", 5: "Hard", 7: "Expert", 9: "ExpertPlus"}
    return _num_map.get(diff_num, str(diff_num))


def _mode_from_gamemode(game_mode: str) -> str:
    """SoloStandard → Standard"""
    return game_mode.replace("Solo", "") if game_mode else "Standard"


def _parse_iso_datetime_to_ts(value: object) -> int:
    if not isinstance(value, str) or not value:
        return 0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _parse_unix_datetime_to_ts(value: object) -> int:
    if value is None:
        return 0
    try:
        if isinstance(value, (int, float, str)):
            raw_value = value
        else:
            raw_value = str(value)
        return int(float(raw_value))
    except (TypeError, ValueError):
        return 0


def _parse_local_date_filter(value: object, *, end_of_day: bool = False) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    else:
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.astimezone().astimezone(timezone.utc)


def _apply_beatsaver_meta(entry: MapEntry, meta: Optional[dict]) -> None:
    beatsaver_key = str(entry.beatsaver_key or (meta or {}).get("beatsaver_key") or "").strip()
    if beatsaver_key and not entry.beatsaver_key:
        entry.beatsaver_key = beatsaver_key
    if beatsaver_key:
        if not entry.beatsaver_page_url:
            entry.beatsaver_page_url = f"https://beatsaver.com/maps/{beatsaver_key}"
        if not entry.beatsaver_download_url:
            entry.beatsaver_download_url = f"https://beatsaver.com/api/download/key/{beatsaver_key}"

    if not meta:
        return

    if not entry.beatsaver_cover_url:
        entry.beatsaver_cover_url = str(meta.get("beatsaver_cover_url") or "")
    if not entry.beatsaver_preview_url:
        entry.beatsaver_preview_url = str(meta.get("beatsaver_preview_url") or "")
    if not entry.beatsaver_page_url:
        entry.beatsaver_page_url = str(meta.get("beatsaver_page_url") or "")
    if not entry.beatsaver_download_url:
        entry.beatsaver_download_url = str(meta.get("beatsaver_download_url") or "")
    if not entry.beatsaver_description:
        entry.beatsaver_description = str(meta.get("beatsaver_description") or "")
    if entry.beatsaver_uploaded_ts <= 0:
        entry.beatsaver_uploaded_ts = int(meta.get("beatsaver_uploaded_ts") or 0)
    if entry.beatsaver_votes <= 0:
        entry.beatsaver_votes = int(meta.get("beatsaver_votes") or 0)
    if entry.beatsaver_upvotes <= 0:
        entry.beatsaver_upvotes = int(meta.get("beatsaver_upvotes") or 0)
    if entry.beatsaver_downvotes <= 0:
        entry.beatsaver_downvotes = int(meta.get("beatsaver_downvotes") or 0)
    if entry.beatsaver_rating <= 0:
        entry.beatsaver_rating = float(meta.get("beatsaver_rating") or 0.0)


def _enrich_entries_with_beatsaver_cache(entries: List[MapEntry]) -> List[MapEntry]:
    if not entries:
        return entries
    cache = load_beatsaver_meta_cache()
    for entry in entries:
        song_hash = (entry.song_hash or "").upper()
        _apply_beatsaver_meta(entry, cache.get(song_hash))
    return entries


def _cache_beatsaver_meta_from_entries(entries: List[MapEntry]) -> List[MapEntry]:
    meta_entries = []
    for entry in entries:
        song_hash = (entry.song_hash or "").upper()
        if not song_hash:
            continue
        if not any(
            (
                entry.beatsaver_key,
                entry.beatsaver_page_url,
                entry.beatsaver_download_url,
                entry.beatsaver_cover_url,
                entry.beatsaver_preview_url,
                entry.beatsaver_description,
            )
        ):
            continue
        meta_entries.append({
            "hash": song_hash,
            "beatsaver_key": str(entry.beatsaver_key or "").strip(),
            "beatsaver_page_url": str(entry.beatsaver_page_url or "").strip(),
            "beatsaver_download_url": str(entry.beatsaver_download_url or "").strip(),
            "beatsaver_cover_url": str(entry.beatsaver_cover_url or "").strip(),
            "beatsaver_preview_url": str(entry.beatsaver_preview_url or "").strip(),
            "beatsaver_description": str(entry.beatsaver_description or ""),
            "beatsaver_uploaded_ts": int(entry.beatsaver_uploaded_ts or 0),
            "beatsaver_rating": float(entry.beatsaver_rating or 0.0),
            "beatsaver_votes": int(entry.beatsaver_votes or 0),
            "beatsaver_upvotes": int(entry.beatsaver_upvotes or 0),
            "beatsaver_downvotes": int(entry.beatsaver_downvotes or 0),
            "song_name": str(entry.song_name or ""),
            "song_author": str(entry.song_author or ""),
            "mapper": str(entry.mapper or ""),
        })
    if meta_entries:
        upsert_beatsaver_meta_cache(meta_entries)
    return _enrich_entries_with_beatsaver_cache(entries)


def _collect_beatsaver_cache_targets(entries: List[MapEntry]) -> Tuple[List[str], Dict[str, str]]:
    missing_hashes: List[str] = []
    seed_map: Dict[str, str] = {}
    seen_hashes: set[str] = set()
    for entry in entries:
        song_hash = (entry.song_hash or "").upper()
        if not song_hash or song_hash in seen_hashes:
            continue
        seen_hashes.add(song_hash)
        if entry.beatsaver_key:
            seed_map[song_hash] = str(entry.beatsaver_key).strip()
        has_link = bool(entry.beatsaver_key or entry.beatsaver_page_url or entry.beatsaver_download_url)
        if not has_link:
            missing_hashes.append(song_hash)
    return missing_hashes, seed_map


def _cache_missing_beatsaver_metadata(
    entries: List[MapEntry],
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> List[MapEntry]:
    missing_hashes, seed_map = _collect_beatsaver_cache_targets(entries)
    if not missing_hashes and not seed_map:
        return _enrich_entries_with_beatsaver_cache(entries)

    def _progress(done: int, total: int) -> None:
        if on_progress is not None:
            on_progress(done, total, f"Updating BeatSaver metadata... {done}/{total}")

    total = len(missing_hashes)
    if total > 0 and on_progress is not None:
        on_progress(0, total, f"Updating BeatSaver metadata... 0/{total}")
    if missing_hashes or seed_map:
        update_beatsaver_meta_cache(missing_hashes, seed_map=seed_map, on_progress=_progress)
    return _enrich_entries_with_beatsaver_cache(entries)


# ──────────────────────────────────────────────────────────────────────────────
# データ読み込み
# ──────────────────────────────────────────────────────────────────────────────

def _ss_player_score_info(scores: Dict, lb_id: str, max_score_from_map: int = 0
                          ) -> Tuple[float, bool, bool, float, int, str]:
    """SS player scores から (player_pp, cleared, nf_clear, acc, rank, mods) を返す。"""
    entry = scores.get(str(lb_id))
    if not entry:
        return 0.0, False, False, 0.0, 0, ""
    sc = entry.get("score", {})
    player_pp = float(sc.get("pp") or 0)
    base_score = int(sc.get("baseScore") or 0)
    modifiers = (sc.get("modifiers") or "").upper()
    # SS modifiers は "NFSC" のように連結されているので 2 文字ずつカンマ区切りに正規化
    mods_str = ",".join(modifiers[i:i+2] for i in range(0, len(modifiers), 2)) if modifiers else ""
    rank = int(sc.get("rank") or 0)
    # 精度算出 (max_score が 0 の場合は 0%)
    acc = (base_score / max_score_from_map * 100.0) if max_score_from_map > 0 and base_score > 0 else 0.0
    has_nf = "NF" in modifiers
    cleared = base_score > 0 and not has_nf
    nf_clear = base_score > 0 and has_nf
    return player_pp, cleared, nf_clear, acc, rank, mods_str


def _bl_player_score_info(scores: Dict, map_id: str
                          ) -> Tuple[float, bool, bool, float, int, str]:
    """BL player scores から (player_pp, cleared, nf_clear, acc, rank, mods) を返す。"""
    entry = scores.get(str(map_id))
    if not entry:
        return 0.0, False, False, 0.0, 0, ""
    player_pp = float(entry.get("pp") or 0)
    base_score = int(entry.get("baseScore") or 0)
    modifiers = (entry.get("modifiers") or "").upper()
    # BL modifiers は "NF,SC" 形式だが念のため正規化
    mods_str = ",".join(m.strip() for m in modifiers.replace(",", " ").split() if m.strip()) if modifiers else ""
    accuracy = float(entry.get("accuracy") or 0)
    rank = int(entry.get("rank") or 0)
    acc = accuracy * 100.0 if accuracy else 0.0
    has_nf = "NF" in modifiers
    cleared = base_score > 0 and not has_nf
    nf_clear = base_score > 0 and has_nf
    return player_pp, cleared, nf_clear, acc, rank, mods_str


def _ss_player_score_timeset(scores: Dict, lb_id: str) -> int:
    entry = scores.get(str(lb_id)) or {}
    sc = entry.get("score", {})
    return _parse_iso_datetime_to_ts(sc.get("timeSet"))


def _bl_player_score_timeset(scores: Dict, map_id: str) -> int:
    entry = scores.get(str(map_id)) or {}
    return _parse_unix_datetime_to_ts(entry.get("timeset"))


_MAP_ENTRIES_CACHE: Dict[Tuple[str, str, bool, Tuple[Tuple[str, bool, int, int], ...]], List[MapEntry]] = {}


def _file_signature(*paths: Path) -> Tuple[Tuple[str, bool, int, int], ...]:
    signature: List[Tuple[str, bool, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
            signature.append((str(path), True, stat.st_mtime_ns, stat.st_size))
        except OSError:
            signature.append((str(path), False, 0, 0))
    return tuple(signature)


def _clone_entries(entries: List[MapEntry]) -> List[MapEntry]:
    return [replace(entry) for entry in entries]


def load_ss_maps(steam_id: Optional[str] = None, filter_stars: bool = True) -> List[MapEntry]:
    """ScoreSaber ランクマップをキャッシュから読み込む。

    filter_stars=False にすると stars=0 のマップも返す（AccSaber 用）。
    """
    path = _CACHE_DIR / "scoresaber_ranked_maps.json"
    if not path.exists():
        return []
    ss_player_path = _CACHE_DIR / f"scoresaber_player_scores_{steam_id}.json" if steam_id else Path()
    bl_ranked_path = _CACHE_DIR / "beatleader_ranked_maps.json"
    bl_player_path = _CACHE_DIR / f"beatleader_player_scores_{steam_id}.json" if steam_id else Path()
    cache_key = (
        "ss",
        steam_id or "",
        bool(filter_stars),
        _file_signature(path, ss_player_path, bl_ranked_path, bl_player_path),
    )
    cached_entries = _MAP_ENTRIES_CACHE.get(cache_key)
    if cached_entries is not None:
        return _enrich_entries_with_beatsaver_cache(_clone_entries(cached_entries))

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    # トップレベル構造: {fetched_at: ..., leaderboards: {lb_id: {...}, ...}, ...}
    if "leaderboards" in raw:
        maps_dict: Dict[str, dict] = raw["leaderboards"]
    else:
        # 旧フォーマット互換: トップレベルに直接マップがある場合
        maps_dict = {
            k: v for k, v in raw.items()
            if k not in ("fetched_at", "max_pages", "total_maps") and isinstance(v, dict)
        }

    # プレイヤースコアを読み込む
    ss_scores: Dict[str, dict] = {}
    if steam_id:
        if ss_player_path.exists():
            try:
                sd = json.loads(ss_player_path.read_text(encoding="utf-8"))
                ss_scores = sd.get("scores", {})
            except Exception:
                pass

    # ScoreSaber キャッシュには譜面時間がないため BL キャッシュから補完する
    bl_maps = load_bl_maps(steam_id)
    bl_duration_index = {
        (e.song_hash.upper(), e.mode, e.difficulty): e.duration_seconds
        for e in bl_maps
        if e.duration_seconds > 0
    }
    bl_link_index = {
        (e.song_hash.upper(), e.mode, e.difficulty): e
        for e in bl_maps
        if e.leaderboard_id or e.beatleader_page_url or e.beatleader_replay_url
    }

    entries: List[MapEntry] = []
    for lb_id_str, m in maps_dict.items():
        stars = float(m.get("stars") or 0)
        if filter_stars and stars <= 0:
            continue

        diff_info = m.get("difficulty", {})
        diff_num = int(diff_info.get("difficulty") or 0)
        diff_raw = diff_info.get("difficultyRaw") or ""
        game_mode = diff_info.get("gameMode") or "SoloStandard"
        max_score = int(m.get("maxScore") or 0)
        song_hash = (m.get("songHash") or "").upper()
        difficulty = _diff_from_raw(diff_raw, diff_num)
        mode = _mode_from_gamemode(game_mode)
        ranked_date_ts = _parse_iso_datetime_to_ts(m.get("rankedDate") or m.get("qualifiedDate"))
        bl_link_entry = bl_link_index.get((song_hash, mode, difficulty))

        player_pp, cleared, nf_clear, acc, rank, mods = _ss_player_score_info(
            ss_scores, lb_id_str, max_score
        )
        played_at_ts = _ss_player_score_timeset(ss_scores, lb_id_str)
        _fc = bool((ss_scores.get(str(lb_id_str)) or {}).get("score", {}).get("fullCombo"))

        entries.append(MapEntry(
            song_name=m.get("songName") or "",
            song_author=m.get("songAuthorName") or "",
            mapper=m.get("levelAuthorName") or "",
            song_hash=song_hash,
            difficulty=difficulty,
            mode=mode,
            stars=stars,
            max_pp=float(m.get("maxPP") or 0),
            player_pp=player_pp,
            cleared=cleared,
            nf_clear=nf_clear,
            player_acc=acc,
            player_rank=rank,
            leaderboard_id=lb_id_str,
            source="scoresaber",
            player_mods=mods,
            full_combo=_fc,
            score_source="SS",
            duration_seconds=bl_duration_index.get((song_hash, mode, difficulty), 0),
            played_at_ts=played_at_ts,
            source_date_ts=ranked_date_ts,
            beatleader_page_url=bl_link_entry.beatleader_page_url if bl_link_entry else "",
            beatleader_replay_url=bl_link_entry.beatleader_replay_url if bl_link_entry else "",
        ))

    _MAP_ENTRIES_CACHE[cache_key] = _clone_entries(entries)
    return _enrich_entries_with_beatsaver_cache(entries)


def load_bl_maps(steam_id: Optional[str] = None) -> List[MapEntry]:
    """BeatLeader ランクマップをキャッシュから読み込む。"""
    path = _CACHE_DIR / "beatleader_ranked_maps.json"
    if not path.exists():
        return []
    bl_player_path = _CACHE_DIR / f"beatleader_player_scores_{steam_id}.json" if steam_id else Path()
    cache_key = (
        "bl",
        steam_id or "",
        True,
        _file_signature(path, bl_player_path),
    )
    cached_entries = _MAP_ENTRIES_CACHE.get(cache_key)
    if cached_entries is not None:
        return _enrich_entries_with_beatsaver_cache(_clone_entries(cached_entries))

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    all_maps: List[dict] = []
    for page in raw.get("pages", []):
        page_data = page.get("data", {})
        if isinstance(page_data, dict):
            inner = page_data.get("data", [])
            if isinstance(inner, list):
                all_maps.extend(inner)

    # プレイヤースコアを読み込む
    bl_scores: Dict[str, dict] = {}
    if steam_id:
        if bl_player_path.exists():
            try:
                bd = json.loads(bl_player_path.read_text(encoding="utf-8"))
                bl_scores = bd.get("scores", {})
            except Exception:
                pass

    entries: List[MapEntry] = []
    bl_replay_idx = _build_bl_replay_hash_index(bl_scores) if bl_scores else {}
    for m in all_maps:
        diff = m.get("difficulty", {})
        song = m.get("song", {})
        map_id = str(m.get("id") or "")
        stars = float(diff.get("stars") or 0)
        ranked_date_ts = int(diff.get("rankedTime") or diff.get("qualifiedTime") or diff.get("nominatedTime") or 0)
        diff_name = diff.get("difficultyName") or "ExpertPlus"
        mode_name = diff.get("modeName") or "Standard"
        song_hash = (song.get("hash") or "").upper()
        replay_url = bl_replay_idx.get((song_hash, mode_name, diff_name), "")

        player_pp, cleared, nf_clear, acc, rank, mods = _bl_player_score_info(bl_scores, map_id)
        played_at_ts = _bl_player_score_timeset(bl_scores, map_id)
        _fc = bool((bl_scores.get(str(map_id)) or {}).get("fullCombo"))

        entries.append(MapEntry(
            song_name=song.get("name") or "",
            song_author=song.get("author") or "",
            mapper=song.get("mapper") or "",
            song_hash=song_hash,
            difficulty=diff_name,
            mode=mode_name,
            stars=stars,
            max_pp=0.0,
            player_pp=player_pp,
            cleared=cleared,
            nf_clear=nf_clear,
            player_acc=acc,
            player_rank=rank,
            leaderboard_id=map_id,
            source="beatleader",
            player_mods=mods,
            full_combo=_fc,
            score_source="BL",
            duration_seconds=_normalize_duration_seconds(song.get("duration")),
            played_at_ts=played_at_ts,
            source_date_ts=ranked_date_ts,
            beatleader_page_url=f"https://beatleader.com/leaderboard/global/{map_id}" if map_id else "",
            beatleader_replay_url=replay_url,
            beatleader_attempts=int(m.get("attempts") or 0),
            beatleader_plays=int(m.get("plays") or 0),
        ))

    _MAP_ENTRIES_CACHE[cache_key] = _clone_entries(entries)
    return _enrich_entries_with_beatsaver_cache(entries)


def _build_ss_score_hash_index(
    ss_scores: Dict[str, dict],
) -> Dict[Tuple[str, str, str], Tuple[float, bool, bool, float, int, str, int]]:
    """SS player scores キャッシュから (hash, mode, diff) → (pp, cleared, nf_clear, acc, rank, mods, played_at_ts) を構築。

    SS ranked maps に含まれない Easy 等のマップもカバーするため AccSaber 用途で使用。
    """
    idx: Dict[Tuple[str, str, str], Tuple[float, bool, bool, float, int, str, int]] = {}
    for lb_id_str, entry in ss_scores.items():
        lb = entry.get("leaderboard", {})
        song_hash = (lb.get("songHash") or "").upper()
        if not song_hash:
            continue
        diff_info = lb.get("difficulty", {})
        diff_raw = diff_info.get("difficultyRaw") or ""
        diff_num = int(diff_info.get("difficulty") or 0)
        game_mode = diff_info.get("gameMode") or "SoloStandard"
        diff_name = _diff_from_raw(diff_raw, diff_num)
        mode = _mode_from_gamemode(game_mode)
        max_score = int(lb.get("maxScore") or 0)
        pp, cleared, nf_clear, acc, rank, mods = _ss_player_score_info(ss_scores, lb_id_str, max_score)
        played_at_ts = _ss_player_score_timeset(ss_scores, lb_id_str)
        key = (song_hash, mode, diff_name)
        # クリア済み優先、同キーに複数スコアがあれば最高 pp を保持
        if key not in idx or (cleared and not idx[key][1]) or pp > idx[key][0]:
            idx[key] = (pp, cleared, nf_clear, acc, rank, mods, played_at_ts)
    return idx


def _build_ss_hash_index(entries: List[MapEntry]) -> Dict[Tuple[str, str, str], MapEntry]:
    """hash+mode+diff → MapEntry のインデックスを構築。"""
    idx: Dict[Tuple[str, str, str], MapEntry] = {}
    for e in entries:
        key = (e.song_hash.upper(), e.mode, e.difficulty)
        idx[key] = e
    return idx


def _build_bl_hash_index(entries: List[MapEntry]) -> Dict[Tuple[str, str, str], MapEntry]:
    return _build_ss_hash_index(entries)


def _build_bl_score_hash_index(
    bl_scores: Dict[str, dict],
) -> Dict[Tuple[str, str, str], Tuple[float, bool, bool, float, int, str, int]]:
    """BL player scores キャッシュから (hash, mode, diff) のインデックスを構築する。"""
    idx: Dict[Tuple[str, str, str], Tuple[float, bool, bool, float, int, str, int]] = {}
    for map_id, entry in bl_scores.items():
        lb = entry.get("leaderboard") or {}
        song = lb.get("song") or {}
        diff = lb.get("difficulty") or {}
        song_hash = (song.get("hash") or "").upper()
        if not song_hash:
            continue
        diff_name = diff.get("difficultyName") or "ExpertPlus"
        mode = diff.get("modeName") or "Standard"
        pp, cleared, nf_clear, acc, rank, mods = _bl_player_score_info(bl_scores, str(map_id))
        played_at_ts = _bl_player_score_timeset(bl_scores, str(map_id))
        key = (song_hash, mode, diff_name)
        if key not in idx or (cleared and not idx[key][1]) or pp > idx[key][0]:
            idx[key] = (pp, cleared, nf_clear, acc, rank, mods, played_at_ts)
    return idx


def _build_bl_replay_hash_index(bl_scores: Dict[str, dict]) -> Dict[Tuple[str, str, str], str]:
    idx: Dict[Tuple[str, str, str], str] = {}
    best_meta: Dict[Tuple[str, str, str], Tuple[bool, float, int]] = {}
    for map_id, entry in bl_scores.items():
        lb = entry.get("leaderboard") or {}
        song = lb.get("song") or {}
        diff = lb.get("difficulty") or {}
        song_hash = (song.get("hash") or "").upper()
        if not song_hash:
            continue
        diff_name = diff.get("difficultyName") or "ExpertPlus"
        mode = diff.get("modeName") or "Standard"
        key = (song_hash, mode, diff_name)
        score_id = str(entry.get("id") or entry.get("originalId") or "").strip()
        if not score_id:
            continue
        pp = float(entry.get("pp") or 0.0)
        played_at_ts = _bl_player_score_timeset(bl_scores, str(map_id))
        cleared = bool(entry.get("baseScore") or 0)
        current = best_meta.get(key)
        candidate = (cleared, pp, played_at_ts)
        if current is None or candidate > current:
            best_meta[key] = candidate
            idx[key] = f"https://replay.beatleader.com/?scoreId={score_id}"
    return idx


def _build_bl_leaderboard_hash_index(bl_scores: Dict[str, dict]) -> Dict[Tuple[str, str, str], str]:
    idx: Dict[Tuple[str, str, str], str] = {}
    best_meta: Dict[Tuple[str, str, str], Tuple[bool, float, int]] = {}
    for map_id, entry in bl_scores.items():
        lb = entry.get("leaderboard") or {}
        song = lb.get("song") or {}
        diff = lb.get("difficulty") or {}
        song_hash = (song.get("hash") or "").upper()
        if not song_hash:
            continue
        diff_name = diff.get("difficultyName") or "ExpertPlus"
        mode = diff.get("modeName") or "Standard"
        leaderboard_id = str(entry.get("leaderboardId") or lb.get("id") or map_id or "")
        if not leaderboard_id:
            continue
        key = (song_hash, mode, diff_name)
        pp = float(entry.get("pp") or 0.0)
        played_at_ts = _bl_player_score_timeset(bl_scores, str(map_id))
        cleared = bool(entry.get("baseScore") or 0)
        candidate = (cleared, pp, played_at_ts)
        current = best_meta.get(key)
        if current is None or candidate > current:
            best_meta[key] = candidate
            idx[key] = leaderboard_id
    return idx


def _fetch_bl_leaderboards_by_hash(session: requests.Session, song_hash: str) -> Dict[Tuple[str, str], str]:
    if not song_hash:
        return {}
    try:
        resp = session.get(f"https://api.beatleader.xyz/leaderboards/hash/{song_hash}", timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return {}

    result: Dict[Tuple[str, str], str] = {}
    for item in payload.get("leaderboards") or []:
        diff = item.get("difficulty") or {}
        difficulty = str(diff.get("difficultyName") or "ExpertPlus")
        mode = str(diff.get("modeName") or "Standard")
        leaderboard_id = str(item.get("id") or "")
        if leaderboard_id:
            result[(mode, difficulty)] = leaderboard_id
    return result


def _fetch_bl_top_replay_url(
    session: requests.Session,
    leaderboard_id: str,
    countries: str = "",
) -> str:
    if not leaderboard_id:
        return ""
    params = {
        "page": 1,
        "count": 1,
        "sortBy": "rank",
        "order": "desc",
    }
    if countries:
        params["countries"] = countries
    try:
        resp = session.get(f"https://api.beatleader.xyz/leaderboard/{leaderboard_id}", params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return ""

    scores = payload.get("scores") or []
    if not scores:
        return ""
    score_id = str(scores[0].get("id") or scores[0].get("originalId") or "").strip()
    if not score_id:
        return ""
    return f"https://replay.beatleader.com/?scoreId={score_id}"


def _normalize_duration_seconds(value: object) -> int:
    if value is None:
        return 0
    try:
        if isinstance(value, (int, float, str)):
            raw_value = value
        else:
            raw_value = str(value)
        seconds = int(round(float(raw_value)))
    except (TypeError, ValueError):
        return 0
    return seconds if seconds > 0 else 0


def load_bplist_maps(
    bplist_path: Path,
    service: str,
    steam_id: Optional[str] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> List[MapEntry]:
    """開いた .bplist ファイルをロードし、service に応じてランク情報を付与する。

    service: "scoresaber" | "beatleader" | "accsaber_rl" | "none"
    on_progress: callable(done, total, label) — accsaber_rl 時のみ使用
    """
    try:
        bplist = json.loads(bplist_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"bplist load error: {e}") from e

    songs = bplist.get("songs") or bplist.get("Songs") or []

    if service == "scoresaber":
        ranked = load_ss_maps(steam_id)
        idx = _build_ss_hash_index(ranked)
    elif service == "beatleader":
        ranked = load_bl_maps(steam_id)
        idx = _build_bl_hash_index(ranked)
    elif service == "accsaber_rl":
        ranked = load_accsaber_reloaded_maps(steam_id, "all", on_progress=on_progress)
        idx = _build_ss_hash_index(ranked)
    elif service == "accsaber":
        ranked = load_accsaber_maps(steam_id, "all", on_progress=on_progress)
        idx = _build_ss_hash_index(ranked)
    else:
        idx = {}
        ranked = []

    entries: List[MapEntry] = []
    for song in songs:
        s_hash = (song.get("hash") or "").upper()
        s_name = song.get("songName") or ""
        diffs = song.get("difficulties") or []

        if not diffs:
            # 難易度指定なし → idx からハッシュで探す
            for e in (ranked or []):
                if e.song_hash == s_hash:
                    entries.append(e)
            if not ranked:
                entries.append(MapEntry(
                    song_name=s_name, song_author="", mapper="",
                    song_hash=s_hash, difficulty="", mode="",
                    stars=0.0, max_pp=0.0, player_pp=0.0,
                    cleared=False, nf_clear=False,
                    player_acc=0.0, player_rank=0,
                    leaderboard_id="", source="open",
                    duration_seconds=0,
                ))
        else:
            for d in diffs:
                char = d.get("characteristic") or "Standard"
                diff_name = d.get("name") or "ExpertPlus"
                key = (s_hash, char, diff_name)
                if key in idx:
                    entries.append(idx[key])
                else:
                    # ランク情報なし → 最低限の情報で登録
                    entries.append(MapEntry(
                        song_name=s_name, song_author="", mapper="",
                        song_hash=s_hash, difficulty=diff_name, mode=char,
                        stars=0.0, max_pp=0.0, player_pp=0.0,
                        cleared=False, nf_clear=False,
                        player_acc=0.0, player_rank=0,
                        leaderboard_id="", source="open",
                        duration_seconds=0,
                    ))

    return _enrich_entries_with_beatsaver_cache(entries)


def load_beatsaver_maps(
    steam_id: Optional[str] = None,
    query: str = "",
    days: int = 7,
    min_rating: float = 0.0,
    min_votes: int = 0,
    max_maps: Optional[int] = None,
    from_dt: Optional[datetime] = None,
    to_dt: Optional[datetime] = None,
    unranked_only: bool = True,
    exclude_ai: bool = True,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    session: Optional[requests.Session] = None,
) -> List[MapEntry]:
    """BeatSaver の検索 API から譜面を取得し、未プレイ判定付きで返す。"""
    now = datetime.now(timezone.utc)
    if to_dt is None:
        to_dt = now
    elif to_dt.tzinfo is None:
        to_dt = to_dt.replace(tzinfo=timezone.utc)
    else:
        to_dt = to_dt.astimezone(timezone.utc)
    if from_dt is None:
        from_dt = to_dt - timedelta(days=max(1, days))
    elif from_dt.tzinfo is None:
        from_dt = from_dt.replace(tzinfo=timezone.utc)
    else:
        from_dt = from_dt.astimezone(timezone.utc)
    if from_dt > to_dt:
        from_dt, to_dt = to_dt, from_dt

    ss_scores_raw: Dict[str, dict] = {}
    bl_scores_raw: Dict[str, dict] = {}
    if steam_id:
        ss_path = _CACHE_DIR / f"scoresaber_player_scores_{steam_id}.json"
        if ss_path.exists():
            try:
                ss_data = json.loads(ss_path.read_text(encoding="utf-8"))
                ss_scores_raw = ss_data.get("scores", {})
            except Exception:
                pass
        bl_path = _CACHE_DIR / f"beatleader_player_scores_{steam_id}.json"
        if bl_path.exists():
            try:
                bl_data = json.loads(bl_path.read_text(encoding="utf-8"))
                bl_scores_raw = bl_data.get("scores", {})
            except Exception:
                pass

    ss_score_idx = _build_ss_score_hash_index(ss_scores_raw)
    bl_score_idx = _build_bl_score_hash_index(bl_scores_raw)
    bl_replay_idx = _build_bl_replay_hash_index(bl_scores_raw)
    bl_leaderboard_idx = _build_bl_leaderboard_hash_index(bl_scores_raw)
    bl_ranked_idx = _build_bl_hash_index(load_bl_maps())

    session = session or requests.Session()
    entries: List[MapEntry] = []
    pages = 1
    q = query.strip()
    bl_api_hash_cache: Dict[str, Dict[Tuple[str, str], str]] = {}

    for page in range(0, 20):
        if max_maps is not None and len(entries) >= max_maps:
            break
        if on_progress:
            on_progress(page, max(pages, 1), f"Searching BeatSaver... {page + 1}/{max(pages, 1)}")
        resp = session.get(
            f"https://api.beatsaver.com/search/text/{page}",
            params={
                "q": q,
                "pageSize": 100 if max_maps is None else min(100, max_maps),
                "from": from_dt.isoformat().replace("+00:00", "Z"),
                "to": to_dt.isoformat().replace("+00:00", "Z"),
                "minRating": min_rating,
                "minVotes": min_votes,
                "order": "Latest",
                "ascending": "false",
            },
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        docs = payload.get("docs") or []
        info = payload.get("info") or {}
        try:
            pages = max(1, int(info.get("pages") or 1))
        except (TypeError, ValueError):
            pages = 1
        if not docs:
            break

        for doc in docs:
            if max_maps is not None and len(entries) >= max_maps:
                break
            if unranked_only and any(doc.get(flag) for flag in ("ranked", "qualified", "blRanked", "blQualified")):
                continue
            tags = [str(tag).lower() for tag in (doc.get("tags") or [])]
            if exclude_ai and (
                doc.get("automapper")
                or str(doc.get("declaredAi") or "None") != "None"
                or "ai" in tags
            ):
                continue

            metadata = doc.get("metadata") or {}
            stats = doc.get("stats") or {}
            versions = doc.get("versions") or []
            version = next((item for item in versions if item.get("hash") or item.get("key")), versions[0] if versions else {})
            song_hash = (version.get("hash") or "").upper()
            if not song_hash:
                continue

            rating_value = float(stats.get("score") or 0.0)
            rating_percent = rating_value * 100.0 if rating_value <= 1.0 else rating_value
            upvotes = int(stats.get("upvotes") or 0)
            downvotes = int(stats.get("downvotes") or 0)
            votes = upvotes + downvotes
            uploaded_ts = _parse_iso_datetime_to_ts(doc.get("lastPublishedAt") or doc.get("uploaded") or doc.get("createdAt"))
            description = str(doc.get("description") or "").replace("\r\n", "\n").strip()
            cover_url = version.get("coverURL") or ""
            preview_url = version.get("previewURL") or ""
            download_url = version.get("downloadURL") or ""
            beatsaver_key = str(doc.get("id") or doc.get("key") or version.get("key") or "")
            page_url = f"https://beatsaver.com/maps/{beatsaver_key}" if beatsaver_key else ""
            duration_seconds = _normalize_duration_seconds(metadata.get("duration"))
            difficulties = version.get("diffs") or []
            if not difficulties:
                difficulties = [{"difficulty": "ExpertPlus", "characteristic": "Standard", "nps": 0.0, "stars": 0.0}]

            for diff in difficulties:
                characteristic = diff.get("characteristic") or "Standard"
                if characteristic in ("Lightshow", "Legacy"):
                    continue
                difficulty = diff.get("difficulty") or diff.get("label") or "ExpertPlus"
                nps_value = float(diff.get("nps") or 0.0)
                star_value = float(diff.get("stars") or diff.get("blStars") or 0.0)
                key = (song_hash, characteristic, difficulty)
                ss_match = ss_score_idx.get(key)
                bl_match = bl_score_idx.get(key)
                bl_entry = bl_ranked_idx.get(key)
                if song_hash not in bl_api_hash_cache:
                    bl_api_hash_cache[song_hash] = _fetch_bl_leaderboards_by_hash(session, song_hash)
                bl_leaderboard_id = (
                    bl_leaderboard_idx.get(key)
                    or (bl_entry.leaderboard_id if bl_entry else "")
                    or bl_api_hash_cache[song_hash].get((characteristic, difficulty), "")
                )
                bl_page_url = (
                    f"https://beatleader.com/leaderboard/global/{bl_leaderboard_id}"
                    if bl_leaderboard_id
                    else ""
                )
                bl_replay_url = bl_replay_idx.get(key, "")

                cleared = False
                nf_clear = False
                score_source = ""
                played_at_ts = 0
                best_match: Optional[Tuple[float, bool, bool, float, int, str, int]] = None
                if ss_match and bl_match:
                    best_match = ss_match if (2 if ss_match[1] else 1 if ss_match[2] else 0, ss_match[3]) >= (2 if bl_match[1] else 1 if bl_match[2] else 0, bl_match[3]) else bl_match
                    score_source = "SS" if best_match is ss_match else "BL"
                elif ss_match:
                    best_match = ss_match
                    score_source = "SS"
                elif bl_match:
                    best_match = bl_match
                    score_source = "BL"
                if best_match is not None:
                    _, cleared, nf_clear, _, _, _, played_at_ts = best_match

                entries.append(MapEntry(
                    song_name=metadata.get("songName") or doc.get("name") or "",
                    song_author=metadata.get("songAuthorName") or "",
                    mapper=metadata.get("levelAuthorName") or (doc.get("uploader") or {}).get("name") or "",
                    song_hash=song_hash,
                    difficulty=difficulty,
                    mode=characteristic,
                    stars=star_value,
                    max_pp=0.0,
                    player_pp=rating_percent,
                    cleared=cleared,
                    nf_clear=nf_clear,
                    player_acc=nps_value,
                    player_rank=votes,
                    leaderboard_id=bl_leaderboard_id,
                    source="beatsaver",
                    score_source=score_source,
                    duration_seconds=duration_seconds,
                    played_at_ts=played_at_ts,
                    source_date_ts=uploaded_ts,
                    beatsaver_key=beatsaver_key,
                    beatsaver_cover_url=cover_url,
                    beatsaver_preview_url=preview_url,
                    beatsaver_page_url=page_url,
                    beatsaver_download_url=download_url or (f"https://beatsaver.com/api/download/key/{beatsaver_key}" if beatsaver_key else ""),
                    beatsaver_rating=rating_value,
                    beatsaver_votes=votes,
                    beatsaver_upvotes=upvotes,
                    beatsaver_downvotes=downvotes,
                    beatsaver_uploaded_ts=uploaded_ts,
                    beatsaver_description=description,
                    beatleader_page_url=bl_page_url,
                    beatleader_replay_url=bl_replay_url,
                    beatleader_global1_replay_url="",
                    beatleader_local1_replay_url="",
                    beatleader_attempts=bl_entry.beatleader_attempts if bl_entry else 0,
                    beatleader_plays=bl_entry.beatleader_plays if bl_entry else 0,
                ))
        if page + 1 >= pages:
            break

    if on_progress:
        on_progress(1, 1, "Done")
    return entries if max_maps is None else entries[:max_maps]


# ──────────────────────────────────────────────────────────────────────────────
# 数値ソート対応アイテム
# ──────────────────────────────────────────────────────────────────────────────

class _NumItem(QTableWidgetItem):
    def __init__(self, text: str, sort_val: float = 0.0) -> None:
        super().__init__(text)
        self._v = sort_val

    def __lt__(self, other: "QTableWidgetItem") -> bool:  # type: ignore[override]
        other_v = other._v if isinstance(other, _NumItem) else 0.0  # type: ignore[attr-defined]
        return self._v < other_v


# 難易度アイコン: Beat Saber 公式カラー + 短縮テキスト
_DIFF_INFO: Dict[str, tuple] = {
    "Easy":       ("Es",  QColor("#1acc1a")),
    "Normal":     ("N",  QColor("#59b0f4")),
    "Hard":       ("H",  QColor("#f4a015")),
    "Expert":     ("Ex",   QColor("#ff4e4e")),
    "ExpertPlus": ("E+",  QColor("#bf2aff")),
}

# モードアイコン
_MODE_INFO: Dict[str, str] = {
    "Standard":  "2S",
    "OneSaber":  "1S",
    "NoArrows":  "NA",
    "90Degree":  "90°",
    "360Degree": "360°",
    "Lightshow": "LS",
    "Lawless":   "Law",
}


def _diff_item(difficulty: str) -> QTableWidgetItem:
    short, color = _DIFF_INFO.get(difficulty, (difficulty[:4], QColor("#aaaaaa")))
    _diff_val = {"Easy": 1, "Normal": 3, "Hard": 5, "Expert": 7, "ExpertPlus": 9}
    item = _NumItem(short, float(_diff_val.get(difficulty, 0)))
    item.setBackground(color)
    item.setForeground(QColor("#DDDDDD") if is_dark() else QColor("#000000"))
    item.setToolTip(difficulty)
    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    # 太字にする（ただし、環境によってはフォントサイズが変わってしまうため、スタイルシートで擬似的に太字にする）
    font = item.font()
    font.setBold(True)
    item.setFont(font)
    return item


def _mode_item(mode: str) -> QTableWidgetItem:
    short = _MODE_INFO.get(mode, mode[:4])
    item = QTableWidgetItem(short)
    item.setToolTip(mode)
    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    return item


def _format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "-"
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def _duration_item(seconds: int) -> QTableWidgetItem:
    item = _NumItem(_format_duration(seconds), float(seconds))
    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return item


def _preview_text_to_html(text: str) -> str:
    if not text:
        return ""

    escaped = html.escape(text)
    escaped = re.sub(
        r'\[([^\]]+)\]\((https?://[^\s)]+)\)',
        lambda match: f'<a href="{match.group(2)}">{match.group(1)}</a>',
        escaped,
    )
    escaped = re.sub(
        r'(?<!["=])(https?://[^\s<]+)',
        lambda match: f'<a href="{match.group(1)}">{match.group(1)}</a>',
        escaped,
    )
    return escaped.replace("\n", "<br>")


def _format_played_at(ts: int) -> str:
    if ts <= 0:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _played_at_item(ts: int) -> QTableWidgetItem:
    item = _NumItem(_format_played_at(ts), float(ts))
    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return item


def _format_source_date(ts: int, *, include_time: bool = False) -> str:
    if ts <= 0:
        return "-"
    fmt = "%Y-%m-%d %H:%M" if include_time else "%Y-%m-%d"
    return datetime.fromtimestamp(ts).strftime(fmt)


def _source_date_item(ts: int, *, include_time: bool = False) -> QTableWidgetItem:
    sort_value = float(ts) if ts > 0 else -1.0
    item = _NumItem(_format_source_date(ts, include_time=include_time), sort_value)
    item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return item


def _sort_dir_from_mode(sort_mode: str) -> str:
    """sort_mode 文字列から 'asc'/'desc' を返す。"""
    return "desc" if sort_mode in (
        "pp_high", "ap_high", "acc_high", "rank_high", "star_desc", "fc_desc", "duration_desc",
        "bl_plays_desc", "bl_attempts_desc",
        "status_desc", "song_desc", "date_desc", "playtime_desc", "diff_desc", "mode_desc", "cat_desc",
        "mapper_desc", "author_desc",
    ) else "asc"


def _make_playlist_cover(
    cover_type: str,  # "star" | "true" | "standard" | "tech" | "default"
    label: str = "",  # "star" 時は星数文字列
    sort_dir: str = "asc",  # "asc" | "desc"
    source: str = "",  # "ss" | "bl" | "rl" | ""
) -> str:
    """プレイリストカバー画像を生成し data:image/png;base64,... を返す。

    cover_type:
        "star"     → SS: scoresaber_logo.svg / BL: beatleader_logo.webp + ★N (黄)
        "true"     → accsaberreloaded_logo + Tr (緑)
        "standard" → accsaberreloaded_logo + St (青)
        "tech"     → accsaberreloaded_logo + Tc (赤)
        "default"  → SS/BL ロゴ or app_icon のみ
    sort_dir: "asc" → ⇧, "desc" → ⇩
    """
    SIZE = 256

    # ベース画像選択
    if cover_type in ("true", "standard", "tech"):
        base_path = RESOURCES_DIR / "accsaberreloaded_logo.png"
    elif source == "ss":
        base_path = RESOURCES_DIR / "scoresaber_logo.svg"
    elif source == "bl":
        base_path = RESOURCES_DIR / "beatleader_logo.webp"
    elif source == "acc":
        base_path = RESOURCES_DIR / "asssaber_logo.webp"
    elif source == "rl":
        base_path = RESOURCES_DIR / "accsaberreloaded_logo.png"
    else:
        base_path = RESOURCES_DIR / "app_icon.png"

    if str(base_path).endswith(".svg") and base_path.exists():
        renderer = QSvgRenderer(str(base_path))
        base_img = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32)
        base_img.fill(QColor(30, 30, 30))
        _svg_painter = QPainter(base_img)
        renderer.render(_svg_painter)
        _svg_painter.end()
    elif base_path.exists():
        base_img = QImage(str(base_path))
        base_img = base_img.scaled(
            SIZE, SIZE,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        cx = (base_img.width() - SIZE) // 2
        cy = (base_img.height() - SIZE) // 2
        base_img = base_img.copy(cx, cy, SIZE, SIZE)
    else:
        base_img = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32)
        base_img.fill(QColor(30, 30, 30))

    canvas = base_img.convertToFormat(QImage.Format.Format_ARGB32)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    # カテゴリ・ラベル設定
    if cover_type == "true":
        main_text, text_color = "True", QColor(0, 220, 80)
    elif cover_type == "standard":
        main_text, text_color = "Std", QColor(80, 180, 255)
    elif cover_type == "tech":
        main_text, text_color = "Tech", QColor(255, 80, 80)
    elif cover_type == "star":
        main_text, text_color = f"\u2605{label}", QColor(255, 220, 0)
    else:
        main_text, text_color = "", QColor(255, 255, 255)

    arrow = "\u21e7" if sort_dir == "asc" else "\u21e9"

    if main_text:
        from PySide6.QtCore import QRect as _QRect
        from PySide6.QtGui import QFontMetrics as _QFM
        bar_h = SIZE // 2  # 128px — テキストが確実に入る高さ
        painter.fillRect(0, SIZE - bar_h, SIZE, bar_h, QColor(0, 0, 0, 190))
        # フォントサイズをテキストが収まるよう自動調整（ピクセル単位）
        text_area = _QRect(8, SIZE - bar_h + 8, SIZE - 16, bar_h - 16)
        px = 72  # 開始ピクセルサイズ
        font_main = QFont("Segoe UI", 1, QFont.Weight.Black)
        font_main.setPixelSize(px)
        while px > 8:
            fm = _QFM(font_main)
            br = fm.boundingRect(main_text)
            if br.width() <= text_area.width() and br.height() <= text_area.height():
                break
            px -= 2
            font_main.setPixelSize(px)
        painter.setFont(font_main)
        # 影
        painter.setPen(QColor(0, 0, 0, 230))
        painter.drawText(_QRect(text_area.x() + 2, text_area.y() + 2,
                                text_area.width(), text_area.height()),
                         Qt.AlignmentFlag.AlignCenter, main_text)
        # 本体
        painter.setPen(text_color)
        painter.drawText(text_area, Qt.AlignmentFlag.AlignCenter, main_text)

    # ソート矢印（右下隅）
    from PySide6.QtCore import QRect as _QRect2
    font_arrow = QFont("Segoe UI Symbol", 1, QFont.Weight.Bold)
    font_arrow.setPixelSize(28)
    painter.setFont(font_arrow)
    painter.setPen(QColor(0, 0, 0, 200))
    painter.drawText(_QRect2(SIZE - 50, SIZE - 40, 48, 38),
                     Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, arrow)
    painter.setPen(QColor(255, 255, 255, 230))
    painter.drawText(_QRect2(SIZE - 52, SIZE - 42, 48, 38),
                     Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, arrow)

    painter.end()

    fd, tmp_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        canvas.save(tmp_path)
        with open(tmp_path, "rb") as f:
            png_data = f.read()
    finally:
        os.unlink(tmp_path)
    return "data:image/png;base64," + base64.b64encode(png_data).decode("ascii")


def _make_bplist(title: str, entries: List[MapEntry], image: str = "") -> dict:
    songs = []
    for e in entries:
        char = e.mode or "Standard"
        diff = e.difficulty or "ExpertPlus"
        songs.append({
            "hash": e.song_hash,
            "songName": e.song_name,
            "difficulties": [{"characteristic": char, "name": diff}],
        })
    return {
        "playlistTitle": title,
        "playlistAuthor": "MyBeatSaberStats",
        "image": image,
        "songs": songs,
    }


def _save_bplist(parent: QWidget, title: str, entries: List[MapEntry], init_dir: str = "", image: str = "") -> Optional[str]:
    """bplist ファイルを保存ダイアログで保存する。保存したファイルのパスを返す（キャンセル時は None）。"""
    if not entries:
        QMessageBox.information(parent, "Export", "No maps to export.")
        return None

    safe_title = title.replace(" ", "_").replace("/", "-")
    default_name = str(Path(init_dir) / f"{safe_title}.bplist") if init_dir else f"{safe_title}.bplist"
    path, _ = QFileDialog.getSaveFileName(
        parent, "Save bplist file", default_name,
        "BeatSaber Playlist (*.bplist);;JSON (*.json)"
    )
    if not path:
        return None

    bplist = _make_bplist(title, entries, image)
    try:
        Path(path).write_text(json.dumps(bplist, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
    except Exception as e:
        QMessageBox.critical(parent, "Save Error", str(e))
        return None


def load_accsaber_maps(
    steam_id: Optional[str] = None,
    category: str = "all",
    on_progress=None,
) -> List[MapEntry]:
    """AccSaber のカテゴリプレイリストを API から取得し AccSaber/SS クリア情報を付与する。

    category: "all" | "true" | "standard" | "tech"
    on_progress(done: int, total: int, label: str) — 進捗コールバック（省略可）

    クリア判定の優先順位:
      1. AccSaber player scores API — SC 等の無効モディファイアを除外した公式クリア
      2. SS/BL player scores — AccSaber スコア取得不可時のフォールバック
    """
    _PLAYLIST_URLS: Dict[str, str] = {
        "true":     "https://accsaber.com/api/playlists/true",
        "standard": "https://accsaber.com/api/playlists/standard",
        "tech":     "https://accsaber.com/api/playlists/tech",
    }
    _ACC_DIFF_NORM = {
        "easy": "Easy", "normal": "Normal", "hard": "Hard",
        "expert": "Expert", "expertplus": "ExpertPlus", "expert+": "ExpertPlus",
    }
    cats = ["true", "standard", "tech"] if category == "all" else [category]

    session = requests.Session()

    # キャッシュからマップデータを読み込む（Snapshot 時に保存済みの場合）
    from .accsaber import load_accsaber_maps_cache as _load_acc_cache
    _acc_cache = _load_acc_cache()

    # AccSaber ranked-maps から (hash.upper(), diff) → complexity インデックスを構築
    complexity_index: Dict[Tuple[str, str], float] = {}
    ranked_date_index: Dict[Tuple[str, str], int] = {}
    _ranked_maps_src: List[dict] = (_acc_cache.get("ranked_maps") if _acc_cache else None) or []
    if not _ranked_maps_src:
        try:
            rm = session.get("https://accsaber.com/api/ranked-maps", timeout=30)
            if rm.status_code == 200:
                _ranked_maps_src = rm.json()
        except Exception:
            pass
    for m in _ranked_maps_src:
        h = (m.get("songHash") or "").upper()
        dn = _ACC_DIFF_NORM.get((m.get("difficulty") or "").lower(), m.get("difficulty") or "")
        c = m.get("complexity") or 0.0
        if h and dn:
            complexity_index[(h, dn)] = float(c)
            ranked_date_index[(h, dn)] = _parse_iso_datetime_to_ts(m.get("dateRanked"))

    # AccSaber プレイヤースコアを取得し (hash, diff) → cleared/nf セットを構築
    # AccSaber は SC (SmallCubes) 等の特定モディファイアをスコアとしてカウントしないため
    # SS player scores とは独立して AccSaber 公式クリア判定を行う。
    acc_score_cleared: set = set()   # (hash.upper(), diff) — AccSaber 正規クリア
    acc_score_nf: set = set()         # (hash.upper(), diff) — NF クリア
    acc_score_ap: Dict[Tuple[str, str], Tuple[float, int]] = {}  # (hash, diff) → (ap, rank)
    acc_score_ts: Dict[Tuple[str, str], int] = {}  # (hash, diff) → played_at_ts
    acc_player_scores_available = False
    if steam_id:
        from .accsaber import load_player_scores_from_cache as _load_acc_score_cache
        _acc_score_list = _load_acc_score_cache(steam_id)
        if _acc_score_list is None:
            try:
                ar = session.get(
                    f"https://accsaber.com/api/players/{steam_id}/scores?pageSize=2000",
                    timeout=15,
                )
                if ar.status_code == 200:
                    _acc_score_list = ar.json()
            except Exception:
                _acc_score_list = None
        if _acc_score_list is not None:
            for asc in _acc_score_list:
                h = (asc.get("songHash") or "").upper()
                dn = _ACC_DIFF_NORM.get((asc.get("difficulty") or "").lower(), asc.get("difficulty", ""))
                mods = (asc.get("mods") or "").upper()
                if "NF" in mods:
                    acc_score_nf.add((h, dn))
                else:
                    acc_score_cleared.add((h, dn))
                ap = float(asc.get("ap") or 0)
                rank = int(asc.get("rank") or 0)
                time_set = _parse_iso_datetime_to_ts(asc.get("timeSet"))
                if ap > 0 and h and dn:
                    key_ap: Tuple[str, str] = (h, dn)
                    if ap > acc_score_ap.get(key_ap, (0.0, 0))[0]:
                        acc_score_ap[key_ap] = (ap, rank)
                if h and dn and time_set > 0:
                    key_ts: Tuple[str, str] = (h, dn)
                    if time_set > acc_score_ts.get(key_ts, 0):
                        acc_score_ts[key_ts] = time_set
            acc_player_scores_available = True

    # SS player scores — pp/acc/rank 表示用、および AccSaber 取得不可時のフォールバック
    ss_scores_raw: Dict[str, dict] = {}
    if steam_id:
        sp = _CACHE_DIR / f"scoresaber_player_scores_{steam_id}.json"
        if sp.exists():
            try:
                sd = json.loads(sp.read_text(encoding="utf-8"))
                ss_scores_raw = sd.get("scores", {})
            except Exception:
                pass
    ss_score_idx = _build_ss_score_hash_index(ss_scores_raw)

    # BL ランクマップキャッシュを読み込んでインデックス化（フォールバック用）
    bl_ranked = load_bl_maps()
    bl_index = _build_bl_hash_index(bl_ranked)

    bl_scores: Dict[str, dict] = {}
    if steam_id:
        bp = _CACHE_DIR / f"beatleader_player_scores_{steam_id}.json"
        if bp.exists():
            try:
                bd = json.loads(bp.read_text(encoding="utf-8"))
                bl_scores = bd.get("scores", {})
            except Exception:
                pass

    from dataclasses import replace as _dc_replace

    # key → (entry, [cat, ...]) で複数カテゴリを集積する
    seen_entries: Dict[Tuple[str, str, str], MapEntry] = {}
    seen_cats: Dict[Tuple[str, str, str], List[str]] = {}

    for i, cat in enumerate(cats):
        if on_progress:
            on_progress(i, len(cats), f"Loading AccSaber {cat}...")
        _cached_playlists: Dict[str, dict] = (_acc_cache.get("playlists") if _acc_cache else None) or {}
        if cat in _cached_playlists:
            bplist_data = _cached_playlists[cat]
        else:
            url = _PLAYLIST_URLS[cat]
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            bplist_data = resp.json()
        songs = bplist_data.get("songs") or []
        for song in songs:
            s_hash = (song.get("hash") or "").upper()
            s_name = song.get("songName") or ""
            diffs = song.get("difficulties") or []
            for d in diffs:
                char = d.get("characteristic") or "Standard"
                diff_name = _ACC_DIFF_NORM.get((d.get("name") or "").lower(), d.get("name") or "ExpertPlus")
                key = (s_hash, char, diff_name)
                if key not in seen_entries:
                    # SS player scores から pp/acc/rank 取得 (モディファイア有スコアも含む)
                    ss_info = ss_score_idx.get(key)
                    ss_pp = 0.0
                    ss_cleared = ss_nf = False
                    ss_acc = 0.0
                    ss_rank = 0
                    ss_mods = ""
                    ss_played_at_ts = 0
                    if ss_info:
                        ss_pp, ss_cleared, ss_nf, ss_acc, ss_rank, ss_mods, ss_played_at_ts = ss_info

                    # BL スコア取得 (フォールバック用)
                    bl_entry = bl_index.get(key)
                    bl_pp = 0.0
                    bl_cleared = bl_nf = False
                    bl_acc_val = 0.0
                    bl_rank = 0
                    bl_stars = 0.0
                    bl_mods = ""
                    bl_played_at_ts = 0
                    if bl_entry:
                        bl_pp, bl_cleared, bl_nf, bl_acc_val, bl_rank, bl_mods = _bl_player_score_info(
                            bl_scores, bl_entry.leaderboard_id
                        )
                        bl_played_at_ts = _bl_player_score_timeset(bl_scores, bl_entry.leaderboard_id)
                        bl_stars = bl_entry.stars

                    # クリア判定: AccSaber 公式スコアを優先
                    key_hd = (s_hash, diff_name)  # AccSaber API にはモード情報なし
                    if acc_player_scores_available:
                        if key_hd in acc_score_cleared:
                            final_cleared, final_nf, final_mods = True, False, ""
                        elif key_hd in acc_score_nf:
                            final_cleared, final_nf, final_mods = False, True, "NF"
                        else:
                            # AccSaber にスコアなし — SS/BL でプレイ済みなら「要再プレイ」扱い
                            if ss_cleared or ss_nf:
                                final_cleared, final_nf, final_mods = False, True, ss_mods
                            elif bl_cleared or bl_nf:
                                final_cleared, final_nf, final_mods = False, True, bl_mods
                            else:
                                final_cleared, final_nf, final_mods = False, False, ""
                    else:
                        # AccSaber スコア取得不可 → SS/BL フォールバック
                        if ss_cleared or ss_nf:
                            final_cleared, final_nf, final_mods = ss_cleared, ss_nf, ss_mods
                        elif bl_cleared or bl_nf:
                            final_cleared, final_nf, final_mods = bl_cleared, bl_nf, bl_mods
                        else:
                            final_cleared, final_nf, final_mods = False, False, ""

                    final_pp = ss_pp or bl_pp
                    final_acc = ss_acc or bl_acc_val
                    final_rank = ss_rank or bl_rank

                    acc_ap_entry = acc_score_ap.get(key_hd, (0.0, 0))
                    final_acc_ap = acc_ap_entry[0]
                    final_acc_rank = acc_ap_entry[1]
                    acc_played_at_ts = acc_score_ts.get(key_hd, 0)

                    if acc_player_scores_available and (key_hd in acc_score_cleared or key_hd in acc_score_nf):
                        final_score_src = "AS"
                        final_played_at_ts = acc_played_at_ts
                    elif ss_pp > 0 or ss_cleared or ss_nf:
                        final_score_src = "SS"
                        final_played_at_ts = ss_played_at_ts
                    elif bl_pp > 0 or bl_cleared or bl_nf:
                        final_score_src = "BL"
                        final_played_at_ts = bl_played_at_ts
                    else:
                        final_score_src = ""
                        final_played_at_ts = 0

                    seen_entries[key] = MapEntry(
                        song_name=s_name, song_author="", mapper="",
                        song_hash=s_hash, difficulty=diff_name, mode=char,
                        stars=bl_stars, max_pp=0.0, player_pp=final_pp,
                        cleared=final_cleared, nf_clear=final_nf,
                        player_acc=final_acc,
                        player_rank=final_acc_rank if final_acc_rank else final_rank,
                        leaderboard_id="", source="accsaber",
                        acc_category=cat,
                        acc_rl_ap=final_acc_ap,
                        acc_complexity=complexity_index.get((s_hash, diff_name), 0.0),
                        player_mods=final_mods,
                        score_source=final_score_src,
                        duration_seconds=bl_entry.duration_seconds if bl_entry else 0,
                        played_at_ts=final_played_at_ts,
                        source_date_ts=ranked_date_index.get((s_hash, diff_name), 0),
                    )
                    seen_cats[key] = [cat]
                else:
                    seen_cats[key].append(cat)

    # 複数カテゴリに属する場合は "/" で結合
    entries: List[MapEntry] = []
    for key, entry in seen_entries.items():
        cat_list = seen_cats.get(key, [])
        if len(cat_list) > 1:
            entry = _dc_replace(entry, acc_category="/".join(cat_list))
        entries.append(entry)

    if on_progress:
        on_progress(len(cats), len(cats), "Done")
    return entries


def _fetch_rl_ap_index(
    player_id: str,
    session: Optional[requests.Session] = None,
) -> Dict[str, Tuple[float, int, int]]:
    """AccSaber Reloaded プレイヤーの mapDifficultyId → (ap, rank, played_at_ts) インデックスを取得する。

    キャッシュが存在する場合はキャッシュから読み込む。
    """
    if not player_id:
        return {}

    def _build_index(scores: list) -> Dict[str, Tuple[float, int, int]]:
        result: Dict[str, Tuple[float, int, int]] = {}
        for score in scores:
            diff_id = score.get("mapDifficultyId")
            ap = float(score.get("ap") or 0)
            rank = int(score.get("rank") or 0)
            played_at_ts = _parse_iso_datetime_to_ts(score.get("timeSet"))
            if diff_id and ap > 0:
                prev_ap = result.get(diff_id, (0.0, 0, 0))[0]
                if prev_ap < ap:
                    result[diff_id] = (ap, rank, played_at_ts)
        return result

    from .accsaber_reloaded import load_player_scores_from_cache as _load_rl_score_cache
    _cached = _load_rl_score_cache(player_id)
    if _cached is not None:
        return _build_index(_cached)

    if session is None:
        session = requests.Session()
    from .accsaber_reloaded import BASE_URL as _RL_BASE, _PAGE_SIZE as _RL_PAGE
    all_scores: list = []
    page = 0
    while True:
        resp = session.get(
            f"{_RL_BASE}/users/{player_id}/scores",
            params={"page": page, "size": _RL_PAGE},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        all_scores.extend(data.get("content", []))
        if data.get("last", True):
            break
        page += 1
    return _build_index(all_scores)


def load_accsaber_reloaded_maps(
    steam_id: Optional[str] = None,
    category: str = "all",
    on_progress=None,
) -> List[MapEntry]:
    """AccSaber Reloaded の全マップを API から取得し BL ランク情報を付与する。

    category: "all" | "true" | "standard" | "tech"
    on_progress(done: int, total: int, label: str) — 進捗コールバック（省略可）
    """
    from .accsaber_reloaded import CATEGORY_IDS

    _RL_DIFF_TO_BS: Dict[str, str] = {
        "EASY":        "Easy",
        "NORMAL":      "Normal",
        "HARD":        "Hard",
        "EXPERT":      "Expert",
        "EXPERT_PLUS": "ExpertPlus",
    }
    _NON_OVERALL_IDS = {k: v for k, v in CATEGORY_IDS.items() if k != "overall"}
    _UUID_TO_CAT: Dict[str, str] = {v: k for k, v in _NON_OVERALL_IDS.items()}  # uuid → "true"/"standard"/"tech"
    target_cat_uuids: set
    if category == "all":
        target_cat_uuids = set(_NON_OVERALL_IDS.values())
    else:
        uuid = _NON_OVERALL_IDS.get(category, "")
        target_cat_uuids = {uuid} if uuid else set()

    # AccSaber Reloaded の全マップを取得（キャッシュ優先）
    session = requests.Session()

    def _rl_progress(page: int, total: int) -> None:
        if on_progress:
            on_progress(page, total, f"Fetching AccSaber Reloaded maps... {page}/{total}")

    from .accsaber_reloaded import fetch_all_maps_full, load_all_maps_from_cache as _load_rl_cache
    all_maps = _load_rl_cache()
    if all_maps is None:
        all_maps = fetch_all_maps_full(session=session, on_progress=_rl_progress)
    elif on_progress:
        on_progress(1, 1, "Loaded AccSaber Reloaded maps from cache")

    # RL プレイヤースコア (AP, rank) を mapDifficultyId でインデックス化
    # mapDifficultyId -> (ap, rank)
    rl_ap_index: Dict[str, Tuple[float, int, int]] = {}
    if steam_id:
        if on_progress:
            on_progress(0, 1, "Fetching RL player scores (AP)...")
        try:
            rl_ap_index = _fetch_rl_ap_index(steam_id, session=session)
        except Exception:
            pass  # AP 取得失敗時は 0 のまま
        if on_progress:
            on_progress(1, 1, "Done")

    # BL プレイヤースコアを読み込む
    bl_scores: Dict[str, dict] = {}
    if steam_id:
        bp = _CACHE_DIR / f"beatleader_player_scores_{steam_id}.json"
        if bp.exists():
            try:
                bd = json.loads(bp.read_text(encoding="utf-8"))
                bl_scores = bd.get("scores", {})
            except Exception:
                pass

    # SS プレイヤースコアを読み込む（BL スコアが無い場合のフォールバック）
    ss_scores: Dict[str, dict] = {}
    if steam_id:
        sp = _CACHE_DIR / f"scoresaber_player_scores_{steam_id}.json"
        if sp.exists():
            try:
                sd = json.loads(sp.read_text(encoding="utf-8"))
                ss_scores = sd.get("scores", {})
            except Exception:
                pass

    # BL ランクマップキャッシュを hash インデックス化（スター取得用）
    bl_ranked = load_bl_maps()
    bl_index = _build_bl_hash_index(bl_ranked)

    seen: set = set()
    entries: List[MapEntry] = []

    for song in all_maps:
        s_hash = (song.get("songHash") or "").upper()
        s_name = song.get("songName") or ""
        s_author = song.get("songAuthorName") or ""

        for diff in song.get("difficulties") or []:
            if not diff.get("active", False):
                continue
            cat_uuid = diff.get("categoryId", "")
            if cat_uuid not in target_cat_uuids:
                continue
            acc_cat = _UUID_TO_CAT.get(cat_uuid, "")

            char = diff.get("characteristic") or "Standard"
            diff_bs = _RL_DIFF_TO_BS.get(diff.get("difficulty", ""), "ExpertPlus")
            key = (s_hash, char, diff_bs)
            if key in seen:
                continue
            seen.add(key)

            # BL leaderboard ID でプレイヤースコアを取得
            bl_lb_id = str(diff.get("blLeaderboardId") or "")
            complexity = float(diff.get("complexity") or 0.0)
            bl_pp, bl_cleared, bl_nf, bl_acc, bl_rank_val, bl_mods = _bl_player_score_info(bl_scores, bl_lb_id)
            bl_played_at_ts = _bl_player_score_timeset(bl_scores, bl_lb_id)
            bl_has_any_score = bl_pp > 0 or bl_cleared or bl_nf

            # SS スコアも常に取得する
            # AccSaber Reloaded は BL/SS の精度を比較して高い方を採用するだけで
            # モディファイアによる無効化は行わない
            ss_lb_id = str(diff.get("ssLeaderboardId") or "")
            ss_pp_v = 0.0
            ss_cleared = ss_nf = False
            ss_acc_v = 0.0
            ss_rank_v = 0
            ss_mods_v = ""
            ss_played_at_ts = 0
            if ss_lb_id and ss_scores:
                ss_max_score = int(
                    (ss_scores.get(str(ss_lb_id), {}).get("leaderboard") or {}).get("maxScore") or 0
                )
                ss_pp_v, ss_cleared, ss_nf, ss_acc_v, ss_rank_v, ss_mods_v = _ss_player_score_info(
                    ss_scores, ss_lb_id, ss_max_score
                )
                ss_played_at_ts = _ss_player_score_timeset(ss_scores, ss_lb_id)

            # (cleared=2 > nf=1 > unplayed=0, acc) のタプル比較で高い方を採用
            use_ss = (
                (2 if ss_cleared else 1 if ss_nf else 0, ss_acc_v) >
                (2 if bl_cleared else 1 if bl_nf else 0, bl_acc)
            )
            if use_ss:
                cleared, nf_clear, acc, rank, score_mods, player_pp = (
                    ss_cleared, ss_nf, ss_acc_v, ss_rank_v, ss_mods_v, ss_pp_v
                )
                score_src = "SS"
                played_at_ts = ss_played_at_ts
            else:
                cleared, nf_clear, acc, rank, score_mods, player_pp = (
                    bl_cleared, bl_nf, bl_acc, bl_rank_val, bl_mods, bl_pp
                )
                score_src = "BL" if bl_has_any_score else ""
                played_at_ts = bl_played_at_ts if bl_has_any_score else 0

            # RL AP・rank（mapDifficultyId による精定値）
            rl_diff_id = diff.get("id") or ""
            rl_score = rl_ap_index.get(rl_diff_id, (0.0, 0, 0))
            ap = rl_score[0]
            rl_rank = rl_score[1]
            rl_played_at_ts = rl_score[2]
            ranked_date_ts = _parse_iso_datetime_to_ts(diff.get("rankedAt") or diff.get("createdAt") or song.get("createdAt"))
            pending = _is_rl_pending_difficulty(diff)

            # BL ランクマップからスター取得 (hash+char+diff 一致)
            bl_entry = bl_index.get(key)
            stars = bl_entry.stars if bl_entry else 0.0

            entries.append(MapEntry(
                song_name=s_name,
                song_author=s_author,
                mapper="",
                song_hash=s_hash,
                difficulty=diff_bs,
                mode=char,
                stars=stars,
                max_pp=0.0,
                player_pp=player_pp,
                cleared=cleared,
                nf_clear=nf_clear,
                player_acc=acc,
                player_rank=rl_rank if rl_rank else rank,
                leaderboard_id=bl_lb_id,
                source="accsaber_reloaded",
                acc_category=acc_cat,
                acc_rl_ap=ap,
                acc_complexity=complexity,
                player_mods=score_mods,
                score_source=score_src,
                duration_seconds=bl_entry.duration_seconds if bl_entry else 0,
                played_at_ts=rl_played_at_ts if rl_played_at_ts else played_at_ts,
                source_date_ts=ranked_date_ts,
                pending=pending,
                beatsaver_key=str(song.get("beatsaverCode") or ""),
            ))

    if on_progress:
        on_progress(1, 1, "Done")
    return _enrich_entries_with_beatsaver_cache(entries)


# ──────────────────────────────────────────────────────────────────────────────
# スレッド通信用シグナル
# ──────────────────────────────────────────────────────────────────────────────

class _LoadSignals(QObject):
    finished = Signal(list)        # List[MapEntry]
    error = Signal(str)            # エラーメッセージ
    progress = Signal(int, int, str)  # done, total, label


class _PreviewSignals(QObject):
    loaded = Signal(int, str, bytes)
    error = Signal(int, str)


class _ThumbnailSignals(QObject):
    loaded = Signal(str, bytes)
    error = Signal(str, str)


class _BeatSaverMetaSignals(QObject):
    finished = Signal(list)
    error = Signal(str)


# ──────────────────────────────────────────────────────────────────────────────
# バッチエクスポート
# ──────────────────────────────────────────────────────────────────────────────

_BATCH_SRC_PREFIX: Dict[str, str] = {
    "ss": "SS", "bl": "BL", "acc": "AS", "rl": "RL", "bs": "BS", "pl": "PL",
}

@dataclass
class _BatchPreset:
    """一括出力プリセットの定義。"""
    label: str
    source: str         # "ss" | "bl" | "rl"
    rl_cat: str         # "true" | "standard" | "tech" | ""
    uncleared: bool     # True = 未クリアのみ
    sort_mode: str      # "star_asc" | "pp_high" | "ap_high"
    filename_base: str  # 出力ファイル名プレフィックス
    split_by_star: bool # True = ★ごとに分割出力


@dataclass
class _BatchConfig:
    """バッチエクスポートの1設定（フィルタ条件として保存）。
    マップデータは保持せず、Export 時に毎回新鮮なデータをロードして適用する。
    """
    label: str
    filename_base: str
    source: str            # "ss" | "bl" | "rl" | "acc" | "bs"
    # Status filter
    show_cleared: bool = True
    show_nf: bool = True
    show_unplayed: bool = True
    show_queued: bool = False
    # Category filter (RL only)
    cat_true: bool = True
    cat_standard: bool = True
    cat_tech: bool = True
    # Star range
    star_min: float = 0.0
    star_max: float = 20.0
    # Export style
    split_mode: str = "star"   # "single" | "star" | "category"
    # Sort
    sort_mode: str = "star_asc"  # "star_asc"|"star_desc"|"pp_high"|"pp_low"|"ap_high"|"ap_low"|"acc_high"|"acc_low"|"rank_low"|"rank_high"
    # Song filter
    song_filter: str = ""
    # BeatSaver source filters
    bs_query: str = ""
    bs_from_date: str = ""
    bs_to_date: str = ""
    bs_days: int = 7
    bs_max_maps: int = 1000
    bs_min_rating: int = 50
    bs_min_votes: int = 0
    bs_unranked_only: bool = True
    bs_exclude_ai: bool = True
    # Enabled
    enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "_BatchConfig":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    def display_text(self) -> str:
        src = self.source.upper()
        if self.source == "rl":
            cats = [n for flag, n in [(self.cat_true, "Tr"), (self.cat_standard, "Std"), (self.cat_tech, "Tch")] if flag]
            src = f"RL[{'+'.join(cats) or 'none'}]"
        elif self.source == "acc":
            cats = [n for flag, n in [(self.cat_true, "Tr"), (self.cat_standard, "Std"), (self.cat_tech, "Tch")] if flag]
            src = f"Acc[{'+'.join(cats) or 'none'}]"
        elif self.source == "bs":
            src = f"BS[R{self.bs_min_rating},V{self.bs_min_votes},M{self.bs_max_maps}]"
        sts = "".join([s for flag, s in [(self.show_cleared, "✓"), (self.show_nf, "⚠"), (self.show_unplayed, "✗"), (self.show_queued, "Q")] if flag])
        sort_label = {
            "star_asc": "Star↑", "star_desc": "Star↓",
            "pp_high": "PP↓", "pp_low": "PP↑",
            "ap_high": "AP↓", "ap_low": "AP↑",
            "acc_high": "Acc↓", "acc_low": "Acc↑",
            "rank_low": "Rank↑", "rank_high": "Rank↓",
            "bs_rate_high": "Rate↓", "bs_rate_low": "Rate↑",
            "bs_upvotes_high": "⇧Votes↓", "bs_upvotes_low": "⇧Votes↑",
            "bs_downvotes_high": "⇩Votes↓", "bs_downvotes_low": "⇩Votes↑",
            "fc_desc": "FC↓", "fc_asc": "FC↑",
            "status_desc": "Sts↓", "status_asc": "Sts↑",
            "song_desc": "Song↓", "song_asc": "Song↑",
            "date_desc": "Date↓", "date_asc": "Date↑",
            "duration_desc": "Len↓", "duration_asc": "Len↑",
            "bl_plays_desc": "BLPlay↓", "bl_plays_asc": "BLPlay↑",
            "bl_attempts_desc": "BLAtt↓", "bl_attempts_asc": "BLAtt↑",
            "diff_desc": "Diff↓", "diff_asc": "Diff↑",
            "mode_desc": "Mode↓", "mode_asc": "Mode↑",
            "cat_desc": "Cat↓", "cat_asc": "Cat↑",
            "mapper_desc": "Mapper↓", "mapper_asc": "Mapper↑",
            "author_desc": "Author↓", "author_asc": "Author↑",
            "playtime_desc": "Played↓", "playtime_asc": "Played↑",
        }.get(self.sort_mode, self.sort_mode)
        q_tag = f" \U0001f50d\"{self.song_filter}\"" if self.song_filter else ""
        if self.source in ("rl", "acc"):
            return f"{self.label}  [{src} / {sts} / {self.split_mode} / {sort_label}]{q_tag}"
        star = f"★{self.star_min:g}-{self.star_max:g}"
        return f"{self.label}  [{src} / {sts} / {star} / {self.split_mode} / {sort_label}]{q_tag}"


_BATCH_PRESETS: List[_BatchPreset] = [
    _BatchPreset("SS — Uncleared All",                   "ss", "", True,  "star_asc", "", False),
    _BatchPreset("SS — Uncleared per ★",                 "ss", "", True,  "star_asc", "",    True),
    _BatchPreset("SS — High PP per ★",                   "ss", "", False, "pp_high",  "",    True),
    _BatchPreset("BL — Uncleared All",                   "bl", "", True,  "star_asc", "", False),
    _BatchPreset("BL — Uncleared per ★",                 "bl", "", True,  "star_asc", "",    True),
    _BatchPreset("BL — High PP per ★",                   "bl", "", False, "pp_high",  "",    True),
    _BatchPreset("AccSaber RL — Uncleared per Category", "rl", "", True,  "star_asc", "",    True),
    _BatchPreset("AccSaber RL — High AP per Category",   "rl", "", False, "ap_high",  "",    True),
    _BatchPreset("AccSaber RL — Oldest Played per Category", "rl", "", False, "playtime_asc", "", True),
]


def _sort_entries(entries: List[MapEntry], sort_mode: str) -> List[MapEntry]:
    """sort_mode に従って MapEntry をソートした新しいリストを返す。"""
    result = list(entries)
    if sort_mode == "pp_high":
        result.sort(key=lambda e: (-e.player_pp, e.stars, e.song_name))
    elif sort_mode == "pp_low":
        result.sort(key=lambda e: (e.player_pp, e.stars, e.song_name))
    elif sort_mode == "ap_high":
        result.sort(key=lambda e: (-e.acc_rl_ap, e.stars, e.song_name))
    elif sort_mode == "ap_low":
        result.sort(key=lambda e: (e.acc_rl_ap, e.stars, e.song_name))
    elif sort_mode == "acc_high":
        result.sort(key=lambda e: (-e.player_acc, e.stars, e.song_name))
    elif sort_mode == "acc_low":
        result.sort(key=lambda e: (e.player_acc, e.stars, e.song_name))
    elif sort_mode == "rank_low":
        result.sort(key=lambda e: (e.player_rank or 999999, e.stars, e.song_name))
    elif sort_mode == "rank_high":
        result.sort(key=lambda e: (-(e.player_rank or 0), e.stars, e.song_name))
    elif sort_mode == "bs_rate_high":
        result.sort(key=lambda e: (-e.player_pp, e.song_name.lower()))
    elif sort_mode == "bs_rate_low":
        result.sort(key=lambda e: (e.player_pp, e.song_name.lower()))
    elif sort_mode == "bs_upvotes_high":
        result.sort(key=lambda e: (-e.beatsaver_upvotes, e.song_name.lower()))
    elif sort_mode == "bs_upvotes_low":
        result.sort(key=lambda e: (e.beatsaver_upvotes, e.song_name.lower()))
    elif sort_mode == "bs_downvotes_high":
        result.sort(key=lambda e: (-e.beatsaver_downvotes, e.song_name.lower()))
    elif sort_mode == "bs_downvotes_low":
        result.sort(key=lambda e: (e.beatsaver_downvotes, e.song_name.lower()))
    elif sort_mode == "star_desc":
        result.sort(key=lambda e: (-e.stars, e.song_name))
    elif sort_mode == "fc_desc":
        result.sort(key=lambda e: (-int(e.full_combo), e.stars, e.song_name))
    elif sort_mode == "fc_asc":
        result.sort(key=lambda e: (int(e.full_combo), e.stars, e.song_name))
    elif sort_mode == "status_desc":
        result.sort(key=lambda e: (-(30 if e.cleared else 20 if e.nf_clear else 10), e.song_name.lower()))
    elif sort_mode == "status_asc":
        result.sort(key=lambda e: ((30 if e.cleared else 20 if e.nf_clear else 10), e.song_name.lower()))
    elif sort_mode == "song_desc":
        result.sort(key=lambda e: e.song_name.lower(), reverse=True)
    elif sort_mode == "song_asc":
        result.sort(key=lambda e: e.song_name.lower())
    elif sort_mode in ("diff_desc", "diff_asc"):
        _dord = {"Easy": 1, "Normal": 3, "Hard": 5, "Expert": 7, "ExpertPlus": 9}
        result.sort(key=lambda e: (_dord.get(e.difficulty, 0), e.song_name.lower()), reverse=(sort_mode == "diff_desc"))
    elif sort_mode == "mode_desc":
        result.sort(key=lambda e: e.mode.lower(), reverse=True)
    elif sort_mode == "mode_asc":
        result.sort(key=lambda e: e.mode.lower())
    elif sort_mode == "cat_desc":
        result.sort(key=lambda e: e.acc_category.lower(), reverse=True)
    elif sort_mode == "cat_asc":
        result.sort(key=lambda e: e.acc_category.lower())
    elif sort_mode == "mapper_desc":
        result.sort(key=lambda e: e.mapper.lower(), reverse=True)
    elif sort_mode == "mapper_asc":
        result.sort(key=lambda e: e.mapper.lower())
    elif sort_mode == "author_desc":
        result.sort(key=lambda e: e.song_author.lower(), reverse=True)
    elif sort_mode == "author_asc":
        result.sort(key=lambda e: e.song_author.lower())
    elif sort_mode == "date_desc":
        result.sort(
            key=lambda e: (
                0 if e.source_date_ts > 0 else 1,
                -e.source_date_ts if e.source_date_ts > 0 else 0,
                e.song_name.lower(),
            )
        )
    elif sort_mode == "date_asc":
        result.sort(
            key=lambda e: (
                0 if e.source_date_ts > 0 else 1,
                e.source_date_ts if e.source_date_ts > 0 else 0,
                e.song_name.lower(),
            )
        )
    elif sort_mode == "duration_desc":
        result.sort(key=lambda e: (-e.duration_seconds, e.song_name.lower()))
    elif sort_mode == "duration_asc":
        result.sort(key=lambda e: (e.duration_seconds, e.song_name.lower()))
    elif sort_mode == "bl_plays_desc":
        result.sort(key=lambda e: (-e.beatleader_plays, -e.beatleader_attempts, e.song_name.lower()))
    elif sort_mode == "bl_plays_asc":
        result.sort(key=lambda e: (e.beatleader_plays, e.beatleader_attempts, e.song_name.lower()))
    elif sort_mode == "bl_attempts_desc":
        result.sort(key=lambda e: (-e.beatleader_attempts, -e.beatleader_plays, e.song_name.lower()))
    elif sort_mode == "bl_attempts_asc":
        result.sort(key=lambda e: (e.beatleader_attempts, e.beatleader_plays, e.song_name.lower()))
    elif sort_mode == "playtime_desc":
        result.sort(
            key=lambda e: (
                0 if e.cleared else 1 if e.nf_clear else 2,
                -e.played_at_ts if e.cleared else 0,
                e.song_name.lower(),
            )
        )
    elif sort_mode == "playtime_asc":
        result.sort(
            key=lambda e: (
                0 if not e.played else 1 if e.nf_clear and not e.cleared else 2,
                e.played_at_ts if e.cleared else 0,
                e.song_name.lower(),
            )
        )
    else:
        result.sort(key=lambda e: (e.stars, e.song_name))
    return result


def _apply_config_filter(maps: List[MapEntry], cfg: "_BatchConfig") -> List[MapEntry]:
    """_BatchConfig のフィルタ条件をマップリストに適用してソート済みリストを返す。"""
    q = cfg.song_filter.lower() if cfg.song_filter else ""
    keywords = q.split() if q else []
    result: List[MapEntry] = []
    for e in maps:
        if keywords:
            targets = (e.song_name.lower(), e.song_author.lower(), e.mapper.lower())
            if not all(any(kw in t for t in targets) for kw in keywords):
                continue
        if e.stars < cfg.star_min or e.stars >= cfg.star_max:
            continue
        if e.pending:
            if not cfg.show_queued:
                continue
        else:
            if e.cleared and not cfg.show_cleared:
                continue
            if e.nf_clear and not cfg.show_nf:
                continue
            if not e.played and not cfg.show_unplayed:
                continue
        if cfg.source in ("rl", "acc"):
            if e.acc_category == "true" and not cfg.cat_true:
                continue
            if e.acc_category == "standard" and not cfg.cat_standard:
                continue
            if e.acc_category == "tech" and not cfg.cat_tech:
                continue
        result.append(e)
    return _sort_entries(result, cfg.sort_mode)


def _pregenerate_covers(configs: "List[_BatchConfig]") -> Dict[str, str]:
    """必要なカバー画像を事前生成してキャッシュ辞書を返す（メインスレッドで呼ぶこと）。"""
    cache: Dict[str, str] = {}
    for cfg in configs:
        sd = _sort_dir_from_mode(cfg.sort_mode)
        if cfg.split_mode == "star":
            for si in range(21):
                key = f"star:{si}:{sd}:{cfg.source}"
                if key not in cache:
                    cache[key] = _make_playlist_cover("star", str(si), sd, cfg.source)
        elif cfg.split_mode == "category":
            for cat in ("true", "standard", "tech", "unknown"):
                key = f"cat:{cat}:{sd}:{cfg.source}"
                if key not in cache:
                    cache[key] = _make_playlist_cover(cat, "", sd, cfg.source)
        else:
            if cfg.source in ("rl", "acc"):
                # RL/Acc single: cat フラグが1つだけ True ならそのカテゴリテキストを使用
                _rl_cats = [c for c, f in [("true", cfg.cat_true), ("standard", cfg.cat_standard), ("tech", cfg.cat_tech)] if f]
                _rl_ct = _rl_cats[0] if len(_rl_cats) == 1 else "default"
                key = f"acc_single:{cfg.source}:{_rl_ct}:{sd}"
                if key not in cache:
                    cache[key] = _make_playlist_cover(_rl_ct, "", sd, cfg.source)
            else:
                key = f"default:{sd}:{cfg.source}"
                if key not in cache:
                    cache[key] = _make_playlist_cover("default", "", sd, cfg.source)
    return cache


def _config_export_tag(cfg: "_BatchConfig") -> str:
    """_BatchConfig のフィルタ・ソート条件からファイル名タグを生成する。"""
    parts: List[str] = []
    status_tag = _status_filter_tag(
        cfg.show_cleared,
        cfg.show_nf,
        cfg.show_unplayed,
        cfg.show_queued,
    )
    if status_tag is not None:
        parts.append(status_tag)
    if cfg.star_min > 0.0 or cfg.star_max < 20.0:
        parts.append(f"star{cfg.star_min:g}-{cfg.star_max:g}")
    if cfg.source in ("rl", "acc"):
        cats = [n for flag, n in [(cfg.cat_true, "T"), (cfg.cat_standard, "S"), (cfg.cat_tech, "Tc")] if flag]
        if len(cats) < 3:
            parts.append("+".join(cats) if cats else "nocat")
    if cfg.song_filter:
        safe_q = re.sub(r'[\\/:*?"<>|]', '', cfg.song_filter).strip().replace(' ', '-')[:20]
        if safe_q:
            parts.append(safe_q)
    if cfg.source == "bs":
        if cfg.bs_query:
            safe_bs_q = re.sub(r'[\\/:*?"<>|]', '', cfg.bs_query).strip().replace(' ', '-')[:20]
            if safe_bs_q:
                parts.append(f"q-{safe_bs_q}")
        if cfg.bs_min_rating > 0:
            parts.append(f"rate{cfg.bs_min_rating}")
        if cfg.bs_min_votes > 0:
            parts.append(f"votes{cfg.bs_min_votes}")
    _sort_tags = {
        "star_asc": "StarAsc", "star_desc": "StarDesc",
        "date_desc": "DateDesc", "date_asc": "DateAsc",
        "duration_desc": "DurationDesc", "duration_asc": "DurationAsc",
        "bl_plays_desc": "BLPlaysDesc", "bl_plays_asc": "BLPlaysAsc",
        "bl_attempts_desc": "BLAttemptsDesc", "bl_attempts_asc": "BLAttemptsAsc",
        "playtime_desc": "PlayedDesc", "playtime_asc": "PlayedAsc",
        "pp_high": "PPDesc", "pp_low": "PPAsc",
        "ap_high": "APDesc", "ap_low": "APAsc",
        "acc_high": "AccDesc", "acc_low": "AccAsc",
        "rank_low": "RankAsc", "rank_high": "RankDesc",
        "fc_desc": "FCDesc", "fc_asc": "FCAsc",
        "status_desc": "StsDesc", "status_asc": "StsAsc",
        "song_desc": "SongDesc", "song_asc": "SongAsc",
        "diff_desc": "DiffDesc", "diff_asc": "DiffAsc",
        "mode_desc": "ModeDesc", "mode_asc": "ModeAsc",
        "cat_desc": "CatDesc", "cat_asc": "CatAsc",
        "mapper_desc": "MapperDesc", "mapper_asc": "MapperAsc",
        "author_desc": "AuthorDesc", "author_asc": "AuthorAsc",
    }
    parts.append(_sort_tags.get(cfg.sort_mode, cfg.sort_mode))
    return "_".join(parts)


_SORT_SYMBOL: Dict[str, str] = {
    "star_asc":      "★↑",
    "star_desc":     "★↓",
    "date_desc":     "Date↓",
    "date_asc":      "Date↑",
    "duration_desc": "Len↓",
    "duration_asc":  "Len↑",
    "bl_plays_desc": "BLPlay↓",
    "bl_plays_asc":  "BLPlay↑",
    "bl_attempts_desc": "BLAtt↓",
    "bl_attempts_asc":  "BLAtt↑",
    "playtime_desc": "Played↓",
    "playtime_asc":  "Played↑",
    "pp_high":       "PP↓",
    "pp_low":        "PP↑",
    "ap_high":       "AP↓",
    "ap_low":        "AP↑",
    "acc_high":      "Acc↓",
    "acc_low":       "Acc↑",
    "rank_low":      "Rank↑",
    "rank_high":     "Rank↓",
    "bs_rate_high":  "Rate↓",
    "bs_rate_low":   "Rate↑",
    "bs_upvotes_high": "⇧Votes↓",
    "bs_upvotes_low":  "⇧Votes↑",
    "bs_downvotes_high": "⇩Votes↓",
    "bs_downvotes_low":  "⇩Votes↑",
    "fc_desc":       "FC↑",
    "fc_asc":        "FC↓",
    "status_desc":   "Sts↓",
    "status_asc":    "Sts↑",
    "song_desc":     "Song↓",
    "song_asc":      "Song↑",
    "diff_desc":     "Diff↓",
    "diff_asc":      "Diff↑",
    "mode_desc":     "Mode↓",
    "mode_asc":      "Mode↑",
    "cat_desc":      "Cat↓",
    "cat_asc":       "Cat↑",
    "mapper_desc":   "Mapper↓",
    "mapper_asc":    "Mapper↑",
    "author_desc":   "Author↓",
    "author_asc":    "Author↑",
}
_CAT_LABEL: Dict[str, str] = {
    "true": "True", "standard": "Standard", "tech": "Tech",
}
_SRC_LABEL: Dict[str, str] = {
    "ss": "SS", "bl": "BL", "rl": "RL", "acc": "Acc",
}


def _sort_indicator_from_mode(sort_mode: str) -> Tuple[int, Qt.SortOrder]:
    sort_col_map = {
        "status_desc": (_COL_STATUS, Qt.SortOrder.DescendingOrder),
        "status_asc": (_COL_STATUS, Qt.SortOrder.AscendingOrder),
        "song_desc": (_COL_SONG, Qt.SortOrder.DescendingOrder),
        "song_asc": (_COL_SONG, Qt.SortOrder.AscendingOrder),
        "date_desc": (_COL_SOURCE_DATE, Qt.SortOrder.DescendingOrder),
        "date_asc": (_COL_SOURCE_DATE, Qt.SortOrder.AscendingOrder),
        "duration_desc": (_COL_DURATION, Qt.SortOrder.DescendingOrder),
        "duration_asc": (_COL_DURATION, Qt.SortOrder.AscendingOrder),
        "bl_plays_desc": (_COL_BL_PLAYS, Qt.SortOrder.DescendingOrder),
        "bl_plays_asc": (_COL_BL_PLAYS, Qt.SortOrder.AscendingOrder),
        "bl_attempts_desc": (_COL_BL_ATTEMPTS, Qt.SortOrder.DescendingOrder),
        "bl_attempts_asc": (_COL_BL_ATTEMPTS, Qt.SortOrder.AscendingOrder),
        "playtime_desc": (_COL_PLAY_TIME, Qt.SortOrder.DescendingOrder),
        "playtime_asc": (_COL_PLAY_TIME, Qt.SortOrder.AscendingOrder),
        "diff_desc": (_COL_DIFF, Qt.SortOrder.DescendingOrder),
        "diff_asc": (_COL_DIFF, Qt.SortOrder.AscendingOrder),
        "mode_desc": (_COL_MODE, Qt.SortOrder.DescendingOrder),
        "mode_asc": (_COL_MODE, Qt.SortOrder.AscendingOrder),
        "cat_desc": (_COL_ACC_CAT, Qt.SortOrder.DescendingOrder),
        "cat_asc": (_COL_ACC_CAT, Qt.SortOrder.AscendingOrder),
        "pp_high": (_COL_PLAYER_PP, Qt.SortOrder.DescendingOrder),
        "pp_low": (_COL_PLAYER_PP, Qt.SortOrder.AscendingOrder),
        "ap_high": (_COL_PLAYER_PP, Qt.SortOrder.DescendingOrder),
        "ap_low": (_COL_PLAYER_PP, Qt.SortOrder.AscendingOrder),
        "acc_high": (_COL_PLAYER_ACC, Qt.SortOrder.DescendingOrder),
        "acc_low": (_COL_PLAYER_ACC, Qt.SortOrder.AscendingOrder),
        "rank_low": (_COL_PLAYER_RANK, Qt.SortOrder.AscendingOrder),
        "rank_high": (_COL_PLAYER_RANK, Qt.SortOrder.DescendingOrder),
        "bs_rate_high": (_COL_BS_RATE, Qt.SortOrder.DescendingOrder),
        "bs_rate_low": (_COL_BS_RATE, Qt.SortOrder.AscendingOrder),
        "bs_upvotes_high": (_COL_BS_UPVOTES, Qt.SortOrder.DescendingOrder),
        "bs_upvotes_low": (_COL_BS_UPVOTES, Qt.SortOrder.AscendingOrder),
        "bs_downvotes_high": (_COL_BS_DOWNVOTES, Qt.SortOrder.DescendingOrder),
        "bs_downvotes_low": (_COL_BS_DOWNVOTES, Qt.SortOrder.AscendingOrder),
        "star_asc": (_COL_STARS, Qt.SortOrder.AscendingOrder),
        "star_desc": (_COL_STARS, Qt.SortOrder.DescendingOrder),
        "fc_desc": (_COL_FC, Qt.SortOrder.DescendingOrder),
        "fc_asc": (_COL_FC, Qt.SortOrder.AscendingOrder),
        "mapper_desc": (_COL_MAPPER, Qt.SortOrder.DescendingOrder),
        "mapper_asc": (_COL_MAPPER, Qt.SortOrder.AscendingOrder),
        "author_desc": (_COL_AUTHOR, Qt.SortOrder.DescendingOrder),
        "author_asc": (_COL_AUTHOR, Qt.SortOrder.AscendingOrder),
    }
    return sort_col_map.get(sort_mode, (_COL_STATUS, Qt.SortOrder.DescendingOrder))


def _status_filter_tag(
    show_cleared: bool,
    show_nf: bool,
    show_unplayed: bool,
    show_queued: bool,
) -> Optional[str]:
    if show_cleared and show_nf and show_unplayed:
        if show_queued:
            return "+Q"
        return None

    parts: List[str] = []
    if show_cleared:
        parts.append("Cleared")
    if show_nf:
        parts.append("NF")
    if show_unplayed:
        parts.append("Unplayed")
    if show_queued:
        parts.append("Q")
    return "+".join(parts) if parts else "none"


def _playlist_title(
    cfg: "_BatchConfig",
    star_group: Optional[int] = None,
    category: Optional[str] = None,
) -> str:
    """プレイリストタイトルを生成する。
    形式: {サービス}★{番号} / {サービス} {カテゴリ}  +  {フィルター(あれば)}  +  {ソート記号}
    """
    src = _SRC_LABEL.get(cfg.source, cfg.source.upper())
    sort_sym = _SORT_SYMBOL.get(cfg.sort_mode, cfg.sort_mode)

    # --- カテゴリ / ★ 部分 ---
    if star_group is not None:
        head = f"{src}★{star_group}"
    elif category is not None:
        head = f"{src} {_CAT_LABEL.get(category, category.capitalize())}"
    else:  # single
        if cfg.source in ("rl", "acc"):
            rl_cats = [c for c, f in [("true", cfg.cat_true), ("standard", cfg.cat_standard), ("tech", cfg.cat_tech)] if f]
            if len(rl_cats) == 1:
                head = f"{src} {_CAT_LABEL.get(rl_cats[0], rl_cats[0].capitalize())}"
            else:
                head = src
        else:
            head = src

    # --- フィルター部分（全ステータスが有効な場合は省略）---
    filter_str = _status_filter_tag(
        cfg.show_cleared,
        cfg.show_nf,
        cfg.show_unplayed,
        cfg.show_queued,
    ) or ""

    if cfg.song_filter:
        filter_parts = [p for p in [filter_str, f'"{cfg.song_filter}"'] if p]
        filter_str = "+".join(filter_parts)

    parts = [p for p in [head, filter_str, sort_sym] if p]
    return " ".join(parts)


def _write_config_files(
    maps: List[MapEntry],
    cfg: "_BatchConfig",
    folder_path: Path,
    saved: List[str],
    errors: List[str],
    covers: Dict[str, str],
) -> None:
    """_BatchConfig の split_mode に従ってファイルを書き出す。"""
    tag = _config_export_tag(cfg)
    src_pfx = _BATCH_SRC_PREFIX.get(cfg.source, cfg.source.upper())
    _legacy = {"All", "single", "split", "cat"}
    fname_base = "" if cfg.filename_base in _legacy else cfg.filename_base
    fbase = "_".join(p for p in [src_pfx, fname_base, tag] if p)
    if cfg.split_mode == "star":
        groups: Dict[int, List[MapEntry]] = {}
        for e in maps:
            si = max(1, math.floor(e.stars)) if e.stars > 0 else 0
            groups.setdefault(si, []).append(e)
        _sort_dir = _sort_dir_from_mode(cfg.sort_mode)
        for si in sorted(groups.keys()):
            fname = f"{fbase}_{si:02d}star.bplist"
            _img = covers.get(f"star:{si}:{_sort_dir}:{cfg.source}", "")
            bplist = _make_bplist(_playlist_title(cfg, star_group=si), groups[si], _img)
            (folder_path / fname).write_text(
                json.dumps(bplist, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            saved.append(fname)
    elif cfg.split_mode == "category":
        cat_groups: Dict[str, List[MapEntry]] = {"true": [], "standard": [], "tech": []}
        for e in maps:
            cat = e.acc_category or "unknown"
            cat_groups.setdefault(cat, []).append(e)
        _sort_dir = _sort_dir_from_mode(cfg.sort_mode)
        for cat in sorted(cat_groups.keys()):
            fname = f"{fbase}_{cat.capitalize()}.bplist"
            _img = covers.get(f"cat:{cat}:{_sort_dir}:{cfg.source}", "")
            bplist = _make_bplist(_playlist_title(cfg, category=cat), cat_groups[cat], _img)
            (folder_path / fname).write_text(
                json.dumps(bplist, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            saved.append(fname)
    else:  # "single"
        fname = f"{fbase}.bplist"
        _sort_dir = _sort_dir_from_mode(cfg.sort_mode)
        if cfg.source in ("rl", "acc"):
            _rl_cats = [c for c, f in [("true", cfg.cat_true), ("standard", cfg.cat_standard), ("tech", cfg.cat_tech)] if f]
            _rl_ct = _rl_cats[0] if len(_rl_cats) == 1 else "default"
            _img = covers.get(f"acc_single:{cfg.source}:{_rl_ct}:{_sort_dir}", "")
        else:
            _img = covers.get(f"default:{_sort_dir}:{cfg.source}", "")
        bplist = _make_bplist(_playlist_title(cfg), maps, _img)
        (folder_path / fname).write_text(
            json.dumps(bplist, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        saved.append(fname)


def _run_export_configs(
    sigs: "_LoadSignals",
    steam_id: Optional[str],
    configs: "List[_BatchConfig]",
    folder_path: Path,
    covers: Dict[str, str],
) -> None:
    """バッチ設定リストを使って最新データをロードしてエクスポートする（スレッド実行）。
    完了時: sigs.finished.emit([saved_files, errors])
    """
    try:
        saved_files, errors = export_playlist_configs(
            steam_id,
            configs,
            folder_path,
            progress=lambda done, total, label: sigs.progress.emit(done, total, label),
            covers=covers,
        )
        sigs.finished.emit([saved_files, errors])
    except Exception as top_exc:
        sigs.error.emit(str(top_exc))


# ──────────────────────────────────────────────────────────────────────────────
# PlaylistWindow
# ──────────────────────────────────────────────────────────────────────────────

# テーブル列インデックス
_COL_STATUS = 0
_COL_COVER = 1
_COL_SONG = 2
_COL_ONECLICK = 3
_COL_DELETE = 4
_COL_SOURCE_DATE = 5
_COL_DURATION = 6
_COL_PLAY_TIME = 7
_COL_DIFF = 8
_COL_MODE = 9
_COL_ACC_CAT = 10
_COL_SERVICE = 11     # スコア元サービス表示 (BL / SS / AS)
_COL_PLAYER_RANK = 12
_COL_STARS = 13
_COL_PLAYER_ACC = 14
_COL_PLAYER_PP = 15
_COL_BS_RATE = 16
_COL_BS_UPVOTES = 17
_COL_BS_DOWNVOTES = 18
_COL_FC = 19
_COL_MOD = 20
_COL_MAPPER = 21
_COL_AUTHOR = 22
_COL_BL_PLAYS = 23
_COL_BL_ATTEMPTS = 24
_COL_COUNT = 25

_COL_LABELS = [
    "Status", "Cover", "Song", "DL", "Del", "Date", "Length", "Played At", "Diff", "Mode", "Category", "Service", "Rank", "★", "Acc %", "PP",
    "Rate %", "⇧", "⇩", "FC", "Mods", "Mapper", "Author", "BL Plays", "BL Attempts",
]


class _PresetListWidget(QListWidget):
    """行テキストクリックでもチェックボックスをトグルできる QListWidget。"""

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        item = self.itemAt(event.pos())
        if item is not None and event.button() == Qt.MouseButton.LeftButton:
            before = item.checkState()
            super().mouseReleaseEvent(event)
            # Qt が (already-selected 等の理由で) トグルしなかった場合は手動トグル
            if item.checkState() == before:
                new = (
                    Qt.CheckState.Unchecked
                    if before == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                )
                item.setCheckState(new)
        else:
            super().mouseReleaseEvent(event)


class PlaylistWindow(QMainWindow):
    """Playlist 画面デモ。"""

    def __init__(
        self,
        steam_id: Optional[str] = None,
        initial_source_tab: str = "snapshot",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Playlist / Maps")
        self.resize(1300, 800)

        self._steam_id = steam_id
        self._all_entries: List[MapEntry] = []   # ロード済み全データ
        self._filtered: List[MapEntry] = []       # フィルタ後データ
        self._snapshot_all_entries: List[MapEntry] = []
        self._snapshot_filtered: List[MapEntry] = []
        self._maps_all_entries: List[MapEntry] = []
        self._maps_filtered: List[MapEntry] = []
        self._snapshot_source_key = "ss"
        self._maps_source_key = "bs"
        self._snapshot_loaded_source_key = ""
        self._maps_loaded_source_key = ""
        self._snapshot_loaded_steam_id: Optional[str] = None
        self._maps_loaded_steam_id: Optional[str] = None
        self._pending_load_source_key = ""
        self._pending_load_maps_tab = False
        self._snapshot_last_load_text = "Last Load: -"
        self._maps_last_load_text = "Last Load: -"
        self._snapshot_sort_mode = "status_desc"
        self._maps_sort_mode = "date_desc"
        self._load_signals = _LoadSignals()
        self._load_signals.finished.connect(self._on_load_finished)
        self._load_signals.error.connect(self._on_load_error)
        self._load_signals.progress.connect(self._on_load_progress)
        self._preview_signals = _PreviewSignals()
        self._preview_signals.loaded.connect(self._on_preview_loaded)
        self._preview_signals.error.connect(self._on_preview_error)
        self._thumbnail_signals = _ThumbnailSignals()
        self._thumbnail_signals.loaded.connect(self._on_thumbnail_loaded)
        self._thumbnail_signals.error.connect(self._on_thumbnail_error)
        self._beatsaver_meta_signals = _BeatSaverMetaSignals()
        self._beatsaver_meta_signals.finished.connect(self._on_beatsaver_meta_batch_finished)
        self._beatsaver_meta_signals.error.connect(self._on_beatsaver_meta_batch_error)
        self._preview_cache: Dict[str, bytes] = {}
        self._thumbnail_cache: Dict[str, QPixmap] = {}
        self._thumbnail_queue: List[str] = []
        self._thumbnail_pending: set[str] = set()
        self._thumbnail_active_url = ""
        self._installed_beatsaber_dir = ""
        self._installed_level_keys: set[str] = set()
        self._installed_level_dirs: Dict[str, Path] = {}
        self._row_height = 34
        self._restored_snapshot_state: Optional[dict] = None
        self._restored_maps_state: Optional[dict] = None
        self._highest_diff_only_snapshot = False
        self._highest_diff_only_maps = True
        self._bs_rating_sync = False
        self._bs_votes_sync = False
        self._bs_date_sync = False
        self._preview_token = 0
        self._current_preview_url = ""
        self._progress_dlg: Optional[QProgressDialog] = None
        self._deferred_maps_restore_scheduled = False
        self._bl_api_session = requests.Session()
        self._bl_top_replay_cache: Dict[Tuple[str, str], str] = {}
        self._bl_preview_replay_index: Optional[Dict[Tuple[str, str, str], str]] = None
        self._bl_preview_leaderboard_index: Optional[Dict[Tuple[str, str, str], str]] = None
        self._beatsaver_meta_pending_hashes: List[str] = []
        self._beatsaver_meta_pending_set: set[str] = set()
        self._beatsaver_meta_pending_seed_map: Dict[str, str] = {}
        self._beatsaver_meta_inflight_hashes: set[str] = set()
        self._beatsaver_meta_total_hashes: set[str] = set()
        self._beatsaver_meta_completed_hashes: set[str] = set()
        self._beatsaver_meta_active_hash = ""
        self._preview_description_text = ""
        self._preview_title_full_text = "No map selected"
        self._table_render_token = 0
        self._table_render_active = False
        self._pending_restore_entry: Optional[MapEntry] = None

        central = QWidget(self)
        self.setCentralWidget(central)
        _main_layout = QHBoxLayout(central)
        _main_layout.setSpacing(0)
        _main_layout.setContentsMargins(4, 4, 4, 4)
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        _main_layout.addWidget(self._splitter)
        self.__cols = self._splitter  # right panel を後で追加するため保持
        _left_w = QWidget()
        root = QVBoxLayout(_left_w)
        root.setSpacing(4)
        root.setContentsMargins(2, 2, 2, 2)
        self._splitter.addWidget(_left_w)

        # ─ Source ───────────────────────────────────────────────────
        src_group = QGroupBox("Source")
        src_vbox = QVBoxLayout(src_group)
        src_vbox.setSpacing(4)
        src_vbox.setContentsMargins(6, 16, 6, 4)

        self._source_tabs = QTabWidget()
        self._source_tabs.setDocumentMode(True)
        self._source_tabs.setStyleSheet(self._source_tabs_stylesheet())
        src_vbox.addWidget(self._source_tabs)

        _snapshot_tab = QWidget()
        _snapshot_layout = QVBoxLayout(_snapshot_tab)
        _snapshot_layout.setSpacing(4)
        _snapshot_layout.setContentsMargins(0, 0, 0, 0)

        _maps_tab = QWidget()
        _maps_layout = QVBoxLayout(_maps_tab)
        _maps_layout.setSpacing(4)
        _maps_layout.setContentsMargins(0, 0, 0, 0)

        self._source_tab_snapshot_idx = self._source_tabs.addTab(_snapshot_tab, "Snapshot")
        self._source_tab_maps_idx = self._source_tabs.addTab(_maps_tab, "Maps")

        self._src_group = QButtonGroup(self)
        self._rb_ss = QRadioButton(SOURCE_SS)
        self._rb_bl = QRadioButton(SOURCE_BL)
        self._rb_acc = QRadioButton(SOURCE_ACC)
        self._rb_acc_rl = QRadioButton(SOURCE_ACC_RL)
        self._rb_bs = QRadioButton(SOURCE_BS)
        self._rb_open = QRadioButton(SOURCE_OPEN)
        self._rb_ss.setChecked(True)

        # 1行目: ScoreSaber / BeatLeader / AccSaber / AccSaber RL / BeatSaver
        src_row1 = QHBoxLayout()
        src_row1.setSpacing(8)
        for i, rb in enumerate([self._rb_ss, self._rb_bl, self._rb_acc, self._rb_acc_rl]):
            self._src_group.addButton(rb, i)
            src_row1.addWidget(rb)
        src_row1.addStretch()
        _snapshot_layout.addLayout(src_row1)
        self._secondary_buttons: List[QPushButton] = []
        self._last_snapshot_source_button: QRadioButton = self._rb_ss

        def _set_standard_button_height(*buttons: QPushButton, height: int = 26) -> None:
            for button in buttons:
                button.setMinimumHeight(height)

        def _set_nonshrinking_button_width(*buttons: QPushButton) -> None:
            for button in buttons:
                button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
                button.setMinimumWidth(max(button.minimumWidth(), button.sizeHint().width()))

        def _register_secondary_buttons(*buttons: QPushButton) -> None:
            _set_standard_button_height(*buttons)
            self._secondary_buttons.extend(buttons)

        # 2行目: Open File + ファイル操作 + Load ボタン
        src_row2 = QHBoxLayout()
        src_row2.setSpacing(8)
        self._src_group.addButton(self._rb_open, 5)
        src_row2.addWidget(self._rb_open)

        # Open file
        self._open_edit = QLineEdit()
        self._open_edit.setPlaceholderText(".bplist / .json file path...")
        self._open_edit.setEnabled(False)
        self._open_edit.setMinimumWidth(240)
        self._btn_browse = QPushButton("Browse...")
        _register_secondary_buttons(self._btn_browse)
        self._btn_browse.setEnabled(False)
        self._btn_browse.clicked.connect(self._browse_bplist)
        self._svc_label = QLabel("Service:")
        self._svc_label.setEnabled(False)
        self._svc_combo = QComboBox()
        self._svc_combo.addItem("None", userData="none")
        self._svc_combo.addItem("ScoreSaber", userData="scoresaber")
        self._svc_combo.addItem("BeatLeader", userData="beatleader")
        self._svc_combo.addItem("AccSaber", userData="accsaber")
        self._svc_combo.addItem("AccSaber RL", userData="accsaber_rl")
        self._svc_combo.setEnabled(False)
        self._svc_combo.currentIndexChanged.connect(self._on_svc_combo_changed)
        src_row2.addWidget(self._open_edit)
        src_row2.addWidget(self._btn_browse)
        src_row2.addWidget(self._svc_label)
        src_row2.addWidget(self._svc_combo)
        src_row2.addStretch()

        _snapshot_layout.addLayout(src_row2)

        self._src_group.addButton(self._rb_bs, 4)

        self._btn_load = QPushButton("⏵  Load")
        self._btn_load.setMinimumHeight(28)
        self._btn_load.setMinimumWidth(90)
        self._btn_load.setStyleSheet(
            "QPushButton { background-color: #1976D2; color: white; font-weight: bold;"
            " border-radius: 4px; padding: 2px 10px; }"
            " QPushButton:hover { background-color: #1E88E5; }"
            " QPushButton:pressed { background-color: #1565C0; }"
            " QPushButton:disabled { background-color: #555; color: #aaa; }"
        )
        self._btn_load.clicked.connect(self._on_load_clicked)
        self._last_load_label = QLabel("Last Load: -")
        self._last_load_label.setStyleSheet("color: #aaa;")

        src_footer_row = QHBoxLayout()
        src_footer_row.setSpacing(8)
        src_footer_row.addStretch()
        src_footer_row.addWidget(self._last_load_label)
        src_footer_row.addWidget(self._btn_load)
        src_vbox.addLayout(src_footer_row)

        self._bs_filter_row_widget = QWidget()
        _bs_filter_rows = QVBoxLayout(self._bs_filter_row_widget)
        _bs_filter_rows.setContentsMargins(0, 0, 0, 0)
        _bs_filter_rows.setSpacing(4)
        _bs_filter_row_top = QHBoxLayout()
        _bs_filter_row_top.setContentsMargins(0, 0, 0, 0)
        _bs_filter_row_top.setSpacing(8)
        _bs_filter_row_bottom = QHBoxLayout()
        _bs_filter_row_bottom.setContentsMargins(0, 0, 0, 0)
        _bs_filter_row_bottom.setSpacing(8)
        self._bs_filter_label = QLabel("BeatSaver Load:")
        self._bs_query_label = QLabel("Query:")
        self._bs_query_edit = QLineEdit()
        self._bs_query_edit.setPlaceholderText("Optional BeatSaver API query")
        self._bs_query_edit.setToolTip("Load時に BeatSaver API へ渡す検索語です。空欄なら新着を対象にします")
        self._bs_query_edit.setMinimumWidth(180)
        self._bs_max_label = QLabel("MAX:")
        self._bs_max_maps = QSpinBox()
        self._bs_max_maps.setRange(1, 1000)
        self._bs_max_maps.setValue(1000)
        self._bs_max_maps.setToolTip("Load時に取得する最大件数")
        self._bs_window_label = QLabel("Days:")
        self._bs_days = QSpinBox()
        self._bs_days.setRange(1, 365)
        self._bs_days.setValue(7)
        self._bs_days.setSuffix(" d")
        self._bs_days.setToolTip("直近何日分を検索対象にするか")
        self._bs_from_label = QLabel("From:")
        self._bs_from_date = QDateEdit()
        self._bs_from_date.setCalendarPopup(True)
        self._bs_from_date.setDisplayFormat("yyyy/MM/dd")
        self._bs_from_date.setMinimumWidth(120)
        self._bs_from_date.setToolTip("検索開始日")
        self._bs_to_label = QLabel("To:")
        self._bs_to_date = QDateEdit()
        self._bs_to_date.setCalendarPopup(True)
        self._bs_to_date.setDisplayFormat("yyyy/MM/dd")
        self._bs_to_date.setMinimumWidth(120)
        self._bs_to_date.setToolTip("検索終了日")
        self._bs_to_latest_btn = QPushButton("Latest")
        self._bs_to_latest_btn.setToolTip("To を今日の日付に設定します")
        self._bs_min_rating = QSlider(Qt.Orientation.Horizontal)
        self._bs_min_rating.setRange(0, 100)
        self._bs_min_rating.setValue(50)
        self._bs_min_rating.setToolTip("BeatSaver rating (%) の下限")
        self._bs_min_rating.setFixedWidth(110)
        self._bs_rating_label = QLabel("Rating ≥")
        self._bs_rating_value_label = QSpinBox()
        self._bs_rating_value_label.setRange(0, 100)
        self._bs_rating_value_label.setSuffix(" %")
        self._bs_rating_value_label.setValue(50)
        self._bs_rating_value_label.setFixedWidth(70)
        self._bs_min_votes = QSlider(Qt.Orientation.Horizontal)
        self._bs_min_votes.setRange(0, 1000)
        self._bs_min_votes.setValue(0)
        self._bs_min_votes.setToolTip("BeatSaver votes の下限")
        self._bs_min_votes.setFixedWidth(110)
        self._bs_votes_value_label = QSpinBox()
        self._bs_votes_value_label.setRange(0, 1000)
        self._bs_votes_value_label.setValue(0)
        self._bs_votes_value_label.setFixedWidth(72)
        self._bs_votes_label = QLabel("Votes ≥")
        self._cb_bs_unranked = QCheckBox("Unranked only")
        self._cb_bs_unranked.setChecked(True)
        self._cb_bs_no_ai = QCheckBox("Exclude AI")
        self._cb_bs_no_ai.setChecked(True)
        _bs_filter_row_top.addWidget(self._bs_filter_label)
        _bs_filter_row_top.addWidget(self._bs_query_label)
        _bs_filter_row_top.addWidget(self._bs_query_edit)
        _bs_filter_row_top.addWidget(self._bs_max_label)
        _bs_filter_row_top.addWidget(self._bs_max_maps)
        _bs_filter_row_top.addWidget(self._bs_window_label)
        _bs_filter_row_top.addWidget(self._bs_days)
        _bs_filter_row_top.addWidget(self._bs_from_label)
        _bs_filter_row_top.addWidget(self._bs_from_date)
        _bs_filter_row_top.addWidget(self._bs_to_label)
        _bs_filter_row_top.addWidget(self._bs_to_date)
        _bs_filter_row_top.addWidget(self._bs_to_latest_btn)
        _bs_filter_row_top.addStretch()
        _bs_filter_row_bottom.addSpacing(6)
        _bs_filter_row_bottom.addWidget(self._bs_rating_label)
        _bs_filter_row_bottom.addWidget(self._bs_min_rating)
        _bs_filter_row_bottom.addWidget(self._bs_rating_value_label)
        _bs_filter_row_bottom.addWidget(self._bs_votes_label)
        _bs_filter_row_bottom.addWidget(self._bs_min_votes)
        _bs_filter_row_bottom.addWidget(self._bs_votes_value_label)
        _bs_filter_row_bottom.addWidget(self._cb_bs_unranked)
        _bs_filter_row_bottom.addWidget(self._cb_bs_no_ai)
        _bs_filter_row_bottom.addStretch()
        _bs_filter_rows.addLayout(_bs_filter_row_top)
        _bs_filter_rows.addLayout(_bs_filter_row_bottom)
        self._bs_days.valueChanged.connect(self._sync_bs_dates_from_days)
        self._bs_from_date.dateChanged.connect(self._sync_bs_days_from_dates)
        self._bs_to_date.dateChanged.connect(self._sync_bs_days_from_dates)
        self._bs_to_latest_btn.clicked.connect(self._set_bs_to_latest)
        self._bs_min_rating.valueChanged.connect(self._on_bs_source_rating_changed)
        self._bs_min_votes.valueChanged.connect(self._on_bs_source_votes_changed)
        self._bs_rating_value_label.valueChanged.connect(self._on_bs_source_rating_changed)
        self._bs_votes_value_label.valueChanged.connect(self._on_bs_source_votes_changed)
        _register_secondary_buttons(self._bs_to_latest_btn)
        _maps_layout.addWidget(self._bs_filter_row_widget)

        self._src_group.buttonToggled.connect(self._on_source_changed)
        self._source_tabs.currentChanged.connect(self._on_source_tab_changed)
        root.addWidget(src_group)

        # ─ Filter ───────────────────────────────────────────────────
        filter_group = QGroupBox("Loaded List Filter")
        filter_layout = QVBoxLayout(filter_group)
        filter_layout.setSpacing(6)
        filter_layout.setContentsMargins(6, 16, 6, 9)
        filter_row1 = QHBoxLayout()
        filter_row1.setSpacing(8)
        filter_row2 = QHBoxLayout()
        filter_row2.setSpacing(8)

        filter_row1.addSpacing(6)
        self._star_label = QLabel("★ Stars:")
        filter_row1.addWidget(self._star_label)
        self._star_min = QDoubleSpinBox()
        self._star_min.setRange(0.0, 20.0)
        self._star_min.setDecimals(1)
        self._star_min.setSingleStep(0.5)
        self._star_min.setValue(0.0)
        self._star_min.setFixedWidth(68)
        self._star_min.valueChanged.connect(self._apply_filter)
        filter_row1.addWidget(self._star_min)
        self._star_sep_label = QLabel("–")
        filter_row1.addWidget(self._star_sep_label)
        self._star_max = QDoubleSpinBox()
        self._star_max.setRange(0.0, 20.0)
        self._star_max.setDecimals(1)
        self._star_max.setSingleStep(0.5)
        self._star_max.setValue(20.0)
        self._star_max.setFixedWidth(68)
        self._star_max.valueChanged.connect(self._apply_filter)
        filter_row1.addWidget(self._star_max)

        #filter_row1.addSpacing(8)
        self._cat_filter_label = QLabel("Category:")
        self._cat_filter_label.setVisible(False)
        filter_row1.addWidget(self._cat_filter_label)
        self._cb_cat_true = QCheckBox("True")
        self._cb_cat_true.setChecked(True)
        self._cb_cat_true.setVisible(False)
        self._cb_cat_true.toggled.connect(self._apply_filter)
        self._cb_cat_standard = QCheckBox("Standard")
        self._cb_cat_standard.setChecked(True)
        self._cb_cat_standard.setVisible(False)
        self._cb_cat_standard.toggled.connect(self._apply_filter)
        self._cb_cat_tech = QCheckBox("Tech")
        self._cb_cat_tech.setChecked(True)
        self._cb_cat_tech.setVisible(False)
        self._cb_cat_tech.toggled.connect(self._apply_filter)
        filter_row1.addWidget(self._cb_cat_true)
        filter_row1.addWidget(self._cb_cat_standard)
        filter_row1.addWidget(self._cb_cat_tech)
        self._bs_post_filter_widget = QWidget()
        filter_row3 = QHBoxLayout(self._bs_post_filter_widget)
        filter_row3.setContentsMargins(0, 0, 0, 0)
        filter_row3.setSpacing(8)
        self._bs_post_filter_label = QLabel("BeatSaver:")
        self._bs_filter_rating_label = QLabel("Rating ≥")
        self._bs_filter_min_rating = QSlider(Qt.Orientation.Horizontal)
        self._bs_filter_min_rating.setRange(0, 100)
        self._bs_filter_min_rating.setValue(50)
        self._bs_filter_min_rating.setFixedWidth(110)
        self._bs_filter_min_rating.valueChanged.connect(self._on_bs_filter_rating_changed)
        self._bs_filter_rating_value_label = QSpinBox()
        self._bs_filter_rating_value_label.setRange(0, 100)
        self._bs_filter_rating_value_label.setSuffix(" %")
        self._bs_filter_rating_value_label.setValue(50)
        self._bs_filter_rating_value_label.setFixedWidth(70)
        self._bs_filter_rating_value_label.valueChanged.connect(self._on_bs_filter_rating_changed)
        self._bs_filter_votes_label = QLabel("Votes ≥")
        self._bs_filter_min_votes = QSlider(Qt.Orientation.Horizontal)
        self._bs_filter_min_votes.setRange(0, 1000)
        self._bs_filter_min_votes.setValue(0)
        self._bs_filter_min_votes.setFixedWidth(110)
        self._bs_filter_min_votes.valueChanged.connect(self._on_bs_filter_votes_changed)
        self._bs_filter_votes_value_label = QSpinBox()
        self._bs_filter_votes_value_label.setRange(0, 1000)
        self._bs_filter_votes_value_label.setValue(0)
        self._bs_filter_votes_value_label.setFixedWidth(72)
        self._bs_filter_votes_value_label.valueChanged.connect(self._on_bs_filter_votes_changed)
        filter_row3.addSpacing(6)
        filter_row3.addWidget(self._bs_post_filter_label)
        filter_row3.addWidget(self._bs_filter_rating_label)
        filter_row3.addWidget(self._bs_filter_min_rating)
        filter_row3.addWidget(self._bs_filter_rating_value_label)
        filter_row3.addWidget(self._bs_filter_votes_label)
        filter_row3.addWidget(self._bs_filter_min_votes)
        filter_row3.addWidget(self._bs_filter_votes_value_label)
        filter_row1.addWidget(self._bs_post_filter_widget)
        filter_row1.addStretch()
        self._bs_to_date.setDate(QDate.currentDate())
        self._sync_bs_dates_from_days()
        self._set_bs_rating_value(50)
        self._set_bs_votes_value(0)
        self._count_label = QLabel("0 maps")
        filter_row1.addWidget(self._count_label)

        filter_row2.addWidget(QLabel("🔍 Song: "))
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Filter loaded rows by song / author / mapper...")
        self._search_edit.setToolTip("Load後の一覧を絞り込みます。スペース区切りで複数キーワードのAND検索ができます")
        self._search_edit.setMinimumWidth(180)
        self._search_edit.textChanged.connect(self._apply_filter)
        filter_row2.addWidget(self._search_edit)

        filter_row2.addSpacing(12)
        filter_row2.addWidget(QLabel("Status:"))
        self._cb_sts_cleared = QCheckBox("Cleared ✔")
        self._cb_sts_cleared.setChecked(True)
        self._cb_sts_cleared.toggled.connect(self._apply_filter)
        self._cb_sts_nf = QCheckBox("NF ⚠")
        self._cb_sts_nf.setChecked(True)
        self._cb_sts_nf.toggled.connect(self._apply_filter)
        self._cb_sts_unplayed = QCheckBox("Unplayed ✖")
        self._cb_sts_unplayed.setChecked(True)
        self._cb_sts_unplayed.toggled.connect(self._apply_filter)
        self._cb_sts_queued = QCheckBox("Que [Q]")
        self._cb_sts_queued.setChecked(False)
        self._cb_sts_queued.toggled.connect(self._apply_filter)
        self._cb_bs_not_downloaded = QCheckBox("Not downloaded")
        self._cb_bs_not_downloaded.setChecked(True)
        self._cb_bs_not_downloaded.setToolTip("Beat Saber に未ダウンロードの譜面を表示します")
        self._cb_bs_not_downloaded.toggled.connect(self._apply_filter)
        self._cb_bs_downloaded = QCheckBox("Downloaded")
        self._cb_bs_downloaded.setChecked(True)
        self._cb_bs_downloaded.setToolTip("Beat Saber にダウンロード済みの譜面を表示します")
        self._cb_bs_downloaded.toggled.connect(self._apply_filter)
        self._cb_top_diff_only = QCheckBox("Highest diff only")
        self._cb_top_diff_only.setToolTip("複数難易度がある場合、最高難易度の譜面のみを表示します")
        self._cb_top_diff_only.setChecked(self._highest_diff_only_snapshot)
        self._cb_top_diff_only.toggled.connect(self._on_top_diff_only_toggled)
        filter_row2.addWidget(self._cb_sts_cleared)
        filter_row2.addWidget(self._cb_sts_nf)
        filter_row2.addWidget(self._cb_sts_unplayed)
        filter_row2.addWidget(self._cb_sts_queued)
        filter_row2.addWidget(self._cb_bs_not_downloaded)
        filter_row2.addWidget(self._cb_bs_downloaded)
        filter_row2.addWidget(self._cb_top_diff_only)
        filter_row2.addStretch()
        self._btn_filter_reset = QPushButton("Reset")
        _register_secondary_buttons(self._btn_filter_reset)
        self._btn_filter_reset.clicked.connect(self._on_reset_filters_clicked)
        filter_row2.addWidget(self._btn_filter_reset)

        filter_layout.addLayout(filter_row1)
        filter_layout.addLayout(filter_row2)

        root.addWidget(filter_group)

        # ─ Export ───────────────────────────────────────────────────
        export_group = QGroupBox("Export")
        export_row = QHBoxLayout(export_group)
        export_row.setSpacing(12)

        # Split 条件
        export_row.addWidget(QLabel("Style:  "))
        self._export_style_grp = QButtonGroup(self)
        self._rb_exp_single = QRadioButton("Single file")
        self._rb_exp_single.setToolTip("単一ファイルとして出力します")

        self._rb_exp_split = QRadioButton("Split by ★")
        self._rb_exp_split.setToolTip("★ごとにファイルを分割して出力します")

        self._rb_exp_single.setChecked(True)
        self._export_style_grp.addButton(self._rb_exp_single, 0)
        self._export_style_grp.addButton(self._rb_exp_split, 1)
        export_row.addWidget(self._rb_exp_single)
        export_row.addWidget(self._rb_exp_split)

        export_row.addSpacing(16)
        export_row.addWidget(QLabel("Sort:"))
        self._sort_label = QLabel("★ ↑")
        self._sort_label.setStyleSheet("color: #aaa; font-style: italic;")
        self._sort_label.setToolTip("テーブルヘッダをクリックしてソートを変えるとここに反映されます")
        export_row.addWidget(self._sort_label)

        self._export_info_label = QLabel("")
        export_row.addWidget(self._export_info_label)

        export_row.addStretch()

        self._btn_export = QPushButton("📤 Export")
        self._btn_export.setToolTip(
            "Content と Style の条件に従って bplist を出力します。\n"
            "フィルタ中の範囲が対象です。\n"
            "Split by ★ の場合は保存フォルダを選択してください。"
        )
        self._btn_export.clicked.connect(self._on_export)
        self._apply_export_button_theme()
        export_row.addWidget(self._btn_export)

        self._btn_add_to_batch = QPushButton("➕ Add to Batch")
        _register_secondary_buttons(self._btn_add_to_batch)
        self._btn_add_to_batch.setToolTip(
            "フィルタ中のマップを Batch Export キューに追加します。\n"
            "Content / Style の設定が反映されます。"
        )
        self._btn_add_to_batch.clicked.connect(self._add_to_batch)
        export_row.addWidget(self._btn_add_to_batch)

        root.addWidget(export_group)

        # ─ テーブル ─────────────────────────────────────────────────
        self._table_stack = QStackedWidget(self)
        self._snapshot_table = self._create_playlist_table()
        self._maps_table = self._create_playlist_table()
        self._table_stack.addWidget(self._snapshot_table)
        self._table_stack.addWidget(self._maps_table)
        self._table = self._snapshot_table
        self._default_numeric_delegate = QStyledItemDelegate(self)
        self._maps_rate_delegate = _PercentageBarDelegate(self, max_value=100.0, gradient_min=0.0)
        self._apply_row_height(refresh_table=False)

        root.addWidget(self._table_stack, 1)

        self._selection_status_row = QWidget()
        self._selection_status_row.setStyleSheet("background: transparent;")
        _selection_status_layout = QHBoxLayout(self._selection_status_row)
        _selection_status_layout.setContentsMargins(4, 0, 0, 0)
        _selection_status_layout.setSpacing(0)
        self._selection_status_label = QLabel("0 rows selected")
        self._selection_status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._selection_status_label.setMinimumHeight(18)
        _selection_status_layout.addWidget(self._selection_status_label)
        self._btn_download_selected = QPushButton("")
        self._btn_download_selected.setIcon(QIcon(str(RESOURCES_DIR / "onclick_download.png")))
        self._btn_download_selected.setIconSize(QSize(18, 18))
        self._btn_download_selected.setFixedWidth(34)
        self._btn_download_selected.setToolTip("選択中の譜面をまとめてダウンロードします")
        self._btn_download_selected.setEnabled(False)
        self._btn_download_selected.clicked.connect(self._download_selected_entries)
        _register_secondary_buttons(self._btn_download_selected)
        _selection_status_layout.addSpacing(8)
        _selection_status_layout.addWidget(self._btn_download_selected)
        self._beatsaver_cache_status_label = QLabel("BeatSaver cache: idle")
        self._beatsaver_cache_status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._beatsaver_cache_status_label.setMinimumHeight(18)
        self._beatsaver_cache_status_label.setStyleSheet("color: #aaa;")
        _selection_status_layout.addSpacing(12)
        _selection_status_layout.addWidget(self._beatsaver_cache_status_label)
        _selection_status_layout.addStretch()

        self._btn_row_height_up = QPushButton("▲")
        self._btn_row_height_up.setToolTip("行の高さを大きくする")
        self._btn_row_height_up.setFixedWidth(28)
        self._btn_row_height_up.clicked.connect(self._on_row_height_up)
        self._btn_row_height_dn = QPushButton("▼")
        self._btn_row_height_dn.setToolTip("行の高さを小さくする")
        self._btn_row_height_dn.setFixedWidth(28)
        self._btn_row_height_dn.clicked.connect(self._on_row_height_dn)

        self._btn_scroll_top = QPushButton("↑")
        self._btn_scroll_top.setToolTip("一覧を一番上までスクロール")
        self._btn_scroll_top.clicked.connect(self._scroll_table_to_top)
        self._btn_scroll_bottom = QPushButton("↓")
        self._btn_scroll_bottom.setToolTip("一覧を一番下までスクロール")
        self._btn_scroll_bottom.clicked.connect(self._scroll_table_to_bottom)
        _register_secondary_buttons(
            self._btn_row_height_up,
            self._btn_row_height_dn,
            self._btn_scroll_top,
            self._btn_scroll_bottom,
        )
        self._btn_row_height_up.setFixedHeight(20)
        self._btn_row_height_dn.setFixedHeight(20)
        self._btn_scroll_top.setFixedHeight(20)
        self._btn_scroll_bottom.setFixedHeight(20)
        self._btn_row_height_up.setStyleSheet("QPushButton { padding: 0px; }")
        self._btn_row_height_dn.setStyleSheet("QPushButton { padding: 0px; }")
        self._btn_scroll_top.setFixedWidth(28)
        self._btn_scroll_bottom.setFixedWidth(28)
        self._btn_scroll_top.setStyleSheet("QPushButton { padding: 0px; }")
        self._btn_scroll_bottom.setStyleSheet("QPushButton { padding: 0px; }")
        _selection_status_layout.addWidget(self._btn_row_height_up)
        _selection_status_layout.addSpacing(4)
        _selection_status_layout.addWidget(self._btn_row_height_dn)
        _selection_status_layout.addSpacing(8)
        _selection_status_layout.addWidget(self._btn_scroll_top)
        _selection_status_layout.addSpacing(4)
        _selection_status_layout.addWidget(self._btn_scroll_bottom)
        root.addWidget(self._selection_status_row, 0)

        self.statusBar().hide()
        self._update_selection_status()

        # ─ Right panel: Batch Export ────────────────────────────────────
        _right_w = QWidget()
        _right_w.setMinimumWidth(180)
        _rl = QVBoxLayout(_right_w)
        _rl.setSpacing(4)
        _rl.setContentsMargins(4, 4, 4, 4)
        self.__cols.addWidget(_right_w)

        # 右パネル内を縦スプリッタで分割 (上: Preview / 中: Batch Queue / 下: Quick Presets)
        _right_splitter = QSplitter(Qt.Orientation.Vertical)
        _right_splitter.setChildrenCollapsible(False)
        _rl.addWidget(_right_splitter)

        self._preview_pane = QWidget()
        self._preview_pane.setObjectName("previewPane")
        _preview_layout = QVBoxLayout(self._preview_pane)
        _preview_layout.setSpacing(6)
        _preview_layout.setContentsMargins(6, 6, 6, 6)
        _right_splitter.addWidget(self._preview_pane)

        self._preview_title_widget = QWidget()
        _preview_title_row = QHBoxLayout(self._preview_title_widget)
        _preview_title_row.setSpacing(6)
        _preview_title_row.setContentsMargins(0, 0, 0, 0)
        _preview_layout.addWidget(self._preview_title_widget, 0)

        self._preview_title_label = QLabel("No map selected")
        self._preview_title_label.setWordWrap(False)
        self._preview_title_label.setStyleSheet("font-weight: 600;")
        _preview_title_row.addWidget(self._preview_title_label, 1)

        self._preview_translate_button = QPushButton("Translate")
        self._preview_translate_button.setVisible(True)
        self._preview_translate_button.setEnabled(False)
        self._preview_translate_button.setToolTip("Translate BeatSaver description with Google")
        self._preview_translate_button.clicked.connect(self._translate_preview_description)
        _register_secondary_buttons(self._preview_translate_button)
        _preview_title_row.addWidget(self._preview_translate_button, 0)

        self._preview_bsr_button = QPushButton("")
        self._preview_bsr_button.setVisible(False)
        self._preview_bsr_button.setToolTip("Copy BSR")
        self._preview_bsr_button.setProperty("copy_text", "")
        self._preview_bsr_button.clicked.connect(self._copy_preview_bsr)
        _register_secondary_buttons(self._preview_bsr_button)
        _preview_title_row.addWidget(self._preview_bsr_button, 0)

        _preview_content = QWidget()
        _preview_content_layout = QHBoxLayout(_preview_content)
        _preview_content_layout.setSpacing(6)
        _preview_content_layout.setContentsMargins(0, 0, 0, 0)

        _preview_media_col = QWidget()
        _preview_media_layout = QVBoxLayout(_preview_media_col)
        _preview_media_layout.setSpacing(6)
        _preview_media_layout.setContentsMargins(0, 0, 0, 0)
        _preview_content_layout.addWidget(_preview_media_col, 0)

        self._preview_image = QLabel("(no cover)")
        self._preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_image.setFixedSize(160, 160)
        self._preview_image.setStyleSheet("border: 1px solid #555; background: #1a1a1a;")
        _preview_media_layout.addWidget(self._preview_image, 0, Qt.AlignmentFlag.AlignTop)

        _preview_link_rows = QVBoxLayout()
        _preview_link_rows.setSpacing(2)
        _preview_link_rows.setContentsMargins(0, 0, 0, 0)
        _preview_media_layout.addLayout(_preview_link_rows)

        # ── 1行目: BeatSaver / OneClickDownload ──
        _preview_link_row1 = QHBoxLayout()
        _preview_link_row1.setSpacing(4)
        _preview_link_row1.setContentsMargins(0, 0, 0, 0)
        _preview_link_rows.addLayout(_preview_link_row1)

        # ── 2行目: BeatLeader / Replay / Global#1 / Local#1 ──
        _preview_link_row2 = QHBoxLayout()
        _preview_link_row2.setSpacing(4)
        _preview_link_row2.setContentsMargins(0, 0, 0, 0)
        _preview_link_rows.addLayout(_preview_link_row2)

        self._preview_text_col = QWidget()
        _preview_text_layout = QVBoxLayout(self._preview_text_col)
        _preview_text_layout.setSpacing(4)
        _preview_text_layout.setContentsMargins(0, 0, 0, 0)
        _preview_content_layout.addWidget(self._preview_text_col, 1)

        self._preview_meta_text = QTextBrowser()
        self._preview_meta_text.setReadOnly(True)
        self._preview_meta_text.setOpenExternalLinks(True)
        self._preview_meta_text.setOpenLinks(True)
        self._preview_meta_text.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._preview_meta_text.customContextMenuRequested.connect(self._show_preview_meta_context_menu)
        self._preview_meta_text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._preview_meta_text.setMinimumHeight(140)
        self._apply_preview_meta_frame_theme()
        _preview_text_layout.addWidget(self._preview_meta_text, 1)

        self._btn_preview_open = QPushButton("")
        self._btn_preview_open.setEnabled(False)
        self._btn_preview_open.setToolTip("Open on BeatSaver")
        self._btn_preview_open.setIcon(QIcon(str(RESOURCES_DIR / "beatsaver_logo.png")))
        self._btn_preview_open.setIconSize(QSize(18, 18))
        self._btn_preview_open.setFixedWidth(34)
        self._btn_preview_open.clicked.connect(self._open_selected_preview_url)
        _register_secondary_buttons(self._btn_preview_open)
        _preview_link_row1.addWidget(self._btn_preview_open)

        self._btn_preview_download = QPushButton("")
        self._btn_preview_download.setEnabled(False)
        self._btn_preview_download.setToolTip("OneClickDownload")
        self._btn_preview_download.setIcon(QIcon(str(RESOURCES_DIR / "onclick_download.png")))
        self._btn_preview_download.setIconSize(QSize(18, 18))
        self._btn_preview_download.setFixedWidth(34)
        self._btn_preview_download.clicked.connect(self._download_selected_preview_entry)
        _register_secondary_buttons(self._btn_preview_download)
        _preview_link_row1.addWidget(self._btn_preview_download)

        self._btn_preview_bl = QPushButton("")
        self._btn_preview_bl.setEnabled(False)
        self._btn_preview_bl.setToolTip("Open on BeatLeader")
        self._btn_preview_bl.setIcon(QIcon(str(RESOURCES_DIR / "beatleader_logo.webp")))
        self._btn_preview_bl.setIconSize(QSize(18, 18))
        self._btn_preview_bl.setFixedWidth(34)
        self._btn_preview_bl.clicked.connect(self._open_selected_preview_url)
        _register_secondary_buttons(self._btn_preview_bl)
        _preview_link_row2.addWidget(self._btn_preview_bl)

        self._btn_preview_replay = QPushButton("")
        self._btn_preview_replay.setIcon(QIcon(str(RESOURCES_DIR / "replay_btn.png")))
        self._btn_preview_replay.setIconSize(QSize(18, 18))
        self._btn_preview_replay.setEnabled(False)
        self._btn_preview_replay.setToolTip("Open replay")
        self._btn_preview_replay.setFixedWidth(34)
        self._btn_preview_replay.clicked.connect(self._open_selected_preview_url)
        _register_secondary_buttons(self._btn_preview_replay)
        _preview_link_row2.addWidget(self._btn_preview_replay)

        self._btn_preview_global1_replay = QPushButton("")
        self._btn_preview_global1_replay.setIcon(QIcon(str(RESOURCES_DIR / "global#1_replay_btn.png")))
        self._btn_preview_global1_replay.setIconSize(QSize(18, 18))
        self._btn_preview_global1_replay.setEnabled(False)
        self._btn_preview_global1_replay.setToolTip("Global #1 Replay on BeatLeader")
        self._btn_preview_global1_replay.setFixedWidth(34)
        self._btn_preview_global1_replay.clicked.connect(self._open_selected_preview_url)
        _register_secondary_buttons(self._btn_preview_global1_replay)
        _preview_link_row2.addWidget(self._btn_preview_global1_replay)

        self._btn_preview_local1_replay = QPushButton("")
        self._btn_preview_local1_replay.setIcon(QIcon(str(RESOURCES_DIR / "local#1_replay_btn.png")))
        self._btn_preview_local1_replay.setIconSize(QSize(18, 18))
        self._btn_preview_local1_replay.setEnabled(False)
        self._btn_preview_local1_replay.setToolTip("Local #1 Replay on BeatLeader")
        self._btn_preview_local1_replay.setFixedWidth(34)
        self._btn_preview_local1_replay.clicked.connect(self._open_selected_preview_url)
        _register_secondary_buttons(self._btn_preview_local1_replay)
        _preview_link_row2.addWidget(self._btn_preview_local1_replay)

        _preview_media_layout.addStretch(1)
        _preview_layout.addWidget(_preview_content, 1)

        # ── 上ペイン: Batch Export ──
        _top_pane = QWidget()
        _top_layout = QVBoxLayout(_top_pane)
        _top_layout.setSpacing(6)
        _top_layout.setContentsMargins(0, 0, 0, 0)
        _right_splitter.addWidget(_top_pane)

        _batch_title_row = QHBoxLayout()
        _batch_title = QLabel("Batch Export")
        _batch_title.setStyleSheet("font-weight: bold; font-size: 13px;")
        _batch_title_row.addWidget(_batch_title)
        _batch_title_row.addStretch()
        self._btn_bq_load = QPushButton("⏴ Load")
        self._btn_bq_load.setToolTip("選択中の1件から Source / Filter / Export Style を復元")
        self._btn_bq_load.setEnabled(False)
        self._btn_bq_load.clicked.connect(self._batch_restore_selected)
        _register_secondary_buttons(self._btn_bq_load)
        _batch_title_row.addWidget(self._btn_bq_load)
        self._btn_preview_cover = QPushButton("🖼️ Playlist Covers")
        _register_secondary_buttons(self._btn_preview_cover)
        self._btn_preview_cover.setToolTip("出力フォルダを選んで .bplist のカバー画像を一覧表示します")
        self._btn_preview_cover.clicked.connect(self._show_cover_preview)
        _batch_title_row.addWidget(self._btn_preview_cover)
        _top_layout.addLayout(_batch_title_row)

        self._batch_queue_list = QListWidget()
        self._batch_queue_list.setAlternatingRowColors(True)
        self._batch_queue_list.setWordWrap(True)
        self._batch_queue_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._batch_queue_list.setToolTip("チェックした項目のみ出力されます（行選択でRemove対象）")
        _top_layout.addWidget(self._batch_queue_list, 1)

        _queue_btn_row = QHBoxLayout()
        _btn_bq_all = QPushButton("All")
        _btn_bq_all.setToolTip("すべてを有効化")
        _btn_bq_all.clicked.connect(lambda: self._batch_set_all_enabled(True))
        _queue_btn_row.addWidget(_btn_bq_all)
        _btn_bq_none = QPushButton("None")
        _btn_bq_none.setToolTip("すべてを無効化")
        _btn_bq_none.clicked.connect(lambda: self._batch_set_all_enabled(False))
        _queue_btn_row.addWidget(_btn_bq_none)
        self._batch_count_label = QLabel("0 items")
        _queue_btn_row.addWidget(self._batch_count_label)
        _queue_btn_row.addStretch()
        self._btn_bq_remove = QPushButton("Remove")
        self._btn_bq_remove.setToolTip("選択行を削除")
        self._btn_bq_remove.setEnabled(False)
        self._btn_bq_remove.clicked.connect(self._batch_remove_selected)
        _queue_btn_row.addWidget(self._btn_bq_remove)
        _btn_bq_clear = QPushButton("Clear")
        _btn_bq_clear.setToolTip("すべてを削除")
        _btn_bq_clear.clicked.connect(self._batch_clear)
        _queue_btn_row.addWidget(_btn_bq_clear)
        _register_secondary_buttons(_btn_bq_all, _btn_bq_none, self._btn_bq_remove, _btn_bq_clear)
        _set_nonshrinking_button_width(_btn_bq_all, _btn_bq_none, self._btn_bq_load, self._btn_bq_remove, _btn_bq_clear)
        _top_layout.addLayout(_queue_btn_row)

        _export_all_btn_row = QHBoxLayout()
        self._btn_batch_export_all = QPushButton("📤 Export All")
        self._btn_batch_export_all.clicked.connect(self._batch_export_all)
        self._btn_batch_export_all.setFixedHeight(26)
        self._btn_batch_export_all.setStyleSheet(
            "QPushButton { background-color: #1a6b3a; color: white; font-weight: bold; font-size: 12px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #1e8046; }"
            "QPushButton:disabled { background-color: #444; color: #888; }"
        )
        _export_all_btn_row.addWidget(self._btn_batch_export_all)
        _top_layout.addLayout(_export_all_btn_row)

        # ── 下ペイン: Quick Presets ──
        _bot_pane = QWidget()
        _bot_layout = QVBoxLayout(_bot_pane)
        _bot_layout.setSpacing(6)
        _bot_layout.setContentsMargins(0, 0, 0, 0)
        _right_splitter.addWidget(_bot_pane)

        _bot_layout.addWidget(QLabel("Quick Presets:"))

        self._preset_list_w = _PresetListWidget()
        self._preset_list_w.setAlternatingRowColors(True)
        for _p in _BATCH_PRESETS:
            _pi = QListWidgetItem(_p.label)
            _pi.setFlags(_pi.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            _pi.setCheckState(Qt.CheckState.Unchecked)
            _pi.setData(Qt.ItemDataRole.UserRole, _p)
            self._preset_list_w.addItem(_pi)
        _bot_layout.addWidget(self._preset_list_w, 1)

        _preset_btn_row = QHBoxLayout()
        _btn_pa = QPushButton("All")
        _btn_pa.clicked.connect(
            lambda: [self._preset_list_w.item(i).setCheckState(Qt.CheckState.Checked)
                     for i in range(self._preset_list_w.count())]
        )
        _btn_pn = QPushButton("None")
        _btn_pn.clicked.connect(
            lambda: [self._preset_list_w.item(i).setCheckState(Qt.CheckState.Unchecked)
                     for i in range(self._preset_list_w.count())]
        )
        _preset_btn_row.addWidget(_btn_pa)
        _preset_btn_row.addWidget(_btn_pn)
        _preset_btn_row.addStretch()
        self._btn_add_presets = QPushButton("➕ Add to Batch")
        _register_secondary_buttons(_btn_pa, _btn_pn, self._btn_add_presets)
        _set_nonshrinking_button_width(_btn_pa, _btn_pn, self._btn_add_presets)
        self._btn_add_presets.clicked.connect(self._batch_add_presets)
        _preset_btn_row.addWidget(self._btn_add_presets)
        _bot_layout.addLayout(_preset_btn_row)

        self._btn_quick_export = QPushButton("📤 Quick Export")
        self._btn_quick_export.clicked.connect(self._quick_export_presets)
        self._btn_quick_export.setFixedHeight(26)
        self._btn_quick_export.setStyleSheet(
            "QPushButton { background-color: #1a4a6b; color: white; font-weight: bold; font-size: 12px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #1e5a80; }"
            "QPushButton:disabled { background-color: #444; color: #888; }"
        )
        _bot_layout.addWidget(self._btn_quick_export)

        _right_w.setMinimumWidth(max(
            180,
            _batch_title.sizeHint().width() + self._btn_bq_load.minimumWidth() + self._btn_preview_cover.sizeHint().width() + 64,
            self._batch_count_label.sizeHint().width()
            + _btn_bq_all.minimumWidth()
            + _btn_bq_none.minimumWidth()
            + self._btn_bq_remove.minimumWidth()
            + _btn_bq_clear.minimumWidth()
            + 72,
            _btn_pa.minimumWidth() + _btn_pn.minimumWidth() + self._btn_add_presets.minimumWidth() + 56,
            self._btn_batch_export_all.sizeHint().width() + 24,
            self._btn_quick_export.sizeHint().width() + 24,
        ))

        _right_splitter.setSizes([280, 280, 220])

        # ── バッチ状態 ──
        self._export_dir: str = load_playlist_export_dir()
        self._batch_configs: List[_BatchConfig] = load_playlist_batch_configs()
        self._export_sigs = _LoadSignals()
        self._export_sigs.finished.connect(self._on_export_finished)
        self._export_sigs.error.connect(self._on_export_error)
        self._apply_secondary_button_theme()
        self._export_sigs.progress.connect(self._on_export_progress)
        self._batch_progress_dlg: Optional[QProgressDialog] = None
        self._batch_queue_list.itemChanged.connect(self._on_batch_item_changed)
        self._batch_queue_list.itemSelectionChanged.connect(self._update_batch_queue_actions)
        self._batch_refresh_queue()
        self._initial_source_tab = initial_source_tab
        self._initial_restore_started = False

        # スプリッタ初期サイズ: 左を広く、右パネルを 252px
        self._splitter.setSizes([940, 350])
        self._load_window_state()
        self._update_filter_export_ui()
        self._update_table_visual_mode()
        self._clear_preview()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if self._initial_restore_started:
            return
        self._initial_restore_started = True
        QTimer.singleShot(0, self._finish_initial_restore)

    def _finish_initial_restore(self) -> None:
        self.select_source_tab(self._initial_source_tab)
        self._restore_saved_snapshot_state()
        if self._is_maps_tab():
            self._schedule_deferred_maps_restore()
        self._update_filter_export_ui()
        self._update_table_visual_mode()
        self._clear_preview()

    def _schedule_deferred_maps_restore(self) -> None:
        if self._deferred_maps_restore_scheduled or not self._restored_maps_state:
            return
        self._deferred_maps_restore_scheduled = True
        QTimer.singleShot(1, self._restore_saved_maps_state_deferred)

    def _restore_saved_maps_state_deferred(self) -> None:
        self._deferred_maps_restore_scheduled = False
        self._restore_saved_maps_state()

    def _create_playlist_table(self) -> QTableWidget:
        table = _PlaylistTableWidget(0, _COL_COUNT, self)
        table.setHorizontalHeaderLabels(_COL_LABELS)
        table.setItemDelegate(_NoFocusItemDelegate(table))
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setSortingEnabled(True)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setStyleSheet(self._playlist_table_stylesheet())
        table.verticalHeader().setDefaultSectionSize(self._row_height)
        table.verticalHeader().setVisible(False)

        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(True)
        hdr.setSectionsMovable(True)
        hdr.sectionClicked.connect(self._on_header_clicked)
        hdr.setStyle(_SwapSortArrowStyle())

        table.setColumnWidth(_COL_STATUS, 52)
        table.setColumnWidth(_COL_COVER, self._thumbnail_edge_size())
        table.setColumnWidth(_COL_SONG, 260)
        table.setColumnWidth(_COL_ONECLICK, 46)
        table.setColumnWidth(_COL_DELETE, 46)
        table.setColumnWidth(_COL_SOURCE_DATE, 112)
        table.setColumnWidth(_COL_DURATION, 40)
        table.setColumnWidth(_COL_PLAY_TIME, 110)
        table.setColumnWidth(_COL_DIFF, 26)
        table.setColumnWidth(_COL_MODE, 42)
        table.setColumnWidth(_COL_ACC_CAT, 60)
        table.setColumnWidth(_COL_SERVICE, 48)
        table.setColumnWidth(_COL_PLAYER_RANK, 60)
        table.setColumnWidth(_COL_STARS, 40)
        table.setColumnWidth(_COL_PLAYER_ACC, 60)
        table.setColumnWidth(_COL_PLAYER_PP, 45)
        table.setColumnWidth(_COL_BS_RATE, 60)
        table.setColumnWidth(_COL_BS_UPVOTES, 50)
        table.setColumnWidth(_COL_BS_DOWNVOTES, 50)
        table.setColumnWidth(_COL_FC, 32)
        table.setColumnWidth(_COL_MOD, 45)
        table.setColumnWidth(_COL_AUTHOR, 140)
        table.setColumnWidth(_COL_MAPPER, 120)
        table.setColumnWidth(_COL_BL_PLAYS, 78)
        table.setColumnWidth(_COL_BL_ATTEMPTS, 88)
        table.itemSelectionChanged.connect(self._update_selection_status)
        table.itemSelectionChanged.connect(self._update_preview_from_selection)
        table.itemSelectionChanged.connect(table.viewport().update)
        table.verticalScrollBar().valueChanged.connect(lambda _value, current_table=table: self._hydrate_visible_row_widgets(current_table))
        return table

    def _is_maps_tab(self, index: Optional[int] = None) -> bool:
        tab_index = self._source_tabs.currentIndex() if index is None else index
        return tab_index == self._source_tab_maps_idx

    def _sync_active_table_state(self) -> None:
        if self._table is self._maps_table:
            self._maps_all_entries = self._all_entries
            self._maps_filtered = self._filtered
        else:
            self._snapshot_all_entries = self._all_entries
            self._snapshot_filtered = self._filtered

    def _activate_table_for_tab(self, index: Optional[int] = None) -> None:
        if self._is_maps_tab(index):
            self._table = self._maps_table
            self._all_entries = self._maps_all_entries
            self._filtered = self._maps_filtered
            self._table_stack.setCurrentWidget(self._maps_table)
            self._last_load_label.setText(self._maps_last_load_text)
        else:
            self._table = self._snapshot_table
            self._all_entries = self._snapshot_all_entries
            self._filtered = self._snapshot_filtered
            self._table_stack.setCurrentWidget(self._snapshot_table)
            self._last_load_label.setText(self._snapshot_last_load_text)
        self._count_label.setText(f"{len(self._filtered):,} maps")
        self._update_sort_label()
        self._update_selection_status()
        self._update_beatsaver_cache_status()
        self._update_preview_from_selection()

    def _current_tab_sort_mode(self) -> str:
        return self._maps_sort_mode if self._is_maps_tab() else self._snapshot_sort_mode

    def _set_current_tab_sort_mode(self, sort_mode: str) -> None:
        if self._is_maps_tab():
            self._maps_sort_mode = sort_mode
        else:
            self._snapshot_sort_mode = sort_mode

    def _apply_saved_sort_for_current_tab(self) -> None:
        sort_col, sort_order = _sort_indicator_from_mode(self._current_tab_sort_mode())
        self._table.horizontalHeader().setSortIndicator(sort_col, sort_order)
        self._update_sort_label()

    def _reset_beatsaver_cache_status(self) -> None:
        self._beatsaver_meta_pending_hashes.clear()
        self._beatsaver_meta_pending_set.clear()
        self._beatsaver_meta_pending_seed_map.clear()
        self._beatsaver_meta_inflight_hashes.clear()
        self._beatsaver_meta_total_hashes.clear()
        self._beatsaver_meta_completed_hashes.clear()
        self._beatsaver_meta_active_hash = ""
        self._update_beatsaver_cache_status()

    def _update_beatsaver_cache_status(self, error_text: str = "") -> None:
        total = len(self._beatsaver_meta_total_hashes)
        done = len(self._beatsaver_meta_completed_hashes)
        inflight = len(self._beatsaver_meta_inflight_hashes)
        pending = len(self._beatsaver_meta_pending_set)
        if error_text:
            self._beatsaver_cache_status_label.setText(f"BeatSaver cache: {error_text}")
            return
        if total <= 0:
            self._beatsaver_cache_status_label.setText("BeatSaver cache: idle")
            return
        if inflight > 0 or pending > 0:
            active_text = f", active hash: {self._beatsaver_meta_active_hash[:12]}" if self._beatsaver_meta_active_hash else ""
            self._beatsaver_cache_status_label.setText(
                f"BeatSaver cache: {done}/{total} done, {inflight} active, {pending} queued{active_text}"
            )
            return
        self._beatsaver_cache_status_label.setText(f"BeatSaver cache: {done}/{total} done")

    def _queue_beatsaver_cache_entries(
        self,
        entries: List[MapEntry],
        *,
        prioritize: bool = False,
    ) -> None:
        missing_hashes, seed_map = _collect_beatsaver_cache_targets(entries)
        cache = load_beatsaver_meta_cache()
        resolved_hashes: set[str] = set()
        for entry in entries:
            song_hash = (entry.song_hash or "").upper()
            if not song_hash:
                continue
            cache_entry = cache.get(song_hash)
            if _has_full_beatsaver_meta(cache_entry) or bool(entry.beatsaver_cover_url or entry.beatsaver_description):
                resolved_hashes.add(song_hash)
        added = False
        queue_hashes = list(missing_hashes)
        for song_hash in seed_map:
            if song_hash not in queue_hashes:
                queue_hashes.append(song_hash)
        for song_hash in queue_hashes:
            self._beatsaver_meta_total_hashes.add(song_hash)
            if song_hash in resolved_hashes:
                self._beatsaver_meta_completed_hashes.add(song_hash)
        for song_hash in queue_hashes:
            if song_hash in resolved_hashes:
                continue
            if song_hash in self._beatsaver_meta_pending_set or song_hash in self._beatsaver_meta_inflight_hashes:
                continue
            if prioritize:
                self._beatsaver_meta_pending_hashes.insert(0, song_hash)
            else:
                self._beatsaver_meta_pending_hashes.append(song_hash)
            self._beatsaver_meta_pending_set.add(song_hash)
            added = True
        for song_hash, key in seed_map.items():
            if song_hash not in self._beatsaver_meta_pending_seed_map:
                self._beatsaver_meta_pending_seed_map[song_hash] = key
        if added or seed_map:
            self._update_beatsaver_cache_status()
            self._start_next_beatsaver_meta_batch()

    def _visible_first_entries(self) -> List[MapEntry]:
        if not self._filtered or self._table.rowCount() <= 0:
            return list(self._filtered)
        first_row = self._table.rowAt(0)
        if first_row < 0:
            first_row = 0
        last_row = self._table.rowAt(max(0, self._table.viewport().height() - 1))
        if last_row < first_row:
            last_row = min(self._table.rowCount() - 1, first_row + 12)

        ordered: List[MapEntry] = []
        seen_ids: set[int] = set()
        for row in range(first_row, min(last_row + 1, self._table.rowCount())):
            item = self._table.item(row, _COL_SONG)
            if item is None:
                continue
            entry = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(entry, MapEntry):
                ordered.append(entry)
                seen_ids.add(id(entry))
        for entry in self._filtered:
            if id(entry) in seen_ids:
                continue
            ordered.append(entry)
        return ordered

    def _queue_beatsaver_cache_for_current_entries(self) -> None:
        if not self._filtered:
            return
        self._queue_beatsaver_cache_entries(self._visible_first_entries())

    def _start_next_beatsaver_meta_batch(self) -> None:
        if self._beatsaver_meta_inflight_hashes or not self._beatsaver_meta_pending_hashes:
            self._update_beatsaver_cache_status()
            return
        batch_hashes: List[str] = []
        while self._beatsaver_meta_pending_hashes and len(batch_hashes) < 1:
            song_hash = self._beatsaver_meta_pending_hashes.pop(0)
            self._beatsaver_meta_pending_set.discard(song_hash)
            if song_hash in self._beatsaver_meta_inflight_hashes:
                continue
            batch_hashes.append(song_hash)
        if not batch_hashes:
            self._update_beatsaver_cache_status()
            return
        self._beatsaver_meta_inflight_hashes = set(batch_hashes)
        self._beatsaver_meta_active_hash = batch_hashes[0]
        self._update_beatsaver_cache_status()
        seed_map = {
            song_hash: self._beatsaver_meta_pending_seed_map.get(song_hash, "")
            for song_hash in batch_hashes
            if self._beatsaver_meta_pending_seed_map.get(song_hash)
        }

        def _task() -> None:
            try:
                update_beatsaver_meta_cache(batch_hashes, seed_map=seed_map)
                self._beatsaver_meta_signals.finished.emit(batch_hashes)
            except Exception as exc:  # noqa: BLE001
                self._beatsaver_meta_signals.error.emit(str(exc))

        threading.Thread(target=_task, daemon=True).start()

    def _on_beatsaver_meta_batch_finished(self, hashes: List[str]) -> None:
        cache = load_beatsaver_meta_cache()
        changed_hashes = {str(song_hash).upper() for song_hash in hashes}
        self._beatsaver_meta_inflight_hashes.clear()
        self._beatsaver_meta_active_hash = ""
        self._beatsaver_meta_completed_hashes.update(changed_hashes)
        for dataset in (self._snapshot_all_entries, self._maps_all_entries):
            for entry in dataset:
                song_hash = (entry.song_hash or "").upper()
                if song_hash in changed_hashes:
                    _apply_beatsaver_meta(entry, cache.get(song_hash))
        for song_hash in changed_hashes:
            self._beatsaver_meta_pending_seed_map.pop(song_hash, None)
        selected_entry = self._selected_entry()
        self._refresh_rows_for_hashes(changed_hashes)
        self._update_beatsaver_cache_status()
        if selected_entry is not None and (selected_entry.song_hash or "").upper() in changed_hashes:
            self._update_preview_from_selection()
        self._start_next_beatsaver_meta_batch()

    def _on_beatsaver_meta_batch_error(self, _msg: str) -> None:
        self._beatsaver_meta_inflight_hashes.clear()
        self._beatsaver_meta_active_hash = ""
        self._update_beatsaver_cache_status("retrying after error")
        self._start_next_beatsaver_meta_batch()

    def _save_window_state(self) -> None:
        try:
            payload: dict = {}
            if _PLAYLIST_WINDOW_PATH.exists():
                try:
                    payload = json.loads(_PLAYLIST_WINDOW_PATH.read_text(encoding="utf-8"))
                except Exception:
                    payload = {}
            payload["splitter_sizes"] = self._splitter.sizes()
            payload["window_width"] = self.width()
            payload["window_height"] = self.height()
            payload["row_height"] = self._row_height
            payload["last_load_text_snapshot"] = self._snapshot_last_load_text
            payload["last_load_text_maps"] = self._maps_last_load_text
            payload["highest_diff_only_snapshot"] = self._highest_diff_only_snapshot
            payload["highest_diff_only_maps"] = self._highest_diff_only_maps
            snapshot_state = self._build_list_state_payload(
                self._snapshot_all_entries,
                self._snapshot_source_key,
                self._snapshot_sort_mode,
                self._snapshot_last_load_text,
            )
            if snapshot_state is not None:
                payload["snapshot_state"] = snapshot_state
            else:
                payload.pop("snapshot_state", None)
            maps_state = self._build_list_state_payload(
                self._maps_all_entries,
                self._maps_source_key,
                self._maps_sort_mode,
                self._maps_last_load_text,
            )
            if maps_state is not None:
                payload["maps_state"] = maps_state
            else:
                payload.pop("maps_state", None)
            _PLAYLIST_WINDOW_PATH.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _load_window_state(self) -> None:
        if not _PLAYLIST_WINDOW_PATH.exists():
            return
        try:
            data = json.loads(_PLAYLIST_WINDOW_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        w = data.get("window_width")
        h = data.get("window_height")
        row_height = data.get("row_height")
        last_load_text_snapshot = data.get("last_load_text_snapshot")
        last_load_text_maps = data.get("last_load_text_maps")
        highest_diff_only_snapshot = data.get("highest_diff_only_snapshot")
        highest_diff_only_maps = data.get("highest_diff_only_maps")
        if isinstance(w, int) and isinstance(h, int) and w > 200 and h > 200:
            self.resize(w, h)
        sizes = data.get("splitter_sizes")
        if isinstance(sizes, list) and len(sizes) == 2:
            self._splitter.setSizes(sizes)
        if isinstance(row_height, int):
            self._row_height = max(18, min(row_height, 64))
            self._apply_row_height(refresh_table=False)
        if isinstance(last_load_text_snapshot, str) and last_load_text_snapshot.strip():
            self._snapshot_last_load_text = last_load_text_snapshot
        if isinstance(last_load_text_maps, str) and last_load_text_maps.strip():
            self._maps_last_load_text = last_load_text_maps
        if isinstance(highest_diff_only_snapshot, bool):
            self._highest_diff_only_snapshot = highest_diff_only_snapshot
        if isinstance(highest_diff_only_maps, bool):
            self._highest_diff_only_maps = highest_diff_only_maps
        snapshot_state = data.get("snapshot_state")
        if isinstance(snapshot_state, dict):
            self._restored_snapshot_state = snapshot_state
        maps_state = data.get("maps_state")
        if isinstance(maps_state, dict):
            self._restored_maps_state = maps_state

    def _highest_diff_only_default_for_tab(self, index: Optional[int] = None) -> bool:
        tab_index = self._source_tabs.currentIndex() if index is None else index
        return tab_index == self._source_tab_maps_idx

    def _highest_diff_only_value_for_tab(self, index: Optional[int] = None) -> bool:
        tab_index = self._source_tabs.currentIndex() if index is None else index
        return self._highest_diff_only_maps if tab_index == self._source_tab_maps_idx else self._highest_diff_only_snapshot

    def _set_highest_diff_only_for_tab(self, checked: bool, index: Optional[int] = None) -> None:
        tab_index = self._source_tabs.currentIndex() if index is None else index
        if tab_index == self._source_tab_maps_idx:
            self._highest_diff_only_maps = checked
        else:
            self._highest_diff_only_snapshot = checked

    def _apply_highest_diff_only_for_current_tab(self) -> None:
        checked = self._highest_diff_only_value_for_tab()
        self._cb_top_diff_only.blockSignals(True)
        self._cb_top_diff_only.setChecked(checked)
        self._cb_top_diff_only.blockSignals(False)

    def _on_top_diff_only_toggled(self, checked: bool) -> None:
        self._set_highest_diff_only_for_tab(checked)
        self._apply_filter()

    def _current_source_key(self) -> str:
        if self._rb_ss.isChecked():
            return "ss"
        if self._rb_bl.isChecked():
            return "bl"
        if self._rb_acc.isChecked():
            return "acc"
        if self._rb_acc_rl.isChecked():
            return "acc_rl"
        if self._rb_bs.isChecked():
            return "bs"
        if self._rb_open.isChecked():
            return "open"
        return "ss"

    def _sync_bs_dates_from_days(self, _value: int = 0) -> None:
        if self._bs_date_sync:
            return
        self._bs_date_sync = True
        try:
            to_date = self._bs_to_date.date() if self._bs_to_date.date().isValid() else QDate.currentDate()
            self._bs_to_date.setDate(to_date)
            self._bs_from_date.setDate(to_date.addDays(-(max(1, self._bs_days.value()) - 1)))
        finally:
            self._bs_date_sync = False

    def _sync_bs_days_from_dates(self, _date: QDate) -> None:
        if self._bs_date_sync:
            return
        self._bs_date_sync = True
        try:
            from_date = self._bs_from_date.date()
            to_date = self._bs_to_date.date()
            if self.sender() is self._bs_to_date and to_date == QDate.currentDate():
                from_date = to_date.addDays(-(max(1, self._bs_days.value()) - 1))
                self._bs_from_date.setDate(from_date)
            if from_date > to_date:
                if self.sender() is self._bs_from_date:
                    self._bs_to_date.setDate(from_date)
                    to_date = from_date
                else:
                    self._bs_from_date.setDate(to_date)
                    from_date = to_date
            self._bs_days.setValue(max(1, from_date.daysTo(to_date) + 1))
        finally:
            self._bs_date_sync = False

    def _set_bs_to_latest(self) -> None:
        self._bs_to_date.setDate(QDate.currentDate())
        self._sync_bs_dates_from_days()

    def _playlist_table_stylesheet(self) -> str:
        return table_stylesheet()

    def _source_tabs_stylesheet(self) -> str:
        if is_dark():
            return (
                "QTabWidget::pane { border: 0; margin: 0; padding: 0; }"
                "QTabBar::tab { background: #2b2b2b; color: #d8d8d8; border: 1px solid #4d4d4d;"
                " border-bottom: 0; border-top-left-radius: 4px; border-top-right-radius: 4px;"
                " padding: 6px 14px; margin-right: 2px; }"
                "QTabBar::tab:hover { background: #365c7c; color: #ffffff; }"
                "QTabBar::tab:selected { background: #1976d2; color: #ffffff; font-weight: 600; }"
            )
        return (
            "QTabWidget::pane { border: 0; margin: 0; padding: 0; }"
            "QTabBar::tab { background: #f1f5f9; color: #334155; border: 1px solid #cbd5e1;"
            " border-bottom: 0; border-top-left-radius: 4px; border-top-right-radius: 4px;"
            " padding: 6px 14px; margin-right: 2px; }"
            "QTabBar::tab:hover { background: #dbeafe; color: #1d4ed8; }"
            "QTabBar::tab:selected { background: #93c5fd; color: #0f172a; font-weight: 600; }"
        )

    def _set_bs_rating_value(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        if self._bs_rating_sync:
            return
        self._bs_rating_sync = True
        try:
            self._bs_min_rating.setValue(value)
            self._bs_rating_value_label.setValue(value)
            self._bs_filter_min_rating.setValue(value)
            self._bs_filter_rating_value_label.setValue(value)
        finally:
            self._bs_rating_sync = False
        if hasattr(self, "_search_edit"):
            self._apply_filter()

    def _set_bs_votes_value(self, value: int) -> None:
        value = max(0, min(1000, int(value)))
        if self._bs_votes_sync:
            return
        self._bs_votes_sync = True
        try:
            self._bs_min_votes.setValue(value)
            self._bs_votes_value_label.setValue(value)
            self._bs_filter_min_votes.setValue(value)
            self._bs_filter_votes_value_label.setValue(value)
        finally:
            self._bs_votes_sync = False
        if hasattr(self, "_search_edit"):
            self._apply_filter()

    def _apply_preview_menu_theme(self, menu: QMenu) -> None:
        if is_dark():
            menu.setStyleSheet(
                "QMenu { background: #2b2b2b; color: #f3f4f6; border: 1px solid #5f6368; }"
                "QMenu::item { background: #2b2b2b; color: #f3f4f6; padding: 6px 24px 6px 24px; }"
                "QMenu::item:selected { background: #365c7c; color: #ffffff; }"
                "QMenu::separator { height: 1px; background: #4b5563; margin: 4px 8px; }"
            )
            return
        menu.setStyleSheet(
            "QMenu { background: #ffffff; color: #111111; border: 1px solid #cfcfcf; }"
            "QMenu::item { background: #ffffff; color: #111111; padding: 6px 24px 6px 24px; }"
            "QMenu::item:selected { background: #dbeafe; color: #111111; }"
            "QMenu::separator { height: 1px; background: #dddddd; margin: 4px 8px; }"
        )

    def _apply_preview_meta_frame_theme(self) -> None:
        if is_dark():
            self._preview_pane.setStyleSheet(
                "#previewPane {"
                " border: 1px solid #5f6368;"
                " border-radius: 4px;"
                " background-color: #262626;"
                "}"
            )
            self._preview_text_col.setStyleSheet("QWidget { border: none; background: transparent; }")
            self._preview_meta_text.setStyleSheet(
                "QTextBrowser {"
                " border: none;"
                " background-color: transparent;"
                " padding: 0px;"
                "}"
            )
        else:
            self._preview_pane.setStyleSheet(
                "#previewPane {"
                " border: 1px solid #9fb5c7;"
                " border-radius: 4px;"
                " background-color: #ffffff;"
                "}"
            )
            self._preview_text_col.setStyleSheet("QWidget { border: none; background: transparent; }")
            self._preview_meta_text.setStyleSheet(
                "QTextBrowser {"
                " border: none;"
                " background-color: transparent;"
                " padding: 0px;"
                "}"
            )

    def _on_bs_source_rating_changed(self, value: int) -> None:
        self._set_bs_rating_value(value)

    def _on_bs_filter_rating_changed(self, value: int) -> None:
        self._set_bs_rating_value(value)

    def _on_bs_source_votes_changed(self, value: int) -> None:
        self._set_bs_votes_value(value)

    def _on_bs_filter_votes_changed(self, value: int) -> None:
        self._set_bs_votes_value(value)

    def _show_preview_meta_context_menu(self, position) -> None:
        menu = self._preview_meta_text.createStandardContextMenu()
        self._apply_preview_menu_theme(menu)
        selected_text = self._preview_meta_text.textCursor().selectedText().replace("\u2029", "\n").strip()
        if selected_text:
            menu.addSeparator()
            action = menu.addAction("Translate with Google")
            action.triggered.connect(lambda _checked=False, text=selected_text: self._translate_preview_text(text))
        menu.exec(self._preview_meta_text.mapToGlobal(position))

    def _translate_preview_text(self, text: str) -> None:
        selected_text = text.strip()
        if not selected_text:
            return
        QDesktopServices.openUrl(
            QUrl(f"https://translate.google.com/?sl=auto&tl=ja&text={quote(selected_text)}&op=translate")
        )

    def _build_list_state_payload(self, entries: List[MapEntry], source_key: str, sort_mode: str, last_load_text: str) -> Optional[dict]:
        if not entries:
            return None
        return {
            "source": source_key,
            "sort_mode": sort_mode,
            "last_load_text": last_load_text,
            "open_path": self._open_edit.text().strip(),
            "open_service": str(self._svc_combo.currentData() or "none"),
            "bs_query": self._bs_query_edit.text().strip(),
            "bs_max_maps": self._bs_max_maps.value(),
            "bs_days": self._bs_days.value(),
            "bs_from_date": self._bs_from_date.date().toString("yyyy-MM-dd"),
            "bs_to_date": self._bs_to_date.date().toString("yyyy-MM-dd"),
            "bs_min_rating": self._bs_min_rating.value(),
            "bs_min_votes": self._bs_min_votes.value(),
            "bs_unranked_only": self._cb_bs_unranked.isChecked(),
            "bs_exclude_ai": self._cb_bs_no_ai.isChecked(),
            "entries": [entry.to_dict() for entry in entries],
        }

    def _restore_saved_snapshot_state(self) -> None:
        state = self._restored_snapshot_state
        self._restored_snapshot_state = None
        if not state:
            return

        source = str(state.get("source") or "").strip().lower()
        sort_mode = str(state.get("sort_mode") or "status_desc").strip() or "status_desc"
        last_load_text = str(state.get("last_load_text") or "").strip()
        source_button = {
            "ss": self._rb_ss,
            "bl": self._rb_bl,
            "acc": self._rb_acc,
            "acc_rl": self._rb_acc_rl,
            "open": self._rb_open,
        }.get(source)
        if source:
            self._snapshot_source_key = source
        self._snapshot_sort_mode = sort_mode
        if last_load_text:
            self._snapshot_last_load_text = last_load_text
        if source_button is not None and not self._is_maps_tab():
            self._last_snapshot_source_button = source_button
            source_button.setChecked(True)

        self._open_edit.setText(str(state.get("open_path") or ""))
        open_service = str(state.get("open_service") or "none")
        open_index = self._svc_combo.findData(open_service)
        if open_index >= 0:
            self._svc_combo.setCurrentIndex(open_index)

        raw_entries = state.get("entries")
        if not isinstance(raw_entries, list):
            return
        restored_entries: List[MapEntry] = []
        for item in raw_entries:
            if not isinstance(item, dict):
                continue
            try:
                restored_entries.append(MapEntry.from_dict(item))
            except Exception:
                continue
        if not restored_entries:
            return

        self._snapshot_all_entries = _enrich_entries_with_beatsaver_cache(restored_entries)
        self._snapshot_filtered = list(self._snapshot_all_entries)
        self._snapshot_loaded_source_key = self._snapshot_source_key
        self._snapshot_loaded_steam_id = self._steam_id
        if not self._is_maps_tab():
            self._activate_table_for_tab(self._source_tab_snapshot_idx)
            self._apply_saved_sort_for_current_tab()
            self._apply_filter()

    def _restore_saved_maps_state(self) -> None:
        state = self._restored_maps_state
        self._restored_maps_state = None
        if not state:
            return

        source = str(state.get("source") or "").strip().lower()
        sort_mode = str(state.get("sort_mode") or "date_desc").strip() or "date_desc"
        last_load_text = str(state.get("last_load_text") or "").strip()
        source_buttons = {
            "ss": self._rb_ss,
            "bl": self._rb_bl,
            "acc": self._rb_acc,
            "acc_rl": self._rb_acc_rl,
            "bs": self._rb_bs,
            "open": self._rb_open,
        }
        source_button = source_buttons.get(source)
        if source:
            self._maps_source_key = source
        self._maps_sort_mode = sort_mode
        if last_load_text:
            self._maps_last_load_text = last_load_text
        if source_button is not None and self._is_maps_tab():
            source_button.setChecked(True)

        self._open_edit.setText(str(state.get("open_path") or ""))
        open_service = str(state.get("open_service") or "none")
        open_index = self._svc_combo.findData(open_service)
        if open_index >= 0:
            self._svc_combo.setCurrentIndex(open_index)

        self._bs_query_edit.setText(str(state.get("bs_query") or ""))
        self._bs_max_maps.setValue(int(state.get("bs_max_maps") or 1000))
        self._bs_days.setValue(int(state.get("bs_days") or 7))
        from_date = QDate.fromString(str(state.get("bs_from_date") or ""), "yyyy-MM-dd")
        to_date = QDate.fromString(str(state.get("bs_to_date") or ""), "yyyy-MM-dd")
        if to_date.isValid():
            self._bs_to_date.setDate(to_date)
        if from_date.isValid():
            self._bs_from_date.setDate(from_date)
        else:
            self._sync_bs_dates_from_days()
        self._set_bs_rating_value(int(state.get("bs_min_rating") or 50))
        self._set_bs_votes_value(int(state.get("bs_min_votes") or 0))
        self._cb_bs_unranked.setChecked(bool(state.get("bs_unranked_only", True)))
        self._cb_bs_no_ai.setChecked(bool(state.get("bs_exclude_ai", True)))

        raw_entries = state.get("entries")
        if not isinstance(raw_entries, list):
            return
        restored_entries: List[MapEntry] = []
        for item in raw_entries:
            if not isinstance(item, dict):
                continue
            try:
                restored_entries.append(MapEntry.from_dict(item))
            except Exception:
                continue
        if not restored_entries:
            return

        for entry in restored_entries:
            _apply_beatsaver_meta(entry, None)

        self._maps_all_entries = restored_entries
        self._maps_filtered = list(self._maps_all_entries)
        self._maps_loaded_source_key = self._maps_source_key
        self._maps_loaded_steam_id = self._steam_id
        if self._is_maps_tab():
            self._activate_table_for_tab(self._source_tab_maps_idx)
            self._apply_saved_sort_for_current_tab()
            self._apply_filter()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._save_window_state()
        super().closeEvent(event)

    def _apply_secondary_button_theme(self) -> None:
        style = _secondary_button_stylesheet()
        for button in self._secondary_buttons:
            button.setStyleSheet(style)

    def _apply_export_button_theme(self) -> None:
        if _is_windows_light_app_light():
            self._btn_export.setMinimumHeight(30)
            self._btn_export.setMinimumWidth(104)
            self._btn_export.setStyleSheet(
                "QPushButton { font-weight: bold; padding: 3px 14px; }"
            )
        else:
            self._btn_export.setMinimumHeight(26)
            self._btn_export.setMinimumWidth(0)
            self._btn_export.setStyleSheet(
                "QPushButton { font-weight: bold; padding: 2px 8px; }"
            )

    def apply_theme(self) -> None:
        """テーマ切替後に呼び出してテーブルスタイルと行色を更新する。"""
        self._apply_secondary_button_theme()
        self._apply_export_button_theme()
        self._source_tabs.setStyleSheet(self._source_tabs_stylesheet())
        self._snapshot_table.setStyleSheet(self._playlist_table_stylesheet())
        self._maps_table.setStyleSheet(self._playlist_table_stylesheet())
        self._apply_preview_meta_frame_theme()
        if self._all_entries:
            self._refresh_table(self._filtered)

    def can_reuse_filter_preset_source(self, source: str) -> bool:
        source_key = str(source or "").strip().lower()
        return (
            source_key == self._snapshot_loaded_source_key
            and bool(self._snapshot_all_entries)
            and self._snapshot_loaded_steam_id == self._steam_id
        )

    def apply_filter_preset(
        self,
        source: str,
        star_min: float = 0.0,
        star_max: float = 20.0,
        categories: Optional[List[str]] = None,
        show_cleared: bool = True,
        show_nf: bool = True,
        show_unplayed: bool = True,
        show_queued: bool = False,
        sort_mode: str = "status_desc",
    ) -> None:
        """Stats 画面からの遷移用。ソース・星範囲・カテゴリ・ソートをプリセットして Load する。

        source: "ss" | "bl" | "acc" | "acc_rl"
        categories: None = 全カテゴリ / ["true"/"standard"/"tech"] で絞り込み
        sort_mode: "status_desc" | "pp_high" | "ap_high" など
        """
        source_key = str(source or "").strip().lower()

        # ソースラジオボタンを切り替え（シグナルで _on_source_changed が呼ばれる）
        _rb_map = {"ss": self._rb_ss, "bl": self._rb_bl, "acc": self._rb_acc, "acc_rl": self._rb_acc_rl}
        rb = _rb_map.get(source_key)
        if rb is not None:
            rb.setChecked(True)

        # source 切替後の既定値で前回フィルタをクリアする
        self._reset_filters()

        # 星範囲を設定（シグナルを一時ブロックして二重フィルタを防ぐ）
        self._star_min.blockSignals(True)
        self._star_max.blockSignals(True)
        self._star_min.setValue(star_min)
        self._star_max.setValue(star_max)
        self._star_min.blockSignals(False)
        self._star_max.blockSignals(False)

        # カテゴリチェックボックスを設定
        for cat, cb in [
            ("true", self._cb_cat_true),
            ("standard", self._cb_cat_standard),
            ("tech", self._cb_cat_tech),
        ]:
            cb.blockSignals(True)
            cb.setChecked(categories is None or cat in categories)
            cb.blockSignals(False)

        # Status チェックを設定
        for checked, cb in [
            (show_cleared, self._cb_sts_cleared),
            (show_nf, self._cb_sts_nf),
            (show_unplayed, self._cb_sts_unplayed),
            (show_queued, self._cb_sts_queued),
        ]:
            cb.blockSignals(True)
            cb.setChecked(checked)
            cb.blockSignals(False)

        # Export スタイルを Split by ★ / Category に設定
        self._rb_exp_split.setChecked(True)

        # ソートインジケータを sort_mode に合わせて設定
        sort_col, sort_order = _sort_indicator_from_mode(sort_mode)
        self._table.horizontalHeader().setSortIndicator(sort_col, sort_order)
        self._update_sort_label()

        can_reuse_loaded_snapshot = self.can_reuse_filter_preset_source(source_key)
        if can_reuse_loaded_snapshot:
            self._source_tabs.blockSignals(True)
            self._source_tabs.setCurrentIndex(self._source_tab_snapshot_idx)
            self._source_tabs.blockSignals(False)
            self._activate_table_for_tab(self._source_tab_snapshot_idx)
            self._all_entries = self._snapshot_all_entries
            self._filtered = self._snapshot_filtered
            self._apply_filter()
            return

        # データをロード
        self._load_data(reset_filters=False)

    # ──────────────────────────────────────────────────────────────────────────
    # ソース選択イベント
    # ──────────────────────────────────────────────────────────────────────────

    def _on_source_changed(self, btn, checked: bool) -> None:
        if checked:
            current_source_key = self._current_source_key()
            if current_source_key == "bs":
                self._maps_source_key = current_source_key
            else:
                self._snapshot_source_key = current_source_key
            if btn is not self._rb_bs and isinstance(btn, QRadioButton):
                self._last_snapshot_source_button = btn
            target_tab = self._source_tab_maps_idx if self._rb_bs.isChecked() else self._source_tab_snapshot_idx
            if self._source_tabs.currentIndex() != target_tab:
                self._source_tabs.blockSignals(True)
                self._source_tabs.setCurrentIndex(target_tab)
                self._source_tabs.blockSignals(False)
                self._apply_highest_diff_only_for_current_tab()
                self._activate_table_for_tab(target_tab)
                self._apply_saved_sort_for_current_tab()
                self._update_table_visual_mode()
        open_mode = self._rb_open.isChecked()
        beatsaver_mode = self._rb_bs.isChecked()
        self._open_edit.setEnabled(open_mode)
        self._btn_browse.setEnabled(open_mode)
        self._svc_label.setEnabled(open_mode)
        self._svc_combo.setEnabled(open_mode)
        self._btn_add_to_batch.setVisible(not open_mode)
        # open 以外のソースに切り替えたらヘッダを元に戻す
        if not open_mode:
            hdr_item = self._table.horizontalHeaderItem(_COL_ACC_CAT)
            if hdr_item is not None:
                hdr_item.setText("Category")
        # Filter / Export UI をソース状態に合わせて更新
        self._update_filter_export_ui()
        # ソースに応じて PP / Acc / Rank 列ヘッダを切り替え
        self._update_score_headers()

    def _on_source_tab_changed(self, index: int) -> None:
        if index == self._source_tab_maps_idx:
            if not self._rb_bs.isChecked():
                self._rb_bs.setChecked(True)
            if self._restored_maps_state:
                self._schedule_deferred_maps_restore()
        elif self._rb_bs.isChecked():
            self._last_snapshot_source_button.setChecked(True)
        self._apply_highest_diff_only_for_current_tab()
        self._activate_table_for_tab(index)
        self._apply_saved_sort_for_current_tab()
        self._update_table_visual_mode()
        self._update_score_headers()

    def select_source_tab(self, tab_name: str) -> None:
        target = (tab_name or "snapshot").strip().lower()
        if target == "maps":
            self._source_tabs.setCurrentIndex(self._source_tab_maps_idx)
            return
        self._source_tabs.setCurrentIndex(self._source_tab_snapshot_idx)

    def _update_filter_export_ui(self) -> None:
        """現在のソース/サービス設定に応じて Filter・Export の表示状態を更新する。"""
        is_acc = self._rb_acc.isChecked() or self._rb_acc_rl.isChecked() or (
            self._rb_open.isChecked() and self._svc_combo.currentData() == "accsaber_rl"
        )
        is_rl = self._rb_acc_rl.isChecked() or (
            self._rb_open.isChecked() and self._svc_combo.currentData() == "accsaber_rl"
        )
        is_bs = self._rb_bs.isChecked()
        # AccSaber / AccSaber RL ではカテゴリ別分割なので Split ラベルを切り替え
        self._rb_exp_split.setText("Split by Category (True / Standard / Tech)" if is_acc else "Split by ★")
        # Category filter チェックボックスは AccSaber / AccSaber RL のときのみ表示
        for w in [self._cat_filter_label, self._cb_cat_true, self._cb_cat_standard, self._cb_cat_tech]:
            w.setVisible(is_acc)
        # ★レンジは AccSaber / AccSaber RL / BeatSaver では非表示
        for w in [self._star_label, self._star_min, self._star_sep_label, self._star_max]:
            w.setVisible(not is_acc and not is_bs)
        self._cb_sts_queued.setVisible(is_rl)
        self._cb_bs_not_downloaded.setVisible(is_bs)
        self._cb_bs_downloaded.setVisible(is_bs)
        self._bs_filter_row_widget.setVisible(is_bs)
        self._bs_post_filter_widget.setVisible(is_bs)
        self._btn_download_selected.setVisible(is_bs)
        if is_bs:
            self._search_edit.setPlaceholderText("Filter loaded BeatSaver rows by song / author / mapper...")
            self._search_edit.setToolTip("Load後の BeatSaver 一覧を絞り込みます。Load Query は下の BeatSaver 行で指定します")
        else:
            self._search_edit.setPlaceholderText("Filter loaded rows by song / author / mapper...")
            self._search_edit.setToolTip("Load後の一覧を絞り込みます。スペース区切りで複数キーワードのAND検索ができます")
        self._update_table_visual_mode()

    def _update_table_visual_mode(self) -> None:
        is_bs = self._rb_bs.isChecked()
        show_cover = is_bs or not self._is_maps_tab()
        for col in (_COL_ACC_CAT, _COL_STARS, _COL_FC, _COL_MOD):
            self._table.setColumnHidden(col, is_bs)
        for col in (_COL_PLAYER_RANK, _COL_PLAYER_ACC, _COL_PLAYER_PP):
            self._table.setColumnHidden(col, is_bs)
        for col in (_COL_BS_RATE, _COL_BS_UPVOTES, _COL_BS_DOWNVOTES):
            self._table.setColumnHidden(col, not is_bs)
        self._table.setColumnHidden(_COL_COVER, not show_cover)
        self._table.setColumnHidden(_COL_ONECLICK, not is_bs)
        self._table.setColumnHidden(_COL_DELETE, not is_bs)
        self._table.setColumnHidden(_COL_BL_PLAYS, True)
        self._table.setColumnHidden(_COL_BL_ATTEMPTS, True)
        self._table.setItemDelegateForColumn(
            _COL_BS_RATE,
            self._maps_rate_delegate if is_bs else self._default_numeric_delegate,
        )

    def _on_svc_combo_changed(self) -> None:
        """サービスコンボ変更時に Filter/Export UI とテーブルヘッダを更新する。"""
        self._update_filter_export_ui()
        self._update_score_headers()

    def _update_score_headers(self) -> None:
        """ソースに応じて Date / PP / Acc % / Rank 列のヘッダを切り替える。"""
        if self._is_maps_tab():
            date_label = "Published"
            pp_label, acc_label, rank_label = "PP", "Acc %", "Rank"
        elif self._rb_ss.isChecked():
            date_label, pp_label, acc_label, rank_label = "Ranked", "PP", "Acc %", "Rank"
        elif self._rb_bl.isChecked():
            date_label, pp_label, acc_label, rank_label = "Ranked", "PP", "Acc %", "Rank"
        elif self._rb_acc.isChecked() or self._rb_acc_rl.isChecked():
            date_label, pp_label, acc_label, rank_label = "Ranked", "AP", "Acc %", "Rank"
        elif self._rb_bs.isChecked():
            date_label, pp_label, acc_label, rank_label = "Published", "PP", "Acc %", "Rank"
        else:
            # open モード: service コンボで決まる
            svc = self._svc_combo.currentData() or "none"
            if svc == "scoresaber":
                date_label, pp_label, acc_label, rank_label = "Ranked", "PP", "Acc %", "Rank"
            elif svc == "beatleader":
                date_label, pp_label, acc_label, rank_label = "Ranked", "PP", "Acc %", "Rank"
            elif svc in ("accsaber", "accsaber_rl"):
                date_label, pp_label, acc_label, rank_label = "Ranked", "AP", "Acc %", "Rank"
            else:
                date_label, pp_label, acc_label, rank_label = "Date", "PP", "Acc %", "Rank"
        for col, label in [
            (_COL_SOURCE_DATE, date_label),
            (_COL_PLAYER_PP, pp_label),
            (_COL_PLAYER_ACC, acc_label),
            (_COL_PLAYER_RANK, rank_label),
        ]:
            item = self._table.horizontalHeaderItem(col)
            if item is not None:
                item.setText(label)
        # AccSaber / AccSaber RL 時は★列を Category に切り替え
        is_acc_mode = self._rb_acc.isChecked() or self._rb_acc_rl.isChecked() or (
            self._rb_open.isChecked() and self._svc_combo.currentData() in ("accsaber_rl", "accsaber")
        )
        star_hdr = self._table.horizontalHeaderItem(_COL_STARS)
        if star_hdr is not None:
            star_hdr.setText("Cmplx" if is_acc_mode else "★")

    def _current_sort_mode(self) -> str:
        """テーブルヘッダの現在のソート状態から sort_mode を返す。"""
        col = self._table.horizontalHeader().sortIndicatorSection()
        order = self._table.horizontalHeader().sortIndicatorOrder()
        is_desc = (order == Qt.SortOrder.DescendingOrder)
        if col == _COL_STATUS:
            return "status_desc" if is_desc else "status_asc"
        if col == _COL_SONG:
            return "song_desc" if is_desc else "song_asc"
        if col == _COL_SOURCE_DATE:
            return "date_desc" if is_desc else "date_asc"
        if col == _COL_DURATION:
            return "duration_desc" if is_desc else "duration_asc"
        if col == _COL_BL_PLAYS:
            return "bl_plays_desc" if is_desc else "bl_plays_asc"
        if col == _COL_BL_ATTEMPTS:
            return "bl_attempts_desc" if is_desc else "bl_attempts_asc"
        if col == _COL_PLAY_TIME:
            return "playtime_desc" if is_desc else "playtime_asc"
        if col == _COL_DIFF:
            return "diff_desc" if is_desc else "diff_asc"
        if col == _COL_MODE:
            return "mode_desc" if is_desc else "mode_asc"
        if col == _COL_ACC_CAT:
            return "cat_desc" if is_desc else "cat_asc"
        if col == _COL_PLAYER_PP:
            # AccSaber/RL モードでは AP 表示のため ap ソートモードを返す
            hdr = self._table.horizontalHeaderItem(_COL_PLAYER_PP)
            if hdr and hdr.text() == "AP":
                return "ap_high" if is_desc else "ap_low"
            return "pp_high" if is_desc else "pp_low"
        if col == _COL_BS_RATE:
            return "bs_rate_high" if is_desc else "bs_rate_low"
        if col == _COL_BS_UPVOTES:
            return "bs_upvotes_high" if is_desc else "bs_upvotes_low"
        if col == _COL_BS_DOWNVOTES:
            return "bs_downvotes_high" if is_desc else "bs_downvotes_low"
        if col == _COL_PLAYER_ACC:
            return "acc_high" if is_desc else "acc_low"
        if col == _COL_PLAYER_RANK:
            return "rank_high" if is_desc else "rank_low"
        if col == _COL_STARS:
            return "star_desc" if is_desc else "star_asc"
        if col == _COL_FC:
            return "fc_desc" if is_desc else "fc_asc"
        if col == _COL_MAPPER:
            return "mapper_desc" if is_desc else "mapper_asc"
        if col == _COL_AUTHOR:
            return "author_desc" if is_desc else "author_asc"
        return "star_asc"

    def _make_export_tag(self) -> str:
        """現在のフィルタ・ソート・検索状態を反映したファイル名タグを返す。

        例: "unplayed_star3-8_pp_high" / "cleared+nf_q_boss_pp_high"
        """
        parts: List[str] = []

        # ステータス
        status_tag = _status_filter_tag(
            self._cb_sts_cleared.isChecked(),
            self._cb_sts_nf.isChecked(),
            self._cb_sts_unplayed.isChecked(),
            self._cb_sts_queued.isChecked(),
        )
        if status_tag is not None:
            parts.append(status_tag)

        # 検索テキスト
        search = self._search_edit.text().strip()
        if search:
            safe = search.replace(" ", "-")[:20]
            parts.append(f"q_{safe}")

        # ★レンジ
        s_min = self._star_min.value()
        s_max = self._star_max.value()
        if s_min > 0.0 or s_max < 20.0:
            parts.append(f"star{s_min:.0f}-{s_max:.0f}")

        # AccSaber / AccSaber RL カテゴリ
        if self._rb_acc.isChecked() or self._rb_acc_rl.isChecked():
            cat_parts: List[str] = []
            if self._cb_cat_true.isChecked():
                cat_parts.append("T")
            if self._cb_cat_standard.isChecked():
                cat_parts.append("S")
            if self._cb_cat_tech.isChecked():
                cat_parts.append("Tc")
            if len(cat_parts) < 3:
                parts.append("+".join(cat_parts) if cat_parts else "nocat")

        # ソート
        sort_mode = self._current_sort_mode()
        _sort_tags = {
            "star_asc": "star_asc", "star_desc": "star_desc",
            "date_desc": "date_desc", "date_asc": "date_asc",
            "duration_desc": "duration_desc", "duration_asc": "duration_asc",
            "bl_plays_desc": "bl_plays_desc", "bl_plays_asc": "bl_plays_asc",
            "bl_attempts_desc": "bl_attempts_desc", "bl_attempts_asc": "bl_attempts_asc",
            "playtime_desc": "playtime_desc", "playtime_asc": "playtime_asc",
            "pp_high": "pp_desc", "pp_low": "pp_asc",
            "ap_high": "ap_desc", "ap_low": "ap_asc",
            "acc_high": "acc_desc", "acc_low": "acc_asc",
            "rank_low": "rank_asc", "rank_high": "rank_desc",
            "bs_rate_high": "rate_desc", "bs_rate_low": "rate_asc",
            "bs_upvotes_high": "upvotes_desc", "bs_upvotes_low": "upvotes_asc",
            "bs_downvotes_high": "downvotes_desc", "bs_downvotes_low": "downvotes_asc",
            "fc_desc": "fc_desc", "fc_asc": "fc_asc",
            "status_desc": "status_desc", "status_asc": "status_asc",
            "song_desc": "song_desc", "song_asc": "song_asc",
            "diff_desc": "diff_desc", "diff_asc": "diff_asc",
            "mode_desc": "mode_desc", "mode_asc": "mode_asc",
            "cat_desc": "cat_desc", "cat_asc": "cat_asc",
            "mapper_desc": "mapper_desc", "mapper_asc": "mapper_asc",
            "author_desc": "author_desc", "author_asc": "author_asc",
        }
        parts.append(_sort_tags.get(sort_mode, sort_mode))

        return "_".join(parts) if parts else "all"

    def _update_sort_label(self) -> None:
        """Export エリアのソート表示ラベルを現在のテーブルソート状態に合わせて更新する。"""
        col = self._table.horizontalHeader().sortIndicatorSection()
        order = self._table.horizontalHeader().sortIndicatorOrder()
        is_desc = (order == Qt.SortOrder.DescendingOrder)
        col_name = _COL_LABELS[col] if 0 <= col < len(_COL_LABELS) else "?"
        arrow = "↓" if is_desc else "↑"
        self._sort_label.setText(f"{col_name} {arrow}")

    def _on_header_clicked(self, _col: int) -> None:
        """ヘッダクリック後にソート表示ラベルを更新する。"""
        self._set_current_tab_sort_mode(self._current_sort_mode())
        self._update_sort_label()
        self._save_window_state()

    def _browse_bplist(self) -> None:
        # 前回のエクスポート先または開いたファイルのディレクトリを初期フォルダにする
        current = self._open_edit.text().strip()
        init_dir = str(Path(current).parent) if current and Path(current).exists() else self._export_dir
        path, _ = QFileDialog.getOpenFileName(
            self, "Open bplist file", init_dir,
            "Playlist files (*.bplist *.json);;BeatSaber Playlist (*.bplist);;JSON (*.json);;All files (*)"
        )
        if path:
            self._open_edit.setText(path)
            self._save_export_dir(str(Path(path).parent))
            # ファイル名の先頭でサービスを自動選択
            stem = Path(path).stem.lower()
            if stem.startswith("ss"):
                svc = "scoresaber"
            elif stem.startswith("bl"):
                svc = "beatleader"
            elif stem.startswith("rl") or stem.startswith("accsaber_reloaded"):
                svc = "accsaber_rl"
            elif stem.startswith("accsaber") or stem.startswith("as_"):
                svc = "accsaber"
            else:
                svc = None
            if svc is not None:
                idx = self._svc_combo.findData(svc)
                if idx >= 0:
                    self._svc_combo.setCurrentIndex(idx)

    def _reset_filters(self) -> None:
        widgets = [
            self._search_edit,
            self._star_min,
            self._star_max,
            self._bs_filter_min_rating,
            self._bs_filter_min_votes,
            self._cb_sts_cleared,
            self._cb_sts_nf,
            self._cb_sts_unplayed,
            self._cb_sts_queued,
            self._cb_bs_not_downloaded,
            self._cb_bs_downloaded,
            self._cb_top_diff_only,
            self._cb_cat_true,
            self._cb_cat_standard,
            self._cb_cat_tech,
        ]
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self._search_edit.clear()
            self._star_min.setValue(0.0)
            self._star_max.setValue(20.0)
            self._set_bs_rating_value(50)
            self._set_bs_votes_value(0)
            self._cb_sts_cleared.setChecked(True)
            self._cb_sts_nf.setChecked(True)
            self._cb_sts_unplayed.setChecked(True)
            self._cb_sts_queued.setChecked(False)
            self._cb_bs_not_downloaded.setChecked(True)
            self._cb_bs_downloaded.setChecked(True)
            default_highest = self._highest_diff_only_default_for_tab()
            self._set_highest_diff_only_for_tab(default_highest)
            self._cb_top_diff_only.setChecked(default_highest)
            self._cb_cat_true.setChecked(True)
            self._cb_cat_standard.setChecked(True)
            self._cb_cat_tech.setChecked(True)
        finally:
            for widget in widgets:
                widget.blockSignals(False)

    def _on_reset_filters_clicked(self) -> None:
        self._reset_filters()
        self._apply_filter()

    def _on_load_clicked(self) -> None:
        load_text = f"Last Load: {datetime.now().strftime('%Y/%m/%d %H:%M:%S')}"
        if self._is_maps_tab():
            self._maps_last_load_text = load_text
        else:
            self._snapshot_last_load_text = load_text
        self._last_load_label.setText(load_text)
        self._load_data()

    # ──────────────────────────────────────────────────────────────────────────
    # データ読み込み
    # ──────────────────────────────────────────────────────────────────────────

    def _load_data(self, reset_filters: bool = True) -> None:
        """選択されたソースに応じてマップデータを読み込む。"""
        self._btn_load.setEnabled(False)
        self._installed_beatsaber_dir = ""
        self._reset_beatsaver_cache_status()
        self._pending_load_source_key = self._current_source_key()
        self._pending_load_maps_tab = self._is_maps_tab()
        worker_fn = None
        pending_title = ""
        pending_open_path: Optional[Path] = None
        pending_open_service = ""
        beatsaver_opts = None

        steam_id = self._steam_id

        if self._rb_ss.isChecked():
            pending_title = SOURCE_SS
            worker_fn = lambda sig: self._run_load_ss(sig, steam_id)

        elif self._rb_bl.isChecked():
            pending_title = SOURCE_BL
            worker_fn = lambda sig: self._run_load_bl(sig, steam_id)

        elif self._rb_acc.isChecked():
            pending_title = SOURCE_ACC
            worker_fn = lambda sig: self._run_load_acc(sig, steam_id, "all")

        elif self._rb_acc_rl.isChecked():
            pending_title = SOURCE_ACC_RL
            worker_fn = lambda sig: self._run_load_acc_rl(sig, steam_id, "all")

        elif self._rb_bs.isChecked():
            pending_title = SOURCE_BS
            beatsaver_opts = {
                "query": self._bs_query_edit.text().strip(),
                "max_maps": self._bs_max_maps.value(),
                "days": self._bs_days.value(),
                "from_date": self._bs_from_date.date().toString("yyyy-MM-dd"),
                "to_date": self._bs_to_date.date().toString("yyyy-MM-dd"),
                "min_rating": self._bs_min_rating.value(),
                "min_votes": self._bs_min_votes.value(),
                "unranked_only": self._cb_bs_unranked.isChecked(),
                "exclude_ai": self._cb_bs_no_ai.isChecked(),
            }
            worker_fn = lambda sig, opts=beatsaver_opts: self._run_load_beatsaver(sig, steam_id, opts)

        elif self._rb_open.isChecked():
            file_path_str = self._open_edit.text().strip()
            if not file_path_str:
                QMessageBox.warning(self, "Open File", "Please specify a .bplist or .json file.")
                self._btn_load.setEnabled(True)
                return
            pending_open_path = Path(file_path_str)
            if not pending_open_path.exists():
                QMessageBox.warning(self, "Open File", f"File not found:\n{pending_open_path}")
                self._btn_load.setEnabled(True)
                return
            if pending_open_path.suffix.lower() not in (".bplist", ".json"):
                QMessageBox.warning(self, "Open File", "Unsupported file type. Please open a .bplist or .json file.")
                self._btn_load.setEnabled(True)
                return
            pending_open_service = str(self._svc_combo.currentData() or "none")
            pending_title = f"Open: {pending_open_path.name}"
            if pending_open_service == "accsaber_rl":
                worker_fn = lambda sig, bp=pending_open_path: self._run_load_open_rl(sig, bp, steam_id)
            else:
                worker_fn = lambda sig, bp=pending_open_path, sv=pending_open_service: self._run_load_open(sig, bp, sv, steam_id)

        if worker_fn is None:
            self._btn_load.setEnabled(True)
            return

        prep_steps = 4
        self._update_load_progress_dialog(0, prep_steps, "Preparing load... 0%")
        if reset_filters and not self._rb_bs.isChecked():
            self._reset_filters()
        self._update_load_progress_dialog(1, prep_steps, "Preparing load... 25%")
        self._all_entries = []
        self._filtered = []
        self._table.setRowCount(0)
        self._sync_active_table_state()
        self._update_load_progress_dialog(2, prep_steps, "Preparing load... 50%")
        self._clear_preview()
        self._update_load_progress_dialog(3, prep_steps, "Preparing load... 75%")
        self._setWindowTitle_source(pending_title)
        if pending_open_path is not None and pending_open_service == "accsaber_rl":
            self._open_bplist_path = pending_open_path
        self._update_load_progress_dialog(4, prep_steps, "Preparing load... 100%")
        self._start_async_load(worker_fn)

    def _show_load_progress_dialog(self, label: str = "Loading...") -> None:
        dlg = self._progress_dlg
        if dlg is not None:
            dlg.setLabelText(label)
            dlg.show()
            QApplication.processEvents()
            return
        dlg = QProgressDialog(label, "Cancel", 0, 0, self)
        dlg.setWindowTitle("Loading")
        dlg.setMinimumWidth(340)
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.show()
        self._progress_dlg = dlg
        QApplication.processEvents()

    def _close_load_progress_dialog(self) -> None:
        dlg = self._progress_dlg
        self._progress_dlg = None
        if dlg is not None:
            dlg.close()

    def _update_load_progress_dialog(self, done: int, total: int, label: str) -> None:
        self._show_load_progress_dialog(label)
        dlg = self._progress_dlg
        if dlg is None:
            return
        dlg.setRange(0, max(1, total))
        dlg.setValue(max(0, min(done, total)))
        dlg.setLabelText(label)
        QApplication.processEvents()

    def _start_async_load(self, worker_fn) -> None:
        """API 取得をスレッドで実行してプログレスダイアログを表示する。"""
        self._show_load_progress_dialog("Loading...")
        dlg = self._progress_dlg
        if dlg is None:
            self._btn_load.setEnabled(True)
            return
        dlg.setRange(0, 0)

        sigs = self._load_signals

        def _task() -> None:
            try:
                worker_fn(sigs)
            except Exception as exc:  # noqa: BLE001
                sigs.error.emit(str(exc))

        t = threading.Thread(target=_task, daemon=True)

        def _on_cancel() -> None:
            # キャンセルボタンは UI を閉じるだけ（スレッドは自然終了を待つ）
            self._close_load_progress_dialog()
            self._btn_load.setEnabled(True)

        dlg.canceled.connect(_on_cancel)
        t.start()

    def _run_load_open_rl(self, sigs: _LoadSignals, bplist_path: Path, steam_id: Optional[str]) -> None:
        """open + AccSaber RL の非同期ロードタスク。"""
        def _progress(done: int, total: int, label: str) -> None:
            sigs.progress.emit(done, total, label)
        try:
            entries = load_bplist_maps(bplist_path, "accsaber_rl", steam_id, on_progress=_progress)
            sigs.finished.emit(entries)
        except Exception as exc:
            sigs.error.emit(str(exc))

    def _run_load_ss(self, sigs: _LoadSignals, steam_id: Optional[str]) -> None:
        def _progress(done: int, total: int, label: str) -> None:
            sigs.progress.emit(done, total, label)
        try:
            _progress(0, 1, "Loading ScoreSaber cache...")
            entries = load_ss_maps(steam_id)
            _progress(1, 1, "Loading ScoreSaber cache...")
            sigs.finished.emit(entries)
        except Exception as exc:
            sigs.error.emit(str(exc))

    def _run_load_bl(self, sigs: _LoadSignals, steam_id: Optional[str]) -> None:
        def _progress(done: int, total: int, label: str) -> None:
            sigs.progress.emit(done, total, label)
        try:
            _progress(0, 1, "Loading BeatLeader cache...")
            entries = load_bl_maps(steam_id)
            _progress(1, 1, "Loading BeatLeader cache...")
            sigs.finished.emit(entries)
        except Exception as exc:
            sigs.error.emit(str(exc))

    def _run_load_open(self, sigs: _LoadSignals, bplist_path: Path, service: str, steam_id: Optional[str]) -> None:
        def _progress(done: int, total: int, label: str) -> None:
            sigs.progress.emit(done, total, label)
        try:
            _progress(0, 1, f"Loading {bplist_path.name}...")
            entries = load_bplist_maps(bplist_path, service, steam_id)
            _progress(1, 1, f"Loading {bplist_path.name}...")
            sigs.finished.emit(entries)
        except Exception as exc:
            sigs.error.emit(str(exc))

    def _run_load_acc(self, sigs: _LoadSignals, steam_id: Optional[str], category: str) -> None:
        def _progress(done: int, total: int, label: str) -> None:
            sigs.progress.emit(done, total, label)
        try:
            entries = load_accsaber_maps(steam_id, category, on_progress=_progress)
            sigs.finished.emit(entries)
        except Exception as exc:
            sigs.error.emit(str(exc))

    def _run_load_acc_rl(self, sigs: _LoadSignals, steam_id: Optional[str], category: str) -> None:
        def _progress(done: int, total: int, label: str) -> None:
            sigs.progress.emit(done, total, label)
        try:
            entries = load_accsaber_reloaded_maps(steam_id, category, on_progress=_progress)
            sigs.finished.emit(entries)
        except Exception as exc:
            sigs.error.emit(str(exc))

    def _run_load_beatsaver(self, sigs: _LoadSignals, steam_id: Optional[str], opts: Dict[str, object]) -> None:
        def _progress(done: int, total: int, label: str) -> None:
            sigs.progress.emit(done, total, label)
        try:
            query = str(opts.get("query") or "")
            max_maps_raw = opts.get("max_maps")
            days_raw = opts.get("days")
            from_date_raw = str(opts.get("from_date") or "")
            to_date_raw = str(opts.get("to_date") or "")
            min_rating_raw = opts.get("min_rating")
            min_votes_raw = opts.get("min_votes")
            max_maps = int(max_maps_raw) if isinstance(max_maps_raw, (int, float, str)) else 1000
            days = int(days_raw) if isinstance(days_raw, (int, float, str)) else 7
            from_dt = _parse_local_date_filter(from_date_raw)
            to_dt = _parse_local_date_filter(to_date_raw, end_of_day=True)
            min_rating_percent = float(min_rating_raw) if isinstance(min_rating_raw, (int, float, str)) else 0.0
            min_rating = min_rating_percent / 100.0
            min_votes = int(min_votes_raw) if isinstance(min_votes_raw, (int, float, str)) else 0
            entries = load_beatsaver_maps(
                steam_id=steam_id,
                query=query,
                days=days,
                min_rating=min_rating,
                min_votes=min_votes,
                max_maps=max_maps,
                from_dt=from_dt,
                to_dt=to_dt,
                unranked_only=bool(opts.get("unranked_only", True)),
                exclude_ai=bool(opts.get("exclude_ai", True)),
                on_progress=_progress,
            )
            entries = _cache_beatsaver_meta_from_entries(entries)
            sigs.finished.emit(entries)
        except Exception as exc:
            sigs.error.emit(str(exc))

    def _on_load_progress(self, done: int, total: int, label: str) -> None:
        dlg = self._progress_dlg
        if dlg is not None and not dlg.wasCanceled():
            if total > 0:
                dlg.setMaximum(total)
                dlg.setValue(done)
            dlg.setLabelText(label)

    def _on_load_finished(self, entries: List[MapEntry]) -> None:
        self._close_load_progress_dialog()
        self._btn_load.setEnabled(True)
        self._all_entries = entries
        if self._pending_load_maps_tab:
            self._maps_loaded_steam_id = self._steam_id
            self._maps_loaded_source_key = self._pending_load_source_key
        else:
            self._snapshot_loaded_steam_id = self._steam_id
            self._snapshot_loaded_source_key = self._pending_load_source_key
        self._sync_active_table_state()
        if not entries:
            self._count_label.setText("0 maps")
            self._save_window_state()
            if self._rb_bs.isChecked():
                QMessageBox.information(
                    self,
                    "BeatSaver",
                    "条件に一致する譜面が見つかりませんでした。\n\n"
                    f"Load Query: {self._bs_query_edit.text().strip() or '(empty)'}\n"
                    f"Max: {self._bs_max_maps.value()}\n"
                    f"Days: {self._bs_days.value()}\n"
                    f"From: {self._bs_from_date.date().toString('yyyy/MM/dd')}\n"
                    f"To: {self._bs_to_date.date().toString('yyyy/MM/dd')}\n"
                    f"Rating >= {self._bs_min_rating.value()}%\n"
                    f"Votes >= {self._bs_min_votes.value()}\n"
                    f"Unranked only: {'on' if self._cb_bs_unranked.isChecked() else 'off'}\n"
                    f"Exclude AI: {'on' if self._cb_bs_no_ai.isChecked() else 'off'}"
                )
            self._clear_preview()
            return
        # AccSaber RL のときは ★ フィルタをリセット（★が意味を持たないため）
        is_rl = self._rb_acc_rl.isChecked() or (
            self._rb_open.isChecked() and self._svc_combo.currentData() == "accsaber_rl"
        )
        if is_rl:
            self._star_min.blockSignals(True)
            self._star_max.blockSignals(True)
            self._star_min.setValue(0.0)
            self._star_max.setValue(20.0)
            self._star_min.blockSignals(False)
            self._star_max.blockSignals(False)
        # open + AccSaber RL の場合はヘッダを Service に変更
        if self._rb_open.isChecked() and self._svc_combo.currentData() == "accsaber_rl":
            hdr_item = self._table.horizontalHeaderItem(_COL_ACC_CAT)
            if hdr_item is not None:
                hdr_item.setText("Service")
        # Open モード: プレイリストの曲順で表示するためソートをリセット
        if self._rb_open.isChecked():
            self._table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
            self._set_current_tab_sort_mode(self._current_sort_mode())
        elif self._rb_bs.isChecked():
            self._table.horizontalHeader().setSortIndicator(_COL_SOURCE_DATE, Qt.SortOrder.DescendingOrder)
            self._set_current_tab_sort_mode("date_desc")
            self._update_sort_label()
        else:
            self._set_current_tab_sort_mode(self._current_sort_mode())
        self._apply_filter()
        if not self._rb_bs.isChecked():
            self._queue_beatsaver_cache_for_current_entries()
        self._save_window_state()

    def _on_load_error(self, msg: str) -> None:
        self._close_load_progress_dialog()
        self._btn_load.setEnabled(True)
        QMessageBox.critical(self, "Load Error", msg)

    def _selected_entry(self) -> Optional[MapEntry]:
        selection_model = self._table.selectionModel()
        if selection_model is None:
            return None
        rows = selection_model.selectedRows()
        if not rows:
            return None
        item = self._table.item(rows[0].row(), _COL_SONG)
        if item is None:
            return None
        entry = item.data(Qt.ItemDataRole.UserRole)
        return entry if isinstance(entry, MapEntry) else None

    def _selected_entries(self) -> List[MapEntry]:
        selection_model = self._table.selectionModel()
        if selection_model is None:
            return []
        selected_entries: List[MapEntry] = []
        for model_index in selection_model.selectedRows():
            item = self._table.item(model_index.row(), _COL_SONG)
            if item is None:
                continue
            entry = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(entry, MapEntry):
                selected_entries.append(entry)
        return selected_entries

    def _clear_preview(self) -> None:
        self._preview_image.setPixmap(QPixmap())
        self._preview_image.setText("(no cover)")
        self._preview_description_text = ""
        self._preview_title_full_text = "No map selected"
        self._preview_translate_button.setEnabled(False)
        self._preview_bsr_button.setText("")
        self._preview_bsr_button.setProperty("copy_text", "")
        self._preview_bsr_button.setVisible(False)
        self._update_preview_title_label()
        self._preview_meta_text.clear()
        self._btn_preview_open.setEnabled(False)
        self._btn_preview_open.setProperty("url", "")
        self._btn_preview_bl.setEnabled(False)
        self._btn_preview_bl.setProperty("url", "")
        self._btn_preview_replay.setEnabled(False)
        self._btn_preview_replay.setProperty("url", "")
        self._btn_preview_download.setEnabled(False)
        self._btn_preview_download.setProperty("url", "")
        self._btn_preview_global1_replay.setEnabled(False)
        self._btn_preview_global1_replay.setProperty("url", "")
        self._btn_preview_global1_replay.setProperty("leaderboard_id", "")
        self._btn_preview_global1_replay.setProperty("countries", "")
        self._btn_preview_local1_replay.setEnabled(False)
        self._btn_preview_local1_replay.setProperty("url", "")
        self._btn_preview_local1_replay.setProperty("leaderboard_id", "")
        self._btn_preview_local1_replay.setProperty("countries", "")

    def _resolve_bl_top_replay_url(self, leaderboard_id: str, countries: str = "") -> str:
        cache_key = (leaderboard_id, countries.upper())
        if cache_key in self._bl_top_replay_cache:
            return self._bl_top_replay_cache[cache_key]
        url = _fetch_bl_top_replay_url(self._bl_api_session, leaderboard_id, countries)
        self._bl_top_replay_cache[cache_key] = url
        return url

    def _load_bl_preview_link_indices(self) -> Tuple[Dict[Tuple[str, str, str], str], Dict[Tuple[str, str, str], str]]:
        if self._bl_preview_replay_index is None or self._bl_preview_leaderboard_index is None:
            replay_index: Dict[Tuple[str, str, str], str] = {}
            leaderboard_index: Dict[Tuple[str, str, str], str] = {}
            steam_id = self._steam_id
            if steam_id:
                bp = _CACHE_DIR / f"beatleader_player_scores_{steam_id}.json"
                if bp.exists():
                    try:
                        bd = json.loads(bp.read_text(encoding="utf-8"))
                        bl_scores = bd.get("scores", {})
                        replay_index = _build_bl_replay_hash_index(bl_scores)
                        leaderboard_index = _build_bl_leaderboard_hash_index(bl_scores)
                    except Exception:
                        replay_index = {}
                        leaderboard_index = {}
            self._bl_preview_replay_index = replay_index
            self._bl_preview_leaderboard_index = leaderboard_index
        return self._bl_preview_replay_index, self._bl_preview_leaderboard_index

    def _update_preview_title_label(self) -> None:
        available_width = self._preview_title_widget.width()
        if self._preview_translate_button.isVisible():
            available_width -= self._preview_translate_button.sizeHint().width() + 6
        if self._preview_bsr_button.isVisible():
            available_width -= self._preview_bsr_button.sizeHint().width() + 6
        available_width = max(available_width, 40)
        elided = self._preview_title_label.fontMetrics().elidedText(
            self._preview_title_full_text,
            Qt.TextElideMode.ElideRight,
            available_width,
        )
        self._preview_title_label.setText(elided)
        self._preview_title_label.setToolTip(self._preview_title_full_text if elided != self._preview_title_full_text else "")

    def _copy_preview_bsr(self) -> None:
        copy_text = str(self._preview_bsr_button.property("copy_text") or "")
        if copy_text:
            QApplication.clipboard().setText(copy_text)

    def _translate_preview_description(self) -> None:
        if self._preview_description_text.strip():
            self._translate_preview_text(self._preview_description_text)

    def _update_preview_from_selection(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self._clear_preview()
            return

        cached_beatsaver_meta = load_beatsaver_meta_cache().get((entry.song_hash or "").upper())
        _apply_beatsaver_meta(entry, cached_beatsaver_meta)
        if entry.source != "beatsaver":
            self._queue_beatsaver_cache_entries([entry], prioritize=True)
        if not entry.beatleader_page_url or not entry.beatleader_replay_url:
            replay_index, leaderboard_index = self._load_bl_preview_link_indices()
            bl_key = (entry.song_hash.upper(), entry.mode, entry.difficulty)
            leaderboard_id = leaderboard_index.get(bl_key, "")
            replay_url = replay_index.get(bl_key, "")
            if leaderboard_id:
                if not entry.leaderboard_id:
                    entry.leaderboard_id = leaderboard_id
                if not entry.beatleader_page_url:
                    entry.beatleader_page_url = f"https://beatleader.com/leaderboard/global/{leaderboard_id}"
            if replay_url and not entry.beatleader_replay_url:
                entry.beatleader_replay_url = replay_url

        self._preview_title_full_text = entry.song_name or "(untitled)"
        has_bl_stats_source = bool(entry.leaderboard_id and entry.source in ("beatleader", "beatsaver"))
        if entry.beatsaver_key:
            bsr_text = f"BSR: {entry.beatsaver_key or '-'}"
            self._preview_bsr_button.setText(bsr_text)
            self._preview_bsr_button.setProperty("copy_text", entry.beatsaver_key or "")
            self._preview_bsr_button.setVisible(True)
        else:
            self._preview_bsr_button.setText("")
            self._preview_bsr_button.setProperty("copy_text", "")
            self._preview_bsr_button.setVisible(False)

        self._preview_description_text = str(entry.beatsaver_description or "").strip()
        self._preview_translate_button.setEnabled(bool(self._preview_description_text))
        details = [self._preview_description_text] if self._preview_description_text else []
        self._update_preview_title_label()
        self._preview_meta_text.setHtml(_preview_text_to_html("\n".join(details)))
        self._btn_preview_open.setEnabled(bool(entry.beatsaver_page_url))
        self._btn_preview_open.setProperty("url", entry.beatsaver_page_url)
        self._btn_preview_bl.setEnabled(bool(entry.beatleader_page_url))
        self._btn_preview_bl.setProperty("url", entry.beatleader_page_url)
        self._btn_preview_replay.setEnabled(bool(entry.beatleader_replay_url))
        self._btn_preview_replay.setProperty("url", entry.beatleader_replay_url)
        self._btn_preview_download.setEnabled(self._can_download_beatsaver_entry(entry))
        self._btn_preview_download.setProperty("url", entry.beatsaver_download_url)
        bl_leaderboard_id = ""
        if entry.source in ("beatsaver", "beatleader"):
            bl_leaderboard_id = entry.leaderboard_id
        elif entry.beatleader_page_url:
            bl_leaderboard_id = entry.beatleader_page_url.rstrip("/").rsplit("/", 1)[-1]
        self._btn_preview_global1_replay.setProperty("leaderboard_id", bl_leaderboard_id)
        self._btn_preview_global1_replay.setProperty("countries", "")
        self._btn_preview_local1_replay.setProperty("leaderboard_id", bl_leaderboard_id)
        self._btn_preview_local1_replay.setProperty("countries", "JP")
        self._btn_preview_global1_replay.setEnabled(bool(bl_leaderboard_id or entry.beatleader_global1_replay_url))
        self._btn_preview_global1_replay.setProperty("url", entry.beatleader_global1_replay_url)
        self._btn_preview_local1_replay.setEnabled(bool(bl_leaderboard_id or entry.beatleader_local1_replay_url))
        self._btn_preview_local1_replay.setProperty("url", entry.beatleader_local1_replay_url)

        cover_url = entry.beatsaver_cover_url
        if not cover_url:
            self._preview_image.setPixmap(QPixmap())
            self._preview_image.setText("(no cover)")
            return
        self._current_preview_url = cover_url
        cached = self._preview_cache.get(cover_url)
        if cached is None:
            cached = _read_cover_cache(cover_url)
            if cached is not None:
                self._preview_cache[cover_url] = cached
        if cached is not None:
            self._cache_thumbnail_pixmap(cover_url, cached)
            self._set_preview_image(cached)
            return

        self._preview_token += 1
        token = self._preview_token
        self._preview_image.setPixmap(QPixmap())
        self._preview_image.setText("Loading cover...")

        def _task() -> None:
            try:
                resp = requests.get(cover_url, timeout=10)
                resp.raise_for_status()
                self._preview_signals.loaded.emit(token, cover_url, resp.content)
            except Exception as exc:  # noqa: BLE001
                self._preview_signals.error.emit(token, str(exc))

        threading.Thread(target=_task, daemon=True).start()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._update_preview_title_label()

    def _set_preview_image(self, data: bytes) -> None:
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self._preview_image.setPixmap(QPixmap())
            self._preview_image.setText("(failed to decode image)")
            return
        self._preview_image.setPixmap(
            pixmap.scaled(
                160,
                160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self._preview_image.setText("")

    def _on_preview_loaded(self, token: int, url: str, data: bytes) -> None:
        if token != self._preview_token or url != self._current_preview_url:
            return
        self._preview_cache[url] = data
        _write_cover_cache(url, data)
        self._cache_thumbnail_pixmap(url, data)
        self._set_preview_image(data)

    def _on_preview_error(self, token: int, msg: str) -> None:
        if token != self._preview_token:
            return
        self._preview_image.setPixmap(QPixmap())
        self._preview_image.setText(f"(cover load failed)\n{msg}")

    def _cache_thumbnail_pixmap(self, url: str, data: bytes) -> Optional[QPixmap]:
        cached = self._thumbnail_cache.get(url)
        if cached is not None:
            return cached
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return None
        thumb_edge = self._thumbnail_edge_size()
        thumb = pixmap.scaled(
            thumb_edge,
            thumb_edge,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._thumbnail_cache[url] = thumb
        return thumb

    def _set_cover_cell_thumbnail(self, label: QLabel, url: str) -> None:
        if not url:
            label.clear()
            return
        pixmap = self._thumbnail_cache.get(url)
        if pixmap is not None:
            self._set_cover_label_pixmap(label, pixmap)
            return
        cached_data = self._preview_cache.get(url)
        if cached_data is None:
            cached_data = _read_cover_cache(url)
            if cached_data is not None:
                self._preview_cache[url] = cached_data
        if cached_data is not None:
            pixmap = self._cache_thumbnail_pixmap(url, cached_data)
            if pixmap is not None:
                self._set_cover_label_pixmap(label, pixmap)
                return
        if url == self._thumbnail_active_url or url in self._thumbnail_pending:
            return
        self._thumbnail_pending.add(url)

        if url not in self._thumbnail_queue:
            self._thumbnail_queue.append(url)
        self._pump_thumbnail_queue()

    def _pump_thumbnail_queue(self) -> None:
        if self._thumbnail_active_url:
            return
        while self._thumbnail_queue:
            url = self._thumbnail_queue.pop(0)
            if url not in self._thumbnail_pending:
                continue
            self._thumbnail_active_url = url

            cached_data = _read_cover_cache(url)
            if cached_data is not None:
                self._thumbnail_signals.loaded.emit(url, cached_data)
                return

            def _task() -> None:
                try:
                    resp = requests.get(url, timeout=10)
                    resp.raise_for_status()
                    self._thumbnail_signals.loaded.emit(url, resp.content)
                except Exception as exc:  # noqa: BLE001
                    self._thumbnail_signals.error.emit(url, str(exc))

            threading.Thread(target=_task, daemon=True).start()
            return

    def _on_thumbnail_loaded(self, url: str, data: bytes) -> None:
        self._thumbnail_pending.discard(url)
        if self._thumbnail_active_url == url:
            self._thumbnail_active_url = ""
        self._preview_cache[url] = data
        _write_cover_cache(url, data)
        pixmap = self._cache_thumbnail_pixmap(url, data)
        if pixmap is None:
            self._pump_thumbnail_queue()
            return
        for table in (self._snapshot_table, self._maps_table):
            for row in range(table.rowCount()):
                cover_widget = table.cellWidget(row, _COL_COVER)
                if cover_widget is None:
                    continue
                image_label = cover_widget.findChild(QLabel)
                if image_label is not None and str(image_label.property("cover_url") or "") == url:
                    self._set_cover_label_pixmap(image_label, pixmap)
        self._pump_thumbnail_queue()

    def _on_thumbnail_error(self, url: str, _msg: str) -> None:
        self._thumbnail_pending.discard(url)
        if self._thumbnail_active_url == url:
            self._thumbnail_active_url = ""
        self._pump_thumbnail_queue()

    def _open_selected_preview_url(self) -> None:
        sender = self.sender()
        url = str(sender.property("url") if sender is not None else self._btn_preview_open.property("url") or "")
        if not url and sender in (self._btn_preview_global1_replay, self._btn_preview_local1_replay):
            leaderboard_id = str(sender.property("leaderboard_id") or "")
            countries = str(sender.property("countries") or "")
            if leaderboard_id:
                url = self._resolve_bl_top_replay_url(leaderboard_id, countries)
                sender.setProperty("url", url)
                entry = self._selected_entry()
                if entry is not None:
                    if sender is self._btn_preview_global1_replay:
                        entry.beatleader_global1_replay_url = url
                    else:
                        entry.beatleader_local1_replay_url = url
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _resolve_beatsaver_download_url(self, entry: MapEntry) -> str:
        url = str(entry.beatsaver_download_url or "").strip()
        if url.startswith(("http://", "https://")):
            return url
        if url.startswith("beatsaver://"):
            key = url.split("://", 1)[1].strip()
            if key:
                return f"https://beatsaver.com/api/download/key/{key}"
        key = str(entry.beatsaver_key or "").strip()
        if key:
            return f"https://beatsaver.com/api/download/key/{key}"
        return ""

    def _build_beatsaver_folder_name(self, entry: MapEntry) -> str:
        key = str(entry.beatsaver_key or "").strip() or "custom"
        title = re.sub(r'[\\/:*?"<>|]', '', (entry.song_name or '').strip())
        title = re.sub(r'\s+', ' ', title).strip(' .') or 'Unknown Song'
        return f"{key} ({title})"

    def _find_beatsaver_map_root(self, extracted_dir: Path) -> Optional[Path]:
        for info_name in ("Info.dat", "info.dat"):
            for info_path in extracted_dir.rglob(info_name):
                if info_path.is_file():
                    return info_path.parent
        return None

    def _should_refilter_for_install_state_change(self) -> bool:
        return self._rb_bs.isChecked() and (
            not self._cb_bs_not_downloaded.isChecked()
            or not self._cb_bs_downloaded.isChecked()
        )

    def _refresh_beatsaver_entry_install_state(self, target_entry: MapEntry) -> None:
        if self._should_refilter_for_install_state_change() or not (target_entry.song_hash or "").strip():
            self._apply_filter()
            self._restore_selected_entry(target_entry)
            self._update_preview_from_selection()
            return

        self._refresh_rows_for_hashes({target_entry.song_hash.upper()})
        self._update_selection_status()
        self._restore_selected_entry(target_entry)
        self._update_preview_from_selection()

    def _refresh_downloaded_state_after_install(self, target_entry: MapEntry, installed_dir: Optional[Path] = None) -> None:
        custom_levels_dir = self._custom_levels_dir()
        self._installed_beatsaber_dir = str(custom_levels_dir) if custom_levels_dir else ""
        beatsaver_key = str(target_entry.beatsaver_key or "").strip().lower()
        if beatsaver_key:
            self._installed_level_keys.add(beatsaver_key)
            if installed_dir is not None:
                self._installed_level_dirs[beatsaver_key] = installed_dir
        self._refresh_beatsaver_entry_install_state(target_entry)

    def _can_download_beatsaver_entry(self, entry: MapEntry) -> bool:
        return (
            entry.source == "beatsaver"
            and bool(self._resolve_beatsaver_download_url(entry))
            and not self._is_beatsaver_entry_installed(entry)
        )

    def _installed_beatsaver_map_dir(self, entry: MapEntry) -> Optional[Path]:
        beatsaver_key = str(entry.beatsaver_key or "").strip().lower()
        if not beatsaver_key:
            return None
        self._refresh_installed_levels_cache()
        if not self._installed_beatsaber_dir:
            return None
        return self._installed_level_dirs.get(beatsaver_key)

    def _can_delete_beatsaver_entry(self, entry: MapEntry) -> bool:
        return entry.source == "beatsaver" and self._installed_beatsaver_map_dir(entry) is not None

    def _download_beatsaver_entry(self, entry: MapEntry, show_dialogs: bool = True) -> bool:
        custom_levels_dir = self._custom_levels_dir()
        if not custom_levels_dir:
            if show_dialogs:
                QMessageBox.warning(self, "OneClickDownload", "Beat Saber フォルダが設定されていません。Settings で設定してください。")
            return False

        download_url = self._resolve_beatsaver_download_url(entry)
        if not download_url:
            if show_dialogs:
                QMessageBox.warning(self, "OneClickDownload", "ダウンロード URL を取得できませんでした。")
            return False

        if self._is_beatsaver_entry_installed(entry):
            self._refresh_downloaded_state_after_install(entry)
            if show_dialogs:
                QMessageBox.information(self, "OneClickDownload", "この譜面はすでに Beat Saber に入っています。")
            return True

        target_dir = custom_levels_dir / self._build_beatsaver_folder_name(entry)
        try:
            custom_levels_dir.mkdir(parents=True, exist_ok=True)
            response = requests.get(download_url, timeout=60)
            response.raise_for_status()

            with tempfile.TemporaryDirectory(prefix="mbss_bsdl_") as tmp_dir_str:
                tmp_dir = Path(tmp_dir_str)
                archive_path = tmp_dir / "map.zip"
                archive_path.write_bytes(response.content)

                extract_dir = tmp_dir / "extract"
                extract_dir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(archive_path) as zip_file:
                    zip_file.extractall(extract_dir)

                map_root = self._find_beatsaver_map_root(extract_dir)
                if map_root is None:
                    raise RuntimeError("譜面 zip の中に Info.dat が見つかりませんでした。")
                if target_dir.exists():
                    raise RuntimeError(f"保存先フォルダがすでに存在します: {target_dir.name}")
                shutil.copytree(map_root, target_dir)
        except Exception as exc:
            if show_dialogs:
                QMessageBox.critical(self, "OneClickDownload", f"譜面のダウンロードに失敗しました。\n\n{exc}")
            return False

        self._refresh_downloaded_state_after_install(entry, installed_dir=target_dir)
        return True

    def _delete_beatsaver_entry(self, entry: MapEntry, show_dialogs: bool = False) -> bool:
        target_dir = self._installed_beatsaver_map_dir(entry)
        if target_dir is None:
            self._refresh_installed_levels_cache(force=True)
            self._refresh_beatsaver_entry_install_state(entry)
            return False

        try:
            shutil.rmtree(target_dir)
        except Exception as exc:
            if show_dialogs:
                QMessageBox.critical(self, "Delete Map", f"譜面の削除に失敗しました。\n\n{exc}")
            return False

        beatsaver_key = str(entry.beatsaver_key or "").strip().lower()
        if beatsaver_key:
            self._installed_level_keys.discard(beatsaver_key)
            self._installed_level_dirs.pop(beatsaver_key, None)
        self._refresh_beatsaver_entry_install_state(entry)
        return True

    def _download_selected_preview_entry(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            QMessageBox.information(self, "OneClickDownload", "譜面が選択されていません。")
            return
        self._download_beatsaver_entry(entry)

    def _download_selected_entries(self) -> None:
        entries = [
            entry for entry in self._selected_entries()
            if self._can_download_beatsaver_entry(entry)
        ]
        if not entries:
            QMessageBox.information(self, "Download", "ダウンロード可能な譜面が選択されていません。")
            return

        success_count = 0
        failed_count = 0
        for entry in entries:
            if self._download_beatsaver_entry(entry, show_dialogs=False):
                success_count += 1
            else:
                failed_count += 1

        if failed_count:
            QMessageBox.warning(
                self,
                "Download",
                f"{success_count} maps downloaded, {failed_count} failed.",
            )
        else:
            QMessageBox.information(
                self,
                "Download",
                f"{success_count} maps downloaded.",
            )

    def _setWindowTitle_source(self, src: str) -> None:
        self.setWindowTitle(f"Playlist / Maps - {src}")

    def _open_batch_export(self) -> None:
        pass  # 互換性のため残す（右パネルは常時表示）

    def _load_export_dir(self) -> str:
        """前回のエクスポート先フォルダを読み込む。"""
        return load_playlist_export_dir()

    def _save_export_dir(self, folder: str) -> None:
        """エクスポート先フォルダを保存する。"""
        self._export_dir = folder
        save_playlist_export_dir(folder)

    def _batch_load_configs(self) -> "List[_BatchConfig]":
        """保存済みバッチ設定を読み込む。"""
        return load_playlist_batch_configs()

    def _batch_save_configs(self) -> None:
        """バッチ設定をファイルに保存する。"""
        try:
            _BATCH_CONFIG_PATH.write_text(
                json.dumps([c.to_dict() for c in self._batch_configs], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _batch_set_all_enabled(self, enabled: bool) -> None:
        """\u3059\u3079\u3066\u306e\u30d0\u30c3\u30c1\u8a2d\u5b9a\u306e enabled \u3092\u4e00\u62ec\u5909\u66f4\u3057\u3066\u4fdd\u5b58\u3059\u308b\u3002"""
        for cfg in self._batch_configs:
            cfg.enabled = enabled
        self._batch_save_configs()
        self._batch_refresh_queue()

    def _batch_refresh_queue(self) -> None:
        """バッチキューの表示を更新する。"""
        self._batch_queue_list.blockSignals(True)
        self._batch_queue_list.clear()
        enabled_count = 0
        for cfg in self._batch_configs:
            item = QListWidgetItem(cfg.display_text())
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if cfg.enabled else Qt.CheckState.Unchecked)
            self._batch_queue_list.addItem(item)
            if cfg.enabled:
                enabled_count += 1
        self._batch_count_label.setText(f"{enabled_count} item{'s' if enabled_count != 1 else ''}")
        self._batch_queue_list.blockSignals(False)
        self._update_batch_queue_actions()

    def _update_batch_queue_actions(self) -> None:
        selected_count = len(self._batch_queue_list.selectedItems())
        self._btn_bq_load.setEnabled(selected_count == 1)
        self._btn_bq_remove.setEnabled(selected_count >= 1)

    def _batch_restore_selected(self) -> None:
        rows = sorted({self._batch_queue_list.row(item) for item in self._batch_queue_list.selectedItems()})
        if len(rows) != 1:
            return
        row = rows[0]
        if not (0 <= row < len(self._batch_configs)):
            return
        cfg = self._batch_configs[row]

        radio_map = {
            "ss": self._rb_ss,
            "bl": self._rb_bl,
            "acc": self._rb_acc,
            "rl": self._rb_acc_rl,
            "bs": self._rb_bs,
        }
        source_button = radio_map.get(cfg.source)
        if source_button is None:
            QMessageBox.information(self, "Batch Load", f"Unsupported source: {cfg.source}")
            return

        self._reset_filters()
        source_button.setChecked(True)

        widgets = [
            self._search_edit,
            self._star_min,
            self._star_max,
            self._cb_sts_cleared,
            self._cb_sts_nf,
            self._cb_sts_unplayed,
            self._cb_sts_queued,
            self._cb_cat_true,
            self._cb_cat_standard,
            self._cb_cat_tech,
            self._bs_query_edit,
            self._bs_max_maps,
            self._bs_days,
            self._bs_from_date,
            self._bs_to_date,
            self._bs_min_rating,
            self._bs_filter_min_rating,
            self._bs_min_votes,
            self._bs_filter_min_votes,
            self._cb_bs_unranked,
            self._cb_bs_no_ai,
            self._rb_exp_single,
            self._rb_exp_split,
        ]
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self._search_edit.setText(cfg.song_filter)
            self._star_min.setValue(cfg.star_min)
            self._star_max.setValue(cfg.star_max)
            self._cb_sts_cleared.setChecked(cfg.show_cleared)
            self._cb_sts_nf.setChecked(cfg.show_nf)
            self._cb_sts_unplayed.setChecked(cfg.show_unplayed)
            self._cb_sts_queued.setChecked(cfg.show_queued)
            self._cb_cat_true.setChecked(cfg.cat_true)
            self._cb_cat_standard.setChecked(cfg.cat_standard)
            self._cb_cat_tech.setChecked(cfg.cat_tech)
            self._bs_query_edit.setText(cfg.bs_query)
            self._bs_max_maps.setValue(cfg.bs_max_maps)
            self._bs_days.setValue(cfg.bs_days)
            if cfg.bs_to_date:
                self._bs_to_date.setDate(QDate.fromString(cfg.bs_to_date, "yyyy-MM-dd"))
            if cfg.bs_from_date:
                self._bs_from_date.setDate(QDate.fromString(cfg.bs_from_date, "yyyy-MM-dd"))
            self._set_bs_rating_value(cfg.bs_min_rating)
            self._set_bs_votes_value(cfg.bs_min_votes)
            self._cb_bs_unranked.setChecked(cfg.bs_unranked_only)
            self._cb_bs_no_ai.setChecked(cfg.bs_exclude_ai)
            self._rb_exp_single.setChecked(cfg.split_mode == "single")
            self._rb_exp_split.setChecked(cfg.split_mode != "single")
            sort_col, sort_order = _sort_indicator_from_mode(cfg.sort_mode)
            self._table.horizontalHeader().setSortIndicator(sort_col, sort_order)
        finally:
            for widget in widgets:
                widget.blockSignals(False)

        self._update_sort_label()
        self._load_data(reset_filters=False)

    def _on_batch_item_changed(self, item: QListWidgetItem) -> None:
        """チェックボックスの変化を _BatchConfig.enabled に反映して保存する。"""
        row = self._batch_queue_list.row(item)
        if 0 <= row < len(self._batch_configs):
            self._batch_configs[row].enabled = (item.checkState() == Qt.CheckState.Checked)
            self._batch_save_configs()
            enabled_count = sum(1 for cfg in self._batch_configs if cfg.enabled)
            self._batch_count_label.setText(f"{enabled_count} item{'s' if enabled_count != 1 else ''}")

    def _batch_remove_selected(self) -> None:
        rows = sorted(
            {self._batch_queue_list.row(s) for s in self._batch_queue_list.selectedItems()},
            reverse=True,
        )
        for r in rows:
            del self._batch_configs[r]
        self._batch_save_configs()
        self._batch_refresh_queue()

    def _batch_clear(self) -> None:
        if not self._batch_configs:
            return
        ans = QMessageBox.question(
            self, "Clear Queue",
            f"Clear all {len(self._batch_configs)} items from the queue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._batch_configs.clear()
        self._batch_save_configs()
        self._batch_refresh_queue()

    def _add_to_batch(self) -> None:
        """現在のフィルタ条件をバッチキューに追加する。"""
        if not self._all_entries:
            QMessageBox.information(self, "Add to Batch", "No data loaded.")
            return

        search_text = self._search_edit.text().strip()

        split = self._rb_exp_split.isChecked()
        is_acc_any = self._rb_acc.isChecked() or self._rb_acc_rl.isChecked()

        if self._rb_ss.isChecked():
            src_tag = "ss"
        elif self._rb_bl.isChecked():
            src_tag = "bl"
        elif self._rb_acc.isChecked():
            src_tag = "acc"
        elif self._rb_bs.isChecked():
            src_tag = "bs"
        elif self._rb_open.isChecked():
            svc = self._svc_combo.currentData()
            src_tag = {"scoresaber": "ss", "beatleader": "bl", "accsaber_rl": "rl"}.get(svc, "pl")
        else:
            src_tag = "rl"
        src_label = _BATCH_SRC_PREFIX.get(src_tag, src_tag.upper())
        display_style = ("cat" if is_acc_any else "split") if split else ""
        filename_base = ""
        name = "_".join(p for p in [src_label, display_style] if p)

        split_mode = ("category" if is_acc_any else "star") if split else "single"
        sort_mode = self._current_sort_mode()

        cfg = _BatchConfig(
            label=name,
            filename_base=filename_base,
            source=src_tag,
            show_cleared=self._cb_sts_cleared.isChecked(),
            show_nf=self._cb_sts_nf.isChecked(),
            show_unplayed=self._cb_sts_unplayed.isChecked(),
            show_queued=self._cb_sts_queued.isChecked(),
            cat_true=self._cb_cat_true.isChecked() if is_acc_any else True,
            cat_standard=self._cb_cat_standard.isChecked() if is_acc_any else True,
            cat_tech=self._cb_cat_tech.isChecked() if is_acc_any else True,
            star_min=self._star_min.value(),
            star_max=self._star_max.value(),
            split_mode=split_mode,
            sort_mode=sort_mode,
            song_filter=search_text,
            bs_query=self._bs_query_edit.text().strip() if src_tag == "bs" else "",
            bs_from_date=self._bs_from_date.date().toString("yyyy-MM-dd") if src_tag == "bs" else "",
            bs_to_date=self._bs_to_date.date().toString("yyyy-MM-dd") if src_tag == "bs" else "",
            bs_days=self._bs_days.value() if src_tag == "bs" else 7,
            bs_max_maps=self._bs_max_maps.value() if src_tag == "bs" else 1000,
            bs_min_rating=self._bs_min_rating.value() if src_tag == "bs" else 50,
            bs_min_votes=self._bs_min_votes.value() if src_tag == "bs" else 0,
            bs_unranked_only=self._cb_bs_unranked.isChecked() if src_tag == "bs" else True,
            bs_exclude_ai=self._cb_bs_no_ai.isChecked() if src_tag == "bs" else True,
        )
        self._batch_configs.append(cfg)
        self._batch_save_configs()
        self._batch_refresh_queue()

    def _batch_add_presets(self) -> None:
        """チェックされたプリセットをバッチキューに追加する（即時・データロード不要）。同一設定は追加しない。"""
        checked: List[_BatchPreset] = []
        for i in range(self._preset_list_w.count()):
            it = self._preset_list_w.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                checked.append(it.data(Qt.ItemDataRole.UserRole))
        if not checked:
            QMessageBox.information(self, "Add Presets", "No presets checked.")
            return

        added = 0
        for p in checked:
            src_pfx = _BATCH_SRC_PREFIX.get(p.source, p.source.upper())
            if p.source in ("rl", "acc"):
                if p.split_by_star and not p.rl_cat:
                    split_mode = "category"
                    cat_true = cat_standard = cat_tech = True
                else:
                    split_mode = "single"
                    cat_true = p.rl_cat == "true"
                    cat_standard = p.rl_cat == "standard"
                    cat_tech = p.rl_cat == "tech"
            else:
                cat_true = cat_standard = cat_tech = True
                split_mode = "star" if p.split_by_star else "single"
            split_code = {"star": "split", "category": "cat", "single": "single"}.get(split_mode, split_mode)
            batch_label = f"{src_pfx}_{split_code}"

            cfg = _BatchConfig(
                label=batch_label,
                filename_base=p.filename_base,
                source=p.source,
                show_cleared=not p.uncleared,
                show_nf=True,
                show_unplayed=True,
                show_queued=False,
                cat_true=cat_true,
                cat_standard=cat_standard,
                cat_tech=cat_tech,
                split_mode=split_mode,
                sort_mode=p.sort_mode,
            )
            # 同一設定が已存在する場合はスキップ
            def _is_duplicate(existing: _BatchConfig, new: _BatchConfig) -> bool:
                return (
                    existing.source == new.source and
                    existing.split_mode == new.split_mode and
                    existing.sort_mode == new.sort_mode and
                    existing.show_cleared == new.show_cleared and
                    existing.show_nf == new.show_nf and
                    existing.show_unplayed == new.show_unplayed and
                    existing.show_queued == new.show_queued and
                    existing.cat_true == new.cat_true and
                    existing.cat_standard == new.cat_standard and
                    existing.cat_tech == new.cat_tech and
                    existing.bs_query == new.bs_query and
                    existing.bs_from_date == new.bs_from_date and
                    existing.bs_to_date == new.bs_to_date and
                    existing.bs_days == new.bs_days and
                    existing.bs_max_maps == new.bs_max_maps and
                    existing.bs_min_rating == new.bs_min_rating and
                    existing.bs_min_votes == new.bs_min_votes and
                    existing.bs_unranked_only == new.bs_unranked_only and
                    existing.bs_exclude_ai == new.bs_exclude_ai
                )
            if any(_is_duplicate(e, cfg) for e in self._batch_configs):
                continue
            self._batch_configs.append(cfg)
            added += 1

        if added == 0:
            QMessageBox.information(self, "Add Presets", "All selected presets are already in the queue.")
            return
        self._batch_save_configs()
        self._batch_refresh_queue()

    def _on_export_progress(self, done: int, total: int, label: str) -> None:
        if self._batch_progress_dlg and not self._batch_progress_dlg.wasCanceled():
            if total > 0:
                self._batch_progress_dlg.setMaximum(total)
                self._batch_progress_dlg.setValue(done)
            self._batch_progress_dlg.setLabelText(label)

    def _on_export_finished(self, result: list) -> None:
        if self._batch_progress_dlg:
            self._batch_progress_dlg.close()
            self._batch_progress_dlg = None
        self._btn_batch_export_all.setEnabled(True)
        self._btn_quick_export.setEnabled(True)
        saved: List[str] = result[0]
        errors: List[str] = result[1]
        self._show_bplist_covers_dialog(
            f"Export Complete — {len(saved)} file(s)",
            self._export_dir,
            saved,
            errors,
        )

    def _on_export_error(self, msg: str) -> None:
        if self._batch_progress_dlg:
            self._batch_progress_dlg.close()
            self._batch_progress_dlg = None
        self._btn_batch_export_all.setEnabled(True)
        self._btn_quick_export.setEnabled(True)
        QMessageBox.critical(self, "Export Error", msg)

    def _quick_export_presets(self) -> None:
        """チェックされたプリセットをキューに追加せず直接エクスポートする。"""
        checked: List[_BatchPreset] = []
        for i in range(self._preset_list_w.count()):
            it = self._preset_list_w.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                checked.append(it.data(Qt.ItemDataRole.UserRole))
        if not checked:
            QMessageBox.information(self, "Quick Export", "No presets checked.")
            return

        folder = QFileDialog.getExistingDirectory(self, "Select output folder", self._export_dir)
        if not folder:
            return
        self._save_export_dir(folder)

        configs: List[_BatchConfig] = []
        for p in checked:
            src_pfx = _BATCH_SRC_PREFIX.get(p.source, p.source.upper())
            if p.source in ("rl", "acc"):
                if p.split_by_star and not p.rl_cat:
                    split_mode = "category"
                    cat_true = cat_standard = cat_tech = True
                else:
                    split_mode = "single"
                    cat_true = p.rl_cat == "true"
                    cat_standard = p.rl_cat == "standard"
                    cat_tech = p.rl_cat == "tech"
            else:
                cat_true = cat_standard = cat_tech = True
                split_mode = "star" if p.split_by_star else "single"
            split_code = {"star": "split", "category": "cat", "single": "single"}.get(split_mode, split_mode)
            batch_label = f"{src_pfx}_{split_code}"
            configs.append(_BatchConfig(
                label=batch_label,
                filename_base=p.filename_base,
                source=p.source,
                show_cleared=not p.uncleared,
                show_nf=True,
                show_unplayed=True,
                show_queued=False,
                cat_true=cat_true,
                cat_standard=cat_standard,
                cat_tech=cat_tech,
                split_mode=split_mode,
                sort_mode=p.sort_mode,
            ))

        folder_path = Path(folder)
        steam_id = self._steam_id
        sigs = self._export_sigs

        dlg = QProgressDialog("Starting...", "Cancel", 0, len(configs), self)
        dlg.setWindowTitle("Quick Export")
        dlg.setMinimumWidth(420)
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.show()
        self._batch_progress_dlg = dlg
        self._btn_quick_export.setEnabled(False)

        def _task() -> None:
            try:
                covers = _pregenerate_covers(configs)
                _run_export_configs(sigs, steam_id, configs, folder_path, covers)
            except Exception as exc:
                sigs.error.emit(str(exc))

        dlg.canceled.connect(lambda: self._btn_quick_export.setEnabled(True))
        threading.Thread(target=_task, daemon=True).start()

    def _batch_export_all(self) -> None:
        """バッチ設定リストの最新データをロードして一括エクスポートする（非同期）。"""
        configs = [c for c in self._batch_configs if c.enabled]
        if not configs:
            QMessageBox.information(self, "Export All",
                "No items checked in batch queue." if self._batch_configs else "Batch queue is empty.")
            return

        folder = QFileDialog.getExistingDirectory(self, "Select output folder", self._export_dir)
        if not folder:
            return
        self._save_export_dir(folder)

        folder_path = Path(folder)
        steam_id = self._steam_id
        sigs = self._export_sigs

        dlg = QProgressDialog("Starting...", "Cancel", 0, len(configs), self)
        dlg.setWindowTitle("Batch Export")
        dlg.setMinimumWidth(420)
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.show()
        self._batch_progress_dlg = dlg
        self._btn_batch_export_all.setEnabled(False)

        def _task() -> None:
            try:
                covers = _pregenerate_covers(configs)
                _run_export_configs(sigs, steam_id, configs, folder_path, covers)
            except Exception as exc:
                sigs.error.emit(str(exc))

        dlg.canceled.connect(lambda: self._btn_batch_export_all.setEnabled(True))
        threading.Thread(target=_task, daemon=True).start()

    # ──────────────────────────────────────────────────────────────────────────
    # カバー画像プレビュー
    # ──────────────────────────────────────────────────────────────────────────

    def _show_bplist_covers_dialog(
        self,
        title: str,
        folder: str,
        filenames: List[str],
        errors: List[str],
    ) -> None:
        show_bplist_covers_dialog(self, title, folder, filenames, errors)

    def _show_cover_preview(self) -> None:
        """出力フォルダを選択して .bplist ファイルのカバー画像を一覧表示する。"""
        folder = QFileDialog.getExistingDirectory(
            self, "Select export folder to preview", self._export_dir
        )
        if not folder:
            return
        bplist_files = sorted(f.name for f in Path(folder).glob("*.bplist"))
        if not bplist_files:
            QMessageBox.information(self, "Preview", "No .bplist files found in the selected folder.")
            return
        self._show_bplist_covers_dialog(f"Cover Preview — {Path(folder).name}", folder, bplist_files, [])

    def _scroll_table_to_top(self) -> None:
        self._table.scrollToTop()

    def _scroll_table_to_bottom(self) -> None:
        self._table.scrollToBottom()

    def _custom_levels_dir(self) -> Path:
        beatsaber_dir = load_beatsaber_dir().strip()
        if not beatsaber_dir:
            return Path()
        return Path(beatsaber_dir) / "Beat Saber_Data" / "CustomLevels"

    def _refresh_installed_levels_cache(self, force: bool = False) -> None:
        custom_levels_dir = self._custom_levels_dir()
        cache_key = str(custom_levels_dir)
        if not force and cache_key == self._installed_beatsaber_dir:
            return

        installed_keys: set[str] = set()
        installed_dirs: Dict[str, Path] = {}
        if custom_levels_dir.is_dir():
            try:
                for child in custom_levels_dir.iterdir():
                    if not child.is_dir():
                        continue
                    match = re.match(r"^([0-9A-Za-z]+)(?:\s*[\(\[]|$)", child.name.strip())
                    if match:
                        beatsaver_key = match.group(1).lower()
                        installed_keys.add(beatsaver_key)
                        installed_dirs[beatsaver_key] = child
            except Exception:
                installed_keys = set()
                installed_dirs = {}

        self._installed_beatsaber_dir = cache_key
        self._installed_level_keys = installed_keys
        self._installed_level_dirs = installed_dirs

    def _is_beatsaver_entry_installed(self, entry: MapEntry) -> bool:
        beatsaver_key = str(entry.beatsaver_key or "").strip().lower()
        if not beatsaver_key:
            return False
        self._refresh_installed_levels_cache()
        if not self._installed_beatsaber_dir:
            return False
        return beatsaver_key in self._installed_level_keys

    def _make_oneclick_button(self, entry: MapEntry) -> QWidget:
        button = QPushButton("")
        button.setIcon(QIcon(str(RESOURCES_DIR / "onclick_download.png")))
        icon_edge = max(26, min(self._row_height - 6, 30))
        button.setIconSize(QSize(icon_edge, icon_edge))
        button.setFixedWidth(34)
        button.setFixedHeight(max(28, self._row_height))
        button.setFlat(True)
        button.setStyleSheet(
            "QPushButton { padding: 0px; border: none; background: transparent; }"
            "QPushButton:disabled { border: none; background: transparent; }"
        )
        button.clicked.connect(lambda _checked=False, current_entry=entry: self._download_beatsaver_entry(current_entry))

        has_download = bool(entry.beatsaver_download_url)
        has_beatsaber_dir = bool(load_beatsaber_dir().strip())
        installed = has_download and self._is_beatsaver_entry_installed(entry)
        button.setEnabled(has_download and not installed)
        if not has_download:
            button.setToolTip("OneClickDownload unavailable")
        elif installed:
            button.setToolTip("Already installed in Beat Saber")
        elif not has_beatsaber_dir:
            button.setToolTip("OneClickDownload (Beat Saber folder not set; install state not checked)")
        else:
            button.setToolTip("OneClickDownload")
        container = QWidget()
        container.setProperty("mbss_cell_widget", True)
        container.setStyleSheet("background: transparent;")
        container.setFixedHeight(self._row_height)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignCenter)
        return container

    def _make_delete_button(self, entry: MapEntry) -> QWidget:
        button = QPushButton("")
        button.setIcon(QIcon(str(RESOURCES_DIR / "trash.png")))
        icon_edge = max(18, min(self._row_height - 6, 30))
        button.setIconSize(QSize(icon_edge, icon_edge))
        button.setFixedWidth(34)
        button.setFixedHeight(max(22, self._row_height))
        button.setFlat(True)
        button.setStyleSheet(
            "QPushButton { padding: 0px; border: none; background: transparent; }"
            "QPushButton:disabled { border: none; background: transparent; }"
        )
        button.clicked.connect(lambda _checked=False, current_entry=entry: self._delete_beatsaver_entry(current_entry))

        installed = self._can_delete_beatsaver_entry(entry)
        button.setEnabled(installed)
        button.setToolTip("Delete from Beat Saber" if installed else "Delete unavailable")
        container = QWidget()
        container.setProperty("mbss_cell_widget", True)
        container.setStyleSheet("background: transparent;")
        container.setFixedHeight(self._row_height)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignCenter)
        return container

    def _thumbnail_edge_size(self) -> int:
        return max(28, min(self._row_height, 128))

    def _set_cover_label_pixmap(self, label: QLabel, pixmap: QPixmap) -> None:
        edge = self._thumbnail_edge_size()
        label.setFixedSize(edge, edge)
        label.setPixmap(pixmap)
        label.setText("")

    def _make_cover_cell_widget(self, entry: MapEntry) -> QWidget:
        container = QWidget()
        container.setProperty("mbss_cell_widget", True)
        container.setStyleSheet("background: transparent;")
        container.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        image_label = QLabel()
        image_label.setProperty("cover_url", entry.beatsaver_cover_url)
        image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        edge = self._thumbnail_edge_size()
        image_label.setFixedSize(edge, edge)
        image_label.setStyleSheet("background: transparent;")
        layout.addWidget(image_label, 0, Qt.AlignmentFlag.AlignCenter)

        self._set_cover_cell_thumbnail(image_label, entry.beatsaver_cover_url)
        return container

    def _apply_row_height(self, refresh_table: bool = True) -> None:
        for table in (self._snapshot_table, self._maps_table):
            header = table.verticalHeader()
            header.setMinimumSectionSize(0)
            header.setDefaultSectionSize(self._row_height)
            header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
            table.setColumnWidth(_COL_COVER, self._thumbnail_edge_size())
        if not refresh_table or not self._filtered:
            return
        selected_entry = self._selected_entry()
        self._thumbnail_cache.clear()
        self._refresh_table(self._filtered)
        self._restore_selected_entry(selected_entry)

    def _restore_selected_entry(self, target: Optional[MapEntry]) -> None:
        if target is None:
            return
        if self._table_render_active:
            self._pending_restore_entry = target
            return
        for row in range(self._table.rowCount()):
            item = self._table.item(row, _COL_SONG)
            if item is None:
                continue
            if item.data(Qt.ItemDataRole.UserRole) is target:
                self._table.selectRow(row)
                self._table.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)
                return

    def _refresh_rows_for_hashes(self, song_hashes: set[str]) -> None:
        if not song_hashes:
            return
        for table in (self._snapshot_table, self._maps_table):
            for row in range(table.rowCount()):
                item = table.item(row, _COL_SONG)
                if item is None:
                    continue
                entry = item.data(Qt.ItemDataRole.UserRole)
                if not isinstance(entry, MapEntry):
                    continue
                if (entry.song_hash or "").upper() not in song_hashes:
                    continue
                if not table.isColumnHidden(_COL_COVER):
                    table.setCellWidget(row, _COL_COVER, self._make_cover_cell_widget(entry))
                if not table.isColumnHidden(_COL_ONECLICK):
                    oneclick_sort_val = 1.0 if self._is_beatsaver_entry_installed(entry) else 0.0
                    oneclick_item = _NumItem("", oneclick_sort_val)
                    oneclick_item.setToolTip("Downloaded" if oneclick_sort_val > 0 else "Not downloaded")
                    table.setItem(row, _COL_ONECLICK, oneclick_item)
                    table.setCellWidget(row, _COL_ONECLICK, self._make_oneclick_button(entry))
                if not table.isColumnHidden(_COL_DELETE):
                    delete_sort_val = 1.0 if self._can_delete_beatsaver_entry(entry) else 0.0
                    delete_item = _NumItem("", delete_sort_val)
                    delete_item.setToolTip("Installed" if delete_sort_val > 0 else "Not installed")
                    table.setItem(row, _COL_DELETE, delete_item)
                    table.setCellWidget(row, _COL_DELETE, self._make_delete_button(entry))

    def _hydrate_visible_row_widgets(self, table: Optional[QTableWidget] = None) -> None:
        target_table = self._table if table is None else table
        row_count = target_table.rowCount()
        if row_count <= 0:
            return
        if not target_table.isVisible() or target_table.viewport().height() <= 0:
            return
        top_row = target_table.rowAt(0)
        if top_row < 0:
            top_row = 0
        bottom_row = target_table.rowAt(target_table.viewport().height() - 1)
        if bottom_row < 0:
            bottom_row = row_count - 1
        start_row = max(0, top_row - 4)
        end_row = min(row_count - 1, bottom_row + 4)
        show_cover = not target_table.isColumnHidden(_COL_COVER)
        show_oneclick = not target_table.isColumnHidden(_COL_ONECLICK)
        show_delete = not target_table.isColumnHidden(_COL_DELETE)

        for row in range(start_row, end_row + 1):
            item = target_table.item(row, _COL_SONG)
            if item is None:
                continue
            entry = item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(entry, MapEntry):
                continue
            if show_cover and target_table.cellWidget(row, _COL_COVER) is None:
                target_table.setCellWidget(row, _COL_COVER, self._make_cover_cell_widget(entry))
            if show_oneclick and target_table.cellWidget(row, _COL_ONECLICK) is None:
                target_table.setCellWidget(row, _COL_ONECLICK, self._make_oneclick_button(entry))
            if show_delete and target_table.cellWidget(row, _COL_DELETE) is None:
                target_table.setCellWidget(row, _COL_DELETE, self._make_delete_button(entry))

    def _on_row_height_up(self) -> None:
        self._row_height = min(self._row_height + 4, 64)
        self._apply_row_height(refresh_table=True)
        self._save_window_state()

    def _on_row_height_dn(self) -> None:
        self._row_height = max(self._row_height - 4, 18)
        self._apply_row_height(refresh_table=True)
        self._save_window_state()

    # ──────────────────────────────────────────────────────────────────────────
    # フィルタ
    # ──────────────────────────────────────────────────────────────────────────

    def _apply_filter(self) -> None:
        """フィルタ条件に従ってテーブルを更新する。"""
        text = self._search_edit.text().strip().lower()
        keywords = text.split() if text else []
        star_min = self._star_min.value()
        star_max = self._star_max.value()
        show_cleared = self._cb_sts_cleared.isChecked()
        show_nf = self._cb_sts_nf.isChecked()
        show_unplayed = self._cb_sts_unplayed.isChecked()
        show_bs_not_downloaded = self._cb_bs_not_downloaded.isChecked()
        show_bs_downloaded = self._cb_bs_downloaded.isChecked()
        min_bs_rating = self._bs_filter_min_rating.value()
        min_bs_votes = self._bs_filter_min_votes.value()
        highest_diff_only = self._cb_top_diff_only.isChecked()
        show_queued = self._cb_sts_queued.isChecked() and (
            self._rb_acc_rl.isChecked() or (
                self._rb_open.isChecked() and self._svc_combo.currentData() == "accsaber_rl"
            )
        )
        rl_mode = self._rb_acc.isChecked() or self._rb_acc_rl.isChecked()
        cat_filter: Optional[set] = None
        if rl_mode:
            allowed: set = set()
            if self._cb_cat_true.isChecked():
                allowed.add("true")
            if self._cb_cat_standard.isChecked():
                allowed.add("standard")
            if self._cb_cat_tech.isChecked():
                allowed.add("tech")
            cat_filter = allowed

        result: List[MapEntry] = []
        for e in self._all_entries:
            # 星フィルタ
            if e.stars < star_min or e.stars >= star_max:
                continue
            # テキストフィルタ
            if keywords:
                targets = (e.song_name.lower(), e.song_author.lower(), e.mapper.lower())
                if not all(any(kw in t for t in targets) for kw in keywords):
                    continue
            if self._rb_bs.isChecked():
                if e.player_pp < min_bs_rating:
                    continue
                if e.beatsaver_votes < min_bs_votes:
                    continue
                is_downloaded = self._is_beatsaver_entry_installed(e)
                if is_downloaded and not show_bs_downloaded:
                    continue
                if not is_downloaded and not show_bs_not_downloaded:
                    continue
            # ステータスフィルタ
            if e.pending:
                if not show_queued:
                    continue
            else:
                if e.cleared and not show_cleared:
                    continue
                if e.nf_clear and not show_nf:
                    continue
                if not e.played and not show_unplayed:
                    continue
            # カテゴリフィルタ (AccSaber RL)
            if cat_filter is not None and e.acc_category not in cat_filter:
                continue
            result.append(e)

        if highest_diff_only:
            best_by_key: Dict[Tuple[str, str], Tuple[int, float, MapEntry]] = {}
            for entry in result:
                song_key = entry.song_hash.upper() or "\t".join([
                    entry.song_name,
                    entry.song_author,
                    entry.mapper,
                ]).lower()
                key = (song_key, entry.mode or "")
                candidate = (_DIFF_ORDER.get(entry.difficulty, 0), entry.stars, entry)
                current = best_by_key.get(key)
                if current is None or candidate[:2] > current[:2]:
                    best_by_key[key] = candidate
            result = [
                entry for entry in result
                if best_by_key[(entry.song_hash.upper() or "\t".join([
                    entry.song_name,
                    entry.song_author,
                    entry.mapper,
                ]).lower(), entry.mode or "")][2] is entry
            ]

        self._filtered = result
        self._sync_active_table_state()
        self._count_label.setText(f"{len(result):,} maps")
        self._refresh_table(result)

    # ──────────────────────────────────────────────────────────────────────────
    # テーブル更新
    # ──────────────────────────────────────────────────────────────────────────

    def _update_selection_status(self) -> None:
        selected_entries = self._selected_entries()
        selected_rows = len(selected_entries)
        self._selection_status_label.setText(
            f"{selected_rows} row{'s' if selected_rows != 1 else ''} selected"
        )
        self._btn_download_selected.setEnabled(any(self._can_download_beatsaver_entry(entry) for entry in selected_entries))

    def _populate_table_row(
        self,
        table: QTableWidget,
        row: int,
        e: MapEntry,
        cleared_bg: QColor,
        nf_bg: QColor,
        unplayed_bg: QColor,
        is_acc_mode: bool,
        is_bs_mode: bool,
    ) -> None:
        status_val = 30 if e.cleared else 20 if e.nf_clear else 10
        status_item = _NumItem(e.status_str, float(status_val))
        status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if e.cleared:
            status_item.setBackground(cleared_bg)
        elif e.nf_clear:
            status_item.setBackground(nf_bg)
        else:
            status_item.setBackground(unplayed_bg)
        table.setItem(row, _COL_STATUS, status_item)

        song_item = QTableWidgetItem(e.song_name)
        song_item.setData(Qt.ItemDataRole.UserRole, e)
        table.setItem(row, _COL_SONG, song_item)

        oneclick_sort_val = 1.0 if self._is_beatsaver_entry_installed(e) else 0.0
        oneclick_item = _NumItem("", oneclick_sort_val)
        oneclick_item.setToolTip("Downloaded" if oneclick_sort_val > 0 else "Not downloaded")
        table.setItem(row, _COL_ONECLICK, oneclick_item)
        delete_sort_val = 1.0 if self._can_delete_beatsaver_entry(e) else 0.0
        delete_item = _NumItem("", delete_sort_val)
        delete_item.setToolTip("Installed" if delete_sort_val > 0 else "Not installed")
        table.setItem(row, _COL_DELETE, delete_item)
        has_bl_stats_source = bool(e.leaderboard_id and e.source in ("beatleader", "beatsaver"))
        table.setItem(
            row,
            _COL_SOURCE_DATE,
            _source_date_item(e.source_date_ts, include_time=(e.source == "beatsaver")),
        )
        table.setItem(row, _COL_DURATION, _duration_item(e.duration_seconds))

        played_at_item = _played_at_item(e.played_at_ts)
        played_at_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        table.setItem(row, _COL_PLAY_TIME, played_at_item)
        table.setItem(row, _COL_DIFF, _diff_item(e.difficulty))
        table.setItem(row, _COL_MODE, _mode_item(e.mode))

        if is_acc_mode:
            star_item = _NumItem(f"{e.acc_complexity:.1f}" if e.acc_complexity > 0 else "-", e.acc_complexity)
            star_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(row, _COL_STARS, star_item)
        else:
            star_text = f"{e.stars:.2f}" if e.stars > 0 else "-"
            star_item = _NumItem(star_text, e.stars)
            star_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(row, _COL_STARS, star_item)

        if is_acc_mode:
            pp_item = _NumItem(
                f"{e.acc_rl_ap:.2f}" if e.acc_rl_ap > 0 else "-",
                e.acc_rl_ap,
            )
        elif is_bs_mode:
            pp_item = _NumItem("-", 0.0)
        else:
            pp_item = _NumItem(
                f"{e.player_pp:.1f}" if e.player_pp > 0 else "-",
                e.player_pp,
            )
        pp_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        table.setItem(row, _COL_PLAYER_PP, pp_item)

        if is_bs_mode:
            bs_rate_item = _NumItem(
                f"{e.player_pp:.1f}" if e.player_pp > 0 else "-",
                e.player_pp,
            )
            bs_rate_item.setData(Qt.ItemDataRole.UserRole, e.player_pp)
        else:
            bs_rate_item = _NumItem("-", 0.0)
        bs_rate_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        table.setItem(row, _COL_BS_RATE, bs_rate_item)

        svc_item = QTableWidgetItem(e.score_source if e.score_source else "-")
        svc_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        table.setItem(row, _COL_SERVICE, svc_item)

        if is_bs_mode:
            acc_item = _NumItem("-", 0.0)
        else:
            acc_item = _NumItem(
                f"{e.player_acc:.2f}%" if e.player_acc > 0 else "-",
                e.player_acc,
            )
        acc_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        table.setItem(row, _COL_PLAYER_ACC, acc_item)

        bs_upvotes_item = _NumItem(
            str(e.beatsaver_upvotes) if is_bs_mode else "-",
            float(e.beatsaver_upvotes if is_bs_mode else 0.0),
        )
        bs_upvotes_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        table.setItem(row, _COL_BS_UPVOTES, bs_upvotes_item)

        if is_bs_mode:
            rank_item = _NumItem("-", 999_999_999)
        else:
            rank_item = _NumItem(
                str(e.player_rank) if e.player_rank > 0 else "-",
                e.player_rank if e.player_rank > 0 else 999_999_999,
            )
        rank_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        table.setItem(row, _COL_PLAYER_RANK, rank_item)

        bs_downvotes_item = _NumItem(
            str(e.beatsaver_downvotes) if is_bs_mode else "-",
            float(e.beatsaver_downvotes if is_bs_mode else 0.0),
        )
        bs_downvotes_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        table.setItem(row, _COL_BS_DOWNVOTES, bs_downvotes_item)
        table.setItem(row, _COL_AUTHOR, QTableWidgetItem(e.song_author))
        table.setItem(row, _COL_MAPPER, QTableWidgetItem(e.mapper))

        bl_plays_item = _NumItem(
            str(e.beatleader_plays) if has_bl_stats_source else "-",
            float(e.beatleader_plays if has_bl_stats_source else -1.0),
        )
        bl_plays_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        table.setItem(row, _COL_BL_PLAYS, bl_plays_item)
        bl_attempts_item = _NumItem(
            str(e.beatleader_attempts) if has_bl_stats_source else "-",
            float(e.beatleader_attempts if has_bl_stats_source else -1.0),
        )
        bl_attempts_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        table.setItem(row, _COL_BL_ATTEMPTS, bl_attempts_item)

        fc_item = _NumItem("FC" if e.full_combo else "", 1.0 if e.full_combo else 0.0)
        fc_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        table.setItem(row, _COL_FC, fc_item)
        mod_item = QTableWidgetItem(e.player_mods)
        mod_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        table.setItem(row, _COL_MOD, mod_item)

        cat_display = {"true": "True", "standard": "Standard", "tech": "Tech"}
        if self._rb_open.isChecked():
            svc_disp = {"scoresaber": "SS", "beatleader": "BL", "accsaber_reloaded": "RL"}.get(e.source, e.source)
            cat_text = svc_disp
        elif e.source == "beatsaver":
            cat_text = "-"
        elif e.source in ("scoresaber", "beatleader"):
            cat_text = "-"
        else:
            raw_cat = e.acc_category or ""
            cats = raw_cat.split("/")
            cat_text = "/".join(cat_display.get(c, c.capitalize()) for c in cats) if raw_cat else ""
        table.setItem(row, _COL_ACC_CAT, QTableWidgetItem(cat_text))

    def _finish_table_render(self, table: QTableWidget, render_token: int) -> None:
        if render_token != self._table_render_token:
            return
        self._table_render_active = False
        table.setSortingEnabled(True)
        self._hydrate_visible_row_widgets(table)
        pending_target = self._pending_restore_entry
        self._pending_restore_entry = None
        if table is self._table and pending_target is not None:
            self._restore_selected_entry(pending_target)

    def _populate_table_rows_chunk(
        self,
        table: QTableWidget,
        entries: List[MapEntry],
        start_row: int,
        render_token: int,
        cleared_bg: QColor,
        nf_bg: QColor,
        unplayed_bg: QColor,
        is_acc_mode: bool,
        is_bs_mode: bool,
    ) -> None:
        if render_token != self._table_render_token:
            return
        chunk_size = 120
        end_row = min(len(entries), start_row + chunk_size)
        table.setUpdatesEnabled(False)
        for row in range(start_row, end_row):
            self._populate_table_row(
                table,
                row,
                entries[row],
                cleared_bg,
                nf_bg,
                unplayed_bg,
                is_acc_mode,
                is_bs_mode,
            )
        table.setUpdatesEnabled(True)
        self._hydrate_visible_row_widgets(table)
        if end_row < len(entries):
            QTimer.singleShot(
                0,
                lambda current_table=table, current_entries=entries, next_row=end_row, current_token=render_token,
                current_cleared_bg=cleared_bg, current_nf_bg=nf_bg, current_unplayed_bg=unplayed_bg,
                current_is_acc_mode=is_acc_mode, current_is_bs_mode=is_bs_mode:
                    self._populate_table_rows_chunk(
                        current_table,
                        current_entries,
                        next_row,
                        current_token,
                        current_cleared_bg,
                        current_nf_bg,
                        current_unplayed_bg,
                        current_is_acc_mode,
                        current_is_bs_mode,
                    )
            )
            return
        self._finish_table_render(table, render_token)

    def _refresh_table(self, entries: List[MapEntry]) -> None:
        table = self._table
        self._table_render_token += 1
        render_token = self._table_render_token
        self._table_render_active = True
        table.setSortingEnabled(False)
        table.setUpdatesEnabled(False)
        table.setRowCount(0)
        table.setRowCount(len(entries))
        self._thumbnail_queue.clear()
        self._thumbnail_pending.clear()
        self._thumbnail_active_url = ""

        _cleared_bg = QColor(0x26, 0x49, 0x30, 180) if is_dark() else QColor(0xC8, 0xE6, 0xC9)
        _nf_bg = QColor(0x5C, 0x4A, 0x1A, 180) if is_dark() else QColor(0xFF, 0xF3, 0xCD)
        _unplayed_bg = QColor(0x4A, 0x2A, 0x2A, 180) if is_dark() else QColor(0xFF, 0xCC, 0xCC)
        _is_acc_mode = self._rb_acc.isChecked() or self._rb_acc_rl.isChecked() or (
            self._rb_open.isChecked() and self._svc_combo.currentData() in ("accsaber_rl", "accsaber")
        )
        _is_bs_mode = self._rb_bs.isChecked()
        self._update_table_visual_mode()
        table.setUpdatesEnabled(True)
        table.clearSelection()
        if not entries:
            self._table_render_active = False
            table.setSortingEnabled(True)
            self._clear_preview()
            self._update_selection_status()
            return
        self._clear_preview()
        self._update_selection_status()
        self._populate_table_rows_chunk(
            table,
            entries,
            0,
            render_token,
            _cleared_bg,
            _nf_bg,
            _unplayed_bg,
            _is_acc_mode,
            _is_bs_mode,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 一括出力
    # ──────────────────────────────────────────────────────────────────────────

    def _on_export(self) -> None:
        """Style ラジオに応じて出力メソッドを呼ぶ。"""
        split = self._rb_exp_split.isChecked()
        acc_source = self._rb_acc_rl.isChecked()
        tag = self._make_export_tag()

        # Open モード時は元ファイル名をベースにする
        if self._rb_open.isChecked():
            src_path = self._open_edit.text().strip()
            stem = Path(src_path).stem if src_path else "export"
            tag = f"{stem}_{tag}"

        if split and acc_source:
            self._export_by_category(list(self._filtered), tag)
        elif split:
            self._export_per_star_all(tag)
        else:
            self._export_all_by_pp(tag)

    def _export_all_by_pp(self, tag: str = "all") -> None:
        """全マップを ★ → Player PP 降順で 1 つの bplist に出力する。"""
        target = list(self._filtered)
        sorted_entries = _sort_entries(target, self._current_sort_mode())

        if not sorted_entries:
            QMessageBox.information(self, "Export", "No maps found.")
            return

        title = f"Maps ({tag})"
        _sort_dir = _sort_dir_from_mode(self._current_sort_mode())
        src = "ss" if self._rb_ss.isChecked() else "bl" if self._rb_bl.isChecked() else "rl"
        image = _make_playlist_cover("default", "", _sort_dir, src)
        saved = _save_bplist(self, title, sorted_entries, self._export_dir, image)
        if saved:
            folder = str(Path(saved).parent)
            self._save_export_dir(folder)
            self._show_bplist_covers_dialog(
                "Export Complete", folder, [Path(saved).name], []
            )

    # ── ★別分割出力 共通ヘルパー ─────────────────────────────────────────

    def _group_by_star(self, entries: List[MapEntry]) -> Dict[int, List[MapEntry]]:
        """MapEntry のリストを ★ の整数値でグループ化する。"""
        groups: Dict[int, List[MapEntry]] = {}
        for e in entries:
            star_int = max(1, math.floor(e.stars)) if e.stars > 0 else 0
            groups.setdefault(star_int, []).append(e)
        return groups

    def _export_per_star(
        self,
        entries: List[MapEntry],
        filename_suffix: str,
        title_template: str,
    ) -> None:
        """★別に分割して bplist ファイルをフォルダに一括保存する。

        filename_suffix: ファイル名の末尾タグ
        title_template: {star} を含むプレイリストタイトルテンプレート
        """
        if not entries:
            QMessageBox.information(self, "Export", "No maps found.")
            return

        folder = QFileDialog.getExistingDirectory(self, "Select output folder", self._export_dir)
        if not folder:
            return
        self._save_export_dir(folder)

        groups = self._group_by_star(entries)
        saved_fnames: List[str] = []
        errors: List[str] = []
        _sort_dir = _sort_dir_from_mode(self._current_sort_mode())

        for star_int in sorted(groups.keys()):
            group_entries = _sort_entries(groups[star_int], self._current_sort_mode())
            title = title_template.format(star=star_int)
            filename = f"{star_int:02d}star_{filename_suffix}.bplist"
            out_path = Path(folder) / filename
            image = _make_playlist_cover("star", str(star_int), _sort_dir)
            bplist = _make_bplist(title, group_entries, image)
            try:
                out_path.write_text(
                    json.dumps(bplist, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                saved_fnames.append(filename)
            except Exception as exc:
                errors.append(f"★{star_int}: {exc}")

        self._show_bplist_covers_dialog(
            f"Export Complete — {len(saved_fnames)} file(s)",
            folder,
            saved_fnames,
            errors,
        )

    def _export_per_star_all(self, tag: str = "all") -> None:
        """全マップを ★ ごとに別ファイル (PP 降順) で出力する。"""
        self._export_per_star(
            list(self._filtered),
            filename_suffix=tag,
            title_template="{star}★ " + tag,
        )

    def _export_by_category(self, entries: List[MapEntry], tag: str = "all") -> None:
        """acc_category ごとに別ファイルで出力する (AccSaber / AccSaber Reloaded 用)。"""
        if not entries:
            QMessageBox.information(self, "Export", "No maps found.")
            return

        folder = QFileDialog.getExistingDirectory(self, "Select output folder", self._export_dir)
        if not folder:
            return
        self._save_export_dir(folder)

        folder_path = Path(folder)
        groups: Dict[str, List[MapEntry]] = {}
        for e in entries:
            cat = e.acc_category or "unknown"
            groups.setdefault(cat, []).append(e)

        saved_fnames: List[str] = []
        errors: List[str] = []
        _sort_dir = _sort_dir_from_mode(self._current_sort_mode())

        for cat in sorted(groups.keys()):
            try:
                cat_entries = _sort_entries(groups[cat], self._current_sort_mode())
                fname = folder_path / f"{cat}_{tag}.bplist"
                image = _make_playlist_cover(cat, "", _sort_dir)
                bplist = _make_bplist(f"{cat.capitalize()} ({tag})", cat_entries, image)
                fname.write_text(
                    json.dumps(bplist, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                saved_fnames.append(fname.name)
            except Exception as exc:
                errors.append(f"{cat}: {exc}")

        self._show_bplist_covers_dialog(
            f"Export Complete — {len(saved_fnames)} file(s)",
            folder,
            saved_fnames,
            errors,
        )
