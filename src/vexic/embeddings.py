from vexic.ports import missing_host_port

EMBEDDING_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    raise missing_host_port("Embeddings")
