from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, OrganizationRepositoryDep, UserRepositoryDep
from app.api.schemas import MeResponse

router = APIRouter(tags=["identity"])


@router.get(
    "/me",
    response_model=MeResponse,
    summary="The authenticated user and their organization",
    description=(
        "Demonstrates that user_id and org_id are available on any "
        "authenticated request, and that reads run through an RLS-scoped "
        "session."
    ),
)
async def me(
    ctx: CurrentUser,
    users: UserRepositoryDep,
    organizations: OrganizationRepositoryDep,
) -> MeResponse:
    user = await users.get_by_id(ctx.user_id)
    organization = await organizations.get(ctx.org_id)

    if user is None or organization is None:
        # Valid signature, but the subject no longer resolves inside this
        # tenant: user deleted, or org membership changed since issuance.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token subject no longer exists",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return MeResponse(
        user_id=user.id,
        org_id=organization.id,
        email=user.email,
        full_name=user.full_name,
        org_name=organization.name,
        org_slug=organization.slug,
    )
