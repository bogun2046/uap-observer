from __future__ import annotations

import uuid

from uap_platform.documents import ExtractionInput, ExtractionOutcome, FeedXmlExtractor


def request() -> ExtractionInput:
    return ExtractionInput(
        document_version_id=uuid.uuid4(),
        source_object_id=uuid.uuid4(),
        media_type="application/atom+xml",
        extractor_name=FeedXmlExtractor.name,
        extractor_version=FeedXmlExtractor.version,
    )


def test_document_feed_uses_first_entry_and_preserves_offsets() -> None:
    payload = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Original post</title><author><name>observer42</name></author>
      <content>A witness reported three lights.</content></entry>
      <entry><title>Comment</title><content>This must not replace the post.</content></entry>
    </feed>"""

    result = FeedXmlExtractor().extract(request(), payload)

    assert result.outcome is ExtractionOutcome.SUCCEEDED
    assert result.title == "Original post"
    assert result.author == "observer42"
    assert "three lights" in result.text
    assert "must not replace" not in result.text
    assert result.location_map


def test_document_feed_fails_closed_for_invalid_xml() -> None:
    result = FeedXmlExtractor().extract(request(), b"<feed>")
    assert result.outcome is ExtractionOutcome.FAILED
    assert result.error_code == "invalid_xml"
