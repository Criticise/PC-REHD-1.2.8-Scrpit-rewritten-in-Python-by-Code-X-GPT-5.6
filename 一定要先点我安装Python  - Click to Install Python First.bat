@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1

for %%I in ("%~dp0.") do set "SCRIPT_DIR=%%~fI"
set "PS1=%SCRIPT_DIR%\先点Bat文件 - Click Bat First.ps1"
set "PS_HOST="
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"

if not exist "%PS1%" (
    echo [FAIL] PowerShell system detector is missing: %PS1%
    exit /b 10
)

rem Probe every candidate. A broken PowerShell 7 installation must not block
rem the Windows PowerShell 5.1 fallback that is included with Windows.
if defined PC_REHD_CODE_X_PWSH call :TryPowerShellHost "%PC_REHD_CODE_X_PWSH%"
if defined PC_REHD_CODE_X_POWERSHELL call :TryPowerShellHost "%PC_REHD_CODE_X_POWERSHELL%"
for /f "delims=" %%J in ('where pwsh.exe 2^>nul') do call :TryPowerShellHost "%%~fJ"

if not defined PS_HOST for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\pwsh.exe" /ve 2^>nul') do call :TryRegistryValue "%%A" "%%B"
if not defined PS_HOST for /f "tokens=2,*" %%A in ('reg query "HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\pwsh.exe" /ve 2^>nul') do call :TryRegistryValue "%%A" "%%B"
if not defined PS_HOST for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\Microsoft\PowerShellCore\InstalledVersions" /s /v InstallLocation 2^>nul') do call :TryRegistryRoot "%%A" "%%B"
if not defined PS_HOST for /f "tokens=2,*" %%A in ('reg query "HKCU\SOFTWARE\Microsoft\PowerShellCore\InstalledVersions" /s /v InstallLocation 2^>nul') do call :TryRegistryRoot "%%A" "%%B"

call :TryPowerShellHost "%SCRIPT_DIR%\PowerShell\7\pwsh.exe"
call :TryPowerShellHost "%SCRIPT_DIR%\PowerShell\pwsh.exe"
call :TryPowerShellHost "%ProgramFiles%\PowerShell\7\pwsh.exe"
call :TryPowerShellHost "%ProgramW6432%\PowerShell\7\pwsh.exe"
call :TryPowerShellHost "%ProgramFiles%\PowerShell\7-preview\pwsh.exe"
call :TryPowerShellHost "%ProgramW6432%\PowerShell\7-preview\pwsh.exe"

for /f "delims=" %%J in ('where powershell.exe 2^>nul') do call :TryPowerShellHost "%%~fJ"
call :TryPowerShellHost "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
call :TryPowerShellHost "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if not defined PS_HOST (
    echo [FAIL] No working PowerShell host was found.
    exit /b 11
)

pushd "%SCRIPT_DIR%" >nul
"%PS_HOST%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set "EXITCODE=%ERRORLEVEL%"
popd >nul
exit /b %EXITCODE%

:TryRegistryValue
if /I "%~1"=="REG_SZ" call :TryPowerShellHost "%~2"
if /I "%~1"=="REG_EXPAND_SZ" call :TryPowerShellHost "%~2"
exit /b 0

:TryRegistryRoot
if /I "%~1"=="REG_SZ" call :TryPowerShellHost "%~2\pwsh.exe"
if /I "%~1"=="REG_EXPAND_SZ" call :TryPowerShellHost "%~2\pwsh.exe"
exit /b 0

:TryPowerShellHost
if defined PS_HOST exit /b 0
if "%~1"=="" exit /b 0
if not exist "%~1" exit /b 0
"%~1" -NoLogo -NoProfile -NonInteractive -Command "exit 0" >nul 2>&1
if "%ERRORLEVEL%"=="0" set "PS_HOST=%~f1"
exit /b 0
