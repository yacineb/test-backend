from app.application.deps import AuthDeps
from app.application.tokens import issue_token_pair
from app.domain.auth import TokenPair
from app.domain.errors import InactiveUser, InvalidCredentials


async def login(deps: AuthDeps, email: str, password: str) -> TokenPair:
    user = await deps.users.get_by_email(email.strip().lower())

    if user is None:
        # Cost the same wall-clock time as a wrong password, otherwise response
        # latency is a user-enumeration oracle.
        deps.hasher.verify_dummy(password)
        raise InvalidCredentials("invalid email or password")

    if not deps.hasher.verify(password, user.password_hash):
        raise InvalidCredentials("invalid email or password")

    if not user.is_active:
        raise InactiveUser("account is disabled")

    pair = await issue_token_pair(deps, user.id, user.org_id)
    await deps.uow.commit()
    return pair
