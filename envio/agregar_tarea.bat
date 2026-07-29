@echo off
:: Crea tarea programada que corre sync_envio todos los dias a las 22:00
:: Ejecutar como Administrador

set SCRIPT_DIR=%~dp0
set SCRIPT=%SCRIPT_DIR%sync_envio.py
set TASK_NAME=CastroSyncEnvio

:: ── Detectar Python 32-bit ────────────────────────────────────────────────────
set PYTHON_EXE=

:: 1) py launcher con flag -3.12-32
where py >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_EXE=py -3.12-32
    goto :found
)

:: 2) Rutas tipicas de instalacion 32-bit
for %%P in (
    "C:\Python312-32\python.exe"
    "C:\Python312\python.exe"
    "C:\Program Files (x86)\Python312\python.exe"
    "C:\Windows\py.exe"
) do (
    if exist %%P (
        set PYTHON_EXE=%%P
        goto :found
    )
)

echo [ERROR] No se encontro Python. Instalar Python 3.12 de 32 bits.
echo         O editar este .bat y setear PYTHON_EXE manualmente.
pause
exit /b 1

:found
echo [INFO] Usando Python: %PYTHON_EXE%

:: ── Crear tarea ───────────────────────────────────────────────────────────────
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "\"%PYTHON_EXE%\" \"%SCRIPT%\"" ^
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
