import time, secrets, hashlib, uuid
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from passlib.hash import bcrypt
from comet.utils.models import database, settings, default_config
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi import status

security = HTTPBasic()
admin_api = APIRouter(prefix="/admin")
user_api = APIRouter()

# Pydantic models
class UserCreate(BaseModel):
    username: str
    password: str
    role: str | None = "user"

class UserOut(BaseModel):
    id: str
    username: str
    role: str
    is_active: bool
    created_at: int

class TokenCreate(BaseModel):
    user_id: str
    name: str | None = None
    expires_in_days: int | None = None
    monthly_quota: int | None = None

class TokenOut(BaseModel):
    id: str
    user_id: str
    name: str | None
    is_active: bool
    created_at: int
    expires_at: int | None
    last_used: int | None
    usage_count: int
    monthly_quota: int | None

class ConfigCreate(BaseModel):
    name: str
    config: dict | None = None  # if None use default_config

class ConfigOut(BaseModel):
    id: str
    name: str
    created_at: int
    is_global: bool | None = False

class AssignConfig(BaseModel):
    config_id: str

class UserLogin(BaseModel):
    username: str
    password: str


async def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    # Reuse existing single admin from settings for now
    if credentials.username != settings.DASHBOARD_ADMIN_USERNAME or credentials.password != settings.DASHBOARD_ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return True

# Admin endpoints
@admin_api.post("/users", response_model=UserOut)
async def create_user(payload: UserCreate, _: bool = Depends(verify_admin)):
    # Check if exists
    existing = await database.fetch_val("SELECT 1 FROM users WHERE username=:u", {"u": payload.username})
    if existing:
        raise HTTPException(400, "Username already exists")
    uid = str(uuid.uuid4())
    now = int(time.time())
    pwd_hash = bcrypt.hash(payload.password)
    await database.execute("INSERT INTO users (id, username, password_hash, role, is_active, created_at) VALUES (:id,:u,:p,:r,1,:c)", {"id": uid, "u": payload.username, "p": pwd_hash, "r": payload.role or "user", "c": now})
    row = {"id": uid, "username": payload.username, "role": payload.role or "user", "is_active": True, "created_at": now}
    return row

@admin_api.get("/users", response_model=list[UserOut])
async def list_users(_: bool = Depends(verify_admin)):
    rows = await database.fetch_all("SELECT id, username, role, is_active, created_at FROM users ORDER BY created_at DESC")
    return [dict(r) | {"is_active": bool(r["is_active"])} for r in rows]

class UserUpdate(BaseModel):
    password: str | None = None
    role: str | None = None
    is_active: bool | None = None

@admin_api.patch("/users/{user_id}", response_model=UserOut)
async def update_user(user_id: str, payload: UserUpdate, _: bool = Depends(verify_admin)):
    row = await database.fetch_one("SELECT * FROM users WHERE id=:id", {"id": user_id})
    if not row:
        raise HTTPException(404, "User not found")
    updates = []
    params = {"id": user_id}
    if payload.password:
        updates.append("password_hash=:ph")
        params["ph"] = bcrypt.hash(payload.password)
    if payload.role:
        updates.append("role=:r")
        params["r"] = payload.role
    if payload.is_active is not None:
        updates.append("is_active=:ia")
        params["ia"] = 1 if payload.is_active else 0
    if updates:
        await database.execute(f"UPDATE users SET {', '.join(updates)} WHERE id=:id", params)
    new_row = await database.fetch_one("SELECT id, username, role, is_active, created_at FROM users WHERE id=:id", {"id": user_id})
    return dict(new_row) | {"is_active": bool(new_row["is_active"])}

@admin_api.post("/tokens", response_model=TokenOut)
async def create_token(payload: TokenCreate, _: bool = Depends(verify_admin)):
    user = await database.fetch_one("SELECT id FROM users WHERE id=:id", {"id": payload.user_id})
    if not user:
        raise HTTPException(404, "User not found")
    token_raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token_raw.encode()).hexdigest()
    tid = str(uuid.uuid4())
    now = int(time.time())
    expires_at = None
    if payload.expires_in_days:
        expires_at = now + payload.expires_in_days * 86400
    await database.execute("""
        INSERT INTO api_tokens (id,user_id,token_hash,name,is_active,created_at,expires_at,usage_count,monthly_quota)
        VALUES (:id,:uid,:th,:name,1,:c,:exp,0,:mq)
    """, {"id": tid, "uid": payload.user_id, "th": token_hash, "name": payload.name, "c": now, "exp": expires_at, "mq": payload.monthly_quota})
    # Return token meta; include one-time raw token
    return TokenOut(id=tid, user_id=payload.user_id, name=payload.name, is_active=True, created_at=now, expires_at=expires_at, last_used=None, usage_count=0, monthly_quota=payload.monthly_quota)

@admin_api.get("/tokens", response_model=list[TokenOut])
async def list_tokens(_: bool = Depends(verify_admin)):
    rows = await database.fetch_all("SELECT * FROM api_tokens ORDER BY created_at DESC")
    out = []
    for r in rows:
        out.append(TokenOut(
            id=r["id"], user_id=r["user_id"], name=r["name"], is_active=bool(r["is_active"]),
            created_at=r["created_at"], expires_at=r["expires_at"], last_used=r["last_used"], usage_count=r["usage_count"], monthly_quota=r["monthly_quota"]
        ))
    return out

class TokenUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    expires_at: int | None = None
    monthly_quota: int | None = None

@admin_api.patch("/tokens/{token_id}", response_model=TokenOut)
async def update_token(token_id: str, payload: TokenUpdate, _: bool = Depends(verify_admin)):
    row = await database.fetch_one("SELECT * FROM api_tokens WHERE id=:id", {"id": token_id})
    if not row: raise HTTPException(404, "Token not found")
    updates=[]; params={"id": token_id}
    if payload.name is not None: updates.append("name=:n"); params["n"]=payload.name
    if payload.is_active is not None: updates.append("is_active=:ia"); params["ia"]=1 if payload.is_active else 0
    if payload.expires_at is not None: updates.append("expires_at=:ea"); params["ea"]=payload.expires_at
    if payload.monthly_quota is not None: updates.append("monthly_quota=:mq"); params["mq"]=payload.monthly_quota
    if updates:
        await database.execute(f"UPDATE api_tokens SET {', '.join(updates)} WHERE id=:id", params)
    new = await database.fetch_one("SELECT * FROM api_tokens WHERE id=:id", {"id": token_id})
    return TokenOut(id=new["id"], user_id=new["user_id"], name=new["name"], is_active=bool(new["is_active"]), created_at=new["created_at"], expires_at=new["expires_at"], last_used=new["last_used"], usage_count=new["usage_count"], monthly_quota=new["monthly_quota"])    

# Config management
@admin_api.post("/configs", response_model=ConfigOut)
async def create_config(payload: ConfigCreate, _: bool = Depends(verify_admin)):
    existing = await database.fetch_val("SELECT 1 FROM configs WHERE name=:n", {"n": payload.name})
    if existing:
        raise HTTPException(400, "Config name exists")
    cid = str(uuid.uuid4())
    now = int(time.time())
    import orjson
    cfg = payload.config or default_config
    await database.execute("INSERT INTO configs (id,name,config_json,created_at) VALUES (:id,:n,:c,:t)", {"id": cid, "n": payload.name, "c": orjson.dumps(cfg).decode(), "t": now})
    return ConfigOut(id=cid, name=payload.name, created_at=now)

@admin_api.get("/configs", response_model=list[ConfigOut])
async def list_configs(_: bool = Depends(verify_admin)):
    rows = await database.fetch_all("SELECT id,name,created_at FROM configs ORDER BY created_at DESC")
    return [ConfigOut(id=r["id"], name=r["name"], created_at=r["created_at"]) for r in rows]

@admin_api.post("/users/{user_id}/assign_config", response_model=UserOut)
async def assign_config(user_id: str, payload: AssignConfig, _: bool = Depends(verify_admin)):
    cfg = await database.fetch_one("SELECT id FROM configs WHERE id=:id", {"id": payload.config_id})
    if not cfg:
        raise HTTPException(404, "Config not found")
    await database.execute("UPDATE users SET current_config_id=:cid WHERE id=:uid", {"cid": payload.config_id, "uid": user_id})
    new = await database.fetch_one("SELECT id,username,role,is_active,created_at FROM users WHERE id=:id", {"id": user_id})
    return {"id": new["id"], "username": new["username"], "role": new["role"], "is_active": bool(new["is_active"]), "created_at": new["created_at"]}

# Token auth dependency for user dashboard (legacy / API based)
async def get_token_from_request(request: Request):
    api_key = request.headers.get("X-API-Key") or request.query_params.get("apikey")
    if not api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing API key")
    h = hashlib.sha256(api_key.encode()).hexdigest()
    tok = await database.fetch_one("SELECT * FROM api_tokens WHERE token_hash=:h", {"h": h})
    if not tok or not tok["is_active"]:
        raise HTTPException(403, "Invalid token")
    now = int(time.time())
    if tok["expires_at"] and tok["expires_at"] < now:
        raise HTTPException(403, "Token expired")
    # quota check
    if tok["monthly_quota"] and tok["usage_count"] >= tok["monthly_quota"]:
        raise HTTPException(403, "Quota exceeded")
    await database.execute("UPDATE api_tokens SET usage_count=usage_count+1, last_used=:n WHERE id=:id", {"n": now, "id": tok["id"]})
    return tok

# Simple user-facing dashboard endpoint (shows their config link)
@user_api.get("/dashboard")
async def dashboard_page(request: Request):
    # Serve user dashboard HTML
    from fastapi.responses import HTMLResponse
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates("comet/templates")
    # simple template - uses JS to fetch session data
    return templates.TemplateResponse("dashboard.html", {"request": request})

@user_api.get("/dashboard/data")
async def user_dashboard_data(request: Request):
    # Session cookie auth
    session_token = request.cookies.get("session")
    if not session_token:
        raise HTTPException(401, "Not logged in")
    sess = await database.fetch_one("SELECT user_id, expires_at FROM sessions WHERE token=:t", {"t": session_token})
    now = int(time.time())
    if not sess or sess["expires_at"] < now:
        raise HTTPException(401, "Session expired")
    user_id = sess["user_id"]
    user = await database.fetch_one("SELECT current_config_id FROM users WHERE id=:id", {"id": user_id})
    import base64, orjson
    cfg_json = None
    if user and user["current_config_id"]:
        row = await database.fetch_one("SELECT config_json FROM configs WHERE id=:id", {"id": user["current_config_id"]})
        if row:
            cfg_json = row["config_json"]
    cfg_bytes = cfg_json.encode() if cfg_json else orjson.dumps(default_config)
    b64 = base64.b64encode(cfg_bytes).decode()
    base_url = settings.COMET_URL.rstrip('/') if settings.COMET_URL else ''
    manifest_link = f"{base_url}/{b64}/manifest.json"
    # tokens list
    tokens = await database.fetch_all("SELECT id,name,is_active,usage_count,last_used FROM api_tokens WHERE user_id=:u ORDER BY created_at DESC", {"u": user_id})
    token_list = []
    for t in tokens:
        token_list.append({"id": t["id"], "name": t["name"], "is_active": bool(t["is_active"]), "usage_count": t["usage_count"], "last_used": t["last_used"]})
    return {"config_url": manifest_link, "tokens": token_list}

@user_api.post("/login")
async def user_login(payload: UserLogin, response: Response):
    user = await database.fetch_one("SELECT id,password_hash,is_active FROM users WHERE username=:u", {"u": payload.username})
    if not user or not bool(user["is_active"]):
        raise HTTPException(401, "Invalid credentials")
    if not bcrypt.verify(payload.password, user["password_hash"]):
        raise HTTPException(401, "Invalid credentials")
    # issue session
    sid = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    expires = now + 86400 * 7  # 7 days
    await database.execute("INSERT INTO sessions (id,user_id,token,created_at,expires_at) VALUES (:i,:u,:t,:c,:e)", {"i": sid, "u": user["id"], "t": token, "c": now, "e": expires})
    response.set_cookie("session", token, max_age=86400*7, httponly=True, samesite="Lax")
    return {"status": "ok"}

@user_api.post("/logout")
async def user_logout(request: Request, response: Response):
    token = request.cookies.get("session")
    if token:
        await database.execute("DELETE FROM sessions WHERE token=:t", {"t": token})
    response.delete_cookie("session")
    return {"status": "ok"}

@user_api.get("/api/dashboard")
async def user_dashboard(token = Depends(get_token_from_request)):
    # Use token's user current_config if set else default
    user = await database.fetch_one("SELECT current_config_id FROM users WHERE id=(SELECT user_id FROM api_tokens WHERE id=:tid)", {"tid": token["id"]})
    import base64, orjson
    cfg_json = None
    if user and user["current_config_id"]:
        row = await database.fetch_one("SELECT config_json FROM configs WHERE id=:id", {"id": user["current_config_id"]})
        if row:
            cfg_json = row["config_json"]
    if cfg_json:
        cfg_bytes = cfg_json.encode()
    else:
        cfg_bytes = orjson.dumps(default_config)
    b64 = base64.b64encode(cfg_bytes).decode()
    base_url = settings.COMET_URL.rstrip('/') if settings.COMET_URL else ''
    manifest_link = f"{base_url}/{b64}/manifest.json"
    return {"token_id": token["id"], "config_url": manifest_link, "has_custom_config": bool(cfg_json)}
