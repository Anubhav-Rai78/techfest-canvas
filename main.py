import os, random, string

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# Allow your frontend to talk to this backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)

# Connect to Supabase
supabase = create_client(
    os.getenv('SUPABASE_URL'),
    os.getenv('SUPABASE_KEY')
)

# ── Generate a unique code like PXF-A8K3M2 ──
def generate_code():
    chars = string.ascii_uppercase + string.digits
    random_part = ''.join(random.choices(chars, k=6))
    return f'PXF-{random_part}'

# ── Data models ──
class RegisterRequest(BaseModel):
    name: str
    phone: str
    college: str

class LoginRequest(BaseModel):
    fest_code: str

# ── REGISTRATION endpoint ──
@app.post('/register')
async def register(data: RegisterRequest):
    existing = supabase.table('participants') \
        .select('fest_code') \
        .eq('phone', data.phone) \
        .execute()

    if existing.data:
        return {
            'status': 'already_registered',
            'fest_code': existing.data[0]['fest_code'],
            'message': 'You are already registered!'
        }

    for _ in range(10):
        code = generate_code()
        check = supabase.table('participants') \
            .select('id') \
            .eq('fest_code', code) \
            .execute()
        if not check.data:
            break

    supabase.table('participants').insert({
        'name': data.name,
        'phone': data.phone,
        'college': data.college,
        'fest_code': code
    }).execute()

    return {
        'status': 'success',
        'fest_code': code,
        'message': f'Welcome {data.name}! Your Fest ID is {code}'
    }

# ── LOGIN endpoint ──
@app.post('/login')
async def login(data: LoginRequest):
    result = supabase.table('participants') \
        .select('name, college') \
        .eq('fest_code', data.fest_code.upper().strip()) \
        .execute()

    if not result.data:
        raise HTTPException(status_code=401, detail='Invalid Fest ID')

    user = result.data[0]
    return {
        'status': 'success',
        'name': user['name'],
        'college': user['college'],
        'fest_code': data.fest_code.upper().strip()
    }

# ── HEALTH CHECK (keeps Render awake) ──
@app.get('/health')
async def health():
    return {'status': 'ok'}
