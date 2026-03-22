import os, random, string, re, asyncio, httpx, math
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime, timezone

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

# ── Self-ping to keep Render awake ──
async def keep_alive():
    await asyncio.sleep(60)
    while True:
        try:
            async with httpx.AsyncClient() as client:
                await client.get('https://techfest-canvas.onrender.com/health', timeout=10)
        except Exception:
            pass
        await asyncio.sleep(600)

@app.on_event('startup')
async def startup_event():
    asyncio.create_task(keep_alive())

# ── Canvas formula ──
def calculate_canvas(n: int):
    n = max(1, min(100, n))
    ratio    = math.sqrt(n / 100)
    size     = max(10, math.floor(200 * ratio))
    cooldown = max(5, round(30 / ratio))
    return size, cooldown

def generate_code():
    chars = string.ascii_uppercase + string.digits
    return 'PXF-' + ''.join(random.choices(chars, k=6))

def generate_room_id():
    chars = string.ascii_uppercase + string.digits
    return 'RM-' + ''.join(random.choices(chars, k=6))

def get_admin_password():
    setting = supabase.table('admin_settings').select('value').eq('key', 'admin_password').execute()
    if not setting.data:
        raise HTTPException(status_code=500, detail='Admin settings not found')
    return setting.data[0]['value']

def verify_admin(password: str):
    if password != get_admin_password():
        raise HTTPException(status_code=403, detail='Invalid admin password')

# ── Models ──
class RegisterRequest(BaseModel):
    name: str
    phone: str
    college: str

    @field_validator('name')
    @classmethod
    def validate_name(cls, v):
        v = v.strip()
        if len(v) < 2: raise ValueError('Name must be at least 2 characters')
        if len(v) > 60: raise ValueError('Name is too long')
        if not re.match(r"^[A-Za-z\s\.\'\-]+$", v): raise ValueError('Name can only contain letters and spaces')
        return v

    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v):
        v = v.strip().replace(' ', '').replace('-', '').replace('+91', '')
        if not v.isdigit(): raise ValueError('Phone number must contain only digits')
        if len(v) != 10: raise ValueError('Phone number must be exactly 10 digits')
        if v[0] not in '6789': raise ValueError('Phone number must start with 6, 7, 8, or 9')
        return v

    @field_validator('college')
    @classmethod
    def validate_college(cls, v):
        v = v.strip()
        if len(v) < 2: raise ValueError('College name must be at least 2 characters')
        if len(v) > 100: raise ValueError('College name is too long')
        return v

class LoginRequest(BaseModel):
    fest_code: str
    room_id: str = None

    @field_validator('fest_code')
    @classmethod
    def validate_fest_code(cls, v):
        v = v.strip().upper()
        if not re.match(r'^PXF-[A-Z0-9]{6}$', v): raise ValueError('Invalid Fest ID format')
        return v

class PlacePixelRequest(BaseModel):
    fest_code: str
    x: int
    y: int
    color: str
    room_id: str = None

    @field_validator('color')
    @classmethod
    def validate_color(cls, v):
        if not re.match(r'^#[0-9a-fA-F]{6}$', v): raise ValueError('Invalid color format')
        return v.lower()

class AdminRequest(BaseModel):
    password: str

class AdminSettingRequest(BaseModel):
    password: str
    key: str
    value: str

class CreateRoomRequest(BaseModel):
    password: str
    name: str
    user_count: int = 100

class RoomActionRequest(BaseModel):
    password: str
    room_id: str

class ApplyFormulaRequest(BaseModel):
    password: str
    user_count: int
    room_id: str = None  # None = global canvas

class GlobalConfigRequest(BaseModel):
    password: str
    canvas_width: int
    canvas_height: int
    cooldown_seconds: int

# ── REGISTRATION ──
@app.post('/register')
async def register(data: RegisterRequest):
    phone_clean = data.phone.strip().replace(' ', '').replace('-', '').replace('+91', '')
    existing = supabase.table('participants').select('fest_code, name').eq('phone', phone_clean).execute()
    if existing.data:
        user = existing.data[0]
        return { 'status': 'already_registered', 'fest_code': user['fest_code'], 'name': user['name'], 'message': f'This phone number is already registered under the name "{user["name"]}".' }
    for _ in range(10):
        code = generate_code()
        check = supabase.table('participants').select('id').eq('fest_code', code).execute()
        if not check.data: break
    supabase.table('participants').insert({ 'name': data.name.strip(), 'phone': phone_clean, 'college': data.college.strip(), 'fest_code': code }).execute()
    return { 'status': 'success', 'fest_code': code, 'name': data.name.strip(), 'message': f'Welcome {data.name.strip()}! Your Fest ID is {code}' }

# ── LOGIN ──
@app.post('/login')
async def login(data: LoginRequest):
    result = supabase.table('participants').select('name, college, last_pixel_at').eq('fest_code', data.fest_code).execute()
    if not result.data: raise HTTPException(status_code=401, detail='Invalid Fest ID')
    user = result.data[0]

    # If room_id provided, check room exists and join if not already joined
    room_data = None
    if data.room_id:
        room = supabase.table('rooms').select('*').eq('id', data.room_id).eq('active', True).execute()
        if not room.data: raise HTTPException(status_code=404, detail='Room not found or inactive')
        room_data = room.data[0]
        # Auto-join room
        existing = supabase.table('room_participants').select('id, last_pixel_at').eq('room_id', data.room_id).eq('fest_code', data.fest_code).execute()
        if not existing.data:
            supabase.table('room_participants').insert({ 'room_id': data.room_id, 'fest_code': data.fest_code }).execute()
            last_pixel_at = None
        else:
            last_pixel_at = existing.data[0].get('last_pixel_at')
    else:
        last_pixel_at = user.get('last_pixel_at')

    # Get global config
    config = supabase.table('admin_settings').select('key,value').in_('key', ['canvas_width','canvas_height','cooldown_seconds']).execute()
    config_map = {c['key']: c['value'] for c in (config.data or [])}

    return {
        'status': 'success',
        'name': user['name'],
        'college': user['college'],
        'fest_code': data.fest_code,
        'last_pixel_at': last_pixel_at,
        'room': room_data,
        'canvas_width': int(config_map.get('canvas_width', 200)),
        'canvas_height': int(config_map.get('canvas_height', 200)),
        'cooldown_seconds': int(config_map.get('cooldown_seconds', 30)),
    }

# ── PLACE PIXEL ──
@app.post('/place')
async def place_pixel(data: PlacePixelRequest):
    now = datetime.now(timezone.utc)

    # Get canvas config
    config = supabase.table('admin_settings').select('key,value').in_('key', ['canvas_width','canvas_height','cooldown_seconds']).execute()
    config_map = {c['key']: c['value'] for c in (config.data or [])}
    canvas_w   = int(config_map.get('canvas_width', 200))
    canvas_h   = int(config_map.get('canvas_height', 200))
    cooldown_s = int(config_map.get('cooldown_seconds', 30))

    if data.x < 0 or data.x >= canvas_w or data.y < 0 or data.y >= canvas_h:
        raise HTTPException(status_code=400, detail=f'Coordinates out of bounds ({canvas_w}×{canvas_h})')

    if data.room_id:
        # Room mode
        room = supabase.table('rooms').select('cooldown_seconds').eq('id', data.room_id).execute()
        if room.data: cooldown_s = room.data[0]['cooldown_seconds']
        participant = supabase.table('room_participants').select('last_pixel_at, pixel_count').eq('room_id', data.room_id).eq('fest_code', data.fest_code).execute()
        if not participant.data: raise HTTPException(status_code=401, detail='Not a member of this room')
        last_pixel_at = participant.data[0].get('last_pixel_at')
        pixel_count   = participant.data[0].get('pixel_count') or 0
    else:
        # Global mode
        result = supabase.table('participants').select('last_pixel_at, pixel_count').eq('fest_code', data.fest_code).execute()
        if not result.data: raise HTTPException(status_code=401, detail='Invalid Fest ID')
        last_pixel_at = result.data[0].get('last_pixel_at')
        pixel_count   = result.data[0].get('pixel_count') or 0

    # Enforce cooldown
    if last_pixel_at:
        last    = datetime.fromisoformat(last_pixel_at.replace('Z', '+00:00'))
        if last.tzinfo is None: last = last.replace(tzinfo=timezone.utc)
        elapsed = (now - last).total_seconds()
        if elapsed < cooldown_s:
            remaining = cooldown_s - elapsed
            raise HTTPException(status_code=429, detail=f'Cooldown active. Wait {int(remaining)+1} seconds.')

    # Place pixel
    if data.room_id:
        supabase.table('room_pixels').upsert(
            { 'room_id': data.room_id, 'x': data.x, 'y': data.y, 'color': data.color, 'placed_by': data.fest_code },
            on_conflict='room_id,x,y'
        ).execute()
        supabase.table('room_participants').update({ 'last_pixel_at': now.isoformat(), 'pixel_count': pixel_count + 1 }).eq('room_id', data.room_id).eq('fest_code', data.fest_code).execute()
    else:
        supabase.table('pixels').upsert(
            { 'x': data.x, 'y': data.y, 'color': data.color, 'placed_by': data.fest_code },
            on_conflict='x,y'
        ).execute()
        supabase.table('participants').update({ 'last_pixel_at': now.isoformat(), 'pixel_count': pixel_count + 1 }).eq('fest_code', data.fest_code).execute()

    return { 'status': 'success', 'x': data.x, 'y': data.y, 'color': data.color }

# ── ADMIN: STATS ──
@app.post('/admin/stats')
async def get_stats(data: AdminRequest):
    verify_admin(data.password)
    participants = supabase.table('participants').select('id', count='exact').execute()
    pixels       = supabase.table('pixels').select('id', count='exact').execute()
    rooms        = supabase.table('rooms').select('*').eq('active', True).execute()
    config       = supabase.table('admin_settings').select('key,value').in_('key', ['canvas_width','canvas_height','cooldown_seconds']).execute()
    config_map   = {c['key']: c['value'] for c in (config.data or [])}
    return {
        'status': 'success',
        'total_participants': participants.count,
        'total_pixels': pixels.count,
        'active_rooms': len(rooms.data) if rooms.data else 0,
        'canvas_width': int(config_map.get('canvas_width', 200)),
        'canvas_height': int(config_map.get('canvas_height', 200)),
        'cooldown_seconds': int(config_map.get('cooldown_seconds', 30)),
    }

# ── ADMIN: CLEAR CANVAS ──
@app.post('/admin/clear-canvas')
async def clear_canvas(data: AdminRequest):
    verify_admin(data.password)
    supabase.table('pixels').delete().neq('id', 0).execute()
    supabase.table('participants').update({'pixel_count': 0}).neq('id', 0).execute()
    # Trigger live reload for all users
    supabase.table('admin_settings').update({'value': 'clear', 'updated_at': datetime.now(timezone.utc).isoformat()}).eq('key', 'last_event').execute()
    return { 'status': 'success', 'message': 'Canvas cleared!' }

# ── ADMIN: APPLY FORMULA ──
@app.post('/admin/apply-formula')
async def apply_formula(data: ApplyFormulaRequest):
    verify_admin(data.password)
    n = max(1, min(100, data.user_count))
    size, cooldown = calculate_canvas(n)

    if data.room_id:
        supabase.table('rooms').update({
            'canvas_width': size, 'canvas_height': size,
            'cooldown_seconds': cooldown, 'user_count': n
        }).eq('id', data.room_id).execute()
    else:
        # Update global config
        for key, val in [('canvas_width', str(size)), ('canvas_height', str(size)), ('cooldown_seconds', str(cooldown))]:
            supabase.table('admin_settings').update({'value': val, 'updated_at': datetime.now(timezone.utc).isoformat()}).eq('key', key).execute()
        # Trigger reload for all users
        _trigger_reload()

    return { 'status': 'success', 'canvas_size': size, 'cooldown': cooldown, 'user_count': n }

# ── ADMIN: SET GLOBAL CONFIG MANUALLY ──
@app.post('/admin/set-config')
async def set_config(data: GlobalConfigRequest):
    verify_admin(data.password)
    w = max(10, min(200, data.canvas_width))
    h = max(10, min(200, data.canvas_height))
    c = max(5, min(300, data.cooldown_seconds))
    for key, val in [('canvas_width', str(w)), ('canvas_height', str(h)), ('cooldown_seconds', str(c))]:
        supabase.table('admin_settings').update({'value': val, 'updated_at': datetime.now(timezone.utc).isoformat()}).eq('key', key).execute()
    _trigger_reload()
    return { 'status': 'success', 'canvas_width': w, 'canvas_height': h, 'cooldown_seconds': c }

# ── ADMIN: SETTING ──
@app.post('/admin/setting')
async def update_setting(data: AdminSettingRequest):
    verify_admin(data.password)
    supabase.table('admin_settings').update({'value': data.value, 'updated_at': datetime.now(timezone.utc).isoformat()}).eq('key', data.key).execute()
    return { 'status': 'success' }

# ── ADMIN: CREATE ROOM ──
@app.post('/admin/create-room')
async def create_room(data: CreateRoomRequest):
    verify_admin(data.password)
    n = max(1, min(100, data.user_count))
    size, cooldown = calculate_canvas(n)
    room_id = generate_room_id()
    supabase.table('rooms').insert({
        'id': room_id, 'name': data.name.strip(),
        'created_by': 'admin',
        'canvas_width': size, 'canvas_height': size,
        'cooldown_seconds': cooldown, 'user_count': n,
        'active': True
    }).execute()
    # Add last_event row if not exists
    try:
        supabase.table('admin_settings').insert({'key': f'room_{room_id}_event', 'value': 'created'}).execute()
    except Exception:
        pass
    return { 'status': 'success', 'room_id': room_id, 'canvas_size': size, 'cooldown': cooldown }

# ── ADMIN: CLOSE ROOM ──
@app.post('/admin/close-room')
async def close_room(data: RoomActionRequest):
    verify_admin(data.password)
    supabase.table('rooms').update({'active': False}).eq('id', data.room_id).execute()
    return { 'status': 'success' }

# ── ADMIN: CLEAR ROOM CANVAS ──
@app.post('/admin/clear-room')
async def clear_room(data: RoomActionRequest):
    verify_admin(data.password)
    supabase.table('room_pixels').delete().eq('room_id', data.room_id).execute()
    supabase.table('room_participants').update({'pixel_count': 0}).eq('room_id', data.room_id).execute()
    return { 'status': 'success' }

# ── ADMIN: LIST ROOMS ──
@app.post('/admin/rooms')
async def list_rooms(data: AdminRequest):
    verify_admin(data.password)
    rooms = supabase.table('rooms').select('*').order('created_at', desc=True).execute()
    return { 'status': 'success', 'rooms': rooms.data or [] }

def _trigger_reload():
    try:
        existing = supabase.table('admin_settings').select('key').eq('key', 'last_event').execute()
        if existing.data:
            supabase.table('admin_settings').update({'value': 'reload', 'updated_at': datetime.now(timezone.utc).isoformat()}).eq('key', 'last_event').execute()
        else:
            supabase.table('admin_settings').insert({'key': 'last_event', 'value': 'reload'}).execute()
    except Exception:
        pass

# ── HEALTH ──
@app.get('/health')
async def health():
    return { 'status': 'ok' }
