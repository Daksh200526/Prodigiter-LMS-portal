import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from database import get_db
import models
from config import settings

load_dotenv()

SECRET_KEY = settings.JWT_SECRET_KEY
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24
PASSWORD_LOGIN_LIMIT = 10
PASSWORD_LOGIN_WINDOW_SECONDS = 15 * 60

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login-password")


def hash_password(password: str) -> str:
    if len(password.encode("utf-8")) > 1024:
        raise ValueError("Password must be 1024 bytes or fewer.")
    prehashed = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return "sha256$" + pwd_context.hash(prehashed)


get_password_hash = hash_password


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        if len(plain.encode("utf-8")) > 1024:
            return False
        if hashed.startswith("sha256$"):
            prehashed = hashlib.sha256(plain.encode("utf-8")).hexdigest()
            return pwd_context.verify(prehashed, hashed.removeprefix("sha256$"))
        legacy_password = plain.encode("utf-8")[:72].decode("utf-8", errors="ignore")
        return pwd_context.verify(legacy_password, hashed)
    except Exception:
        # Prevents 500 crash if database contains an unrecognized or malformed hash
        return False


def is_legacy_password_hash(hashed: str) -> bool:
    return not hashed.startswith("sha256$")


def _rate_limit_key(scope: str, identity: str, window_start: int) -> str:
    value = f"{scope}:{identity.casefold()}:{window_start}".encode()
    return hmac.new(settings.OTP_PEPPER.encode(), value, hashlib.sha256).hexdigest()


def record_rate_limit_attempt(
    db: Session,
    scope: str,
    identity: str,
    limit: int,
    window_seconds: int = PASSWORD_LOGIN_WINDOW_SECONDS,
) -> bool:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    now_epoch = int(now.replace(tzinfo=timezone.utc).timestamp())
    window_start = now_epoch - now_epoch % window_seconds
    window_start_at = datetime.fromtimestamp(window_start, timezone.utc).replace(tzinfo=None)
    key = _rate_limit_key(scope, identity, window_start)
    rate_limit = models.AuthRateLimit
    values = {
        "key": key,
        "attempts": 1,
        "window_started_at": window_start_at,
    }

    dialect = db.get_bind().dialect.name
    if now_epoch % 3600 < 10:
        db.query(rate_limit).filter(
            rate_limit.window_started_at < now - timedelta(days=1)
        ).delete(synchronize_session=False)

    if dialect == "sqlite":
        statement = sqlite_insert(rate_limit).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[rate_limit.key],
            set_={"attempts": rate_limit.attempts + 1},
        )
    elif dialect == "mysql":
        statement = mysql_insert(rate_limit).values(**values)
        statement = statement.on_duplicate_key_update(
            attempts=rate_limit.attempts + 1,
        )
    else:
        record = db.get(rate_limit, key)
        if record:
            record.attempts += 1
        else:
            db.add(rate_limit(**values))
        db.commit()
        record = db.get(rate_limit, key)
        return record.attempts <= limit

    db.execute(statement)
    db.commit()
    attempts = db.get(rate_limit, key).attempts
    return attempts <= limit


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    to_encode["exp"] = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> models.User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        raise credentials_exception
    return user


def require_role(allowed_roles):
    if isinstance(allowed_roles, str):
        allowed_roles = [allowed_roles]

    def role_checker(current_user: models.User = Depends(get_current_user)):
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this resource",
            )
        return current_user
    return role_checker