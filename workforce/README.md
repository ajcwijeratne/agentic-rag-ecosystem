# WijerCo AI Workforce

This directory is the versioned source of truth for the 12-department institutional workforce. Personal voice and company knowledge remain in the external `WIJERCO_PATH`; workforce prompts and skills load from this directory.

- `agent-catalogue.json`: generated machine-readable catalogue.
- `AGENTS/`: orchestrator, department and specialist prompt contracts.
- `SKILLS/role-suites/`: one role-suite `SKILL.md` per specialist.
- `SKILLS/capabilities/`: reusable higher-education and operating capabilities.

Regenerate derived files after editing `workforce/catalogue.py` or the capability map:

```powershell
.\.venv\Scripts\python.exe scripts\generate_workforce.py
```
