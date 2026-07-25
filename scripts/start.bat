@echo off
echo 🚀 pywhispr-web Docker Setup
echo ==========================

REM Check if Docker is running
docker info >nul 2>&1
if errorlevel 1 (
    echo ❌ Docker is not running. Please start Docker and try again.
    pause
    exit /b 1
)

REM Build and run with Docker Compose
echo 🔨 Building and starting pywhispr-web...
docker compose up --build -d

if %errorlevel% equ 0 (
    echo.
    echo 🎉 pywhispr-web is now running!
    echo 📱 Access the application at: http://localhost:5000
    echo.
    echo 🛑 To stop the application, run: docker compose down
    echo 📊 To view logs, run: docker compose logs -f pywhispr-web
) else (
    echo ❌ Failed to start pywhispr-web. Check the logs for errors.
    docker compose logs pywhispr-web
)

pause
