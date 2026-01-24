from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

from PySide6.QtCore import QDate, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QListView,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .snapshot import Snapshot, BASE_DIR


@dataclass
class _MetricDef:
    key: str
    label: str
    is_int: bool = False


def _get_ss_star_metric_value(snap: Snapshot, key: str) -> Optional[float]:
    """ScoreSaber 側 Stats の★別統計をグラフ用に取り出す。

    key 形式: "star_ss_{star}_clear_count" / "star_ss_{star}_clear_rate" / "star_ss_{star}_avg_acc"
    - clear_count: その★帯のクリア数
    - clear_rate: 0.0-1.0 を 0-100(%) に変換して返す
    - avg_acc: average_acc (0.0-100.0)
    """

    if not snap.star_stats:
        return None

    parts = key.split("_")
    if len(parts) < 4 or parts[0] != "star" or parts[1] != "ss":
        return None

    try:
        star = int(parts[2])
    except ValueError:
        return None

    kind = "_".join(parts[3:])
    attr: Optional[str]
    multiplier = 1.0
    if kind == "clear_count":
        attr = "clear_count"
    elif kind == "clear_rate":
        attr = "clear_rate"
        multiplier = 100.0  # % 表示用に 0-1 → 0-100
    elif kind == "avg_acc":
        attr = "average_acc"
    else:
        return None

    for s in snap.star_stats:
        if s.star != star:
            continue
        value = getattr(s, attr, None)
        if value is None:
            return None
        try:
            return float(value) * multiplier
        except (TypeError, ValueError):
            return None

    return None


def _get_bl_star_metric_value(snap: Snapshot, key: str) -> Optional[float]:
    """BeatLeader 側 Stats の★別統計をグラフ用に取り出す。

    key 形式: "star_bl_{star}_clear_count" / "star_bl_{star}_clear_rate" / "star_bl_{star}_avg_acc"
    内容は ScoreSaber 側と同様。
    """

    if not snap.beatleader_star_stats:
        return None

    parts = key.split("_")
    if len(parts) < 4 or parts[0] != "star" or parts[1] != "bl":
        return None

    try:
        star = int(parts[2])
    except ValueError:
        return None

    kind = "_".join(parts[3:])
    attr: Optional[str]
    multiplier = 1.0
    if kind == "clear_count":
        attr = "clear_count"
    elif kind == "clear_rate":
        attr = "clear_rate"
        multiplier = 100.0
    elif kind == "avg_acc":
        attr = "average_acc"
    else:
        return None

    for s in snap.beatleader_star_stats:
        if s.star != star:
            continue
        value = getattr(s, attr, None)
        if value is None:
            return None
        try:
            return float(value) * multiplier
        except (TypeError, ValueError):
            return None

    return None


def _parse_snapshot_datetime(snap: Snapshot) -> datetime:
    """Snapshot の taken_at(ISO8601) を UTC datetime に変換する。"""

    t_str = snap.taken_at
    try:
        if t_str.endswith("Z"):
            t_str = t_str[:-1]
            dt_utc = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
        else:
            dt_utc = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        # 解析に失敗した場合は現在時刻でフォールバック
        dt_utc = datetime.now(timezone.utc)
    return dt_utc


class LineChartWidget(QWidget):
    """単純な折れ線グラフを描画するウィジェット。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._points: List[Tuple[datetime, float]] = []
        self._label: str = ""
        self._t_min_explicit: Optional[datetime] = None
        self._t_max_explicit: Optional[datetime] = None
        self._y_as_int: bool = False
        self.setMinimumHeight(180)
        self.setMinimumWidth(260)

    def set_data(
        self,
        points: List[Tuple[datetime, float]],
        label: str,
        t_min: Optional[datetime] = None,
        t_max: Optional[datetime] = None,
        y_as_int: bool = False,
    ) -> None:
        self._points = sorted(points, key=lambda p: p[0])
        self._label = label
        self._t_min_explicit = t_min
        self._t_max_explicit = t_max
        self._y_as_int = y_as_int
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        """QWidget.paintEvent のオーバーライド。折れ線グラフを描画する。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), self.palette().window())

        if not self._points:
            painter.setPen(self.palette().text().color())
            font = painter.font()
            font.setPointSize(font.pointSize() + 1)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No data")
            painter.end()
            return

        margin_left = 60
        margin_right = 20
        margin_top = 20
        margin_bottom = 40

        area = QRect(
            margin_left,
            margin_top,
            max(10, self.width() - margin_left - margin_right),
            max(10, self.height() - margin_top - margin_bottom),
        )

        times = [p[0].timestamp() for p in self._points]
        values = [p[1] for p in self._points]
        # 横軸は指定があれば From/To の範囲を優先し、無ければデータ範囲を使う
        if self._t_min_explicit is not None and self._t_max_explicit is not None:
            t_min = self._t_min_explicit.timestamp()
            t_max = self._t_max_explicit.timestamp()
        else:
            t_min = min(times)
            t_max = max(times)
        v_min = min(values)
        v_max = max(values)

        if t_max == t_min:
            t_max = t_min + 1.0
        if v_max == v_min:
            # すべて同じ値の場合は少しレンジを広げて表示する
            v_max = v_min + 1.0

        def map_x(t: float) -> int:
            return area.left() + int((t - t_min) / (t_max - t_min) * area.width())

        def map_y(v: float) -> int:
            return area.bottom() - int((v - v_min) / (v_max - v_min) * area.height())

        # 軸
        axis_pen = QPen(self.palette().mid().color())
        axis_pen.setWidth(1)
        painter.setPen(axis_pen)
        painter.drawLine(area.bottomLeft(), area.bottomRight())
        painter.drawLine(area.bottomLeft(), area.topLeft())

        # 目盛り (Y軸は4分割程度)
        painter.setPen(self.palette().mid().color())
        font = painter.font()
        font.setPointSize(max(6, font.pointSize() - 1))
        painter.setFont(font)

        for i in range(5):
            frac = i / 4.0
            y = area.bottom() - int(frac * area.height())
            v = v_min + frac * (v_max - v_min)
            painter.drawLine(area.left() - 4, y, area.left(), y)
            if self._y_as_int:
                text = str(int(round(v)))
            else:
                text = f"{v:.1f}"
            painter.drawText(2, y + 4, text)

        # X軸の日付目盛り
        x_positions: List[float] = []
        if self._t_min_explicit is not None and self._t_max_explicit is not None:
            # From/To が指定されている場合は、日単位の目盛りを打つ
            d_start = self._t_min_explicit.date()
            d_end = self._t_max_explicit.date()
            day_count = (d_end - d_start).days + 1
            # 日数が多すぎるとごちゃつくので、ある程度までに限定
            if day_count <= 10:
                for i in range(day_count):
                    dt_tick = datetime(d_start.year, d_start.month, d_start.day, 0, 0, 0) + timedelta(days=i)
                    x_positions.append(dt_tick.timestamp())
            else:
                x_positions = [t_min, (t_min + t_max) / 2.0, t_max]
        else:
            x_positions = [t_min, (t_min + t_max) / 2.0, t_max]

        for t in x_positions:
            x = map_x(t)
            dt = datetime.fromtimestamp(t).astimezone()
            text = dt.strftime("%m-%d")
            painter.drawLine(x, area.bottom(), x, area.bottom() + 4)
            painter.drawText(x - 20, area.bottom() + 16, 40, 16, Qt.AlignmentFlag.AlignHCenter, text)

        # 折れ線（データそのままを直線で結ぶ）
        line_pen = QPen(QColor(0, 120, 215))  # Windows 系のアクセントカラーに近い青
        line_pen.setWidth(2)
        painter.setPen(line_pen)

        last_x: Optional[int] = None
        last_y: Optional[int] = None
        for dt_val, v in self._points:
            x = map_x(dt_val.timestamp())
            y = map_y(v)
            if last_x is not None and last_y is not None:
                painter.drawLine(last_x, last_y, x, y)
            last_x, last_y = x, y

        # 1 点だけの場合は小さな点を描いておく
        if len(self._points) == 1:
            x = map_x(self._points[0][0].timestamp())
            y = map_y(self._points[0][1])
            painter.drawEllipse(x - 2, y - 2, 4, 4)

        # タイトル
        if self._label:
            title_font = QFont(painter.font())
            title_font.setBold(True)
            painter.setFont(title_font)
            painter.setPen(self.palette().text().color())
            painter.drawText(
                margin_left,
                4,
                max(10, self.width() - margin_left - margin_right),
                margin_top + 12,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                self._label,
            )

        painter.end()


class SnapshotGraphDialog(QDialog):
    """スナップショットを期間で絞りつつ、複数の項目グラフを並べて表示するダイアログ。"""

    def __init__(self, parent: Optional[QWidget], snapshots: List[Snapshot]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Snapshot Graph")
        # 全体は大きめに取り、グラフ自体はコンパクトにする
        self.resize(1200, 700)

        self._snapshots = list(snapshots)
        self._snapshots.sort(key=lambda s: s.taken_at)

        layout = QVBoxLayout(self)

        # 上部: 期間(from/to) と グラフ追加ボタン
        control_row = QHBoxLayout()

        control_row.addWidget(QLabel("From:"))
        self.from_date = QDateEdit(self)
        self.from_date.setCalendarPopup(True)
        control_row.addWidget(self.from_date)

        control_row.addWidget(QLabel("To:"))
        self.to_date = QDateEdit(self)
        self.to_date.setCalendarPopup(True)
        control_row.addWidget(self.to_date)

        # To の右に「Latest」ボタンを配置し、押下で To を今日に設定する
        self.to_latest_btn = QPushButton("Latest", self)
        self.to_latest_btn.setToolTip("Set To date to today")
        self.to_latest_btn.clicked.connect(self._on_to_latest_clicked)
        control_row.addWidget(self.to_latest_btn)

        # 右端側に Add Graph ボタンを寄せる
        control_row.addStretch(1)
        add_btn = QPushButton("Add Graph")
        add_btn.clicked.connect(self._add_graph)
        control_row.addWidget(add_btn)

        layout.addLayout(control_row)

        # 中央: 複数グラフを並べるリスト（ドラッグ＆ドロップで並べ替え可）
        # 横方向に左上から右へ配置し、幅に応じて折り返す。
        self.list_widget = QListWidget(self)
        self.list_widget.setViewMode(QListView.ViewMode.IconMode)
        self.list_widget.setFlow(QListView.Flow.LeftToRight)
        self.list_widget.setWrapping(True)
        self.list_widget.setResizeMode(QListView.ResizeMode.Adjust)
        self.list_widget.setSpacing(8)
        self.list_widget.setMovement(QListView.Movement.Snap)
        self.list_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        # ドラッグ開始には選択が必要なので、単一選択にしておく
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self.list_widget, 1)

        # 下部: 閉じるボタン
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

        self._metric_defs: List[_MetricDef] = self._build_metric_defs()

        self._init_dates()

        self.from_date.dateChanged.connect(self._on_controls_changed)
        self.to_date.dateChanged.connect(self._on_controls_changed)

        # 保存済みレイアウトがあれば復元し、無ければデフォルトで 1 つグラフを追加
        loaded = self._load_saved_state()
        if not loaded:
            self._add_graph()
        self._update_all_charts()

    def _on_to_latest_clicked(self) -> None:
        """To 日付を今日に設定する。"""

        self.to_date.setDate(QDate.currentDate())

    # --- 設定保存/復元 ---

    @staticmethod
    def _settings_path() -> Path:
        return BASE_DIR / "cache" / "snapshot_graph.json"

    def _init_dates(self) -> None:
        if not self._snapshots:
            today = QDate.currentDate()
            self.from_date.setDate(today)
            self.to_date.setDate(today)
            return

        dt_list = [_parse_snapshot_datetime(s) for s in self._snapshots]
        d_min = min(dt_list).date()
        d_max = max(dt_list).date()

        self.from_date.setDate(QDate(d_min.year, d_min.month, d_min.day))
        self.to_date.setDate(QDate(d_max.year, d_max.month, d_max.day))

    def _load_saved_state(self) -> bool:
        """保存済みの From/To とグラフ配置を復元する。

        プレイヤーごとではなく 1 つだけ全体設定として保持する。
        """

        path = self._settings_path()
        if not path.exists():
            return False

        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return False

        # From/To 日付
        from_str = data.get("from")
        to_str = data.get("to")
        try:
            if isinstance(from_str, str):
                d_from = date.fromisoformat(from_str)
                self.from_date.setDate(QDate(d_from.year, d_from.month, d_from.day))
            if isinstance(to_str, str):
                d_to = date.fromisoformat(to_str)
                self.to_date.setDate(QDate(d_to.year, d_to.month, d_to.day))
        except Exception:  # noqa: BLE001
            # 日付復元に失敗しても無視し、スナップショットからの初期値を使う
            pass

        # グラフ配置
        graphs_cfg = data.get("graphs")
        if not isinstance(graphs_cfg, list):
            return False

        # 一旦グラフを空にしてから再構築する
        # （通常は起動時で空だが、安全のためクリアする）
        while self.list_widget.count() > 0:
            item = self.list_widget.takeItem(0)
            w = self.list_widget.itemWidget(item)
            if isinstance(w, _GraphItemWidget):
                w.deleteLater()

        added = False
        metric_keys = {m.key for m in self._metric_defs}
        for g in graphs_cfg:
            if not isinstance(g, dict):
                continue
            key = g.get("metric_key")
            if not isinstance(key, str) or key not in metric_keys:
                continue
            self._add_graph_internal(metric_key=key)
            added = True

        return added

    def _build_metric_defs(self) -> List[_MetricDef]:
        """グラフ表示の対象とする Snapshot の数値項目定義。"""

        metrics: List[_MetricDef] = [
            _MetricDef("scoresaber_pp", "ScoreSaber PP", is_int=False),
            _MetricDef("scoresaber_rank_global", "ScoreSaber Global Rank", is_int=True),
            _MetricDef("scoresaber_rank_country", "ScoreSaber Country Rank", is_int=True),
            _MetricDef("scoresaber_average_ranked_acc", "ScoreSaber Avg Ranked ACC", is_int=False),
            _MetricDef("scoresaber_total_play_count", "ScoreSaber Total Play Count", is_int=True),
            _MetricDef("scoresaber_ranked_play_count", "ScoreSaber Ranked Play Count", is_int=True),
            _MetricDef("beatleader_pp", "BeatLeader PP", is_int=False),
            _MetricDef("beatleader_rank_global", "BeatLeader Global Rank", is_int=True),
            _MetricDef("beatleader_rank_country", "BeatLeader Country Rank", is_int=True),
            _MetricDef("beatleader_average_ranked_acc", "BeatLeader Avg Ranked ACC", is_int=False),
            _MetricDef("beatleader_total_play_count", "BeatLeader Total Play Count", is_int=True),
            _MetricDef("beatleader_ranked_play_count", "BeatLeader Ranked Play Count", is_int=True),
            _MetricDef("accsaber_overall_ap", "AccSaber Overall AP", is_int=False),
            _MetricDef("accsaber_true_ap", "AccSaber True AP", is_int=False),
            _MetricDef("accsaber_standard_ap", "AccSaber Standard AP", is_int=False),
            _MetricDef("accsaber_tech_ap", "AccSaber Tech AP", is_int=False),
            _MetricDef("accsaber_overall_rank", "AccSaber Overall Global Rank", is_int=True),
            _MetricDef("accsaber_overall_rank_country", "AccSaber Overall Country Rank", is_int=True),
            _MetricDef("accsaber_true_rank", "AccSaber True Global Rank", is_int=True),
            _MetricDef("accsaber_true_rank_country", "AccSaber True Country Rank", is_int=True),
            _MetricDef("accsaber_standard_rank", "AccSaber Standard Global Rank", is_int=True),
            _MetricDef("accsaber_standard_rank_country", "AccSaber Standard Country Rank", is_int=True),
            _MetricDef("accsaber_tech_rank", "AccSaber Tech Global Rank", is_int=True),
            _MetricDef("accsaber_tech_rank_country", "AccSaber Tech Country Rank", is_int=True),
            _MetricDef("accsaber_overall_play_count", "AccSaber Overall Play Count", is_int=True),
            _MetricDef("accsaber_true_play_count", "AccSaber True Play Count", is_int=True),
            _MetricDef("accsaber_standard_play_count", "AccSaber Standard Play Count", is_int=True),
            _MetricDef("accsaber_tech_play_count", "AccSaber Tech Play Count", is_int=True),
        ]

        # Stats(ScoreSaber 側) の★別クリア数/クリア率/Avg ACC
        star_values_ss: List[int] = []
        seen_stars_ss = set()
        for snap in self._snapshots:
            for s in snap.star_stats:
                if s.star not in seen_stars_ss:
                    seen_stars_ss.add(s.star)
                    star_values_ss.append(s.star)

        star_values_ss.sort()
        for star in star_values_ss:
            prefix = f"SS★{star}"
            metrics.append(
                _MetricDef(
                    f"star_ss_{star}_clear_count",
                    f"{prefix} Clear Count",
                    is_int=True,
                )
            )
            metrics.append(
                _MetricDef(
                    f"star_ss_{star}_clear_rate",
                    f"{prefix} Clear Rate (%)",
                    is_int=False,
                )
            )
            metrics.append(
                _MetricDef(
                    f"star_ss_{star}_avg_acc",
                    f"{prefix} Avg ACC",
                    is_int=False,
                )
            )

        # Stats(BeatLeader 側) の★別クリア数/クリア率/Avg ACC
        star_values_bl: List[int] = []
        seen_stars_bl = set()
        for snap in self._snapshots:
            for s in snap.beatleader_star_stats:
                if s.star not in seen_stars_bl:
                    seen_stars_bl.add(s.star)
                    star_values_bl.append(s.star)

        star_values_bl.sort()
        for star in star_values_bl:
            prefix = f"BL★{star}"
            metrics.append(
                _MetricDef(
                    f"star_bl_{star}_clear_count",
                    f"{prefix} Clear Count",
                    is_int=True,
                )
            )
            metrics.append(
                _MetricDef(
                    f"star_bl_{star}_clear_rate",
                    f"{prefix} Clear Rate (%)",
                    is_int=False,
                )
            )
            metrics.append(
                _MetricDef(
                    f"star_bl_{star}_avg_acc",
                    f"{prefix} Avg ACC",
                    is_int=False,
                )
            )

        return metrics

    def _on_controls_changed(self, *args) -> None:  # noqa: ANN002, ARG002
        # from > to になってしまった場合は、簡易的に調整する
        if self.from_date.date() > self.to_date.date():
            self.to_date.setDate(self.from_date.date())
        self._update_all_charts()

    def _date_range_py(self) -> Tuple[date, date]:
        d_from_q = self.from_date.date()
        d_to_q = self.to_date.date()
        d_from_py = date(d_from_q.year(), d_from_q.month(), d_from_q.day())
        d_to_py = date(d_to_q.year(), d_to_q.month(), d_to_q.day())
        return d_from_py, d_to_py

    def _graph_widgets(self) -> List["_GraphItemWidget"]:
        widgets: List["_GraphItemWidget"] = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            w = self.list_widget.itemWidget(item)
            if isinstance(w, _GraphItemWidget):
                widgets.append(w)
        return widgets

    def _update_all_charts(self) -> None:
        d_from_py, d_to_py = self._date_range_py()
        for w in self._graph_widgets():
            w.set_date_range(d_from_py, d_to_py)

    def _add_graph(self) -> None:
        self._add_graph_internal(metric_key=None)

    def _add_graph_internal(self, metric_key: Optional[str] = None) -> None:
        item = QListWidgetItem(self.list_widget)
        widget = _GraphItemWidget(self, self._snapshots, self._metric_defs, initial_metric_key=metric_key)
        item.setSizeHint(widget.sizeHint())
        self.list_widget.addItem(item)
        self.list_widget.setItemWidget(item, widget)

        # 現在の期間で初期表示
        d_from_py, d_to_py = self._date_range_py()
        widget.set_date_range(d_from_py, d_to_py)

    def _remove_graph_widget(self, widget: "_GraphItemWidget") -> None:
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if self.list_widget.itemWidget(item) is widget:
                self.list_widget.takeItem(i)
                widget.deleteLater()
                break

    def _save_state(self) -> None:
        """現在の From/To とグラフ配置を設定ファイルに保存する。"""

        try:
            import json

            d_from, d_to = self._date_range_py()
            graphs_cfg: list[dict[str, str]] = []
            for w in self._graph_widgets():
                key = w._current_metric_key()
                if not key:
                    continue
                graphs_cfg.append({"metric_key": key})

            payload = {
                "from": d_from.isoformat(),
                "to": d_to.isoformat(),
                "graphs": graphs_cfg,
            }
            path = self._settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            # 保存失敗は致命的ではないので黙って無視
            return

    def done(self, result: int) -> None:  # type: ignore[override]
        # ダイアログを閉じるタイミングでレイアウトを保存しておく
        self._save_state()
        super().done(result)


class _GraphItemWidget(QWidget):
    """1つのメトリクス用の小さなグラフ＋コントロール。"""

    def __init__(
        self,
        dialog: SnapshotGraphDialog,
        snapshots: List[Snapshot],
        metric_defs: List[_MetricDef],
        initial_metric_key: Optional[str] = None,
    ) -> None:
        super().__init__(dialog)
        self._dialog = dialog
        self._snapshots = list(snapshots)
        self._metric_defs = list(metric_defs)
        self._d_from: Optional[date] = None
        self._d_to: Optional[date] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # 上: Metric 選択と削除ボタン
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Metric:"))
        self.metric_combo = QComboBox(self)
        for m in self._metric_defs:
            self.metric_combo.addItem(m.label, userData=m.key)
        top_row.addWidget(self.metric_combo, 1)

        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._on_remove_clicked)
        top_row.addWidget(remove_btn)

        layout.addLayout(top_row)

        # 下: グラフ
        self.chart = LineChartWidget(self)
        layout.addWidget(self.chart)

        self.metric_combo.currentIndexChanged.connect(self._on_metric_changed)

        # 保存済みのメトリクキーがあればそれを選択する
        if initial_metric_key is not None:
            for idx in range(self.metric_combo.count()):
                data = self.metric_combo.itemData(idx)
                if isinstance(data, str) and data == initial_metric_key:
                    self.metric_combo.setCurrentIndex(idx)
                    break

    def sizeHint(self) -> QSize:  # type: ignore[override]
        # 固定に近い幅・高さを返して、リスト側で横並び・折り返ししやすくする
        base = super().sizeHint()
        w = max(base.width(), 280)
        h = max(base.height(), 220)
        return QSize(w, h)

    def _current_metric_key(self) -> Optional[str]:
        idx = self.metric_combo.currentIndex()
        if idx < 0:
            return None
        data = self.metric_combo.itemData(idx)
        if isinstance(data, str):
            return data
        return None

    def set_date_range(self, d_from: date, d_to: date) -> None:
        self._d_from = d_from
        self._d_to = d_to
        self._update_chart()

    def _on_metric_changed(self, *args) -> None:  # noqa: ANN002, ARG002
        self._update_chart()

    def _on_remove_clicked(self) -> None:
        self._dialog._remove_graph_widget(self)

    def _update_chart(self) -> None:
        key = self._current_metric_key()
        if not key or self._d_from is None or self._d_to is None:
            self.chart.set_data([], "")
            return

        d_from_py = self._d_from
        d_to_py = self._d_to

        # 日単位で 1 点に集約する（同じ日のスナップショットが複数ある場合は「その日の最後の値」を採用）
        day_values: dict[date, float] = {}
        for snap in self._snapshots:
            dt_utc = _parse_snapshot_datetime(snap)
            d = dt_utc.date()
            if not (d_from_py <= d <= d_to_py):
                continue

            if key.startswith("star_ss_"):
                value = _get_ss_star_metric_value(snap, key)
            elif key.startswith("star_bl_"):
                value = _get_bl_star_metric_value(snap, key)
            else:
                value = getattr(snap, key, None)
            if value is None:
                continue
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue

            # 同じ日付が複数ある場合は、後に来た（より新しい）値で上書きする
            day_values[d] = v

        points: List[Tuple[datetime, float]] = []
        for d, v in sorted(day_values.items(), key=lambda item: item[0]):
            dt_plot = datetime(d.year, d.month, d.day, 0, 0, 0)
            points.append((dt_plot, v))

        metric_def = next((m for m in self._metric_defs if m.key == key), None)
        label = metric_def.label if metric_def is not None else key
        y_as_int = bool(metric_def is not None and metric_def.is_int)

        if not points:
            self.chart.set_data([], label, y_as_int=y_as_int)
            return

        # X 軸は From 日の 00:00 〜 「最後のデータ日」の 00:00 をカバーする
        # （最後のデータ日が一番右端に来るようにする）
        last_date = points[-1][0].date()
        t_min = datetime(d_from_py.year, d_from_py.month, d_from_py.day, 0, 0, 0)
        t_max = datetime(last_date.year, last_date.month, last_date.day, 0, 0, 0)

        self.chart.set_data(points, label, t_min=t_min, t_max=t_max, y_as_int=y_as_int)
