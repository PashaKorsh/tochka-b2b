from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.modules.products.router import router as products_router
from backend.database import create_tables


app = FastAPI(
    title="NeoMarket B2B API",
    description="Seller Cabinet API for NeoMarket B2B module",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ───────────────────── Unified error handling ─────────────────────
# Canon b2b-flows.md / spec b2b/neomarket-b2b.yaml#Error require every 4xx
# response to have the shape {"code": ..., "message": ..., "details"?: ...}.
# FastAPI's defaults return {"detail": ...}, so we override them globally.

_STATUS_CODE_NAMES = {
    400: "INVALID_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
}


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """422 — request body/query validation failed."""
    return JSONResponse(
        status_code=422,
        content={
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed",
            "details": {"errors": jsonable_encoder(exc.errors())},
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """401 / 403 / 404 / 409 / ... — unify HTTPException into {code, message}."""
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        # Handler already produced a canonical error body (e.g. auth, invalid category).
        body = {"code": detail["code"], "message": detail["message"]}
        if detail.get("details") is not None:
            body["details"] = detail["details"]
    else:
        body = {
            "code": _STATUS_CODE_NAMES.get(exc.status_code, "ERROR"),
            "message": detail if isinstance(detail, str) else str(detail),
        }
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        headers=getattr(exc, "headers", None),
    )


# Include routers
app.include_router(products_router)


@app.on_event("startup")
async def startup():
    """Create database tables on startup"""
    await create_tables()


@app.get("/")
async def root():
    return {"message": "NeoMarket B2B API", "version": "1.0.0"}


@app.get("/health")
async def health():
    return {"status": "ok"}
