Set WshShell = CreateObject("WScript.Shell")
strPath = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = strPath
WshShell.Run """C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe"" -u """ & strPath & "\telegram_bot\bot.py""", 0, False
