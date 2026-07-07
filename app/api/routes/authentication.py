import time

from fastapi import APIRouter, Body, Depends, HTTPException
from starlette.status import HTTP_201_CREATED, HTTP_400_BAD_REQUEST
from opentelemetry import trace
from app.telemetry import (
    auth_attempts,
    flow_duration,
    flow_entry_counter,
    flow_outcomes,
    flow_validation_outcomes,
    get_tracer,
)

from app.api.dependencies.database import get_repository
from app.core.config import get_app_settings
from app.core.settings.app import AppSettings
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
        auth_attempts.add(1, {"outcome": "denied", "reason": "wrong_password"})
        raise wrong_login_error from existence_error

    if not user.check_password(user_login.password):
        auth_attempts.add(1, {"outcome": "denied", "reason": "wrong_password"})
        raise wrong_login_error

    auth_attempts.add(1, {"outcome": "success", "reason": ""})
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
    tracer = get_tracer()
    flow_entry_counter.add(1, {"flow": "registration_to_publish"})
    _flow_start = time.monotonic()

    with tracer.start_as_current_span("flow.registration_to_publish") as flow_span:
        with tracer.start_as_current_span("flow.validation.username") as val_span:
            username_taken = await check_username_is_taken(users_repo, user_create.username)
            val_span.set_attribute("flow.validation.step", "username")
            val_span.set_attribute("flow.validation.passed", not username_taken)
            if username_taken:
                flow_validation_outcomes.add(1, {"flow": "registration_to_publish", "step": "username", "outcome": "failed"})
                flow_outcomes.add(1, {"flow": "registration_to_publish", "outcome": "failure"})
                flow_duration.record(time.monotonic() - _flow_start, {"flow": "registration_to_publish", "terminal_state": "validation_failed"})
                raise HTTPException(
                    status_code=HTTP_400_BAD_REQUEST,
                    detail=strings.USERNAME_TAKEN,
                )
            flow_validation_outcomes.add(1, {"flow": "registration_to_publish", "step": "username", "outcome": "passed"})

        with tracer.start_as_current_span("flow.validation.email") as val_span:
            email_taken = await check_email_is_taken(users_repo, user_create.email)
            val_span.set_attribute("flow.validation.step", "email")
            val_span.set_attribute("flow.validation.passed", not email_taken)
            if email_taken:
                flow_validation_outcomes.add(1, {"flow": "registration_to_publish", "step": "email", "outcome": "failed"})
                flow_outcomes.add(1, {"flow": "registration_to_publish", "outcome": "failure"})
                flow_duration.record(time.monotonic() - _flow_start, {"flow": "registration_to_publish", "terminal_state": "validation_failed"})
                raise HTTPException(
                    status_code=HTTP_400_BAD_REQUEST,
                    detail=strings.EMAIL_TAKEN,
                )
            flow_validation_outcomes.add(1, {"flow": "registration_to_publish", "step": "email", "outcome": "passed"})

        user = await users_repo.create_user(**user_create.dict())

        token = jwt.create_access_token_for_user(
            user,
            str(settings.secret_key.get_secret_value()),
        )
        flow_outcomes.add(1, {"flow": "registration_to_publish", "outcome": "success"})
        flow_duration.record(time.monotonic() - _flow_start, {"flow": "registration_to_publish", "terminal_state": "registered"})
        flow_span.set_attribute("flow", "registration_to_publish")
        flow_span.set_attribute("flow.outcome", "success")
        return UserInResponse(
            user=UserWithToken(
                username=user.username,
                email=user.email,
                bio=user.bio,
                image=user.image,
                token=token,
            ),
        )
