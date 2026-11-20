def edit(p, pairs):
    b=open(p,'rb').read().decode('utf-8'); crlf='\r\n' in b; s=b.replace('\r\n','\n')
    for a,c in pairs:
        assert s.count(a)==1,(p,a,s.count(a)); s=s.replace(a,c)
    if crlf: s=s.replace('\n','\r\n')
    open(p,'wb').write(s.encode('utf-8'))
edit('src/mybeatsaberstats/player_pp.py', [
('''def pp_limits(snapshot):
    """BeatLeader SkillTriangleChart display scales, in Pass/Tech/Acc order.

    Source: official SkillTriangleChart.svelte, retrieved 2026-11-15
    (saved research session 01a1dc66-4574-7b23-94c9-c12a726f7db0).
    These are display scales, not a hard limit on earned PP.
    """
    from datetime import datetime
    try:
        stamp = datetime.fromisoformat(snapshot.taken_at.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, TypeError):
        stamp = float("inf")
    if stamp < 1793116800:
        return (6000, 1300, 15000)
    if stamp < 1793836800:
        return (3100, 2800, 19000)
    return (3000, 1200, 14500)
''','''def pp_limits(snapshot=None):
    """BeatLeader SkillTriangleChart display scales, in Pass/Tech/Acc order.

    Source: official SkillTriangleChart.svelte, retrieved 2026-11-15
    (saved research session 01a1dc66-4574-7b23-94c9-c12a726f7db0).
    These are display scales, not a hard limit on earned PP.
    The top screen and the compare screen both use the latest scale; the scale
    is not switched by the snapshot date (snapshot is accepted but unused).
    """
    return (3000, 1200, 14500)
'''),
('''        self._bars = [delegate_type(self, max_value=limits[field]) for _, field in PP_FIELDS]''',
 '''        # The scale is only a display guide, not the PP cap: no 🏆 / bold at the scale.
        self._bars = [delegate_type(self, max_value=limits[field], trophy=False) for _, field in PP_FIELDS]'''),
('''        # Reserve enough space for the full values at the existing font size.
        fm = self.fontMetrics()
        self.setMinimumWidth(fm.horizontalAdvance("Pass PP") +
                             fm.horizontalAdvance("19,000.00 / 19,000") + 24)''',
 '''        # Reserve enough space for the full values (value only) at the existing font size.
        fm = self.fontMetrics()
        self.setMinimumWidth(fm.horizontalAdvance("Pass PP") +
                             fm.horizontalAdvance("99,999.99") + 24)'''),
('''            item = QTableWidgetItem(f"{'—' if value is None else fmt(value, 2, comma=True)} / {limit:,}")
            if value is not None:
                item.setData(Qt.ItemDataRole.UserRole, value)
            item.setToolTip("BeatLeader 全体の加重 PP / 成分別の表示上限（公式 Skill Triangle の基準値）")''',
 '''            item = QTableWidgetItem("—" if value is None else fmt(value, 2, comma=True))
            if value is not None:
                item.setData(Qt.ItemDataRole.UserRole, value)
            item.setToolTip(f"BeatLeader 全体の加重 {label}。バーの長さは公式 Skill Triangle の基準値"
                            f"（{limit:,}）に対する割合（取れる PP の上限ではありません）")'''),
])
edit('src/mybeatsaberstats/player_app.py', [
('''        light_text_off_bar: Optional[str] = "#111199",
    ) -> None:
        """ コンストラクタ。''','''        light_text_off_bar: Optional[str] = "#111199",
        trophy: bool = True,
    ) -> None:
        """ コンストラクタ。'''),
('''        :param light_text_off_bar: ライトモード・バーなし/暗いバー時のテキスト色（None=デフォルト）
        """
        super().__init__(parent)
        self._max_value = max_value''','''        :param light_text_off_bar: ライトモード・バーなし/暗いバー時のテキスト色（None=デフォルト）
        :param trophy: max_value に届いたとき太字と 🏆 / 🏅 を付けるか（表示用の目安の上限では False）
        """
        super().__init__(parent)
        self._trophy = trophy
        self._max_value = max_value'''),
('''        if value is not None and value >= self._max_value - 1e-9:
            option.font.setBold(True)''','''        if self._trophy and value is not None and value >= self._max_value - 1e-9:
            option.font.setBold(True)'''),
])
