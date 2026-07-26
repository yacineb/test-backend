from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class User:
    id: UUID
    org_id: UUID
    email: str
    full_name: str
    password_hash: str
    is_active: bool
