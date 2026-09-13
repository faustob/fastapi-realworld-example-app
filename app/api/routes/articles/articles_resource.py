import time
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from starlette import status

from app.api.dependencies.articles import (
    check_article_modification_permissions,
    get_article_by_slug_from_path,
    get_articles_filters,
)
from app.api.dependencies.authentication import get_current_user_authorizer
from app.api.dependencies.database import get_repository
from app.db.repositories.articles import ArticlesRepository
from app.models.domain.articles import Article
from app.models.domain.users import User
from app.models.schemas.articles import (
    ArticleForResponse,
    ArticleInCreate,
    ArticleInResponse,
    ArticleInUpdate,
    ArticlesFilters,
    ListOfArticlesInResponse,
)
from app.core.telemetry import (
    current_flow_id,
    flow_duration_histogram,
    flow_outcome_counter,
    flow_validation_counter,
    tracer,
)
from app.resources import strings
from app.services.articles import check_article_exists, get_slug_for_article

router = APIRouter()


@router.get("", response_model=ListOfArticlesInResponse, name="articles:list-articles")
async def list_articles(
    articles_filters: ArticlesFilters = Depends(get_articles_filters),
    user: Optional[User] = Depends(get_current_user_authorizer(required=False)),
    articles_repo: ArticlesRepository = Depends(get_repository(ArticlesRepository)),
) -> ListOfArticlesInResponse:
    articles = await articles_repo.filter_articles(
        tag=articles_filters.tag,
        author=articles_filters.author,
        favorited=articles_filters.favorited,
        limit=articles_filters.limit,
        offset=articles_filters.offset,
        requested_user=user,
    )
    articles_for_response = [
        ArticleForResponse.from_orm(article) for article in articles
    ]
    return ListOfArticlesInResponse(
        articles=articles_for_response,
        articles_count=len(articles),
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ArticleInResponse,
    name="articles:create-article",
)
async def create_new_article(
    article_create: ArticleInCreate = Body(..., embed=True, alias="article"),
    user: User = Depends(get_current_user_authorizer()),
    articles_repo: ArticlesRepository = Depends(get_repository(ArticlesRepository)),
) -> ArticleInResponse:
    flow_start_time = time.perf_counter()
    with tracer.start_as_current_span("primary_flow.publish_article") as flow_span:
        flow_span.set_attribute("flow.id", current_flow_id())
        try:
            slug = get_slug_for_article(article_create.title)
            with tracer.start_as_current_span(
                "flow.validation.article_slug_uniqueness",
            ) as validation_span:
                article_exists = await check_article_exists(articles_repo, slug)
                validation_span.set_attribute(
                    "validation.outcome",
                    "fail" if article_exists else "pass",
                )
            flow_validation_counter.add(
                1,
                {
                    "validation.step": "article_slug_uniqueness",
                    "outcome": "fail" if article_exists else "pass",
                },
            )
            if article_exists:
                flow_outcome_counter.add(
                    1,
                    {"outcome": "failure", "flow.step": "article_publish"},
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=strings.ARTICLE_ALREADY_EXISTS,
                )

            article = await articles_repo.create_article(
                slug=slug,
                title=article_create.title,
                description=article_create.description,
                body=article_create.body,
                author=user,
                tags=article_create.tags,
            )
            article_in_response = ArticleInResponse(
                article=ArticleForResponse.from_orm(article),
            )
            flow_outcome_counter.add(
                1,
                {"outcome": "success", "flow.step": "article_publish"},
            )
            return article_in_response
        finally:
            flow_duration_histogram.record(time.perf_counter() - flow_start_time)


@router.get("/{slug}", response_model=ArticleInResponse, name="articles:get-article")
async def retrieve_article_by_slug(
    article: Article = Depends(get_article_by_slug_from_path),
) -> ArticleInResponse:
    return ArticleInResponse(article=ArticleForResponse.from_orm(article))


@router.put(
    "/{slug}",
    response_model=ArticleInResponse,
    name="articles:update-article",
    dependencies=[Depends(check_article_modification_permissions)],
)
async def update_article_by_slug(
    article_update: ArticleInUpdate = Body(..., embed=True, alias="article"),
    current_article: Article = Depends(get_article_by_slug_from_path),
    articles_repo: ArticlesRepository = Depends(get_repository(ArticlesRepository)),
) -> ArticleInResponse:
    slug = get_slug_for_article(article_update.title) if article_update.title else None
    article = await articles_repo.update_article(
        article=current_article,
        slug=slug,
        **article_update.dict(),
    )
    return ArticleInResponse(article=ArticleForResponse.from_orm(article))


@router.delete(
    "/{slug}",
    status_code=status.HTTP_204_NO_CONTENT,
    name="articles:delete-article",
    dependencies=[Depends(check_article_modification_permissions)],
    response_class=Response,
)
async def delete_article_by_slug(
    article: Article = Depends(get_article_by_slug_from_path),
    articles_repo: ArticlesRepository = Depends(get_repository(ArticlesRepository)),
) -> None:
    await articles_repo.delete_article(article=article)
