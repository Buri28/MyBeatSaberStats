from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import List
import sys

# プロジェクトルート (開発時: リポジトリ直下, exe 時: exe のあるディレクトリ)
if getattr(sys, "frozen", False):
    # PyInstaller などで exe 化された場合は実行ファイルの場所を基準にする
    _EXE_DIR = Path(sys.executable).resolve().parent
    # バンドルされたリソースが展開されるベースディレクトリ（onefile 時は一時ディレクトリ）
    _MEIPASS = Path(getattr(sys, "_MEIPASS", _EXE_DIR))
    BASE_DIR = _EXE_DIR
    # resources フォルダは「exe と同じ場所」にあればそれを優先し、
    # なければ PyInstaller の展開先 (_MEIPASS) 配下を参照する。
    _res_dir = _EXE_DIR / "resources"
    if not _res_dir.exists():
        _res_dir = _MEIPASS / "resources"
else:
    # 通常の Python 実行時は src/mybeatsaberstats/ から 2 つ上をプロジェクトルートとみなす
    BASE_DIR = Path(__file__).resolve().parents[2]
    _res_dir = BASE_DIR / "resources"

SNAPSHOT_DIR = BASE_DIR / "snapshots"
# アイコンなどのリソースディレクトリ
RESOURCES_DIR = _res_dir


def resource_path(*parts: str) -> Path:
    """Return a Path to a resource inside the bundled resources.

    Search order:
    1. If frozen: EXE_DIR / resources / ...
    2. If frozen: _MEIPASS / resources / ...
    3. package-local resources (this module's sibling "resources")
    4. BASE_DIR / resources / ... (fallback)
    """
    # 1/2: frozen builds (onefile/onedir)
    if getattr(sys, "frozen", False):
        exe_res = _EXE_DIR / "resources"
        candidate = exe_res.joinpath(*parts)
        if candidate.exists():
            return candidate
        meipass_res = Path(getattr(sys, "_MEIPASS", _EXE_DIR)) / "resources"
        candidate = meipass_res.joinpath(*parts)
        if candidate.exists():
            return candidate

    # 3: package-local resources (during development or when installed as package)
    pkg_res = Path(__file__).resolve().parent / "resources"
    candidate = pkg_res.joinpath(*parts)
    if candidate.exists():
        return candidate

    # 4: fallback to BASE_DIR/resources
    return BASE_DIR / "resources" / Path(*parts)


@dataclass
class StarClearStat:
    """★ごとのクリア状況を表す統計。

    - star: ★帯（0, 1, 2, ...）
    - map_count: その★帯のマップ数（ScoreSaber にスコアがある譜面数）
    - clear_count: NF/SS なしでクリアしたマップ数
    - nf_count: NF 付きでのみクリアしたマップ数
    - ss_count: SS(スローソング)を使ってのみクリアしたマップ数
    - clear_rate: map_count に対する clear_count の割合 (0.0 - 1.0)
    - average_acc: その★帯での平均精度 (0.0 - 100.0)
    """

    star: int
    map_count: int
    clear_count: int
    nf_count: int
    ss_count: int = 0
    clear_rate: float = 0.0  # 0.0 - 1.0
    average_acc: float | None = None  # 0.0 - 100.0, None は未集計


@dataclass
class Snapshot:
    """ScoreSaber / BeatLeader / AccSaber の状態スナップショット。"""

    taken_at: str  # ISO8601 文字列
    steam_id: str

    # ScoreSaber
    scoresaber_id: str | None
    scoresaber_name: str | None
    scoresaber_country: str | None
    scoresaber_pp: float | None
    scoresaber_rank_global: int | None
    scoresaber_rank_country: int | None
    scoresaber_average_ranked_acc: float | None
    scoresaber_total_play_count: int | None
    scoresaber_ranked_play_count: int | None

    # BeatLeader
    beatleader_id: str | None
    beatleader_name: str | None
    beatleader_country: str | None
    beatleader_pp: float | None
    beatleader_rank_global: int | None
    beatleader_rank_country: int | None

    # AccSaber ランキング（グローバル / 国別）
    # グローバルランク
    accsaber_overall_rank: int | None = None
    accsaber_true_rank: int | None = None
    accsaber_standard_rank: int | None = None
    accsaber_tech_rank: int | None = None

    # 国別ランク（プレイヤーの所属国ごとのランク）
    accsaber_overall_rank_country: int | None = None
    accsaber_true_rank_country: int | None = None
    accsaber_standard_rank_country: int | None = None
    accsaber_tech_rank_country: int | None = None

    accsaber_overall_play_count: int | None = None
    accsaber_true_play_count: int | None = None
    accsaber_standard_play_count: int | None = None
    accsaber_tech_play_count: int | None = None

    # AccSaber AP (Overall / True / Standard / Tech)
    accsaber_overall_ap: float | None = None
    accsaber_true_ap: float | None = None
    accsaber_standard_ap: float | None = None
    accsaber_tech_ap: float | None = None

    # BeatLeader の追加統計（将来の拡張用）
    beatleader_average_ranked_acc: float | None = None
    beatleader_total_play_count: int | None = None
    beatleader_ranked_play_count: int | None = None

    # ★ごとのクリア統計（ScoreSaber 側）
    star_stats: List[StarClearStat] = field(default_factory=list)

    # BeatLeader 側の★別クリア統計
    beatleader_star_stats: List[StarClearStat] = field(default_factory=list)

    @staticmethod
    def path_for(steam_id: str, taken_at: datetime) -> Path:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{steam_id}_{taken_at:%Y%m%d-%H%M%S}.json"
        return SNAPSHOT_DIR / name

    def save(self, path: Path | None = None) -> Path:
        """スナップショットを JSON として保存する。"""

        if path is None:
            path = self.path_for(self.steam_id, datetime.fromisoformat(self.taken_at))

        data = asdict(self)
        data["star_stats"] = [asdict(s) for s in self.star_stats]
        data["beatleader_star_stats"] = [asdict(s) for s in self.beatleader_star_stats]

        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def load(path: Path) -> "Snapshot":
        import json

        data = json.loads(path.read_text(encoding="utf-8"))

        # ScoreSaber 側の★統計
        star_stats_raw = data.get("star_stats") or []

        # 古い形式との後方互換性を確保する
        converted: list[StarClearStat] = []
        for s in star_stats_raw:
            if not isinstance(s, dict):
                continue

            if "map_count" in s or "clear_count" in s or "nf_count" in s:
                # 新しい形式（ss_count が無い古い保存分も 0 扱いで受け入れる）
                try:
                    if "ss_count" not in s:
                        s = dict(s)
                        s["ss_count"] = 0
                    converted.append(StarClearStat(**s))
                    continue
                except TypeError:
                    pass

            # 旧形式: {"star", "cleared", "ranked_cleared", "clear_rate"}
            star = int(s.get("star", 0))
            cleared = int(s.get("cleared", 0))
            ranked_cleared = int(s.get("ranked_cleared", cleared))
            clear_rate = float(s.get("clear_rate", 0.0))

            converted.append(
                StarClearStat(
                    star=star,
                    map_count=cleared,
                    clear_count=ranked_cleared,
                    nf_count=cleared - ranked_cleared if cleared > ranked_cleared else 0,
                    ss_count=0,
                    clear_rate=clear_rate,
                )
            )

        data["star_stats"] = converted

        # BeatLeader 側の★統計（このフィールドは新しい形式のみ想定）
        bl_raw = data.get("beatleader_star_stats") or []
        bl_converted: list[StarClearStat] = []
        for s in bl_raw:
            if not isinstance(s, dict):
                continue
            try:
                if "ss_count" not in s:
                    s = dict(s)
                    s["ss_count"] = 0
                bl_converted.append(StarClearStat(**s))
            except TypeError:
                continue

        data["beatleader_star_stats"] = bl_converted

        return Snapshot(**data)
