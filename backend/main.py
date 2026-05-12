from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
