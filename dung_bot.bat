@echo off
chcp 65001 >nul
echo Dang dung Manh Moi AI Marketing Bot...
wmic process where "name='python.exe' and commandline like '%%telegram_bot%%'" call terminate >nul 2>&1
echo Da dung bot thanh cong!
pause
