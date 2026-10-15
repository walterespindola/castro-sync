@echo off
set DIR=%~dp0
"C:\Program Files (x86)\Python312-32\python.exe" "%DIR%pow_etl.py" all -c "%DIR%pow_etl.prod.ini"
