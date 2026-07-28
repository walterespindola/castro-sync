@echo off
:: Crea una tarea programada que corre sync_cobros cada hora
:: Ejecutar como Administrador

set SCRIPT_DIR=%~dp0
set SCRIPT=%SCRIPT_DIR%sync_cobros.py
set LOG=%SCRIPT_DIR%sync_cobros.log

:: Nombre de la tarea
set TASK_NAME=CastroSyncCobros

:: Eliminar tarea anterior si existe
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

:: Crear tarea: una vez por dia a las 21:00
schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "\"C:\Windows\py.exe\" -3.12-32 \"%SCRIPT%\"" ^
  /sc daily ^
  /st 21:00 ^
  /ru SYSTEM ^
  /f

if %errorlevel% equ 0 (
    echo [OK] Tarea '%TASK_NAME%' creada correctamente.
    echo      Se ejecutara todos los dias a las 21:00.
) else (
    echo [ERROR] No se pudo crear la tarea. Ejecutar como Administrador.
)
pause
