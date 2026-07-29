@echo off
set DIR=%~dp0
"C:\Program Files (x86)\Python312-32\python.exe" "%DIR%sync_cobros.py" >> "%DIR%sync_cobros.log" 2>&1
