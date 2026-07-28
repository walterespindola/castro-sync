@echo off
:: Instala dependencias Python para sync_envio
:: El paquete fdb va en la subcarpeta fdb\ — NO instalar via pip

py -3.12-32 -m pip install mysql-connector-python
echo.
echo [OK] Dependencias instaladas.
echo NOTA: el paquete fdb\ ya viene incluido en la carpeta, no instalarlo via pip.
pause
