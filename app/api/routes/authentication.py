from fastapi import APIRouter, Body, Depends, HTTPException
from starlette.status import HTTP_201_CREATED, HTTP_400_BAD_REQUEST

from app.api.dependencies.database import get_repository
from app.core.config import get_app_settings
from app.core.settings.app import AppSettings
from app.core.telemetry import (
    flow_entry_counter,
    flow_outcome_counter,
    flow_validation_counter,
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
    flow_entry_counter.add(1, {"flow": "registration_to_first_article"})

    with tracer.start_as_current_span("flow.validation.username_uniqueness") as username_span:
        username_taken = await check_username_is_taken(users_repo, user_create.username)
        username_span.set_attribute("validation.outcome", "fail" if username_taken else "pass")
    flow_validation_counter.add(
        1,
        {
            "validation.step": "username_uniqueness",
            "outcome": "fail" if username_taken else "pass",
        },
    )
    if username_taken:
        flow_outcome_counter.add(
            1,
            {"outcome": "failure", "flow.step": "registration"},
        )
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=strings.USERNAME_TAKEN,
        )

    with tracer.start_as_current_span("flow.validation.email_uniqueness") as email_span:
        email_taken = await check_email_is_taken(users_repo, user_create.email)
        email_span.set_attribute("validation.outcome", "fail" if email_taken else "pass")
    flow_validation_counter.add(
        1,
        {
            "validation.step": "email_uniqueness",
            "outcome": "fail" if email_taken else "pass",
        },
    )
    if email_taken:
        flow_outcome_counter.add(
            1,
            {"outcome": "failure", "flow.step": "registration"},
        )
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=strings.EMAIL_TAKEN,
        )

    user = await users_repo.create_user(**user_create.dict())

    token = jwt.create_access_token_for_user(
        user,
        str(settings.secret_key.get_secret_value()),
    )
    user_in_response = UserInResponse(
        user=UserWithToken(
            username=user.username,
            email=user.email,
            bio=user.bio,
            image=user.image,
            token=token,
        ),
    )
    flow_outcome_counter.add(
        1,
        {"outcome": "success", "flow.step": "registration"},
    )
    return user_in_response
