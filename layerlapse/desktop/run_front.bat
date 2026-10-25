@echo off
REM FRONT VIEW - toys, figures, functional prints. I-run BAGO simulan ang print.
cd /d "%~dp0"
python cc1lapse.py --ip 192.168.100.114 --camera http://192.168.100.134:8080 --box front --fps 30
pause
