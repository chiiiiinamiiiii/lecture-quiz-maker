"""コースの削除: 確認を挟み、やめれば残り、削除すれば一覧から消えて別のコースに移る。API は呼ばない。"""
import json
import shutil

from conftest import copy_fixture_course, open_page, start_app


def test_delete_course_flow(browser, tmp_path_factory):
    d = tmp_path_factory.mktemp("quizmap-delete")
    first = copy_fixture_course(d)
    second = first.with_name(first.name + "-new")
    shutil.copytree(first, second)
    info = json.loads((second / "course.json").read_text(encoding="utf-8"))
    info.update(name="消すコース", created_at="2099-01-01T00:00:00")   # 新しい順で先頭 = 最初に開かれる
    (second / "course.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    gen = start_app(d)
    url = next(gen)
    try:
        pg = open_page(browser, url)
        side = pg.locator('[data-testid="stSidebar"]')
        side.get_by_role("button", name="このコースを削除").click()
        side.get_by_text("元に戻せません").wait_for(timeout=15000)
        assert second.exists(), "確認の前には消さない"
        side.get_by_role("button", name="やめる").click()
        side.get_by_role("button", name="このコースを削除").wait_for(timeout=15000)
        assert second.exists(), "やめたら消さない"

        side.get_by_role("button", name="このコースを削除").click()
        side.get_by_role("button", name="削除する").click()
        side.get_by_text("消すコース").wait_for(state="detached", timeout=20000)
        assert not second.exists() and first.exists()
        pg.wait_for_function("() => window.__qmMapApp && window.__qmMapApp.view", timeout=60000)
        assert pg.errors == []
    finally:
        next(gen, None)
