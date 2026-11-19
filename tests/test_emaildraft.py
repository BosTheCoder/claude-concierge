"""An email card: he edits a job's reply draft in the app and sends it. Gmail is faked; the real
gdraft MIME code and the real API route run."""
import base64
import email
import email.policy

import pytest

from concierge import app_api, emaildraft, threads

if not emaildraft.GDRAFT.exists():
    pytest.skip("toolkit's gdraft not on this machine", allow_module_level=True)

QUOTE = ('<div class="gmail_quote gmail_quote_container"><div class="gmail_attr">On Tue, 17 Nov 2026 at 11:07, '
         'Royal Wharf &lt;rw@x.com&gt; wrote:<br></div><blockquote class="gmail_quote">Dear Bosire, prices below.'
         '</blockquote></div>')


def reply_draft() -> bytes:
    m = email.message.EmailMessage()
    m["From"], m["To"], m["Cc"] = "Bosire Nyakundi <nyakundi@hotmail.co.uk>", "rw@x.com", "someone@x.com"
    m["Subject"], m["In-Reply-To"] = "Re: Parking", "<abc@x.com>"
    m.set_content("Hi,\nThanks.\n\nOn Tue, 17 Nov 2026 at 11:07, Royal Wharf <rw@x.com> wrote:\n\n> Dear Bosire")
    m.add_alternative('<div dir="ltr"><p>Hi,</p><p>Thanks.</p></div><br>' + QUOTE, subtype="html")
    return m.as_bytes()


@pytest.fixture
def gmail(monkeypatch):
    gd = emaildraft._gd()
    state = {"raw": reply_draft(), "sent": None}

    def api(token, path, method="GET", payload=None, **params):
        if path == "/settings/sendAs":
            return {"sendAs": [{"sendAsEmail": "nyakundi@hotmail.co.uk", "displayName": "Bosire Nyakundi", "isDefault": True},
                               {"sendAsEmail": "nyakundipropertymanagement@gmail.com", "displayName": "Nyakundi Property Management"}]}
        if path.startswith("/drafts/") and method == "GET":
            return {"id": "r-1", "message": {"id": "m1", "threadId": "t1",
                                             "raw": base64.urlsafe_b64encode(state["raw"]).decode()}}
        if method == "PUT":
            assert payload["message"]["threadId"] == "t1"
            state["raw"] = base64.urlsafe_b64decode(payload["message"]["raw"])
            return {}
        if path == "/drafts/send":
            state["sent"] = email.message_from_bytes(state["raw"], policy=email.policy.default)
            return {"id": "sent1"}
        raise AssertionError(f"unexpected {method} {path}")

    gd.api = api
    gd.access_token = lambda user: "tok"
    monkeypatch.setattr(emaildraft, "_gd", lambda: gd)
    return state


def test_he_edits_the_reply_changes_from_and_sends_it(gmail, tmp_path, monkeypatch):
    db = threads.connect()
    th = threads.find_or_create(db, job="A1", title="Parking", pane="concierge:A1")
    mid = threads.post(db, th, "Email to rw@x.com: Re: Parking", kind="draft", options=["Send"], ref="r-1",
                       sender=lambda *_: None)
    told = []
    monkeypatch.setattr(app_api, "_bg", lambda fn, *a, **k: fn(db, *a, submitter=lambda pane, text: told.append(text), **k))

    card = emaildraft.load("r-1")
    assert QUOTE not in card["html"] and "Thanks." in card["html"]  # only his new text is editable
    assert card["from"] == "nyakundi@hotmail.co.uk"

    code, out = app_api.post(f"/api/threads/{th['id']}/sendemail", {
        "msg": mid, "from": "nyakundipropertymanagement@gmail.com", "to": "rw@x.com", "cc": "",
        "subject": "Re: Parking", "html": '<div dir="ltr"><p>Hi,</p><p>I will take the 12-month rental.</p></div><br>'})
    assert code == 200, out

    sent = gmail["sent"]
    assert sent["From"].addresses[0].addr_spec == "nyakundipropertymanagement@gmail.com"
    assert sent["Cc"] is None and sent["In-Reply-To"] == "<abc@x.com>"
    html = sent.get_body(("html",)).get_content()
    assert "12-month rental" in html and "Thanks." not in html and QUOTE in html  # thread kept, still collapses
    plain = sent.get_body(("plain",)).get_content()
    assert "12-month rental" in plain and "Royal Wharf" in plain

    row = db.execute("SELECT answer FROM messages WHERE id=?", (mid,)).fetchone()
    assert "nyakundipropertymanagement" in row["answer"]
    assert told and "sent the email" in told[0]
    # Sent mail can't be taken back, and the generic answer route can't fake a send.
    with pytest.raises(ValueError):
        threads.undo(db, mid)
