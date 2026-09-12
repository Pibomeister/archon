# Disposable API + MCP repository-list qualification spec

This file is only a safe dry-run fixture for Archon workflow DAG qualification.
It must not be used for product implementation.

Selected repositories:

- api
- goodword-mcp

Required dry-run behavior:

- Planning phase reaches the joint planning gate and pauses for human approval.
- Implementation phase can enter a repository implementation stage after approval has already been represented by controller state.
- Integration phase runs only the combined local verification stage.
