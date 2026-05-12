from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from jose import JWTError, jwt
from uuid import UUID
import os

from backend.database import get_db
from backend.modules.auth.models import Seller


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"


async def get_current_seller(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db)
) -> Seller:
    """
    Extract seller_id from JWT and return Seller instance.

    IDOR Prevention (canon b2b-flows.md):
    - seller_id taken ONLY from JWT claims, never from request body/query
    - Used in all endpoints that modify seller's resources

    Args:
        token: JWT access token from Authorization header
        db: Database session

    Returns:
        Seller instance

    Raises:
        HTTPException 401: If token invalid or seller not found
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHORIZED", "message": "Could not validate credentials"},
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        seller_id_str: str = payload.get("sub")
        if seller_id_str is None:
            raise credentials_exception
        seller_id = UUID(seller_id_str)
    except (JWTError, ValueError):
        raise credentials_exception

    seller = await db.get(Seller, seller_id)
    if seller is None:
        raise credentials_exception

    return seller
