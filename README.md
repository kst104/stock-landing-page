# Stock Landing Page / Screener Hub

Flask-based stock screener hub.

## Local Run

```powershell
python -m pip install -r requirements.txt
$env:KIS_KEY="your-kis-key"
$env:KIS_SECRET="your-kis-secret"
$env:AUTH_ADMIN_PASSWORD="your-admin-password"
python app.py
```

Open `http://localhost:8888`.

## Environment Variables

Set these values in your deployment environment. See `.env.example`.

- `KIS_KEY`
- `KIS_SECRET`
- `AUTH_ADMIN_EMAIL`
- `AUTH_ADMIN_PASSWORD`

