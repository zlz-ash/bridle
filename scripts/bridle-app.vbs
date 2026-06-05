' Silent launcher for Bridle: runs bridle-app.ps1 with no visible windows.
' Double-click this file. Only the Chrome --app window will appear.
Set sh = CreateObject("WScript.Shell")
ps1 = "D:\Bridle\scripts\bridle-app.ps1"
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """"
' 0 = hidden window, False = do not wait
sh.Run cmd, 0, False
