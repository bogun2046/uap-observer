"""V1 prompt versions mapped to the existing task-specific model schemas."""

from __future__ import annotations

import json
import uuid

from uap_platform.model_governance import ModelTaskType, PromptVersion
from uap_platform.model_governance.contracts import json_sha256
from uap_platform.model_governance.schemas import schema_json_schema

_IDS = {
    # Classification v1.2.0 is a new immutable prompt row. The previous
    # v1.1.1 row remains in the database as historical evidence.
    ModelTaskType.CLASSIFICATION: uuid.UUID("00000000-0000-7000-8000-000000001501"),
    ModelTaskType.SUMMARY: uuid.UUID("00000000-0000-7000-8000-000000001502"),
    ModelTaskType.CLAIM_EXTRACTION: uuid.UUID("00000000-0000-7000-8000-000000001503"),
    ModelTaskType.ENTITY_EXTRACTION: uuid.UUID("00000000-0000-7000-8000-000000001504"),
}

_VERSIONS = {
    ModelTaskType.CLASSIFICATION: "v1.2.0",
    ModelTaskType.SUMMARY: "v1.2.0",
    ModelTaskType.CLAIM_EXTRACTION: "v1.2.0",
    ModelTaskType.ENTITY_EXTRACTION: "v1.2.0",
}

_SYSTEM = """You analyze source material for an internal UAP/UFO archive.
Return one JSON object matching the supplied schema. Treat every claim as a statement made by
the source or a named speaker. Never decide whether UFO claims are true. Evidence offsets are
zero-based character offsets into SOURCE TEXT, with end exclusive. Do not invent evidence."""


def _classification_contract() -> str:
    """Render the frozen classification schema into the versioned prompt."""

    schema = schema_json_schema(ModelTaskType.CLASSIFICATION)
    return """Return exactly one JSON object and no Markdown, prose, or code fences.
The object must match the current frozen classification schema below exactly:

""" + json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + """

Rules:
- Required top-level fields and nesting are exactly those shown in the schema.
- Use only the listed enum values and the listed field types/ranges.
- `relevance.decision` is the relevance result; use `uncertain` when the source is insufficient.
- `relevance.reason` is the required explanation for the decision.
- `relevance.confidence` is a number from 0 to 1, or null when confidence is unavailable.
- `labels` is an array of strings; provide at least one concise label.
- `suggested_document_category` must be one of the schema's allowed category values.
- Do not add any field that is not present in the schema.
"""


def _schema_contract(task_type: ModelTaskType, task_rules: str) -> str:
    """Render one frozen task schema plus task-specific output rules."""

    schema = schema_json_schema(task_type)
    return (
        "Return exactly one JSON object and no Markdown, prose, or code fences.\n"
        "The object must match the current frozen "
        + task_type.value
        + " schema below exactly:\n\n"
        + json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n\nRules:\n"
        + "- The required fields, nesting, types, enums, length limits, and null/empty\n"
        + "  rules are exactly those shown above.\n"
        + "- Every object must obey the schema's additionalProperties rule.\n"
        + "  Do not add schema fields.\n"
        + "- If the source does not support an optional value, omit it or use null\n"
        + "  only where the schema allows null.\n"
        + "- If no item can be extracted, use the schema's valid empty array form\n"
        + "  when an empty array is allowed.\n"
        + "- Never invent a claim, entity, speaker, or evidence locator.\n"
        + task_rules
        + "\n"
    )


_USERS = {
    ModelTaskType.CLASSIFICATION: _classification_contract()
    + """\nDecide whether SOURCE TEXT is materially relevant to UAP, UFO, unidentified anomalous
phenomena, official records, sightings, investigations, or policy. Confidence concerns relevance
only. Do not decide whether any UFO claim is true.

SOURCE TEXT:
{{input}}""",
    ModelTaskType.SUMMARY: _schema_contract(
        ModelTaskType.SUMMARY,
        """- summary and bullets are required and must be non-empty.
- Write both fields in neutral Chinese; bullets is an array of 1-20 strings.
- If the source cannot support a substantive summary, state that limitation faithfully in a concise
  Chinese summary and one bullet rather than inventing content.
- Do not use the old field name bullet_points; it is not part of the frozen schema.
""",
    )
    + """

SOURCE TEXT:
{{input}}""",
    ModelTaskType.CLAIM_EXTRACTION: _schema_contract(
        ModelTaskType.CLAIM_EXTRACTION,
        """- The top-level field is claims, an array that may be empty.
- Each claim item requires claim, source_statement, claim_type, assertion_status, and evidence.
- speaker is optional and may be null when not identifiable.
- claim_type must be one of observation, attribution, event, assessment, other.
- assertion_status must be one of reported, disputed, unverified; it describes source posture,
  not whether the claim is true.
- evidence is a non-empty array of locator objects. For text evidence use zero-based character
  offsets with end exclusive; each locator still must follow the full frozen EvidenceLocator shape.
- Do not use the old evidence_offsets field or invent a fact claim type.
""",
    )
    + """

SOURCE TEXT:
{{input}}""",
    ModelTaskType.ENTITY_EXTRACTION: _schema_contract(
        ModelTaskType.ENTITY_EXTRACTION,
        """- The top-level field is entities, an array that may be empty.
- Each entity item requires name, entity_type, and a non-empty evidence array; aliases is
  optional and defaults to an empty array.
- entity_type must be one of person, organization, location, event, object, concept.
- For text evidence use zero-based character offsets with end exclusive; each locator must follow
  the full frozen EvidenceLocator shape.
- Candidates are proposals for human confirmation, not automatic event merges.
- Do not use category-specific top-level fields such as people, organizations, locations, events,
  objects, or concepts; only entities is permitted.
""",
    )
    + """

SOURCE TEXT:
{{input}}""",
}


def v1_prompts() -> tuple[PromptVersion, ...]:
    prompts: list[PromptVersion] = []
    for task_type, prompt_id in _IDS.items():
        output_schema = schema_json_schema(task_type)
        version = _VERSIONS[task_type]
        values = {
            "task_type": task_type.value,
            "version": version,
            "system_template": _SYSTEM,
            "user_template": _USERS[task_type],
            "output_schema": output_schema,
        }
        prompts.append(
            PromptVersion(
                id=prompt_id,
                task_type=task_type,
                version=version,
                system_template=_SYSTEM,
                user_template=_USERS[task_type],
                output_schema=output_schema,
                content_sha256=json_sha256(values),
                active=True,
            )
        )
    return tuple(prompts)
