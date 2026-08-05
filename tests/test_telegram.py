import pytest
from concierge import telegram


def test_load_token_reads_the_plugin_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nTELEGRAM_BOT_TOKEN=123:abc\n")
    assert telegram.load_token(env) == "123:abc"


def test_load_token_strips_quotes(tmp_path):
    env = tmp_path / ".env"
    env.write_text('TELEGRAM_BOT_TOKEN="123:abc"\n')
    assert telegram.load_token(env) == "123:abc"


def test_load_token_raises_a_useful_error_when_missing(tmp_path):
    with pytest.raises(RuntimeError, match="/telegram:configure"):
        telegram.load_token(tmp_path / "nope.env")


def test_chunk_leaves_a_short_message_alone():
    assert telegram.chunk("hello") == ["hello"]


def test_chunk_splits_on_a_newline_boundary_when_it_can():
    text = "a" * 4000 + "\n" + "b" * 200
    parts = telegram.chunk(text, limit=4096)
    assert len(parts) == 2
    assert parts[0] == "a" * 4000
    assert parts[1] == "b" * 200


def test_chunk_hard_splits_when_there_is_no_newline():
    parts = telegram.chunk("c" * 5000, limit=4096)
    assert [len(p) for p in parts] == [4096, 904]


def test_send_posts_chat_id_and_text():
    calls = []

    def poster(url, payload):
        calls.append((url, payload))
        return {"ok": True}

    telegram.send("999", "hi", token="T", poster=poster)

    url, payload = calls[0]
    assert url == "https://api.telegram.org/botT/sendMessage"
    assert payload["chat_id"] == "999"
    assert payload["text"] == "hi"
    assert "reply_parameters" not in payload


def test_send_threads_only_the_first_chunk():
    calls = []

    def poster(url, payload):
        calls.append(payload)
        return {"ok": True}

    telegram.send("999", "d" * 5000, reply_to=42, token="T", poster=poster)

    assert len(calls) == 2
    assert calls[0]["reply_parameters"] == {"message_id": 42}
    assert "reply_parameters" not in calls[1]
