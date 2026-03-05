import streamlit as st
import sqlite3
import uuid
import time
import tempfile
import ast
import re
from io import BytesIO

# === IMPORTS CU VERIFICARE ===
try:
    import google.generativeai as genai
    GENAI_AVAILABLE = True
except ImportError as e:
    st.error(f"❌ Nu pot importa google.generativeai: {e}")
    st.error("Verifică că requirements.txt conține: google-generativeai==0.8.3")
    st.stop()

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    st.error("❌ Nu pot importa Pillow")
    st.stop()

# TTS cu fallback
EDGE_TTS_AVAILABLE = False
GTTS_AVAILABLE = False

try:
    import edge_tts
    import asyncio
    EDGE_TTS_AVAILABLE = True
except ImportError:
    st.sidebar.warning("⚠️ edge-tts nu e instalat. Folosesc gTTS.")

try:
    from gtts import gTTS
    GTTS_AVAILABLE = True
except ImportError:
    st.sidebar.warning("⚠️ gTTS nu e instalat. Audio dezactivat.")


# === CONSTANTE ===
MAX_MESSAGES_IN_MEMORY = 100
MAX_MESSAGES_TO_SEND_TO_AI = 20
MAX_MESSAGES_IN_DB_PER_SESSION = 500
CLEANUP_DAYS_OLD = 7

VOICE_MALE_RO = "ro-RO-EmilNeural"
VOICE_FEMALE_RO = "ro-RO-AlinaNeural"


# === CONFIG STREAMLIT ===
st.set_page_config(
    page_title="Profesor Liceu",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .stChatMessage { font-size: 16px; }
    div.stButton > button:first-child { background-color: #ff4b4b; color: white; }
    footer {visibility: hidden;}
    
    .svg-container {
        background-color: white;
        padding: 20px;
        border-radius: 10px;
        border: 1px solid #ddd;
        text-align: center;
        margin: 15px 0;
        overflow: auto;
        box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        max-width: 100%;
    }
    .svg-container svg {
        max-width: 100%;
        height: auto;
    }
</style>
""", unsafe_allow_html=True)


# === DATABASE FUNCTIONS ===
def get_db_connection():
    return sqlite3.connect('chat_history.db', check_same_thread=False)


def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp REAL NOT NULL
        )
    ''')
    
    c.execute('CREATE INDEX IF NOT EXISTS idx_history_session ON history(session_id)')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            last_active REAL NOT NULL
        )
    ''')
    
    conn.commit()
    conn.close()


def cleanup_old_sessions(days_old: int = CLEANUP_DAYS_OLD):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        cutoff_time = time.time() - (days_old * 86400)
        c.execute("DELETE FROM history WHERE timestamp < ?", (cutoff_time,))
        c.execute("DELETE FROM sessions WHERE last_active < ?", (cutoff_time,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Cleanup error: {e}")


def save_message_to_db(session_id, role, content):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO history (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                  (session_id, role, content, time.time()))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB error: {e}")


def load_history_from_db(session_id, limit: int = MAX_MESSAGES_IN_MEMORY):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT role, content FROM (
                SELECT role, content, timestamp FROM history 
                WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?
            ) ORDER BY timestamp ASC
        """, (session_id, limit))
        data = c.fetchall()
        conn.close()
        return [{"role": r[0], "content": r[1]} for r in data]
    except:
        return []


def clear_history_db(session_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM history WHERE session_id=?", (session_id,))
    conn.commit()
    conn.close()


def trim_db_messages(session_id: str):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM history WHERE session_id = ?", (session_id,))
        count = c.fetchone()[0]
        
        if count > MAX_MESSAGES_IN_DB_PER_SESSION:
            to_delete = count - MAX_MESSAGES_IN_DB_PER_SESSION
            c.execute("""
                DELETE FROM history WHERE session_id = ? AND id IN (
                    SELECT id FROM history WHERE session_id = ?
                    ORDER BY timestamp ASC LIMIT ?
                )
            """, (session_id, session_id, to_delete))
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"Trim error: {e}")


# === SESSION MANAGEMENT ===
def generate_unique_session_id() -> str:
    return f"{uuid.uuid4().hex[:16]}{hex(int(time.time() * 1000000))[2:][-8:]}{uuid.uuid4().hex[:8]}"


def session_exists_in_db(session_id: str) -> bool:
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT 1 FROM sessions WHERE session_id = ? LIMIT 1", (session_id,))
        exists = c.fetchone() is not None
        conn.close()
        return exists
    except:
        return False


def register_session(session_id: str):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO sessions (session_id, created_at, last_active) VALUES (?, ?, ?)",
                  (session_id, time.time(), time.time()))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Register error: {e}")


def update_session_activity(session_id: str):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE sessions SET last_active = ? WHERE session_id = ?", (time.time(), session_id))
        conn.commit()
        conn.close()
    except:
        pass


def get_or_create_session_id() -> str:
    if "session_id" in st.query_params:
        existing = st.query_params["session_id"]
        if existing and len(existing) >= 16:
            return existing
    
    if "session_id" in st.session_state:
        return st.session_state.session_id
    
    for _ in range(10):
        new_id = generate_unique_session_id()
        if not session_exists_in_db(new_id):
            register_session(new_id)
            return new_id
    
    fallback = f"{uuid.uuid4().hex}{int(time.time())}"
    register_session(fallback)
    return fallback


# === MEMORY MANAGEMENT ===
def trim_session_messages():
    if "messages" in st.session_state:
        if len(st.session_state.messages) > MAX_MESSAGES_IN_MEMORY:
            excess = len(st.session_state.messages) - MAX_MESSAGES_IN_MEMORY
            st.session_state.messages = st.session_state.messages[excess:]


def get_context_for_ai(messages: list) -> list:
    if len(messages) <= MAX_MESSAGES_TO_SEND_TO_AI:
        return messages[:-1]
    first = messages[0] if messages else None
    recent = messages[-(MAX_MESSAGES_TO_SEND_TO_AI - 1):-1]
    if first and first not in recent:
        return [first] + recent
    return recent


def save_message_with_limits(session_id: str, role: str, content: str):
    save_message_to_db(session_id, role, content)
    if len(st.session_state.get("messages", [])) % 10 == 0:
        trim_db_messages(session_id)
    trim_session_messages()


# === AUDIO FUNCTIONS ===
def clean_text_for_audio(text: str) -> str:
    if not text:
        return ""
    
    text = re.sub(r'\[\[DESEN_SVG\]\].*?\[\[/DESEN_SVG\]\]', ' Am desenat o figură. ', text, flags=re.DOTALL)
    text = re.sub(r'<svg.*?</svg>', ' ', text, flags=re.DOTALL)
    
    replacements = {
        r'\\sqrt\{([^}]+)\}': r'radical din \1',
        r'\\frac\{([^}]+)\}\{([^}]+)\}': r'\1 supra \2',
        r'\^(\d+)': r' la puterea \1',
        r'\\pi': 'pi', r'\\alpha': 'alfa', r'\\beta': 'beta',
        r'\\times': ' ori ', r'\\cdot': ' ori ',
        r'\\leq': ' mai mic sau egal ', r'\\geq': ' mai mare sau egal ',
    }
    
    for pattern, repl in replacements.items():
        text = re.sub(pattern, repl, text)
    
    text = re.sub(r'\$+([^$]+)\$+', r' \1 ', text)
    text = re.sub(r'\\[a-zA-Z]+\{[^}]*\}', '', text)
    text = re.sub(r'\\[a-zA-Z]+', '', text)
    text = re.sub(r'[{}\\]', '', text)
    text = re.sub(r'\*+([^*]+)\*+', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text[:3000]


def generate_professor_voice(text: str, voice: str = VOICE_MALE_RO):
    clean = clean_text_for_audio(text)
    if not clean or len(clean) < 10:
        return None
    
    if EDGE_TTS_AVAILABLE:
        try:
            async def _gen():
                comm = edge_tts.Communicate(clean, voice)
                data = BytesIO()
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        data.write(chunk["data"])
                return data.getvalue()
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            audio_bytes = loop.run_until_complete(_gen())
            loop.close()
            
            if audio_bytes:
                result = BytesIO(audio_bytes)
                result.seek(0)
                return result
        except Exception as e:
            print(f"Edge TTS error: {e}")
    
    if GTTS_AVAILABLE:
        try:
            sound = BytesIO()
            tts = gTTS(text=clean[:2000], lang='ro', slow=False)
            tts.write_to_fp(sound)
            sound.seek(0)
            return sound
        except Exception as e:
            print(f"gTTS error: {e}")
    
    return None


# === SVG FUNCTIONS ===
def repair_svg(svg: str) -> str:
    if not svg:
        return None
    svg = svg.strip()
    
    has_open = '<svg' in svg.lower()
    has_close = '</svg>' in svg.lower()
    
    if not has_open:
        svg = f'<svg viewBox="0 0 800 600" xmlns="http://www.w3.org/2000/svg">{svg}</svg>'
    elif not has_close:
        svg += '</svg>'
    
    if 'xmlns=' not in svg:
        svg = svg.replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    
    return svg


def render_message_with_svg(content: str):
    if '[[DESEN_SVG]]' in content or '<svg' in content.lower():
        svg_code = None
        before = after = ""
        
        if '[[DESEN_SVG]]' in content:
            parts = content.split('[[DESEN_SVG]]')
            before = parts[0]
            if '[[/DESEN_SVG]]' in parts[1]:
                inner = parts[1].split('[[/DESEN_SVG]]')
                svg_code = inner[0]
                after = inner[1] if len(inner) > 1 else ""
        elif '<svg' in content.lower():
            match = re.search(r'<svg.*?</svg>', content, re.DOTALL | re.IGNORECASE)
            if match:
                svg_code = match.group(0)
                before = content[:match.start()]
                after = content[match.end():]
        
        if svg_code:
            svg_code = repair_svg(svg_code)
            if before.strip():
                st.markdown(before.strip())
            st.markdown(f'<div class="svg-container">{svg_code}</div>', unsafe_allow_html=True)
            if after.strip():
                st.markdown(after.strip())
            return
    
    st.markdown(content)


# === INIT ===
init_db()
cleanup_old_sessions(CLEANUP_DAYS_OLD)

session_id = get_or_create_session_id()
st.session_state.session_id = session_id
st.query_params["session_id"] = session_id
update_session_activity(session_id)


# === API KEYS ===
raw_keys = None
if "GOOGLE_API_KEYS" in st.secrets:
    raw_keys = st.secrets["GOOGLE_API_KEYS"]
elif "GOOGLE_API_KEY" in st.secrets:
    raw_keys = [st.secrets["GOOGLE_API_KEY"]]
else:
    k = st.sidebar.text_input("API Key:", type="password")
    raw_keys = [k] if k else []

keys = []
if raw_keys:
    if isinstance(raw_keys, str):
        try:
            raw_keys = ast.literal_eval(raw_keys)
        except:
            raw_keys = [raw_keys]
    if isinstance(raw_keys, list):
        for k in raw_keys:
            if k and isinstance(k, str):
                clean_k = k.strip().strip('"').strip("'")
                if clean_k:
                    keys.append(clean_k)

if not keys:
    st.error("❌ Adaugă API Key în Secrets (Settings → Secrets)")
    st.info("📝 Adaugă în secrets: GOOGLE_API_KEY = 'cheia_ta'")
    st.stop()

if "key_index" not in st.session_state:
    st.session_state.key_index = 0


# === SYSTEM PROMPT ===
SYSTEM_PROMPT = r"""
ROL: Ești un profesor de liceu din România, universal (Mate, Fizică, Chimie, Literatură, Franceză, Engleză, Geografie, Istorie, Informatică), bărbat, cu experiență în pregătirea pentru BAC.

REGULI DE IDENTITATE:
1. Folosește EXCLUSIV genul masculin când vorbești despre tine.
2. Te prezinți ca "Domnul Profesor" sau "Profesorul tău virtual".

TON ȘI ADRESARE:
3. Vorbește DIRECT, la persoana I singular: "Sunt aici să te ajut", nu "Profesorul te va ajuta".
4. Fii cald, natural, apropiat și concis.
5. NU saluta în fiecare mesaj, doar la început.
6. La întrebări directe, răspunde DIRECT la subiect.

PREDARE:
1. MATEMATICĂ: Valori exacte ($\sqrt{2}$, $\pi$). LaTeX pentru formule.
2. FIZICĂ/CHIMIE: Condiții ideale, fără frecare/pierderi.
3. ROMÂNĂ: Programa BAC, canoane critice (Călinescu, Lovinescu, Vianu).
4. STIL: Definiție → Exemplu → Aplicație. Explică pașii logici.

DESENARE SVG:
La cerere de desen/hartă:
[[DESEN_SVG]]
<svg viewBox="0 0 800 600" xmlns="http://www.w3.org/2000/svg">
  <!-- cod aici -->
</svg>
[[/DESEN_SVG]]
"""


safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]


def run_chat_with_rotation(history, payload):
    for attempt in range(len(keys) * 2):
        try:
            if st.session_state.key_index >= len(keys):
                st.session_state.key_index = 0
            key = keys[st.session_state.key_index]
            genai.configure(api_key=key)
            model = genai.GenerativeModel("models/gemini-2.0-flash-exp", 
                                          system_instruction=SYSTEM_PROMPT,
                                          safety_settings=safety_settings)
            chat = model.start_chat(history=history)
            stream = chat.send_message(payload, stream=True)
            for chunk in stream:
                try:
                    if chunk.text:
                        yield chunk.text
                except ValueError:
                    continue
            return
        except Exception as e:
            err = str(e)
            if "503" in err or "overloaded" in err:
                time.sleep(2)
                continue
            elif "429" in err or "Quota" in err:
                st.session_state.key_index = (st.session_state.key_index + 1) % len(keys)
                continue
            else:
                raise e
    raise Exception("Serviciul indisponibil")


# === UI ===
st.title("🎓 Profesor Liceu")

with st.sidebar:
    st.header("⚙️ Opțiuni")
    
    if st.button("🗑️ Șterge Istoricul", type="primary"):
        clear_history_db(st.session_state.session_id)
        st.session_state.messages = []
        st.rerun()
    
    enable_audio = st.checkbox("🔊 Voce", value=False)
    
    if enable_audio and EDGE_TTS_AVAILABLE:
        voice_opt = st.radio("🎙️ Voce:", ["👨 Domnul Profesor", "👩 Doamna Profesoară"], index=0)
        selected_voice = VOICE_MALE_RO if "Domnul" in voice_opt else VOICE_FEMALE_RO
    else:
        selected_voice = VOICE_MALE_RO
    
    st.divider()
    st.header("📁 Materiale")
    uploaded = st.file_uploader("Poză/PDF", type=["jpg", "jpeg", "png", "pdf"])
    media = None
    
    if uploaded:
        genai.configure(api_key=keys[st.session_state.key_index])
        if "image" in uploaded.type:
            media = Image.open(uploaded)
            st.image(media, use_container_width=True)
        elif "pdf" in uploaded.type:
            st.info("📄 Procesez PDF...")
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                    tmp.write(uploaded.getvalue())
                with st.spinner("📚 Upload..."):
                    pdf = genai.upload_file(tmp.name, mime_type="application/pdf")
                    while pdf.state.name == "PROCESSING":
                        time.sleep(1)
                        pdf = genai.get_file(pdf.name)
                    media = pdf
                    st.success(f"✅ {uploaded.name}")
            except Exception as e:
                st.error(f"Eroare PDF: {e}")
    
    st.divider()
    if st.checkbox("🔧 Debug"):
        st.caption(f"📊 Mesaje: {len(st.session_state.get('messages', []))}/{MAX_MESSAGES_IN_MEMORY}")
        st.caption(f"🔑 API: {st.session_state.key_index + 1}/{len(keys)}")
        st.caption(f"🔊 Edge: {'✅' if EDGE_TTS_AVAILABLE else '❌'}")


if "messages" not in st.session_state or not st.session_state.messages:
    st.session_state.messages = load_history_from_db(st.session_state.session_id)

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant":
            render_message_with_svg(msg["content"])
        else:
            st.markdown(msg["content"])


if user_input := st.chat_input("Întreabă profesorul..."):
    st.chat_message("user").write(user_input)
    st.session_state.messages.append({"role": "user", "content": user_input})
    save_message_with_limits(st.session_state.session_id, "user", user_input)
    
    context = get_context_for_ai(st.session_state.messages)
    history = []
    for m in context:
        role = "model" if m["role"] == "assistant" else "user"
        history.append({"role": role, "parts": [m["content"]]})
    
    payload = []
    if media:
        payload.append("Analizează:")
        payload.append(media)
    payload.append(user_input)
    
    with st.chat_message("assistant"):
        placeholder = st.empty()
        response = ""
        
        try:
            for chunk in run_chat_with_rotation(history, payload):
                response += chunk
                if "<svg" in response:
                    placeholder.markdown(response.split("<path")[0] + "\n\n*🎨 Desenez...*\n\n▌")
                else:
                    placeholder.markdown(response + "▌")
            
            placeholder.empty()
            render_message_with_svg(response)
            
            st.session_state.messages.append({"role": "assistant", "content": response})
            save_message_with_limits(st.session_state.session_id, "assistant", response)
            
            if enable_audio and (EDGE_TTS_AVAILABLE or GTTS_AVAILABLE):
                with st.spinner("🎙️ Generez vocea..."):
                    audio = generate_professor_voice(response, selected_voice)
                    if audio:
                        st.audio(audio, format='audio/mp3')
                        
        except Exception as e:
            st.error(f"❌ Eroare: {e}")
