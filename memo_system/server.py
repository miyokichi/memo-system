from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .models import ActionPlan, Context, EvaluationRun, Key, Proposal, Review, Text
from .rules import available
from .service import Memory, Task
from .store import Store, utcnow

Kind = Literal["experience", "concept", "fact", "principle", "procedure", "evidence", "relation"]


def create_server(path: str, *, reviewer: bool = False) -> FastMCP:
    store = Store(path)
    memory = Memory(store)
    server = FastMCP(
        "memo-system",
        instructions=(
            "Search uses current knowledge. Pass context attributes to resolve scope. All proposals are "
            "untrusted drafts requiring review. Concretization returns a draft plan and performs no actions."
        ),
    )

    @server.tool()
    def memo_search(query: str = "", context: Context | None = None, kind: Kind | None = None,
                    limit: Annotated[int, Field(ge=1, le=100)] = 20, include_unknown: bool = False) -> dict[str, Any]:
        """Search current knowledge and return the snapshot epoch for optimistic concurrency."""
        return memory.search(query, context or {}, kind, limit, include_unknown)

    @server.tool()
    def memo_history(record_id: Key) -> dict[str, Any]:
        """Read versions, withdrawal reasons, and current links for one ID. For audit and review only."""
        return store.audit(record_id)

    @server.tool()
    def memo_health() -> dict[str, Any]:
        """Report current revision states and effective expiry or dependency blocks for review."""
        epoch, records, _ = store.snapshot()
        now = utcnow()
        live = available(records, now)
        inactive = []
        for key, item in sorted(records.items()):
            if key in live:
                continue
            if item.status.value != "active":
                state = item.status.value
            elif item.valid_until and item.valid_until <= now:
                state = "expired"
            elif item.valid_from and item.valid_from > now:
                state = "scheduled"
            else:
                state = "dependency_or_endpoint_unavailable"
            inactive.append({"id": key, "version": item.version, "state": state})
        return {"epoch": epoch, "available_count": len(live), "inactive": inactive}

    @server.tool()
    def memo_submit(proposal: Proposal, expected_epoch: Annotated[int, Field(ge=0)]) -> dict[str, Any]:
        """Submit a batch for review. Add experience and evidence records via the add operation."""
        return {"proposal_id": store.submit(proposal, expected_epoch), "decision": "pending"}

    @server.tool()
    def memo_proposals(proposal_id: int | None = None) -> list[dict[str, Any]]:
        """List pending or held proposals, or inspect one proposal and its review."""
        return store.proposals(proposal_id)

    @server.tool()
    async def memo_suggest(task: Task, brief: Text, context: Context | None = None,
                     record_ids: list[Key] | None = None) -> dict[str, Any]:
        """Generate a typed draft for abstraction, differentiation, update, forgetting, or validation."""
        return await asyncio.to_thread(memory.suggest, task, brief, context or {}, record_ids or [])

    @server.tool()
    async def memo_concretize(goal: Text, context: Context | None = None, principle_ids: list[Key] | None = None) -> dict[str, Any]:
        """Generate and check a draft plan with current, applicable knowledge and versioned citations."""
        return await asyncio.to_thread(memory.concretize, goal, context or {}, principle_ids or [])

    @server.tool()
    def memo_verify_plan(plan: ActionPlan, goal: Text, expected_epoch: Annotated[int, Field(ge=0)],
                         context: Context | None = None, principle_ids: list[Key] | None = None) -> dict[str, Any]:
        """Check a caller-supplied plan for scope, freshness, citations, and declared action constraints."""
        return memory.verify_plan(plan, goal, context or {}, principle_ids or [], expected_epoch)

    @server.tool()
    def memo_evaluate(runs: Annotated[list[EvaluationRun], Field(min_length=1, max_length=1000)]) -> dict[str, Any]:
        """Score externally authored evaluation cases and observed results against current knowledge."""
        return memory.evaluate(runs)

    if reviewer:
        @server.tool()
        def memo_review(proposal_id: Annotated[int, Field(gt=0)], review: Review) -> dict[str, Any]:
            """Record an authorized expert decision and atomically apply a verified proposal on approval."""
            return store.review(proposal_id, review)

    return server


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Run the memo-system MCP server over stdio.")
    parser.add_argument("--reviewer", action="store_true", help="Expose the review tool to a trusted MCP client.")
    args = parser.parse_args()
    path = os.getenv("MEMO_DB_PATH", str(Path.home() / ".local/share/memo-system/memo.db"))
    create_server(path, reviewer=args.reviewer).run(transport="stdio")


if __name__ == "__main__":
    main()
