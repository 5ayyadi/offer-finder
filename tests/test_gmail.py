import base64

from services.gmail import SCOPES, extract_html, extract_subject


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _part(mime: str, text: str) -> dict:
    return {"mimeType": mime, "body": {"data": _b64(text)}}


def test_scope_is_read_only():
    assert SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_subject_comes_from_headers():
    payload = {"headers": [{"name": "From", "value": "x"}, {"name": "Subject", "value": "Neue Anzeigen"}]}
    assert extract_subject(payload) == "Neue Anzeigen"
    assert extract_subject({"headers": []}) == ""


def test_html_part_is_preferred_in_multipart_alternative():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [_part("text/plain", "plain version"), _part("text/html", "<b>html version</b>")],
    }
    assert extract_html(payload) == "<b>html version</b>"


def test_nested_multipart_is_searched():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "multipart/alternative", "parts": [_part("text/html", "<p>ä nested</p>")]},
            {"mimeType": "image/png", "body": {"attachmentId": "a1"}},
        ],
    }
    assert extract_html(payload) == "<p>ä nested</p>"


def test_plain_text_is_used_when_there_is_no_html():
    assert extract_html(_part("text/plain", "https://www.kleinanzeigen.de/s-anzeige/x/1-2-3")) == (
        "https://www.kleinanzeigen.de/s-anzeige/x/1-2-3"
    )


def test_message_without_body_is_empty():
    assert extract_html({"mimeType": "multipart/mixed", "parts": []}) == ""
