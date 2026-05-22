@echo off
chcp 65001 > nul

:: ── 관리자 권한 확인 ──────────────────────────────────────────────────────────
fsutil dirty query %systemdrive% > nul 2>&1
if %errorLevel% neq 0 (
    echo.
    echo  관리자 권한이 필요합니다.
    echo  잠시 후 UAC 창이 뜨면 [예]를 클릭하세요.
    echo.
    powershell -Command "Start-Process 'python' -ArgumentList 'main.py' -Verb RunAs -WorkingDirectory '%~dp0' -Wait"
    exit /b
)

:: ── 관리자 권한으로 실행 중 ───────────────────────────────────────────────────
echo ================================================
echo  NGTMediPlus EMR 자동 요약  [관리자 권한 확인됨]
echo ================================================
echo.

cd /d "%~dp0"

python -c "import anthropic, PIL, dotenv" 2>nul
if errorlevel 1 (
    echo [설치] 필요한 패키지를 설치합니다...
    pip install -r requirements.txt
)

python main.py
pause
