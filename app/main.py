import asyncio
import hashlib
import hmac
import io
import os
import re
import secrets
import uuid
import warnings
from datetime import datetime, timedelta
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .models import LoginSession, Photo, User

DATABASE_URL = os.environ["DATABASE_URL"]
SESSION_SECRET = os.environ["SESSION_SECRET"].encode()
CSRF_SECRET = os.environ["CSRF_SECRET"].encode()
COOKIE_SECURE_VALUE = os.getenv("COOKIE_SECURE", "true").lower()
if COOKIE_SECURE_VALUE not in {"true", "false"}:
    raise RuntimeError("COOKIE_SECURE must be either 'true' or 'false'")
COOKIE_SECURE = COOKIE_SECURE_VALUE == "true"
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/data/uploads"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", "10485760"))
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "12"))
if len(SESSION_SECRET) < 32 or len(CSRF_SECRET) < 32:
    raise RuntimeError("SESSION_SECRET and CSRF_SECRET must each contain at least 32 bytes")
if MAX_UPLOAD_BYTES < 1024 or MAX_UPLOAD_BYTES > 25 * 1024 * 1024:
    raise RuntimeError("MAX_UPLOAD_BYTES must be between 1 KiB and 25 MiB")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800)
SessionLocal = sessionmaker(engine, expire_on_commit=False)
password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
DUMMY_PASSWORD_HASH = password_hasher.hash("inecat-login-timing-only-not-a-user-password")
COOKIE_NAME = "inecat_session"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
MAX_IMAGE_PIXELS = 12_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
IMAGE_PROCESSING = asyncio.Semaphore(1)

app = FastAPI(title="Inecat Gallery API", docs_url=None, redoc_url=None)


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=12, max_length=128)


class PhotoUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def keyed_hash(key: bytes, value: str) -> str:
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def current_session(request: Request, db: Session = Depends(get_db)):
    raw = request.cookies.get(COOKIE_NAME)
    if not raw or len(raw) > 128:
        return None
    row = db.execute(select(LoginSession, User).join(User).where(
        LoginSession.token_hash == keyed_hash(SESSION_SECRET, raw),
        LoginSession.expires_at > datetime.utcnow(), User.is_active.is_(True),
    )).first()
    return (row[0], row[1]) if row else None


def require_user(session=Depends(current_session)):
    if not session:
        raise HTTPException(401, "Please log in")
    return session


def require_admin(session=Depends(require_user)):
    if not session[1].is_admin:
        raise HTTPException(403, "Administrator access required")
    return session


def check_origin(request: Request):
    expected = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    origin = request.headers.get("origin", "")
    if not origin or origin != f"{scheme}://{expected}":
        raise HTTPException(403, "Invalid request origin")


def check_csrf(request: Request, login, supplied: str | None):
    check_origin(request)
    if not login:
        raise HTTPException(401, "Please log in")
    if not supplied or not hmac.compare_digest(keyed_hash(CSRF_SECRET, supplied), login[0].csrf_hash):
        raise HTTPException(403, "Invalid CSRF token")


def photo_json(photo: Photo, owner: User):
    return {
        "id": photo.id, "title": photo.title, "description": photo.description,
        "url": f"/user-uploads/{photo.stored_name}", "mime_type": photo.mime_type,
        "file_size": photo.file_size, "created_at": photo.created_at.isoformat(),
        "owner_id": owner.id, "owner": owner.username,
    }


@app.on_event("startup")
def startup():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    return response


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok"}


@app.get("/community/")
def home():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/community/app.js")
def frontend_js():
    return FileResponse(Path(__file__).parent / "static" / "app.js", media_type="text/javascript")


@app.get("/community/style.css")
def frontend_css():
    return FileResponse(Path(__file__).parent / "static" / "style.css", media_type="text/css")


@app.get("/api/me")
def me(session=Depends(current_session)):
    if not session:
        return {"user": None}
    user = session[1]
    return {"user": {"id": user.id, "username": user.username, "is_admin": user.is_admin}}


@app.get("/api/csrf")
def csrf(session=Depends(require_user)):
    # Return the per-session value only to the same-origin authenticated page.
    # The raw token is derived from a random secret held only in the server-side session row.
    # We keep it in a short-lived signed-in-memory cache via the session row's keyed value below.
    return {"token": issue_csrf_for_session(session[0])}


def issue_csrf_for_session(login: LoginSession) -> str:
    # CSRF value is stable per session and recoverable only by the server.
    return hmac.new(CSRF_SECRET, f"csrf:{login.token_hash}".encode(), hashlib.sha256).hexdigest()


def create_login(db: Session, user: User, response: JSONResponse):
    raw = secrets.token_urlsafe(48)
    csrf_value = hmac.new(CSRF_SECRET, f"csrf:{keyed_hash(SESSION_SECRET, raw)}".encode(), hashlib.sha256).hexdigest()
    row = LoginSession(
        token_hash=keyed_hash(SESSION_SECRET, raw),
        csrf_hash=keyed_hash(CSRF_SECRET, csrf_value), user_id=user.id,
        expires_at=datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS),
    )
    db.add(row)
    db.execute(delete(LoginSession).where(LoginSession.expires_at <= datetime.utcnow()))
    db.commit()
    response.set_cookie(COOKIE_NAME, raw, max_age=SESSION_TTL_HOURS * 3600, httponly=True,
                        secure=COOKIE_SECURE, samesite="lax", path="/")
    return response


@app.post("/api/register")
def register(data: Credentials, request: Request, db: Session = Depends(get_db)):
    check_origin(request)
    if not USERNAME_RE.fullmatch(data.username):
        raise HTTPException(422, "Use 3-32 letters, numbers, or underscores for username")
    if db.scalar(select(User.id).where(User.username == data.username)):
        raise HTTPException(409, "Username is unavailable")
    user = User(username=data.username, password_hash=password_hasher.hash(data.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Username is unavailable")
    response = JSONResponse({"user": {"id": user.id, "username": user.username, "is_admin": False}})
    return create_login(db, user, response)


@app.post("/api/login")
def login(data: Credentials, request: Request, db: Session = Depends(get_db)):
    check_origin(request)
    user = db.scalar(select(User).where(User.username == data.username, User.is_active.is_(True)))
    try:
        ok = bool(user and password_hasher.verify(user.password_hash, data.password))
    except (VerifyMismatchError, InvalidHashError):
        ok = False
    if not ok:
        # Spend comparable hash work for unknown usernames to reduce enumeration timing clues.
        if not user:
            try:
                password_hasher.verify(DUMMY_PASSWORD_HASH, data.password)
            except Exception:
                pass
        raise HTTPException(401, "Invalid username or password")
    response = JSONResponse({"user": {"id": user.id, "username": user.username, "is_admin": user.is_admin}})
    return create_login(db, user, response)


@app.post("/api/logout")
def logout(request: Request, x_csrf_token: str | None = Header(default=None), session=Depends(require_user), db: Session = Depends(get_db)):
    check_csrf(request, session, x_csrf_token)
    db.delete(session[0])
    db.commit()
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/api/photos")
def list_photos(limit: int = 48, offset: int = 0, db: Session = Depends(get_db)):
    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)
    rows = db.execute(select(Photo, User).join(User, Photo.owner_id == User.id).order_by(Photo.created_at.desc(), Photo.id.desc()).limit(limit).offset(offset)).all()
    return {"photos": [photo_json(photo, owner) for photo, owner in rows], "limit": limit, "offset": offset}


async def read_valid_image(upload: UploadFile) -> tuple[bytes, str]:
    contents = bytearray()
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        contents.extend(chunk)
        if len(contents) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Image exceeds the configured upload limit")
    if not contents:
        raise HTTPException(400, "Choose an image file")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(contents)) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise HTTPException(413, "Image exceeds the 12 megapixel limit")
                if image.format not in {"JPEG", "PNG", "WEBP"}:
                    raise HTTPException(415, "Only JPEG, PNG, and WebP images are allowed")
                image.verify()
            with Image.open(io.BytesIO(contents)) as image:
                image = ImageOps.exif_transpose(image)
                fmt = image.format
                if fmt == "JPEG":
                    image = image.convert("RGB")
                    mime = "image/jpeg"
                elif fmt == "PNG":
                    image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                    mime = "image/png"
                else:
                    image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                    mime = "image/webp"
                output = io.BytesIO()
                image.save(output, format=fmt, optimize=True)
                sanitized = output.getvalue()
                if len(sanitized) > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Processed image exceeds the upload limit")
                return sanitized, mime
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(415, "The uploaded file is not a safe supported image")


@app.post("/api/photos", status_code=201)
async def upload_photo(request: Request, title: str, description: str = "", file: UploadFile = File(...),
                       x_csrf_token: str | None = Header(default=None), session=Depends(require_user), db: Session = Depends(get_db)):
    check_csrf(request, session, x_csrf_token)
    if not 1 <= len(title.strip()) <= 120 or len(description) > 2000:
        raise HTTPException(422, "Title must have 1-120 characters; description is limited to 2000")
    async with IMAGE_PROCESSING:
        content, mime = await read_valid_image(file)
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[mime]
    stored_name = f"{uuid.uuid4().hex}{ext}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_DIR / stored_name
    temp = UPLOAD_DIR / f".{stored_name}.tmp"
    try:
        with temp.open("xb") as handle:
            handle.write(content)
        os.replace(temp, target)
        photo = Photo(owner_id=session[1].id, stored_name=stored_name, title=title.strip(),
                      description=description.strip(), mime_type=mime, file_size=len(content))
        db.add(photo)
        db.commit()
        db.refresh(photo)
        return photo_json(photo, session[1])
    except Exception:
        db.rollback()
        target.unlink(missing_ok=True)
        raise
    finally:
        temp.unlink(missing_ok=True)


@app.patch("/api/photos/{photo_id}")
def update_photo(photo_id: int, data: PhotoUpdate, request: Request, x_csrf_token: str | None = Header(default=None),
                 session=Depends(require_user), db: Session = Depends(get_db)):
    check_csrf(request, session, x_csrf_token)
    if not data.title.strip():
        raise HTTPException(422, "Title cannot be blank")
    stmt = update(Photo).where(Photo.id == photo_id)
    if not session[1].is_admin:
        stmt = stmt.where(Photo.owner_id == session[1].id)
    result = db.execute(stmt.values(title=data.title.strip(), description=data.description.strip()).execution_options(synchronize_session="fetch"))
    if not result.rowcount:
        db.rollback()
        raise HTTPException(404, "Photo not found")
    db.commit()
    photo, owner = db.execute(select(Photo, User).join(User).where(Photo.id == photo_id)).one()
    return photo_json(photo, owner)


@app.delete("/api/photos/{photo_id}")
def delete_photo(photo_id: int, request: Request, x_csrf_token: str | None = Header(default=None),
                 session=Depends(require_user), db: Session = Depends(get_db)):
    check_csrf(request, session, x_csrf_token)
    stmt = select(Photo).where(Photo.id == photo_id)
    if not session[1].is_admin:
        stmt = stmt.where(Photo.owner_id == session[1].id)
    photo = db.scalar(stmt)
    if not photo:
        raise HTTPException(404, "Photo not found")
    stored_name = photo.stored_name
    db.delete(photo)
    db.commit()
    (UPLOAD_DIR / stored_name).unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/admin/users")
def admin_users(session=Depends(require_admin), db: Session = Depends(get_db)):
    users = db.scalars(select(User).order_by(User.created_at.desc())).all()
    return {"users": [{"id": u.id, "username": u.username, "is_admin": u.is_admin,
                        "is_active": u.is_active, "created_at": u.created_at.isoformat()} for u in users]}


@app.patch("/api/admin/users/{user_id}/active")
def admin_set_active(user_id: int, active: bool, request: Request, x_csrf_token: str | None = Header(default=None),
                     session=Depends(require_admin), db: Session = Depends(get_db)):
    check_csrf(request, session, x_csrf_token)
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if target.id == session[1].id and not active:
        raise HTTPException(400, "You cannot deactivate your own account")
    if target.is_admin and not active and db.scalar(select(User.id).where(User.is_admin.is_(True), User.is_active.is_(True), User.id != target.id).limit(1)) is None:
        raise HTTPException(400, "At least one active administrator must remain")
    target.is_active = active
    if not active:
        db.execute(delete(LoginSession).where(LoginSession.user_id == target.id))
    db.commit()
    return {"id": target.id, "is_active": target.is_active}
