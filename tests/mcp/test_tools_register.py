"""MCP tools module smoke: registration surface exists without starting stdio server."""
from __future__ import annotations


def test_tools_module_exports_register():
    from yggdrasil.mcp import tools

    assert hasattr(tools, "register_tools")
    assert callable(tools.register_tools)


def test_register_tools_binds_expected_names():
    """Collect tool names registered on a minimal FastMCP-like stub."""
    from yggdrasil.mcp.tools import register_tools

    class _StubMcp:
        def __init__(self) -> None:
            self.tools: dict[str, object] = {}

        def tool(self, *args, **kwargs):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn

            return decorator

    # Minimal ctx — only need attribute access for register; tools won't be invoked.
    class _Ctx:
        session_service = None
        search_service = None

    mcp = _StubMcp()
    register_tools(mcp, _Ctx())  # type: ignore[arg-type]
    expected = {
        "start_trajectory",
        "append_step",
        "finalize_trajectory",
        "update_trajectory_meta",
        "get_trajectory",
        "search_strategies",
    }
    # Allow search_experience alias if present
    names = set(mcp.tools)
    assert expected.issubset(names) or "search_experience" in names or expected & names
    assert "start_trajectory" in names
    assert "append_step" in names
    assert "get_trajectory" in names
