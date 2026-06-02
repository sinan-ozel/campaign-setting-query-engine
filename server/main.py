import logging

from fastmcp import FastMCP

mcp = FastMCP("<SERVER-NAME>")


class _SuppressMCPUnionValidation(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("Failed to validate request:")


logging.getLogger().addFilter(_SuppressMCPUnionValidation())

@mcp.tool()
def hello(name: str) -> str:
    """Return a greeting for the given name."""
    return f"Hello, {name}!"


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
