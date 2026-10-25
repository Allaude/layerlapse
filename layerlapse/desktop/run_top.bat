@echo off
REM TOP VIEW - HueForge, ID case, clicker. I-run BAGO simulan ang print.
cd /d "%~dp0"
python cc1lapse.py --ip 192.168.100.114 --camera http://192.168.100.134:8080 --box top --fps 12 --hold 3
pause
