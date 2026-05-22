@echo off
chcp 65001 > nul

:: ── 관리자 권한 확인 ──────────────────────────────────────────────────────────
fsutil dirty query %systemdrive% > nul 2>&1
if %errorLevel% neq 0 (
    echo 관리자 권한으로 재시작합니다. UAC 창에서 [예]를 클릭하세요...
    powershell -Command "Start-Process 'cmd.exe' -ArgumentList '/k cd /d \"%~dp0\" && python main.py && pause' -Verb RunAs -WorkingDirectory \"%~dp0\""
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
    if errorlevel 1 (
        echo [오류] 패키지 설치 실패.
        pause
        exit /b 1
    )
)

echo [실행] 프로그램 시작...
python main.py
pause
