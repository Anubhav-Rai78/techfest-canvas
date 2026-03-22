import os, random, string, re, asyncio, httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)

supabase = create_client(
    os.getenv('SUPABASE_URL'),
    os.getenv('SUPABASE_KEY')
)

COOLDOWN_SECONDS = 30

# ── Self-ping to keep Render awake ──
async def keep_alive():
    await asyncio.sleep(60)  # wait 1 min after startup
    while True:
        try:
            async with httpx.AsyncClient() as client:
                await client.get('https://techfest-canvas.onrender.com/health', timeout=10)
        except Exception:
            pass
        await asyncio.sleep(600)  # ping every 10 minutes

@app.on_event('startup')
async def startup_event():
    asyncio.create_task(keep_alive())

def generate_code():
    chars = string.ascii_uppercase + string.digits
    return 'PXF-' + ''.join(random.choices(chars, k=6))

class RegisterRequest(BaseModel):
    name: str
    phone: str
    college: str

    @field_validator('name')
    @classmethod
    def validate_name(cls, v):
        v = v.strip()
        if len(v) < 2:
            raise ValueError('Name must be at least 2 characters')
        if len(v) > 60:
            raise ValueError('Name is too long')
        if not re.match(r"^[A-Za-z\s\.\'\-]+$", v):
            raise ValueError('Name can only contain letters, spaces, dots, apostrophes, and hyphens')
        return v

    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v):
        v = v.strip().replace(' ', '').replace('-', '').replace('+91', '')
        if not v.isdigit():
            raise ValueError('Phone number must contain only digits')
        if len(v) != 10:
            raise ValueError('Phone number must be exactly 10 digits')
        if v[0] not in '6789':
            raise ValueError('Phone number must start with 6, 7, 8, or 9')
        return v

    @field_validator('college')
    @classmethod
    def validate_college(cls, v):
        v = v.strip()
        if len(v) < 2:
            raise ValueError('College name must be at least 2 characters')
        if len(v) > 100:
            raise ValueError('College name is too long')
        if not re.match(r"^[A-Za-z0-9\s\.\,\'\-\(\)&]+$", v):
            raise ValueError('College name contains invalid characters')
        return v

class LoginRequest(BaseModel):
    fest_code: str

    @field_validator('fest_code')
    @classmethod
    def validate_fest_code(cls, v):
        v = v.strip().upper()
        if not re.match(r'^PXF-[A-Z0-9]{6}$', v):
            raise ValueError('Invalid Fest ID format')
        return v

class PlacePixelRequest(BaseModel):
    fest_code: str
    x: int
    y: int
    color: str

    @field_validator('color')
    @classmethod
    def validate_color(cls, v):
        if not re.match(r'^#[0-9a-fA-F]{6}$', v):
            raise ValueError('Invalid color format')
        return v.lower()

    @field_validator('x')
    @classmethod
    def validate_x(cls, v):
        if v < 0 or v >= 200:
            raise ValueError('x must be between 0 and 199')
        return v

    @field_validator('y')
    @classmethod
    def validate_y(cls, v):
        if v < 0 or v >= 200:
            raise ValueError('y must be between 0 and 199')
        return v

class AdminRequest(BaseModel):
    password: str

class AdminSettingRequest(BaseModel):
    password: str
    key: str
    value: str

# ── REGISTRATION ──
@app.post('/register')
async def register(data: RegisterRequest):
    # Normalize for comparison
    phone_clean = data.phone.strip().replace(' ', '').replace('-', '').replace('+91', '')
    name_clean  = data.name.strip().lower()

    # Check by phone number first (unique identifier)
    existing_phone = supabase.table('participants') \
        .select('fest_code, name, phone') \
        .eq('phone', phone_clean) \
        .execute()

    if existing_phone.data:
        user = existing_phone.data[0]
        return {
            'status': 'already_registered',
            'fest_code': user['fest_code'],
            'name': user['name'],
            'message': f'This phone number is already registered under the name "{user["name"]}".'
        }

    # Check by name + phone combo (case insensitive name)
    existing_name = supabase.table('participants') \
        .select('fest_code, name, phone') \
        .ilike('name', name_clean) \
        .eq('phone', phone_clean) \
        .execute()

    if existing_name.data:
        user = existing_name.data[0]
        return {
            'status': 'already_registered',
            'fest_code': user['fest_code'],
            'name': user['name'],
            'message': f'You are already registered!'
        }

    # Generate unique code
    for _ in range(10):
        code = generate_code()
        check = supabase.table('participants') \
            .select('id') \
            .eq('fest_code', code) \
            .execute()
        if not check.data:
            break

    # Save to database
    supabase.table('participants').insert({
        'name': data.name.strip(),
        'phone': phone_clean,
        'college': data.college.strip(),
        'fest_code': code
    }).execute()

    return {
        'status': 'success',
        'fest_code': code,
        'name': data.name.strip(),
        'message': f'Welcome {data.name.strip()}! Your Fest ID is {code}'
    }

# ── LOGIN ──
@app.post('/login')
async def login(data: LoginRequest):
    result = supabase.table('participants') \
        .select('name, college, last_pixel_at') \
        .eq('fest_code', data.fest_code) \
        .execute()

    if not result.data:
        raise HTTPException(status_code=401, detail='Invalid Fest ID')

    user = result.data[0]
    return {
        'status': 'success',
        'name': user['name'],
        'college': user['college'],
        'fest_code': data.fest_code,
        'last_pixel_at': user.get('last_pixel_at')
    }

# ── PLACE PIXEL ──
@app.post('/place')
async def place_pixel(data: PlacePixelRequest):
    from datetime import datetime, timezone

    # Validate fest code
    result = supabase.table('participants') \
        .select('name, last_pixel_at') \
        .eq('fest_code', data.fest_code) \
        .execute()

    if not result.data:
        raise HTTPException(status_code=401, detail='Invalid Fest ID')

    user = result.data[0]
    now  = datetime.now(timezone.utc)

    # Enforce cooldown
    if user['last_pixel_at']:
        last    = datetime.fromisoformat(user['last_pixel_at'].replace('Z', '+00:00'))
        elapsed = (now - last).total_seconds()
        if elapsed < COOLDOWN_SECONDS:
            remaining = COOLDOWN_SECONDS - elapsed
            raise HTTPException(
                status_code=429,
                detail=f'Cooldown active. Wait {int(remaining)+1} seconds.'
            )

    # Place pixel
    supabase.table('pixels').upsert(
        { 'x': data.x, 'y': data.y, 'color': data.color, 'placed_by': data.fest_code },
        on_conflict='x,y'
    ).execute()

    # Update cooldown timestamp and increment pixel count
    supabase.table('participants') \
        .update({ 'last_pixel_at': now.isoformat() }) \
        .eq('fest_code', data.fest_code) \
        .execute()

    # Increment pixel count
    current = supabase.table('participants').select('pixel_count').eq('fest_code', data.fest_code).execute()
    if current.data:
        new_count = (current.data[0].get('pixel_count') or 0) + 1
        supabase.table('participants').update({'pixel_count': new_count}).eq('fest_code', data.fest_code).execute()

    return { 'status': 'success', 'x': data.x, 'y': data.y, 'color': data.color }

# ── ADMIN: CLEAR CANVAS ──
@app.post('/admin/clear-canvas')
async def clear_canvas(data: AdminRequest):
    # Verify admin password
    setting = supabase.table('admin_settings') \
        .select('value') \
        .eq('key', 'admin_password') \
        .execute()

    if not setting.data or setting.data[0]['value'] != data.password:
        raise HTTPException(status_code=403, detail='Invalid admin password')

    supabase.table('pixels').delete().neq('id', 0).execute()
    return { 'status': 'success', 'message': 'Canvas cleared!' }

# ── ADMIN: UPDATE SETTING ──
@app.post('/admin/setting')
async def update_setting(data: AdminSettingRequest):
    setting = supabase.table('admin_settings') \
        .select('value') \
        .eq('key', 'admin_password') \
        .execute()

    if not setting.data or setting.data[0]['value'] != data.password:
        raise HTTPException(status_code=403, detail='Invalid admin password')

    supabase.table('admin_settings') \
        .update({ 'value': data.value }) \
        .eq('key', data.key) \
        .execute()

    return { 'status': 'success' }

# ── ADMIN: GET STATS ──
@app.post('/admin/stats')
async def get_stats(data: AdminRequest):
    setting = supabase.table('admin_settings') \
        .select('value') \
        .eq('key', 'admin_password') \
        .execute()

    if not setting.data or setting.data[0]['value'] != data.password:
        raise HTTPException(status_code=403, detail='Invalid admin password')

    participants = supabase.table('participants').select('id', count='exact').execute()
    pixels       = supabase.table('pixels').select('id', count='exact').execute()

    return {
        'status': 'success',
        'total_participants': participants.count,
        'total_pixels': pixels.count
    }

# ── HEALTH ──
@app.get('/health')
async def health():
    return { 'status': 'ok' }
