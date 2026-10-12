' Voice keyboard (Gemini): no console window, lives in the system tray; right-click the tray icon to quit
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\python_code\voice-dictate"
sh.Run """C:\python_code\voice-dictate\.venv\Scripts\pythonw.exe"" -X utf8 ""C:\python_code\voice-dictate\dictate.py""", 0, False
