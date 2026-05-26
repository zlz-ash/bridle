@echo off
chcp 65001 >nul
REM ============================================================
REM Bridle Test Runner — 按 plan.md 指定顺序分组运行测试
REM 用法: run_tests.bat [group]
REM   groups: workspace | api | mirror | exec | contract | persistence | engine | all
REM ============================================================

setlocal enabledelayedexpansion

cd /d "%~dp0backend"

if not exist ".test-workspaces" mkdir ".test-workspaces"

set "PYTEST=python -m pytest"
set "BASE_OPTS=-v --tb=short --color=yes"

echo ============================================================
echo   Bridle Test Runner
echo   Test Workspaces: backend\.test-workspaces\
echo ============================================================
echo.

if "%1"=="" set "GROUP=all"
if not "%1"=="" set "GROUP=%1"

if /i "%GROUP%"=="all" goto :all
if /i "%GROUP%"=="workspace" goto :workspace
if /i "%GROUP%"=="api" goto :api
if /i "%GROUP%"=="mirror" goto :mirror
if /i "%GROUP%"=="exec" goto :exec
if /i "%GROUP%"=="contract" goto :contract
if /i "%GROUP%"=="persistence" goto :persistence
if /i "%GROUP%"=="engine" goto :engine
if /i "%GROUP%"=="clean" goto :clean

echo Unknown group: %GROUP%
echo Valid groups: workspace, api, mirror, exec, contract, persistence, engine, all, clean
exit /b 1

:workspace
echo [Group 1/7] Workspace path tests...
%PYTEST% tests/test_api/test_workspace.py %BASE_OPTS%
goto :eof

:api
echo [Group 2/7] Tasks & Plans API tests...
%PYTEST% tests/test_api/test_tasks_api.py tests/test_api/test_plans_api.py %BASE_OPTS%
goto :eof

:mirror
echo [Group 3/7] JSON mirror & resync tests...
%PYTEST% tests/test_api/test_json_mirror.py %BASE_OPTS%
goto :eof

:exec
echo [Group 4/7] Execution API tests...
%PYTEST% tests/test_api/test_execution_api.py %BASE_OPTS%
goto :eof

:contract
echo [Group 5/7] REST contract & history tests...
%PYTEST% tests/test_api/test_contract.py %BASE_OPTS%
goto :eof

:persistence
echo [Group 6/7] Persistence tests...
%PYTEST% tests/test_persistence/ %BASE_OPTS%
goto :eof

:engine
echo [Group 7/7] Engine tests...
%PYTEST% tests/test_engine/ %BASE_OPTS%
goto :eof

:all
echo Running ALL test groups...

echo.
echo === [1/7] Workspace ===
%PYTEST% tests/test_api/test_workspace.py %BASE_OPTS%

echo.
echo === [2/7] Tasks & Plans API ===
%PYTEST% tests/test_api/test_tasks_api.py tests/test_api/test_plans_api.py %BASE_OPTS%

echo.
echo === [3/7] JSON Mirror ===
%PYTEST% tests/test_api/test_json_mirror.py %BASE_OPTS%

echo.
echo === [4/7] Execution API ===
%PYTEST% tests/test_api/test_execution_api.py %BASE_OPTS%

echo.
echo === [5/7] REST Contract ===
%PYTEST% tests/test_api/test_contract.py %BASE_OPTS%

echo.
echo === [6/7] Persistence ===
%PYTEST% tests/test_persistence/ %BASE_OPTS%

echo.
echo === [7/7] Engine ===
%PYTEST% tests/test_engine/ %BASE_OPTS%

echo.
echo ============================================================
echo   All test groups completed.
echo ============================================================
goto :eof

:clean
echo Cleaning test workspaces...
rmdir /s /q ".test-workspaces" 2>nul
echo Done. .test-workspaces\ removed.
goto :eof
