from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(
    title="Document processing API",
    version="0.1.0",
)


class Health(BaseModel):
    status: str


@app.get("/health", tags=["ops"], summary="Liveness probe")
def health() -> Health:
    return Health(status="ok")
