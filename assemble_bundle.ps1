$dst = 'C:\dev\agentic-rag\deploy\wijerco-update-2026-08-16'
$src = 'C:\Users\ajwij\AppData\Roaming\Claude\local-agent-mode-sessions\41d3f39c-9273-44bf-8c20-1d94aed85bf7\720adcb7-5fb7-4002-b016-b2747bf9b07f\local_2be64e37-bd09-44a6-8ae0-2f569f5fc6fa\outputs'
Copy-Item (Join-Path $src 'deploy.bat') (Join-Path $dst 'deploy.bat') -Force
Copy-Item (Join-Path $src 'README.txt') (Join-Path $dst 'README.txt') -Force
Copy-Item (Join-Path $src 'remote_deploy.json') (Join-Path $dst 'remote_deploy.json') -Force
Copy-Item (Join-Path $src 'remote_deploy_credentials.json') (Join-Path $dst 'remote_deploy_credentials.json') -Force
Copy-Item 'C:\dev\agentic-rag\docker-compose.yml' (Join-Path $dst 'docker-compose.yml') -Force
Get-ChildItem $dst | Select-Object Name, Length
