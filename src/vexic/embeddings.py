from functools import cache
from typing import Any

EMBEDDING_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384


@cache
def _load_model() -> Any:
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBEDDING_MODEL_NAME)


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []

    model = _load_model()
    raw_embeddings = model.encode(
        texts,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )

    import numpy as np

    embeddings = np.asarray(raw_embeddings, dtype=np.float32)

    if embeddings.ndim != 2 or embeddings.shape[1] != EMBEDDING_DIM:
        raise ValueError(
            f"Expected {EMBEDDING_DIM}-dim embeddings from {EMBEDDING_MODEL_NAME}; "
            f"got shape {embeddings.shape}."
        )

    return embeddings.tolist()
