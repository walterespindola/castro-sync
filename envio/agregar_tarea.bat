@echo off
:: Crea tarea programada que corre sync_envio todos los dias a las 22:00
:: Ejecutar como Administrador

set SCRIPT_DIR=%~dp0
set SCRIPT=%SCRIPT_DIR%sync_envio.py
set TASK_NAME=CastroSyncEnvio

schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "\"C:\Windows\py.exe\" -3.12-32 \"%SCRIPT%\"" ^
  /sc daily ^
  /st 22:00 ^
  /ru SYSTEM ^
  /f

if %errorlevel% equ 0 (
    echo [OK] Tarea '%TASK_NAME%' creada. Se ejecutara todos los dias a las 22:00.
) else (
    echo [ERROR] No se pudo crear la tarea. Ejecutar como Administrador.
)
pause
