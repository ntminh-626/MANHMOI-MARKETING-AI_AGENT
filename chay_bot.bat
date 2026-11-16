@echo off
chcp 65001 >nul
title MANH MOI AI MARKETING BOT (Telegram + Threads)
cd /d "%~dp0"

:loop
echo [%date% %time%] Dang khoi dong Manh Moi AI Marketing Bot...
"C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe" -u telegram_bot\bot.py
echo [%date% %time%] Bot bi dung dot ngot. Tu dong khoi dong lai sau 5 giay...
timeout /t 5 /nobreak >nul
goto loop
