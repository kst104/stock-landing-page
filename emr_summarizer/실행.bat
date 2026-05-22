@echo off
echo ================================================
echo  NGTMediPlus EMR 자동 요약
echo ================================================
echo.

cd /d "%~dp0"

python -c "import anthropic, PIL, dotenv" 2>nul
if errorlevel 1 (
    echo [설치] 필요한 패키지를 설치합니다...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [오류] 패키지 설치 실패.
        pause
        exit /b 1
    )
    echo [완료] 설치 완료
    echo.
)

echo [실행] 프로그램 시작...
python main.py

if errorlevel 1 (
    echo.
    echo [오류] 실행 중 문제가 발생했습니다.
    pause
)
