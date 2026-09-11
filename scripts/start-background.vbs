' Chrome History Exporter をコンソールなしで常駐起動する
' スタートアップ フォルダ (shell:startup) にこのファイルのショートカットを置くと
' ログオン時に自動起動できる。
Option Explicit

Dim shell, fso, root, pythonw, script
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
script = fso.BuildPath(root, "run.py")

' PATH 上の pythonw.exe を使う（py ランチャしか無い場合は pyw にフォールバック）
pythonw = "pythonw.exe"
If shell.Run("cmd /c where pythonw.exe", 0, True) <> 0 Then
    pythonw = "pyw.exe"
End If

shell.Run """" & pythonw & """ """ & script & """ run", 0, False
