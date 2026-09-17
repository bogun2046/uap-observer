from __future__ import annotations

import json
from decimal import Decimal

from uap_platform.model_governance import ModelTaskType
from uap_platform.model_governance.schemas import schema_json_schema, validate_output
from uap_platform.v1 import load_v1_config
from uap_platform.v1.prompts import v1_prompts


def test_approved_source_config_keeps_v1_1_to_one_source() -> None:
    config = load_v1_config()
    assert config.deepseek_monthly_budget_cny == Decimal("20.00")
    assert config.deepseek_budget_warning_ratio_provisional == Decimal("0.80")
    assert config.v1_1_source.slug == "reddit-ufos-new"
    assert config.v1_1_source.fetch_url == "https://www.reddit.com/r/UFOs/new/.rss"
    assert {item.source_type for item in config.sources} == {"rss", "web"}
    assert {item.slug for item in config.sources} == {
        "reddit-ufos-new",
        "reddit-uap-new",
        "war-gov-pursue",
        "war-gov-uap-releases",
    }


def test_v1_prompts_map_to_existing_task_types() -> None:
    prompts = v1_prompts()
    assert {item.task_type for item in prompts} == {
        ModelTaskType.CLASSIFICATION,
        ModelTaskType.SUMMARY,
        ModelTaskType.CLAIM_EXTRACTION,
        ModelTaskType.ENTITY_EXTRACTION,
    }
    claim_prompt = next(
        item for item in prompts if item.task_type is ModelTaskType.CLAIM_EXTRACTION
    )
    assert "source_statement" in str(claim_prompt.output_schema)
    assert "Never decide whether UFO claims are true" in claim_prompt.system_template


def test_classification_prompt_v120_explicitly_matches_frozen_schema() -> None:
    prompts = v1_prompts()
    classification = next(
        item for item in prompts if item.task_type is ModelTaskType.CLASSIFICATION
    )

    assert classification.version == "v1.2.0"
    assert "relevance" in classification.user_template
    assert "relevance.decision" in classification.user_template
    assert "relevance.reason" in classification.user_template
    assert "relevance.confidence" in classification.user_template
    assert "suggested_document_category" in classification.user_template
    assert '"additionalProperties": false' in classification.user_template
    assert "Return exactly one JSON object" in classification.user_template
    assert "Do not add any field" in classification.user_template
    for value in (
        "relevant",
        "irrelevant",
        "uncertain",
        "official_report",
        "government_document",
        "military",
        "scientific_research",
        "historical_event",
        "sighting",
        "disputed_event",
        "other",
    ):
        assert value in classification.user_template


def test_classification_contract_accepts_frozen_shape_and_rejects_old_or_extra_fields() -> None:
    valid = {
        "relevance": {
            "decision": "relevant",
            "reason": "The source describes a UAP sighting.",
            "confidence": 0.9,
        },
        "labels": ["UAP", "sighting"],
        "suggested_document_category": "sighting",
    }
    old_shape = {
        "relevant": True,
        "reason": "The source describes a UAP sighting.",
        "confidence": 0.9,
        "category": "sighting report",
    }

    parsed, valid_errors = validate_output(ModelTaskType.CLASSIFICATION, valid)
    old_parsed, old_errors = validate_output(ModelTaskType.CLASSIFICATION, old_shape)
    extra_parsed, extra_errors = validate_output(
        ModelTaskType.CLASSIFICATION, {**valid, "unexpected": True}
    )

    assert parsed == valid
    assert valid_errors == ()
    assert old_parsed is None
    assert old_errors
    assert extra_parsed is None
    assert extra_errors


def test_downstream_prompts_embed_their_exact_frozen_schemas() -> None:
    prompts = {item.task_type: item for item in v1_prompts()}
    for task_type in (
        ModelTaskType.SUMMARY,
        ModelTaskType.CLAIM_EXTRACTION,
        ModelTaskType.ENTITY_EXTRACTION,
    ):
        prompt = prompts[task_type]
        schema_text = json.dumps(
            schema_json_schema(task_type), ensure_ascii=False, sort_keys=True, indent=2
        )
        assert prompt.version == "v1.2.0"
        assert prompt.output_schema == schema_json_schema(task_type)
        assert schema_text in prompt.user_template
        assert "Return exactly one JSON object" in prompt.user_template
        assert "additionalProperties" in prompt.user_template
        assert "Do not add schema fields" in prompt.user_template

    assert "summary and bullets are required" in prompts[ModelTaskType.SUMMARY].user_template
    assert "bullet_points" in prompts[ModelTaskType.SUMMARY].user_template
    assert "claims" in prompts[ModelTaskType.CLAIM_EXTRACTION].user_template
    assert "evidence" in prompts[ModelTaskType.CLAIM_EXTRACTION].user_template
    assert "evidence_offsets" in prompts[ModelTaskType.CLAIM_EXTRACTION].user_template
    assert "entities" in prompts[ModelTaskType.ENTITY_EXTRACTION].user_template
    assert "people" in prompts[ModelTaskType.ENTITY_EXTRACTION].user_template


def test_downstream_contracts_accept_frozen_shapes_and_reject_observed_old_shapes() -> None:
    evidence = {"locator_type": "text", "start": 0, "end": 12}
    valid_outputs: dict[ModelTaskType, dict[str, object]] = {
        ModelTaskType.SUMMARY: {
            "language_code": "zh-CN",
            "summary": "这是一个中性的中文摘要。",
            "bullets": ["来源描述了一段未经证实的经历。"],
        },
        ModelTaskType.CLAIM_EXTRACTION: {
            "claims": [
                {
                    "claim": "来源描述了一段经历。",
                    "source_statement": "来源称发生了一段经历。",
                    "speaker": None,
                    "claim_type": "attribution",
                    "assertion_status": "reported",
                    "evidence": [evidence],
                }
            ]
        },
        ModelTaskType.ENTITY_EXTRACTION: {
            "entities": [
                {
                    "name": "示例地点",
                    "entity_type": "location",
                    "aliases": [],
                    "evidence": [evidence],
                }
            ]
        },
    }
    for task_type, value in valid_outputs.items():
        parsed, errors = validate_output(task_type, value)
        assert parsed is not None
        assert errors == ()

    old_summary: dict[str, object] = {"summary": "摘要", "bullet_points": ["旧字段"]}
    old_claim: dict[str, object] = {
        "claims": [
            {
                "claim": "旧结构",
                "source_statement": "来源陈述",
                "speaker": None,
                "claim_type": "fact",
                "assertion_status": "reported",
                "evidence_offsets": [{"start": 0, "end": 4}],
            }
        ]
    }
    old_entities: dict[str, object] = {
        "people": [{"name": "某人"}],
        "organizations": [],
        "locations": [],
        "events": [],
        "objects": [],
        "concepts": [],
    }
    old_outputs: tuple[tuple[ModelTaskType, dict[str, object]], ...] = (
        (ModelTaskType.SUMMARY, old_summary),
        (ModelTaskType.CLAIM_EXTRACTION, old_claim),
        (ModelTaskType.ENTITY_EXTRACTION, old_entities),
    )
    for task_type, value in old_outputs:
        parsed, errors = validate_output(task_type, value)
        assert parsed is None
        assert errors

    extra_values = {
        ModelTaskType.SUMMARY: {**valid_outputs[ModelTaskType.SUMMARY], "extra": True},
        ModelTaskType.CLAIM_EXTRACTION: {
            **valid_outputs[ModelTaskType.CLAIM_EXTRACTION],
            "extra": True,
        },
        ModelTaskType.ENTITY_EXTRACTION: {
            **valid_outputs[ModelTaskType.ENTITY_EXTRACTION],
            "extra": True,
        },
    }
    for task_type, value in extra_values.items():
        parsed, errors = validate_output(task_type, value)
        assert parsed is None
        assert errors
