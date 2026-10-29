$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
& 'C:\Users\mattw\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe' 'C:\Users\mattw\Documents\repos\easy-hermes\select-models.py' --hermes-home 'C:\Users\mattw\AppData\Local\hermes' --full @args
exit $LASTEXITCODE
