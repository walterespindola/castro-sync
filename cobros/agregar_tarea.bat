@echo off
:: Crea tarea programada que corre sync_cobros todos los dias a las 21:00
:: Ejecutar como Administrador

set SCRIPT_DIR=%~dp0
set SCRIPT=%SCRIPT_DIR%sync_cobros.py
set TASK_NAME=CastroSyncCobros

:: ── Detectar Python 32-bit ────────────────────────────────────────────────────
set PYTHON_EXE=

:: 1) py launcher
where py >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_EXE=py -3.12-32
    goto :found
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

:: 3) Instalacion para todos los usuarios
for %%P in (
    "C:\Python312-32\python.exe"
    "C:\Python312\python.exe"
    "C:\Program Files (x86)\Python312-32\python.exe"
    "C:\Program Files (x86)\Python312\python.exe"
    "C:\Program Files\Python312-32\python.exe"
    "C:\Program Files\Python312\python.exe"
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

if "%TASK_USER%"=="SYSTEM" (
    schtasks /create ^
      /tn "%TASK_NAME%" ^
      /tr "\"%PYTHON_EXE%\" \"%SCRIPT%\"" ^
      /sc daily /st 21:00 ^
      /ru SYSTEM /f
) else (
    schtasks /create ^
      /tn "%TASK_NAME%" ^
      /tr "\"%PYTHON_EXE%\" \"%SCRIPT%\"" ^
      /sc daily /st 21:00 ^
      /ru "%TASK_USER%" /rp * /f
)

if %errorlevel% equ 0 (
    echo [OK] Tarea '%TASK_NAME%' creada. Se ejecutara todos los dias a las 21:00.
) else (
    echo [ERROR] No se pudo crear la tarea. Ejecutar como Administrador.
)
pause
