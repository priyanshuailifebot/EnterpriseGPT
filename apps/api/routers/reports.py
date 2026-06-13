"""Report export endpoints (PDF download from agent markdown output)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from core.security import get_current_active_user
from models.user import User
from services.pdf_service import _safe_filename, render_pdf_bytes

router = APIRouter(prefix="/reports", tags=["reports"])


class RenderPdfRequest(BaseModel):
    title: str = Field(default="Report", max_length=255)
    content: str = Field(min_length=1, max_length=500_000)


@router.post(
    "/pdf",
    summary="Render markdown/plain report text as a downloadable PDF",
    response_class=Response,
)
async def render_report_pdf(
    body: RenderPdfRequest,
    _: User = Depends(get_current_active_user),
) -> Response:
    try:
        pdf_bytes = render_pdf_bytes(title=body.title, content=body.content)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    filename = _safe_filename(body.title)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
