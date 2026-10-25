@echo off
REM FRONT VIEW setup: iguguhit mo ang box kung saan nagpa-park ang toolhead.
REM Bubukas ang browser. Pagkatapos i-save, isara ang browser tab at ang window na ito.
cd /d "%~dp0"
python cc1lapse.py setup --camera http://192.168.100.134:8080 --name front --rotate 90
pause
