from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime

from .models import Context, Draft, Fact, Principle, Record, Relation, Scope, Status


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def match(scope: Scope, context: Context) -> bool | None:
    unknown = False
    for key, allowed in scope.items():
        if key not in context:
            unknown = True
        elif canonical(context[key]) not in {canonical(value) for value in allowed}:
            return False
    return None if unknown else True


def applicability(record: Draft, context: Context) -> bool | None:
    scope = match(record.scope, context)
    exclusions = [match(clause, context) for clause in record.exceptions]
    if scope is False or True in exclusions:
        return False
    if scope is None or None in exclusions:
        return None
    return True


def intersection(left: Scope, right: Scope) -> Scope | None:
    combined = dict(left)
    for key, values in right.items():
        if key in combined:
            allowed = {canonical(value) for value in combined[key]}
            values = [value for value in values if canonical(value) in allowed]
            if not values:
                return None
        combined[key] = values
    return combined


def within(inner: Scope, outer: Scope) -> bool:
    return all(
        key in inner and {canonical(v) for v in inner[key]} <= {canonical(v) for v in allowed}
        for key, allowed in outer.items()
    )


def overlap(left: Draft, right: Draft) -> bool:
    shared = intersection(left.scope, right.scope)
    if shared is None or any(within(shared, clause) for clause in left.exceptions + right.exceptions):
        return False
    return not (
        left.valid_until and right.valid_from and left.valid_until <= right.valid_from
        or right.valid_until and left.valid_from and right.valid_until <= left.valid_from
    )


def dependencies(records):
    index = defaultdict(list)
    for record in records.values():
        if isinstance(record.payload, Relation):
            index[record.payload.source_id].append(record)
    return index


def available(records: dict[str, Record], now: datetime, context: Context | None = None,
              *, include_unknown: bool = False) -> set[str]:
    live = {
        key for key, record in records.items()
        if record.status == Status.ACTIVE
        and (record.valid_from is None or record.valid_from <= now)
        and (record.valid_until is None or now < record.valid_until)
        and (
            context is None or applicability(record, context) is True
            or include_unknown and applicability(record, context) is None
        )
    }
    by_source = dependencies(records)
    while True:
        removed = set()
        for key in live:
            record = records[key]
            if isinstance(record.payload, Relation):
                if {record.payload.source_id, record.payload.target_id} - live:
                    removed.add(key)
                continue
            links = by_source.get(key, [])
            support = [link for link in links if link.payload.relation == "supported_by"]
            if support and not any(
                link.id in live and link.payload.target_id in live
                and records[link.payload.target_id].payload.approved for link in support
            ):
                removed.add(key)
            if any(
                link.payload.relation == "depends_on"
                and (context is None or applicability(link, context) is not False)
                and (link.id not in live or link.payload.target_id not in live)
                for link in links
                if link.status != Status.RETIRED
            ):
                removed.add(key)
        if not removed:
            return live
        live -= removed


def normalize(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", text).casefold() if char.isalnum())


def meaning(record: Draft) -> str:
    payload = record.payload
    if isinstance(payload, Principle):
        return normalize(payload.directive)
    if isinstance(payload, Fact):
        return canonical([normalize(payload.subject), normalize(payload.predicate), payload.value])
    data = payload.model_dump(mode="json")
    for field in ("rationale_hypothesis", "source_version"):
        data.pop(field, None)
    return normalize(canonical(data))


def terms(text: str) -> set[str]:
    chunks = re.findall(r"[a-z0-9_]+|[^\W\x00-\x7f]+", unicodedata.normalize("NFKC", text).casefold())
    return {item for chunk in chunks for item in {chunk, *(chunk[i:i + 2] for i in range(len(chunk) - 1))}}


def ranked(records, query: str, limit: int) -> list[Record]:
    words = terms(query)
    if not words:
        return sorted(records, key=lambda r: (getattr(r, "changed_at", None), r.id), reverse=True)[:limit]
    scores = []
    for record in records:
        title = terms(record.title)
        body = terms(canonical(record.payload.model_dump(mode="json")))
        score = 3 * len(words & title) + len(words & body)
        if score:
            scores.append((score, getattr(record, "changed_at", None), record.id, record))
    return [item[3] for item in sorted(scores, key=lambda item: item[:3], reverse=True)[:limit]]
