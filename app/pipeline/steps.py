"""Provider mocks, reproduced verbatim from the exercise statement.

Do not change the signatures, the sleeps, or the failure rates. Everything
that adapts them to the pipeline lives in tasks.py.
"""

import random
import time
import uuid

def ocr() -> str:
    time.sleep(random.uniform(1, 15))
    if random.random() < 1/3:
        raise TimeoutError("OCR provider timeout")
    return "lorem ipsum..."

def metadata(text: str) -> dict:
    time.sleep(random.uniform(1, 10))
    if random.random() < 1/3:
        raise ValueError("metadata extraction failed")
    return {"doc_type": "fake_type"}

def chunking(text: str) -> list[str]:
    time.sleep(random.uniform(1, 12))
    if random.random() < 1/3:
        raise ValueError("chunking failed")
    return ["chunk_1", "chunk_2", "..."]

def external_call(doc_id: str, ocr_text: str, meta: dict, chunks: list[str]) -> str:
    """Simule l'appel HTTP sortant vers le partenaire.
    Retourne un job_id opaque. Le résultat réel arrive plus tard via webhook."""
    time.sleep(random.uniform(1, 5))
    if random.random() < 1/3:
        raise ConnectionError("partner unreachable")
    return f"j_{uuid.uuid4().hex[:16]}"
