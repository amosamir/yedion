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

SECTIONS = [
    (r"בחזרה לתפילות רחוב",       "בחזרה לתפילות רחוב"),
    (r"דרשת שבת",                  "דרשת שבת"),
    (r"משולחן המזכיר",             "משולחן המזכיר"),
    (r"תפילת יחיד",                "תפילת יחיד"),
    (r"כמו רקפות",                 "כמו רקפות"),
    (r"טיל פגש|מתכנית רעות",       "מתכנית רעות"),
    (r"בין שגרת חירום",            "מצוות חרום יישובי"),
    (r"(?m)^מהמועצה\s*$",          "מהמועצה ובית הכנסת"),
    (r"הודעה לחברים על מינוי",    "מהמזכירות"),
    (r"התנועה ואנחנו",             "הקיבוץ הדתי"),
    (r"(?m)^כלבודף\s*$",           "כלבודף"),
    (r"הרכבת כבר עברה",            "הרכבת כבר עברה"),
    (r"ממגילת היסוד",              "ממגילת היסוד"),
    (r"שווה\s*קריאה",              "שווה קריאה"),
    (r"מהמרפאה",                   "מהמרפאה"),
    (r"מעשר כספים מכספי",          "מעשר כספים"),
]

# These three always get their own segment, always at the end in this order
TAIL_ORDER = ["הקיבוץ הדתי", "לוח זמנים", "כלבודף"]

PARASHA_RE = re.compile(
    r"(בראשית|נח|לך לך|וירא|חיי שרה|תולדות|ויצא|וישלח|וישב|מקץ|ויגש|ויחי|"
    r"שמות|וארא|בא|בשלח|יתרו|משפטים|תרומה|תצוה|כי תשא|ויקהל|פקודי|"
    r"ויקרא|צו|שמיני|תזריע|מצורע|אחרי|קדושים|אמור|בהר|בחוקותי|"
    r"במדבר|נשא|בהעלותך|שלח|קרח|חוקת|בלק|פינחס|מטות|מסעי|"
    r"דברים|ואתחנן|עקב|ראה|שופטים|כי תצא|כי תבוא|נצבים|וילך|האזינו|וזאת הברכה)"
)


def extract_text_from_pdf(path: str) -> str:
    pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            raw = page.extract_text() or ""
            # Fix RTL: reverse each line
            lines = raw.split("\n")
            pages.append("\n".join(line[::-1] for line in lines))
    full = "\n\n".join(pages)
    full = re.sub(r"\n\d{1,2}\n", "\n", full)          # strip page numbers
    full = re.sub(r"[■●•◆▪]", "", full)                # strip bullets
    full = re.sub(r"\n{3,}", "\n\n", full)
    return full.strip()


def detect_title(text: str) -> str:
    head = text[:600]
    # Try parasha name
    m = PARASHA_RE.search(head)
    if m:
        title = m.group(0)
        # Check for combined (e.g. כי תשא–פרה)
        rest = head[m.end():]
        m2 = re.search(r"[–\-]\s*(" + PARASHA_RE.pattern + r")", rest[:50])
        if m2:
            title += "–" + m2.group(1)
        m3 = re.search(r"גיליון\s+(\d+)", head)
        if m3:
            return f"ידיעון {title} גיליון {m3.group(1)}"
        return f"ידיעון {title}"
    m3 = re.search(r"גיליון\s+(\d+)", head)
    if m3:
        return f"גיליון {m3.group(1)}"
    return f"ידיעון {datetime.now().strftime('%d.%m.%Y')}"


def split_segments(text: str) -> list[dict]:
    compiled = [(re.compile(pat, re.MULTILINE), title) for pat, title in SECTIONS]

    # Detect לוח זמנים — the last page with prayer times
    lz_match = re.search(r"כ[\s]*י[\s]+ת[\s]*י[\s]*ש[\s]*א.*\nזמני|זמני התפילות", text)

    lines = text.split("\n")
    current = "פתיח"
    tagged = []
    for i, line in enumerate(lines):
        context = "\n".join(l for _, l in tagged[-2:]) + "\n" + line
        for regex, title in compiled:
            if regex.search(context):
                current = title
                break
        # Detect לוח זמנים inline
        if i > len(lines) * 0.8 and re.search(r"זמני התפילות|הדלקת נרות", line):
            current = "לוח זמנים"
        tagged.append((current, line))

    # Group consecutive lines by section
    raw = []
    for sec, grp in groupby(tagged, key=lambda x: x[0]):
        body = "\n".join(l for _, l in grp).strip()
        words = len(body.split())
        raw.append({"title": sec, "body": body, "words": words})

    # Absorb tiny stubs (TOC entries) into previous section
    merged = []
    for s in raw:
        if s["words"] < 25 and merged and s["title"] not in TAIL_ORDER:
            merged[-1]["body"] += "\n\n" + s["body"]
            merged[-1]["words"] += s["words"]
        else:
            merged.append(dict(s))

    # Pack into display segments (~600 words max), never break TAIL_ORDER sections
    MAX = 600
    segments = []
    buf_title = buf_body = ""
    buf_words = 0

    def flush():
        nonlocal buf_title, buf_body, buf_words
        if buf_body.strip():
            segments.append({"title": buf_title, "body": buf_body.strip()})
        buf_title = buf_body = ""
        buf_words = 0

    for s in merged:
        if s["title"] in TAIL_ORDER:
            flush()
            segments.append({"title": s["title"], "body": s["body"]})
            continue
        if buf_words + s["words"] > MAX and buf_words > 80:
            flush()
        if not buf_title:
            buf_title = s["title"]
        buf_body += ("\n\n" if buf_body else "") + s["body"]
        buf_words += s["words"]
    flush()

    # Enforce tail order: move TAIL_ORDER segments to end in correct order
    main = [s for s in segments if s["title"] not in TAIL_ORDER]
    tail = []
    for t in TAIL_ORDER:
        matches = [s for s in segments if s["title"] == t]
        tail.extend(matches)

    return main + tail

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
        title = detect_title(text)
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

# ─── HTML TEMPLATES ──────────────────────────────────────────────────────────

LISTENER_HTML = """<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
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
#app{display:flex;flex-direction:column;height:100dvh;max-width:520px;
  margin:0 auto;padding:max(env(safe-area-inset-top),14px) 16px
  max(env(safe-area-inset-bottom),14px);gap:10px}

/* header */
#hdr{display:flex;flex-direction:column;gap:3px;padding-top:4px}
#issue-lbl{font-size:11px;color:var(--accent);font-weight:700;letter-spacing:.08em;text-transform:uppercase}
#seg-lbl{font-size:21px;font-weight:900;line-height:1.2}
#pos-lbl{font-size:12px;color:var(--muted)}

/* progress */
#pbar{height:3px;background:var(--border);border-radius:99px;overflow:hidden;flex-shrink:0}
#pfill{height:100%;background:var(--accent);border-radius:99px;transition:width .4s ease;width:0}

/* text */
#ta{flex:1;background:var(--surface);border-radius:var(--r);padding:20px;
  overflow-y:auto;border:1px solid var(--border);-webkit-overflow-scrolling:touch}
#body{font-size:19px;line-height:1.95;white-space:pre-wrap}

/* playing indicator */
#pi{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--accent);
  opacity:0;transition:opacity .3s;height:18px;flex-shrink:0}
#pi.on{opacity:1}
.bars{display:flex;gap:3px;align-items:flex-end;height:14px}
.bars span{width:3px;background:var(--accent);border-radius:2px;
  animation:b .8s ease-in-out infinite}
.bars span:nth-child(2){animation-delay:.15s}
.bars span:nth-child(3){animation-delay:.3s}
@keyframes b{0%,100%{height:3px}50%{height:13px}}

/* main controls */
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

/* speed */
#spd{display:flex;gap:7px;justify-content:center;flex-shrink:0;padding-bottom:2px}
.sb{background:var(--surface2);border:1px solid var(--border);color:var(--muted);
  border-radius:99px;padding:8px 16px;font-size:15px;
  font-family:'Heebo',sans-serif;font-weight:700;cursor:pointer;transition:all .2s}
.sb.on{background:var(--accent);color:#111;border-color:var(--accent)}

/* loading */
#ls{position:fixed;inset:0;background:var(--bg);display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:14px;z-index:99;
  font-size:17px;color:var(--muted);text-align:center;padding:36px}
#ls .em{font-size:52px}
#ls.h{display:none}

/* list button */
#listbtn{position:fixed;top:max(env(safe-area-inset-top),14px);left:16px;
  background:var(--surface2);border:1px solid var(--border);
  color:var(--text);width:44px;height:44px;border-radius:12px;
  font-size:20px;cursor:pointer;z-index:10;
  display:flex;align-items:center;justify-content:center}

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

/* voice */
#vcbtn{width:100%;padding:18px;background:var(--surface2);border:2px solid var(--border);
  border-radius:var(--r);color:var(--text);font-family:'Heebo',sans-serif;
  font-size:20px;font-weight:700;cursor:pointer;transition:all .2s;flex-shrink:0}
#vcbtn.listening{background:#3a1a1a;border-color:#e87a7a;color:#e87a7a;
  animation:pulse 1s ease-in-out infinite}
#vcbtn.ok{border-color:var(--accent);color:var(--accent)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.6}}
#vcmsg{text-align:center;font-size:13px;color:var(--muted);min-height:18px;flex-shrink:0}
</style>
</head>
<body>
<div id="ls"><div class="em">📖</div><div id="lmsg">טוען...</div></div>
<button id="listbtn" onclick="openD()">☰</button>

<div id="app">
  <div id="hdr">
    <div id="issue-lbl">ידיעון</div>
    <div id="seg-lbl">טוען...</div>
    <div id="pos-lbl"></div>
  </div>
  <div id="pbar"><div id="pfill"></div></div>
  <div id="ta"><div id="body"></div></div>
  <div id="pi"><div class="bars"><span></span><span></span><span></span></div><span>מקריא...</span></div>
  <div id="ctrl">
    <button class="btn nav" onclick="nav(-1)">
      <span class="ic">⟪</span><span class="lb">קודם</span>
    </button>
    <button class="btn play" id="pb" onclick="toggle()">▶</button>
    <button class="btn nav" onclick="nav(1)">
      <span class="ic">⟫</span><span class="lb">הבא</span>
    </button>
  </div>
  <div id="spd">
    <button class="sb" onclick="spd(.8)">×0.8</button>
    <button class="sb on" onclick="spd(1)">×1</button>
    <button class="sb" onclick="spd(1.2)">×1.2</button>
    <button class="sb" onclick="spd(1.5)">×1.5</button>
  </div>
  <button id="vcbtn" onclick="startListen()">🎙 דבר אלי</button>
  <div id="vcmsg"></div>
</div>

<div id="ov" onclick="closeD()"></div>
<div id="drw">
  <div id="drwhd">כל הקטעים</div>
  <div id="segl"></div>
</div>

<script>
let S=null,synth=window.speechSynthesis,utt=null,playing=false,rate=1,heVoice=null;
function initV(){
  const vs=synth.getVoices();
  heVoice=vs.find(v=>v.name==='Carmit')||vs.find(v=>v.lang==='he-IL')||
           vs.find(v=>v.lang.startsWith('he'))||null;
}
if(synth.onvoiceschanged!==undefined)synth.onvoiceschanged=initV;
initV();

async function load(){
  const r=await fetch('/api/current');
  const d=await r.json();
  if(d.no_issue){document.getElementById('lmsg').textContent='\u05d0\u05d9\u05df \u05d9\u05d3\u05d9\u05e2\u05d5\u05df \u05d6\u05de\u05d9\u05df';return;}
  S=d;
  document.getElementById('ls').setAttribute('style','display:none !important');
  render(); renderD();
}
function render(){
  const seg=S.segments[S.current_position];
  document.getElementById('issue-lbl').textContent=S.issue_title;
  document.getElementById('seg-lbl').textContent=seg.title;
  document.getElementById('pos-lbl').textContent='\u05e7\u05d8\u05e2 '+(S.current_position+1)+' \u05de\u05ea\u05d5\u05da '+S.total;
  document.getElementById('body').textContent=seg.body;
  document.getElementById('pfill').style.width=((S.current_position+1)/S.total*100)+'%';
  document.getElementById('ta').scrollTop=0;
  renderD();
}
function renderD(){
  if(!S)return;
  document.getElementById('segl').innerHTML=S.segments.map((s,i)=>'<div class="si '+(i===S.current_position?'cur':'')+'" onclick="jump('+i+')"><div class="n">'+(i+1)+'</div><div class="dot"></div><div class="nm">'+s.title+'</div></div>').join('');
}
function jump(p){stop();S.current_position=p;savePos(p);render();closeD();}
function nav(d){
  stop();
  const n=S.current_position+d;
  if(n<0||n>=S.total)return;
  S.current_position=n;savePos(n);render();
}
function toggle(){playing?pause():speak();}
function speak(){
  if(!S)return;
  synth.cancel();
  const seg=S.segments[S.current_position];
  utt=new SpeechSynthesisUtterance(seg.title+'.\n\n'+seg.body);
  utt.lang='he-IL'; utt.rate=rate;
  if(heVoice)utt.voice=heVoice;
  utt.onstart=()=>{playing=true;document.getElementById('pb').textContent='⏸';
    document.getElementById('pb').classList.add('on');
    document.getElementById('pi').classList.add('on')};
  utt.onend=()=>{
    setIdle();
    if(S.current_position<S.total-1){
      S.current_position++;savePos(S.current_position);render();speak();
    }
  };
  utt.onerror=setIdle;
  synth.speak(utt);
}
function pause(){synth.cancel();setIdle();}
function stop(){synth.cancel();setIdle();}
function setIdle(){
  playing=false;
  document.getElementById('pb').textContent='▶';
  document.getElementById('pb').classList.remove('on');
  document.getElementById('pi').classList.remove('on');
}
function spd(s){
  rate=s;
  document.querySelectorAll('.sb').forEach(b=>b.classList.toggle('on',
    parseFloat(b.textContent.replace('×',''))===s));
  if(playing){stop();speak();}
}
async function savePos(p){
  await fetch('/api/set_position',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({position:p})});
}
function openD(){document.getElementById('drw').classList.add('o');
  document.getElementById('ov').classList.add('o');}
function closeD(){document.getElementById('drw').classList.remove('o');
  document.getElementById('ov').classList.remove('o');}

// ── Voice control ──────────────────────────────────────────────
const SpeechRec=window.SpeechRecognition||window.webkitSpeechRecognition;
let rec=null;
function vcMsg(txt,ok){
  const el=document.getElementById('vcmsg');
  el.textContent=txt;
  el.style.color=ok?'var(--accent)':'var(--muted)';
}
function startListen(){
  if(!SpeechRec){vcMsg('זיהוי קולי לא נתמך בדפדפן זה',false);return;}
  if(rec){rec.abort();rec=null;}
  rec=new SpeechRec();
  rec.lang='he-IL';
  rec.interimResults=false;
  rec.maxAlternatives=5;
  const btn=document.getElementById('vcbtn');
  btn.classList.add('listening');
  btn.textContent='👂 מאזין...';
  vcMsg('דבר עכשיו',false);
  rec.onresult=(e)=>{
    const alts=Array.from(e.results[0]).map(a=>a.transcript.trim());
    console.log('heard:',alts);
    const heard=alts.join(' ');
    handleCmd(heard);
  };
  rec.onerror=(e)=>{
    vcMsg('לא הצלחתי לשמוע — נסה שוב',false);
    resetVcBtn();
  };
  rec.onend=resetVcBtn;
  rec.start();
}
function resetVcBtn(){
  const btn=document.getElementById('vcbtn');
  btn.classList.remove('listening');
  btn.classList.remove('ok');
  btn.textContent='🎙 דבר אלי';
  rec=null;
}
function handleCmd(heard){
  const btn=document.getElementById('vcbtn');
  btn.classList.remove('listening');
  // normalize
  const h=heard.replace(/[.,!?]/g,'');
  let done=false;
  // play
  if(/הפעל|התחל|המשך|קרא/.test(h)){speak();done=true;}
  // pause/stop
  else if(/עצור|השהה|פסק|הפסק/.test(h)){pause();done=true;}
  // next
  else if(/הבא|קטע הבא|קדימה/.test(h)){nav(1);done=true;}
  // prev
  else if(/קודם|קטע קודם|אחורה/.test(h)){nav(-1);done=true;}
  // beginning of issue
  else if(/התחלה|חזור|ראשון|גיליון/.test(h)){
    stop();S.current_position=0;savePos(0);render();done=true;}
  // speed
  else if(/מהיר יותר|מהר יותר/.test(h)){
    const speeds=[0.8,1,1.2,1.5];
    const i=speeds.indexOf(rate);
    if(i<speeds.length-1)spd(speeds[i+1]);done=true;}
  else if(/איטי יותר|לאט יותר/.test(h)){
    const speeds=[0.8,1,1.2,1.5];
    const i=speeds.indexOf(rate);
    if(i>0)spd(speeds[i-1]);done=true;}

  if(done){
    btn.classList.add('ok');
    vcMsg('בוצע: '+heard,true);
    setTimeout(()=>{btn.classList.remove('ok');vcMsg('',false);},2000);
  } else {
    vcMsg('לא הבנתי: "'+heard+'"',false);
    setTimeout(()=>vcMsg('',false),3000);
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
  --green:#2d5f3f;--green-light:#edf5f0;
  --text:#1a1a18;--muted:#888;--r:16px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Heebo',sans-serif;min-height:100vh}
.wrap{max-width:700px;margin:0 auto;padding:40px 24px}
h1{font-size:32px;font-weight:900;margin-bottom:3px}
.sub{color:var(--muted);font-size:14px;margin-bottom:36px}
.card{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:28px;margin-bottom:20px}
.card h2{font-size:17px;font-weight:700;margin-bottom:16px}

/* listener link */
.llink{display:flex;align-items:center;gap:10px;background:var(--green-light);
  border:1px solid #c5deca;border-radius:10px;padding:14px 16px;
  text-decoration:none;color:var(--green);font-weight:700;font-size:15px;
  transition:opacity .2s}
.llink:hover{opacity:.85}
.llink .ic{font-size:22px}

/* drop zone */
#dz{border:2px dashed var(--border);border-radius:12px;padding:48px 20px;
  text-align:center;cursor:pointer;transition:all .2s;background:var(--bg)}
#dz:hover,#dz.over{border-color:var(--green);background:var(--green-light)}
#dz .ic{font-size:38px;margin-bottom:10px}
#dz .ht{font-size:16px;font-weight:700;margin-bottom:4px}
#dz .sb{font-size:13px;color:var(--muted)}
#fi{display:none}
#fn{margin-top:10px;font-size:13px;color:var(--muted);display:none}
.ubtn{width:100%;margin-top:14px;padding:16px;background:var(--green);color:#fff;
  border:none;border-radius:12px;font-size:17px;font-weight:700;
  font-family:'Heebo',sans-serif;cursor:pointer;transition:opacity .2s;display:none}
.ubtn:hover{opacity:.9}
.ubtn.vis{display:block}

/* progress */
#prog{height:4px;background:var(--border);border-radius:99px;overflow:hidden;
  margin-top:12px;display:none}
#pfill{height:100%;background:var(--green);border-radius:99px;
  width:0;transition:width .4s}

/* status */
#st{margin-top:14px;padding:13px 15px;border-radius:10px;font-size:14px;
  font-weight:700;display:none}
#st.ok{background:var(--green-light);color:var(--green)}
#st.err{background:#fef0f0;color:#c0392b}
#st.wait{background:#f0f0f0;color:var(--muted)}

/* segment preview */
#preview{margin-top:14px;display:none}
#preview h3{font-size:13px;color:var(--muted);margin-bottom:8px}
#preview-list{display:flex;flex-wrap:wrap;gap:6px}
.ptag{background:var(--green-light);color:var(--green);border-radius:99px;
  padding:4px 12px;font-size:13px;font-weight:700}

/* issues */
.row{display:flex;align-items:center;padding:13px 0;
  border-bottom:1px solid var(--border);gap:11px}
.row:last-child{border-bottom:none}
.ri{flex:1}
.rt{font-weight:700;font-size:15px}
.rm{font-size:12px;color:var(--muted);margin-top:2px}
.abtn{padding:8px 14px;background:var(--green);color:#fff;border:none;
  border-radius:8px;font-family:'Heebo',sans-serif;font-weight:700;
  font-size:13px;cursor:pointer;opacity:.5;transition:opacity .2s}
.abtn.cur{opacity:1;background:#888;cursor:default}
.abtn:not(.cur):hover{opacity:1}
</style>
</head>
<body>
<div class="wrap">
  <h1>📰 ניהול ידיעון</h1>
  <p class="sub">בארות יצחק — הצד שלך</p>

  <div class="card">
    <a href="/" class="llink" target="_blank">
      <span class="ic">🎧</span> פתח נגן האזנה (צד אבא)
    </a>
  </div>

  <div class="card">
    <h2>העלאת ידיעון חדש</h2>
    <div id="dz" onclick="document.getElementById('fi').click()"
         ondragover="event.preventDefault();this.classList.add('over')"
         ondragleave="this.classList.remove('over')"
         ondrop="drop(event)">
      <div class="ic">📄</div>
      <div class="ht">גרור לכאן קובץ PDF של הידיעון</div>
      <div class="sb">או לחץ לבחירה</div>
    </div>
    <input type="file" id="fi" accept=".pdf" onchange="pick(this.files[0])">
    <div id="fn"></div>
    <button class="ubtn" id="ub" onclick="upload()">⬆ העלה ועבד</button>
    <div id="prog"><div id="pfill"></div></div>
    <div id="st"></div>
    <div id="preview">
      <h3>קטעים שזוהו:</h3>
      <div id="preview-list"></div>
    </div>
  </div>

  <div class="card">
    <h2>ידיעונים שמורים</h2>
    <div id="ilist"><div style="color:var(--muted);font-size:14px">טוען...</div></div>
  </div>
</div>

<script>
let file=null;
function drop(e){
  e.preventDefault();
  document.getElementById('dz').classList.remove('over');
  const f=e.dataTransfer.files[0];
  if(f&&f.name.endsWith('.pdf'))pick(f);
}
function pick(f){
  if(!f)return; file=f;
  const fn=document.getElementById('fn');
  fn.style.display='block'; fn.textContent='📎 '+f.name;
  document.getElementById('ub').classList.add('vis');
  document.getElementById('preview').style.display='none';
  document.getElementById('st').style.display='none';
}
async function upload(){
  if(!file)return;
  const ub=document.getElementById('ub'),st=document.getElementById('st');
  const prog=document.getElementById('prog'),fill=document.getElementById('pfill');
  ub.disabled=true; ub.textContent='⏳ מעבד...';
  st.className='wait'; st.style.display='block'; st.textContent='מחלץ טקסט ומחלק לקטעים...';
  prog.style.display='block'; fill.style.width='30%';
  const fd=new FormData(); fd.append('pdf',file);
  try{
    fill.style.width='65%';
    const r=await fetch('/api/upload',{method:'POST',body:fd});
    const d=await r.json();
    fill.style.width='100%';
    if(d.ok){
      st.className='ok';
      st.textContent='\u2705 "'+d.title+'" \u2014 '+d.segments+' \u05e7\u05d8\u05e2\u05d9\u05dd';
      if(d.preview){
        document.getElementById('preview').style.display='block';
        document.getElementById('preview-list').innerHTML=
          d.preview.map((t,i)=>'<span class="ptag">'+(i+1)+'. '+t+'</span>').join('');
      }
      loadIssues();
    }else{
      st.className='err'; st.textContent='❌ '+d.error;
    }
  }catch(e){st.className='err';st.textContent='❌ שגיאת חיבור';}
  ub.disabled=false; ub.textContent='⬆ העלה ועבד';
}
async function loadIssues(){
  const[ir,cr]=await Promise.all([fetch('/api/issues'),fetch('/api/current')]);
  const issues=await ir.json(), cur=await cr.json();
  const curId=cur.issue_id||null;
  const list=document.getElementById('ilist');
  if(!issues.length){list.innerHTML='<div style="color:var(--muted);font-size:14px">אין ידיעונים עדיין</div>';return;}
  list.innerHTML=issues.map(i=>{
    const d=new Date(i.created_at).toLocaleDateString('he-IL');
    const isCur=i.id===curId;
    return '<div class="row"><div class="ri"><div class="rt">'+i.title+'</div><div class="rm">'+d+' \u00b7 '+i.seg_count+' \u05e7\u05d8\u05e2\u05d9\u05dd'+(isCur?' \u00b7 <strong>\u05e4\u05e2\u05d9\u05dc</strong>':'')+
      '</div></div><button class="abtn '+(isCur?'cur':'')+'" onclick="'+(isCur?'':('activate('+i.id+')'))+'">'+(isCur?'\u2713 \u05e4\u05e2\u05d9\u05dc':'\u05d4\u05e4\u05e2\u05dc')+'</button></div>';
  }).join('');
}
async function activate(id){
  await fetch('/api/set_issue',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({issue_id:id})});
  loadIssues();
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

