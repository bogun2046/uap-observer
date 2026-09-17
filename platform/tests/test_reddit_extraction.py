from __future__ import annotations

import json
import uuid

import pytest

from uap_platform.documents import (
    ExtractionInput,
    ExtractionOutcome,
    RedditSourceExtractor,
)

DOCUMENT_VERSION_ID = uuid.UUID("00000000-0000-7000-8000-000000009101")
SOURCE_OBJECT_ID = uuid.UUID("00000000-0000-7000-8000-000000009102")


def request(media_type: str = "application/xml") -> ExtractionInput:
    return ExtractionInput(
        document_version_id=DOCUMENT_VERSION_ID,
        source_object_id=SOURCE_OBJECT_ID,
        media_type=media_type,
        extractor_name=RedditSourceExtractor.name,
        extractor_version=RedditSourceExtractor.version,
    )


ATOM_WITH_BODY = b"""<entry xmlns="http://www.w3.org/2005/Atom">
  <id>t3_example1</id>
  <title>Lights above the lake</title>
  <author><name>/u/observer42</name></author>
  <published>2026-09-16T12:00:00+00:00</published>
  <link href="https://www.reddit.com/r/UFOs/comments/example1/lights/" />
  <content type="html">&lt;table&gt;&lt;tr&gt;&lt;td&gt;
    &lt;a href=&quot;https://example.invalid/preview&quot;&gt;preview&lt;/a&gt;
    &lt;/td&gt;&lt;td&gt;&lt;div class=&quot;md&quot;&gt;
    &lt;p&gt;I watched three lights move silently above the lake.&lt;/p&gt;
    &lt;p&gt;The observation lasted about two minutes.&lt;/p&gt;
    &lt;/div&gt;&lt;br/&gt; submitted by &lt;a&gt;/u/observer42&lt;/a&gt;
    &lt;a href=&quot;https://www.reddit.com/comments/example1&quot;&gt;comments&lt;/a&gt;
    &lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;</content>
</entry>"""


def test_atom_extracts_only_reddit_body_with_metadata() -> None:
    result = RedditSourceExtractor().extract(request(), ATOM_WITH_BODY)

    assert result.outcome is ExtractionOutcome.SUCCEEDED
    assert result.title == "Lights above the lake"
    assert result.author == "/u/observer42"
    assert result.source_date == "2026-09-16T12:00:00+00:00"
    assert "three lights move silently" in result.text
    assert "two minutes" in result.text
    assert "preview" not in result.text
    assert "submitted by" not in result.text
    assert "comments" not in result.text
    assert result.location_map[0]["kind"] == "reddit_post_body"


@pytest.mark.parametrize("body", [b"", b"[removed]", b"[deleted]"])
def test_atom_without_usable_body_fails_closed(body: bytes) -> None:
    payload = (
        b'<entry><title>Only a title</title><content type="text">'
        + body
        + b"</content></entry>"
    )

    result = RedditSourceExtractor().extract(request(), payload)

    assert result.outcome is ExtractionOutcome.FAILED
    assert result.error_code == "reddit_body_unavailable"
    assert result.text == ""
    assert result.output_sha256 is None


def test_official_api_json_extracts_same_post_body() -> None:
    payload = json.dumps(
        {
            "data": {
                "children": [
                    {
                        "kind": "t3",
                        "data": {
                            "name": "t3_example1",
                            "title": "Lights above the lake",
                            "selftext": "A detailed first-person report from the observer.",
                            "author": "observer42",
                            "created_utc": 1_789_560_000,
                        },
                    }
                ]
            }
        }
    ).encode()

    result = RedditSourceExtractor().extract(request("application/json"), payload)

    assert result.outcome is ExtractionOutcome.SUCCEEDED
    assert "detailed first-person report" in result.text
    assert result.author == "observer42"
    assert result.source_date is not None


def test_invalid_api_json_and_missing_api_body_are_visible() -> None:
    invalid = RedditSourceExtractor().extract(request("application/json"), b"not-json")
    missing = RedditSourceExtractor().extract(
        request("application/json"),
        b'{"data":{"children":[{"data":{"title":"title","selftext":""}}]}}',
    )

    assert invalid.error_code == "reddit_api_invalid_json"
    assert missing.error_code == "reddit_body_unavailable"


@pytest.mark.parametrize(
    "payload",
    [
        b"<html><title>Prove your humanity</title><body>continue</body></html>",
        b"<html><body>This site is not for bots.</body></html>",
        b'<html><body><form id="captcha">CAPTCHA challenge</form></body></html>',
        b"<html><title>Reddit security verification challenge</title></html>",
    ],
)
def test_challenge_html_fails_without_derived_text(payload: bytes) -> None:
    result = RedditSourceExtractor().extract(request("text/html"), payload)

    assert result.outcome is ExtractionOutcome.FAILED
    assert result.error_code == "source_bot_challenge"
    assert result.text == ""
    assert result.output_sha256 is None
