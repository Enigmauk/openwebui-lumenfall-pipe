"""Small authenticated FastAPI boundary for the development worker."""

from __future__ import annotations

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .db import DuplicateRequestConflict
from .security import valid_worker_bearer
from .service import WorkerService


class CreateJob(BaseModel):
    chat_id: str = Field(min_length=1, max_length=128)
    assistant_message_id: str = Field(min_length=1, max_length=128)
    friendly_model: str = Field(min_length=1, max_length=100)
    model_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    prompt: str = Field(min_length=1, max_length=20_000)
    options: dict = Field(default_factory=dict)
    user_credential: str = Field(min_length=1, max_length=8192)


class RefreshCredential(BaseModel):
    user_credential: str = Field(min_length=1, max_length=8192)


def create_app(service: WorkerService, worker_secret: str) -> FastAPI:
    app = FastAPI(title="Lumenfall video worker", docs_url=None, redoc_url=None)

    def authenticate(authorization: str | None = Header(None)) -> None:
        prefix = "Bearer "
        candidate = authorization[len(prefix):] if authorization and authorization.startswith(prefix) else None
        if not valid_worker_bearer(candidate, worker_secret):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Worker authentication required.")

    def authenticate_owner(
        _: None = Depends(authenticate),
        owner: str | None = Header(None, alias="X-OpenWebUI-User-Id"),
    ) -> str:
        if not owner:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Owner context required.")
        return owner

    @app.get("/health")
    async def health(_: None = Depends(authenticate)):
        return {"status": "ok", "database": service.store.ping()}

    @app.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
    async def create_job(form: CreateJob, background: BackgroundTasks,
                         owner: str = Depends(authenticate_owner)):
        try:
            job, created = service.create_job(owner_user_id=owner, **form.model_dump())
        except DuplicateRequestConflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if created:
            background.add_task(service.advance, job.job_id)
        return {**job.public(), "created": created}

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str, owner: str = Depends(authenticate_owner)):
        job = service.owned(job_id, owner)
        if not job:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Video job not found.")
        return job.public()

    @app.delete("/jobs/{job_id}")
    async def cancel_job(job_id: str, owner: str = Depends(authenticate_owner)):
        job = await service.request_cancel(job_id, owner)
        if not job:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Video job not found.")
        return job.public()

    @app.post("/jobs/{job_id}/delivery-credential")
    async def refresh(job_id: str, form: RefreshCredential,
                      owner: str = Depends(authenticate_owner)):
        job = service.refresh_credential(job_id, owner, form.user_credential)
        if not job:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Video job not found.")
        return job.public()

    return app
