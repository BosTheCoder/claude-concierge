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
