import streamlit as st
import google.generativeai as genai
from PIL import Image
import edge_tts
import asyncio
from io import BytesIO
import sqlite3
import uuid
import time
import tempfile
import ast
import re


# === CONSTANTE ===
MAX_MESSAGES_IN_MEMORY = 100
MAX_MESSAGES_TO_SEND_TO_AI = 20
MAX_MESSAGES_IN_DB_PER_SESSION = 500
CLEANUP_DAYS_OLD = 7

VOICE_MALE_RO = "ro-RO-EmilNeural"
VOICE_FEMALE_RO = "ro-RO-AlinaNeural"


st.set_page_config(page_title="Profesor Liceu", page_icon="🎓", layout="wide", initial_sidebar_state="expanded")

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
        cutoff_time = time.time() - (days_old * 24 * 60 * 60)
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
                    SELECT id FROM
					                    SELECT id FROM history WHERE session_id = ?
					                    ORDER BY timestamp ASC LIMIT ?
					                )
					            """, (session_id, session_id, to_delete))
					            conn.commit()
					        conn.close()
					    except Exception as e:
					        print(f"Trim error: {e}")


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


					def trim_session_messages():
					    if "messages" in st.session_state:
					        if len(st.session_state.messages) > MAX_MESSAGES_IN_MEMORY:
					            excess = len(st.session_state.messages) - MAX_MESSAGES_IN_MEMORY
					            st.session_state.messages = st.session_state.messages[excess:]
					            st.toast(f"📝 Am arhivat {excess} mesaje vechi.", icon="📦")


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
    
						    num = r'(\d+[.,]?\d*)'
    
						    # Rezistență - Ω (FIX: procesăm COMPLET)
						    text = re.sub(num + r'\s*GΩ', r'\1 gigaohmi', text)
						    text = re.sub(num + r'\s*MΩ', r'\1 megaohmi', text)
						    text = re.sub(num + r'\s*kΩ', r'\1 kiloohmi', text)
						    text = re.sub(num + r'\s*mΩ', r'\1 miliohmi', text)
						    text = re.sub(num + r'\s*μΩ', r'\1 microohmi', text)
						    text = re.sub(num + r'\s*µΩ', r'\1 microohmi', text)
						    text = re.sub(num + r'\s*nΩ', r'\1 nanoohmi', text)
						    text = re.sub(num + r'\s*Ω', r'\1 ohmi', text)
						    # FIX FINAL: Ω rămas = ohmi
						    text = re.sub(r'Ω', ' ohmi ', text)
    
						    # Temperatură
						    text = re.sub(num + r'\s*°C', r'\1 grade Celsius', text)
						    text = re.sub(num + r'\s*°F', r'\1 grade Fahrenheit', text)
						    text = re.sub(num + r'\s*°K', r'\1 Kelvin', text)
						    text = re.sub(num + r'\s*K\b', r'\1 Kelvin', text)
						    text = re.sub(num + r'\s*°', r'\1 grade', text)
    
						    # Tensiune
						    text = re.sub(num + r'\s*MV', r'\1 megavolți', text)
						    text = re.sub(num + r'\s*kV', r'\1 kilovolți', text)
						    text = re.sub(num + r'\s*mV', r'\1 milivolți', text)
						    text = re.sub(num + r'\s*μV', r'\1 microvolți', text)
						    text = re.sub(num + r'\s*V\b', r'\1 volți', text)
    
						    # Curent
						    text = re.sub(num + r'\s*kA', r'\1 kiloamperi', text)
						    text = re.sub(num + r'\s*mA', r'\1 miliamperi', text)
						    text = re.sub(num + r'\s*μA', r'\1 microamperi', text)
						    text = re.sub(num + r'\s*nA', r'\1 nanoamperi', text)
						    text = re.sub(num + r'\s*A\b', r'\1 amperi', text)
    
						    # Putere
						    text = re.sub(num + r'\s*GW', r'\1 gigawați', text)
						    text = re.sub(num + r'\s*MW', r'\1 megawați', text)
						    text = re.sub(num + r'\s*kW', r'\1 kilowați', text)
						    text = re.sub(num + r'\s*mW', r'\1 miliwați', text)
						    text = re.sub(num + r'\s*μW', r'\1 microwați', text)
						    text = re.sub(num + r'\s*W\b', r'\1 wați', text)
    
						    # Frecvență
						    text = re.sub(num + r'\s*THz', r'\1 terahertzi', text)
						    text = re.sub(num + r'\s*GHz', r'\1 gigahertzi', text)
						    text = re.sub(num + r'\s*MHz', r'\1 megahertzi', text)
						    text = re.sub(num + r'\s*kHz', r'\1 kilohertzi', text)
						    text = re.sub(num + r'\s*Hz', r'\1 hertzi', text)
    
						    # Capacitate
						    text = re.sub(num + r'\s*μF', r'\1 microfarazi', text)
						    text = re.sub(num + r'\s*nF', r'\1 nanofarazi', text)
						    text = re.sub(num + r'\s*pF', r'\1 picofarazi', text)
						    text = re.sub(num + r'\s*F\b', r'\1 farazi', text)
    
						    # Inductanță
						    text = re.sub(num + r'\s*mH', r'\1 milihenry', text)
						    text = re.sub(num + r'\s*μH', r'\1 microhenry', text)
						    text = re.sub(num + r'\s*H\b', r'\1 henry', text)
    
						    # Sarcină
						    text = re.sub(num + r'\s*mC', r'\1 milicoulombi', text)
						    text = re.sub(num + r'\s*μC', r'\1 microcoulombi', text)
						    text = re.sub(num + r'\s*C\b', r'\1 coulombi', text)
    
						    # Flux magnetic
						    text = re.sub(num + r'\s*Wb', r'\1 weberi', text)
						    text = re.sub(num + r'\s*T\b', r'\1 tesla', text)
    
						    # Forță
						    text = re.sub(num + r'\s*kN', r'\1 kilonewtoni', text)
						    text = re.sub(num + r'\s*N\b', r'\1 newtoni', text)
    
						    # Energie
						    text = re.sub(num + r'\s*MJ', r'\1 megajouli', text)
						    text = re.sub(num + r'\s*kJ', r'\1 kilojouli', text)
						    text = re.sub(num + r'\s*J\b', r'\1 jouli', text)
    
						    # Presiune
						    text = re.sub(num + r'\s*MPa', r'\1 megapascali', text)
						    text = re.sub(num + r'\s*kPa', r'\1 kilopascali', text)
						    text = re.sub(num + r'\s*Pa', r'\1 pascali', text)
						    text = re.sub(num + r'\s*atm', r'\1 atmosfere', text)
						    text = re.sub(num + r'\s*bar', r'\1 bari', text)
    
						    # Lungime
						    text = re.sub(num + r'\s*km\b', r'\1 kilometri', text)
						    text = re.sub(num + r'\s*cm\b', r'\1 centimetri', text)
						    text = re.sub(num + r'\s*mm\b', r'\1 milimetri', text)
						    text = re.sub(num + r'\s*μm', r'\1 micrometri', text)
						    text = re.sub(num + r'\s*nm\b', r'\1 nanometri', text)
						    text = re.sub(num + r'\s*m\b', r'\1 metri', text)
    
						    # Masă
						    text = re.sub(num + r'\s*kg\b', r'\1 kilograme', text)
						    text = re.sub(num + r'\s*mg\b', r'\1 miligrame', text)
						    text = re.sub(num + r'\s*g\b', r'\1 grame', text)
						    text = re.sub(num + r'\s*t\b', r'\1 tone', text)
    
						    # Volum
						    text = re.sub(num + r'\s*mL', r'\1 mililitri', text)
						    text = re.sub(num + r'\s*L\b', r'\1 litri', text)
						    text = re.sub(num + r'\s*m³', r'\1 metri cubi', text)
						    text = re.sub(num + r'\s*cm³', r'\1 centimetri cubi', text)
    
						    # Timp
						    text = re.sub(num + r'\s*ms\b', r'\1 milisecunde', text)
						    text = re.sub(num + r'\s*μs', r'\1 microsecunde', text)
						    text = re.sub(num + r'\s*min\b', r'\1 minute', text)
						    text = re.sub(num + r'\s*s\b', r'\1 secunde', text)
						    text = re.sub(num + r'\s*h\b', r'\1 ore', text)
    
						    # Suprafață
						    text = re.sub(num + r'\s*km²', r'\1 kilometri pătrați', text)
						    text = re.sub(num + r'\s*m²', r'\1 metri pătrați', text)
						    text = re.sub(num + r'\s*cm²', r'\1 centimetri pătrați', text)
						    text = re.sub(num + r'\s*ha\b', r'\1 hectare', text)
    
						    # Viteză
						    text = re.sub(num + r'\s*m/s²', r'\1 metri pe secundă la pătrat', text)
						    text = re.sub(num + r'\s*m/s\b', r'\1 metri pe secundă', text)
						    text = re.sub(num + r'\s*km/h', r'\1 kilometri pe oră', text)
						    text = re.sub(num + r'\s*rad/s', r'\1 radiani pe secundă', text)
    
						    # Densitate
						    text = re.sub(num + r'\s*kg/m³', r'\1 kilograme pe metru cub', text)
						    text = re.sub(num + r'\s*g/cm³', r'\1 grame pe centimetru cub', text)
    
						    # Chimie
						    text = re.sub(num + r'\s*mol\b', r'\1 moli', text)
						    text = re.sub(num + r'\s*mol/L', r'\1 moli pe litru', text)
						    text = re.sub(num + r'\s*g/mol', r'\1 grame pe mol', text)
    
						    # Energie specială
						    text = re.sub(num + r'\s*kWh', r'\1 kilowatt oră', text)
						    text = re.sub(num + r'\s*eV', r'\1 electronvolți', text)
    
						    # Indici cu underscore (P_r, V_0)
						    text = re.sub(r'([A-Za-zα-ωΑ-Ω])\s*_\s*\{([^}]+)\}', r'\1 indice \2', text)
						    text = re.sub(r'([A-Za-zα-ωΑ-Ω])\s*_\s*([A-Za-z0-9α-ωΑ-Ω]+)', r'\1 indice \2', text)
    
						    # Combinații speciale
						    special = {
						        '>=': ' mai mare sau egal cu ',
						        '<=': ' mai mic sau egal cu ',
						        '!=': ' diferit de ',
						        '==': ' egal cu ',
						        '>>': ' mult mai mare decât ',
						        '<<': ' mult mai mic decât ',
						        '->': ' implică ',
						        '=>': ' rezultă că ',
						        '...': ' ',
						        'N·m': ' newton metri ',
						    }
						    for combo, repl in special.items():
						        text = text.replace(combo, repl)
    
						    # Caractere grecești și simboluri
						    greek = {
						        'α': ' alfa ', 'β': ' beta ', 'γ': ' gama ', 'δ': ' delta ',
						        'ε': ' epsilon ', 'ζ': ' zeta ', 'η': ' eta ', 'θ': ' teta ',
						        'ι': ' iota ', 'κ': ' kapa ', 'λ': ' lambda ', 'μ': ' miu ',
						        'ν': ' niu ', 'ξ': ' csi ', 'ο': ' omicron ', 'π': ' pi ',
						        'ρ': ' ro ', 'σ': ' sigma ', 'ς': ' sigma ', 'τ': ' tau ',
						        'υ': ' ipsilon ', 'φ': ' fi ', 'χ': ' hi ', 'ψ': ' psi ',
						        'ω': ' omega ',
						        'Α': ' alfa ', 'Β': ' beta ', 'Γ': ' gama ', 'Δ': ' delta ',
						        'Ε': ' epsilon ', 'Ζ': ' zeta ', 'Η': ' eta ', 'Θ': ' teta ',
						        'Ι': ' iota ', 'Κ': ' kapa ', 'Λ': ' lambda ', 'Μ': ' miu ',
						        'Ν': ' niu ', 'Ξ': ' csi ', 'Ο': ' omicron ', 'Π': ' pi ',
						        'Ρ': ' ro ', 'Σ': ' sigma ', 'Τ': ' tau ', 'Υ': ' ipsilon ',
						        'Φ': ' fi ', 'Χ': ' hi ', 'Ψ': ' psi ',
						        # Ω ȘTERS - procesat ca ohmi mai sus
						        'ₐ': ' indice a ', 'ₑ': ' indice e ', 'ᵢ': ' indice i ',
						        'ₒ': ' indice o ', 'ₚ': ' indice p ', 'ᵣ': ' indice r ',
						        'ₛ': ' indice s ', 'ₜ': ' indice t ', 'ᵤ': ' indice u ',
						        'ᵥ': ' indice v ', 'ₓ': ' indice x ', 'ₙ': ' indice n ',
						        '⁰': ' la puterea 0 ', '¹': ' la puterea 1 ', '²': ' la pătrat ',
						        '³': ' la cub ', '⁴': ' la puterea 4 ', 'ⁿ': ' la puterea n ',
						        '₀': ' indice 0 ', '₁': ' indice 1 ', '₂': ' indice 2 ',
						        '₃': ' indice 3 ', '₄': ' indice 4 ', '₅': ' indice 5 ',
						        '∞': ' infinit ', '∑': ' suma ', '∫': ' integrala ', '∂': ' derivata parțială ',
						        '√': ' radical din ', '±': ' plus minus ', '×': ' ori ', '÷': ' împărțit la ',
						        '≠': ' diferit de ', '≈': ' aproximativ egal cu ', '≡': ' identic cu ',
						        '≤': ' mai mic sau egal cu ', '≥': ' mai mare sau egal cu ',
						        '∈': ' aparține lui ', '∉': ' nu aparține lui ', '⊂': ' inclus în ',
						        '∪': ' reunit cu ', '∩': ' intersectat cu ', '∅': ' mulțimea vidă ',
						        '∀': ' pentru orice ', '∃': ' există ', '→': ' implică ',
						        '⇒': ' rezultă că ', '↔': ' echivalent cu ',
						        '>': ' mai mare decât ', '<': ' mai mic decât ', '=': ' egal ',
						        '+': ' plus ', '−': ' minus ', '·': ' ori ',
						        '½': ' o doime ', '⅓': ' o treime ', '¼': ' un sfert ', '¾': ' trei sferturi ',
						        '%': ' procent ', '°': ' grade ',
						        'ℕ': ' mulțimea numerelor naturale ', 'ℤ': ' mulțimea numerelor întregi ',
						        'ℚ': ' mulțimea numerelor raționale ', 'ℝ': ' mulțimea numerelor reale ',
						        'ℂ': ' mulțimea numerelor complexe ',
						    }
						    for symbol, pronun in greek.items():
						        text = text.replace(symbol, pronun)
    
						    # Punctuație specială
						    text = re.sub(r'(\d)\s*:\s*(\d)', r'\1 este la \2', text)
						    text = re.sub(r'(\d+)\s*/\s*(\d+)', r'\1 supra \2', text)
						    text = re.sub(r'(\w):\s+', r'\1. ', text)
    
						    # LaTeX
						    latex = {
						        r'\\sqrt\{([^}]+)\}': r' radical din \1 ',
						        r'\\frac\{([^}]+)\}\{([^}]+)\}': r' \1 supra \2 ',
						        r'\^(\d+)': r' la puterea \1 ',
						        r'\^\{([^}]+)\}': r' la puterea \1 ',
						        r'_(\d+)': r' indice \1 ',
						        r'_\{([^}]+)\}': r' indice \1 ',
						        r'\\alpha': ' alfa ', r'\\beta': ' beta ', r'\\gamma': ' gama ',
						        r'\\delta': ' delta ', r'\\epsilon': ' epsilon ', r'\\eta': ' eta ',
						        r'\\theta': ' teta ', r'\\lambda': ' lambda ', r'\\mu': ' miu ',
						        r'\\pi': ' pi ', r'\\rho': ' ro ', r'\\sigma': ' sigma ',
						        r'\\tau': ' tau ', r'\\phi': ' fi ', r'\\omega': ' omega ',
						        r'\\times': ' ori ', r'\\cdot': ' ori ', r'\\pm': ' plus minus ',
						        r'\\leq': ' mai mic sau egal cu ', r'\\geq': ' mai mare sau egal cu ',
						        r'\\neq': ' diferit de ', r'\\approx': ' aproximativ egal cu ',
						        r'\\infty': ' infinit ', r'\\sum': ' suma ', r'\\int': ' integrala ',
						        r'\\lim': ' limita ', r'\\sin': ' sinus de ', r'\\cos': ' cosinus de ',
						        r'\\tan': ' tangentă de ', r'\\in': ' aparține lui ',
						    }
						    for pattern, repl in latex.items():
						        text = re.sub(pattern, repl, text)
    
						    # Curățare finală
						    text = re.sub(r'\$\$?([^$]+)\$\$?', r' \1 ', text)
						    text = re.sub(r'\\[a-zA-Z]+\{[^}]*\}', '', text)
						    text = re.sub(r'\\[a-zA-Z]+', '', text)
						    text = re.sub(r'[{}\\]', '', text)
						    text = re.sub(r'\*+([^*]+)\*+', r'\1', text)
						    text = re.sub(r'`[^`]+`', '', text)
						    text = re.sub(r'<[^>]+>', '', text)
						    text = re.sub(r'[│▌►◄■▪▫\[\](){}]', ' ', text)
						    text = re.sub(r'\s*:\s*', '. ', text)
						    text = re.sub(r'\s+', ' ', text)
    
						    text = text.strip()
						    if len(text) > 3000:
						        text = text[:3000]
						        last = max(text.rfind('.'), text.rfind('!'), text.rfind('?'))
						        if last > 2500:
						            text = text[:last + 1]
    
						    return text


						async def _generate_audio_edge_tts(text: str, voice: str = VOICE_MALE_RO):
						    try:
						        clean = clean_text_for_audio(text)
						        if not clean or len(clean) < 10:
						            return None
        
						        comm = edge_tts.Communicate(clean, voice)
						        data = BytesIO()
						        async for chunk in comm.stream():
						            if chunk["type"] == "audio":
						                data.write(chunk["data"])
						        data.seek(0)
						        return data.getvalue()
						    except Exception as e:
						        print(f"Edge TTS error: {e}")
						        return None


						def generate_professor_voice(text: str, voice: str = VOICE_MALE_RO):
						    try:
						        loop = asyncio.new_event_loop()
						        asyncio.set_event_loop(loop)
						        try:
						            audio_bytes = loop.run_until_complete(_generate_audio_edge_tts(text, voice))
						        finally:
						            loop.close()
        
						        if audio_bytes:
						            result = BytesIO(audio_bytes)
						            result.seek(0)
						            return result
						        return None
						    except Exception as e:
						        print(f"Voice error: {e}")
						        return None


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
						    has_svg = '[[DESEN_SVG]]' in content or '<svg' in content.lower()
						    has_elem = any(t in content.lower() for t in ['<path', '<rect', '<circle'])
    
						    if has_svg or (has_elem and 'stroke=' in content):
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
    st.error("❌ Adaugă API Key în Settings → Secrets")
    st.stop()

if "key_index" not in st.session_state:
    st.session_state.key_index = 0


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


def run_chat_with_rotation(history, payload):
    max_retries = len(keys) * 2
    for attempt in range(max_retries):
        try:
            if st.session_state.key_index >= len(keys):
                st.session_state.key_index = 0
            key = keys[st.session_state.key_index]
            genai.configure(api_key=key)
            model = genai.GenerativeModel("models/gemini-2.5-flash", 
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
                st.toast("🐢 Reîncerc...", icon="⏳")
                time.sleep(2)
                continue
            elif "429" in err or "Quota" in err:
                st.toast(f"⚠️ Schimb cheia {st.session_state.key_index + 1}...", icon="🔄")
                st.session_state.key_index = (st.session_state.key_index + 1) % len(keys)
                continue
            else:
                raise e
    raise Exception("Serviciul indisponibil")


st.title("🎓 Profesor Liceu")

with st.sidebar:
    st.header("⚙️ Opțiuni")
    
    if st.button("🗑️ Șterge Istoricul", type="primary"):
        clear_history_db(st.session_state.session_id)
        st.session_state.messages = []
        st.rerun()
    
    enable_audio = st.checkbox("🔊 Voce", value=False)
    
    if enable_audio:
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
                st.error(f"Eroare: {e}")
    
    st.divider()
    if st.checkbox("🔧 Debug"):
        st.caption(f"📊 Mesaje: {len(st.session_state.get('messages', []))}/{MAX_MESSAGES_IN_MEMORY}")
        st.caption(f"🔑 API: {st.session_state.key_index + 1}/{len(keys)}")


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
            
            if enable_audio:
                with st.spinner("🎙️ Generez vocea..."):
                    audio = generate_professor_voice(response, selected_voice)
                    if audio:
                        st.audio(audio, format='audio/mp3')
                        
        except Exception as e:
            st.error(f"❌ Eroare: {e}")
