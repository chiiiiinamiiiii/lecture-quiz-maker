"""取り込み画面の戻り確認。"""
import json

from conftest import exception_text, main_text

NAME = "readd_test_資料.pdf"            # ファイル名から講義回が読めない名前
OLD = b"%PDF-1.4 readd old"


def _course(data_dir):
    return next((data_dir / "courses").iterdir())


def _set_hand_edited(course):
    """一覧に NAME を「講義回 40・使う」で置いた状態にする（ほかの回と重ならない番号なので、そのまま残るはず）。"""
    info_path = course / "course.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    (course / "uploads").mkdir(exist_ok=True)
    for p in (course / "uploads").glob("readd_test_*"):
        p.unlink()
    (course / "uploads" / NAME).write_bytes(OLD)
    info["files"] = [f for f in info["files"] if not f["name"].startswith("readd_test_")]
    info["files"].append({"name": NAME, "lecture": 40, "use": True})
    info_path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


def _files(course):
    return {f["name"]: f for f in json.loads((course / "course.json").read_text(encoding="utf-8"))["files"]}


def _upload(page, tmp_path, data: bytes) -> str:
    """取り込み画面で NAME の PDF を追加し、出たトーストの文言を返す。"""
    page.get_by_text("資料の取り込み", exact=True).click()
    page.wait_for_selector("text=入れた PDF はすべて使います", timeout=15000)
    src = tmp_path / NAME
    src.write_bytes(data)
    page.locator("[data-testid='stFileUploader'] input[type='file']").first.set_input_files(str(src))
    page.get_by_role("button", name="追加する").click(timeout=15000)
    page.wait_for_selector("[data-testid='stToast']", timeout=15000)
    toast = page.locator("[data-testid='stToast']").first.inner_text()
    page.wait_for_timeout(1500)
    assert exception_text(page) is None
    return toast


def test_readd_same_file_keeps_lecture_and_use(page, data_dir, tmp_path):
    """#6: 一覧にある同じファイルを追加し直しても、手で決めた講義回と「使う」は残る。"""
    course = _course(data_dir)
    _set_hand_edited(course)
    toast = _upload(page, tmp_path, OLD)
    files = _files(course)
    assert files[NAME]["lecture"] == 40 and files[NAME]["use"] is True
    assert not any(n.startswith("readd_test_") and n != NAME for n in files)
    assert "そのまま" in toast


def test_readd_different_content_adds_new_entry_and_keeps_old(page, data_dir, tmp_path):
    """#6: 同じ名前で中身が違えば別名（名前 (2).pdf）で足され、元の行の講義回と「使う」は残る。"""
    course = _course(data_dir)
    _set_hand_edited(course)
    toast = _upload(page, tmp_path, b"%PDF-1.4 readd new")
    files = _files(course)
    assert files[NAME]["lecture"] == 40 and files[NAME]["use"] is True
    assert (course / "uploads" / NAME).read_bytes() == OLD
    assert "readd_test_資料 (2).pdf" in files
    # 講義回が読めない資料も使われ、続きの番号が振られる（講義回の割り当ての手順は無い）
    new = files["readd_test_資料 (2).pdf"]
    assert new["use"] is True and new["lecture"] == max(f["lecture"] for n, f in files.items() if n != "readd_test_資料 (2).pdf") + 1
    assert all(f["use"] for f in files.values())
    assert "追加しました" in toast


def test_ingest_page_in_english(page, data_dir, tmp_path):
    """取り込み画面の文言が英語に切り替わり、追加の知らせも英語になる。手で決めた講義回は残る。"""
    course = _course(data_dir)
    _set_hand_edited(course)
    page.get_by_text("English", exact=True).click()
    page.wait_for_timeout(3000)
    page.get_by_text("Import materials", exact=True).click()
    page.wait_for_selector("text=Every uploaded PDF is used", timeout=15000)
    text = main_text(page)
    assert exception_text(page) is None
    for s in ("Import materials:", "Add PDFs", "Every uploaded PDF is used"):
        assert s in text
    for s in ("資料の取り込み：", "入れた PDF はすべて使います", "資料を追加する"):
        assert s not in text

    src = tmp_path / NAME
    src.write_bytes(OLD)
    page.locator("[data-testid='stFileUploader'] input[type='file']").first.set_input_files(str(src))
    page.get_by_role("button", name="Add", exact=True).click(timeout=15000)
    page.wait_for_selector("[data-testid='stToast']", timeout=15000)
    assert "already in the list" in page.locator("[data-testid='stToast']").first.inner_text()
    files = _files(course)
    assert files[NAME]["lecture"] == 40 and files[NAME]["use"] is True
