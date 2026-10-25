@echo off
REM TOP VIEW setup: iguguhit mo ang box kung saan nagpa-park ang toolhead.
REM Kung patagilid ang larawan sa browser, palitan ang --rotate 0 ng 90, 180 o 270.
cd /d "%~dp0"
python cc1lapse.py setup --camera http://192.168.100.134:8080 --name top --rotate 0
pause
