from fastapi import APIRouter, Body, Depends, HTTPException
from starlette.status import HTTP_201_CREATED, HTTP_400_BAD_REQUEST

from app.api.dependencies.database import get_repository
from app.core.config import get_app_settings
from app.core.settings.app import AppSettings
from app.core.telemetry import (
    flow_entries_counter,
    flow_validation_outcomes_counter,
    tracer,
)
from app.db.errors import EntityDoesNotExist
from app.db.repositories.users import UsersRepository
from app.models.schemas.users import (
    UserInCreate,
    UserInLogin,
    UserInResponse,
    UserWithToken,
)
from app.resources import strings
from app.services import jwt
from app.services.authentication import check_email_is_taken, check_username_is_taken

router = APIRouter()


@router.post("/login", response_model=UserInResponse, name="auth:login")
async def login(
    user_login: UserInLogin = Body(..., embed=True, alias="user"),
    users_repo: UsersRepository = Depends(get_repository(UsersRepository)),
    settings: AppSettings = Depends(get_app_settings),
) -> UserInResponse:
    wrong_login_error = HTTPException(
        status_code=HTTP_400_BAD_REQUEST,
        detail=strings.INCORRECT_LOGIN_INPUT,
    )

    try:
        user = await users_repo.get_user_by_email(email=user_login.email)
    except EntityDoesNotExist as existence_error:
        raise wrong_login_error from existence_error

    if not user.check_password(user_login.password):
        raise wrong_login_error

    token = jwt.create_access_token_for_user(
        user,
        str(settings.secret_key.get_secret_value()),
    )
    return UserInResponse(
        user=UserWithToken(
            username=user.username,
            email=user.email,
            bio=user.bio,
            image=user.image,
            token=token,
        ),
    )


@router.post(
    "",
    status_code=HTTP_201_CREATED,
    response_model=UserInResponse,
    name="auth:register",
)
async def register(
    user_create: UserInCreate = Body(..., embed=True, alias="user"),
    users_repo: UsersRepository = Depends(get_repository(UsersRepository)),
    settings: AppSettings = Depends(get_app_settings),
) -> UserInResponse:
    flow_entries_counter.add(1, {"flow.name": "primary_flow"})

    with tracer.start_as_current_span("primary_flow.validate_registration") as validation_span:
        username_taken = await check_username_is_taken(users_repo, user_create.username)
        flow_validation_outcomes_counter.add(
            1,
            {
                "flow.name": "primary_flow",
                "validation.step": "username_uniqueness",
                "validation.outcome": "failed" if username_taken else "passed",
            },
        )
        if username_taken:
            validation_span.set_attribute("validation.outcome", "failed")
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=strings.USERNAME_TAKEN,
            )

        email_taken = await check_email_is_taken(users_repo, user_create.email)
        flow_validation_outcomes_counter.add(
            1,
            {
                "flow.name": "primary_flow",
                "validation.step": "email_uniqueness",
                "validation.outcome": "failed" if email_taken else "passed",
            },
        )
        if email_taken:
            validation_span.set_attribute("validation.outcome", "failed")
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=strings.EMAIL_TAKEN,
            )

        validation_span.set_attribute("validation.outcome", "passed")

    user = await users_repo.create_user(**user_create.dict())

    token = jwt.create_access_token_for_user(
        user,
        str(settings.secret_key.get_secret_value()),
    )
    return UserInResponse(
        user=UserWithToken(
            username=user.username,
            email=user.email,
            bio=user.bio,
            image=user.image,
            token=token,
        ),
    )
