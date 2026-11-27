Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")
BaseDir = FSO.GetParentFolderName(WScript.ScriptFullName)
Pyw = BaseDir & "\.venv\Scripts\pythonw.exe"
App = BaseDir & "\app.py"
WshShell.CurrentDirectory = BaseDir
WshShell.Run """" & Pyw & """ """ & App & """", 0, False
