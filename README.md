# 📦 Link Filter Telegram Bot

## Files
- `main.py` — Bot source code
- `requirements.txt` — Python dependencies
- `Procfile` — Railway deployment config

---

## 🚀 Railway Deployment

### Step 1 — GitHub par push karo
```bash
git init
git add .
git commit -m "init bot"
git remote add origin https://github.com/YOUR/REPO.git
git push -u origin main
```

### Step 2 — Railway mein import karo
1. railway.app → New Project → Deploy from GitHub repo
2. Repo select karo

### Step 3 — Environment Variables set karo
Railway dashboard → Variables tab mein yeh add karo:

| Variable       | Example Value                                          |
|----------------|--------------------------------------------------------|
| `BOT_TOKEN`    | `123456789:ABCdef...`                                  |
| `ADMIN_ID`     | `987654321`                                            |
| `DATABASE_URL` | `postgresql://user:pass@ep-xxx.neon.tech/neondb?sslmode=require` |
| `CHANNEL_IDS`  | `-100123456789,-100987654321`                          |

> **Note:** `CHANNEL_IDS` = comma-separated channel IDs (bot must be admin in each channel)

### Step 4 — Deploy
Railway auto-deploy ho jaata hai push par. Worker service start hogi.

---

## 🗃️ Neon DB Setup
1. console.neon.tech → New Project
2. Connection string copy karo (`postgresql://...`)
3. `DATABASE_URL` mein paste karo
4. Tables automatically create hongi first run par

---

## 📖 Bot Usage

| Action | Description |
|--------|-------------|
| Post bhejo | Text/Photo/Video/Doc bot ko bhejo — preview milega |
| `/send` | Saare pending posts channels mein distribute |
| `/cancel` | Current batch clear karo |
| `/footer Join @ch` | Footer set karo |
| `/footer` | Current footer dekhein |
| `/start` | Bot status |

### Link Filter Logic
- `https://terabox.com/xyz` → **RAKHEGA** (tera word hai)
- `https://drive.google.com/xyz` → **HATAYEGA**
- `https://t.me/someChannel` → **HATAYEGA**
- `https://myteralink.com` → **RAKHEGA** (tera word hai)

### Split Distribution
Posts round-robin style distribute hote hain channels mein:
- Post 1 → Channel 1
- Post 2 → Channel 2
- Post 3 → Channel 1 (agar 2 channels hain)
