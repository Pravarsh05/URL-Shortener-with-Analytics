import io
from fastapi import APIRouter, HTTPException, Response, status
import qrcode
from app.config import settings
from app.database import get_db_pool

router = APIRouter(prefix="/api/v1/links", tags=["QR Code"])

@router.get("/{code}/qr")
async def get_link_qr(code: str):
    """Generates a high-resolution QR code PNG image for the shortened URL."""
    pool = get_db_pool()
    query = "SELECT code, is_active FROM links WHERE code = $1;"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, code)

    if not row or not row["is_active"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found or inactive.")

    target_short_url = f"{settings.BASE_URL.rstrip('/')}/{code}"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=3,
    )
    qr.add_data(target_short_url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="#0F172A", back_color="#FFFFFF")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    return Response(content=buf.getvalue(), media_type="image/png")
