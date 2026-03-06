# ידיעון בארות יצחק — נגן קול 🎧

אפליקציית web להמרת ידיעון PDF לקריאה קולית.  
**צדך:** העלאת PDF שבועי + ניהול.  
**צד אבא:** נגן נוח באייפון — כפתורים גדולים, קול עברי מובנה.

---

## פריסה ב-Railway (חינמי, 10 דקות)

### שלב 1 — GitHub
1. צור חשבון ב-[github.com](https://github.com) אם אין לך
2. צור repo חדש בשם `yedion`
3. העלה את שלושת הקבצים: `app.py`, `requirements.txt`, `Procfile`

### שלב 2 — Railway
1. כנס ל-[railway.app](https://railway.app) → **Login with GitHub**
2. לחץ **New Project** → **Deploy from GitHub repo** → בחר `yedion`
3. Railway מזהה Python אוטומטית ומתחיל לבנות

### שלב 3 — PostgreSQL
1. בתוך הפרויקט ב-Railway: **+ New** → **Database** → **PostgreSQL**
2. לחץ על ה-PostgreSQL → לשונית **Variables**
3. העתק את הערך של `DATABASE_URL`
4. עבור ל-service של `yedion` → **Variables** → הוסף:
   - `DATABASE_URL` = הערך שהעתקת
5. Railway מאתחל אוטומטית

### שלב 4 — קבל כתובת
בתוך service ה-yedion → לשונית **Settings** → **Domains** → **Generate Domain**  
תקבל כתובת בסגנון: `yedion-production.up.railway.app`

---

## שימוש שבועי

| מי | כתובת | מה עושים |
|----|--------|----------|
| אתה | `/admin` | גוררים PDF → "העלה ועבד" |
| אבא | `/` | לוחצים ▶ ומאזינים |

**הקישור לאבא:** שלח לו את `https://YOUR-APP.railway.app` — הוסף ל-Home Screen:  
Safari → כפתור שיתוף → "הוסף למסך הבית" → יופיע כאפליקציה

---

## תכונות הנגן
- ▶ / ⏸ הפעל / השהה
- ⟪ ⟫ קטע קודם / הבא (עוצר ועובר)
- ☰ קפיצה ישירה לכל קטע
- ×0.8 / ×1 / ×1.2 / ×1.5 שליטה על מהירות
- זוכר באיזה קטע עצר
- ממשיך אוטומטית לקטע הבא בסיום

---

## הגדרה מקומית (לבדיקה)

```bash
# דרוש: Python 3.10+ ו-PostgreSQL מקומי
pip install flask pdfplumber psycopg2-binary gunicorn
export DATABASE_URL="postgresql://localhost/yedion"
python -c "import app; app.init_db()"
python app.py
# פתח: http://localhost:5000/admin
```
