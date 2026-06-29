from typing import Any

from vexic.ports import HostPortNotConfigured

EMBEDDING_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

_EMBEDDING_MODEL: Any | None = None


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    model = _embedding_model()
    vectors = [_normalize_embedding(_as_float_list(vector)) for vector in model.embed(texts)]
    if len(vectors) != len(texts):
        raise ValueError("Embedder must return exactly one embedding per input text.")
    return vectors


def _embedding_model() -> Any:
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise HostPortNotConfigured(
                "Embeddings require the optional local-embed extra. "
                "Install it with `pip install vexic[local-embed]`."
            ) from exc
        _EMBEDDING_MODEL = TextEmbedding(model_name=EMBEDDING_MODEL_NAME)
    return _EMBEDDING_MODEL


def _as_float_list(vector: Any) -> list[float]:
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    values = [float(value) for value in vector]
    if len(values) != EMBEDDING_DIM:
        raise ValueError(f"Expected {EMBEDDING_DIM}-dim embedding; got {len(values)}.")
    return values


def _normalize_embedding(vector: list[float]) -> list[float]:
    magnitude = sum(value * value for value in vector) ** 0.5
    if magnitude == 0:
        raise ValueError("Embedding magnitude must be greater than zero.")
    return [value / magnitude for value in vector]
