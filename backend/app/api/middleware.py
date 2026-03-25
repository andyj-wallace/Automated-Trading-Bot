"""
FastAPI error-handling middleware.

Catches any unhandled exception that escapes a route handler, logs it to
error.log with full request context, and returns a standard JSON error
envelope so the client always receives a consistent response shape.

HTTPException is intentionally NOT caught here — FastAPI handles those
itself and they are expected, not unexpected.
"""

import traceback
import uuid

from fastapi import Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.monitoring.logger import error_logger


class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = str(uuid.uuid4())

        try:
            response = await call_next(request)
            return response
        except HTTPException:
            # Let FastAPI's built-in HTTPException handler deal with these.
            raise
        except Exception as exc:
            error_logger.error(
                "Unhandled exception",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )
            return JSONResponse(
                status_code=500,
                content={
                    "data": None,
                    "error": {
                        "code": "INTERNAL_SERVER_ERROR",
                        "message": "An unexpected error occurred",
                        "request_id": request_id,
                    },
                },
            )
