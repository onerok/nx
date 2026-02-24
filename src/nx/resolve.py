"""Session resolution protocol for the Nexus CLI.

Resolves a bare session name to a (node, session) tuple by querying
all nodes in the fleet. Handles fully qualified names (node/session),
unique matches, and ambiguous matches with fzf disambiguation.
"""

import subprocess
import sys

from nx.config import FleetConfig
from nx.ssh import fan_out
from nx.tmux import build_list_cmd, parse_list_output


class SessionNotFound(Exception):
    """Raised when no session matches the given name on any node."""

    pass


class AmbiguousSession(Exception):
    """Raised when multiple sessions match and disambiguation is not possible."""

    pass


async def resolve_session(name: str, config: FleetConfig) -> tuple[str, str]:
    """Resolve a session name to a (node, session) tuple.

    Handles three cases:
    1. Fully qualified name (contains '/'): split and return directly.
    2. Bare name with exactly one match: return the match.
    3. Bare name with multiple matches: disambiguate via fzf (interactive)
       or raise AmbiguousSession (non-interactive).

    Args:
        name: Session name, either bare ("api") or fully qualified ("dev/api").
        config: Fleet configuration with node list and settings.

    Returns:
        tuple[str, str]: A (node, session_name) tuple identifying the target.

    Raises:
        SessionNotFound: If no session matches the given name on any node.
        AmbiguousSession: If multiple sessions match and disambiguation fails
            (user cancels fzf or stdin is not a tty).
    """
    # Reason: Split on first '/' only, in case session names theoretically
    # contain slashes (they can't in tmux, but this is defensive).
    if "/" in name:
        node, session = name.split("/", 1)
        return (node, session)

    # Fan out to all nodes to find matching sessions
    results = await fan_out(
        config.nodes, build_list_cmd(), max_concurrent=config.max_concurrent_ssh
    )

    # Collect all (node, session_name) matches
    matches: list[tuple[str, str]] = []
    for node, result in results.items():
        if result.returncode != 0:
            continue
        sessions = parse_list_output(result.stdout)
        for session in sessions:
            if session.name == name:
                matches.append((node, session.name))

    # 0 matches
    if len(matches) == 0:
        raise SessionNotFound(f"Session '{name}' not found on any node.")

    # 1 match
    if len(matches) == 1:
        return matches[0]

    # N matches (ambiguous) — attempt disambiguation
    match_strs = [f"{node}/{session}" for node, session in matches]

    if sys.stdin.isatty():
        return _disambiguate_interactive(match_strs, config.default_node)
    else:
        raise AmbiguousSession(
            f"Ambiguous session '{name}'. "
            f"Matches: {', '.join(match_strs)}. "
            f"Use fully qualified name (node/session)."
        )


def _disambiguate_interactive(
    match_strs: list[str], default_node: str
) -> tuple[str, str]:
    """Launch fzf to let the user pick from ambiguous session matches.

    Sorts matches so that the default_node appears first, then
    alphabetically by node name.

    Args:
        match_strs: List of "node/session" strings to choose from.
        default_node: The fleet's default node, prioritized in sort order.

    Returns:
        tuple[str, str]: The selected (node, session_name) tuple.

    Raises:
        AmbiguousSession: If the user cancels the fzf selection.
    """
    # Reason: Sort default_node matches first, then alphabetical, so the
    # most likely target is pre-selected in fzf.
    sorted_matches = sorted(
        match_strs,
        key=lambda m: (0 if m.split("/", 1)[0] == default_node else 1, m),
    )

    fzf_input = "\n".join(sorted_matches)
    result = subprocess.run(
        ["fzf", "--prompt", "Select session: "],
        input=fzf_input,
        text=True,
        capture_output=True,
    )

    if result.returncode != 0:
        raise AmbiguousSession("Selection cancelled.")

    selected = result.stdout.strip()
    node, session = selected.split("/", 1)
    return (node, session)
