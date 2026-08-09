"""
ידיעון בארות יצחק — נגן קול
Flask + PostgreSQL (Railway) | Web Speech API (iOS)
"""
import os, re, json, secrets
from datetime import datetime, timedelta
from itertools import groupby
from flask import Flask, request, jsonify, render_template_string, session, make_response
import pdfplumber
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=365)

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ─── DB ───────────────────────────────────────────────────────────────────────

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS issues (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS segments (
            id SERIAL PRIMARY KEY,
            issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS listener_state (
            id INTEGER PRIMARY KEY DEFAULT 1,
            issue_id INTEGER,
            segment_position INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS abbreviations (
            abbr TEXT PRIMARY KEY,
            expansion TEXT NOT NULL DEFAULT '',
            count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS users (
            id            SERIAL PRIMARY KEY,
            name          TEXT UNIQUE NOT NULL,
            active        BOOLEAN NOT NULL DEFAULT TRUE,
            issue_id      INTEGER REFERENCES issues(id) ON DELETE SET NULL,
            segment_pos   INTEGER NOT NULL DEFAULT 0,
            chunk_pos     INTEGER NOT NULL DEFAULT 0,
            play_speed    REAL NOT NULL DEFAULT 1.0,
            show_greeting BOOLEAN NOT NULL DEFAULT TRUE,
            last_seen     TIMESTAMP
        );
        INSERT INTO listener_state (id, issue_id, segment_position)
        VALUES (1, NULL, 0)
        ON CONFLICT (id) DO NOTHING;
    """)
    # Seed known abbreviations (without expansion — user will fill via voice)
    known = [
        '\u05d1"\u05d4', '\u05d3"\u05e8', '\u05e6\u05d4"\u05dc',
        '\u05d1\u05e2"\u05de', '\u05d6"\u05dc', '\u05e9\u05dc\u05d9\u05d8"\u05d0',
        '\u05d6\u05e6"\u05dc', '\u05de"\u05de', '\u05ea"\u05ea',
        '\u05db"\u05e7', '\u05de\u05e8"\u05df',
    ]
    for a in known:
        cur.execute(
            "INSERT INTO abbreviations (abbr, expansion, count) VALUES (%s, '', 0) ON CONFLICT (abbr) DO NOTHING",
            (a,)
        )
    # Seed letter names with __ prefix (editable by admin, not shown as unresolved)
    letter_names = [
        ('__\u05d0', '\u05d0\u05dc\u05e3'), ('__\u05d1', '\u05d1\u05d9\u05ea'),
        ('__\u05d2', '\u05d2\u05d9\u05de\u05dc'), ('__\u05d3', '\u05d3\u05dc\u05ea'),
        ('__\u05d4', '\u05d4\u05d0'), ('__\u05d5', '\u05d5\u05d5'),
        ('__\u05d6', 'zayin'), ('__\u05d7', '\u05d7\u05ea'),
        ('__\u05d8', '\u05d8\u05d9\u05ea'), ('__\u05d9', '\u05d9\u05d5\u05d3'),
        ('__\u05db', '\u05db\u05e3'), ('__\u05da', '\u05db\u05e3'),
        ('__\u05dc', '\u05dc\u05de\u05d3'), ('__\u05de', '\u05de\u05dd'),
        ('__\u05dd', '\u05de\u05dd'), ('__\u05e0', '\u05e0\u05d5\u05df'),
        ('__\u05df', '\u05e0\u05d5\u05df'), ('__\u05e1', '\u05e1\u05de\u05da'),
        ('__\u05e2', '\u05e2\u05d9\u05df'), ('__\u05e4', '\u05e4\u05d0'),
        ('__\u05e3', '\u05e4\u05d0'), ('__\u05e6', '\u05e6\u05d3\u05d9'),
        ('__\u05e5', '\u05e6\u05d3\u05d9'), ('__\u05e7', '\u05e7\u05d5\u05e3'),
        ('__\u05e8', '\u05e8\u05d9\u05e9'), ('__\u05e9', '\u05e9\u05d9\u05df'),
        ('__\u05ea', '\u05ea\u05d5'),
    ]
    for abbr, exp in letter_names:
        cur.execute(
            "INSERT INTO abbreviations (abbr, expansion, count) VALUES (%s, %s, 0) ON CONFLICT (abbr) DO NOTHING",
            (abbr, exp)
        )
    conn.commit()
    cur.close()
    conn.close()

# ─── Abbreviation detection (for PDF upload) ─────────────────────────────────
# Gershayim (״ or ") must appear before the LAST letter: e.g. צה"ל — valid; מ"שהו — invalid
# Geresh (׳ or ') after a single letter: ר' → valid abbreviation
_GERSHAYIM_RE = re.compile(r'[\u05d0-\u05ea]+["\u05f4][\u05d0-\u05ea]+')
_GERESH_RE    = re.compile(r'(?<!\S)([\u05d0-\u05ea])[\'\u05f3](?=\s|$)')

def _gershayim_valid(word: str) -> bool:
    """Gershayim is valid only when it appears before the last letter (standard acronym)."""
    # Strip gershayim chars and find position
    for g in ('"', '\u05f4'):
        idx = word.find(g)
        if idx != -1:
            letters_after = word[idx+1:]
            # Valid: exactly one Hebrew letter after gershayim
            if len(letters_after) == 1 and '\u05d0' <= letters_after <= '\u05ea':
                return True
            return False
    return False

def _is_hebrew_year(word: str) -> bool:
    """4-letter word starting with ת + century letter — it's a year, skip."""
    century = 'שרקצנמלכי'
    return len(word) == 4 and word[0] == '\u05ea' and word[1] in century

def extract_abbreviations(text: str) -> dict:
    """Return {abbr: count} for valid acronyms in text (excluding years)."""
    counts = {}
    # Gershayim words — only if gershayim is before last letter
    for m in _GERSHAYIM_RE.finditer(text):
        w = m.group(0)
        if not _gershayim_valid(w):
            continue
        letters = w.replace('"', '').replace('\u05f4', '')
        if _is_hebrew_year(letters):
            continue
        counts[w] = counts.get(w, 0) + 1
    # Geresh after single letter: ר׳ / ר'
    for m in _GERESH_RE.finditer(text):
        letter = m.group(1)
        key = letter + "'"          # store with plain apostrophe
        counts[key] = counts.get(key, 0) + 1
    return counts

def upsert_abbreviations(counts: dict):
    """Insert new abbrevs with count; update count for existing ones."""
    if not counts:
        return
    conn = get_db(); cur = conn.cursor()
    for abbr, cnt in counts.items():
        cur.execute("""
            INSERT INTO abbreviations (abbr, expansion, count)
            VALUES (%s, '', %s)
            ON CONFLICT (abbr) DO UPDATE SET count = abbreviations.count + EXCLUDED.count
        """, (abbr, cnt))
    conn.commit(); cur.close(); conn.close()

# ─── PDF PROCESSING ──────────────────────────────────────────────────────────

TAIL_ORDER = ["הקיבוץ הדתי", "לוח זמנים", "כלבודף"]

PARASHA_RE = re.compile(
    r"(?<![א-ת])(בראשית|נח|לך לך|וירא|חיי שרה|תולדות|ויצא|וישלח|וישב|מקץ|ויגש|ויחי|"
    r"שמות|וארא|בא|בשלח|יתרו|משפטים|תרומה|תצוה|כי תשא|ויקהל|פקודי|"
    r"ויקרא|צו|שמיני|תזריע|מצורע|אחרי מות|אחרי|קדושים|אמור|בהר|בחוקותי|"
    r"במדבר|נשא|בהעלותך|שלח|קרח|חוקת|בלק|פינחס|מטות|מסעי|"
    r"דברים|ואתחנן|עקב|ראה|שופטים|כי תצא|כי תבוא|נצבים|וילך|האזינו|וזאת הברכה)(?![א-ת])"
)
# Special parshiyot / additions that can follow a parasha name with a dash
SPECIAL_RE = re.compile(r"(שקלים|זכור|פרה|החודש|הגדול|שובה|ראש השנה|יום כיפור)")

# Nikud (U+05B0-U+05C7) + Taamei hamikra (U+0591-U+05AF) — remove without leaving spaces
NIKUD_RE = re.compile(r"[\u0591-\u05c7]")
_PH = "\x00"  # placeholder — never appears in Hebrew text
_HE_CHAR = re.compile(r"^[\u05d0-\u05ea]$")

def strip_nikud(text: str) -> str:
    # Replace each diacritic with placeholder, then remove placeholder + adjacent spaces.
    text = NIKUD_RE.sub(_PH, text)
    text = re.sub(r" ?" + re.escape(_PH) + r" ?", "", text)
    return text

# ── Hebrew number words for time normalization ────────────────────────────────
_HE_HOURS = ['אפס','אחת','שתיים','שלוש','ארבע','חמש','שש','שבע','שמונה','תשע','עשר',
    'אחת עשרה','שתים עשרה','שלוש עשרה','ארבע עשרה','חמש עשרה','שש עשרה',
    'שבע עשרה','שמונה עשרה','תשע עשרה','עשרים','עשרים ואחת','עשרים ושתיים','עשרים ושלוש']
_HE_MINUTES = ['','ואחת','ושתיים','ושלוש','וארבע','וחמש','ושש','ושבע',
    'ושמונה','ותשע','ועשר','ואחת עשרה','ושתים עשרה','ושלוש עשרה','וארבע עשרה',
    'וחמש עשרה','ושש עשרה','ושבע עשרה','ושמונה עשרה','ותשע עשרה','ועשרים',
    'ועשרים ואחת','ועשרים ושתיים','ועשרים ושלוש','ועשרים וארבע','ועשרים וחמש',
    'ועשרים ושש','ועשרים ושבע','ועשרים ושמונה','ועשרים ותשע','ושלושים',
    'ושלושים ואחת','ושלושים ושתיים','ושלושים ושלוש','ושלושים וארבע',
    'ושלושים וחמש','ושלושים ושש','ושלושים ושבע','ושלושים ושמונה',
    'ושלושים ותשע','וארבעים','וארבעים ואחת','וארבעים ושתיים','וארבעים ושלוש',
    'וארבעים וארבע','וארבעים וחמש','וארבעים ושש','וארבעים ושבע',
    'וארבעים ושמונה','וארבעים ותשע','וחמישים','וחמישים ואחת',
    'וחמישים ושתיים','וחמישים ושלוש','וחמישים וארבע','וחמישים וחמש',
    'וחמישים ושש','וחמישים ושבע','וחמישים ושמונה','וחמישים ותשע']

_TIME_RE = re.compile(r'(?<!\d)(\d{1,2}):(\d{1,2})(?!\d)')

def _time_to_hebrew(m: re.Match) -> str:
    """Convert HH:MM to Hebrew words. PDF RTL sometimes reverses the digits."""
    a, b = int(m.group(1)), int(m.group(2))
    # a:b — decide which is hours and which is minutes
    # Valid time: hours 0-23, minutes 0-59
    a_valid = a <= 23 and b <= 59
    b_valid = b <= 23 and a <= 59
    if a_valid and not b_valid:
        h_num, min_num = a, b          # normal order
    elif b_valid and not a_valid:
        h_num, min_num = b, a          # PDF RTL swap
    elif a_valid and b_valid:
        # Both valid — PDF often reverses, so prefer swap when a > 23 is impossible
        # Heuristic: if a > b it's more likely h:m (e.g. 17:26 not 26:17)
        # But PDF gives us reversed, so if a looks like minutes (>23) swap
        h_num, min_num = (b, a) if a > 23 else (a, b)
    else:
        return m.group(0)  # neither valid, keep original
    h_str = _HE_HOURS[h_num] if h_num < len(_HE_HOURS) else str(h_num)
    m_str = (' ' + _HE_MINUTES[min_num]) if min_num > 0 else ''
    return h_str + m_str

def normalize_text_for_storage(text: str) -> str:
    """
    Normalize text for storage and display — mirrors the non-ABBR_DICT parts
    of normalizeForSpeech() in JS.  Called on segment body/title before saving.
    ABBR_DICT expansion (ראשי תיבות) is intentionally left to the JS side.
    """
    # 1. Convert HH:MM time expressions to Hebrew words FIRST (before removing colons)
    text = _TIME_RE.sub(_time_to_hebrew, text)
    # 2. Remove characters iOS TTS reads as letter names
    text = re.sub(r'[(){}\[\]<>]', ' ', text)
    # 3. Remove symbols: /, \, |, ~, ^, @, #, *, +, =, %, &, $, `
    text = re.sub(r'[/\\|~^@#*+=&$%`]', ' ', text)
    # 4. Collapse multiple spaces / trim lines
    lines = []
    for line in text.split('\n'):
        lines.append(re.sub(r' {2,}', ' ', line).strip())
    return '\n'.join(lines)

def rejoin_spaced_letters(line: str) -> str:
    """If a line consists almost entirely of single Hebrew letters (with optional punctuation), join the letters."""
    tokens = [t for t in line.split(" ") if t]
    if not tokens:
        return line
    he_count = sum(1 for t in tokens if _HE_CHAR.match(t))
    # If at least 70% of tokens are single Hebrew letters, it's a spaced-letter line
    if he_count >= 2 and he_count / len(tokens) >= 0.7:
        # Join only the Hebrew letter tokens, keep punctuation with a space
        result = []
        buf = []
        for tok in tokens:
            if _HE_CHAR.match(tok):
                buf.append(tok)
            else:
                if buf:
                    result.append("".join(buf))
                    buf = []
                result.append(tok)
        if buf:
            result.append("".join(buf))
        return " ".join(result)
    return line


def fix_rtl_line(line: str) -> str:
    """Reverse line for RTL fix, but also fix digit sequences that got reversed."""
    reversed_line = line[::-1]
    # Fix numbers: sequences of digits that were reversed need to be re-reversed
    def fix_num(m):
        return m.group(0)[::-1]
    return re.sub(r"\d+", fix_num, reversed_line)

# Fonts that lack unicode mapping — OCR needed
_SCRIPT_FONTS = {"GuttmanYad-Brush", "GuttmanYad", "GuttmanYadBrush", "GuttmanKav"}

def _ocr_page_region(fitz_page, bbox_rect):
    """Render a region of a page and OCR it with Hebrew tesseract."""
    try:
        import pytesseract
        from PIL import Image
        import fitz as _fitz
        mat = _fitz.Matrix(3, 3)  # ~216 DPI
        pix = fitz_page.get_pixmap(matrix=mat, clip=bbox_rect)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        text = pytesseract.image_to_string(img, lang="heb", config="--psm 6")
        return text.strip()
    except Exception:
        return ""

def extract_text_from_pdf(path: str) -> str:
    import fitz as _fitz
    doc = _fitz.open(path)
    pages = []

    for page in doc:
        page_lines = []
        # Collect spans by line (group by approximate y)
        blocks = page.get_text("dict")["blocks"]
        y_spans = {}  # y_bucket -> list of (x, text)
        for b in blocks:
            if "lines" not in b:
                continue
            for line in b["lines"]:
                y = round(line["bbox"][1] / 5) * 5  # bucket by 5pt
                for span in line["spans"]:
                    fname = span["font"]
                    txt = span["text"]
                    # If this is a script font with no unicode content, OCR it
                    if fname in _SCRIPT_FONTS or (not txt.strip() and any(sf in fname for sf in _SCRIPT_FONTS)):
                        # Expand bbox slightly for context
                        r = _fitz.Rect(span["bbox"]).inflate(5)
                        # Expand to full line height
                        r = _fitz.Rect(0, r.y0 - 10, page.rect.width, r.y1 + 10)
                        ocr_text = _ocr_page_region(page, r)
                        txt = ocr_text if ocr_text else txt
                    if y not in y_spans:
                        y_spans[y] = []
                    y_spans[y].append((span["bbox"][0], txt))

        # Reconstruct lines sorted by y, then x (RTL: reverse x)
        for y in sorted(y_spans.keys()):
            spans = sorted(y_spans[y], key=lambda s: s[0], reverse=True)
            line_text = " ".join(s[1] for s in spans if s[1].strip())
            if line_text.strip():
                page_lines.append(line_text)

        raw = "\n".join(page_lines)
        lines = raw.split("\n")
        fixed = [rejoin_spaced_letters(fix_rtl_line(l)) for l in lines]
        pages.append("\n".join(fixed))

    full = "\n\n".join(pages)
    full = re.sub(r"\n\d{1,3}\n", "\n", full)   # strip page numbers
    full = re.sub(r"[■●•◆▪]", "", full)          # strip bullets
    full = re.sub(r"\n{3,}", "\n\n", full)
    full = strip_nikud(full)
    return full.strip()


def extract_text_from_docx(path: str) -> str:
    """Extract text from a .docx file, preserving article structure, without duplicating text boxes."""
    from docx import Document as _Document

    doc = _Document(path)
    body = doc.element.body
    NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'

    def elem_text(elem):
        return ''.join(n.text or '' for n in elem.iter()
                       if n.tag.endswith('}t') or n.tag == 't')

    def para_is_bold(p_elem):
        runs = p_elem.findall('.//{%s}r' % NS)
        if not runs: return False
        bold_runs = sum(1 for r in runs if r.find('.//{%s}b' % NS) is not None)
        return bold_runs > len(runs) // 2

    def clean(text):
        text = re.sub(r"['\u05f3\"\u05f4\u05f4\u05f4\u05f4]", '', text)  # geresh
        text = text.replace('\u05f4', '').replace('\u05f3', '')
        text = text.replace('"', '').replace("'", '')
        text = re.sub(r'\u05f4|\u05f3|[״\'"]', '', text)  # all geresh variants
        text = re.sub(r'■', ' __ARTICLE_END__ ', text)
        # Collapse spaced Hebrew letters: ו א ת ח נ ן → ואתחנן
        for _ in range(10):
            new = re.sub(r'(?<!\S)([א-ת]) ([א-ת])(?!\S)', r'\1\2', text)
            if new == text: break
            text = new
        return text.strip()

    # Collect all text-box content first (to detect duplicates)
    # Word stores txbx text in <w:txbxContent> AND repeats it as inline fallback
    seen_txbx_keys = set()
    txbx_chunks = []
    for elem in body.iter():
        if 'txbxContent' in (elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag):
            txt = clean(elem_text(elem))
            key = txt[:100]
            if txt and key not in seen_txbx_keys:
                seen_txbx_keys.add(key)
                txbx_chunks.append(txt)

    chunks = []
    for child in body:
        tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag

        if tag == 'p':
            # Skip paragraphs that are inside text boxes (already collected)
            is_txbx = any('txbxContent' in (a.tag.split('}')[-1] if '}' in a.tag else a.tag)
                          for a in child.iter())
            if is_txbx:
                continue
            txt = clean(elem_text(child))
            if not txt:
                continue
            # Skip if this paragraph's text matches a txbx we already have
            if txt[:100] in seen_txbx_keys:
                continue
            bold = para_is_bold(child)
            short = len(txt) <= 60
            if bold and short:
                chunks.append('')
                chunks.append(txt)
                chunks.append('')
            else:
                chunks.append(txt)

        elif tag == 'tbl':
            seen_cells = set()
            for row in child.findall('.//{%s}tr' % NS):
                for cell in row.findall('.//{%s}tc' % NS):
                    # Each paragraph in cell on its own line
                    for p in cell.findall('.//{%s}p' % NS):
                        pt = clean(elem_text(p))
                        if pt and pt not in seen_cells:
                            seen_cells.add(pt)
                            chunks.append(pt)
            chunks.append('')

    full = '\n'.join(txbx_chunks + [''] + chunks)
    full = re.sub(r'\n{3,}', '\n\n', full)
    full = strip_nikud(full)
    return full.strip()


def extract_raw_head(path: str) -> str:
    with pdfplumber.open(path) as pdf:
        raw = pdf.pages[0].extract_text() or ""
    lines = raw.split("\n")
    flipped = "\n".join(fix_rtl_line(l) for l in lines)
    return strip_nikud(flipped)


_PARASHA_LIST = [
    "בראשית","נח","לך לך","וירא","חיי שרה","תולדות","ויצא","וישלח","וישב","מקץ","ויגש","ויחי",
    "שמות","וארא","בא","בשלח","יתרו","משפטים","תרומה","תצוה","כי תשא","כי תישא","ויקהל","פקודי",
    "ויקרא","צו","שמיני","תזריע","מצורע","אחרי מות","אחרי","קדושים","אמור","בהר","בחוקותי",
    "במדבר","נשא","בהעלותך","שלח","קרח","חוקת","בלק","פינחס","מטות","מסעי",
    "דברים","ואתחנן","עקב","ראה","שופטים","כי תצא","כי תבוא","נצבים","וילך","האזינו","וזאת הברכה",
]
_SPECIAL_LIST = ["שקלים","זכור","פרה","החודש","הגדול","שובה"]
_ALL_PARASHA = _PARASHA_LIST + _SPECIAL_LIST
# Regex matching parasha names with optional spaces between letters (for spaced-PDF titles)
def _make_spaced_pattern(name: str) -> str:
    # Each char can have \s* around it; spaces within name become \s+
    parts = []
    for c in name:
        if c == " ":
            parts.append(r"\s+")
        else:
            if parts and parts[-1] not in (r"\s+",):
                parts.append(r"\s*")
            parts.append(re.escape(c))
    return "".join(parts)
_PARASHA_SPACED_RE = re.compile(
    r"(?<![א-ת])(" + "|".join(_make_spaced_pattern(p) for p in _PARASHA_LIST) + r")(?![א-ת])"
)
_SPECIAL_SPACED_RE = re.compile(
    r"(?<![א-ת])(" + "|".join(_make_spaced_pattern(p) for p in _SPECIAL_LIST) + r")(?![א-ת])"
)

def _normalize_parasha(raw: str) -> str:
    """Remove spaces from matched (possibly spaced) parasha name and look it up."""
    compact = re.sub(r"\s+", "", raw)
    # Map compact form back to display name
    mapping = {re.sub(r"\s+","",p): p for p in _PARASHA_LIST + _SPECIAL_LIST}
    # Handle כי תישא -> כי תשא
    mapping["כיתישא"] = "כי תשא"
    return mapping.get(compact, re.sub(r"\s+", " ", raw).strip())

def detect_title(text: str, raw_head: str = "") -> str:
    head = strip_nikud(text[:1500])  # wider search window for docx
    search_head = strip_nikud(raw_head) if raw_head else head
    # Issue number — search entire head (docx text boxes may appear out of order)
    m3 = re.search(r"גיליון\s+(\d+)", head)
    issue_num = m3.group(1) if m3 else None
    m = _PARASHA_SPACED_RE.search(search_head)
    if m:
        parasha = _normalize_parasha(m.group(1))
        rest = search_head[m.end():m.end()+80]
        m2 = re.search(r"\s*[-–]\s*(" + "|".join(_make_spaced_pattern(p) for p in _ALL_PARASHA) + r")", rest)
        if m2:
            parasha += " - " + _normalize_parasha(m2.group(1))
        if issue_num:
            return "ידיעון " + parasha + " גיליון " + issue_num
        return "ידיעון " + parasha
    if issue_num:
        return "גיליון " + issue_num
    return "ידיעון " + datetime.now().strftime("%d.%m.%Y")


# ── Article boundary detection ────────────────────────────────────────────────
# A new article starts when we see a short line (likely a heading) that is
# followed by body text. We detect headings as: line <= 40 chars, not purely
# punctuation/numbers, standing alone (preceded and followed by blank line or
# short line).

def is_heading(line: str, prev_blank: bool) -> bool:
    line = line.strip()
    if not line:
        return False
    if len(line) > 50:
        return False
    # Must have Hebrew letters
    if not re.search(r"[\u05d0-\u05ea]", line):
        return False
    # Common heading indicators: short, possibly after blank line
    return True

def split_into_articles(text: str) -> list[dict]:
    """Split text into articles. Each article = heading + body block."""
    lines = text.split("\n")
    articles = []
    current_heading = "פתיח"
    current_body_lines = []

    def flush_article():
        body = "\n".join(current_body_lines).strip()
        if body or current_heading != "פתיח":
            articles.append({
                "heading": current_heading,
                "body": body,
                "words": len(body.split())
            })

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # Detect article boundary: short non-empty line after blank
        prev_blank = (i > 0 and not lines[i-1].strip())
        next_blank = (i < len(lines)-1 and not lines[i+1].strip()) if i < len(lines)-1 else True

        if (prev_blank or i == 0) and is_heading(line, prev_blank) and len(line) <= 40:
            # Could be a new heading — check if next non-blank line is longer (body)
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            next_content = lines[j].strip() if j < len(lines) else ""
            # If next content is substantially longer, this is a heading
            if not next_content or len(next_content) > len(line):
                flush_article()
                current_heading = line
                current_body_lines = []
                i += 1
                continue

        current_body_lines.append(lines[i])
        i += 1

    flush_article()
    return articles


def detect_tail_section(heading: str, body: str) -> str | None:
    """Return TAIL_ORDER section name if this article belongs to one."""
    combined = heading + " " + body[:200]
    if re.search(r"הקיבוץ הדתי|התנועה ואנחנו", combined):
        return "הקיבוץ הדתי"
    if re.search(r"לוח זמנים|זמני התפילות|הדלקת נרות|מנחה.*שחרית|שחרית.*מנחה", combined):
        return "לוח זמנים"
    if re.search(r"^כלבודף|כלבודף$", heading.strip()):
        return "כלבודף"
    return None


def split_segments(text: str) -> list[dict]:
    MAX_WORDS = 700   # target max per segment

    articles = split_into_articles(text)

    # Classify each article
    main_articles = []
    tail_dict = {t: [] for t in TAIL_ORDER}

    for art in articles:
        tail = detect_tail_section(art["heading"], art["body"])
        if tail:
            tail_dict[tail].append(art)
        else:
            main_articles.append(art)

    # Pack main articles into segments of ~MAX_WORDS
    # Never split an article across segments
    segments = []
    buf_headings = []
    buf_body_parts = []
    buf_words = 0

    def flush_main():
        nonlocal buf_headings, buf_body_parts, buf_words
        if not buf_body_parts and not buf_headings:
            return
        seg_num = len(segments) + 1
        # Title = segment number + article names
        names = [h for h in buf_headings if h and h != "פתיח"]
        if names:
            title = str(seg_num) + ". " + " / ".join(names)
        else:
            title = str(seg_num) + ". פתיח"
        body = "\n\n".join(buf_body_parts)
        segments.append({"title": title, "body": body})
        buf_headings = []
        buf_body_parts = []
        buf_words = 0

    for art in main_articles:
        w = art["words"]
        # If this single article exceeds MAX, flush what we have and add it alone
        if buf_words > 0 and buf_words + w > MAX_WORDS:
            flush_main()
        heading_line = art["heading"] if art["heading"] != "פתיח" else ""
        buf_headings.append(art["heading"])
        buf_body_parts.append((heading_line + "\n" + art["body"]).strip())
        buf_words += w
        # If we've hit the target, flush
        if buf_words >= MAX_WORDS:
            flush_main()

    flush_main()

    # Add tail sections in order (only if they have content)
    for t in TAIL_ORDER:
        arts = tail_dict[t]
        if not arts:
            continue
        seg_num = len(segments) + 1
        title = str(seg_num) + ". " + t
        body = "\n\n".join(
            (a["heading"] + "\n" + a["body"]).strip() for a in arts
        )
        segments.append({"title": title, "body": body})

    return segments

# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.route("/")
def listener():
    return render_template_string(LISTENER_HTML)

@app.route("/admin")
def admin():
    return render_template_string(ADMIN_HTML)

@app.route("/admin/users")
def admin_users():
    return render_template_string(USERS_ADMIN_HTML)

@app.route("/api/users")
def get_users():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""SELECT u.*, i.title as issue_title,
                   s.title as segment_title
                   FROM users u
                   LEFT JOIN issues i ON i.id=u.issue_id
                   LEFT JOIN segments s ON s.issue_id=u.issue_id AND s.position=u.segment_pos
                   ORDER BY u.last_seen DESC NULLS LAST""")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/users/save", methods=["POST"])
def save_user():
    data = request.json
    conn = get_db(); cur = conn.cursor()
    if data.get("id"):
        new_issue_id = data.get("issue_id") or None
        # If issue changed, reset segment position to 0
        cur.execute("SELECT issue_id FROM users WHERE id=%s", (data["id"],))
        existing = cur.fetchone()
        old_issue_id = existing["issue_id"] if existing else None
        seg_pos = 0 if new_issue_id != old_issue_id else None
        if seg_pos is not None:
            cur.execute("""UPDATE users SET name=%s, active=%s, play_speed=%s,
                           show_greeting=%s, issue_id=%s, segment_pos=0, chunk_pos=0
                           WHERE id=%s""",
                        (data["name"], data["active"], data["play_speed"],
                         data["show_greeting"], new_issue_id, data["id"]))
        else:
            explicit_pos = data.get("segment_pos")
            if explicit_pos is not None:
                cur.execute("""UPDATE users SET name=%s, active=%s, play_speed=%s,
                               show_greeting=%s, issue_id=%s, segment_pos=%s, chunk_pos=0
                               WHERE id=%s""",
                            (data["name"], data["active"], data["play_speed"],
                             data["show_greeting"], new_issue_id,
                             int(explicit_pos), data["id"]))
            else:
                cur.execute("""UPDATE users SET name=%s, active=%s, play_speed=%s,
                               show_greeting=%s, issue_id=%s WHERE id=%s""",
                            (data["name"], data["active"], data["play_speed"],
                             data["show_greeting"], new_issue_id, data["id"]))
    else:
        issue_id = get_latest_issue_id()
        cur.execute("""INSERT INTO users (name, active, issue_id, segment_pos,
                       chunk_pos, play_speed, show_greeting)
                       VALUES (%s, %s, %s, 0, 0, %s, %s) RETURNING id""",
                    (data["name"], data.get("active", True), issue_id,
                     data.get("play_speed", 1.0), data.get("show_greeting", True)))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/users/set_speed", methods=["POST"])
def set_speed():
    user = get_current_user()
    if not user: return jsonify({"ok": False})
    data = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE users SET play_speed=%s WHERE id=%s",
                (data["speed"], user["id"]))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/users/delete", methods=["POST"])
def delete_user():
    data = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id=%s", (data["id"],))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/upload", methods=["POST"])
def upload():
    if "pdf" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["pdf"]
    fname = f.filename or ""
    is_docx = fname.lower().endswith(".docx")
    tmp = "/tmp/upload.docx" if is_docx else "/tmp/upload.pdf"
    f.save(tmp)

    try:
        if is_docx:
            text = extract_text_from_docx(tmp)
            # For docx, use first non-empty paragraph as title hint
            raw_head = text[:500]
        else:
            text = extract_text_from_pdf(tmp)
            raw_head = extract_raw_head(tmp)
        title = detect_title(text, raw_head)
        segments = split_segments(text)

        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO issues (title, created_at) VALUES (%s, %s) RETURNING id",
                    (title, datetime.now().isoformat()))
        issue_id = cur.fetchone()["id"]
        for i, seg in enumerate(segments):
            clean_title = normalize_text_for_storage(seg["title"])
            clean_body  = normalize_text_for_storage(seg["body"])
            cur.execute(
                "INSERT INTO segments (issue_id, position, title, body) VALUES (%s,%s,%s,%s)",
                (issue_id, i, clean_title, clean_body)
            )
        cur.execute("UPDATE listener_state SET issue_id=%s, segment_position=0 WHERE id=1",
                    (issue_id,))
        conn.commit()
        cur.close(); conn.close()

        # Extract and upsert abbreviations found in this issue
        abbr_counts = extract_abbreviations(text)
        upsert_abbreviations(abbr_counts)

        return jsonify({"ok": True, "issue_id": issue_id, "title": title,
                        "segments": len(segments),
                        "preview": [s["title"] for s in segments]})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ─── User helpers ────────────────────────────────────────────────────────────

def get_current_user():
    """Return user row from session cookie, or None."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
    user = cur.fetchone()
    cur.close(); conn.close()
    return user

def get_latest_issue_id():
    """Return id of most recently uploaded issue, or None."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id FROM issues ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    cur.close(); conn.close()
    return row["id"] if row else None

# ─── Auth routes ─────────────────────────────────────────────────────────────

@app.route("/api/whoami")
def whoami():
    user = get_current_user()
    if not user:
        return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, "name": user["name"], "id": user["id"],
                    "play_speed": user["play_speed"],
                    "show_greeting": user["show_greeting"]})

@app.route("/api/login_check", methods=["POST"])
def login_check():
    """Check if a username exists, without logging in."""
    name = (request.json.get("name") or "").strip()
    if not name:
        return jsonify({"exists": False})
    conn = get_db(); cur = conn.cursor()
    # Case-insensitive + strip to handle slight TTS variations
    cur.execute("SELECT id FROM users WHERE lower(trim(name))=lower(%s)", (name,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return jsonify({"exists": bool(row)})

@app.route("/api/login", methods=["POST"])
def login():
    name = (request.json.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "no_name"})
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE lower(trim(name))=lower(%s)", (name,))
    user = cur.fetchone()
    if user:
        if not user["active"]:
            cur.close(); conn.close()
            return jsonify({"ok": False, "error": "inactive"})
        # Update last_seen
        cur.execute("UPDATE users SET last_seen=%s WHERE id=%s",
                    (datetime.now(), user["id"]))
        conn.commit()
        cur.close(); conn.close()
        session.permanent = True
        session["user_id"] = user["id"]
        return jsonify({"ok": True, "name": user["name"], "new_user": False,
                        "play_speed": user["play_speed"],
                        "show_greeting": user["show_greeting"]})
    else:
        # New user — create automatically
        issue_id = get_latest_issue_id()
        cur.execute("""
            INSERT INTO users (name, active, issue_id, segment_pos, chunk_pos,
                               play_speed, show_greeting, last_seen)
            VALUES (%s, TRUE, %s, 0, 0, 1.0, TRUE, %s)
            RETURNING id
        """, (name, issue_id, datetime.now()))
        new_id = cur.fetchone()["id"]
        conn.commit(); cur.close(); conn.close()
        session.permanent = True
        session["user_id"] = new_id
        return jsonify({"ok": True, "name": name, "new_user": True,
                        "play_speed": 1.0, "show_greeting": True})

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})

# ─── Current issue/position (per user) ───────────────────────────────────────

@app.route("/api/current")
def current():
    user = get_current_user()
    if not user:
        return jsonify({"need_login": True})
    if not user["issue_id"]:
        return jsonify({"no_issue": True})
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM issues WHERE id=%s", (user["issue_id"],))
    issue = cur.fetchone()
    if not issue:
        cur.close(); conn.close()
        return jsonify({"no_issue": True})
    cur.execute("SELECT * FROM segments WHERE issue_id=%s ORDER BY position",
                (user["issue_id"],))
    segs = cur.fetchall()
    cur.close(); conn.close()
    return jsonify({
        "issue_title": issue["title"],
        "issue_id": issue["id"],
        "segments": [{"position": s["position"], "title": s["title"],
                       "body": s["body"]} for s in segs],
        "current_position": user["segment_pos"],
        "chunk_pos": user["chunk_pos"],
        "total": len(segs)
    })

@app.route("/api/set_position", methods=["POST"])
def set_position():
    user = get_current_user()
    if not user: return jsonify({"ok": False})
    data = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE users SET segment_pos=%s, chunk_pos=%s WHERE id=%s",
                (data.get("position", 0), data.get("chunk", 0), user["id"]))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/set_issue", methods=["POST"])
def set_issue():
    user = get_current_user()
    if not user: return jsonify({"ok": False})
    data = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE users SET issue_id=%s, segment_pos=0, chunk_pos=0 WHERE id=%s",
                (data["issue_id"], user["id"]))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/activate_issue_all", methods=["POST"])
def activate_issue_all():
    """Set issue as active for ALL users, resetting their position to 0."""
    data = request.json
    issue_id = data.get("issue_id")
    if not issue_id:
        return jsonify({"ok": False})
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE users SET issue_id=%s, segment_pos=0, chunk_pos=0", (issue_id,))
    count = cur.rowcount
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True, "updated": count})

@app.route("/api/issues")
def issues():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT i.*, COUNT(s.id) as seg_count
        FROM issues i LEFT JOIN segments s ON s.issue_id = i.id
        GROUP BY i.id ORDER BY i.id DESC
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/update_issue", methods=["POST"])
def update_issue():
    data = request.json
    conn = get_db(); cur = conn.cursor()
    if "title" in data:
        cur.execute("UPDATE issues SET title=%s WHERE id=%s",
                    (data["title"], data["issue_id"]))
    if "description" in data:
        cur.execute("UPDATE issues SET description=%s WHERE id=%s",
                    (data["description"], data["issue_id"]))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/rename_segment", methods=["POST"])
def rename_segment():
    data = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE segments SET title=%s WHERE id=%s",
                (data["title"], data["segment_id"]))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/abbreviations")
def get_abbreviations():
    """Return all abbreviations, sorted: unresolved first, then resolved.
    Within each group: count DESC, then abbr ASC."""
    only_unresolved = request.args.get("unresolved") == "1"
    conn = get_db(); cur = conn.cursor()
    if only_unresolved:
        cur.execute("""SELECT abbr, expansion, count FROM abbreviations
                       WHERE expansion='' AND abbr NOT LIKE '\\_\\_%'
                       ORDER BY count DESC, abbr ASC""")
    else:
        cur.execute("""SELECT abbr, expansion, count FROM abbreviations
                       ORDER BY
                         CASE WHEN abbr LIKE '\\_\\_%' THEN 2 ELSE 0 END,
                         CASE WHEN expansion='' THEN 0 ELSE 1 END,
                         count DESC, abbr ASC""")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/abbreviations/save", methods=["POST"])
def save_abbreviation():
    data = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO abbreviations (abbr, expansion, count)
        VALUES (%s, %s, %s)
        ON CONFLICT (abbr) DO UPDATE SET expansion=EXCLUDED.expansion, count=EXCLUDED.count
    """, (data["abbr"], data.get("expansion",""), data.get("count",0)))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/abbreviations/delete", methods=["POST"])
def delete_abbreviation():
    data = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM abbreviations WHERE abbr=%s", (data["abbr"],))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/abbreviations/update", methods=["POST"])
def update_abbreviation():
    data = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE abbreviations SET expansion=%s WHERE abbr=%s",
                (data["expansion"], data["abbr"]))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/delete_issue", methods=["POST"])
def delete_issue():
    data = request.json
    issue_id = data["issue_id"]
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT issue_id FROM listener_state WHERE id=1")
    state = cur.fetchone()
    if state and state["issue_id"] == issue_id:
        cur.execute("UPDATE listener_state SET issue_id=NULL, segment_position=0 WHERE id=1")
    cur.execute("DELETE FROM issues WHERE id=%s", (issue_id,))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/segments/<int:issue_id>")
def get_segments(issue_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM segments WHERE issue_id=%s ORDER BY position", (issue_id,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/delete_segment", methods=["POST"])
def delete_segment():
    data = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM segments WHERE id=%s", (data["segment_id"],))
    cur.execute("""
        WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (ORDER BY position) - 1 as new_pos
            FROM segments WHERE issue_id=%s
        )
        UPDATE segments SET position=ranked.new_pos
        FROM ranked WHERE segments.id=ranked.id
    """, (data["issue_id"],))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/renormalize", methods=["POST", "GET"])
def renormalize():
    """Re-run normalize_text_for_storage on all existing segments (for already-uploaded issues)."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id, title, body FROM segments")
    rows = cur.fetchall()
    count = 0
    for row in rows:
        new_title = normalize_text_for_storage(row["title"])
        new_body  = normalize_text_for_storage(row["body"])
        if new_title != row["title"] or new_body != row["body"]:
            cur.execute("UPDATE segments SET title=%s, body=%s WHERE id=%s",
                        (new_title, new_body, row["id"]))
            count += 1
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True, "updated": count})

# ─── HTML TEMPLATES ──────────────────────────────────────────────────────────

LISTENER_HTML = """<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="/manifest.json">
<title>ידיעון בארות יצחק</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Heebo:wght@300;400;700;900&display=swap');
:root{
  --bg:#0f0f12; --surface:#1a1a20; --surface2:#22222c;
  --border:#2e2e3a; --accent:#e8c97a; --accent2:#7ab8e8;
  --text:#f0ede6; --muted:#777; --r:20px;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;background:var(--bg);color:var(--text);
  font-family:'Heebo',sans-serif;overflow:hidden}

/* ── LOADING SCREEN ── */
#ls{position:fixed;inset:0;background:var(--bg);display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:14px;z-index:99;
  font-size:17px;color:var(--muted);text-align:center;padding:36px}
#ls .em{font-size:52px}

/* ── BLIND SCREEN ── */
#blind{position:fixed;inset:0;background:#f5f2ec;display:none;
  flex-direction:column;align-items:center;justify-content:space-between;
  padding:env(safe-area-inset-top, 20px) 20px env(safe-area-inset-bottom, 20px);
  z-index:10}
#blind-top{display:flex;flex-direction:column;align-items:center;gap:6px;
  padding-top:12px;width:100%}
#blind-title{font-size:14px;color:#888;font-weight:700;
  letter-spacing:.08em;text-align:center}
#blind-seg{font-size:20px;font-weight:900;text-align:center;
  line-height:1.3;color:#1a1a18}
#blind-pi{display:flex;align-items:center;justify-content:center;
  gap:8px;height:20px;opacity:0;transition:opacity .3s}
#blind-pi.on{opacity:1}
#blind-pi .bars span{background:#2d5f3f}
#blind-status{font-size:15px;color:#666;text-align:center;
  min-height:22px}
#blind-phrase{font-size:13px;color:#555;text-align:center;
  min-height:52px;line-height:1.4;padding:0 12px;
  direction:rtl;font-style:italic;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
#vcbtn{flex:1;width:100%;max-width:100%;
  background:#2d5f3f;border:none;
  border-radius:28px;color:#fff;font-family:'Heebo',sans-serif;
  font-size:36px;font-weight:900;cursor:pointer;transition:all .2s;
  -webkit-user-select:none;user-select:none;
  display:flex;align-items:center;justify-content:center;
  margin:8px 0}
#vcbtn:active{transform:scale(.97);opacity:.9}
#vcbtn.listening{background:#c0392b;
  animation:pulse 1s ease-in-out infinite}
#vcbtn.ok{background:#1a7a40}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
#vcmsg{font-size:14px;color:#666;text-align:center;min-height:20px}
#blind-bottom{display:flex;flex-direction:column;align-items:center;
  gap:10px;width:100%;padding-bottom:8px}
#detail-btn{padding:14px 36px;background:transparent;
  border:2px solid #ccc;border-radius:99px;
  color:#888;font-family:'Heebo',sans-serif;
  font-size:16px;font-weight:700;cursor:pointer;transition:all .2s;width:100%}
#detail-btn:active{background:#e8e4dc}
#detail-phrase{font-size:13px;color:#888;text-align:center;
  min-height:18px;padding:2px 8px;direction:rtl;
  font-style:italic;border-bottom:1px solid var(--border);
  padding-bottom:6px;margin-bottom:2px}

/* ── DETAIL SCREEN ── */
#app{display:none;flex-direction:column;height:100dvh;max-width:520px;
  margin:0 auto;padding:max(env(safe-area-inset-top),14px) 16px
  max(env(safe-area-inset-bottom),14px);gap:10px}
#app.vis{display:flex}

/* back to blind button */
#back-blind{width:100%;padding:16px 20px;
  background:var(--accent);color:#111;
  border:none;border-radius:var(--r);
  font-family:'Heebo',sans-serif;font-size:18px;font-weight:900;
  cursor:pointer;flex-shrink:0;text-align:center;
  transition:opacity .2s;-webkit-user-select:none;user-select:none}
#back-blind:active{opacity:.85}
#switch-issue-btn{width:100%;padding:10px 20px;
  background:transparent;color:var(--muted);
  border:1px solid var(--border);border-radius:var(--r);
  font-family:'Heebo',sans-serif;font-size:15px;font-weight:700;
  cursor:pointer;flex-shrink:0;text-align:center;
  transition:opacity .2s;-webkit-user-select:none;user-select:none}
#switch-issue-btn:active{opacity:.7}
#user-bar{display:flex;align-items:center;gap:6px;padding:6px 0;
  font-size:13px;color:var(--muted);border-bottom:1px solid var(--border);margin-bottom:8px}
#user-name-lbl{flex:1;font-weight:700;color:var(--text)}

#hdr{display:flex;flex-direction:column;gap:3px}
#issue-lbl{font-size:11px;color:var(--accent);font-weight:700;
  letter-spacing:.08em;text-transform:uppercase}
#seg-lbl{font-size:21px;font-weight:900;line-height:1.2}
#pos-lbl{font-size:12px;color:var(--muted)}

#pbar{height:3px;background:var(--border);border-radius:99px;overflow:hidden;flex-shrink:0}
#pfill{height:100%;background:var(--accent);border-radius:99px;transition:width .4s ease;width:0}

#ta{flex:1;background:var(--surface);border-radius:var(--r);padding:20px;
  overflow-y:auto;border:1px solid var(--border);-webkit-overflow-scrolling:touch}
#body{font-size:19px;line-height:1.95;white-space:pre-wrap}

#pi{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--accent);
  opacity:0;transition:opacity .3s;height:18px;flex-shrink:0}
#pi.on{opacity:1}
.bars{display:flex;gap:3px;align-items:flex-end;height:14px}
.bars span{width:3px;background:var(--accent);border-radius:2px;
  animation:b .8s ease-in-out infinite}
.bars span:nth-child(2){animation-delay:.15s}
.bars span:nth-child(3){animation-delay:.3s}
@keyframes b{0%,100%{height:3px}50%{height:13px}}

#ctrl{display:grid;grid-template-columns:1fr 1.7fr 1fr;gap:10px;flex-shrink:0}
.btn{border:none;border-radius:var(--r);cursor:pointer;
  font-family:'Heebo',sans-serif;font-weight:700;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;
  -webkit-user-select:none;user-select:none;transition:transform .12s,background .2s}
.btn:active{transform:scale(.94)}
.nav{background:var(--surface2);color:var(--text);padding:22px 10px;border:1px solid var(--border)}
.nav .ic{font-size:26px}
.nav .lb{font-size:11px;color:var(--muted)}
.play{background:var(--accent);color:#111;padding:22px 10px;font-size:34px;border-radius:24px}
.play.on{background:var(--accent2)}

#spd{display:flex;gap:7px;justify-content:center;flex-shrink:0;padding-bottom:2px}
.sb{background:var(--surface2);border:1px solid var(--border);color:var(--muted);
  border-radius:99px;padding:8px 14px;font-size:14px;
  font-family:'Heebo',sans-serif;font-weight:700;cursor:pointer;transition:all .2s}
.sb.on{background:var(--accent);color:#111;border-color:var(--accent)}

/* list button */
#listbtn{position:fixed;top:max(env(safe-area-inset-top),14px);left:16px;
  background:var(--surface2);border:1px solid var(--border);
  color:var(--text);width:44px;height:44px;border-radius:12px;
  font-size:20px;cursor:pointer;z-index:10;
  display:none;align-items:center;justify-content:center}
#listbtn.vis{display:flex}

/* drawer */
#ov{position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:20;
  opacity:0;pointer-events:none;transition:opacity .3s}
#ov.o{opacity:1;pointer-events:all}
#drw{position:fixed;top:0;right:-100%;bottom:0;width:min(340px,90vw);
  background:var(--surface);z-index:21;transition:right .3s ease;
  display:flex;flex-direction:column;
  padding-top:max(env(safe-area-inset-top),0px)}
#drw.o{right:0}
#drwhd{padding:20px 20px 14px;font-size:18px;font-weight:900;
  border-bottom:1px solid var(--border)}
#segl{overflow-y:auto;flex:1;-webkit-overflow-scrolling:touch}
.si{padding:15px 20px;border-bottom:1px solid var(--border);cursor:pointer;
  display:flex;align-items:center;gap:11px;transition:background .15s}
.si:active{background:var(--surface2)}
.si.cur{background:var(--surface2)}
.si .n{font-size:12px;color:var(--muted);min-width:22px;text-align:center}
.si .nm{font-size:16px;font-weight:700}
.si.cur .nm{color:var(--accent)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--accent);flex-shrink:0;opacity:0}
.si.cur .dot{opacity:1}
</style>
</head>
<body>

<!-- Loading -->
<div id="ls"><div class="em">📖</div><div id="lmsg">טוען...</div></div>

<!-- End screen (shown on סיום command on iOS) -->
<div id="end-screen" style="display:none;position:fixed;inset:0;background:#1a1a18;
  z-index:999;flex-direction:column;align-items:center;justify-content:center;gap:24px">
  <div style="font-size:48px">🔇</div>
  <div style="font-size:22px;color:#fff;font-weight:900;text-align:center">סיום ההאזנה</div>
  <div style="font-size:16px;color:#aaa;text-align:center">ניתן לסגור את הדפדפן</div>
  <button onclick="document.getElementById('end-screen').style.display='none';showBlind();"
    style="margin-top:16px;padding:16px 32px;background:#2d5f3f;color:#fff;border:none;
    border-radius:16px;font-size:18px;font-family:Heebo,sans-serif;font-weight:700;cursor:pointer">
    חזור לאפליקציה
  </button>
</div>

<!-- Blind screen -->
<div id="blind">
  <div id="blind-top">
    <div id="blind-user-name" style="font-size:13px;color:#2d5f3f;font-weight:700;text-align:center;min-height:18px"></div>
    <div id="blind-title">ידיעון בארות יצחק</div>
    <div id="blind-seg">טוען...</div>
    <div id="blind-pi">
      <div class="bars"><span></span><span></span><span></span></div>
      <span style="font-size:13px;color:#2d5f3f">מקריא...</span>
    </div>
    <div id="blind-status"></div>
    <div id="blind-phrase"></div>
  </div>
  <button id="vcbtn" onclick="startListen()">דבר אלי</button>
  <div id="blind-bottom">
    <div id="vcmsg"></div>
    <button id="detail-btn" onclick="showDetail()">פרטים נוספים</button>
  </div>
</div>

<!-- Detail screen -->
<button id="listbtn" onclick="openD()">☰</button>
<div id="app">
  <button id="back-blind" onclick="showBlind()">🎙 הפעלה קולית</button>
  <button id="switch-issue-btn" onclick="startIssuePicker(false)">⇄ החלף ידיעון</button>
  <div id="user-bar">
    <span id="user-name-lbl"></span>
    <button onclick="switchUser()" style="font-size:12px;padding:4px 12px;background:transparent;border:1px solid var(--border,#ccc);border-radius:6px;cursor:pointer;margin-right:4px">החלף משתמש</button>
    <button onclick="doLogout()" style="font-size:12px;padding:4px 12px;background:transparent;border:1px solid var(--border,#ccc);border-radius:6px;cursor:pointer;margin-right:4px">התנתק</button>
    <button onclick="startLoginFlow(function(){sessionStorage.removeItem('greeted');load();})" style="font-size:12px;padding:4px 12px;background:transparent;border:1px solid var(--border,#ccc);border-radius:6px;cursor:pointer">התחבר</button>
  </div>
  <div id="hdr">
    <div id="issue-lbl">ידיעון</div>
    <div id="seg-lbl">טוען...</div>
    <div id="pos-lbl"></div>
  </div>
  <div id="pbar"><div id="pfill"></div></div>
  <div id="detail-phrase"></div>
  <div id="ta"><div id="body"></div></div>
  <div id="pi">
    <div class="bars"><span></span><span></span><span></span></div>
    <span>מקריא...</span>
  </div>
  <div id="ctrl">
    <button class="btn nav" onclick="nav(-1)">
      <span class="ic">⟪</span><span class="lb">קודם</span>
    </button>
    <button class="btn play" id="pb" onclick="toggle()">&#9654;</button>
    <button class="btn nav" onclick="nav(1)">
      <span class="ic">⟫</span><span class="lb">הבא</span>
    </button>
  </div>
  <div id="spd">
    <button class="sb" onclick="spd(.6)">x0.6</button>
    <button class="sb on" onclick="spd(1)">x1</button>
    <button class="sb" onclick="spd(1.2)">x1.2</button>
    <button class="sb" onclick="spd(1.5)">x1.5</button>
  </div>
</div>

<div id="ov" onclick="closeD()"></div>
<div id="drw">
  <div id="drwhd">כל הקטעים</div>
  <div id="segl"></div>
</div>

<script>
var S=null,synth=window.speechSynthesis,utt=null,playing=false,rate=1,heVoice=null,greetingActive=false,wakeLock=null;
var currentScreen='blind';
var chunks=[],chunkIdx=0,chunkStopped=false;
var articleBreaks=[],currentArticle=0;

// ── Voices ──────────────────────────────────────────────────────
function initV(){
  var vs=synth.getVoices();
  heVoice=vs.find(function(v){return v.name==='Carmit';})||
          vs.find(function(v){return v.lang==='he-IL';})||
          vs.find(function(v){return v.lang.startsWith('he');})||null;
}
if(synth.onvoiceschanged!==undefined)synth.onvoiceschanged=initV;
initV();

// ── Screen switching ─────────────────────────────────────────────
function disableVoiceOver(){
  // Suppress VoiceOver while our app is in foreground:
  // Hide everything except vcbtn from the accessibility tree.
  // vcbtn stays visible so VO can find and activate it (2 taps).
  var blind=document.getElementById('blind');
  if(blind){
    // Hide top section and bottom section from VO
    var top=document.getElementById('blind-top');
    var bot=document.getElementById('blind-bottom');
    if(top)top.setAttribute('aria-hidden','true');
    if(bot)bot.setAttribute('aria-hidden','true');
  }
  // Make sure vcbtn is fully accessible
  var btn=document.getElementById('vcbtn');
  if(btn){
    btn.removeAttribute('aria-hidden');
    btn.setAttribute('aria-label','\u05d3\u05d1\u05e8 \u05d0\u05dc\u05d9');
    btn.setAttribute('role','button');
  }
}

function enableVoiceOver(){
  // Restore full VO access — called when app goes to background (incoming call etc.)
  var top=document.getElementById('blind-top');
  var bot=document.getElementById('blind-bottom');
  if(top)top.removeAttribute('aria-hidden');
  if(bot)bot.removeAttribute('aria-hidden');
}
function showBlind(){
  currentScreen='blind';
  document.getElementById('blind').style.display='flex';
  document.getElementById('app').classList.remove('vis');
  document.getElementById('listbtn').classList.remove('vis');
  disableVoiceOver();
  updateBlindSeg();
}
function showDetail(){
  currentScreen='detail';
  document.getElementById('blind').style.display='none';
  document.getElementById('app').classList.add('vis');
  document.getElementById('listbtn').classList.add('vis');
}

// ── Login flow ───────────────────────────────────────────────────
var currentUser=null;

function startLoginFlow(onDone){
  unlockTTS();
  setTimeout(function(){
    sayHebrew('\u05de\u05d9 \u05d0\u05ea\u05d4?', function(){
      dictListenOnce(function(heard){
        var name=(heard||'')
          .replace(/[\u0591-\u05c7]/g,'')
          .replace(/[.,!?״׳"'\-]/g,'')
          .replace(/\s+/g,' ')
          .trim();
        if(!name){
          // Nothing heard — prompt and retry
          sayHebrew('\u05dc\u05d0 \u05e9\u05de\u05e2\u05ea\u05d9. \u05e0\u05e1\u05d4 \u05e9\u05d5\u05d1.', function(){
            setTimeout(function(){ startLoginFlow(onDone); }, 500);
          });
          return;
        }
        fetch('/api/login_check',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({name:name})
        }).then(function(r){ return r.json(); }).then(function(d){
          if(d.exists){
            sayHebrew('\u05d4\u05de\u05e9\u05ea\u05de\u05e9 '+name+' \u05db\u05d1\u05e8 \u05e7\u05d9\u05d9\u05dd. \u05d4\u05d0\u05dd \u05d6\u05d4 \u05d0\u05ea\u05d4?', function(){
              dictListenOnce(function(ans){
                if(!ans){
                  // Nothing heard — retry confirmation
                  sayHebrew('\u05dc\u05d0 \u05e9\u05de\u05e2\u05ea\u05d9. \u05d4\u05d0\u05dd \u05d6\u05d4 \u05d0\u05ea\u05d4, '+name+'?', function(){
                    dictListenOnce(function(ans2){
                      ans=ans2||'';
                      _handleLoginConfirm(ans, name, true, onDone);
                    });
                  });
                  return;
                }
                _handleLoginConfirm(ans, name, true, onDone);
              });
            });
          } else {
            sayHebrew('\u05e9\u05dd \u05d7\u05d3\u05e9: '+name+'. \u05d4\u05d0\u05dd \u05e0\u05db\u05d5\u05df?', function(){
              dictListenOnce(function(ans){
                if(!ans){
                  sayHebrew('\u05dc\u05d0 \u05e9\u05de\u05e2\u05ea\u05d9. \u05d4\u05d0\u05dd \u05d4\u05e9\u05dd '+name+' \u05e0\u05db\u05d5\u05df?', function(){
                    dictListenOnce(function(ans2){
                      ans=ans2||'';
                      _handleLoginConfirm(ans, name, false, onDone);
                    });
                  });
                  return;
                }
                _handleLoginConfirm(ans, name, false, onDone);
              });
            });
          }
        }).catch(function(){
          sayHebrew('\u05e9\u05d2\u05d9\u05d0\u05ea \u05ea\u05e7\u05e9\u05d5\u05e8\u05ea. \u05e0\u05e1\u05d4 \u05e9\u05d5\u05d1.', function(){
            setTimeout(function(){ startLoginFlow(onDone); }, 500);
          });
        });
      });
    });
  }, 300);
}

function _handleLoginConfirm(ans, name, exists, onDone){
  var a=normH(ans||'');
  var yes=/כן|בסדר|נכון|אוקי|אני|כ$/.test(a) && !/לא/.test(a);
  var no=/לא/.test(a);
  if(yes){
    doLogin(name, onDone);
  } else if(no || exists){
    if(no && exists){
      sayHebrew('\u05d0\u05d6 \u05d1\u05d7\u05e8 \u05d1\u05d1\u05e7\u05e9\u05d4 \u05e9\u05dd \u05d0\u05d7\u05e8.', function(){
        setTimeout(function(){ startLoginFlow(onDone); }, 500);
      });
    } else if(no){
      sayHebrew('\u05e0\u05e1\u05d4 \u05e9\u05d5\u05d1.', function(){
        setTimeout(function(){ startLoginFlow(onDone); }, 500);
      });
    } else {
      // Unclear answer
      setTimeout(function(){ startLoginFlow(onDone); }, 1000);
    }
  } else {
    setTimeout(function(){ startLoginFlow(onDone); }, 1000);
  }
}

function doLogin(name, onDone){
  fetch('/api/login',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:name})
  }).then(function(r){ return r.json(); }).then(function(d){
    if(!d.ok){
      if(d.error==='inactive'){
        sayHebrew('\u05de\u05e9\u05ea\u05de\u05e9 '+name+' \u05d0\u05d9\u05e0\u05d5 \u05e4\u05e2\u05d9\u05dc.');
      } else {
        sayHebrew('\u05e9\u05d2\u05d9\u05d0\u05d4. \u05e0\u05e1\u05d4 \u05e9\u05d5\u05d1.');
        setTimeout(function(){ startLoginFlow(onDone); }, 2000);
      }
      return;
    }
    currentUser={name:d.name, show_greeting:d.show_greeting, play_speed:d.play_speed, new_user:d.new_user};
    rate=d.play_speed||1;
    document.querySelectorAll('.sb').forEach(function(b){
      b.classList.toggle('on',parseFloat(b.textContent.replace('x',''))===rate);
    });
    if(onDone) onDone();
  });
}

// ── Load ─────────────────────────────────────────────────────────
async function load(){
  await loadAbbreviations();
  // Restore currentUser from session if not already set
  if(!currentUser){
    var wr=await fetch('/api/whoami');
    var wd=await wr.json();
    if(wd.logged_in){
      currentUser={name:wd.name, show_greeting:wd.show_greeting, play_speed:wd.play_speed};
      rate=wd.play_speed||1;
      document.querySelectorAll('.sb').forEach(function(b){
        b.classList.toggle('on',parseFloat(b.textContent.replace('x',''))===rate);
      });
    }
  }
  var r=await fetch('/api/current');
  var d=await r.json();
  document.getElementById('ls').style.display='none';

  if(d.need_login){
    // Not logged in — start login flow
    document.getElementById('blind').style.display='flex';
    document.getElementById('blind-seg').textContent='\u05d1\u05d1\u05e7\u05e9\u05d4 \u05d4\u05d6\u05d3\u05d4\u05d4...';
    // Wait for first touch to unlock TTS
    function startAfterTouch(){
      document.removeEventListener('touchstart',startAfterTouch);
      document.removeEventListener('mousedown',startAfterTouch);
      startLoginFlow(function(){
        load(); // reload after login
      });
    }
    document.addEventListener('touchstart',startAfterTouch,{once:true});
    document.addEventListener('mousedown',startAfterTouch,{once:true});
    return;
  }

  if(d.no_issue){
    document.getElementById('blind').style.display='flex';
    document.getElementById('blind-seg').textContent='\u05d0\u05d9\u05df \u05d9\u05d3\u05d9\u05e2\u05d5\u05df \u05d6\u05de\u05d9\u05df';
    return;
  }
  S=d;
  // Update user name display immediately
  var blindName=document.getElementById('blind-user-name');
  if(blindName) blindName.textContent=currentUser?currentUser.name:'';
  var nameLbl=document.getElementById('user-name-lbl');
  if(nameLbl) nameLbl.textContent=currentUser?currentUser.name:'';
  // Apply user speed
  if(currentUser && currentUser.play_speed){
    rate=currentUser.play_speed;
    document.querySelectorAll('.sb').forEach(function(b){
      b.classList.toggle('on',parseFloat(b.textContent.replace('x',''))===rate);
    });
  }
  // Resume from saved chunk position if available
  chunkIdx=d.chunk_pos||0;
  render();
  renderD();
  showBlind();
  // Opening announcement
  var shouldGreet=(!currentUser||currentUser.show_greeting) && !sessionStorage.getItem('greeted');
  if(shouldGreet){
    function greetOnce(){
      document.removeEventListener('touchstart',greetOnce);
      document.removeEventListener('mousedown',greetOnce);
      sessionStorage.setItem('greeted','1');
      var seg=S.segments[S.current_position];
      unlockTTS();
      greetingActive=true;
      var who=currentUser?' '+currentUser.name:'';
      var greetText=
        '\u05e9\u05dc\u05d5\u05dd'+who+'. ' +
        S.issue_title+'. '+
        '\u05d0\u05e0\u05d7\u05e0\u05d5 \u05d1\u05d7\u05dc\u05e7 '+seg.title+'. '+
        '\u05db\u05d3\u05d9 \u05dc\u05d4\u05ea\u05d7\u05d9\u05dc \u05d4\u05e7\u05e9 \u05e2\u05dc \u05de\u05e8\u05db\u05d6 \u05d4\u05de\u05e1\u05da \u05d5\u05d0\u05de\u05d5\u05e8 \u05d4\u05ea\u05d7\u05dc. '+
        '\u05dc\u05e8\u05e9\u05d9\u05de\u05ea \u05e4\u05e7\u05d5\u05d3\u05d5\u05ea \u05d0\u05de\u05d5\u05e8 \u05e2\u05d6\u05e8\u05d4.';
      setTimeout(function(){
        sayHebrew(greetText);
        var estMs=Math.max(3000, Math.round(greetText.length*70)+2000);
        setTimeout(function(){ greetingActive=false; }, estMs);
      },200);
    }
    document.addEventListener('touchstart',greetOnce,{once:true});
    document.addEventListener('mousedown',greetOnce,{once:true});
  }
}

// ── Render ───────────────────────────────────────────────────────
function render(){
  if(!S)return;
  var seg=S.segments[S.current_position];
  document.getElementById('issue-lbl').textContent=S.issue_title;
  document.getElementById('seg-lbl').textContent=seg.title;
  document.getElementById('pos-lbl').textContent=
    '\u05e7\u05d8\u05e2 '+(S.current_position+1)+' \u05de\u05ea\u05d5\u05da '+S.total;
  // Display body — strip internal markers, wrap each line in dir=auto span
  var bodyEl=document.getElementById('body');
  var rawBody=(seg.body||'').replace(/__ARTICLE_END__/g,'').replace(/ {2,}/g,' ');
  var bodyLines=rawBody.split(String.fromCharCode(10));
  bodyEl.innerHTML=bodyLines.map(function(line){
    var esc=line.replace(/&/g,'&amp;').replace(/[<]/g,'&lt;').replace(/[>]/g,'&gt;');
    return '<span dir="auto" style="display:block">'+esc+'</span>';
  }).join('');
  document.getElementById('pfill').style.width=
    ((S.current_position+1)/S.total*100)+'%';
  document.getElementById('ta').scrollTop=0;
  // Show current user name in both screens
  var nameLbl=document.getElementById('user-name-lbl');
  if(nameLbl) nameLbl.textContent=currentUser?currentUser.name:'';
  var blindName=document.getElementById('blind-user-name');
  if(blindName) blindName.textContent=currentUser?currentUser.name:'';
  updateBlindSeg();
  renderD();
}

async function doLogout(){
  pause();
  await fetch('/api/logout',{method:'POST'});
  currentUser=null;
  sessionStorage.removeItem('greeted');
  location.reload();
}

async function switchUser(){
  pause();
  await fetch('/api/logout',{method:'POST'});
  currentUser=null;
  sessionStorage.removeItem('greeted');
  startLoginFlow(function(){
    sessionStorage.removeItem('greeted');
    load();
  });
}
function updateBlindSeg(){
  if(!S)return;
  var seg=S.segments[S.current_position];
  document.getElementById('blind-seg').textContent=
    seg.title+' ('+(S.current_position+1)+'/'+S.total+')';
}
function renderD(){
  if(!S)return;
  document.getElementById('segl').innerHTML=S.segments.map(function(s,i){
    return '<div class="si '+(i===S.current_position?'cur':'')+
      '" onclick="jump('+i+')"><div class="n">'+(i+1)+
      '</div><div class="dot"></div><div class="nm">'+s.title+'</div></div>';
  }).join('');
}

// ── Playback ─────────────────────────────────────────────────────
// Chunk-based playback: text split into ~5-word chunks.
// pause() saves current chunk index → resume() restarts from that chunk.

var MAX_WORDS=20;
var COMMA_MIN=15;
var COMMA_MAX=25;

function splitChunks(text){
  // Extract __ARTICLE_END__ markers before splitting, preserve as standalone chunks
  var parts = text.split('__ARTICLE_END__');
  var result = [];
  parts.forEach(function(part, idx){
    var sub = _splitChunksInner(part.trim());
    result = result.concat(sub);
    if(idx < parts.length - 1) result.push('__ARTICLE_END__');
  });
  return result.filter(function(s){return s.length>0;});
}

function _splitChunksInner(text){
  // First split into sentences at . ! ?
  // Then within long sentences, break at comma near word 15-25, else at word 20
  var sentences=[];
  var raw=text.split(/([.!?]+)/);
  // Rejoin punctuation with preceding sentence
  for(var i=0;i<raw.length;i+=2){
    var s=raw[i]+(raw[i+1]||'');
    s=s.trim();
    if(s)sentences.push(s);
  }

  var result=[];
  sentences.forEach(function(sent){
    var words=sent.split(' ').filter(function(w){return w.length>0;});
    if(words.length<=MAX_WORDS){
      result.push(words.join(' '));
      return;
    }
    // Long sentence — find break points
    var start=0;
    while(start<words.length){
      var end=start+MAX_WORDS;
      if(end>=words.length){result.push(words.slice(start).join(' '));break;}
      // Look for comma between COMMA_MIN and COMMA_MAX words from start
      var breakAt=-1;
      for(var w=start+COMMA_MIN-1;w<Math.min(start+COMMA_MAX,words.length);w++){
        if(words[w].slice(-1)===','){breakAt=w+1;break;}
      }
      if(breakAt>0){
        result.push(words.slice(start,breakAt).join(' '));
        start=breakAt;
      } else {
        result.push(words.slice(start,end).join(' '));
        start=end;
      }
    }
  });
  return result.filter(function(s){return s.trim().length>0;});
}

function showPhrase(text){
  var t=text||'';
  var el1=document.getElementById('blind-phrase');
  var el2=document.getElementById('detail-phrase');
  if(el1)el1.textContent=t;
  if(el2)el2.textContent=t;
}

function jump(p){stop();S.current_position=p;savePos(p);render();closeD();}

function nav(d){
  var wasPlaying=playing;
  stop();
  var n=S.current_position+d;
  if(n<0||n>=S.total)return;
  S.current_position=n;savePos(n);render();
  if(wasPlaying){speak();}
  else{sayHebrew(S.segments[S.current_position].title);}
}

function toggle(){playing?pause():resume();}

// ── Hebrew abbreviation dictionary (loaded from DB) ─────────────
var ABBR_DICT = {};  // populated by loadAbbreviations()

async function loadAbbreviations(){
  try{
    var r=await fetch('/api/abbreviations');
    var rows=await r.json();
    ABBR_DICT={};
    rows.forEach(function(row){
      if(!row.expansion) return;
      if(row.abbr.startsWith('__')){
        // Letter name entry: key is the single letter after __
        var ch=row.abbr.slice(2);
        LETTER_NAMES[ch]=row.expansion;
      } else {
        ABBR_DICT[row.abbr]=row.expansion;
        var v=row.abbr.replace(/"/g,'\u05f4');
        if(v!==row.abbr) ABBR_DICT[v]=row.expansion;
      }
    });
  } catch(e){ console.warn('abbr load failed',e); }
}

var LETTER_NAMES = {
  'א':'אלף','ב':'בית','ג':'גימל',
  'ד':'דלת','ה':'הא','ו':'וו',
  'ז':'זיין','ח':'חת','ט':'טית',
  'י':'יוד','כ':'כף','ך':'כף',
  'ל':'למד','מ':'מם','ם':'מם',
  'נ':'נון','ן':'נון',
  'ס':'סמך','ע':'עין','פ':'פא',
  'ף':'פא','צ':'צדי',
  'ץ':'צדי','ק':'קוף',
  'ר':'ריש','ש':'שין','ת':'תו'
};

function expandLetterNames(word){
  // Convert each Hebrew letter in word to its name
  var result=[];
  for(var i=0;i<word.length;i++){
    var ch=word[i];
    result.push(LETTER_NAMES[ch]||ch);
  }
  return result.join(' ');
}

function normalizeForSpeech(text){
  // Note: punctuation, brackets, symbols, and time expressions (HH:MM)
  // are already normalized server-side before storage.
  // This function handles only the parts that depend on ABBR_DICT (loaded at runtime).

  // 1. Replace gershayim words
  text=text.replace(/([\u05d0-\u05ea]{1,8})["\u05f4]([\u05d0-\u05ea]{1,3})/g,
    function(match, pre, post){
      if(post.length!==1){ return pre+post; }
      if(ABBR_DICT[match])return ABBR_DICT[match];
      var letters=pre+post;
      var totalLen=letters.length;
      if(totalLen===4 && letters[0]==='\u05ea' &&
         '\u05e9\u05e8\u05e7\u05e6\u05e0\u05de\u05dc\u05db\u05d9'.indexOf(letters[1])>=0){
        return expandLetterNames(letters);
      }
      if(totalLen>=4) return letters;
      return expandLetterNames(letters);
    });
  // 2. Geresh after single letter: ד' → look up in ABBR_DICT first, else letter name
  text=text.replace(/([\u05d0-\u05ea])['\u05f3](?=\s|$)/g,
    function(m,ch){
      var key=ch+"'";
      if(ABBR_DICT[key]) return ABBR_DICT[key];
      return LETTER_NAMES[ch]||ch;
    });
  // 3. Single isolated Hebrew letter → letter name
  text=text.replace(/(?<![^\u05d0-\u05ea\s])(^|\s)([\u05d0-\u05ea])(\s|$)/g,
    function(m,pre,ch,post){ return pre+(LETTER_NAMES[ch]||ch)+post; });
  return text.replace(/ {2,}/g,' ');
}

function acquireWakeLock(){
  if('wakeLock' in navigator && !wakeLock){
    navigator.wakeLock.request('screen').then(function(wl){
      wakeLock=wl;
      wakeLock.addEventListener('release',function(){ wakeLock=null; });
    }).catch(function(){});
  }
}
function releaseWakeLock(){
  if(wakeLock){ wakeLock.release(); wakeLock=null; }
}

function speak(){
  if(!S)return;
  var seg=S.segments[S.current_position];
  // Build chunks: title first, then 1s pause marker, then body (with article breaks)
  var titleChunks=splitChunks(normalizeForSpeech(seg.title));
  // Parse articles from body and build chunks with article-break markers
  var bodyChunks=buildBodyChunksWithArticles(seg.body);
  chunks=titleChunks.concat(['__PAUSE1__']).concat(bodyChunks);
  // Build article index: positions of __ARTICLE_BREAK__ + start
  articleBreaks=buildArticleBreakIndex(titleChunks.length+1, bodyChunks);
  currentArticle=0;
  chunkIdx=0;
  synth.cancel();
  chunkStopped=false;
  acquireWakeLock();
  onSpeakStart();
  _nextChunk();
}

// Parse body into articles separated by headings (short lines after blank line)
// Returns array of {heading, body} same as Python split_into_articles logic
function parseArticles(bodyText){
  var lines=bodyText.split(String.fromCharCode(10));
  var articles=[];
  var curHead='';
  var curLines=[];
  function flush(){
    var b=curLines.join(String.fromCharCode(10)).trim();
    if(b||curHead) articles.push({heading:curHead,body:b});
  }
  for(var i=0;i<lines.length;i++){
    var line=lines[i].trim();
    var prevBlank=(i>0 && !lines[i-1].trim());
    if((prevBlank||i===0) && line && line.length<=40
       && /[\u05d0-\u05ea]/.test(line)){
      // Peek: next non-blank line
      var j=i+1;
      while(j<lines.length && !lines[j].trim()) j++;
      var nextContent=j<lines.length?lines[j].trim():'';
      if(!nextContent||nextContent.length>line.length){
        flush();
        curHead=line; curLines=[]; continue;
      }
    }
    curLines.push(lines[i]);
  }
  flush();
  return articles;
}

// Build body chunks with __ARTICLE_BREAK__ markers between articles
function buildBodyChunksWithArticles(bodyText){
  var articles=parseArticles(bodyText);
  if(articles.length<=1){
    return splitChunks(normalizeForSpeech(bodyText));
  }
  var result=[];
  articles.forEach(function(art,idx){
    if(idx>0) result.push('__ARTICLE_BREAK__');
    if(art.heading){
      result=result.concat(splitChunks(normalizeForSpeech(art.heading)));
    }
    if(art.body){
      result=result.concat(splitChunks(normalizeForSpeech(art.body)));
    }
  });
  return result;
}

// Build array of chunk indices where each article starts (after title+PAUSE1)
function buildArticleBreakIndex(bodyStartIdx, bodyChunks){
  var breaks=[bodyStartIdx]; // article 0 starts at body start
  for(var i=0;i<bodyChunks.length;i++){
    if(bodyChunks[i]==='__ARTICLE_BREAK__'){
      breaks.push(bodyStartIdx+i+1); // article N starts after the break marker
    }
  }
  return breaks;
}

// Article-level navigation: d=1 next article, d=-1 prev article
function articleNav(d){
  if(!S||!articleBreaks||articleBreaks.length<=1){
    // No multiple articles — fall back to segment nav
    nav(d); return;
  }
  var wasPlaying=playing;
  pause();
  // Find which article we're currently in
  var cur=0;
  for(var i=articleBreaks.length-1;i>=0;i--){
    if(chunkIdx>=articleBreaks[i]){cur=i;break;}
  }
  var target=cur+d;
  if(target<0) target=0;
  if(target>=articleBreaks.length) target=articleBreaks.length-1;
  currentArticle=target;
  chunkIdx=articleBreaks[target];
  chunkStopped=false;
  savePos(S.current_position, chunkIdx);
  if(wasPlaying){
    acquireWakeLock(); onSpeakStart(); _nextChunk();
  } else {
    // Say the article heading
    var seg=S.segments[S.current_position];
    var arts=parseArticles(seg.body);
    var art=arts[target];
    if(art&&art.heading) sayHebrew(art.heading);
  }
}

function resume(){
  if(chunks.length && chunkIdx<chunks.length){
    synth.cancel();
    chunkStopped=false;
    acquireWakeLock();
    onSpeakStart();
    _nextChunk();
  } else {
    speak();
  }
}

function _nextChunk(){
  if(chunkStopped||!playing)return;
  if(chunkIdx>=chunks.length){onSpeakEnd();return;}
  var text=chunks[chunkIdx];
  // Pause marker — wait 1 second then continue
  if(text==='__PAUSE1__'){
    savePos(S.current_position, chunkIdx);
    chunkIdx++;
    setTimeout(function(){ if(!chunkStopped&&playing) _nextChunk(); }, 1000);
    return;
  }
  // Article end marker (■) — 2 second pause, nothing displayed
  if(text==='__ARTICLE_END__'){
    savePos(S.current_position, chunkIdx);
    chunkIdx++;
    setTimeout(function(){ if(!chunkStopped&&playing) _nextChunk(); }, 2000);
    return;
  }
  // Article break marker — 2 second pause between articles
  if(text==='__ARTICLE_BREAK__'){
    // Update currentArticle counter
    for(var ai=0;ai<articleBreaks.length;ai++){
      if(articleBreaks[ai]===chunkIdx+1) currentArticle=ai;
    }
    savePos(S.current_position, chunkIdx);
    chunkIdx++;
    setTimeout(function(){ if(!chunkStopped&&playing) _nextChunk(); }, 2000);
    return;
  }
  showPhrase(text);
  savePos(S.current_position, chunkIdx);
  utt=new SpeechSynthesisUtterance(text);
  utt.lang='he-IL'; utt.rate=rate;
  if(heVoice)utt.voice=heVoice;
  utt.onend=function(){
    if(chunkStopped)return;
    chunkIdx++;
    _nextChunk();
  };
  utt.onerror=function(e){
    if(e&&e.error==='interrupted')return;
    setIdle();
  };
  synth.speak(utt);
}

function pause(){
  if(playing){
    chunkStopped=true;
    synth.cancel();
    releaseWakeLock();
    setIdle();
  }
}

function stop(){
  chunkStopped=true;
  chunks=[]; chunkIdx=0;
  articleBreaks=[]; currentArticle=0;
  synth.cancel();
  releaseWakeLock();
  setIdle();
  showPhrase('');
}

function speakTitle(){
  if(!S)return;
  sayHebrew(S.segments[S.current_position].title);
}
function onSpeakStart(){
  playing=true;
  document.getElementById('pb').innerHTML='&#9646;&#9646;';
  document.getElementById('pb').classList.add('on');
  document.getElementById('pi').classList.add('on');
  document.getElementById('blind-pi').classList.add('on');
  document.getElementById('blind-status').textContent='\u05de\u05e7\u05e8\u05d9\u05d0...';
}
function onSpeakEnd(){
  setIdle();
  showPhrase('');
  if(S&&S.current_position<S.total-1){
    S.current_position++;savePos(S.current_position);render();
    setTimeout(function(){ speak(); }, 2000);
  }
}
function setIdle(){
  playing=false;
  document.getElementById('pb').innerHTML='&#9654;';
  document.getElementById('pb').classList.remove('on');
  document.getElementById('pi').classList.remove('on');
  document.getElementById('blind-pi').classList.remove('on');
  document.getElementById('blind-status').textContent='';
}
function spd(s){
  rate=s;
  document.querySelectorAll('.sb').forEach(function(b){
    b.classList.toggle('on',parseFloat(b.textContent.replace('x',''))===s);
  });
  // Save to user profile
  fetch('/api/users/set_speed',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({speed:s})}).catch(function(){});
  // restart from beginning of segment
  if(playing){stop();speak();}
}

// ── Phone call / background handling ────────────────────────────
var pausedForCall=false;
document.addEventListener('visibilitychange',function(){
  if(document.hidden){
    if(playing){pause(); pausedForCall=true;}
    // Restore full VoiceOver so user can answer incoming call
    enableVoiceOver();
  } else {
    // Returned to foreground — suppress VO again
    disableVoiceOver();
    if(pausedForCall){
      pausedForCall=false;
      showResumePrompt();
    }
  }
});

function showResumePrompt(){
  if(currentScreen!=='blind')return;
  var st=document.getElementById('blind-status');
  st.textContent='\u05dc\u05d7\u05e5 \u05dc\u05d4\u05de\u05e9\u05da \u05d0\u05d5 \u05d0\u05de\u05d5\u05e8 \u05d4\u05de\u05e9\u05da'; // לחץ להמשך או אמור המשך
  st.style.color='#2d5f3f';
  st.style.fontWeight='700';
  // Auto-clear after 8 seconds
  setTimeout(function(){
    if(!playing){st.textContent='';st.style.color='';st.style.fontWeight='';}
  },8000);
}

// ── Save position ────────────────────────────────────────────────
async function savePos(p, chk){
  await fetch('/api/set_position',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({position:p, chunk:chk||0})});
}

// ── Drawer ───────────────────────────────────────────────────────
function openD(){document.getElementById('drw').classList.add('o');
  document.getElementById('ov').classList.add('o');}
function closeD(){document.getElementById('drw').classList.remove('o');
  document.getElementById('ov').classList.remove('o');}

// ── Audio feedback ───────────────────────────────────────────────
var audioCtx=null;
function getAudio(){
  if(!audioCtx)audioCtx=new(window.AudioContext||window.webkitAudioContext)();
  return audioCtx;
}
function beep(freq,dur,vol){
  try{
    var ctx=getAudio();
    var o=ctx.createOscillator();
    var g=ctx.createGain();
    o.connect(g);g.connect(ctx.destination);
    o.frequency.value=freq;
    g.gain.setValueAtTime(vol||0.3,ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001,ctx.currentTime+dur);
    o.start(ctx.currentTime);
    o.stop(ctx.currentTime+dur);
  }catch(e){}
}
// sayHebrew defined below (supports optional onEnd callback)

// iOS requires speechSynthesis to be "unlocked" within a user gesture.
// We do this once per button tap by speaking an empty utterance immediately.
function unlockTTS(){
  // Only unlock if nothing is currently speaking — don't interrupt greeting
  if(synth.speaking || synth.pending) return;
  var u=new SpeechSynthesisUtterance('');
  u.volume=0;
  synth.speak(u);
  synth.cancel();
}

// ── Voice recognition ────────────────────────────────────────────
var SpeechRec=window.SpeechRecognition||window.webkitSpeechRecognition;
var rec=null;

function vcMsg(txt,bright){
  var el=document.getElementById('vcmsg');
  el.textContent=txt;
  el.style.color=bright?'var(--accent)':'var(--muted)';
}

function startListen(){
  unlockTTS(); // must be first — unlocks iOS TTS within this user gesture
  if(greetingActive) return; // don't interrupt greeting
  if(!S){
    // No active issue — go straight to issue picker
    sayHebrew('\u05d0\u05d9\u05df \u05d9\u05d3\u05d9\u05e2\u05d5\u05df \u05e4\u05e2\u05d9\u05dc. \u05d0\u05d9\u05d6\u05d4 \u05d9\u05d3\u05d9\u05e2\u05d5\u05df \u05dc\u05d4\u05e4\u05e2\u05d9\u05dc?', function(){
      startIssuePicker(false);
    });
    return;
  }
  if(!SpeechRec){
    sayHebrew('\u05d6\u05d9\u05d4\u05d5\u05d9 \u05e7\u05d5\u05dc\u05d9 \u05dc\u05d0 \u05e0\u05ea\u05de\u05da');
    vcMsg('\u05d6\u05d9\u05d4\u05d5\u05d9 \u05e7\u05d5\u05dc\u05d9 \u05dc\u05d0 \u05e0\u05ea\u05de\u05da \u05d1\u05d3\u05e4\u05d3\u05e4\u05df \u05d6\u05d4',false);
    return;
  }
  if(rec){rec.abort();rec=null;}
  // Stop speech during listening — save pause state so commands can resume
  var wasPlaying=playing;
  if(playing) pause();  // saves pausedText/pausedChar

  rec=new SpeechRec();
  rec.lang='he-IL';
  rec.interimResults=true;   // get interim results for faster response
  rec.maxAlternatives=6;

  var btn=document.getElementById('vcbtn');
  btn.classList.add('listening');
  btn.textContent='\u05de\u05d0\u05d6\u05d9\u05df...';
  vcMsg('',false);

  beep(880,0.12,0.25);
  // No TTS cue — it gets transcribed and confuses the recognizer

  var handled=false;
  var silenceTimer=null;

  function finalize(heard){
    if(handled)return;
    handled=true;
    clearTimeout(timeoutId);
    clearTimeout(silenceTimer);
    if(rec){rec.abort(); rec=null;}
    handleCmd(heard, wasPlaying);
  }

  var timeoutId=setTimeout(function(){
    if(!handled){
      handled=true;
      if(rec){rec.abort();}
      beep(440,0.2,0.2);
      vcMsg('\u05dc\u05d0 \u05e9\u05de\u05e2\u05ea\u05d9 \u05e4\u05e7\u05d5\u05d3\u05d4',false);
      if(wasPlaying) resume();
      resetVcBtn();
    }
  },8000);

  // Delay before starting rec:
  // VoiceOver announces "דבר אלי" after the double-tap — wait for it to finish
  // before opening the microphone, otherwise VO speech gets transcribed.
  var voDelay=/iPhone|iPad/.test(navigator.userAgent)?900:80;
  setTimeout(function(){
    try{ rec.start(); } catch(e){}
  }, voDelay);

  rec.onresult=function(e){
    // Get best transcript so far
    var result=e.results[e.results.length-1];
    var alts=Array.from(result).map(function(a){return a.transcript.trim();});
    var heard=alts.join(' ');

    if(result.isFinal){
      finalize(heard);
    } else {
      // Interim: reset silence timer — if 1.5s of silence after speech, finalize
      clearTimeout(silenceTimer);
      silenceTimer=setTimeout(function(){finalize(heard);},1500);
    }
  };
  rec.onerror=function(e){
    if(handled)return;
    // 'no-speech' is normal (timeout) — don't show error
    if(e.error==='no-speech'){
      handled=true;
      clearTimeout(timeoutId); clearTimeout(silenceTimer);
      vcMsg('\u05dc\u05d0 \u05e9\u05de\u05e2\u05ea\u05d9 \u05e4\u05e7\u05d5\u05d3\u05d4',false);
      if(wasPlaying) resume();
      resetVcBtn();
      return;
    }
    // 'not-allowed' or other real errors
    handled=true;
    clearTimeout(timeoutId); clearTimeout(silenceTimer);
    beep(440,0.2,0.2);
    vcMsg('\u05d1\u05e2\u05d9\u05d9\u05ea \u05de\u05d9\u05e7\u05e8\u05d5\u05e4\u05d5\u05df \u2014 \u05d0\u05e4\u05e9\u05e8 \u05d2\u05d9\u05e9\u05d4 \u05dc\u05de\u05d9\u05e7\u05e8\u05d5\u05e4\u05d5\u05df?',false);
    if(wasPlaying) resume();
    resetVcBtn();
  };
  rec.onend=function(){clearTimeout(timeoutId);clearTimeout(silenceTimer);resetVcBtn();};
}

function resetVcBtn(){
  var btn=document.getElementById('vcbtn');
  btn.classList.remove('listening');
  btn.classList.remove('ok');
  btn.textContent='\u05d3\u05d1\u05e8 \u05d0\u05dc\u05d9';
  rec=null;
}

// ── Issue picker via voice ────────────────────────────────────────
var issuePickerActive=false;
var issuePickerWasPlaying=false;

// ── Dictionary flow ──────────────────────────────────────────────
var dictQueue=[];       // abbrevs left to process
var dictCurrent=null;  // current abbrev being asked about
var dictPending=null;  // proposed expansion waiting for confirmation
var dictWasPlaying=false;

function startDictFlow(wasPlaying){
  dictWasPlaying=wasPlaying;
  stop(); // cancel any playback + synth
  // Fetch unresolved abbreviations
  fetch('/api/abbreviations?unresolved=1')
    .then(function(r){return r.json();})
    .then(function(rows){
      var count=rows.length;
      if(count===0){
        setTimeout(function(){ sayHebrew('\u05d0\u05d9\u05df \u05e8\u05d0\u05e9\u05d9 \u05ea\u05d9\u05d1\u05d5\u05ea \u05dc\u05e4\u05d9\u05e2\u05e0\u05d5\u05d7. \u05ea\u05d5\u05d3\u05d4!'); }, 300);
        return;
      }
      dictQueue=rows.slice();
      var intro=
        '\u05ea\u05d5\u05d3\u05d4 \u05e2\u05dc \u05e2\u05d6\u05e8\u05ea\u05da \u05d1\u05e4\u05d9\u05e2\u05e0\u05d5\u05d7 \u05e8\u05d0\u05e9\u05d9 \u05ea\u05d9\u05d1\u05d5\u05ea. '+
        '\u05d9\u05e9 \u05dc\u05d9 '+count+' \u05e8\u05d0\u05e9\u05d9 \u05ea\u05d9\u05d1\u05d5\u05ea \u05dc\u05d0 \u05de\u05e4\u05d5\u05e2\u05e0\u05d7\u05d9\u05dd. '+
        '\u05d0\u05e0\u05d9 \u05d0\u05e7\u05e8\u05d9\u05d0 \u05dc\u05da \u05db\u05dc \u05e4\u05e2\u05dd \u05d1\u05d9\u05d8\u05d5\u05d9 \u05d0\u05d7\u05d3, \u05d5\u05d0\u05ea\u05d4 \u05ea\u05d2\u05d9\u05d3 \u05dc\u05d9 \u05d1\u05de\u05d4 \u05dc\u05d4\u05d7\u05dc\u05d9\u05e3 \u05d0\u05d5\u05ea\u05d5. '+
        '\u05dc\u05d3\u05dc\u05d2 \u05e2\u05dc \u05d1\u05d9\u05d8\u05d5\u05d9 \u05d0\u05de\u05d5\u05e8 \u05d3\u05dc\u05d2. '+
        '\u05dc\u05e1\u05d9\u05d5\u05dd \u05d0\u05de\u05d5\u05e8 \u05de\u05e1\u05e4\u05d9\u05e7. '+
        '\u05db\u05d3\u05d9 \u05dc\u05d4\u05ea\u05d7\u05d9\u05dc \u05d0\u05de\u05d5\u05e8 \u05d4\u05ea\u05d7\u05dc.';
      // Wait for synth to fully clear before starting long intro
      setTimeout(function(){ sayHebrew(intro, function(){ dictListenYesNo(); }); }, 400);
    });
}

function dictListenYesNo(){
  dictListenOnce(function(heard){
    var h=normH(heard);
    // Explicit negative → exit
    if(heard && /\u05dc\u05d0|\u05e2\u05e6\u05d5\u05e8|\u05de\u05e1\u05e4\u05d9\u05e7|\u05d3\u05d9/.test(h) && h.length<8){
      dictEnd();
    // Empty (timeout/no-speech) → repeat prompt once more
    } else if(!heard.trim()){
      sayHebrew('\u05db\u05d3\u05d9 \u05dc\u05d4\u05ea\u05d7\u05d9\u05dc \u05d0\u05de\u05d5\u05e8 \u05d4\u05ea\u05d7\u05dc.', function(){ dictListenYesNo(); });
    } else {
      dictAskNext();
    }
  });
}

function dictAskNext(){
  if(!dictQueue.length){
    sayHebrew('\u05e1\u05d9\u05d9\u05de\u05e0\u05d5. \u05ea\u05d5\u05d3\u05d4!', function(){ dictEnd(); });
    return;
  }
  dictCurrent=dictQueue.shift();
  dictPending=null;
  var spoken=expandLetterNames(dictCurrent.abbr.replace(/["\u05f4]/g,''));
  sayHebrew('\u05de\u05d4 \u05d6\u05d4 '+spoken+'?', function(){ dictListenAnswer(); });
}

function dictListenAnswer(){
  dictListenOnce(function(heard){
    var h=normH(heard);
    // Stop commands
    if(/\u05de\u05e1\u05e4\u05d9\u05e7|\u05d6\u05d4\u05d5|\u05d3\u05d9/.test(h)){
      sayHebrew('\u05d1\u05e1\u05d3\u05e8. \u05ea\u05d5\u05d3\u05d4!', function(){ dictEnd(); });
      return;
    }
    // Skip commands
    if(/\u05d3\u05dc\u05d2|\u05e2\u05d1\u05d5\u05e8|\u05d4\u05dc\u05d0\u05d4/.test(h)){
      dictAskNext();
      return;
    }
    // Meaningful answer — propose it
    dictPending=heard.trim();
    var spoken=expandLetterNames(dictCurrent.abbr.replace(/["\u05f4]/g,''));
    sayHebrew('\u05d4\u05d0\u05dd '+spoken+' \u05d6\u05d4 '+dictPending+'?', function(){ dictListenConfirm(); });
  });
}

function dictListenConfirm(){
  dictListenOnce(function(heard){
    var h=normH(heard);
    if(/\u05db\u05df|\u05d1\u05e1\u05d3\u05e8|\u05e0\u05db\u05d5\u05df|\u05d0\u05d5\u05e7\u05d9|\u05e0\u05d5|\u05e0\u05e2/.test(h) && !/\u05dc\u05d0/.test(h)){
      // כן / בסדר / נכון / אוקי — save and move on
      fetch('/api/abbreviations/update',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({abbr:dictCurrent.abbr, expansion:dictPending})
      }).then(function(){
        ABBR_DICT[dictCurrent.abbr]=dictPending;
        var v=dictCurrent.abbr.replace(/"/g,'\u05f4');
        if(v!==dictCurrent.abbr) ABBR_DICT[v]=dictPending;
        dictAskNext();
      });
    } else if(/\u05dc\u05d0/.test(h)){
      // לא — ask again
      var spoken=expandLetterNames(dictCurrent.abbr.replace(/["\u05f4]/g,''));
      sayHebrew('\u05d0\u05d6 \u05de\u05d4 \u05d6\u05d4 '+spoken+'?', function(){ dictListenAnswer(); });
    } else {
      // Unclear — treat as new answer
      dictPending=heard.trim();
      var spoken=expandLetterNames(dictCurrent.abbr.replace(/["\u05f4]/g,''));
      sayHebrew('\u05d4\u05d0\u05dd '+spoken+' \u05d6\u05d4 '+dictPending+'?', function(){ dictListenConfirm(); });
    }
  });
}

function dictEnd(){
  dictQueue=[];
  dictCurrent=null;
  dictPending=null;
  if(dictWasPlaying) resume();
  resetVcBtn();
}

// sayHebrew with onEnd callback
// Uses real utt.onend + safety timeout fallback — no more fixed setTimeout estimate
function sayHebrew(text, onEnd){
  synth.cancel();
  var u=new SpeechSynthesisUtterance(text);
  u.lang='he-IL'; u.rate=rate;
  if(heVoice) u.voice=heVoice;
  if(onEnd){
    var fired=false;
    var safetyMs=Math.max(4000, Math.round(text.length * 80 / rate) + 2000);
    var safetyTimer=setTimeout(function(){
      if(!fired){ fired=true; onEnd(); }
    }, safetyMs);
    u.onend=function(){
      clearTimeout(safetyTimer);
      if(!fired){
        fired=true;
        // Small gap after speech ends before opening mic — avoids capturing echo
        setTimeout(onEnd, 300);
      }
    };
    u.onerror=function(){
      clearTimeout(safetyTimer);
      if(!fired){ fired=true; setTimeout(onEnd, 300); }
    };
  }
  synth.speak(u);
}

// Normalize heard text for command matching (same as handleCmd)
function normH(text){
  return (text||'')
    .replace(/[\u0591-\u05c7]/g,'')
    .replace(/[.,!?״׳"'\-]/g,'')
    .replace(/\s+/g,' ')
    .trim();
}
function dictDebug(txt){ var el=document.getElementById('vcmsg'); if(el) el.textContent=txt; }
function dictListenOnce(callback){
  if(!SpeechRec){ callback(''); return; }
  if(rec){rec.abort();rec=null;}
  dictDebug('\u05de\u05d0\u05d6\u05d9\u05df...');
  beep(880,0.12,0.25); // signal mic is open
  var r2=new SpeechRec();
  r2.lang='he-IL'; r2.interimResults=true; r2.maxAlternatives=4;
  var handled=false;
  var silT=null;
  var lastHeard='';
  var tout=setTimeout(function(){
    if(!handled){
      handled=true;
      if(r2){r2.abort();r2=null;}
      dictDebug('\u05dc\u05d0 \u05e9\u05de\u05e2\u05ea\u05d9');
      callback('');
    }
  },12000);
  function fin(heard){
    if(handled)return; handled=true;
    clearTimeout(tout); clearTimeout(silT);
    if(r2){r2.abort();r2=null;}
    dictDebug('\u05e9\u05de\u05e2\u05ea\u05d9: '+heard);
    callback(heard);
  }
  r2.onresult=function(e){
    var res=e.results[e.results.length-1];
    var heard=Array.from(res).map(function(a){return a.transcript.trim();}).join(' ');
    lastHeard=heard;
    dictDebug('\u05e9\u05de\u05e2\u05ea\u05d9: '+heard);
    if(res.isFinal){
      fin(heard);
    } else {
      // Interim: wait for 2s silence before finalizing
      clearTimeout(silT);
      silT=setTimeout(function(){ fin(lastHeard); }, 2000);
    }
  };
  r2.onerror=function(e){
    if(!handled){
      handled=true; clearTimeout(tout); clearTimeout(silT);
      if(r2){r2.abort();r2=null;}
      dictDebug('\u05e9\u05d2\u05d9\u05d0\u05d4: '+e.error);
      callback('');
    }
  };
  r2.onend=function(){
    // If recognition ended without result (e.g. network), finalize with what we have
    if(!handled){ fin(lastHeard); }
  };
  // Since sayHebrew now waits for real TTS onend before calling us,
  // we only need a short delay for iOS mic to initialize
  var voDelay=/iPhone|iPad/.test(navigator.userAgent)?400:100;
  setTimeout(function(){ try{r2.start();}catch(e){ dictDebug('err: '+e); } }, voDelay);
}

function startIssuePicker(wasPlaying){
  issuePickerActive=true;
  issuePickerWasPlaying=wasPlaying;
  pause();
  // Ask user which issue, then start listening immediately when TTS ends
  var q=new SpeechSynthesisUtterance('\u05dc\u05d0\u05d9\u05d6\u05d4 \u05e7\u05d5\u05d1\u05e5 \u05dc\u05e2\u05d1\u05d5\u05e8?');
  q.lang='he-IL'; q.rate=rate;
  if(heVoice)q.voice=heVoice;
  q.onend=function(){
    vcMsg('\u05d0\u05de\u05d5\u05e8: \u05d4\u05e7\u05d5\u05d3\u05dd / \u05d4\u05d1\u05d0 / \u05d4\u05d0\u05d7\u05e8\u05d5\u05df / \u05e4\u05e8\u05e9\u05ea... / \u05d2\u05d9\u05dc\u05d9\u05d5\u05df...',false);
    beep(880,0.12,0.25);
    setTimeout(listenForIssue, 150);
  };
  synth.speak(q);
}

function listenForIssue(){
  if(!SpeechRec){issuePickerActive=false;return;}
  if(rec){rec.abort();rec=null;}
  var btn=document.getElementById('vcbtn');
  btn.classList.add('listening');
  btn.textContent='\u05de\u05d0\u05d6\u05d9\u05df...';

  rec=new SpeechRec();
  rec.lang='he-IL';
  rec.interimResults=false;
  rec.maxAlternatives=6;

  var handled=false;
  var tid=setTimeout(function(){
    if(!handled){handled=true;if(rec)rec.abort();
      sayHebrew('\u05dc\u05d0 \u05e9\u05de\u05e2\u05ea\u05d9');
      issuePickerActive=false; resetVcBtn(); vcMsg('',false);}
  },7000);

  rec.onresult=function(e){
    if(handled)return; handled=true;
    clearTimeout(tid);
    var alts=Array.from(e.results[0]).map(function(a){return a.transcript.trim();});
    var heard=alts.join(' ');
    rec=null; resetVcBtn();
    handleIssueChoice(heard);
  };
  rec.onerror=function(){
    if(handled)return; handled=true; clearTimeout(tid);
    issuePickerActive=false; resetVcBtn(); vcMsg('',false);
  };
  rec.onend=function(){clearTimeout(tid); resetVcBtn();};
  rec.start();
}

async function handleIssueChoice(heard){
  var h=heard.replace(/[.,!?״׳]/g,'').trim();
  issuePickerActive=false;
  vcMsg('',false);

  // Fetch all issues sorted newest first (id DESC)
  var resp=await fetch('/api/issues');
  var issues=await resp.json(); // [{id, title, ...}]
  if(!issues||!issues.length){sayHebrew('\u05d0\u05d9\u05df \u05e7\u05d1\u05e6\u05d9\u05dd');return;}

  // Find current issue index
  var curId=S?S.issue_id:null;
  var curIdx=issues.findIndex(function(iss){return iss.id===curId;});

  var targetIssue=null;

  // "הקודם" / "הגיליון הקודם" etc — older = higher index in DESC list
  if(/\u05e7\u05d5\u05d3\u05dd|\u05e7\u05d5\u05d3\u05de/.test(h)){
    var ni=curIdx+1;
    if(ni<issues.length)targetIssue=issues[ni];
    else{sayHebrew('\u05d0\u05d9\u05df \u05d2\u05d9\u05dc\u05d9\u05d5\u05df \u05e7\u05d5\u05d3\u05dd');return;}
  }
  // "הבא" / "הגיליון הבא" — newer = lower index
  else if(/\u05d4\u05d1\u05d0|\u05d1\u05d0\u05d4/.test(h)){
    var ni2=curIdx-1;
    if(ni2>=0)targetIssue=issues[ni2];
    else{sayHebrew('\u05d0\u05d9\u05df \u05d2\u05d9\u05dc\u05d9\u05d5\u05df \u05d7\u05d3\u05e9 \u05d9\u05d5\u05ea\u05e8');return;}
  }
  // "האחרון" / "הגיליון האחרון" — newest = first in DESC list (id DESC)
  else if(/\u05d0\u05d7\u05e8\u05d5\u05df|\u05d0\u05d7\u05e8\u05d5\u05e0/.test(h)){
    targetIssue=issues[0];
  }
  // "גיליון מספר 3647" or "גיליון 3647"
  else if(/\u05d2\u05d9\u05dc\u05d9\u05d5\u05df/.test(h)){
    var numMatch=h.match(/(\d{3,5})/);
    if(numMatch){
      var num=parseInt(numMatch[1]);
      targetIssue=issues.find(function(iss){return iss.title.indexOf(String(num))>=0;});
    }
    if(!targetIssue){sayHebrew('\u05dc\u05d0 \u05de\u05e6\u05d0\u05ea\u05d9 \u05d0\u05ea \u05d4\u05d2\u05d9\u05dc\u05d9\u05d5\u05df');return;}
  }
  // "פרשת X" or parasha name
  else {
    // Search by parasha name in issue title
    var best=null;
    issues.forEach(function(iss){
      if(iss.title && iss.title.indexOf && h.split(' ').some(function(w){
        return w.length>2 && iss.title.indexOf(w)>=0;
      })) best=iss;
    });
    if(best){targetIssue=best;}
    else{sayHebrew('\u05dc\u05d0 \u05d4\u05d1\u05e0\u05ea\u05d9');return;}
  }

  if(!targetIssue){sayHebrew('\u05dc\u05d0 \u05de\u05e6\u05d0\u05ea\u05d9');return;}
  if(targetIssue.id===curId){sayHebrew('\u05d6\u05d4 \u05db\u05d1\u05e8 \u05d4\u05e7\u05d5\u05d1\u05e5 \u05d4\u05e4\u05e2\u05d9\u05dc');return;}

  // Activate the issue
  await fetch('/api/set_issue',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({issue_id:targetIssue.id})});
  // Reload state
  await load();
  sayHebrew('\u05e2\u05d5\u05d1\u05e8 \u05dc: '+targetIssue.title);
  if(issuePickerWasPlaying) setTimeout(speak,2000);
}

function handleCmd(heard, wasPlaying){
  var btn=document.getElementById('vcbtn');
  btn.classList.remove('listening');
  // Normalize: remove nikud, punctuation, extra spaces
  var h=heard
    .replace(/[\u0591-\u05c7]/g,'')   // strip nikud/taamim
    .replace(/[.,!?״׳"'\-]/g,'')
    .replace(/\s+/g,' ')
    .trim();
  vcMsg('\u05e9\u05de\u05e2\u05ea\u05d9: "'+h+'"',false); // debug: show what was heard
  var done=false;
  var label='';
  var noEcho=false;

  if(/\u05d0\u05d9\u05df \u05d9\u05d3\u05d9\u05e2\u05d5\u05df/.test(h)){
    resetVcBtn(); return;
  }

  // ── סיום
  if(/\u05e1\u05d9\u05d5\u05dd/.test(h)){
    done=true; label='\u05e1\u05d9\u05d5\u05dd'; noEcho=true;
    beep(1046,0.15,0.2);
    pause();
    sayHebrew('\u05e9\u05dc\u05d5\u05dd... \u05e9\u05dc\u05d5\u05dd.');
    setTimeout(function(){
      var isPWA = window.matchMedia('(display-mode: standalone)').matches
                  || window.navigator.standalone === true;
      if(isPWA){
        // In PWA mode window.close() works and returns to home screen
        window.close();
        // Fallback if close didn't work
        setTimeout(function(){ window.location.replace('/bye'); }, 500);
      } else {
        history.pushState(null,'','/bye');
        history.pushState(null,'','/bye');
        window.location.replace('/bye');
      }
    },1800);
  }
  // ── התנתק
  else if(/\u05d4\u05ea\u05e0\u05ea\u05e7/.test(h)){
    done=true; label='\u05d4\u05ea\u05e0\u05ea\u05e7'; noEcho=true;
    pause();
    fetch('/api/logout',{method:'POST'}).then(function(){
      currentUser=null;
      sessionStorage.removeItem('greeted');
      sayHebrew('\u05d4\u05ea\u05e0\u05ea\u05e7\u05ea \u05d1\u05d4\u05e6\u05dc\u05d7\u05d4.');
      setTimeout(function(){ location.reload(); }, 2000);
    });
  }
  // ── התחבר
  else if(/\u05d4\u05ea\u05d7\u05d1\u05e8/.test(h)){
    done=true; label='\u05d4\u05ea\u05d7\u05d1\u05e8'; noEcho=true;
    pause();
    startLoginFlow(function(){
      sessionStorage.removeItem('greeted');
      load();
    });
  }
  // ── החלף משתמש
  else if(/\u05d4\u05d7\u05dc\u05e3/.test(h)&&/\u05de\u05e9\u05ea\u05de\u05e9/.test(h)){
    done=true; label='\u05d4\u05d7\u05dc\u05e3 \u05de\u05e9\u05ea\u05de\u05e9'; noEcho=true;
    pause();
    fetch('/api/logout',{method:'POST'}).then(function(){
      currentUser=null;
      sessionStorage.removeItem('greeted');
      startLoginFlow(function(){
        sessionStorage.removeItem('greeted');
        load();
      });
    });
  }
  // ── החלף קובץ / גיליון / ידיעון
  else if(/\u05d4\u05d7\u05dc\u05e3|\u05e9\u05e0\u05d4/.test(h)&&/\u05e7\u05d5\u05d1\u05e5|\u05d2\u05d9\u05dc\u05d9\u05d5\u05df|\u05d9\u05d3\u05d9\u05e2\u05d5\u05df/.test(h)){
    done=true; label='\u05d4\u05d7\u05dc\u05e3 \u05e7\u05d5\u05d1\u05e5'; noEcho=true;
    startIssuePicker(wasPlaying);
  }
  // ── עצור / השהה — check early so "עצור" doesn't get caught by other patterns
  else if(/\u05e2\u05e6\u05d5\u05e8|\u05e2\u05e6\u05e8|\u05d4\u05e9\u05d4\u05d4|\u05e4\u05e1\u05e7|\u05d4\u05e4\u05e1\u05e7/.test(h)){
    pause(); done=true; label='\u05e2\u05e6\u05d5\u05e8';
  }
  // ── המשך / תמשיך / המשך לקרוא
  else if(/\u05d4\u05de\u05e9\u05da|\u05ea\u05de\u05e9\u05d9\u05da|\u05d4\u05de\u05e9\u05d9\u05da/.test(h)){
    done=true; label='\u05de\u05de\u05e9\u05d9\u05da'; noEcho=true;
    resume();
  }
  // ── הפעל / קרא / התחל
  else if(/\u05d4\u05e4\u05e2\u05dc|\u05d4\u05ea\u05d7\u05dc|\u05e7\u05e8\u05d0|\u05e7\u05e8\u05d9\u05d0\u05d4|\u05d4\u05e7\u05e8\u05d0/.test(h)){
    done=true; label='\u05d4\u05e4\u05e2\u05dc'; noEcho=true;
    speak();
  }
  // ── המאמר הבא / המאמר הקודם
  if(/\u05de\u05d0\u05de\u05e8/.test(h)&&/\u05d4\u05d1\u05d0|\u05d4\u05d1\u05d0\u05d4|\u05e7\u05d3\u05d9\u05de\u05d4|\u05d4\u05e7\u05d5\u05d3\u05dd/.test(h)){
    done=true; noEcho=true;
    var artD=/\u05d4\u05d1\u05d0|\u05d4\u05d1\u05d0\u05d4|\u05e7\u05d3\u05d9\u05de\u05d4/.test(h)?1:-1;
    label=artD===1?'\u05de\u05d0\u05de\u05e8 \u05d4\u05d1\u05d0':'\u05de\u05d0\u05de\u05e8 \u05e7\u05d5\u05d3\u05dd';
    articleNav(artD);
  }
  // ── קדימה / הבא / דלג / הקטע הבא
  else if(/\u05d4\u05d1\u05d0|\u05e7\u05d3\u05d9\u05de\u05d4|\u05d0\u05d1\u05d0|\u05d3\u05dc\u05d2/.test(h)&&!/\u05e7\u05d5\u05d3\u05dd/.test(h)){
    done=true; label='\u05e7\u05d8\u05e2 \u05d4\u05d1\u05d0'; noEcho=true;
    nav(1);
  }
  // ── אחורה / קודם / חזור לקטע הקודם
  else if(/\u05e7\u05d5\u05d3\u05dd|\u05e7\u05d5\u05d3\u05de|\u05d0\u05d7\u05d5\u05e8\u05d4|\u05d0\u05d7\u05d5\u05e8/.test(h)){
    done=true; label='\u05e7\u05d8\u05e2 \u05e7\u05d5\u05d3\u05dd'; noEcho=true;
    nav(-1);
  }
  // ── חזור לתחילת הקטע
  else if(/\u05ea\u05d7\u05d9\u05dc\u05ea \u05d4\u05e7\u05d8\u05e2|\u05de\u05d4\u05ea\u05d7\u05dc\u05d4/.test(h)&&!/\u05d9\u05d3\u05d9\u05e2\u05d5\u05df/.test(h)){
    done=true; label='\u05ea\u05d7\u05d9\u05dc\u05ea \u05d4\u05e7\u05d8\u05e2'; noEcho=true;
    stop(); render(); speak();
  }
  // ── עבור לתחילת הידיעון / ראשון
  else if(/\u05ea\u05d7\u05d9\u05dc\u05ea \u05d4\u05d9\u05d3\u05d9\u05e2\u05d5\u05df|\u05e8\u05d0\u05e9\u05d5\u05df|\u05e8\u05d0\u05e9\u05d5\u05e0/.test(h)){
    done=true; label='\u05ea\u05d7\u05d9\u05dc\u05ea \u05d4\u05d9\u05d3\u05d9\u05e2\u05d5\u05df'; noEcho=true;
    stop(); S.current_position=0; savePos(0); render();
    if(wasPlaying){ speak(); } else { sayHebrew(S.segments[0].title); }
  }
  // ── אחרון — go to last segment
  else if(/\u05d0\u05d7\u05e8\u05d5\u05df|\u05d0\u05d7\u05e8\u05d5\u05e0/.test(h)){
    done=true; label='\u05e7\u05d8\u05e2 \u05d0\u05d7\u05e8\u05d5\u05df'; noEcho=true;
    var last=S.total-1;
    stop(); S.current_position=last; savePos(last); render();
    if(wasPlaying){ speak(); } else { sayHebrew(S.segments[last].title); }
  }
  // ── עבור לקטע / פרק מספר X
  else if((/\u05e7\u05d8\u05e2|\u05e4\u05e8\u05e7/.test(h))&&(/[0-9]|\u05d0\u05d7\u05d3|\u05e9\u05ea\u05d9\u05d9\u05dd|\u05e9\u05dc\u05d5\u05e9\u05d4|\u05d0\u05e8\u05d1\u05e2\u05d4|\u05d7\u05de\u05d9\u05e9\u05d4|\u05e9\u05e9\u05d4|\u05e9\u05d1\u05e2\u05d4|\u05e9\u05de\u05d5\u05e0\u05d4|\u05ea\u05e9\u05e2\u05d4/.test(h))){
    done=true; noEcho=true;
    // Extract number — digits or Hebrew words
    var numMap={'\u05d0\u05d7\u05d3':1,'\u05d0\u05d7\u05ea':1,'\u05e9\u05ea\u05d9\u05d9\u05dd':2,'\u05e9\u05ea\u05d9':2,'\u05e9\u05dc\u05d5\u05e9\u05d4':3,'\u05e9\u05dc\u05d5\u05e9':3,'\u05d0\u05e8\u05d1\u05e2\u05d4':4,'\u05d0\u05e8\u05d1\u05e2':4,'\u05d7\u05de\u05d9\u05e9\u05d4':5,'\u05d7\u05de\u05e9':5,'\u05e9\u05e9\u05d4':6,'\u05e9\u05e9':6,'\u05e9\u05d1\u05e2\u05d4':7,'\u05e9\u05d1\u05e2':7,'\u05e9\u05de\u05d5\u05e0\u05d4':8,'\u05e9\u05de\u05d5\u05e0\u05d4':8,'\u05ea\u05e9\u05e2\u05d4':9,'\u05ea\u05e9\u05e2':9,'\u05e2\u05e9\u05e8\u05d4':10,'\u05e2\u05e9\u05e8':10};
    var segNum=null;
    // Try digits first
    var dm=h.match(/([0-9]+)/);
    if(dm) segNum=parseInt(dm[1]);
    else {
      // Try Hebrew number words
      for(var hw in numMap){if(h.indexOf(hw)>=0){segNum=numMap[hw];break;}}
    }
    if(segNum!==null && segNum>=1 && segNum<=S.total){
      var p=segNum-1;
      label='\u05e7\u05d8\u05e2 '+segNum;
      stop(); S.current_position=p; savePos(p); render();
      if(wasPlaying){ speak(); } else { sayHebrew(S.segments[p].title); }
    } else {
      label='\u05dc\u05d0 \u05e0\u05de\u05e6\u05d0'; noEcho=false;
      sayHebrew('\u05e7\u05d8\u05e2 \u05db\u05d6\u05d4 \u05dc\u05d0 \u05e7\u05d9\u05d9\u05dd');
    }
  }
  // ── מילון ראשי תיבות
  else if(/\u05de\u05d9\u05dc\u05d5\u05df/.test(h)){
    done=true; label='\u05de\u05d9\u05dc\u05d5\u05df'; noEcho=true;
    startDictFlow(wasPlaying);
  }
  // ── עזרה / הסבר / הוראות
  else if(/\u05e2\u05d6\u05e8\u05d4|\u05d4\u05e1\u05d1\u05e8|\u05d4\u05d5\u05e8\u05d0\u05d5\u05ea|\u05d4\u05e1\u05d1\u05e8\u05d9\u05dd/.test(h)){
    done=true; label='\u05e2\u05d6\u05e8\u05d4'; noEcho=true;
    sayHebrew(
      '\u05d4\u05e4\u05e7\u05d5\u05d3\u05d5\u05ea \u05d4\u05d6\u05de\u05d9\u05e0\u05d5\u05ea: '+
      '\u05d4\u05ea\u05d7\u05dc, \u05de\u05ea\u05d7\u05d9\u05dc \u05e7\u05e8\u05d9\u05d0\u05d4 \u05de\u05d4\u05ea\u05d7\u05dc\u05d4. '+
      '\u05d4\u05de\u05e9\u05da, \u05de\u05de\u05e9\u05d9\u05da \u05d0\u05d7\u05e8\u05d9 \u05e2\u05e6\u05d9\u05e8\u05d4. '+
      '\u05e2\u05e6\u05d5\u05e8, \u05e2\u05d5\u05e6\u05e8 \u05d0\u05ea \u05d4\u05e7\u05e8\u05d9\u05d0\u05d4. '+
      '\u05e7\u05d3\u05d9\u05de\u05d4, \u05e2\u05d5\u05d1\u05e8 \u05dc\u05e7\u05d8\u05e2 \u05d4\u05d1\u05d0. '+
      '\u05d0\u05d7\u05d5\u05e8\u05d4, \u05d7\u05d5\u05d6\u05e8 \u05dc\u05e7\u05d8\u05e2 \u05d4\u05e7\u05d5\u05d3\u05dd. '+
      '\u05d4\u05de\u05d0\u05de\u05e8 \u05d4\u05d1\u05d0, \u05e7\u05d5\u05e4\u05e5 \u05dc\u05de\u05d0\u05de\u05e8 \u05d4\u05d1\u05d0 \u05d1\u05d0\u05d5\u05ea\u05d5 \u05e4\u05e8\u05e7. '+
      '\u05d4\u05de\u05d0\u05de\u05e8 \u05d4\u05e7\u05d5\u05d3\u05dd, \u05d7\u05d5\u05d6\u05e8 \u05dc\u05de\u05d0\u05de\u05e8 \u05d4\u05e7\u05d5\u05d3\u05dd. '+
      '\u05e8\u05d0\u05e9\u05d5\u05df, \u05e2\u05d5\u05d1\u05e8 \u05dc\u05e7\u05d8\u05e2 \u05d4\u05e8\u05d0\u05e9\u05d5\u05df. '+
      '\u05d0\u05d7\u05e8\u05d5\u05df, \u05e2\u05d5\u05d1\u05e8 \u05dc\u05e7\u05d8\u05e2 \u05d4\u05d0\u05d7\u05e8\u05d5\u05df. '+
      '\u05e7\u05d8\u05e2 3, \u05e7\u05d5\u05e4\u05e5 \u05dc\u05e7\u05d8\u05e2 \u05de\u05e1\u05e4\u05e8 3. '+
      '\u05d9\u05d5\u05ea\u05e8 \u05de\u05d4\u05e8, \u05de\u05d2\u05d1\u05d9\u05e8 \u05de\u05d4\u05d9\u05e8\u05d5\u05ea. '+
      '\u05d9\u05d5\u05ea\u05e8 \u05dc\u05d0\u05d8, \u05de\u05d5\u05e8\u05d9\u05d3 \u05de\u05d4\u05d9\u05e8\u05d5\u05ea. '+
      '\u05d4\u05d7\u05dc\u05e3 \u05d9\u05d3\u05d9\u05e2\u05d5\u05df, \u05e2\u05d5\u05d1\u05e8 \u05dc\u05d9\u05d3\u05d9\u05e2\u05d5\u05df \u05d0\u05d7\u05e8. '+
      '\u05e1\u05d9\u05d5\u05dd, \u05de\u05e1\u05d9\u05d9\u05dd \u05d4\u05d0\u05d6\u05e0\u05d4. '+
      '\u05dc\u05e4\u05d9\u05e8\u05d5\u05d8 \u05d7\u05dc\u05d5\u05e4\u05d5\u05ea \u05d0\u05de\u05d5\u05e8 \u05d0\u05dc\u05d8\u05e8\u05e0\u05d8\u05d9\u05d1\u05d5\u05ea.'
    );
  }
  // ── אלטרנטיבות
  else if(/\u05d0\u05dc\u05d8\u05e8\u05e0\u05d8\u05d9\u05d1|\u05d7\u05dc\u05d5\u05e4\u05d5\u05ea|\u05d7\u05dc\u05d5\u05e4\u05d4/.test(h)){
    done=true; label='\u05d0\u05dc\u05d8\u05e8\u05e0\u05d8\u05d9\u05d1\u05d5\u05ea'; noEcho=true;
    sayHebrew(
      '\u05d7\u05dc\u05d5\u05e4\u05d5\u05ea: '+
      '\u05d1\u05de\u05e7\u05d5\u05dd \u05d4\u05ea\u05d7\u05dc, \u05d2\u05dd \u05d4\u05e4\u05e2\u05dc \u05d0\u05d5 \u05e7\u05e8\u05d0. '+
      '\u05d1\u05de\u05e7\u05d5\u05dd \u05d4\u05de\u05e9\u05da, \u05d2\u05dd \u05ea\u05de\u05e9\u05d9\u05da. '+
      '\u05d1\u05de\u05e7\u05d5\u05dd \u05e2\u05e6\u05d5\u05e8, \u05d2\u05dd \u05d4\u05e9\u05d4\u05d4 \u05d0\u05d5 \u05d4\u05e4\u05e1\u05e7. '+
      '\u05d1\u05de\u05e7\u05d5\u05dd \u05e7\u05d3\u05d9\u05de\u05d4, \u05d2\u05dd \u05d4\u05d1\u05d0, \u05d3\u05dc\u05d2, \u05d0\u05d5 \u05d0\u05d1\u05d0. '+
      '\u05d1\u05de\u05e7\u05d5\u05dd \u05d0\u05d7\u05d5\u05e8\u05d4, \u05d2\u05dd \u05e7\u05d5\u05d3\u05dd \u05d0\u05d5 \u05d7\u05d6\u05d5\u05e8 \u05dc\u05e7\u05d8\u05e2 \u05d4\u05e7\u05d5\u05d3\u05dd. '+
      '\u05d1\u05de\u05e7\u05d5\u05dd \u05e8\u05d0\u05e9\u05d5\u05df, \u05d2\u05dd \u05ea\u05d7\u05d9\u05dc\u05ea \u05d4\u05d9\u05d3\u05d9\u05e2\u05d5\u05df. '+
      '\u05d1\u05de\u05e7\u05d5\u05dd \u05e7\u05d8\u05e2 3, \u05d2\u05dd \u05e4\u05e8\u05e7 3 \u05d0\u05d5 \u05e2\u05d1\u05d5\u05e8 \u05dc\u05e7\u05d8\u05e2 3. '+
      '\u05d1\u05de\u05e7\u05d5\u05dd \u05d9\u05d5\u05ea\u05e8 \u05de\u05d4\u05e8, \u05d2\u05dd \u05de\u05d4\u05d9\u05e8. '+
      '\u05d1\u05de\u05e7\u05d5\u05dd \u05d9\u05d5\u05ea\u05e8 \u05dc\u05d0\u05d8, \u05d2\u05dd \u05d0\u05d9\u05d8\u05d9 \u05d0\u05d5 \u05dc\u05d0\u05d8. '+
      '\u05d1\u05de\u05e7\u05d5\u05dd \u05d4\u05d7\u05dc\u05e3 \u05d9\u05d3\u05d9\u05e2\u05d5\u05df, \u05d2\u05dd \u05e9\u05e0\u05d4 \u05d2\u05d9\u05dc\u05d9\u05d5\u05df \u05d0\u05d5 \u05e9\u05e0\u05d4 \u05e7\u05d5\u05d1\u05e5.'
    );
  }
  // ── מהיר
  else if(/\u05de\u05d4\u05d9\u05e8|\u05de\u05d4\u05e8/.test(h)){
    var speeds=[0.6,1,1.2,1.5];
    var idx=speeds.indexOf(rate);
    if(idx<speeds.length-1)spd(speeds[idx+1]);
    done=true; label='\u05de\u05d4\u05d9\u05e8 \u05d9\u05d5\u05ea\u05e8';
  }
  // ── איטי / לאט
  else if(/\u05d0\u05d9\u05d8\u05d9|\u05dc\u05d0\u05d8/.test(h)){
    var speeds2=[0.6,1,1.2,1.5];
    var idx2=speeds2.indexOf(rate);
    if(idx2>0)spd(speeds2[idx2-1]);
    done=true; label='\u05d0\u05d9\u05d8\u05d9 \u05d9\u05d5\u05ea\u05e8';
  }

  if(done){
    btn.classList.add('ok');
    beep(1046,0.15,0.2);
    if(!noEcho) sayHebrew(label);
    vcMsg('\u05d1\u05d5\u05e6\u05e2: '+label,true);
    setTimeout(function(){btn.classList.remove('ok');vcMsg('',false);resetVcBtn();},2500);
  } else {
    beep(330,0.3,0.2);
    sayHebrew('\u05dc\u05d0 \u05d4\u05d1\u05e0\u05ea\u05d9');
    vcMsg('\u05dc\u05d0 \u05d4\u05d1\u05e0\u05ea\u05d9: "'+h+'"',false);
    setTimeout(function(){vcMsg('',false);resetVcBtn();},4000);
  }
}

load();
</script>
</body>
</html>"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ניהול ידיעון</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Heebo:wght@300;400;700;900&display=swap');
:root{
  --bg:#f5f2ec;--surface:#fff;--border:#ddd8ce;
  --green:#2d5f3f;--green-light:#edf5f0;--green-border:#c5deca;
  --red:#c0392b;--red-light:#fef0f0;
  --text:#1a1a18;--muted:#888;--r:16px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Heebo',sans-serif;min-height:100vh}
.wrap{max-width:760px;margin:0 auto;padding:40px 24px}
h1{font-size:32px;font-weight:900;margin-bottom:3px}
.sub{color:var(--muted);font-size:14px;margin-bottom:36px}
.card{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:28px;margin-bottom:20px}
.card-title{font-size:17px;font-weight:700;margin-bottom:16px}
.llink{display:flex;align-items:center;gap:10px;background:var(--green-light);
  border:1px solid var(--green-border);border-radius:10px;padding:14px 16px;
  text-decoration:none;color:var(--green);font-weight:700;font-size:15px;transition:opacity .2s}
.llink:hover{opacity:.85}
/* upload */
#dz{border:2px dashed var(--border);border-radius:12px;padding:48px 20px;
  text-align:center;cursor:pointer;transition:all .2s;background:var(--bg)}
#dz:hover,#dz.over{border-color:var(--green);background:var(--green-light)}
#dz .ic{font-size:38px;margin-bottom:10px}
#dz .ht{font-size:16px;font-weight:700;margin-bottom:4px}
#dz .sb2{font-size:13px;color:var(--muted)}
#fi{display:none}
#fn{margin-top:10px;font-size:13px;color:var(--muted);display:none}
.ubtn{width:100%;margin-top:14px;padding:16px;background:var(--green);color:#fff;
  border:none;border-radius:12px;font-size:17px;font-weight:700;
  font-family:'Heebo',sans-serif;cursor:pointer;transition:opacity .2s;display:none}
.ubtn:hover{opacity:.9}
.ubtn.vis{display:block}
#prog{height:4px;background:var(--border);border-radius:99px;overflow:hidden;margin-top:12px;display:none}
#pfill{height:100%;background:var(--green);border-radius:99px;width:0;transition:width .4s}
#st{margin-top:14px;padding:13px 15px;border-radius:10px;font-size:14px;font-weight:700;display:none}
#st.ok{background:var(--green-light);color:var(--green)}
#st.err{background:var(--red-light);color:var(--red)}
#st.wait{background:#f0f0f0;color:var(--muted)}
#preview{margin-top:14px;display:none}
#preview h3{font-size:13px;color:var(--muted);margin-bottom:8px}
#preview-list{display:flex;flex-wrap:wrap;gap:6px}
.ptag{background:var(--green-light);color:var(--green);border-radius:99px;padding:4px 12px;font-size:13px;font-weight:700}
/* issue rows */
.issue-row{border:1px solid var(--border);border-radius:12px;margin-bottom:12px;overflow:hidden}
.issue-head{display:flex;align-items:center;gap:12px;padding:14px 18px;
  cursor:pointer;background:var(--surface);transition:background .15s;user-select:none}
.issue-head:hover{background:#faf8f4}
.issue-head.active-issue{background:var(--green-light)}
.ih-badge{background:var(--green);color:#fff;font-size:11px;font-weight:700;padding:3px 9px;border-radius:99px;white-space:nowrap}
.ih-info{flex:1;min-width:0}
.ih-title{font-weight:700;font-size:15px}
.ih-meta{font-size:12px;color:var(--muted);margin-top:2px}
.ih-chevron{font-size:13px;color:var(--muted);transition:transform .2s;flex-shrink:0}
.ih-chevron.open{transform:rotate(180deg)}
/* detail panel */
.issue-detail{display:none;border-top:1px solid var(--border);padding:18px;background:#fdfcfa}
.issue-detail.open{display:block}
/* inline edit */
.edit-row{display:flex;gap:8px;align-items:center;margin-bottom:10px}
.edit-row input,.edit-row textarea{flex:1;border:1px solid var(--border);border-radius:8px;
  padding:8px 12px;font-family:'Heebo',sans-serif;font-size:14px;color:var(--text);background:#fff}
.edit-row input:focus,.edit-row textarea:focus{outline:none;border-color:var(--green)}
.edit-row textarea{resize:none;height:48px}
.edit-label{font-size:12px;color:var(--muted);white-space:nowrap;min-width:60px}
.save-btn{padding:8px 16px;background:var(--green);color:#fff;border:none;
  border-radius:8px;font-family:'Heebo',sans-serif;font-weight:700;font-size:13px;cursor:pointer;white-space:nowrap}
.save-btn:hover{opacity:.9}
.saved-flash{font-size:12px;color:var(--green);display:none}
/* segments */
.segs-header{font-size:13px;font-weight:700;color:var(--muted);margin:14px 0 8px;padding-top:14px;border-top:1px solid var(--border)}
.seg-row{display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid #f0ede8}
.seg-row:last-child{border-bottom:none}
.seg-num{font-size:12px;color:var(--muted);min-width:22px;text-align:center}
.seg-name-input{flex:1;border:1px solid transparent;border-radius:6px;padding:5px 8px;
  font-family:'Heebo',sans-serif;font-size:13px;font-weight:700;color:var(--text);background:transparent;cursor:text}
.seg-name-input:focus{border-color:var(--green);background:#fff;outline:none}
.seg-words{font-size:11px;color:var(--muted);white-space:nowrap}
.del-seg-btn{padding:4px 10px;background:transparent;border:1px solid #e0c0c0;
  border-radius:6px;color:var(--red);font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap}
.del-seg-btn:hover{background:var(--red-light)}
/* action row */
.act-row{display:flex;gap:10px;margin-top:16px;padding-top:14px;border-top:1px solid var(--border)}
.act-btn{padding:10px 18px;border:none;border-radius:10px;font-family:'Heebo',sans-serif;
  font-weight:700;font-size:14px;cursor:pointer;transition:opacity .2s}
.act-btn:hover{opacity:.85}
.act-activate{background:var(--green);color:#fff;flex:1}
.act-activate.is-cur{background:#888;cursor:default}
.act-delete{background:var(--red-light);color:var(--red);border:1px solid #e0c0c0}
</style>
</head>
<body>
<div class="wrap">
  <h1>ניהול ידיעון</h1>
  <p class="sub">בארות יצחק</p>

  <div class="card">
    <a href="/" class="llink" target="_blank">
      <span>🎧</span> פתח נגן האזנה (צד אבא)
    </a>
  </div>

  <div class="card">
    <a href="/admin/abbreviations" class="llink">
      <span>📖</span> מילון ראשי תיבות
    </a>
  </div>

  <div class="card">
    <a href="/admin/users" class="llink">
      <span>👥</span> ניהול משתמשים
    </a>
  </div>

  <div class="card">
    <div class="card-title">העלאת ידיעון חדש</div>
    <div id="dz" onclick="document.getElementById('fi').click()"
         ondragover="event.preventDefault();this.classList.add('over')"
         ondragleave="this.classList.remove('over')"
         ondrop="dropFile(event)">
      <div class="ic">📄</div>
      <div class="ht">גרור לכאן קובץ PDF</div>
      <div class="sb2">או לחץ לבחירה</div>
    </div>
    <input type="file" id="fi" accept=".pdf,.docx" onchange="pick(this.files[0])">
    <div id="fn"></div>
    <button class="ubtn" id="ub" onclick="doUpload()">העלה ועבד</button>
    <div id="prog"><div id="pfill"></div></div>
    <div id="st"></div>
    <div id="preview"><h3>קטעים שזוהו:</h3><div id="preview-list"></div></div>
  </div>

  <div class="card">
    <div class="card-title">ידיעונים שמורים</div>
    <div id="ilist"><div style="color:var(--muted);font-size:14px">טוען...</div></div>
  </div>
</div>

<script>
var file=null, curIssueId=null;

function dropFile(e){
  e.preventDefault();
  document.getElementById('dz').classList.remove('over');
  var f=e.dataTransfer.files[0];
  if(f&&(f.name.endsWith('.pdf')||f.name.endsWith('.docx')))pick(f);
}
function pick(f){
  if(!f)return; file=f;
  var fn=document.getElementById('fn');
  fn.style.display='block'; fn.textContent='קובץ: '+f.name;
  document.getElementById('ub').classList.add('vis');
  document.getElementById('preview').style.display='none';
  document.getElementById('st').style.display='none';
}
async function doUpload(){
  if(!file)return;
  var ub=document.getElementById('ub'),st=document.getElementById('st');
  var prog=document.getElementById('prog'),fill=document.getElementById('pfill');
  ub.disabled=true; ub.textContent='מעבד...';
  st.className='wait'; st.style.display='block'; st.textContent='מחלץ טקסט ומחלק לקטעים...';
  prog.style.display='block'; fill.style.width='30%';
  var fd=new FormData(); fd.append('pdf',file);
  try{
    fill.style.width='65%';
    var r=await fetch('/api/upload',{method:'POST',body:fd});
    var d=await r.json();
    fill.style.width='100%';
    if(d.ok){
      st.className='ok';
      st.textContent='הועלה: '+d.title+' ('+d.segments+' קטעים)';
      if(d.preview){
        document.getElementById('preview').style.display='block';
        document.getElementById('preview-list').innerHTML=
          d.preview.map(function(t,i){return '<span class="ptag">'+(i+1)+'. '+t+'</span>';}).join('');
      }
      loadIssues();
    }else{st.className='err';st.textContent='שגיאה: '+d.error;}
  }catch(e){st.className='err';st.textContent='שגיאת חיבור';}
  ub.disabled=false; ub.textContent='העלה ועבד';
}

async function loadIssues(){
  var ir=await fetch('/api/issues');
  var cr=await fetch('/api/current');
  var issues=await ir.json(), cur=await cr.json();
  curIssueId=cur.issue_id||null;
  var list=document.getElementById('ilist');
  if(!issues.length){list.innerHTML='<div style="color:var(--muted);font-size:14px">אין ידיעונים עדיין</div>';return;}
  list.innerHTML=issues.map(function(iss){
    var isCur=iss.id===curIssueId;
    var d=new Date(iss.created_at).toLocaleDateString('he-IL');
    return '<div class="issue-row" id="ir-'+iss.id+'">'+
      '<div class="issue-head'+(isCur?' active-issue':'')+'" onclick="toggleIssue('+iss.id+')">'+
        (isCur?'<span class="ih-badge">פעיל</span>':'')+
        '<div class="ih-info">'+
          '<div class="ih-title" id="iht-'+iss.id+'">'+iss.title+'</div>'+
          '<div class="ih-meta">'+(iss.description||'ללא תיאור')+' &nbsp;·&nbsp; '+d+' &nbsp;·&nbsp; '+iss.seg_count+' קטעים</div>'+
        '</div>'+
        '<span class="ih-chevron" id="chev-'+iss.id+'">▾</span>'+
      '</div>'+
      '<div class="issue-detail" id="det-'+iss.id+'">'+
        '<div class="edit-row"><span class="edit-label">שם:</span>'+
          '<input id="ititle-'+iss.id+'" value="'+iss.title.split('"').join('&quot;')+'" onkeydown="if(event.keyCode==13)saveTitle('+iss.id+')">'+
          '<button class="save-btn" onclick="saveTitle('+iss.id+')">שמור</button>'+
          '<span class="saved-flash" id="sf-t-'+iss.id+'">נשמר</span>'+
        '</div>'+
        '<div class="edit-row"><span class="edit-label">תיאור:</span>'+
          '<textarea id="idesc-'+iss.id+'">'+((iss.description||'').replace(/</g,'&lt;'))+'</textarea>'+
          '<button class="save-btn" onclick="saveDesc('+iss.id+')">שמור</button>'+
          '<span class="saved-flash" id="sf-d-'+iss.id+'">נשמר</span>'+
        '</div>'+
        '<div class="segs-header">קטעים</div>'+
        '<div id="segs-'+iss.id+'"><div style="color:var(--muted);font-size:13px">טוען...</div></div>'+
        '<div class="act-row">'+
          '<button class="act-btn act-activate'+(isCur?' is-cur':'')+'" id="actbtn-'+iss.id+'" onclick="'+(isCur?'':('activateIssue('+iss.id+')'))+'">'+(isCur?'פעיל כעת':'הפעל ידיעון זה')+'</button>'+
          '<button class="act-btn act-delete" onclick="deleteIssue('+iss.id+')">מחק ידיעון</button>'+
        '</div>'+
      '</div>'+
    '</div>';
  }).join('');
}

function toggleIssue(id){
  var det=document.getElementById('det-'+id);
  var chev=document.getElementById('chev-'+id);
  var isOpen=det.classList.contains('open');
  document.querySelectorAll('.issue-detail').forEach(function(el){el.classList.remove('open');});
  document.querySelectorAll('.ih-chevron').forEach(function(el){el.classList.remove('open');});
  if(!isOpen){det.classList.add('open');chev.classList.add('open');loadSegments(id);}
}

async function loadSegments(issueId){
  var el=document.getElementById('segs-'+issueId);
  var r=await fetch('/api/segments/'+issueId);
  var segs=await r.json();
  if(!segs.length){el.innerHTML='<div style="color:var(--muted);font-size:13px">אין קטעים</div>';return;}
  el.innerHTML=segs.map(function(s){
    var words=s.body.split(' ').length;
    return '<div class="seg-row" id="sr-'+s.id+'">'+
      '<span class="seg-num">'+(s.position+1)+'</span>'+
      '<input class="seg-name-input" id="sn-'+s.id+'" value="'+s.title.split('"').join('&quot;')+
        '" onblur="renameSeg('+s.id+','+issueId+')" onkeydown="if(event.keyCode==13)this.blur()">'+
      '<span class="seg-words">'+words+' \u05de</span>'+
      '<button class="del-seg-btn" onclick="deleteSeg('+s.id+','+issueId+')">מחק</button>'+
    '</div>';
  }).join('');
}

function flash(id){
  var el=document.getElementById(id);
  if(!el)return;
  el.style.display='inline';
  setTimeout(function(){el.style.display='none';},1500);
}

async function saveTitle(id){
  var val=document.getElementById('ititle-'+id).value;
  await fetch('/api/update_issue',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({issue_id:id,title:val})});
  document.getElementById('iht-'+id).textContent=val;
  flash('sf-t-'+id);
}
async function saveDesc(id){
  var val=document.getElementById('idesc-'+id).value;
  await fetch('/api/update_issue',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({issue_id:id,description:val})});
  flash('sf-d-'+id);
  loadIssues();
}
async function renameSeg(segId,issueId){
  var val=document.getElementById('sn-'+segId).value;
  await fetch('/api/rename_segment',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({segment_id:segId,title:val})});
}
async function activateIssue(id){
  if(!confirm('\u05dc\u05d4\u05e4\u05e2\u05d9\u05dc \u05d9\u05d3\u05d9\u05e2\u05d5\u05df \u05d6\u05d4 \u05dc\u05db\u05dc \u05d4\u05de\u05e9\u05ea\u05de\u05e9\u05d9\u05dd \u05d5\u05dc\u05d0\u05e4\u05e1 \u05d0\u05ea \u05d4\u05e4\u05e8\u05e7 \u05e9\u05dc\u05d4\u05dd \u05dc\u05e4\u05e8\u05e7 1?'))return;
  var r=await fetch('/api/activate_issue_all',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({issue_id:id})});
  var d=await r.json();
  if(d.ok) alert('\u05e2\u05d5\u05d3\u05db\u05df '+d.updated+' \u05de\u05e9\u05ea\u05de\u05e9\u05d9\u05dd');
  loadIssues();
}
async function deleteIssue(id){
  if(!confirm('למחוק את הידיעון וכל קטעיו?'))return;
  await fetch('/api/delete_issue',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({issue_id:id})});
  loadIssues();
}
async function deleteSeg(segId,issueId){
  if(!confirm('למחוק קטע זה?'))return;
  await fetch('/api/delete_segment',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({segment_id:segId,issue_id:issueId})});
  loadSegments(issueId);
}
loadIssues();
</script>
</body>
</html>"""

@app.route("/admin/abbreviations")
def admin_abbreviations():
    return render_template_string(ABBR_ADMIN_HTML)

ABBR_ADMIN_HTML = """<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>מילון ראשי תיבות</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Heebo:wght@300;400;700;900&display=swap');
:root{
  --bg:#f5f2ec;--surface:#fff;--border:#ddd8ce;
  --green:#2d5f3f;--green-light:#edf5f0;--green-border:#c5deca;
  --red:#c0392b;--red-light:#fef0f0;--text:#1a1a18;--muted:#888;--r:16px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Heebo',sans-serif;min-height:100vh}
.wrap{max-width:820px;margin:0 auto;padding:36px 20px}
h1{font-size:28px;font-weight:900;margin-bottom:4px}
.sub{color:var(--muted);font-size:14px;margin-bottom:28px}
.back{display:inline-flex;align-items:center;gap:6px;color:var(--green);font-weight:700;
  text-decoration:none;font-size:14px;margin-bottom:24px}
.back:hover{opacity:.8}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:18px;flex-wrap:wrap}
.toolbar input[type=search]{flex:1;min-width:180px;border:1px solid var(--border);
  border-radius:10px;padding:9px 14px;font-family:'Heebo',sans-serif;font-size:14px;
  background:#fff;color:var(--text)}
.toolbar input:focus{outline:none;border-color:var(--green)}
.add-btn{padding:9px 20px;background:var(--green);color:#fff;border:none;
  border-radius:10px;font-family:'Heebo',sans-serif;font-weight:700;font-size:14px;
  cursor:pointer;white-space:nowrap}
.add-btn:hover{opacity:.9}
.save-all-btn{padding:9px 20px;background:#1a6b3f;color:#fff;border:none;
  border-radius:10px;font-family:'Heebo',sans-serif;font-weight:700;font-size:14px;
  cursor:pointer;white-space:nowrap;display:none}
.save-all-btn.vis{display:inline-block}
.save-all-btn:hover{opacity:.9}
.stats{font-size:13px;color:var(--muted);flex:1;text-align:left}
.section-hdr{background:#f0ede8;font-size:12px;font-weight:700;color:var(--muted);
  padding:7px 14px}
table{width:100%;border-collapse:collapse;background:var(--surface);
  border-radius:var(--r);overflow:hidden;border:1px solid var(--border);margin-bottom:20px}
thead th{padding:11px 14px;font-size:12px;font-weight:700;color:var(--muted);
  text-align:right;background:#faf8f4;border-bottom:1px solid var(--border)}
tbody tr{border-bottom:1px solid #f0ede8;transition:background .1s}
tbody tr:last-child{border-bottom:none}
tbody tr:hover{background:#faf8f4}
tbody tr.dirty{background:#fffbea}
tbody tr.new-row{background:#f0fff4}
td{padding:7px 12px;font-size:14px;vertical-align:middle}
td.td-abbr{font-weight:700;white-space:nowrap;width:110px}
td.td-exp{min-width:160px}
td.td-count{width:65px;text-align:center;color:var(--muted);font-size:13px}
td.td-act{width:90px;white-space:nowrap;text-align:center}
.cell-input{width:100%;border:1px solid transparent;border-radius:6px;
  padding:4px 8px;font-family:'Heebo',sans-serif;font-size:14px;color:var(--text);
  background:transparent;cursor:text;transition:border-color .15s,background .15s}
.cell-input:focus{border-color:var(--green);background:#fff;outline:none}
.cell-input.abbr-input{font-weight:700}
.cell-input:not(:focus):hover{background:#f5f5f0}
.del-row-btn{padding:4px 10px;background:transparent;border:1px solid #e0c0c0;
  border-radius:7px;color:var(--red);font-size:12px;font-weight:700;cursor:pointer}
.del-row-btn:hover{background:var(--red-light)}
.ltr-char{font-size:22px;font-weight:700;text-align:center;line-height:1}
.empty{padding:40px;text-align:center;color:var(--muted);font-size:15px}
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);
  background:var(--green);color:#fff;padding:12px 28px;border-radius:99px;
  font-weight:700;font-size:15px;opacity:0;transition:opacity .3s;pointer-events:none}
#toast.show{opacity:1}
</style>
</head>
<body>
<div class="wrap">
  <a href="/admin" class="back">← חזרה לניהול</a>
  <h1>מילון ראשי תיבות</h1>
  <p class="sub">עריכה, הוספה ומחיקה. שינויים מסומנים בצהוב — לחץ "שמור הכל" לשמירה.</p>

  <div class="toolbar">
    <input type="search" id="search" placeholder="חיפוש..." oninput="filterRows()">
    <button class="add-btn" onclick="addRow()">+ הוסף ביטוי</button>
    <button class="save-all-btn" id="saveAllBtn" onclick="saveAll()">💾 שמור הכל</button>
    <span class="stats" id="stats"></span>
  </div>

  <div id="tables"></div>
</div>
<div id="toast"></div>

<script>
var rows=[];
var dirty=new Set(); // origAbbr keys that changed

function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }

async function load(){
  var r=await fetch('/api/abbreviations');
  rows=await r.json();
  dirty.clear();
  renderAll(rows);
}

function renderAll(data){
  var q=(document.getElementById('search').value||'').trim().toLowerCase();
  var letters=data.filter(function(r){return r.abbr.startsWith('__');});
  var abbrevs=data.filter(function(r){return !r.abbr.startsWith('__');});
  var fA=q?abbrevs.filter(function(r){return r.abbr.toLowerCase().includes(q)||(r.expansion||'').includes(q);}):abbrevs;
  var fL=q?letters.filter(function(r){return r.abbr.slice(2).includes(q)||(r.expansion||'').includes(q);}):letters;
  var unresolved=abbrevs.filter(function(r){return !r.expansion;}).length;
  document.getElementById('stats').textContent=
    fA.length+' ראשי תיבות'+(unresolved?' ('+unresolved+' ממתינים)':'')+
    ' | '+fL.length+' שמות אותיות';
  var container=document.getElementById('tables');
  container.innerHTML='';
  if(fA.length) container.appendChild(buildTable('ראשי תיבות', fA, false));
  if(fL.length) container.appendChild(buildTable('שמות אותיות', fL, true));
}

function buildTable(title, data, isLetters){
  var tbl=document.createElement('table');
  tbl.innerHTML='<thead><tr>'+
    '<th>'+(isLetters?'אות':'ראשי תיבות')+'</th>'+
    '<th>'+(isLetters?'הגייה':'פיענוח')+'</th>'+
    (isLetters?'':'<th>מופעים</th>')+
    '<th></th>'+
    '</tr></thead>';
  var hdr=document.createElement('tr');
  hdr.innerHTML='<td colspan="'+(isLetters?3:4)+'" class="section-hdr">'+title+'</td>';
  var tb=document.createElement('tbody');
  tb.appendChild(hdr);
  data.forEach(function(row){ tb.appendChild(makeRow(row,isLetters,false)); });
  tbl.appendChild(tb);
  return tbl;
}

function makeRow(rowData, isLetter, isNew){
  var abbr=rowData.abbr, expansion=rowData.expansion||'', count=rowData.count||0;
  var tr=document.createElement('tr');
  if(isNew) tr.classList.add('new-row');
  tr.dataset.origAbbr=abbr;
  tr.dataset.isLetter=isLetter?'1':'0';

  var abbrCell, countCell, delBtn;
  if(isLetter){
    abbrCell='<td class="td-abbr"><div class="ltr-char">'+esc(abbr.slice(2))+'</div></td>';
    countCell='';
    delBtn='';
  } else {
    abbrCell='<td class="td-abbr"><input class="cell-input abbr-input" data-field="abbr"'+(isNew?'':' readonly')+' value="'+esc(abbr)+'" placeholder="ביטוי" oninput="markDirty(this)"></td>';
    countCell='<td class="td-count"><input class="cell-input" data-field="count" type="number" min="0" value="'+count+'" style="text-align:center" oninput="markDirty(this)"></td>';
    delBtn='<button class="del-row-btn" onclick="delRow(this)">מחק</button>';
  }

  tr.innerHTML=abbrCell+
    '<td class="td-exp"><input class="cell-input" data-field="expansion" value="'+esc(expansion)+'" placeholder="'+(isLetter?'הגייה':'פיענוח')+'" oninput="markDirty(this)"></td>'+
    countCell+
    '<td class="td-act">'+delBtn+'</td>';
  return tr;
}

function markDirty(input){
  var tr=input.closest('tr');
  tr.classList.add('dirty');
  dirty.add(tr.dataset.origAbbr);
  document.getElementById('saveAllBtn').classList.add('vis');
}

function filterRows(){ renderAll(rows); }

function addRow(){
  // Add to top of first table or create one
  var firstTb=document.querySelector('#tables table tbody');
  if(!firstTb){
    // No table yet — render empty table
    document.getElementById('tables').innerHTML='<table><thead><tr><th>ראשי תיבות</th><th>פיענוח</th><th>מופעים</th><th></th></tr></thead><tbody id="newTb"></tbody></table>';
    firstTb=document.getElementById('newTb');
  }
  var tr=makeRow({abbr:'',expansion:'',count:0},false,true);
  firstTb.insertBefore(tr, firstTb.children[0]);
  tr.querySelector('[data-field=abbr]').focus();
  tr.classList.add('dirty');
  document.getElementById('saveAllBtn').classList.add('vis');
}

async function saveAll(){
  var btn=document.getElementById('saveAllBtn');
  btn.textContent='שומר...'; btn.disabled=true;
  var trs=document.querySelectorAll('tbody tr[data-orig-abbr]');
  var promises=[];
  trs.forEach(function(tr){
    if(!tr.classList.contains('dirty') && !tr.classList.contains('new-row')) return;
    var origAbbr=tr.dataset.origAbbr;
    var isLetter=tr.dataset.isLetter==='1';
    var expansion=(tr.querySelector('[data-field=expansion]')||{value:''}).value.trim();
    if(isLetter){
      promises.push(fetch('/api/abbreviations/save',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({abbr:origAbbr, expansion:expansion, count:0})}));
    } else {
      var abbrInput=tr.querySelector('[data-field=abbr]');
      var abbr=abbrInput?abbrInput.value.trim():origAbbr;
      if(!abbr) return;
      var count=parseInt((tr.querySelector('[data-field=count]')||{value:0}).value)||0;
      var p=Promise.resolve();
      if(origAbbr && origAbbr!==abbr && origAbbr!==''){
        p=fetch('/api/abbreviations/delete',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({abbr:origAbbr})});
      }
      promises.push(p.then(function(){
        return fetch('/api/abbreviations/save',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({abbr:abbr, expansion:expansion, count:count})});
      }));
    }
  });
  await Promise.all(promises);
  btn.textContent='💾 שמור הכל'; btn.disabled=false;
  btn.classList.remove('vis');
  showToast('נשמר!');
  await load();
}

async function delRow(btn){
  var tr=btn.closest('tr');
  var abbr=tr.dataset.origAbbr;
  if(abbr && abbr!=='' && !confirm('למחוק את "'+abbr+'"?')) return;
  if(abbr && abbr!==''){
    await fetch('/api/abbreviations/delete',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({abbr:abbr})});
  }
  tr.remove();
  var r=await fetch('/api/abbreviations'); rows=await r.json();
  document.getElementById('stats').textContent=
    rows.filter(function(r){return !r.abbr.startsWith('__');}).length+' ראשי תיבות | '+
    rows.filter(function(r){return !r.abbr.startsWith('__')&&!r.expansion;}).length+' ממתינים';
}

function showToast(msg){
  var t=document.getElementById('toast');
  t.textContent=msg; t.classList.add('show');
  setTimeout(function(){t.classList.remove('show');},2200);
}

load();
</script>
</body>
</html>"""

USERS_ADMIN_HTML = """<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ניהול משתמשים</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Heebo:wght@300;400;700;900&display=swap');
:root{
  --bg:#f5f2ec;--surface:#fff;--border:#ddd8ce;
  --green:#2d5f3f;--green-light:#edf5f0;--green-border:#c5deca;
  --red:#c0392b;--red-light:#fef0f0;--text:#1a1a18;--muted:#888;--r:16px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Heebo',sans-serif;min-height:100vh}
.wrap{max-width:900px;margin:0 auto;padding:36px 20px}
h1{font-size:28px;font-weight:900;margin-bottom:4px}
.sub{color:var(--muted);font-size:14px;margin-bottom:28px}
.back{display:inline-flex;align-items:center;gap:6px;color:var(--green);font-weight:700;
  text-decoration:none;font-size:14px;margin-bottom:24px}
.back:hover{opacity:.8}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:18px;flex-wrap:wrap}
.add-btn{padding:9px 20px;background:var(--green);color:#fff;border:none;
  border-radius:10px;font-family:'Heebo',sans-serif;font-weight:700;font-size:14px;cursor:pointer}
.add-btn:hover{opacity:.9}
.stats{font-size:13px;color:var(--muted);flex:1}
table{width:100%;border-collapse:collapse;background:var(--surface);
  border-radius:var(--r);overflow:hidden;border:1px solid var(--border)}
thead th{padding:10px 12px;font-size:12px;font-weight:700;color:var(--muted);
  text-align:right;background:#faf8f4;border-bottom:1px solid var(--border)}
tbody tr{border-bottom:1px solid #f0ede8;transition:background .1s}
tbody tr:last-child{border-bottom:none}
tbody tr:hover{background:#faf8f4}
tbody tr.dirty{background:#fffbea}
tbody tr.new-row{background:#f0fff4}
td{padding:7px 10px;font-size:13px;vertical-align:middle}
.cell-input{width:100%;border:1px solid transparent;border-radius:6px;
  padding:4px 8px;font-family:'Heebo',sans-serif;font-size:13px;color:var(--text);
  background:transparent;cursor:text}
.cell-input:focus{border-color:var(--green);background:#fff;outline:none}
.cell-input:not(:focus):hover{background:#f5f5f0}
select.cell-input{cursor:pointer}
.del-btn{padding:4px 10px;background:transparent;border:1px solid #e0c0c0;
  border-radius:7px;color:var(--red);font-size:12px;font-weight:700;cursor:pointer}
.del-btn:hover{background:var(--red-light)}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:700}
.badge-on{background:var(--green-light);color:var(--green)}
.badge-off{background:#f0f0f0;color:var(--muted)}
.save-all-btn{padding:9px 20px;background:#1a6b3f;color:#fff;border:none;
  border-radius:10px;font-family:'Heebo',sans-serif;font-weight:700;font-size:14px;
  cursor:pointer;display:none}
.save-all-btn.vis{display:inline-block}
.save-all-btn:hover{opacity:.9}
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);
  background:var(--green);color:#fff;padding:12px 28px;border-radius:99px;
  font-weight:700;font-size:15px;opacity:0;transition:opacity .3s;pointer-events:none}
#toast.show{opacity:1}
.muted{color:var(--muted);font-size:12px}
</style>
</head>
<body>
<div class="wrap">
  <a href="/admin" class="back">← חזרה לניהול</a>
  <h1>ניהול משתמשים</h1>
  <p class="sub">הוספה, עריכה ומחיקה של משתמשים</p>

  <div class="toolbar">
    <button class="add-btn" onclick="addRow()">+ הוסף משתמש</button>
    <button class="save-all-btn" id="saveAllBtn" onclick="saveAll()">💾 שמור הכל</button>
    <span class="stats" id="stats"></span>
  </div>

  <table>
    <thead>
      <tr>
        <th>שם</th>
        <th>פעיל</th>
        <th>מהירות</th>
        <th>הודעת פתיחה</th>
        <th>ידיעון נוכחי</th>
        <th>פרק נוכחי</th>
        <th>כניסה אחרונה</th>
        <th></th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>
</div>
<div id="toast"></div>

<script>
var rows=[], issues=[];

async function load(){
  var r1=await fetch('/api/users');
  rows=await r1.json();
  var r2=await fetch('/api/issues');
  issues=await r2.json();
  render();
}

function render(){
  document.getElementById('stats').textContent=rows.length+' משתמשים | '+
    rows.filter(function(r){return r.active;}).length+' פעילים';
  var tb=document.getElementById('tbody');
  tb.innerHTML='';
  rows.forEach(function(r){ tb.appendChild(makeRow(r,false)); });
}

function issueOptions(selId){
  var opts='<option value="">— ללא —</option>';
  issues.forEach(function(i){
    opts+='<option value="'+i.id+'"'+(i.id==selId?' selected':'')+'>'+esc(i.title)+'</option>';
  });
  return opts;
}

function segOptions(issueId, curPos){
  if(!issueId) return '<option value="0">—</option>';
  var issue=issues.find(function(i){return i.id==issueId;});
  var count=issue?issue.seg_count:0;
  var opts='';
  for(var i=0;i<count;i++){
    opts+='<option value="'+i+'"'+(i==curPos?' selected':'')+'>פרק '+(i+1)+'</option>';
  }
  return opts||'<option value="0">פרק 1</option>';
}

function makeRow(r, isNew){
  var tr=document.createElement('tr');
  if(isNew) tr.classList.add('new-row');
  tr.dataset.id=r.id||'';
  var lastSeen=r.last_seen?new Date(r.last_seen).toLocaleString('he-IL'):'—';
  tr.innerHTML=
    '<td><input class="cell-input" data-f="name" value="'+esc(r.name||'')+'" placeholder="שם" oninput="markDirty(this)"></td>'+
    '<td><select class="cell-input" data-f="active" onchange="markDirty(this)">'+
      '<option value="1"'+(r.active?' selected':'')+'>פעיל</option>'+
      '<option value="0"'+(!r.active?' selected':'')+'>לא פעיל</option>'+
    '</select></td>'+
    '<td><select class="cell-input" data-f="play_speed" onchange="markDirty(this)">'+
      '<option value="0.6"'+(r.play_speed==0.6?' selected':'')+'>x0.6</option>'+
      '<option value="1"'+(r.play_speed==1||!r.play_speed?' selected':'')+'>x1</option>'+
      '<option value="1.2"'+(r.play_speed==1.2?' selected':'')+'>x1.2</option>'+
      '<option value="1.5"'+(r.play_speed==1.5?' selected':'')+'>x1.5</option>'+
    '</select></td>'+
    '<td><select class="cell-input" data-f="show_greeting" onchange="markDirty(this)">'+
      '<option value="1"'+(r.show_greeting?' selected':'')+'>כן</option>'+
      '<option value="0"'+(!r.show_greeting?' selected':'')+'>לא</option>'+
    '</select></td>'+
    '<td><select class="cell-input" data-f="issue_id" onchange="issueChanged(this);markDirty(this)">'+issueOptions(r.issue_id)+'</select></td>'+
    '<td><select class="cell-input" data-f="segment_pos" onchange="markDirty(this)">'+segOptions(r.issue_id, r.segment_pos)+'</select></td>'+
    '<td class="muted">'+lastSeen+'</td>'+
    '<td><button class="del-btn" onclick="delRow(this)">מחק</button></td>';
  return tr;
}

function issueChanged(sel){
  var tr=sel.closest('tr');
  var issueId=sel.value||null;
  var segSel=tr.querySelector('[data-f=segment_pos]');
  segSel.innerHTML=segOptions(issueId,0);
}

function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }

function markDirty(el){
  el.closest('tr').classList.add('dirty');
  document.getElementById('saveAllBtn').classList.add('vis');
}

function addRow(){
  var tb=document.getElementById('tbody');
  var tr=makeRow({name:'',active:true,play_speed:1,show_greeting:true,issue_id:null},true);
  tb.insertBefore(tr, tb.firstChild);
  tr.querySelector('[data-f=name]').focus();
  tr.classList.add('dirty');
  document.getElementById('saveAllBtn').classList.add('vis');
}

async function saveAll(){
  var btn=document.getElementById('saveAllBtn');
  btn.textContent='שומר...'; btn.disabled=true;
  var trs=document.querySelectorAll('#tbody tr.dirty');
  var promises=[];
  trs.forEach(function(tr){
    var name=tr.querySelector('[data-f=name]').value.trim();
    if(!name) return;
    var payload={
      id: tr.dataset.id||null,
      name: name,
      active: tr.querySelector('[data-f=active]').value==='1',
      play_speed: parseFloat(tr.querySelector('[data-f=play_speed]').value),
      show_greeting: tr.querySelector('[data-f=show_greeting]').value==='1',
      issue_id: tr.querySelector('[data-f=issue_id]').value||null,
      segment_pos: parseInt(tr.querySelector('[data-f=segment_pos]').value)||0
    };
    promises.push(fetch('/api/users/save',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)}));
  });
  await Promise.all(promises);
  btn.textContent='💾 שמור הכל'; btn.disabled=false;
  btn.classList.remove('vis');
  showToast('נשמר!');
  await load();
}

async function delRow(btn){
  var tr=btn.closest('tr');
  var id=tr.dataset.id;
  var name=tr.querySelector('[data-f=name]').value;
  if(id && !confirm('למחוק את "'+name+'"?')) return;
  if(id){
    await fetch('/api/users/delete',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:parseInt(id)})});
    await load();
  } else {
    tr.remove();
  }
}

function showToast(msg){
  var t=document.getElementById('toast');
  t.textContent=msg; t.classList.add('show');
  setTimeout(function(){t.classList.remove('show');},2200);
}

load();
</script>
</body>
</html>"""

BYE_HTML = """<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>סיום האזנה</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:#1a1a18;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:24px;
  font-family:'Heebo',Helvetica,sans-serif;color:#fff;text-align:center;padding:24px}
.ic{font-size:56px}
h1{font-size:24px;font-weight:900}
p{font-size:16px;color:#aaa}
a{margin-top:8px;display:inline-block;padding:16px 36px;
  background:#2d5f3f;color:#fff;border-radius:16px;
  font-size:18px;font-weight:700;text-decoration:none}
</style>
</head>
<body>
<div class="ic">🔇</div>
<h1>סיום ההאזנה</h1>
<p>אפשר לסגור את הדפדפן</p>
<a href="/">חזור לאפליקציה</a>
<script>
// Push a dummy state so Back button stays on this page instead of going to the player
history.pushState(null, '', window.location.href);
window.addEventListener('popstate', function(){
  history.pushState(null, '', window.location.href);
});
</script>
</body>
</html>"""

@app.route("/manifest.json")
def manifest():
    import json as _json
    data = {
        "name": "ידיעון בארות יצחק",
        "short_name": "ידיעון",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f0f12",
        "theme_color": "#0f0f12",
        "lang": "he",
        "dir": "rtl",
        "icons": [
            {"src": "https://cdn.jsdelivr.net/npm/twemoji@14/assets/72x72/1f4d6.png",
             "sizes": "72x72", "type": "image/png"},
            {"src": "https://cdn.jsdelivr.net/npm/twemoji@14/assets/72x72/1f4d6.png",
             "sizes": "192x192", "type": "image/png"}
        ]
    }
    from flask import Response
    return Response(_json.dumps(data, ensure_ascii=False),
                    mimetype="application/manifest+json")

@app.route("/bye")
def bye():
    return render_template_string(BYE_HTML)

# ─── STARTUP ─────────────────────────────────────────────────────────────────

# Initialize DB on first request
with app.app_context():
    try:
        init_db()
    except Exception:
        pass

@app.route("/fix")
def fix():
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO listener_state (id, issue_id, segment_position) VALUES (1, NULL, 0) ON CONFLICT (id) DO NOTHING")
    conn.commit(); cur.close(); conn.close()
    return "ok"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

