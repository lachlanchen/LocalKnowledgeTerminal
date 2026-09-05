@echo off
cd /d "%~dp0"
if not exist node_modules call npm ci
if errorlevel 1 exit /b 1
if not exist dist\index.html call npm run build
if errorlevel 1 exit /b 1
node server.mjs
