@echo off
setlocal
echo =============================================================
echo  Instalacion de sync_cobros - Castro Gestion
echo =============================================================
echo.

:: Verificar Python 3.12 32-bit
py -3.12-32 --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Python 3.12 de 32 bits NO esta instalado.
    echo     Descargarlo de: https://www.python.org/downloads/release/python-3122/
    echo     Archivo a instalar: "Windows installer (32-bit)"
    echo.
    echo     Luego volver a ejecutar este install.bat
    pause
    exit /b 1
)

echo [OK] Python 3.12 32-bit encontrado.
echo.

:: Instalar mysql-connector-python
echo Instalando mysql-connector-python...
py -3.12-32 -m pip install --upgrade mysql-connector-python
if %errorlevel% neq 0 (
    echo [ERROR] No se pudo instalar mysql-connector-python.
    pause
    exit /b 1
)

echo.
echo [OK] Dependencias instaladas.
echo.
echo Verifique que sync_cobros.ini tenga los valores correctos
echo para esta instalacion (rutas, servidor MySQL, base de datos IB).
echo.
echo Para programar la ejecucion automatica ejecutar: agregar_tarea.bat
echo.
pause
