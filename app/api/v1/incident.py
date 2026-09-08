"""REST endpoint: automated SRE incident post-mortem export (Markdown and JSON)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

from app.services.incident_log import incident_log, render_markdown

router = APIRouter(tags=["incident"])


@router.get("/incident/post-mortem", summary="Export an incident post-mortem report")
async def get_post_mortem(
    token: str | None = Query(default=None, description="Confirmation token identifying the incident. Defaults to the most recently resolved incident."),
    format: str = Query(default="json", pattern="^(json|markdown)$", description="Output format: 'json' or 'markdown'."),
) -> Response:
    """Return a detailed SRE incident report: timeline, triggering alert, remediation command,
    actor, confirmation token, and Mean Time To Resolution (MTTR).

    Without `token`, returns the most recently *resolved* incident. Pending (unconfirmed or
    dry-run-only) incidents are not eligible for export until they are actually executed.
    """
    record = incident_log.get(token) if token is not None else incident_log.latest_resolved()
    if record is None or record.resolved_at is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No resolved incident found for export.")

    if format == "markdown":
        return Response(
            content=render_markdown(record),
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="post-mortem-{record.token}.md"'},
        )

    payload = record.model_dump(mode="json")
    payload["markdown"] = render_markdown(record)
    return JSONResponse(content=payload)
