@echo off
:: Crea tarea programada que corre pow_etl.py (all, contra produccion) todos
:: los dias a las 23:00, despues de sync_cobros (21:00) y sync_envio (22:00).
:: Ejecutar como Administrador

set SCRIPT_DIR=%~dp0
set SCRIPT=%SCRIPT_DIR%pow_etl.py
set CONFIG=%SCRIPT_DIR%pow_etl.prod.ini
set TASK_NAME=CastroPowETL

if not exist "%CONFIG%" (
    echo [ERROR] No se encontro %CONFIG%
    echo         Copiar pow_etl.prod.ini.example a pow_etl.prod.ini y completarlo antes de programar la tarea.
    pause
    exit /b 1
)

:: ── Detectar Python 32-bit ────────────────────────────────────────────────────
:: NO usar "py -3.12-32" porque SYSTEM puede no tenerlo en PATH.
:: Siempre usar ruta completa al ejecutable.
set PYTHON_EXE=

:: 1) Instalacion para todos los usuarios (preferida para tareas SYSTEM)
for %%P in (
    "C:\Program Files (x86)\Python312-32\python.exe"
    "C:\Program Files (x86)\Python312\python.exe"
    "C:\Program Files\Python312-32\python.exe"
    "C:\Program Files\Python312\python.exe"
    "C:\Python312-32\python.exe"
    "C:\Python312\python.exe"
) do (
    if exist %%P (
        set PYTHON_EXE=%%~P
        goto :found
    )
)

:: 2) Instalacion per-usuario (AppData) — ruta del usuario actual
if exist "%LOCALAPPDATA%\Programs\Python\Python312-32\python.exe" (
    set PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312-32\python.exe
    goto :found
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
    goto :found
)

echo [ERROR] No se encontro Python. Instalar Python 3.12 de 32 bits.
echo         O editar este .bat y setear PYTHON_EXE manualmente.
pause
exit /b 1

:found
echo [INFO] Usando Python: %PYTHON_EXE%

:: Nombres cortos 8.3 (sin espacios) para PYTHON_EXE/SCRIPT/CONFIG -- evita
:: pelear con el anidado de comillas de schtasks/cmd cuando las rutas tienen
:: espacios (ej. "Program Files", "InterSoft SISTEMAS"). Si 8dot3 esta
:: deshabilitado en el volumen, %%~s devuelve la ruta larga sin cambios y
:: hay que resolverlo a mano (fsutil 8dot3name query).
for %%S in ("%PYTHON_EXE%") do set PY_SHORT=%%~sS
for %%S in ("%SCRIPT%") do set SCRIPT_SHORT=%%~sS
for %%S in ("%CONFIG%") do set CONFIG_SHORT=%%~sS
echo [INFO] Ruta corta Python: %PY_SHORT%
echo [INFO] Ruta corta script: %SCRIPT_SHORT%
echo [INFO] Ruta corta config: %CONFIG_SHORT%

:: ── Determinar usuario para la tarea ─────────────────────────────────────────
:: Si Python esta en AppData, SYSTEM no puede acceder — usar el usuario actual
set TASK_USER=SYSTEM
echo %PYTHON_EXE% | findstr /i "AppData" >nul 2>&1
if %errorlevel% equ 0 (
    set TASK_USER=%USERDOMAIN%\%USERNAME%
    echo [INFO] Python instalado per-usuario. La tarea correra como: %TASK_USER%
    echo [INFO] Se pedira la contrasena del usuario.
)

:: ── Crear tarea ───────────────────────────────────────────────────────────────
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

set TR_VALUE=%PY_SHORT% %SCRIPT_SHORT% all -c %CONFIG_SHORT%

if "%TASK_USER%"=="SYSTEM" (
    schtasks /create /tn "%TASK_NAME%" /tr "%TR_VALUE%" /sc daily /st 23:00 /ru SYSTEM /f
) else (
    schtasks /create /tn "%TASK_NAME%" /tr "%TR_VALUE%" /sc daily /st 23:00 /ru "%TASK_USER%" /rp * /f
)

if %errorlevel% equ 0 (
    echo [OK] Tarea '%TASK_NAME%' creada. Se ejecutara todos los dias a las 23:00.
) else (
    echo [ERROR] No se pudo crear la tarea. Ejecutar como Administrador.
)
pause
