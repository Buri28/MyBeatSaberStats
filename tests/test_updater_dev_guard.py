"""開発用のフォルダ（git のリポジトリ）・ソースからの起動ではアップデートしないことの確認。

更新は配布物に無いファイルを消すので、リポジトリに更新をかけると .git やソースが消える
（2026-11-26、リポジトリ直下の exe から更新して .git と直下のファイルが消えた）。
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

import mybeatsaberstats.updater as up


def _make_release_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("MyBeatSaberStats/MyBeatSaberStats.exe", b"new exe")
        z.writestr("MyBeatSaberStats/_internal/version.json", '{"version": "9.9.9"}')
    return path


def _make_install_dir(root: Path) -> Path:
    install = root / "app"
    (install / "_internal").mkdir(parents=True)
    (install / "MyBeatSaberStats.exe").write_bytes(b"old exe")
    (install / "build.ps1").write_text("keep me", encoding="utf-8")
    return install


@pytest.mark.parametrize("marker", [".git", "src/mybeatsaberstats"])
def test_find_marker_in_dev_folder(tmp_path: Path, marker: str) -> None:
    (tmp_path / marker).mkdir(parents=True)
    assert up.find_dev_checkout_marker(tmp_path) == tmp_path / marker


def test_git_file_is_marker(tmp_path: Path) -> None:
    # git worktree / submodule では .git がファイルになる
    (tmp_path / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    assert up.find_dev_checkout_marker(tmp_path) == tmp_path / ".git"


def test_release_folder_has_no_marker(tmp_path: Path) -> None:
    # 配布物の中身（_internal/lib/mybeatsaberstats・cache など）は目印にならない
    (tmp_path / "_internal" / "lib" / "mybeatsaberstats").mkdir(parents=True)
    (tmp_path / "cache").mkdir()
    (tmp_path / "src").mkdir()  # src だけでは目印にしない
    assert up.find_dev_checkout_marker(tmp_path) is None
    up.ensure_update_allowed(tmp_path)  # 例外にならない


def test_source_run_is_refused() -> None:
    with pytest.raises(RuntimeError, match="ソースから起動"):
        up.ensure_update_allowed(None)


def test_apply_update_refuses_source_run_before_download(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)

    def _no_download(*_a, **_k):
        raise AssertionError("ダウンロードしてはいけない")

    monkeypatch.setattr(up, "_read_update_source_bytes", _no_download)
    info = up.UpdateInfo(
        has_update=True, current_version="0.9.0", latest_version="0.9.1", latest_tag="v0.9.1",
        release_notes="", files=["src/mybeatsaberstats/updater.py"], release_zip_url=None,
    )
    with pytest.raises(RuntimeError, match="ソースから起動"):
        up.apply_update(info)


def test_apply_update_refuses_frozen_exe_in_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install = _make_install_dir(tmp_path)
    (install / ".git").mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install / "MyBeatSaberStats.exe"))

    def _no_download(*_a, **_k):
        raise AssertionError("ダウンロードしてはいけない")

    monkeypatch.setattr(up, "_read_update_source_bytes", _no_download)
    info = up.UpdateInfo(
        has_update=True, current_version="0.9.0", latest_version="0.9.1", latest_tag="v0.9.1",
        release_notes="", files=[], release_zip_url="https://example.invalid/MyBeatSaberStats-0.9.1.zip",
    )
    with pytest.raises(RuntimeError, match="開発用のフォルダ"):
        up.apply_update(info)


def test_apply_update_in_release_folder_goes_to_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 普通の利用者のフォルダでは止めず、外部の更新へ進む
    install = _make_install_dir(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install / "MyBeatSaberStats.exe"))
    staged: list[Path] = []
    monkeypatch.setattr(up, "_stage_external_update", lambda info, progress=None, **_k: staged.append(install))
    info = up.UpdateInfo(
        has_update=True, current_version="0.9.0", latest_version="0.9.1", latest_tag="v0.9.1",
        release_notes="", files=[], release_zip_url="https://example.invalid/MyBeatSaberStats-0.9.1.zip",
    )
    up.apply_update(info)
    assert staged == [install]


def test_staged_apply_keeps_repo_files(tmp_path: Path) -> None:
    install = _make_install_dir(tmp_path)
    (install / ".git" / "objects").mkdir(parents=True)
    (install / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    zip_path = _make_release_zip(tmp_path / "release.zip")

    with pytest.raises(RuntimeError, match="開発用のフォルダ"):
        up.apply_staged_update_package(zip_path, install, restart=False, cleanup_dir=tmp_path / "stage")

    # 何も消えず、何も上書きされていない
    assert (install / ".git" / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main\n"
    assert (install / "build.ps1").read_text(encoding="utf-8") == "keep me"
    assert (install / "MyBeatSaberStats.exe").read_bytes() == b"old exe"


def test_staged_apply_still_works_in_release_folder(tmp_path: Path) -> None:
    install = _make_install_dir(tmp_path)
    (install / "cache").mkdir()
    (install / "cache" / "mine.json").write_text("{}", encoding="utf-8")
    zip_path = _make_release_zip(tmp_path / "release.zip")

    up.apply_staged_update_package(zip_path, install, restart=False, cleanup_dir=tmp_path / "stage")

    assert (install / "MyBeatSaberStats.exe").read_bytes() == b"new exe"
    assert (install / "cache" / "mine.json").exists()
