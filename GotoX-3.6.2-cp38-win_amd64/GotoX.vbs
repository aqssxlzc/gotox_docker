'���� GotoX ϵͳ���̸������ߡ�
Dim objShell
Set objShell = WScript.CreateObject("WScript.Shell")
objShell.Environment("Process").Remove("PYTHONPATH")
objShell.Environment("Process").Remove("PYTHONHOME")
objShell.CurrentDirectory = objShell.CurrentDirectory + "\python"
objShell.Run "python.exe ..\launcher\start.py",,False
Set objShell = NoThing
WScript.quit
