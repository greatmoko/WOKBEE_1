# Errors

## [ERR-20260903-001] project-smoke-test-import

**Logged**: 2026-09-03
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
The initial local smoke test could not import the project package.

### Error
```text
ModuleNotFoundError: No module named 'tokbee'
```

### Context
The test was run through `.venv/Scripts/python.exe` without configuring the source checkout's `src` directory on `PYTHONPATH`.

### Suggested Fix
Run source-tree checks with `PYTHONPATH=src` (or install the package in editable mode).

### Metadata
- Reproducible: yes
- Related Files: src/tokbee/core/ai_client.py

### Resolution
- **Resolved**: 2026-09-03
- **Notes**: Rerun the smoke test with the project source path configured.

---

## [ERR-20260903-UI1] offscreen_ui_smoke_command

**Logged**: 2026-09-03T00:00:00Z
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
The ad hoc PySide6 smoke command passed an argument to `Theme` although the project constructor takes no positional arguments.

### Error
```text
TypeError: Theme.__init__() takes 1 positional argument but 2 were given
```

### Context
- Attempted to instantiate `_ModelSettingsPopup` with `Theme('light')` under `QT_QPA_PLATFORM=offscreen`.
- The failure was in the verification command, not application code.

### Suggested Fix
Use the project-defined `Theme()` constructor when running the offscreen UI smoke check.

### Metadata
- Reproducible: yes
- Related Files: src/tokbee/ui/styles/theme.py

---

## [ERR-20260903-002] pyright-not-installed

**Logged**: 2026-09-03
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
The repository environment does not include the `pyright` module.

### Error
```text
No module named pyright
```

### Context
A targeted static type check was attempted with the virtual environment's Python executable.

### Suggested Fix
Install the project's intended pyright tooling before running type checks, if required.

### Metadata
- Reproducible: yes
- Related Files: pyrightconfig.json

### Resolution
- **Resolved**: 2026-09-03
- **Notes**: Installed `pyright 1.1.411` into `.venv`.

---

## [ERR-20260903-003] pyright-existing-type-errors

**Logged**: 2026-09-03
**Priority**: medium
**Status**: pending
**Area**: tests

### Summary
The first full Pyright run reports existing repository type errors.

### Error
```text
231 errors, 4 warnings, 0 informations
```

### Context
The check was run with `.venv/Scripts/pyright.exe` after installation. Errors span existing UI, gateway, engine, and smoke scripts; this is not an installation failure.

### Suggested Fix
Address the existing type errors separately or narrow the Pyright include set in `pyrightconfig.json`.

### Metadata
- Reproducible: yes
- Related Files: pyrightconfig.json

---
