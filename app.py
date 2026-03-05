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
except ImportError:
    st.error("❌ Lipsește 'google-generativeai'. Verifică requirements.txt!")
    st.stop()

try:
    from PIL import Image
except ImportError:
    st.error("❌ Lipsește 'Pillow'. Verifică requirements.txt!")
    st.stop()

# TTS - opțional, cu fallback
EDGE_TTS_AVAILABLE = False
GTTS_AVAILABLE = False

try:
    import edge_tts
    import asyncio
    EDGE_TTS_AVAILABLE = True
except ImportError:
    pass

try:
    from gtts import gTTS
    GTTS_AVAILABLE = True
except ImportError:
    pass


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
    
    c.execute('''
        CREATE INDEX IF NOT EXISTS idx_history_session 
        ON history(session_id)
    ''')
    
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
        cutoff_time = time.time() - (days_old * 24 * 60 * 60)
        c.execute("DELETE FROM history WHERE timestamp < ?", (cutoff_time,))
        c.execute("DELETE FROM sessions WHERE last_active < ?", (cutoff_time,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Eroare la cleanup: {e}")


def save_message_to_db(session_id, role, content):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "INSERT INTO history (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, role, content, time.time())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Eroare DB: {e}")


def load_history_from_db(session_id, limit: int = MAX_MESSAGES_IN_MEMORY):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT role, content FROM (
                SELECT role, content, timestamp 
                FROM history 
                WHERE session_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            ) ORDER BY timestamp ASC
        """, (session_id, limit))
        data = c.fetchall()
        conn.close()
        return [{"role": row[0], "content": row[1]} for row in data]
    except Exception as e:
        print(f"Eroare la încărcarea istoricului: {e}")
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
                DELETE FROM history 
                WHERE session_id = ? 
                AND id IN (
                    SELECT id FROM history 
                    WHERE session_id = ?
                    ORDER BY timestamp ASC
                    LIMIT ?
                )
            """, (session_id, session_id, to_delete))
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"Eroare la curățarea DB: {e}")


# === SESSION MANAGEMENT ===
def generate_unique_session_id() -> str:
    uuid_part = uuid.uuid4().hex[:16]
    time_part = hex(int(time.time() * 1000000))[2:][-8:]
    random_part = uuid.uuid4().hex[:8]
    return f"{uuid_part}{time_part}{random_part}"


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
        c.execute(
            "INSERT OR IGNORE INTO sessions (session_id, created_at, last_active) VALUES (?, ?, ?)",
            (session_id, time.time(), time.time())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Eroare la înregistrarea sesiunii: {e}")


def update_session_activity(session_id: str):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE sessions SET last_active = ? WHERE session_id = ?", (time.time(), session_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Eroare la actualizarea sesiunii: {e}")


def get_or_create_session_id() -> str:
    if "session_id" in st.query_params:
        existing_id = st.query_params["session_id"]
        if existing_id and len(existing_id) >= 16:
            return existing_id
    
    if "session_id" in st.session_state:
        existing_id = st.session_state.session_id
        if existing_id and len(existing_id) >= 16:
            return existing_id
    
    for _ in range(10):
        new_id = generate_unique_session_id()
        if not session_exists_in_db(new_id):
            register_session(new_id)
            return new_id
    
    fallback_id = f"{uuid.uuid4().hex}{int(time.time())}"
    register_session(fallback_id)
    return fallback_id


# === MEMORY MANAGEMENT ===
def trim_session_messages():
    if "messages" in st.session_state:
        current_count = len(st.session_state.messages)
        if current_count > MAX_MESSAGES_IN_MEMORY:
            excess = current_count - MAX_MESSAGES_IN_MEMORY
            st.session_state.messages = st.session_state.messages[excess:]
            st.toast(f"📝 Am arhivat {excess} mesaje vechi.", icon="📦")


def get_context_for_ai(messages: list) -> list:
    if len(messages) <= MAX_MESSAGES_TO_SEND_TO_AI:
        return messages[:-1]
    
    first_message = messages[0] if messages else None
    recent_messages = messages[-(MAX_MESSAGES_TO_SEND_TO_AI - 1):-1]
    
    if first_message and first_message not in recent_messages:
        return [first_message] + recent_messages
    return recent_messages


def save_message_with_limits(session_id: str, role: str, content: str):
    save_message_to_db(session_id, role, content)
    if len(st.session_state.get("messages", [])) % 10 == 0:
        trim_db_messages(session_id)
    trim_session_messages()


# === AUDIO / TTS FUNCTIONS ===
def clean_text_for_audio(text: str) -> str:
    if not text:
        return ""
    
    # Elimină SVG
    text = re.sub(r'\[\[DESEN_SVG\]\].*?\[\[/DESEN_SVG\]\]', 
                  ' Am desenat o figură pentru tine. ', text, flags=re.DOTALL)
    text = re.sub(r'<svg.*?</svg>', ' ', text, flags=re.DOTALL)
    
    # Convertește LaTeX în text citibil
    latex_to_text = {
        r'\\sqrt\{([^}]+)\}': r'radical din \1',
        r'\\sqrt\[(\d+)\]\{([^}]+)\}': r'radical de ordin \1 din \2',
        r'\\frac\{([^}]+)\}\{([^}]+)\}': r'\1 supra \2',
        r'\\dfrac\{([^}]+)\}\{([^}]+)\}': r'\1 supra \2',
        r'\^(\d+)': r' la puterea \1',
        r'\^\{([^}]+)\}': r' la puterea \1',
        r'_(\d+)': r' indice \1',
        r'_\{([^}]+)\}': r' indice \1',
        r'\\alpha': 'alfa',
        r'\\beta': 'beta',
        r'\\gamma': 'gama',
        r'\\delta': 'delta',
        r'\\theta': 'teta',
        r'\\lambda': 'lambda',
        r'\\pi': 'pi',
        r'\\sigma': 'sigma',
        r'\\omega': 'omega',
        r'\\phi': 'fi',
        r'\\times': ' ori ',
        r'\\cdot': ' ori ',
        r'\\div': ' împărțit la ',
        r'\\pm': ' plus minus ',
        r'\\leq': ' mai mic sau egal cu ',
        r'\\geq': ' mai mare sau egal cu ',
        r'\\neq': ' diferit de ',
        r'\\approx': ' aproximativ egal cu ',
        r'\\infty': ' infinit',
        r'\\sum': ' suma ',
        r'\\int': ' integrala ',
        r'\\lim': ' limita ',
        r'\\log': ' logaritm ',
        r'\\ln': ' logaritm natural ',
        r'\\sin': ' sinus ',
        r'\\cos': ' cosinus ',
        r'\\tan': ' tangentă ',
        r'\\rightarrow': ' implică ',
        r'\\Rightarrow': ' rezultă că ',
        r'\\in': ' aparține ',
        r'\\subset': ' inclus în ',
        r'\\cup': ' reunit cu ',
        r'\\cap': ' intersectat cu ',
    }
    
    for pattern, replacement in latex_to_text.items():
        text = re.sub(pattern, replacement, text)
    
    # Elimină delimitatori LaTeX
    text = re.sub(r'\$\$([^$]+)\$\$', r' \1 ', text)
    text = re.sub(r'\$([^$]+)\$', r' \1 ', text)
    text = re.sub(r'\\\[(.+?)\\\]', r' \1 ', text, flags=re.DOTALL)
    text = re.sub(r'\\\((.+?)\\\)', r' \1 ', text)
    
    # Curăță comenzi LaTeX rămase
    text = re.sub(r'\\[a-zA-Z]+\{[^}]*\}', '', text)
    text = re.sub(r'\\[a-zA-Z]+', '', text)
    text = re.sub(r'[{}\\]', '', text)
    
    # Elimină Markdown
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    
    # Elimină HTML
    text = re.sub(r'<[^>]+>', '', text)
    
    # Curăță spații
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[│▌►◄●○■□▪▫]', '', text)
    
    text = text.strip()
    if len(text) > 3000:
        text = text[:3000]
        last_period = max(text.rfind('.'), text.rfind('!'), text.rfind('?'))
        if last_period > 2500:
            text = text[:last_period + 1]
    
    return text


def generate_professor_voice(text: str, voice: str = VOICE_MALE_RO) -> BytesIO:
    """Generează audio cu fallback automat."""
    clean_text = clean_text_for_audio(text)
    
    if not clean_text or len(clean_text) < 10:
        return None
    
    # Încearcă Edge TTS (voce bărbat)
    if EDGE_TTS_AVAILABLE:
        try:
            selected_voice = voice if voice else VOICE_MALE_RO
            
            async def _generate_edge():
                communicate = edge_tts.Communicate(clean_text, selected_voice)
                audio_data = BytesIO()
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        audio_data.write(chunk["data"])
                audio_data.seek(0)
                return audio_data.getvalue()
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                audio_bytes = loop.run_until_complete(_generate_edge())
            finally:
                loop.close()
            
            if audio_bytes:
                result = BytesIO(audio_bytes)
                result.seek(0)
                return result
                
        except Exception as e:
            print(f"Edge TTS failed: {e}")
    
    # Fallback la gTTS
    if GTTS_AVAILABLE:
        try:
            sound_file = BytesIO()
            tts = gTTS(text=clean_text[:2000], lang='ro', slow=False)
            tts.write_to_fp(sound_file)
            sound_file.seek(0)
            return sound_file
        except Exception as e:
            print(f"gTTS failed: {e}")
    
    return None


# === SVG FUNCTIONS ===
def repair_svg(svg_content: str) -> str:
    if not svg_content:
        return None
    
    svg_content = svg_content.strip()
    
    has_svg_open = bool(re.search(r'<svg[^>]*>', svg_content, re.IGNORECASE))
    has_svg_close = '</svg>' in svg_content.lower()
    
    if not has_svg_open:
        svg_content = f'''<svg viewBox="0 0 800 600" xmlns="http://www.w3.org/2000/svg" 
                             style="max-width: 100%; height: auto; background-color: white;">
            {svg_content}
        </svg>'''
        return svg_content
    
    if has_svg_open and not has_svg_close:
        svg_content = svg_content + '\n</svg>'
    
    if not has_svg_open and has_svg_close:
        svg_content = f'<svg viewBox="0 0 800 600" xmlns="http://www.w3.org/2000/svg">\n{svg_content}'
    
    svg_content = repair_unclosed_tags(svg_content)
    
    if 'xmlns=' not in svg_content:
        svg_content = svg_content.replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    
    if 'viewBox=' not in svg_content.lower():
        svg_content = svg_content.replace('<svg', '<svg viewBox="0 0 800 600"', 1)
    
    return svg_content


def repair_unclosed_tags(svg_content: str) -> str:
    self_closing_tags = ['path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon', 'image', 'use']
    
    for tag in self_closing_tags:
        pattern = rf'<{tag}([^>]*[^/])>'
        def fix_tag(match):
            attrs = match.group(1)
            return f'<{tag}{attrs}/>'
        svg_content = re.sub(pattern, fix_tag, svg_content)
    
    text_opens = len(re.findall(r'<text[^>]*>', svg_content))
    text_closes = len(re.findall(r'</text>', svg_content))
    if text_opens > text_closes:
        for _ in range(text_opens - text_closes):
            svg_content = svg_content.replace('</svg>', '</text></svg>')
    
    g_opens = len(re.findall(r'<g[^>]*>', svg_content))
    g_closes = len(re.findall(r'</g>', svg_content))
    if g_opens > g_closes:
        for _ in range(g_opens - g_closes):
            svg_content = svg_content.replace('</svg>', '</g></svg>')
    
    return svg_content


def validate_svg(svg_content: str) -> tuple:
    if not svg_content:
        return False, "SVG gol"
    
    if '<svg' not in svg_content.lower():
        return False, "Lipsește tag-ul <svg>"
    
    if '</svg>' not in svg_content.lower():
        return False, "Lipsește tag-ul </svg>"
    
    visual_elements = ['path', 'rect', 'circle', 'ellipse', 'line', 'text', 'polygon', 'polyline', 'image']
    has_content = any(f'<{elem}' in svg_content.lower() for elem in visual_elements)
    
    if not has_content:
        return False, "SVG fără elemente vizuale"
    
    return True, "OK"


def render_message_with_svg(content: str):
    has_svg_markers = '[[DESEN_SVG]]' in content or '<svg' in content.lower()
    has_svg_elements = any(tag in content.lower() for tag in ['<path', '<rect', '<circle', '<line', '<polygon'])
    
    if has_svg_markers or (has_svg_elements and 'stroke=' in content):
        svg_code = None
        before_text = ""
        after_text = ""
        
        if '[[DESEN_SVG]]' in content:
            parts = content.split('[[DESEN_SVG]]')
            before_text = parts[0]
            if len(parts) > 1 and '[[/DESEN_SVG]]' in parts[1]:
                inner_parts = parts[1].split('[[/DESEN_SVG]]')
                svg_code = inner_parts[0]
                after_text = inner_parts[1] if len(inner_parts) > 1 else ""
            elif len(parts) > 1:
                svg_code = parts[1]
        elif '<svg' in content.lower():
            svg_match = re.search(r'<svg.*?</svg>', content, re.DOTALL | re.IGNORECASE)
            if svg_match:
                svg_code = svg_match.group(0)
                before_text = content[:svg_match.start()]
                after_text = content[svg_match.end():]
            else:
                svg_start = content.lower().find('<svg')
                if svg_start != -1:
                    before_text = content[:svg_start]
                    svg_code = content[svg_start:]
        
        if svg_code:
            svg_code = repair_svg(svg_code)
            is_valid, error = validate_svg(svg_code)
            
            if is_valid:
                if before_text.strip():
                    st.markdown(before_text.strip())
                
                st.markdown(
                    f'<div class="svg-container">{svg_code}</div>',
                    unsafe_allow_html=True
                )
                
                if after_text.strip():
                    st.markdown(after_text.strip())
                return
            else:
                st.warning(f"⚠️ Desenul nu a putut fi afișat corect: {error}")
    
    clean_content = content
    clean_content = re.sub(r'\[\[DESEN_SVG\]\]', '\n🎨 *Desen:*\n', clean_content)
    clean_content = re.sub(r'\[\[/DESEN_SVG\]\]', '\n', clean_content)
    
    st.markdown(clean_content)


# === INIȚIALIZARE ===
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
    k = st.sidebar.text_input("API Key (Manual):", type="password")
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
    st.error("❌ Nu am găsit nicio cheie API validă.")
    st.stop()

if "key_index" not in st.session_state:
    st.session_state.key_index = 0


# === SYSTEM PROMPT (RAW STRING PENTRU LATEX) ===
SYSTEM_PROMPT = r"""
ROL: Ești un profesor de liceu din România, universal (Mate, Fizică, Chimie, Literatură si Gramatica Romana, Franceza, Engleza, Geografie, Istorie, Informatica), bărbat, cu experiență în pregătirea pentru BAC.
    
    REGULI DE IDENTITATE (STRICT):
    1. Folosește EXCLUSIV genul masculin când vorbești despre tine.
       - Corect: "Sunt sigur", "Sunt pregătit", "Am fost atent", "Sunt bucuros".
       - GREȘIT: "Sunt sigură", "Sunt pregătită".
    2. Te prezinți ca "Domnul Profesor" sau "Profesorul tău virtual".
    
    TON ȘI ADRESARE (CRITIC):
    3. Vorbește DIRECT, la persoana I singular.
       - CORECT: "Salut, sunt aici să te ajut." / "Te ascult." / "Sunt pregătit."
       - GREȘIT: "Domnul profesor este aici." / "Profesorul te va ajuta."
    4. Fii cald, natural, apropiat și scurt. Evită introducerile pompoase.
    5. NU SALUTA în fiecare mesaj. Salută DOAR la începutul unei conversații noi.
    6. Dacă elevul pune o întrebare directă, răspunde DIRECT la subiect, fără introduceri de genul "Salut, desigur...".
    7. Folosește "Salut" sau "Te salut" în loc de formule foarte oficiale.
        
    REGULĂ STRICTĂ: Predă exact ca la școală (nivel Gimnaziu/Liceu). 
    NU confunda elevul cu detalii despre "aproximări" sau "lumea reală" (frecare, erori) decât dacă problema o cere specific.

    GHID DE COMPORTAMENT:
    1. MATEMATICĂ:
       - Lucrează cu valori exacte ($\sqrt{2}$, $\pi$) sau standard.
       - Dacă rezultatul e $\sqrt{2}$, lasă-l $\sqrt{2}$. Nu spune "care este aproximativ 1.41".
       - Nu menționa că $\pi$ e infinit; folosește valorile din manual fără comentarii suplimentare. 
       - Explică logica din spate, nu doar calculul.
       - Dacă rezultatul e rad(2), lasă-l rad(2). Nu îl calcula aproximativ.
       - Folosește LaTeX ($...$) pentru toate formulele.

    2. FIZICĂ/CHIMIE:
       - Presupune automat "condiții ideale".
       - Tratează problema exact așa cum apare în culegere.
       - Nu menționa frecarea cu aerul, pierderile de căldură sau imperfecțiunile aparatelor de măsură.
       - Tratează problema exact așa cum apare în culegere, într-un univers matematic perfect.

    3. LIMBA ȘI LITERATURA ROMÂNĂ (CRITIC):
       - Respectă STRICT programa școlară de BAC din România și canoanele criticii (G. Călinescu, E. Lovinescu, T. Vianu).
       - ATENȚIE MAJORA: Ion Creangă (Harap-Alb) este Basm Cult, dar specificul lui este REALISMUL (umanizarea fantasticului, oralitatea), nu romantismul.
       - La poezie: Încadrează corect (Romantism - Eminescu, Modernism - Blaga/Arghezi, Simbolism - Bacovia).
       - Structurează răspunsurile ca un eseu de BAC (Ipoteză -> Argumente (pe text) -> Concluzie).

    4. STIL DE PREDARE:
       - Explică simplu, cald și prietenos. Evită "limbajul de lemn".
       - Folosește analogii pentru concepte grele (ex: "Curentul e ca debitul apei").
       - La teorie: Definiție -> Exemplu Concret -> Aplicație.
       - La probleme: Explică pașii logici ("Facem asta pentru că..."), nu da doar calculul.

    5. MATERIALE UPLOADATE (Cărți/PDF):
       - Dacă primești o carte, păstrează sensul original în rezumate/traduceri.
       - Dacă elevul încarcă o poză sau un PDF, analizează tot conținutul înainte de a răspunde.
       - Păstrează sensul original al textelor din manuale.
           
    6. FUNCȚIE SPECIALĂ - DESENARE (SVG):
        Dacă elevul cere un desen, o diagramă sau o hartă:
        1. Ești OBLIGAT să generezi cod SVG valid.
        2. Codul trebuie încadrat STRICT între tag-uri:
           [[DESEN_SVG]]
           <svg viewBox="0 0 800 600" xmlns="http://www.w3.org/2000/svg">
              <!-- Codul tău aici -->
           </svg>
           [[/DESEN_SVG]]
        3. IMPORTANT: Nu uita tag-ul de deschidere <svg> și cel de închidere </svg>!

        REGULI HĂRȚI (GEOGRAFIE):
        - Nu desena pătrate. Folosește <path> pentru contururi.
        - Râurile = linii albastre.
        - Adaugă etichete text (<text>).
"""


safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]


def run_chat_with_rotation(history_obj, payload):
    max_retries = len(keys) * 2
    for attempt in range(max_retries):
        try:
            if st.session_state.key_index >= len(keys):
                st.session_state.key_index = 0
            current_key = keys[st.session_state.key_index]
            genai.configure(api_key=current_key)
            model = genai.GenerativeModel(
                "models/gemini-2.5-flash",
                system_instruction=SYSTEM_PROMPT,
                safety_settings=safety_settings
            )
            chat = model.start_chat(history=history_obj)
            response_stream = chat.send_message(payload, stream=True)
            for chunk in response_stream:
                try:
                    if chunk.text:
                        yield chunk.text
                except ValueError:
                    continue
            return
        except Exception as e:
            error_msg = str(e)
            if "503" in error_msg or "overloaded" in error_msg:
                st.toast("🐢 Reîncerc...", icon="⏳")
                time.sleep(2)
                continue
            elif "400" in error_msg or "429" in error_msg or "Quota" in error_msg or "API key not valid" in error_msg:
                st.toast(f"⚠️ Schimb cheia {st.session_state.key_index + 1}...", icon="🔄")
                st.session_state.key_index = (st.session_state.key_index + 1) % len(keys)
                continue
            else:
                raise e
    raise Exception("Serviciul este indisponibil momentan.")


# === UI PRINCIPAL ===
st.title("🎓 Profesor Liceu")

with st.sidebar:
    st.header("⚙️ Opțiuni")
    
    if st.button("🗑️ Șterge Istoricul", type="primary"):
        clear_history_db(st.session_state.session_id)
        st.session_state.messages = []
        st.rerun()
    
    enable_audio = st.checkbox("🔊 Voce", value=False)
    
    if enable_audio:
        if EDGE_TTS_AVAILABLE:
            voice_option = st.radio(
                "🎙️ Alege vocea:",
                options=["👨 Domnul Profesor (Emil)", "👩 Doamna Profesoară (Alina)"],
                index=0
            )
            selected_voice = VOICE_MALE_RO if "Emil" in voice_option else VOICE_FEMALE_RO
        else:
            st.caption("ℹ️ Voce standard (gTTS)")
            selected_voice = VOICE_MALE_RO
    else:
        selected_voice = VOICE_MALE_RO
    
    st.divider()
    
    st.header("📁 Materiale")
    uploaded_file = st.file_uploader("Încarcă Poză sau PDF", type=["jpg", "jpeg", "png", "pdf"])
    media_content = None
    
    if uploaded_file:
        genai.configure(api_key=keys[st.session_state.key_index])
        file_type = uploaded_file.type
        
        if "image" in file_type:
            media_content = Image.open(uploaded_file)
            st.image(media_content, caption="Imagine atașată", use_container_width=True)
        elif "pdf" in file_type:
            st.info("📄 PDF Detectat. Se procesează...")
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                    tmp.write(uploaded_file.getvalue())
                    tmp_path = tmp.name
                with st.spinner("📚 Se trimite cartea la AI..."):
                    uploaded_pdf = genai.upload_file(tmp_path, mime_type="application/pdf")
                    while uploaded_pdf.state.name == "PROCESSING":
                        time.sleep(1)
                        uploaded_pdf = genai.get_file(uploaded_pdf.name)
                    media_content = uploaded_pdf
                    st.success(f"✅ Gata: {uploaded_file.name}")
            except Exception as e:
                st.error(f"Eroare upload PDF: {e}")
    
    st.divider()
    
    if st.checkbox("🔧 Debug Info", value=False):
        msg_count = len(st.session_state.get("messages", []))
        st.caption(f"📊 Mesaje: {msg_count}/{MAX_MESSAGES_IN_MEMORY}")
        st.caption(f"🔑 Cheie API: {st.session_state.key_index + 1}/{len(keys)}")
        st.caption(f"🆔 Sesiune: {st.session_state.session_id[:16]}...")
        st.caption(f"🔊 Edge TTS: {'✅' if EDGE_TTS_AVAILABLE else '❌'}")
        st.caption(f"🔊 gTTS: {'✅' if GTTS_AVAILABLE else '❌'}")


# === ÎNCĂRCARE MESAJE ===
if "messages" not in st.session_state or not st.session_state.messages:
    st.session_state.messages = load_history_from_db(st.session_state.session_id)

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant":
            render_message_with_svg(msg["content"])
        else:
            st.markdown(msg["content"])


# === CHAT INPUT ===
if user_input := st.chat_input("Întreabă profesorul..."):
    st.chat_message("user").write(user_input)
    st.session_state.messages.append({"role": "user", "content": user_input})
    save_message_with_limits(st.session_state.session_id, "user", user_input)
    
    context_messages = get_context_for_ai(st.session_state.messages)
    history_obj = []
    for msg in context_messages:
        role_gemini = "model" if msg["role"] == "assistant" else "user"
        history_obj.append({"role": role_gemini, "parts": [msg["content"]]})
    
    final_payload = []
    if media_content:
        final_payload.append("Analizează materialul atașat:")
        final_payload.append(media_content)
    final_payload.append(user_input)
    
    with st.chat_message("assistant"):
        message_placeholder = st.empty()
        full_response = ""
        
        try:
            stream_generator = run_chat_with_rotation(history_obj, final_payload)
            
            for text_chunk in stream_generator:
                full_response += text_chunk
                
                if "<svg" in full_response or ("<path" in full_response and "stroke=" in full_response):
                    message_placeholder.markdown(
                        full_response.split("<path")[0] + "\n\n*🎨 Domnul Profesor desenează...*\n\n▌"
                    )
                else:
                    message_placeholder.markdown(full_response + "▌")
            
            message_placeholder.empty()
            render_message_with_svg(full_response)
            
            st.session_state.messages.append({"role": "assistant", "content": full_response})
            save_message_with_limits(st.session_state.session_id, "assistant", full_response)
            
            if enable_audio and (EDGE_TTS_AVAILABLE or GTTS_AVAILABLE):
                with st.spinner("🎙️ Domnul Profesor vorbește..."):
                    audio_file = generate_professor_voice(full_response, selected_voice)
                    
                    if audio_file:
                        st.audio(audio_file, format='audio/mp3')
                    else:
                        st.caption("🔇 Nu am putut genera vocea.")
                        
        except Exception as e:
            st.error(f"❌ Eroare: {e}")
