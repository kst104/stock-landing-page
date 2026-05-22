@echo off
echo ================================================
echo  NGTMediPlus EMR 자동 요약 - 실행 스크립트
echo ================================================
echo.

REM 현재 스크립트 위치(emr_summarizer 폴더)로 이동
cd /d "%~dp0"

REM 의존성 설치 확인
python -c "import anthropic, pyodbc, dotenv" 2>nul
if errorlevel 1 (
    echo [설치] 필요한 패키지를 설치합니다...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [오류] 패키지 설치 실패. pip 설치 여부를 확인하세요.
        pause
        exit /b 1
    )
    echo [완료] 패키지 설치 완료
    echo.
)

echo [실행] 프로그램을 시작합니다...
python main.py

if errorlevel 1 (
    echo.
    echo [오류] 실행 중 문제가 발생했습니다.
    pause
)
