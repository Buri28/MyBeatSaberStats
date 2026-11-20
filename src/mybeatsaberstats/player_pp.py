"""Player-wide BeatLeader PP breakdown shared by Stats and Compare."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

from .numfmt import fmt, trunc
from .theme import (table_stylesheet, diff_positive_bg, diff_negative_bg,
                    diff_neutral_bg, diff_text_color, label_cell_color, label_cell_text_color)

PP_FIELDS = (("Acc PP", "beatleader_acc_pp"),
             ("Tech PP", "beatleader_tech_pp"),
             ("Pass PP", "beatleader_pass_pp"))


def pp_limits(snapshot=None):
    """BeatLeader SkillTriangleChart display scales, in Pass/Tech/Acc order.

    Source: official SkillTriangleChart.svelte, retrieved 2026-11-15
    (saved research session 01a1dc66-4574-7b23-94c9-c12a726f7db0).
    These are display scales, not a hard limit on earned PP.
    The top screen and the compare screen both use the latest scale; the scale
    is not switched by the snapshot date (snapshot is accepted but unused).
    """
    return (3000, 1200, 14500)


def pp_limits_by_field(snapshot):
    """Keep component scales independent of the display order."""
    return dict(zip(("beatleader_pass_pp", "beatleader_tech_pp", "beatleader_acc_pp"),
                    pp_limits(snapshot)))


class PlayerPPPanel(QTableWidget):
    """Three always-visible player PP rows for the bottom action area."""
    def __init__(self, parent, delegate_type):
        super().__init__(3, 2, parent)
        self.horizontalHeader().hide()
        self.verticalHeader().hide()
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.verticalHeader().setDefaultSectionSize(24)
        self.setFixedHeight(3 * 24 + 2 * self.frameWidth())
        limits = pp_limits_by_field(None)
        # The scale is only a display guide, not the PP cap: no 🏆 / bold at the scale.
        self._bars = [delegate_type(self, max_value=limits[field], trophy=False) for _, field in PP_FIELDS]
        for row, delegate in enumerate(self._bars):
            self.setItemDelegateForRow(row, delegate)
        # Reserve enough space for the full values (value only) at the existing font size.
        fm = self.fontMetrics()
        self.setMinimumWidth(fm.horizontalAdvance("Pass PP") +
                             fm.horizontalAdvance("99,999.99") + 24)
        self.set_snapshots(None)

    def set_snapshots(self, snapshot):
        self.setStyleSheet(table_stylesheet())
        limits = pp_limits_by_field(snapshot)
        for row, (label, field) in enumerate(PP_FIELDS):
            limit = limits[field]
            self._bars[row]._max_value = limit
            name = QTableWidgetItem(label)
            name.setBackground(label_cell_color())
            name.setForeground(label_cell_text_color())
            self.setItem(row, 0, name)
            value = getattr(snapshot, field, None)
            item = QTableWidgetItem("—" if value is None else fmt(value, 2, comma=True))
            if value is not None:
                item.setData(Qt.ItemDataRole.UserRole, value)
            item.setToolTip(f"BeatLeader 全体の加重 {label}。バーの長さは公式 Skill Triangle の基準値"
                            f"（{limit:,}）に対する割合（取れる PP の上限ではありません）")
            self.setItem(row, 1, item)


class PlayerPPTable(QTableWidget):
    def __init__(self, parent=None, *, compare=False):
        super().__init__(3 if compare else 1, 4 if compare else 3, parent)
        self.compare = compare
        self.setHorizontalHeaderLabels(["Metric", "A", "B", "Diff(A⇒B)"] if compare
                                       else [name for name, _ in PP_FIELDS])
        self.verticalHeader().hide()
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setMinimumSectionSize(95)
        self.setMinimumWidth(95 * self.columnCount() + self.frameWidth() * 2)
        self.verticalHeader().setDefaultSectionSize(24)
        self.setFixedHeight(self.horizontalHeader().sizeHint().height() + self.rowCount() * 24
                            + self.frameWidth() * 2)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setToolTip("BeatLeader 全体 PP 内訳（プレイヤー全体の加重合計・★別集計とは別）")
        self.set_snapshots(None)

    def set_snapshots(self, a, b=None):
        self.setStyleSheet(table_stylesheet())
        for row, (label, field) in enumerate(PP_FIELDS):
            av = getattr(a, field, None)
            bv = getattr(b, field, None)
            if not self.compare:
                self.setItem(0, row, QTableWidgetItem("—" if av is None else fmt(av, 2, comma=True)))
                continue
            item = QTableWidgetItem(label)
            item.setBackground(label_cell_color())
            item.setForeground(label_cell_text_color())
            self.setItem(row, 0, item)
            for col, value in ((1, av), (2, bv)):
                self.setItem(row, col, QTableWidgetItem("—" if value is None else fmt(value, 2, comma=True)))
            diff = QTableWidgetItem("—")
            if av is not None and bv is not None:
                # Match the existing metric comparison: compare displayed precision.
                delta = trunc(bv, 2) - trunc(av, 2)
                diff.setText(fmt(delta, 2, comma=True, sign=True))
                diff.setBackground(diff_positive_bg() if delta > 0 else
                                   diff_negative_bg() if delta < 0 else diff_neutral_bg())
                diff.setForeground(diff_text_color())
            self.setItem(row, 3, diff)
