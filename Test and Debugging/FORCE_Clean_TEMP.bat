@echo off
title Clean Temporary Files
echo Cleaning temporary Arabic subtitle files...
rd /s /q "C:\temp\arabic_subs" 2>nul
mkdir "C:\temp\arabic_subs" 2>nul
color 0A
echo ✓ Done! All cached translations deleted.
timeout /t 2 >nul
exit