import base64

import pytest

from concierge import app_api, threads

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 20


def test_new_thread_with_images_saves_them_and_names_them_in_the_brief(tmp_path, monkeypatch):
    monkeypatch.setattr(app_api, "UPLOADS", tmp_path / "uploads")
    briefs = []

    def spawner(**kw):
        briefs.append(kw["brief"])
        return {"id": "Z9"}

    paths = app_api.save_images(["data:image/png;base64," + base64.b64encode(PNG).decode()])
    db = threads.connect()
    app_api.start_thread(db, "what is this", images=paths, spawner=spawner)
    assert paths[0].read_bytes() == PNG and paths[0].suffix == ".png"
    assert str(paths[0]) in briefs[0]


def test_non_images_are_refused_and_nothing_is_written(tmp_path):
    with pytest.raises(ValueError):
        app_api.save_images(["data:text/html;base64," + base64.b64encode(b"<script>").decode()], root=tmp_path)
    assert not any(tmp_path.iterdir())


def test_linked_photos_are_served_from_the_checkout_and_nothing_else_is(tmp_path):
    from concierge.settings import Repo

    repo = tmp_path / "tasks"
    (repo / "job").mkdir(parents=True)
    (repo / "job" / "a b.png").write_bytes(PNG)
    (repo / "job" / "notes.md").write_text("private")
    (tmp_path / "outside.png").write_bytes(PNG)
    repos = [Repo(name="tasks", path=repo, github="https://github.com/Me/tasks")]
    url = "https://github.com/me/tasks/blob/main/job/"

    assert app_api.image(url + "a%20b.png", repos) == (200, PNG, "image/png")
    assert app_api.image(url + "notes.md", repos)[0] == 404
    assert app_api.image(url + "../../outside.png", repos)[0] == 404
    assert app_api.image("https://github.com/someone/else/blob/main/job/a%20b.png", repos)[0] == 404
