# vscode

The VS Code extension for Blaym. It starts the standalone service (`python/blaym_vscode`), records notebook changes through the shared core (`core/ts/src/recording`, compiled in by relative import so the output is CommonJS) and shows a status item and the command "Blaym: record this notebook".

Build: `yarn build` here. Setting `blaym.servicePython` names the interpreter for the service; when empty the repository's `.venv` is used (development).
