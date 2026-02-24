"""SSH transport layer for remote command execution."""

import asyncio
import shlex
from dataclasses import dataclass


@dataclass
class NodeResult:
    """Result of executing a command on a node.

    Attributes:
        stdout: Standard output from the command.
        stderr: Standard error from the command.
        returncode: Exit code of the command.
        node: The node the command was executed on.
    """

    stdout: str
    stderr: str
    returncode: int
    node: str


async def run_on_node(
    node: str, cmd: list[str], timeout: int = 2
) -> NodeResult:
    """Execute a command on a node via SSH (or locally).

    If node is "local", runs the command directly via asyncio subprocess.
    Otherwise, wraps it in an SSH call with ConnectTimeout.

    Args:
        node: Target node name. "local" for local execution.
        cmd: Command and arguments to execute.
        timeout: SSH connection timeout in seconds.

    Returns:
        NodeResult: The command's stdout, stderr, returncode, and node name.
    """
    if node == "local":
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        ssh_cmd = [
            "ssh",
            "-o", f"ConnectTimeout={timeout}",
            node,
            shlex.join(cmd),
        ]
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    stdout_bytes, stderr_bytes = await proc.communicate()
    return NodeResult(
        stdout=stdout_bytes.decode(),
        stderr=stderr_bytes.decode(),
        returncode=proc.returncode or 0,
        node=node,
    )


async def fan_out(
    nodes: list[str], cmd: list[str], max_concurrent: int = 16
) -> dict[str, NodeResult]:
    """Execute a command on multiple nodes concurrently.

    Uses asyncio.Semaphore to limit concurrent SSH connections
    and asyncio.gather to run all tasks in parallel.

    Args:
        nodes: List of node names to execute on.
        cmd: Command and arguments to execute on each node.
        max_concurrent: Maximum number of concurrent SSH connections.

    Returns:
        dict[str, NodeResult]: Mapping of node name to its result.
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _run_with_semaphore(node: str) -> NodeResult:
        async with semaphore:
            return await run_on_node(node, cmd)

    results = await asyncio.gather(
        *[_run_with_semaphore(node) for node in nodes],
        return_exceptions=True,
    )

    output: dict[str, NodeResult] = {}
    for node, result in zip(nodes, results):
        if isinstance(result, Exception):
            output[node] = NodeResult(
                stdout="",
                stderr=str(result),
                returncode=1,
                node=node,
            )
        else:
            output[node] = result

    return output
