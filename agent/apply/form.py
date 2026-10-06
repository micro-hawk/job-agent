from dataclasses import dataclass, field

KINDS = {
    "input_text": "text",
    "textarea": "textarea",
    "input_file": "file",
    "multi_value_single_select": "select",
    "multi_value_multi_select": "multiselect",
}
TEXT_ALTERNATIVES = {"resume_text", "cover_letter_text"}
DEMOGRAPHIC_CONSENT = "gdpr_demographic_data_consent_given"


@dataclass
class Field:
    name: str
    label: str
    kind: str
    required: bool
    options: list[str] = field(default_factory=list)


def _values(raw: dict) -> list[str]:
    return [value["label"] for value in raw.get("values", [])]


def parse_greenhouse_form(data: dict) -> list[Field]:
    fields = []
    for question in data.get("questions", []):
        for raw in question["fields"]:
            if raw["name"] in TEXT_ALTERNATIVES or raw["type"] not in KINDS:
                continue
            fields.append(Field(raw["name"], question["label"].strip(), KINDS[raw["type"]], question["required"], _values(raw)))
    for question in data.get("location_questions") or []:
        if question["label"] == "Location":
            fields.append(Field("candidate-location", "Location", "location", question["required"]))
    for section in data.get("compliance") or []:
        for question in section["questions"]:
            raw = question["fields"][0]
            if raw["name"] == "race":
                fields.append(Field("hispanic_ethnicity", "Are you Hispanic/Latino?", "select", False, ["Yes", "No", "Decline To Self Identify"]))
            fields.append(Field(raw["name"], question["label"], "select", question["required"], _values(raw)))
    demographic = data.get("demographic_questions") or {}
    for question in demographic.get("questions", []):
        if question["type"] in KINDS:
            options = [option["label"] for option in question["answer_options"]]
            fields.append(Field(str(question["id"]), question["label"], KINDS[question["type"]], question["required"], options))
    consent = any(item.get("demographic_data_consent_applies") for item in data.get("data_compliance") or [])
    if demographic.get("questions") and consent:
        fields.append(Field(DEMOGRAPHIC_CONSENT, "Demographic data consent", "checkbox", True))
    return fields
