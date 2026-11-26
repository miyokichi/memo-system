import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from memo_system.models import ActionPlan, EvaluationRun, Proposal, Review, Suggestion
from memo_system.service import Memory, OpenAICompatible
from memo_system.store import KnowledgeError, Store

CHECKS = ["evidence", "scope", "consistency", "regression", "transfer"]


def record(key, kind, **fields):
    return {"id": key, "title": key, "payload": {"kind": kind, **fields}}


def link(key, source, target, relation="supported_by"):
    return record(key, "relation", source_id=source, target_id=target, relation=relation)


def evidence(key):
    return record(key, "evidence", source="仕様管理台帳", source_version="1", authority="品質保証",
                  assertion="正式仕様は承認済みの情報源を参照する", approved=True)


def principle(key="spec"):
    return record(key, "principle", goal="正式仕様", directive="承認済み資料を参照する",
                  required_actions=["verify_version"], forbidden_actions=["use_unapproved"])


def add(draft):
    return {"op": "add", "record": draft}


def plan(key="spec", action="verify_version"):
    return ActionPlan.model_validate({
        "goal": "正式仕様", "rationale": "仕様の有効な版を特定する",
        "steps": [{"action": action, "target": "製品X", "instruction": "資料の版を確認する",
                   "knowledge_ids": [key]}],
    })


def proposal(*operations):
    return Proposal.model_validate_json(json.dumps({"reason": "根拠に基づく変更", "operations": operations}))


def approval():
    return Review.model_validate({"decision": "approve", "findings": "根拠と検証ケースを確認", "verified_checks": CHECKS})


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(str(Path(self.temp.name) / "memo.db"))
        self.memory = Memory(self.store)

    def apply(self, *operations):
        return self.store.review(self.store.submit(proposal(*operations), self.store.snapshot()[0]), approval())

    def search(self, kind, context=None):
        result = self.memory.search("", context or {}, kind, 30, False)
        return {item["record"]["id"] for item in result["results"]}

    def test_independent_support_and_transitive_invalidation(self):
        self.apply(*map(add, [
            evidence("e1"), evidence("e2"), principle(),
            link("s1", "spec", "e1"), link("s2", "spec", "e2"),
            record("guide", "procedure", goal="正式仕様", steps=[{
                "action": "verify_version", "target": "資料", "instruction": "版を確認する"
            }]),
            link("s3", "guide", "e2"), link("d1", "guide", "spec", "depends_on"),
        ]))
        self.apply({"op": "invalidate", "id": "e1", "expected_version": 1})
        self.assertEqual(self.search("principle"), {"spec"})
        self.apply({"op": "invalidate", "id": "e2", "expected_version": 1})
        self.assertFalse(self.search("principle"))
        self.assertFalse(self.search("procedure"))
        self.assertEqual(self.store.history("guide")[0].status, "needs_review")

    def test_scoped_update_and_no_relearning(self):
        self.apply(*map(add, [
            evidence("e1"), record("system-a", "fact", subject="製品仕様", predicate="管理システム", value="A"),
            link("s1", "system-a", "e1"),
        ]))
        self.apply(
            {"op": "revise_scope", "id": "system-a", "expected_version": 1, "replacement":
             record("system-b", "fact", subject="製品仕様", predicate="管理システム", value="B")
             | {"scope": {"generation": ["new"]}}},
            add(evidence("e2")), add(link("s2", "system-b", "e2")),
        )
        self.assertEqual(self.search("fact", {"generation": "old"}), {"system-a"})
        self.assertEqual(self.search("fact", {"generation": "new"}), {"system-b"})
        self.assertFalse(self.search("fact"))
        old_fact = record("regenerated", "fact", subject="製品仕様", predicate="管理システム", value="A")
        with self.assertRaisesRegex(KnowledgeError, "Conflicting facts|withdrawn"):
            self.apply(add(old_fact), add(link("back", "regenerated", "e1")))

    def test_withdrawal_and_restore_require_fresh_evidence(self):
        self.apply(*map(add, [evidence("e1"), principle(), link("s1", "spec", "e1")]))
        self.apply({"op": "invalidate", "id": "spec", "expected_version": 1})
        with self.assertRaisesRegex(KnowledgeError, "repeats withdrawn"):
            self.apply(add(principle("regenerated")), add(link("s2", "regenerated", "e1")))
        restore = {"op": "restore", "id": "spec", "expected_version": 2, "from_version": 1}
        with self.assertRaisesRegex(KnowledgeError, "fresh approved evidence"):
            self.apply(restore)
        self.apply(restore, add(evidence("e2")), add(link("s3", "spec", "e2")))
        self.assertEqual(self.search("principle"), {"spec"})
        self.assertEqual(len(self.store.history("spec")), 3)

    def test_dynamic_expiry_blocks_search_plan_and_eval(self):
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=1)
        self.apply(*map(add, [
            evidence("e1") | {"valid_until": expires.isoformat()},
            principle(), link("s1", "spec", "e1"),
        ]))
        epoch = self.store.snapshot()[0]
        self.assertEqual(self.memory.verify_plan(plan(), "正式仕様", {}, [], epoch)["status"], "constraints_passed")
        self.assertEqual(self.memory.verify_plan(plan(action="use_unapproved"), "正式仕様", {}, [], epoch)["status"],
                         "needs_review")
        run = EvaluationRun.model_validate({
            "case": {"id": "unseen-product", "group": "transfer", "required_actions": ["verify_version"]},
            "actions": ["verify_version"], "used_knowledge": [{"id": "spec", "version": 1}], "asked": False,
        })
        self.assertEqual(self.memory.evaluate([run])["transfer_accuracy"]["value"], 1)
        with patch("memo_system.service.utcnow", return_value=expires):
            self.assertFalse(self.search("principle"))
            self.assertEqual(self.memory.verify_plan(plan(), "正式仕様", {}, [], epoch)["status"], "needs_review")
            self.assertEqual(self.memory.evaluate([run])["stale_usage_rate"]["value"], 1)

    def test_stale_or_unreviewed_proposals_cannot_write(self):
        epoch = self.store.snapshot()[0]
        pending = self.store.submit(proposal(add(evidence("pending"))), epoch)
        self.assertEqual(self.search("evidence"), set())
        self.apply(add(evidence("current")))
        with self.assertRaisesRegex(KnowledgeError, "Knowledge changed"):
            self.store.review(pending, approval())
        self.assertFalse(self.store.history("pending"))

    def test_differentiation_and_validation_use_typed_llm_drafts(self):
        concept = record("status", "concept", definition="承認状態", attributes=["approved"])
        self.apply(*map(add, [evidence("e1"), concept, link("src", "status", "e1")]))
        split = {"op": "split", "retire": ["status"], "records": [
            record("approved", "concept", definition="承認済み", attributes=["approved"]),
            record("draft", "concept", definition="審査中", attributes=["pending"]),
        ]}
        output = Suggestion(summary="承認状態を分ける", proposal=proposal(
            split, add(link("s1", "approved", "e1")), add(link("s2", "draft", "e1"))
        ))

        class FakeLLM:
            def complete(self, instructions, data, response_type):
                if response_type is ActionPlan:
                    return plan()
                self.data = data
                return output

        llm = FakeLLM()
        memory = Memory(self.store, llm)
        result = memory.suggest("differentiate", "承認状態", {}, ["status"])
        self.assertTrue(result["proposal"])
        self.assertEqual(self.search("concept"), {"status"})
        self.assertIsNone(memory.suggest("validate", "承認状態", {}, ["status"])["proposal"])
        self.store.review(self.store.submit(output.proposal, result["epoch"]), approval())
        self.assertEqual(self.search("concept"), {"draft", "approved"})
        memory.suggest("abstract", "status 承認状態", {}, [])
        self.assertTrue(llm.data["withdrawals"])

    def test_scoped_exception_is_applied_to_an_unseen_product(self):
        alternative = record("draft-rule", "principle", goal="正式仕様", directive="暫定仕様として版を保管",
                             required_actions=["use_unapproved"], forbidden_actions=["verify_version"])
        alternative["scope"] = {"phase": ["development"]}
        self.apply(*map(add, [
            evidence("e1"), principle(), alternative, link("s1", "spec", "e1"), link("s2", "draft-rule", "e1"),
            link("exception", "draft-rule", "spec", "exception_to"),
        ]))

        class FakeLLM:
            def complete(self, instructions, data, response_type):
                assert response_type is ActionPlan
                assert [item["id"] for item in data["principles"]] == ["draft-rule"]
                return plan("draft-rule", "use_unapproved")

        memory = Memory(self.store, FakeLLM())
        result = memory.concretize("正式仕様", {"phase": "development", "product": "unseen"}, [])
        self.assertEqual(result["validation"]["status"], "constraints_passed")
        self.assertEqual(result["validation"]["citations"], [{"id": "draft-rule", "version": 1}])
        blocked = memory.verify_plan(plan(), "正式仕様", {}, [], result["epoch"])
        self.assertEqual(blocked["status"], "needs_review")

    def test_paraphrase_from_withdrawn_lineage_is_rejected(self):
        observed = record("case", "experience", situation="仕様を確認", goal="正式仕様", decision="台帳を参照",
                          action="版を照合", outcome="一致")
        self.apply(*map(add, [
            observed, evidence("e1"), principle(), link("s1", "spec", "e1"),
            link("p1", "spec", "case", "derived_from"),
        ]))
        self.apply({"op": "invalidate", "id": "spec", "expected_version": 1})
        item = principle("paraphrase")
        item["payload"]["directive"] = "承認を得た文書を使う"
        with self.assertRaisesRegex(KnowledgeError, "repeats withdrawn"):
            self.apply(add(item), add(link("s2", "paraphrase", "e1")),
                       add(link("p2", "paraphrase", "case", "derived_from")))

    def test_openai_compatible_chat_completions(self):
        def post(url, **kwargs):
            self.assertEqual(url, "https://model.invalid/v1/chat/completions")
            self.assertEqual(kwargs["json"]["model"], "example")
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer mock-key")
            return httpx.Response(200, request=httpx.Request("POST", url), json={
                "choices": [{"message": {"content": '{"summary":"candidate","proposal":null}'}}]
            })

        with patch.dict(os.environ, {"MEMO_LLM_BASE_URL": "https://model.invalid/v1",
                                     "MEMO_LLM_MODEL": "example", "MEMO_LLM_API_KEY": "mock-key"}):
            with patch("memo_system.service.httpx.post", side_effect=post):
                self.assertEqual(OpenAICompatible().complete("instructions", {}, Suggestion).summary, "candidate")


class MCPTest(unittest.TestCase):
    def test_stdio_data_and_reviewer_boundary(self):
        async def run(path):
            async def client(reviewer):
                command = ["-m", "memo_system.server"]
                if reviewer:
                    command.append("--reviewer")
                params = StdioServerParameters(command=sys.executable, args=command,
                                               env={**os.environ, "MEMO_DB_PATH": path})
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        self.assertEqual("memo_review" in {tool.name for tool in tools.tools}, reviewer)
                        if not reviewer:
                            result = await session.call_tool("memo_submit", {
                                "proposal": proposal(add(evidence("source") | {
                                    "valid_from": "2025-01-01T00:00:00Z",
                                })).model_dump(mode="json"),
                                "expected_epoch": 0,
                            })
                            self.assertFalse(result.isError, result.content)
                            return result.structuredContent["proposal_id"]
                        result = await session.call_tool("memo_review", {
                            "proposal_id": proposal_id, "review": approval().model_dump(mode="json"),
                        })
                        self.assertFalse(result.isError, result.content)
                        result = await session.call_tool("memo_search", {"kind": "evidence"})
                        self.assertFalse(result.isError, result.content)
                        self.assertEqual(result.structuredContent["results"][0]["record"]["id"], "source")

            proposal_id = await client(False)
            await client(True)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(asyncio.wait_for(run(str(Path(directory) / "mcp.db")), timeout=15))
