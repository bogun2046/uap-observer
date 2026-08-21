from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence

import pytest

from uap_platform import knowledge as knowledge_package
from uap_platform.knowledge import (
    FROZEN_REASON_CODES,
    MAX_EVIDENCE_UTF8_BYTES,
    AcceptedLocator,
    AnchorStatus,
    ExtractionAnchor,
    ExtractionRecord,
    MappingClass,
    MappingReport,
    SourceCandidate,
    SourceLocator,
    map_knowledge_result,
    map_locator,
    resolve_extraction_anchor,
)
from uap_platform.knowledge.contracts import DuplicatePolicy
from uap_platform.knowledge.locators import LocatorRejected
from uap_platform.knowledge.reasons import (
    KNOWLEDGE_EXTRACTION_AMBIGUOUS,
    KNOWLEDGE_EXTRACTION_MISMATCH,
    KNOWLEDGE_EXTRACTION_MISSING,
    KNOWLEDGE_LOCATOR_UNMAPPABLE,
    KNOWLEDGE_PAYLOAD_MISMATCH,
    LOCATOR_AXIS_CONFLICT,
    LOCATOR_CROSS_AXIS_MISMATCH,
    LOCATOR_DUPLICATE,
    LOCATOR_END_NOT_AFTER_START,
    LOCATOR_EXCERPT_TOO_LARGE,
    LOCATOR_LOCATION_MAP_INVALID,
    LOCATOR_OUT_OF_RANGE,
    LOCATOR_PAGE_RANGE_INVALID,
    LOCATOR_PDF_PAGE_MISSING,
    LOCATOR_TIME_MISSING,
    LOCATOR_TIME_RANGE_INVALID,
)

DOC = uuid.UUID("00000000-0000-7000-8000-000000000001")
EXT_A = uuid.UUID("00000000-0000-7000-8000-0000000000aa")
EXT_B = uuid.UUID("00000000-0000-7000-8000-0000000000bb")
HASH_A = hashlib.sha256(b"input-a").hexdigest()
HASH_B = hashlib.sha256(b"input-b").hexdigest()
TEXT = "page-one.\npage-two.\npage-three."
#                012345678901234567890123456789
# page1 chars 0-9, page2 10-19, page3 20-31


def record(
    extraction_id: uuid.UUID,
    output_sha256: str,
    *,
    outcome: str = "succeeded",
    domain: str = "derived",
    stored: str | None = None,
    name: str = "text",
    version: str = "1.0.0",
) -> ExtractionRecord:
    return ExtractionRecord(
        extraction_id=extraction_id,
        document_version_id=DOC,
        outcome=outcome,
        output_sha256=output_sha256,
        stored_domain=domain,
        stored_sha256=output_sha256 if stored is None else stored,
        extractor_name=name,
        extractor_version=version,
    )


def pdf_map() -> list[dict[str, object]]:
    return [
        {"kind": "pdf_page", "page_start": 1, "page_end": 1, "char_start": 0, "char_end": 9},
        {"kind": "pdf_page", "page_start": 2, "page_end": 2, "char_start": 10, "char_end": 19},
        {"kind": "pdf_page", "page_start": 3, "page_end": 3, "char_start": 20, "char_end": 31},
    ]


def cue_map() -> list[dict[str, object]]:
    return [
        {
            "kind": "subtitle_cue",
            "time_start_ms": 0,
            "time_end_ms": 1000,
            "char_start": 0,
            "char_end": 9,
        },
        {
            "kind": "subtitle_cue",
            "time_start_ms": 1000,
            "time_end_ms": 2000,
            "char_start": 10,
            "char_end": 19,
        },
        {
            "kind": "subtitle_cue",
            "time_start_ms": 2000,
            "time_end_ms": 3000,
            "char_start": 20,
            "char_end": 31,
        },
    ]


def matched_records() -> tuple[ExtractionRecord, ...]:
    return (record(EXT_A, HASH_A),)


def frozen_matched() -> ExtractionAnchor:
    return ExtractionAnchor(status=AnchorStatus.MATCHED, extraction_id=EXT_A)


def frozen_missing() -> ExtractionAnchor:
    return ExtractionAnchor(status=AnchorStatus.MISSING, extraction_id=None)


def frozen_ambiguous() -> ExtractionAnchor:
    return ExtractionAnchor(status=AnchorStatus.AMBIGUOUS, extraction_id=None)


def candidate(*locators: SourceLocator, ordinal: int = 0) -> SourceCandidate:
    return SourceCandidate(ordinal=ordinal, locators=locators)


def report(
    locators: Sequence[SourceLocator],
    *,
    text: str = TEXT,
    location_map: Sequence[dict[str, object]] | None = None,
    records: Sequence[ExtractionRecord] | None = None,
    payload: ExtractionAnchor | None = None,
    policy: DuplicatePolicy = "claim",
    extra_candidates: Sequence[SourceCandidate] = (),
    input_sha256: str = HASH_A,
) -> MappingReport:
    items = [candidate(*tuple(locators))]
    items.extend(extra_candidates)
    return map_knowledge_result(
        candidates=items,
        payload_anchor=payload if payload is not None else frozen_matched(),
        records=records if records is not None else matched_records(),
        document_version_id=DOC,
        input_sha256=input_sha256,
        extracted_text=text,
        location_map=list(location_map or []),
        duplicate_policy=policy,
    )


def test_g8_07_matched_missing_ambiguous_and_later_hash_does_not_change_anchor() -> None:
    matched = resolve_extraction_anchor(
        (record(EXT_A, HASH_A, name="html"),),
        document_version_id=DOC,
        input_sha256=HASH_A,
    )
    missing = resolve_extraction_anchor(
        (record(EXT_A, HASH_B),),
        document_version_id=DOC,
        input_sha256=HASH_A,
    )
    ambiguous = resolve_extraction_anchor(
        (
            record(EXT_A, HASH_A, name="html", version="1"),
            record(EXT_B, HASH_A, name="text", version="2"),
        ),
        document_version_id=DOC,
        input_sha256=HASH_A,
    )
    assert matched.status is AnchorStatus.MATCHED and matched.extraction_id == EXT_A
    assert missing.status is AnchorStatus.MISSING and missing.extraction_id is None
    assert ambiguous.status is AnchorStatus.AMBIGUOUS and ambiguous.extraction_id is None
    assert {item.value for item in AnchorStatus} == {"matched", "missing", "ambiguous"}

    after_new_hash = resolve_extraction_anchor(
        (record(EXT_A, HASH_A), record(EXT_B, HASH_B, name="html", version="9.9.9")),
        document_version_id=DOC,
        input_sha256=HASH_A,
    )
    assert after_new_hash == matched


def test_g8_07_does_not_break_ties_with_recency_or_extractor() -> None:
    rows = (
        record(EXT_B, HASH_A, name="z-last", version="99"),
        record(EXT_A, HASH_A, name="a-first", version="00"),
    )
    anchor = resolve_extraction_anchor(rows, document_version_id=DOC, input_sha256=HASH_A)
    assert anchor.status is AnchorStatus.AMBIGUOUS


def test_g8_07_inconsistent_object_does_not_create_mismatch_status() -> None:
    anchor = resolve_extraction_anchor(
        (record(EXT_A, HASH_A, domain="raw"),),
        document_version_id=DOC,
        input_sha256=HASH_A,
    )
    assert anchor.status is AnchorStatus.MISSING
    assert anchor.extraction_id is None


def test_g8_07_mapper_uses_frozen_payload_not_current_rows() -> None:
    locator = SourceLocator(locator_type="text", start=0, end=4)
    later_same_hash = (record(EXT_A, HASH_A), record(EXT_B, HASH_A, name="later", version="9"))
    recounted = resolve_extraction_anchor(
        later_same_hash, document_version_id=DOC, input_sha256=HASH_A
    )
    assert recounted.status is AnchorStatus.AMBIGUOUS
    assert recounted.extraction_id is None

    frozen = report((locator,), records=later_same_hash, payload=frozen_matched())
    assert frozen.classification is MappingClass.MATERIALIZABLE
    assert frozen.anchor == frozen_matched()
    assert frozen.accepted_candidates[0].accepted_locators[0].envelope["extraction_id"] == str(
        EXT_A
    )

    still_missing = report((locator,), records=matched_records(), payload=frozen_missing())
    assert still_missing.classification is MappingClass.TERMINAL_EXTRACTION_MISSING
    assert still_missing.anchor == frozen_missing()
    assert still_missing.reason_codes()[0] == KNOWLEDGE_EXTRACTION_MISSING


def test_g8_07_nonempty_missing_and_ambiguous_are_terminal() -> None:
    locator = SourceLocator(locator_type="text", start=0, end=4)
    missing = report((locator,), records=(), payload=frozen_missing())
    ambiguous = report(
        (locator,),
        records=(record(EXT_A, HASH_A, name="a"), record(EXT_B, HASH_A, name="b")),
        payload=frozen_ambiguous(),
    )
    mismatch = report((locator,), records=(record(EXT_A, HASH_A, domain="raw"),))
    assert missing.classification is MappingClass.TERMINAL_EXTRACTION_MISSING
    assert missing.reason_codes()[0] == KNOWLEDGE_EXTRACTION_MISSING
    assert ambiguous.classification is MappingClass.TERMINAL_EXTRACTION_AMBIGUOUS
    assert ambiguous.reason_codes()[0] == KNOWLEDGE_EXTRACTION_AMBIGUOUS
    assert mismatch.classification is MappingClass.TERMINAL_EXTRACTION_MISMATCH
    assert mismatch.reason_codes()[0] == KNOWLEDGE_EXTRACTION_MISMATCH
    assert mismatch.anchor.status is AnchorStatus.MATCHED
    assert mismatch.anchor.extraction_id == EXT_A


def test_g8_07_malformed_payload_is_payload_mismatch() -> None:
    locator = SourceLocator(locator_type="text", start=0, end=4)
    matched_without_id = ExtractionAnchor(status=AnchorStatus.MATCHED, extraction_id=None)
    missing_with_id = ExtractionAnchor(status=AnchorStatus.MISSING, extraction_id=EXT_A)
    assert report((locator,), payload=matched_without_id).reason_codes()[0] == (
        KNOWLEDGE_PAYLOAD_MISMATCH
    )
    assert report((locator,), payload=missing_with_id).reason_codes()[0] == (
        KNOWLEDGE_PAYLOAD_MISMATCH
    )


def test_g8_08_five_locator_types_and_envelope_identity() -> None:
    cases = [
        SourceLocator(locator_type="text", start=0, end=8),
        SourceLocator(locator_type="html", start=0, end=8),
        SourceLocator(locator_type="pdf", start=10, end=19, page_start=2, page_end=2),
        SourceLocator(locator_type="video", start=10, end=19, time_start_ms=1000, time_end_ms=2000),
        SourceLocator(locator_type="audio", start=10, end=19, time_start_ms=1000, time_end_ms=2000),
    ]
    maps = {
        "text": [],
        "html": [],
        "pdf": pdf_map(),
        "video": cue_map(),
        "audio": cue_map(),
    }
    envelopes: list[dict[str, object]] = []
    for locator in cases:
        mapped = map_locator(
            locator,
            extracted_text=TEXT,
            location_map=maps[locator.locator_type],
            document_version_id=DOC,
            extraction_id=EXT_A,
            input_sha256=HASH_A,
            locator_ordinal=0,
        )
        envelope = mapped.envelope
        assert envelope["locator_schema_version"] == "evidence-locator.v2"
        assert envelope["document_version_id"] == str(DOC)
        assert envelope["extraction_id"] == str(EXT_A)
        assert envelope["input_sha256"] == HASH_A
        source = envelope["source_locator"]
        assert isinstance(source, dict)
        assert source["locator_type"] == locator.locator_type
        assert source["start"] == locator.start
        assert source["end"] == locator.end
        assert "page_start" not in source or locator.page_start is not None
        if locator.locator_type in ("text", "html"):
            assert mapped.axes.char_start == locator.start
            assert mapped.axes.page_start is None
            assert mapped.axes.time_start_ms is None
        if locator.locator_type == "pdf":
            assert mapped.axes.char_start is None
            assert mapped.axes.page_start == 2
            assert mapped.axes.time_start_ms is None
        if locator.locator_type in ("video", "audio"):
            assert mapped.axes.char_start is None
            assert mapped.axes.page_start is None
            assert mapped.axes.time_start_ms == 1000
        assert mapped.evidence_text == TEXT[locator.start : locator.end]
        envelopes.append(envelope)
    other_extraction = map_locator(
        cases[0],
        extracted_text=TEXT,
        location_map=[],
        document_version_id=DOC,
        extraction_id=EXT_B,
        input_sha256=HASH_A,
        locator_ordinal=0,
    )
    other_hash = map_locator(
        cases[0],
        extracted_text=TEXT,
        location_map=[],
        document_version_id=DOC,
        extraction_id=EXT_A,
        input_sha256=HASH_B,
        locator_ordinal=0,
    )
    assert other_extraction.envelope != envelopes[0]
    assert other_hash.envelope != envelopes[0]
    assert other_extraction.envelope["extraction_id"] == str(EXT_B)
    assert other_hash.envelope["input_sha256"] == HASH_B


def test_g8_08_python_does_not_emit_span_hash() -> None:
    mapped = map_locator(
        SourceLocator(locator_type="text", start=0, end=4),
        extracted_text=TEXT,
        location_map=[],
        document_version_id=DOC,
        extraction_id=EXT_A,
        input_sha256=HASH_A,
        locator_ordinal=0,
    )
    assert "digest" not in AcceptedLocator.__dataclass_fields__
    assert not hasattr(mapped, "digest")
    assert not hasattr(knowledge_package, "canonical_locator_digest")
    assert not hasattr(knowledge_package, "canonical_envelope_text")


def test_g8_08_claim_duplicate_keeps_first_ordinal() -> None:
    locator = SourceLocator(locator_type="text", start=0, end=4)
    result = report((locator, locator, SourceLocator(locator_type="text", start=5, end=8)))
    assert result.classification is MappingClass.MATERIALIZABLE
    accepted = result.accepted_candidates[0]
    assert [item.locator_ordinal for item in accepted.accepted_locators] == [0, 2]
    assert accepted.rejected_locators[0].locator_ordinal == 1
    assert accepted.rejected_locators[0].reason_code == LOCATOR_DUPLICATE
    assert accepted.rejected_locators[0].reason_code == "locator_duplicate"
    assert result.rejected_locator_count == 1


def test_g8_08_entity_keeps_duplicate_ordinals() -> None:
    locator = SourceLocator(locator_type="text", start=0, end=4)
    result = report((locator, locator), policy="entity")
    accepted = result.accepted_candidates[0]
    assert [item.locator_ordinal for item in accepted.accepted_locators] == [0, 1]


def test_g8_09_pdf_and_media_cross_axis() -> None:
    good_pdf = SourceLocator(locator_type="pdf", start=10, end=19, page_start=2, page_end=2)
    wrong_page = SourceLocator(locator_type="pdf", start=10, end=19, page_start=3, page_end=3)
    good_video = SourceLocator(
        locator_type="video", start=10, end=19, time_start_ms=1000, time_end_ms=2000
    )
    two_cues_one_time = SourceLocator(
        locator_type="video", start=10, end=31, time_start_ms=1000, time_end_ms=2000
    )
    extra_time = SourceLocator(
        locator_type="audio", start=10, end=19, time_start_ms=1000, time_end_ms=3000
    )
    assert report((good_pdf,), location_map=pdf_map()).classification is MappingClass.MATERIALIZABLE
    assert (
        report((wrong_page,), location_map=pdf_map()).reason_codes()[0]
        == LOCATOR_CROSS_AXIS_MISMATCH
    )
    assert (
        report((good_video,), location_map=cue_map()).classification is MappingClass.MATERIALIZABLE
    )
    assert (
        report((two_cues_one_time,), location_map=cue_map()).reason_codes()[0]
        == LOCATOR_CROSS_AXIS_MISMATCH
    )
    assert (
        report((extra_time,), location_map=cue_map()).reason_codes()[0]
        == LOCATOR_CROSS_AXIS_MISMATCH
    )


def test_g8_09_location_map_fail_closed() -> None:
    locator = SourceLocator(locator_type="pdf", start=10, end=19, page_start=2, page_end=2)
    with pytest.raises(LocatorRejected) as error:
        map_locator(
            locator,
            extracted_text=TEXT,
            location_map="not-a-list",  # type: ignore[arg-type]
            document_version_id=DOC,
            extraction_id=EXT_A,
            input_sha256=HASH_A,
            locator_ordinal=0,
        )
    assert error.value.reason_code == LOCATOR_LOCATION_MAP_INVALID
    overlapping = [
        {"kind": "pdf_page", "page_start": 1, "page_end": 1, "char_start": 0, "char_end": 15},
        {"kind": "pdf_page", "page_start": 2, "page_end": 2, "char_start": 10, "char_end": 19},
    ]
    assert report((locator,), location_map=overlapping).reason_codes()[0] == (
        LOCATOR_LOCATION_MAP_INVALID
    )
    missing_fields = [{"kind": "pdf_page", "char_start": 10, "char_end": 19}]
    assert report((locator,), location_map=missing_fields).reason_codes()[0] == (
        LOCATOR_LOCATION_MAP_INVALID
    )
    inverted_chars = [
        {"kind": "pdf_page", "page_start": 2, "page_end": 2, "char_start": 19, "char_end": 10}
    ]
    assert report((locator,), location_map=inverted_chars).reason_codes()[0] == (
        LOCATOR_LOCATION_MAP_INVALID
    )
    zero_page = [
        {"kind": "pdf_page", "page_start": 0, "page_end": 1, "char_start": 10, "char_end": 19}
    ]
    assert report((locator,), location_map=zero_page).reason_codes()[0] == (
        LOCATOR_LOCATION_MAP_INVALID
    )
    with pytest.raises(LocatorRejected) as bad_row:
        map_locator(
            locator,
            extracted_text=TEXT,
            location_map=["not-a-row"],  # type: ignore[list-item]
            document_version_id=DOC,
            extraction_id=EXT_A,
            input_sha256=HASH_A,
            locator_ordinal=0,
        )
    assert bad_row.value.reason_code == LOCATOR_LOCATION_MAP_INVALID
    mixed = [*pdf_map(), {"kind": "html_block", "char_start": 0, "char_end": 9}]
    assert report((locator,), location_map=mixed).classification is MappingClass.MATERIALIZABLE
    kindless: list[dict[str, object]] = [
        {"page_start": 2, "page_end": 2, "char_start": 10, "char_end": 19}
    ]
    assert report((locator,), location_map=kindless).classification is MappingClass.MATERIALIZABLE
    assert report((locator,), location_map=cue_map()).reason_codes()[0] == (
        LOCATOR_LOCATION_MAP_INVALID
    )
    cue_kindless: list[dict[str, object]] = [
        {"time_start_ms": 1000, "time_end_ms": 2000, "char_start": 10, "char_end": 19}
    ]
    media = SourceLocator(
        locator_type="video", start=10, end=19, time_start_ms=1000, time_end_ms=2000
    )
    assert report((media,), location_map=cue_kindless).classification is MappingClass.MATERIALIZABLE
    assert report((media,), location_map=kindless).reason_codes()[0] == LOCATOR_LOCATION_MAP_INVALID
    inverted_time = [
        {
            "kind": "subtitle_cue",
            "time_start_ms": 2000,
            "time_end_ms": 1000,
            "char_start": 10,
            "char_end": 19,
        }
    ]
    assert report((media,), location_map=inverted_time).reason_codes()[0] == (
        LOCATOR_LOCATION_MAP_INVALID
    )
    separator_only = SourceLocator(locator_type="pdf", start=9, end=10, page_start=1, page_end=1)
    assert (
        report((separator_only,), location_map=pdf_map()).reason_codes()[0]
        == LOCATOR_CROSS_AXIS_MISMATCH
    )


def test_g8_10_axis_conflicts_empty_candidate_and_invalid_hash() -> None:
    pdf_with_time = SourceLocator(
        locator_type="pdf",
        start=10,
        end=19,
        page_start=2,
        page_end=2,
        time_start_ms=0,
        time_end_ms=1,
    )
    video_with_page = SourceLocator(
        locator_type="video", start=10, end=19, time_start_ms=1000, time_end_ms=2000, page_start=1
    )
    zero_page = SourceLocator(locator_type="pdf", start=10, end=19, page_start=0, page_end=1)
    negative_time = SourceLocator(
        locator_type="audio", start=10, end=19, time_start_ms=-1, time_end_ms=10
    )
    assert (
        report((pdf_with_time,), location_map=pdf_map()).reason_codes()[0] == LOCATOR_AXIS_CONFLICT
    )
    assert (
        report((video_with_page,), location_map=cue_map()).reason_codes()[0]
        == LOCATOR_AXIS_CONFLICT
    )
    assert (
        report((zero_page,), location_map=pdf_map()).reason_codes()[0] == LOCATOR_PAGE_RANGE_INVALID
    )
    assert (
        report((negative_time,), location_map=cue_map()).reason_codes()[0]
        == LOCATOR_TIME_RANGE_INVALID
    )

    empty = map_knowledge_result(
        candidates=(SourceCandidate(ordinal=0, locators=()),),
        payload_anchor=frozen_matched(),
        records=matched_records(),
        document_version_id=DOC,
        input_sha256=HASH_A,
        extracted_text=TEXT,
        location_map=[],
        duplicate_policy="claim",
    )
    assert empty.classification is MappingClass.TERMINAL_UNMAPPABLE
    assert empty.reason_codes()[0] == KNOWLEDGE_LOCATOR_UNMAPPABLE

    other_doc = uuid.UUID("00000000-0000-7000-8000-000000000099")
    failed = record(EXT_A, HASH_A, outcome="failed")
    wrong_doc = ExtractionRecord(
        extraction_id=EXT_A,
        document_version_id=other_doc,
        outcome="succeeded",
        output_sha256=HASH_A,
        stored_domain="derived",
        stored_sha256=HASH_A,
    )
    locator = SourceLocator(locator_type="text", start=0, end=4)
    assert (
        resolve_extraction_anchor(
            (failed, wrong_doc), document_version_id=DOC, input_sha256=HASH_A
        ).status
        is AnchorStatus.MISSING
    )
    assert report((locator,), records=(failed,)).classification is (
        MappingClass.TERMINAL_EXTRACTION_MISMATCH
    )
    assert report((locator,), records=(wrong_doc,)).classification is (
        MappingClass.TERMINAL_EXTRACTION_MISMATCH
    )
    invalid_hash = map_knowledge_result(
        candidates=(candidate(locator),),
        payload_anchor=frozen_matched(),
        records=matched_records(),
        document_version_id=DOC,
        input_sha256="not-a-sha256",
        extracted_text=TEXT,
        location_map=[],
        duplicate_policy="claim",
    )
    assert invalid_hash.classification is MappingClass.TERMINAL_EXTRACTION_MISMATCH
    assert invalid_hash.anchor.status is AnchorStatus.MATCHED
    assert (
        resolve_extraction_anchor(
            matched_records(), document_version_id=DOC, input_sha256="not-a-sha256"
        ).status
        is AnchorStatus.MISSING
    )


def test_g8_10_result_classes_and_boundary_rejections() -> None:
    empty = map_knowledge_result(
        candidates=(),
        payload_anchor=frozen_missing(),
        records=(),
        document_version_id=DOC,
        input_sha256=HASH_A,
        extracted_text=TEXT,
        location_map=[],
        duplicate_policy="claim",
    )
    assert empty.classification is MappingClass.EMPTY_VALID
    assert empty.anchor == frozen_missing()

    good = SourceLocator(locator_type="text", start=0, end=4)
    bad = SourceLocator(locator_type="text", start=0, end=4, page_start=1, page_end=1)
    partial = report(
        (good,),
        extra_candidates=(candidate(bad, ordinal=1),),
    )
    assert partial.classification is MappingClass.MATERIALIZABLE
    assert len(partial.accepted_candidates) == 1
    assert partial.rejected_candidates[0].reason_code == KNOWLEDGE_LOCATOR_UNMAPPABLE
    assert LOCATOR_AXIS_CONFLICT in partial.reason_codes()

    all_bad = report((bad, SourceLocator(locator_type="text", start=90, end=91)))
    assert all_bad.classification is MappingClass.TERMINAL_UNMAPPABLE

    codes = {
        SourceLocator(locator_type="text", start=4, end=4): LOCATOR_END_NOT_AFTER_START,
        SourceLocator(locator_type="text", start=0, end=400): LOCATOR_OUT_OF_RANGE,
        SourceLocator(locator_type="html", start=0, end=4, time_start_ms=0, time_end_ms=1): (
            LOCATOR_AXIS_CONFLICT
        ),
        SourceLocator(locator_type="pdf", start=10, end=19): LOCATOR_PDF_PAGE_MISSING,
        SourceLocator(locator_type="pdf", start=10, end=19, page_start=3, page_end=1): (
            LOCATOR_PAGE_RANGE_INVALID
        ),
        SourceLocator(locator_type="video", start=10, end=19): LOCATOR_TIME_MISSING,
        SourceLocator(
            locator_type="audio", start=10, end=19, time_start_ms=50, time_end_ms=50
        ): LOCATOR_TIME_RANGE_INVALID,
    }
    for locator, expected in codes.items():
        location_map: list[dict[str, object]] = []
        if locator.locator_type == "pdf":
            location_map = pdf_map()
        if locator.locator_type in ("video", "audio"):
            location_map = cue_map()
        actual = report((locator,), location_map=location_map).reason_codes()[0]
        assert actual == expected, (locator, actual, expected)


def test_g8_10_excerpt_utf8_limit_does_not_truncate() -> None:
    ascii_ok = "a" * MAX_EVIDENCE_UTF8_BYTES
    ascii_over = "a" * (MAX_EVIDENCE_UTF8_BYTES + 1)
    ok = report(
        (SourceLocator(locator_type="text", start=0, end=len(ascii_ok)),),
        text=ascii_ok,
    )
    over = report(
        (SourceLocator(locator_type="text", start=0, end=len(ascii_over)),),
        text=ascii_over,
    )
    assert ok.classification is MappingClass.MATERIALIZABLE
    assert ok.accepted_candidates[0].accepted_locators[0].evidence_text == ascii_ok
    assert over.reason_codes()[0] == LOCATOR_EXCERPT_TOO_LARGE

    cjk = "汉" * 2730  # 8190 bytes
    cjk_over = "汉" * 2731  # 8193 bytes
    assert (
        report(
            (SourceLocator(locator_type="text", start=0, end=len(cjk)),),
            text=cjk,
        ).classification
        is MappingClass.MATERIALIZABLE
    )
    assert (
        report(
            (SourceLocator(locator_type="text", start=0, end=len(cjk_over)),),
            text=cjk_over,
        ).reason_codes()[0]
        == LOCATOR_EXCERPT_TOO_LARGE
    )
    emoji = "😀" * 2048  # 8192 bytes
    emoji_over = "😀" * 2049
    assert (
        report(
            (SourceLocator(locator_type="text", start=0, end=len(emoji)),),
            text=emoji,
        ).classification
        is MappingClass.MATERIALIZABLE
    )
    assert (
        report(
            (SourceLocator(locator_type="text", start=0, end=len(emoji_over)),),
            text=emoji_over,
        ).reason_codes()[0]
        == LOCATOR_EXCERPT_TOO_LARGE
    )


def test_frozen_reason_codes_are_complete() -> None:
    assert LOCATOR_DUPLICATE in FROZEN_REASON_CODES
    assert KNOWLEDGE_LOCATOR_UNMAPPABLE in FROZEN_REASON_CODES
    assert KNOWLEDGE_EXTRACTION_MISMATCH in FROZEN_REASON_CODES
    assert KNOWLEDGE_PAYLOAD_MISMATCH in FROZEN_REASON_CODES
    assert len(FROZEN_REASON_CODES) == 19


def test_map_locator_does_not_guess_or_clamp() -> None:
    locator = SourceLocator(locator_type="text", start=0, end=1000)
    with pytest.raises(LocatorRejected) as error:
        map_locator(
            locator,
            extracted_text="short",
            location_map=[],
            document_version_id=DOC,
            extraction_id=EXT_A,
            input_sha256=HASH_A,
            locator_ordinal=0,
        )
    assert error.value.reason_code == LOCATOR_OUT_OF_RANGE
