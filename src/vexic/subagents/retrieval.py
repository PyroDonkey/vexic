"""Tier 3 hybrid retrieval.

Deterministic Python orders the steps; one optional model call fills the
query-rewrite box. The requesting agent sees only the returned facts — BM25
scores, vector distances, and the rewrite never leave this module.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from vexic.embeddings import embed_texts
from vexic.models import QueryRewrite, RetrievedFact
from vexic.ports import AgentFactory, EmbedTexts, missing_host_port
from vexic.storage import (
    CandidateNote,
    LongTermFact,
    fetch_candidate_notes,
    fetch_long_term_facts,
    keyword_candidate_ids,
    keyword_long_term_fact_ids,
    nearest_candidate_ids,
    nearest_long_term_facts,
    record_candidate_retrieval,
    record_long_term_retrieval,
)

# Reciprocal Rank Fusion constant (docs/architecture.md, Long-term Search):
# BM25 and cosine scores are not on the same scale, so ranks fuse instead of
# raw scores.
RRF_K = 60
# Retrieve internally / return to the requester (tunable per call).
RETRIEVE_K = 20
RETURN_K = 5
QUERY_REWRITE_MAX_TOKENS = 4096

QUERY_REWRITE_SYSTEM_PROMPT = """\
You rewrite a conversational memory query into keyword search terms for a
full-text index over short factual statements about the user.

Return search_terms: a short space-separated list of the most likely literal
words (plus close synonyms) that would appear in matching fact statements.
Drop filler words and question phrasing. Keep proper nouns exactly as given.\
"""


def build_query_rewrite_agent(
    model_group: str,
    secrets: Mapping[str, str] | None = None,
) -> Any:
    raise missing_host_port("Long-term query rewrite")


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[int]],
    *,
    k: int = RRF_K,
) -> list[int]:
    """Fuse per-retriever rankings of fact ids into one ranked id list.

    rrf_score(id) = sum over rankings of 1 / (k + rank), rank starting at 1.
    Ties resolve by first appearance across the rankings (scan order), so the
    output is deterministic.
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, fact_id in enumerate(ranking, start=1):
            scores[fact_id] = scores.get(fact_id, 0.0) + 1.0 / (k + rank)
    # dicts preserve insertion order, so a stable sort keeps first-seen ids
    # ahead of later ids with equal scores.
    return sorted(scores, key=lambda fact_id: scores[fact_id], reverse=True)


async def _rewrite_query(
    query: str,
    model_group: str,
    secrets: Mapping[str, str] | None,
    usage: Any,
    query_rewrite_agent_factory: AgentFactory,
) -> str | None:
    # Degrade, never fail: retrieval must still answer from the original query
    # when the rewrite model is down or returns garbage.
    try:
        agent = query_rewrite_agent_factory(model_group, secrets=secrets)
        result = await agent.run(query, usage=usage)
        search_terms = result.output.search_terms.strip()
        return search_terms or None
    except Exception:
        return None


def _embed_query(embedder: EmbedTexts, query: str) -> list[float]:
    embeddings = embedder([query])
    if len(embeddings) != 1:
        raise ValueError("Embedder must return exactly one embedding for one query.")
    return embeddings[0]


async def retrieve_long_term_facts(
    db_path: str,
    query: str,
    *,
    session_id: str = "default",
    agent_id: str | None = None,
    model_group: str | None = None,
    secrets: Mapping[str, str] | None = None,
    usage: Any = None,
    retrieve_k: int = RETRIEVE_K,
    return_k: int = RETURN_K,
    sink: list[RetrievedFact] | None = None,
    query_rewrite_agent_factory: AgentFactory | None = None,
    embed: EmbedTexts | None = None,
) -> list[LongTermFact]:
    """Hybrid Tier 3 retrieval: FTS5 keyword + sqlite-vec KNN, fused via RRF.

    The vector retriever always embeds the original query (meaning lives
    there); the optional rewrite only sharpens the keyword retriever. Each
    returned fact is logged as a retrieval_events row (original query, not the
    rewrite) with its retrieved_count increment at the moment of retrieval.
    """
    keyword_query = query
    rewritten_query: str | None = None
    if model_group is not None:
        agent_factory = query_rewrite_agent_factory or build_query_rewrite_agent
        rewritten = await _rewrite_query(
            query,
            model_group,
            secrets,
            usage,
            agent_factory,
        )
        if rewritten is not None:
            rewritten_query = rewritten
            keyword_query = rewritten

    embedder = embed or embed_texts
    query_embedding = _embed_query(embedder, query)
    keyword_ids = keyword_long_term_fact_ids(
        db_path,
        keyword_query,
        k=retrieve_k,
        agent_id=agent_id,
    )
    vector_ids = [
        neighbor.fact_id
        for neighbor in nearest_long_term_facts(
            db_path,
            query_embedding,
            k=retrieve_k,
            agent_id=agent_id,
        )
    ]

    fused_ids = reciprocal_rank_fusion([keyword_ids, vector_ids])[:return_k]
    facts = fetch_long_term_facts(db_path, fused_ids, agent_id=agent_id)
    event_ids = record_long_term_retrieval(
        db_path,
        [fact.fact_id for fact in facts],
        session_id=session_id,
        agent_id=agent_id,
        query=query,
        rewritten_query=rewritten_query,
        keyword_fact_ids=keyword_ids,
        vector_fact_ids=vector_ids,
        fused_fact_ids=fused_ids,
        forbidden_secret_values=(secrets or {}).values(),
    )
    if sink is not None:
        sink.extend(
            RetrievedFact(fact_id=fact.fact_id, fact_text=fact.fact_text, event_id=event_id)
            for fact, event_id in zip(facts, event_ids)
        )
    return facts


async def retrieve_candidate_fallback(
    db_path: str,
    query: str,
    *,
    session_id: str = "default",
    agent_id: str | None = None,
    secrets: Mapping[str, str] | None = None,
    retrieve_k: int = RETRIEVE_K,
    return_k: int = RETURN_K,
    embed: EmbedTexts | None = None,
) -> list[CandidateNote]:
    """Tier 2 candidate-fallback retrieval from the hosted MCP design.

    The zero-Tier-3-hit rescue: hybrid FTS + sqlite-vec KNN over active
    unpromoted candidates, fused via RRF (same shape as Tier 3, no query
    rewrite — the keyword half is simple here and the path stays cheap). Each
    returned candidate is logged to candidate_retrieval_events with its
    retrieved_count increment. Returns flagged-tentative notes for the caller
    to present as unverified, never as durable facts.
    """
    embedder = embed or embed_texts
    query_embedding = _embed_query(embedder, query)
    keyword_ids = keyword_candidate_ids(db_path, query, k=retrieve_k, agent_id=agent_id)
    vector_ids = nearest_candidate_ids(
        db_path,
        query_embedding,
        k=retrieve_k,
        agent_id=agent_id,
    )

    fused_ids = reciprocal_rank_fusion([keyword_ids, vector_ids])[:return_k]
    notes = fetch_candidate_notes(db_path, fused_ids, agent_id=agent_id)
    record_candidate_retrieval(
        db_path,
        [note.candidate_id for note in notes],
        session_id=session_id,
        agent_id=agent_id,
        query=query,
        forbidden_secret_values=(secrets or {}).values(),
    )
    return notes
