from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12_000)]
Key = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")]
Value = str | int | bool
Scope = dict[Text, Annotated[list[Value], Field(min_length=1)]]
Context = dict[Text, Value]
Timestamp = Annotated[AwareDatetime, Field(strict=False)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Experience(Model):
    kind: Literal["experience"]
    situation: Text
    goal: Text
    decision: Text
    action: Text
    outcome: Text
    context: Context = Field(default_factory=dict)
    successful: bool | None = None


class Concept(Model):
    kind: Literal["concept"]
    definition: Text
    attributes: list[Text]
    identifying_conditions: Scope = Field(default_factory=dict)


class Fact(Model):
    kind: Literal["fact"]
    subject: Text
    predicate: Text
    value: Value


class Principle(Model):
    kind: Literal["principle"]
    goal: Text
    directive: Text
    rationale_hypothesis: Text | None = None
    required_actions: list[Key] = Field(default_factory=list)
    forbidden_actions: list[Key] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_actions(self):
        if set(self.required_actions) & set(self.forbidden_actions):
            raise ValueError("An action cannot be both required and forbidden")
        return self


class Step(Model):
    action: Key
    target: Text
    instruction: Text
    requires: Scope = Field(default_factory=dict)
    knowledge_ids: list[Key] = Field(default_factory=list)


class Procedure(Model):
    kind: Literal["procedure"]
    goal: Text
    steps: Annotated[list[Step], Field(min_length=1)]


class Evidence(Model):
    kind: Literal["evidence"]
    source: Text
    source_version: Text
    authority: Text
    assertion: Text
    approved: bool


class Relation(Model):
    kind: Literal["relation"]
    source_id: Key
    target_id: Key
    relation: Literal[
        "derived_from", "supported_by", "depends_on", "is_a", "exception_to",
        "contradicts", "supersedes", "instantiates",
    ]


Payload = Annotated[
    Experience | Concept | Fact | Principle | Procedure | Evidence | Relation,
    Field(discriminator="kind"),
]


class Draft(Model):
    id: Key
    title: Text
    payload: Payload
    scope: Scope = Field(default_factory=dict)
    exceptions: list[Annotated[Scope, Field(min_length=1)]] = Field(default_factory=list)
    valid_from: Timestamp | None = None
    valid_until: Timestamp | None = None

    @model_validator(mode="after")
    def check_validity(self):
        if self.valid_from and self.valid_until and self.valid_from >= self.valid_until:
            raise ValueError("valid_until must be later than valid_from")
        return self


class Status(StrEnum):
    ACTIVE = "active"
    NEEDS_REVIEW = "needs_review"
    RETIRED = "retired"


class Record(Draft):
    version: int
    status: Status
    reason: Text
    changed_at: AwareDatetime


class Add(Model):
    op: Literal["add"]
    record: Draft


class Revise(Model):
    op: Literal["revise"]
    record: Draft
    expected_version: Annotated[int, Field(gt=0)]


class ScopedRevision(Model):
    op: Literal["revise_scope"]
    id: Key
    expected_version: Annotated[int, Field(gt=0)]
    replacement: Draft


class Replace(Model):
    op: Literal["merge", "split"]
    retire: Annotated[list[Key], Field(min_length=1)]
    records: Annotated[list[Draft], Field(min_length=1)]


class Invalidate(Model):
    op: Literal["invalidate"]
    id: Key
    expected_version: Annotated[int, Field(gt=0)]
    replacement_id: Key | None = None


class Validity(Model):
    valid_from: Timestamp | None = None
    valid_until: Timestamp | None = None


class Restore(Model):
    op: Literal["restore"]
    id: Key
    expected_version: Annotated[int, Field(gt=0)]
    from_version: Annotated[int, Field(gt=0)]
    validity: Validity | None = None


Operation = Annotated[
    Add | Revise | ScopedRevision | Replace | Invalidate | Restore,
    Field(discriminator="op"),
]


class Proposal(Model):
    reason: Text
    operations: Annotated[list[Operation], Field(min_length=1, max_length=50)]
    questions: list[Text] = Field(default_factory=list)


class Suggestion(Model):
    summary: Text
    questions: list[Text] = Field(default_factory=list)
    proposal: Proposal | None = None


class Review(Model):
    decision: Literal["approve", "hold", "reject"]
    findings: Text
    verified_checks: list[Literal["evidence", "scope", "consistency", "regression", "transfer", "causality"]] = Field(
        default_factory=list
    )


class ActionPlan(Model):
    goal: Text
    rationale: Text
    steps: list[Step]
    alternatives: list[Text] = Field(default_factory=list)
    questions: list[Text] = Field(default_factory=list)


class EvaluationCase(Model):
    id: Key
    group: Literal["replay", "transfer", "updated", "regression", "unknown"]
    context: Context = Field(default_factory=dict)
    required_actions: list[Key] = Field(default_factory=list)
    forbidden_actions: list[Key] = Field(default_factory=list)
    required_knowledge: list[Key] = Field(default_factory=list)
    ask_expected: bool = False
    learning_experience_ids: list[Key] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_split(self):
        if self.group == "transfer" and self.learning_experience_ids:
            raise ValueError("Transfer cases must be held out from learning")
        if set(self.required_actions) & set(self.forbidden_actions):
            raise ValueError("Evaluation actions cannot be both required and forbidden")
        if self.group == "unknown" and not self.ask_expected:
            raise ValueError("Unknown cases must expect a question")
        return self


class VersionRef(Model):
    id: Key
    version: Annotated[int, Field(gt=0)]


class EvaluationRun(Model):
    case: EvaluationCase
    actions: list[Key] = Field(default_factory=list)
    used_knowledge: list[VersionRef] = Field(default_factory=list)
    asked: bool


class Withdrawal(Model):
    record: Draft
    reason: Text
    experiences: list[Key] = Field(default_factory=list)
    evidence: list[VersionRef] = Field(default_factory=list)
