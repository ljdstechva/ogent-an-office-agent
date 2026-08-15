@echo off
rem "officecli agent"-style launcher: run inside a document folder to pick a
rem DOCX from it (recursively) and open Ogent's live preview + agent chat.
rem officecli.exe itself is a third-party binary, so the agent command ships
rem as this sibling shim. Put this folder on PATH to type `officecli-agent`.
if "%~1"=="" (
    py -3 "%~dp0..\ogent.py" --agent
) else (
    py -3 "%~dp0..\ogent.py" --agent "%~1"
)
exit /b %errorlevel%
