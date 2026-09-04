"""
Contract §2.8 — every service returns errors in the exact shape:
    { "error": "human-readable message" }
with an appropriate HTTP status code (400 = bad input, 500 = internal failure).
"""
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException


def install_error_handlers(app):
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        # Malformed / missing-field request bodies -> 400, flat {"error": ...} shape.
        return JSONResponse(
            status_code=400,
            content={"error": f"Bad request: {exc.errors()}"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": str(exc.detail)},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        # Never crash silently / never return empty on failure.
        return JSONResponse(
            status_code=500,
            content={"error": f"Internal error: {exc}"},
        )
