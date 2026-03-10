"""
ידיעון בארות יצחק — נגן קול
Flask + PostgreSQL (Railway) | Web Speech API (iOS)
"""
import os, re, json
from datetime import datetime
from itertools import groupby
from flask import Flask, request, jsonify, render_template_string
import pdfplumber
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

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
        INSERT INTO listener_state (id, issue_id, segment_position)
        VALUES (1, NULL, 0)
        ON CONFLICT (id) DO NOTHING;
    """)
    conn.commit()
    cur.close()
    conn.close()

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

def extract_text_from_pdf(path: str) -> str:
    pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            raw = page.extract_text() or ""
            lines = raw.split("\n")
            # Fix RTL, then rejoin lines that were split into single letters by nikud
            fixed = [rejoin_spaced_letters(fix_rtl_line(l)) for l in lines]
            pages.append("\n".join(fixed))
    full = "\n\n".join(pages)
    full = re.sub(r"\n\d{1,3}\n", "\n", full)   # strip page numbers
    full = re.sub(r"[■●•◆▪]", "", full)          # strip bullets
    full = re.sub(r"\n{3,}", "\n\n", full)
    full = strip_nikud(full)
    return full.strip()

def extract_raw_head(path: str) -> str:
    """Extract first-page text WITHOUT rejoin, for parasha detection."""
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
    head = strip_nikud(text[:800])
    search_head = strip_nikud(raw_head) if raw_head else head
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

@app.route("/api/upload", methods=["POST"])
def upload():
    if "pdf" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["pdf"]
    tmp = "/tmp/upload.pdf"
    f.save(tmp)

    try:
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
            cur.execute(
                "INSERT INTO segments (issue_id, position, title, body) VALUES (%s,%s,%s,%s)",
                (issue_id, i, seg["title"], seg["body"])
            )
        cur.execute("UPDATE listener_state SET issue_id=%s, segment_position=0 WHERE id=1",
                    (issue_id,))
        conn.commit()
        cur.close(); conn.close()

        return jsonify({"ok": True, "issue_id": issue_id, "title": title,
                        "segments": len(segments),
                        "preview": [s["title"] for s in segments]})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/current")
def current():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM listener_state WHERE id=1")
    state = cur.fetchone()    
    if not state or not state["issue_id"]:
        cur.close(); conn.close()
        return jsonify({"no_issue": True})

    cur.execute("SELECT * FROM issues WHERE id=%s", (state["issue_id"],))
    issue = cur.fetchone()
    cur.execute("SELECT * FROM segments WHERE issue_id=%s ORDER BY position",
                (state["issue_id"],))
    segs = cur.fetchall()
    cur.close(); conn.close()

    return jsonify({
        "issue_title": issue["title"],
        "issue_id": issue["id"],
        "segments": [{"position": s["position"], "title": s["title"],
                       "body": s["body"]} for s in segs],
        "current_position": state["segment_position"],
        "total": len(segs)
    })

@app.route("/api/set_position", methods=["POST"])
def set_position():
    data = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE listener_state SET segment_position=%s WHERE id=1",
                (data["position"],))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

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

@app.route("/api/set_issue", methods=["POST"])
def set_issue():
    data = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE listener_state SET issue_id=%s, segment_position=0 WHERE id=1",
                (data["issue_id"],))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

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

# ─── HTML TEMPLATES ──────────────────────────────────────────────────────────

LISTENER_HTML = """<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
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
var S=null,synth=window.speechSynthesis,utt=null,playing=false,rate=1,heVoice=null;
var currentScreen='blind';

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

// ── Load ─────────────────────────────────────────────────────────
async function load(){
  var r=await fetch('/api/current');
  var d=await r.json();
  document.getElementById('ls').style.display='none';
  if(d.no_issue){
    document.getElementById('blind').style.display='flex';
    document.getElementById('blind-seg').textContent='אין ידיעון זמין';
    return;
  }
  S=d;
  render();
  renderD();
  showBlind();
}

// ── Render ───────────────────────────────────────────────────────
function render(){
  if(!S)return;
  var seg=S.segments[S.current_position];
  document.getElementById('issue-lbl').textContent=S.issue_title;
  document.getElementById('seg-lbl').textContent=seg.title;
  document.getElementById('pos-lbl').textContent=
    '\u05e7\u05d8\u05e2 '+(S.current_position+1)+' \u05de\u05ea\u05d5\u05da '+S.total;
  document.getElementById('body').textContent=seg.body;
  document.getElementById('pfill').style.width=
    ((S.current_position+1)/S.total*100)+'%';
  document.getElementById('ta').scrollTop=0;
  updateBlindSeg();
  renderD();
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
var chunks=[];          // array of strings for current segment
var chunkIdx=0;         // chunk currently being spoken (or next to speak)
var chunkStopped=false; // flag to stop the loop

var MAX_WORDS=20;
var COMMA_MIN=15;
var COMMA_MAX=25;

function splitChunks(text){
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

// ── Hebrew text normalization for TTS ───────────────────────────
var GERSHAYIM_DICT = {
  // ב"ה and variants
  '\u05d1"\u05d4':'\u05d1\u05e2\u05d6\u05e8\u05ea \u05d4\u05e9\u05dd',
  '\u05d1\u05f4\u05d4':'\u05d1\u05e2\u05d6\u05e8\u05ea \u05d4\u05e9\u05dd',
  // ד"ר
  '\u05d3"\u05e8':'\u05d3\u05d5\u05e7\u05d8\u05d5\u05e8',
  '\u05d3\u05f4\u05e8':'\u05d3\u05d5\u05e7\u05d8\u05d5\u05e8',
  // צה"ל
  '\u05e6\u05d4"\u05dc':'\u05e6\u05d1\u05d0 \u05d4\u05d2\u05e0\u05d4 \u05dc\u05d9\u05e9\u05e8\u05d0\u05dc',
  '\u05e6\u05d4\u05f4\u05dc':'\u05e6\u05d1\u05d0 \u05d4\u05d2\u05e0\u05d4 \u05dc\u05d9\u05e9\u05e8\u05d0\u05dc',
  // בע"מ
  '\u05d1\u05e2"\u05de':'\u05d1\u05e2\u05d9\u05e8\u05d1\u05d5\u05df \u05de\u05d5\u05d2\u05d1\u05dc',
  // ז"ל
  '\u05d6"\u05dc':'\u05d6\u05db\u05e8\u05d5\u05e0\u05d5 \u05dc\u05d1\u05e8\u05db\u05d4',
  // שליט"א
  '\u05e9\u05dc\u05d9\u05d8"\u05d0':'\u05e9\u05d9\u05d7\u05d9\u05d4 \u05dc\u05d9\u05d7\u05d9\u05d4 \u05d8\u05d5\u05d1\u05d4 \u05d0\u05de\u05df',
  // זצ"ל
  '\u05d6\u05e6"\u05dc':'\u05d6\u05db\u05e8 \u05e6\u05d3\u05d9\u05e7 \u05dc\u05d1\u05e8\u05db\u05d4',
  // מ"מ
  '\u05de"\u05de':'\u05de\u05de\u05dc\u05d0',
  // ת"ת
  '\u05ea"\u05ea':'\u05ea\u05dc\u05de\u05d5\u05d3 \u05ea\u05d5\u05e8\u05d4',
  // כ"ק
  '\u05db"\u05e7':'\u05db\u05d1\u05d5\u05d3 \u05e7\u05d3\u05d5\u05e9\u05ea\u05d5',
  // מרן
  '\u05de\u05e8"\u05df':'\u05de\u05e8\u05e0\u05d5',
};

var LETTER_NAMES = {
  '\u05d0':'\u05d0\u05dc\u05e3','\u05d1':'\u05d1\u05d9\u05ea','\u05d2':'\u05d2\u05d9\u05de\u05dc',
  '\u05d3':'\u05d3\u05dc\u05ea','\u05d4':'\u05d4\u05d0','\u05d5':'\u05d5\u05d0\u05d5',
  '\u05d6':'\u05d6\u05d9\u05d9\u05df','\u05d7':'\u05d7\u05ea','\u05d8':'\u05d8\u05d9\u05ea',
  '\u05d9':'\u05d9\u05d5\u05d3','\u05db':'\u05db\u05e3','\u05da':'\u05db\u05e3 \u05e1\u05d5\u05e4\u05d9\u05ea',
  '\u05dc':'\u05dc\u05de\u05d3','\u05de':'\u05de\u05dd','\u05dd':'\u05de\u05dd \u05e1\u05d5\u05e4\u05d9\u05ea',
  '\u05e0':'\u05e0\u05d5\u05df','\u05df':'\u05e0\u05d5\u05df \u05e1\u05d5\u05e4\u05d9\u05ea',
  '\u05e1':'\u05e1\u05de\u05da','\u05e2':'\u05e2\u05d9\u05df','\u05e4':'\u05e4\u05d0',
  '\u05e3':'\u05e4\u05d0 \u05e1\u05d5\u05e4\u05d9\u05ea','\u05e6':'\u05e6\u05d3\u05d9',
  '\u05e5':'\u05e6\u05d3\u05d9 \u05e1\u05d5\u05e4\u05d9\u05ea','\u05e7':'\u05e7\u05d5\u05e3',
  '\u05e8':'\u05e8\u05d9\u05e9','\u05e9':'\u05e9\u05d9\u05df','\u05ea':'\u05ea\u05d5\u05d5'
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
  // 1. Replace gershayim words: look up dict first, else expand to letter names
  text=text.replace(/([\u05d0-\u05ea]{1,6})["\u05f4]([\u05d0-\u05ea]{1,3})/g,
    function(match){
      if(GERSHAYIM_DICT[match])return GERSHAYIM_DICT[match];
      // Not in dict — expand all letters to names
      var letters=match.replace(/["\u05f4]/g,'');
      return expandLetterNames(letters);
    });
  // 2. Geresh after single letter: ד' → דלת, etc.
  text=text.replace(/([\u05d0-\u05ea])['\u05f3](?=\s|$)/g,
    function(m,ch){ return LETTER_NAMES[ch]||ch; });
  // 3. Single isolated Hebrew letter (surrounded by spaces/punctuation) → letter name
  text=text.replace(/(?<![^\u05d0-\u05ea\s])(^|\s)([\u05d0-\u05ea])(\s|$)/g,
    function(m,pre,ch,post){ return pre+(LETTER_NAMES[ch]||ch)+post; });
  return text.replace(/ {2,}/g,' ');
}

function speak(){
  if(!S)return;
  var seg=S.segments[S.current_position];
  chunks=splitChunks(normalizeForSpeech(seg.title+'. '+seg.body));
  chunkIdx=0;
  synth.cancel();
  chunkStopped=false;
  onSpeakStart();
  _nextChunk();
}

function resume(){
  if(chunks.length && chunkIdx<chunks.length){
    synth.cancel();
    chunkStopped=false;
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
  showPhrase(text);
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
    setIdle();
    // chunkIdx stays — resume() will restart from this chunk
  }
}

function stop(){
  chunkStopped=true;
  chunks=[]; chunkIdx=0;
  synth.cancel(); setIdle();
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
    S.current_position++;savePos(S.current_position);render();speak();
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
async function savePos(p){
  await fetch('/api/set_position',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({position:p})});
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
function sayHebrew(text){
  var u=new SpeechSynthesisUtterance(text);
  u.lang='he-IL'; u.rate=1.1;
  if(heVoice)u.voice=heVoice;
  synth.speak(u);
}

// iOS requires speechSynthesis to be "unlocked" within a user gesture.
// We do this once per button tap by speaking an empty utterance immediately.
function unlockTTS(){
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
  if(!S){
    sayHebrew('\u05d0\u05d9\u05df \u05d9\u05d3\u05d9\u05e2\u05d5\u05df \u05d6\u05de\u05d9\u05df');
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
  rec.start();
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
  // "האחרון" / "הקובץ האחרון" — oldest = last in DESC list
  else if(/\u05d0\u05d7\u05e8\u05d5\u05df|\u05d0\u05d7\u05e8\u05d5\u05e0/.test(h)){
    targetIssue=issues[issues.length-1];
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
    sayHebrew('\u05e9\u05dc\u05d5\u05dd');
    setTimeout(function(){
      // Try to close (works on Android/Chrome)
      window.close();
      // On iOS window.close() is a no-op — show end screen instead
      setTimeout(function(){
        document.getElementById('blind').style.display='none';
        document.getElementById('end-screen').style.display='flex';
      },400);
    },600);
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
    <div class="card-title">העלאת ידיעון חדש</div>
    <div id="dz" onclick="document.getElementById('fi').click()"
         ondragover="event.preventDefault();this.classList.add('over')"
         ondragleave="this.classList.remove('over')"
         ondrop="dropFile(event)">
      <div class="ic">📄</div>
      <div class="ht">גרור לכאן קובץ PDF</div>
      <div class="sb2">או לחץ לבחירה</div>
    </div>
    <input type="file" id="fi" accept=".pdf" onchange="pick(this.files[0])">
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
  if(f&&f.name.endsWith('.pdf'))pick(f);
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
  await fetch('/api/set_issue',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({issue_id:id})});
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

