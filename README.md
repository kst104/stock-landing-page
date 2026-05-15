# 주식 랜딩페이지 / 스크리너 허브

Flask 기반 주식 스크리너 허브입니다.

## 로컬 실행

```powershell
python -m pip install -r requirements.txt
$env:KIS_KEY="your-kis-key"
$env:KIS_SECRET="your-kis-secret"
$env:AUTH_ADMIN_PASSWORD="your-admin-password"
python app.py
```

접속 주소는 `http://localhost:8888`입니다.

## 환경 변수

`.env.example`을 참고해 배포 환경에 아래 값을 설정하세요.

- `KIS_KEY`
- `KIS_SECRET`
- `AUTH_ADMIN_EMAIL`
- `AUTH_ADMIN_PASSWORD`

