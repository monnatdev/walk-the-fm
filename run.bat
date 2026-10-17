@echo off
cd /d "%~dp0"
set "CLIP=%~1"
if not "%CLIP%"=="" goto have_clip

rem No clip given (double-click, or ".\run.bat" in a terminal): use the newest recording
rem of the game. Game Bar names clips after the recorded window, so other windows
rem (e.g. VS Code recorded by mistake) are skipped.
for /f "delims=" %%F in ('dir /b /o-d /a-d "%USERPROFILE%\Videos\Captures\MapleStory*.mp4" 2^>nul') do (
  set "CLIP=%USERPROFILE%\Videos\Captures\%%F"
  goto ask
)
echo No game recordings (MapleStory*.mp4) found in "%USERPROFILE%\Videos\Captures".
echo Click the game window first, then press Win+Alt+R to record.
echo Or drag and drop a video file onto run.bat in File Explorer.
pause
exit /b

:ask
echo Newest recording:
echo   %CLIP%
choice /c YN /m "Process this clip"
if errorlevel 2 exit /b

:have_clip
for %%I in ("%CLIP%") do (
  set "CLIPNAME=%%~nxI"
  set "RESULT=%~dp0results\%%~nI"
)
rem results go into this project (results\clip name); the clip itself stays where it is
echo [1/3] Reading "%CLIPNAME%" ...
python fm_ocr.py "%CLIP%" --out "%RESULT%"
if errorlevel 1 goto failed
echo.
echo [2/3] Saving to fm.db ...
rem capture date = YYYY-MM-DD in the file name (Game Bar names have it), else today
python ingest.py "%RESULT%\items.csv" --video "%CLIPNAME%"
if errorlevel 1 goto failed
echo.
echo [3/3] Building report ...
python report.py
if errorlevel 1 goto failed
rem site\index.html: the same page, ready to drag onto app.netlify.com/drop to update the website
if not exist site mkdir site
copy /y report.html site\index.html >nul
echo.
echo Done. Results: "%RESULT%\items.csv"
echo Website copy: "%~dp0site" (drag this folder onto app.netlify.com/drop to update the site)
rem the report is not opened automatically (it would pop up over a recording in progress);
rem report.py printed its link above: Ctrl+click it in the VS Code terminal to open it
pause
exit /b

:failed
echo.
echo Something went wrong (see messages above).
pause
exit /b 1
