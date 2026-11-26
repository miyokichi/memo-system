from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    Add, Draft, Evidence, Experience, Fact, Invalidate, Principle, Procedure,
    Proposal, Record, Relation, Replace, Restore, Review, Revise, ScopedRevision, Status, VersionRef, Withdrawal,
)
from .rules import canonical, dependencies, intersection, meaning, normalize, overlap, within

LEARNED = {"concept", "fact", "principle", "procedure"}


class KnowledgeError(ValueError):
    pass


def utcnow():
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, path: str):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        with self.transaction(write=True) as db:
            for statement in ("""
                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY (id, version)
                );
                CREATE TABLE IF NOT EXISTS proposals (
                    id INTEGER PRIMARY KEY, epoch INTEGER NOT NULL, data TEXT NOT NULL,
                    review TEXT
                );
                CREATE TABLE IF NOT EXISTS withdrawals (
                    seq INTEGER PRIMARY KEY, data TEXT NOT NULL
                );
            """).strip().rstrip(";").split(";"):
                db.execute(statement)

    @contextmanager
    def transaction(self, write=False):
        db = sqlite3.connect(self.path, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _world(db):
        rows = db.execute("""
            SELECT r.data FROM revisions r
            JOIN (SELECT id, MAX(version) AS version FROM revisions GROUP BY id) head
                USING (id, version)
        """)
        records = (Record.model_validate_json(raw) for (raw,) in rows)
        return {record.id: record for record in records}

    @staticmethod
    def _epoch(db):
        return db.execute("SELECT COALESCE(MAX(rowid), 0) FROM revisions").fetchone()[0]

    @staticmethod
    def _withdrawals(db):
        return [Withdrawal.model_validate_json(raw) for (raw,) in db.execute(
            "SELECT data FROM withdrawals ORDER BY seq"
        )]

    def snapshot(self):
        with self.transaction() as db:
            return self._epoch(db), self._world(db), self._withdrawals(db)

    @staticmethod
    def _history(db, record_id):
        return [Record.model_validate_json(raw) for (raw,) in db.execute(
            "SELECT data FROM revisions WHERE id=? ORDER BY version DESC", (record_id,)
        )]

    def history(self, record_id):
        with self.transaction() as db:
            return self._history(db, record_id)

    def audit(self, record_id):
        with self.transaction() as db:
            return {
                "versions": [item.model_dump(mode="json") for item in self._history(db, record_id)],
                "withdrawals": [item.model_dump(mode="json") for item in self._withdrawals(db)
                                if item.record.id == record_id],
                "relations": [item.model_dump(mode="json") for item in self._world(db).values()
                              if isinstance(item.payload, Relation)
                              and record_id in {item.payload.source_id, item.payload.target_id}],
            }

    def submit(self, proposal: Proposal, expected_epoch: int):
        with self.transaction(write=True) as db:
            self._assert_epoch(db, expected_epoch)
            # Check a candidate transaction before assigning a review ID.
            self._compile(db, proposal)
            cursor = db.execute("INSERT INTO proposals(epoch,data) VALUES (?,?)",
                                (expected_epoch, proposal.model_dump_json()))
            return cursor.lastrowid

    def proposals(self, proposal_id=None):
        with self.transaction() as db:
            if proposal_id is None:
                rows = db.execute("SELECT * FROM proposals WHERE review IS NULL OR json_extract(review,'$.decision') = 'hold' ORDER BY id DESC LIMIT 100")
            else:
                rows = db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,))
            return [
                {"proposal_id": key, "expected_epoch": epoch, "proposal": Proposal.model_validate_json(data),
                 "review": Review.model_validate_json(review) if review else None}
                for key, epoch, data, review in rows
            ]

    @staticmethod
    def _assert_epoch(db, epoch):
        if Store._epoch(db) != epoch:
            raise KnowledgeError("Knowledge changed since this proposal was prepared. Revalidate and resubmit.")

    def review(self, proposal_id: int, review: Review):
        with self.transaction(write=True) as db:
            row = db.execute("SELECT epoch,data,review FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise KnowledgeError(f"Unknown proposal {proposal_id}")
            epoch, raw, recorded = row
            if recorded:
                if Review.model_validate_json(recorded) == review:
                    return {"decision": review.decision, "already_recorded": True}
                if Review.model_validate_json(recorded).decision != "hold":
                    raise KnowledgeError("This proposal already has a final decision")
            if review.decision != "approve":
                db.execute("UPDATE proposals SET review=? WHERE id=?", (review.model_dump_json(), proposal_id))
                return {"decision": review.decision}

            self._assert_epoch(db, epoch)
            proposal = Proposal.model_validate_json(raw)
            if proposal.questions:
                raise KnowledgeError("Resolve proposal questions and resubmit before approval")
            world, changes, withdrawals = self._compile(db, proposal)
            adopted = [world[key].payload for key in changes if world[key].status == Status.ACTIVE]
            checks = {"evidence", "scope", "consistency"} if any(
                payload.kind in LEARNED for payload in adopted
            ) else set()
            if any(isinstance(payload, Principle) for payload in adopted):
                checks |= {"transfer", "regression"}
            if not checks <= set(review.verified_checks):
                raise KnowledgeError(f"Missing reviewer checks: {', '.join(sorted(checks - set(review.verified_checks)))}")
            db.executemany("INSERT INTO revisions VALUES (?,?,?)", [
                (key, world[key].version, world[key].model_dump_json()) for key in changes
            ])
            db.executemany("INSERT INTO withdrawals(data) VALUES (?)", [
                (withdrawal.model_dump_json(),) for withdrawal in withdrawals
            ])
            db.execute("UPDATE proposals SET review=? WHERE id=?", (review.model_dump_json(), proposal_id))
            return {
                "decision": review.decision, "epoch": self._epoch(db),
                "changes": [{"id": key, "version": world[key].version, "status": world[key].status} for key in changes],
            }

    def _compile(self, db, proposal):
        original = self._world(db)
        world = original.copy()
        changed = set()
        breaking = set()
        restored = set()
        explicit = set()
        withdrawn_drafts = []
        now = utcnow()

        def head(key, version=None):
            if key not in original or (version is not None and original[key].version != version):
                raise KnowledgeError(f"Missing record or unexpected version: {key}")
            return original[key]

        def draft(record):
            return Draft.model_validate(record.model_dump(include=set(Draft.model_fields)))

        def write(item, status=Status.ACTIVE, reason=proposal.reason):
            version = original[item.id].version + 1 if item.id in original else 1
            world[item.id] = Record(
                **item.model_dump(include=set(Draft.model_fields)),
                status=status, version=version, reason=reason, changed_at=now,
            )
            changed.add(item.id)

        def mark(key):
            if key in explicit:
                raise KnowledgeError(f"Multiple operations for {key}")
            explicit.add(key)

        def add(item):
            mark(item.id)
            if item.id in original:
                raise KnowledgeError(f"Record {item.id} already exists")
            write(item)

        def retire(item):
            if item.status == Status.RETIRED:
                raise KnowledgeError(f"Already retired: {item.id}")
            write(item, Status.RETIRED)
            breaking.add(item.id)
            withdrawn_drafts.append(draft(item))

        pending_links = []
        merged = {}
        detached_sources = set()
        for op in proposal.operations:
            if isinstance(op, Add):
                add(op.record)
            elif isinstance(op, Revise):
                previous = head(op.record.id, op.expected_version)
                mark(previous.id)
                if previous.status == Status.RETIRED:
                    raise KnowledgeError("Use restore to revive a retired record")
                if previous.payload.kind != op.record.payload.kind:
                    raise KnowledgeError("Revisions must preserve the record type")
                if isinstance(previous.payload, Relation) and previous.payload != op.record.payload:
                    raise KnowledgeError("Relation revisions must preserve endpoints and relation type")
                write(op.record)
                breaking.add(previous.id)
                if isinstance(previous.payload, Relation) and previous.payload.relation in {"depends_on", "supported_by"}:
                    breaking.add(previous.payload.source_id)
                if meaning(previous) != meaning(op.record):
                    withdrawn_drafts.append(draft(previous))
            elif isinstance(op, ScopedRevision):
                previous = head(op.id, op.expected_version)
                mark(previous.id)
                replacement = op.replacement
                shared = intersection(previous.scope, replacement.scope)
                if (previous.status != Status.ACTIVE or previous.payload.kind != replacement.payload.kind
                        or not replacement.scope or shared is None
                        or not overlap(previous, replacement)
                        or any(clause not in previous.exceptions for clause in replacement.exceptions)
                        or previous.valid_from != replacement.valid_from
                        or previous.valid_until != replacement.valid_until):
                    raise KnowledgeError("Scoped revision must narrow an active record of the same type and validity")
                # Keep the previous revision in force outside the replacement scope.
                write(draft(previous).model_copy(update={"exceptions": previous.exceptions + [shared]}))
                add(replacement.model_copy(update={"scope": shared, "exceptions": previous.exceptions}))
                withdrawn_drafts.append(draft(previous).model_copy(update={"scope": shared}))
                breaking.add(previous.id)
                pending_links.append((replacement.id, previous.id))
            elif isinstance(op, Replace):
                removed = [head(key) for key in op.retire]
                kinds = {item.payload.kind for item in [*removed, *op.records]}
                if len(kinds) != 1 or "relation" in kinds:
                    raise KnowledgeError("Replacement records must have the same non-relation type")
                if op.op == "split" and (kinds != {"concept"} or len(removed) != 1 or len(op.records) < 2):
                    raise KnowledgeError("Split requires one old concept and at least two new concepts")
                if op.op == "merge" and (len(removed) < 2 or len(op.records) != 1):
                    raise KnowledgeError("Merge requires at least two records and one replacement")
                for item in removed:
                    mark(item.id)
                    retire(item)
                for item in op.records:
                    add(item)
                    pending_links.extend((item.id, old.id) for old in removed)
                    if op.op == "merge":
                        merged[item.id] = set(op.retire)
            elif isinstance(op, Invalidate):
                previous = head(op.id, op.expected_version)
                mark(op.id)
                retire(previous)
                if isinstance(previous.payload, Relation) and previous.payload.relation in {"depends_on", "supported_by"}:
                    detached_sources.add(previous.payload.source_id)
                if op.replacement_id:
                    pending_links.append((op.replacement_id, op.id))
            elif isinstance(op, Restore):
                previous = head(op.id, op.expected_version)
                mark(op.id)
                if op.from_version > previous.version:
                    raise KnowledgeError("Unknown historical version")
                if (previous.status == Status.ACTIVE
                        and (previous.valid_until is None or previous.valid_until > now)):
                    raise KnowledgeError("Restore requires a retired, expired, or review-blocked record")
                row = db.execute("SELECT data FROM revisions WHERE id=? AND version=?",
                                 (op.id, op.from_version)).fetchone()
                old = Record.model_validate_json(row[0])
                item = draft(old)
                if op.validity is not None:
                    item = Draft.model_validate(item.model_dump() | op.validity.model_dump())
                if item.valid_until and item.valid_until <= now:
                    raise KnowledgeError("Cannot restore an expired version without a new validity interval.")
                write(item)
                restored.add(op.id)
                breaking.add(op.id)

        for source, target in pending_links:
            if source not in world or source == target or world[source].status != Status.ACTIVE:
                raise KnowledgeError("Replacement must reference a distinct active record")
            pair = hashlib.sha256(canonical([source, target]).encode()).hexdigest()[:24]
            add(Draft(id=f"supersedes-{pair}", title=f"{source} supersedes {target}",
                      payload=Relation(kind="relation", source_id=source, target_id=target, relation="supersedes")))

        for key in detached_sources - explicit:
            if world[key].status == Status.ACTIVE:
                write(draft(world[key]), Status.NEEDS_REVIEW, "Dependency link retired")
                breaking.add(key)

        # An explicit link revision acknowledges the new target version. Other links
        # to changed targets stop contributing until they have been reviewed again.
        links = dependencies(world)
        propagated = set()
        while breaking - propagated:
            batch = breaking - propagated
            propagated |= batch
            affected = set()
            for source, relations in links.items():
                for link in relations:
                    edge = link.payload
                    if edge.relation in {"depends_on", "supported_by"} and edge.target_id in batch:
                        affected.add(source)
                        if link.id not in explicit and world[link.id].status == Status.ACTIVE:
                            write(draft(link), Status.NEEDS_REVIEW, f"Target changed: {edge.target_id}")
            for key in affected:
                current = world[key]
                if current.status != Status.ACTIVE or key in explicit:
                    continue
                live_links = [world[link.id] for link in links[key]]
                blocked = any(link.payload.relation == "depends_on" and link.status == Status.NEEDS_REVIEW
                              for link in live_links)
                support = [link for link in live_links if link.payload.relation == "supported_by"]
                def still_supports(link):
                    target = world[link.payload.target_id]
                    return (
                        link.status == Status.ACTIVE and target.status == Status.ACTIVE and target.payload.approved
                        and (link.valid_from is None or link.valid_from <= now)
                        and (target.valid_from is None or target.valid_from <= now)
                        and (link.valid_until is None or link.valid_until > now)
                        and (target.valid_until is None or target.valid_until > now)
                    )

                if blocked or support and not any(still_supports(link) for link in support):
                    write(draft(current), Status.NEEDS_REVIEW, "Required knowledge or supporting evidence changed")
                    breaking.add(key)

        original_links = dependencies(original)

        def withdrawal(item, reason):
            experiences, evidence = Store._lineage(item.id, original, original_links)
            return Withdrawal(
                record=item, reason=reason, experiences=sorted(experiences),
                evidence=[VersionRef(id=key, version=version) for key, version in sorted(evidence)],
            )

        withdrawals = [withdrawal(item, proposal.reason) for item in withdrawn_drafts]
        historical = self._withdrawals(db)
        for item in original.values():
            if item.valid_until and item.valid_until <= now:
                historical.append(withdrawal(draft(item), "expired"))
        self._validate(world, original, explicit, restored, now)
        self._validate_withdrawals(world, explicit, restored, historical, {})
        self._validate_withdrawals(world, explicit, restored, withdrawals, merged)
        return world, sorted(changed), withdrawals

    @staticmethod
    def _validate(world, original, explicit, restored, now):
        links = dependencies(world)
        active = {key: item for key, item in world.items() if item.status == Status.ACTIVE}
        graph = {}
        exceptions = set()
        for key, record in active.items():
            if isinstance(record.payload, Relation):
                edge = record.payload
                source, target = world.get(edge.source_id), world.get(edge.target_id)
                if not source or not target or source.id == target.id:
                    raise KnowledgeError(f"Invalid endpoints for {key}")
                if isinstance(source.payload, Relation) or isinstance(target.payload, Relation):
                    raise KnowledgeError("Relation endpoints must be knowledge records")
                expected = {
                    "supported_by": (None, "evidence"),
                    "derived_from": (None, "experience"),
                    "is_a": ("concept", "concept"),
                    "exception_to": ("principle", "principle"),
                    "instantiates": ("procedure", "principle"),
                }
                source_kind, target_kind = expected.get(edge.relation, (None, None))
                if ((source_kind and source.payload.kind != source_kind)
                        or (target_kind and target.payload.kind != target_kind)):
                    raise KnowledgeError(f"Incorrect endpoint type for {edge.relation}")
                if (key in explicit and edge.relation in {"depends_on", "supported_by"}
                        and (target.id not in active or source.id not in active)):
                    raise KnowledgeError(f"Dependency links require active endpoints: {key}")
                if edge.relation in {"depends_on", "supported_by", "is_a", "exception_to"}:
                    graph.setdefault(source.id, set()).add(target.id)
                if edge.relation == "exception_to":
                    if (record.scope or record.exceptions or record.valid_from or record.valid_until
                            or not within(source.scope, target.scope)
                            or within(target.scope, source.scope)):
                        raise KnowledgeError("An exception must have a strictly narrower positive scope than its parent")
                    exceptions.add(frozenset({source.id, target.id}))
                if edge.relation == "contradicts" and source.id in active and target.id in active:
                    if {key, source.id, target.id} & explicit and overlap(source, target):
                        raise KnowledgeError(f"Unresolved explicit contradiction: {source.id}, {target.id}")

        # Kahn's algorithm avoids recursion limits for long dependency chains.
        indegree = {key: 0 for key in world}
        for children in graph.values():
            for child in children:
                indegree[child] += 1
        ready = [key for key, count in indegree.items() if count == 0]
        while ready:
            for child in graph.get(ready.pop(), ()):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if any(indegree.values()):
            raise KnowledgeError("Circular dependency, evidence, or concept hierarchy")

        for key in explicit & active.keys():
            item = active[key]
            if item.payload.kind not in LEARNED:
                continue
            previously = original.get(key)
            support = []
            for link in links.get(key, []):
                if link.payload.relation != "supported_by" or link.id not in active:
                    continue
                target = active.get(link.payload.target_id)
                if (target and isinstance(target.payload, Evidence) and target.payload.approved
                        and overlap(item, target) and overlap(item, link)
                        and (target.valid_until is None or target.valid_until > now)
                        and (link.valid_until is None or link.valid_until > now)):
                    support.append(target)
            if not support:
                raise KnowledgeError(f"Approved evidence required for {key}")
            if key in restored and not any(e.changed_at > previously.changed_at for e in support):
                raise KnowledgeError(f"Restoring {key} requires fresh approved evidence")
            for link in links.get(key, []):
                if link.payload.relation == "depends_on" and link.status != Status.RETIRED:
                    if link.id not in active or link.payload.target_id not in active:
                        raise KnowledgeError(f"Required dependency needs review for {key}")

            for other in active.values():
                if item.id == other.id or not overlap(item, other):
                    continue
                left, right = item.payload, other.payload
                if isinstance(left, Fact) and isinstance(right, Fact):
                    if (normalize(left.subject) == normalize(right.subject)
                            and normalize(left.predicate) == normalize(right.predicate)
                            and canonical(left.value) != canonical(right.value)):
                        raise KnowledgeError(f"Conflicting facts: {item.id}, {other.id}")
                if isinstance(left, Principle) and isinstance(right, Principle):
                    conflict = set(left.required_actions) & set(right.forbidden_actions)
                    conflict |= set(right.required_actions) & set(left.forbidden_actions)
                    if conflict and frozenset({item.id, other.id}) not in exceptions:
                        raise KnowledgeError(f"Conflicting action constraints: {item.id}, {other.id}")

    @staticmethod
    def _lineage(key, records, links):
        experiences, evidence = set(), set()
        for link in links.get(key, []):
            if link.status == Status.RETIRED:
                continue
            target = records.get(link.payload.target_id)
            if not target:
                continue
            if link.payload.relation == "derived_from":
                experiences.add(target.id)
            elif link.payload.relation == "supported_by":
                evidence.add((target.id, target.version))
        return experiences, evidence

    @staticmethod
    def _validate_withdrawals(world, explicit, restored, withdrawals, merged):
        links = dependencies(world)
        for key in explicit - restored:
            item = world[key]
            if item.status != Status.ACTIVE or item.payload.kind not in LEARNED:
                continue
            experiences, evidence = Store._lineage(key, world, links)
            for withdrawal in withdrawals:
                old = withdrawal.record
                withdrawn_scope = old.model_copy(update={"valid_from": None, "valid_until": None})
                if (old.payload.kind != item.payload.kind or old.id in merged.get(key, set())
                        or not overlap(item, withdrawn_scope)):
                    continue
                reused_lineage = (
                    isinstance(item.payload, Principle) and isinstance(old.payload, Principle)
                    and normalize(item.payload.goal) == normalize(old.payload.goal)
                    and experiences and experiences <= set(withdrawal.experiences)
                    and evidence <= {(ref.id, ref.version) for ref in withdrawal.evidence}
                )
                if meaning(old) == meaning(item) or reused_lineage:
                    raise KnowledgeError(f"{key} repeats withdrawn knowledge ({old.id}): {withdrawal.reason}")
