"""tmux command builder and output parser."""

from dataclasses import dataclass


SOCKET_NAME = "nexus"
FIELD_SEPARATOR = "|"
FORMAT_STRING = (
    "#{session_name}"
    "|#{session_windows}"
    "|#{session_attached}"
    "|#{pane_current_path}"
    "|#{pane_current_command}"
    "|#{pane_pid}"
    "|#{pane_dead}"
    "|#{pane_dead_status}"
)


@dataclass
class SessionInfo:
    """Parsed tmux session information.

    Attributes:
        name: Session name.
        windows: Number of windows in the session.
        attached: Number of attached clients.
        pane_path: Current working directory of the active pane.
        pane_cmd: Current command running in the active pane.
        pane_pid: PID of the process in the active pane.
        is_dead: Whether the pane process has exited.
        exit_status: Exit code if the pane is dead, None otherwise.
    """

    name: str
    windows: int
    attached: int
    pane_path: str
    pane_cmd: str
    pane_pid: int
    is_dead: bool
    exit_status: int | None


def build_list_cmd() -> list[str]:
    """Build the tmux command to list all sessions with detailed format.

    Returns:
        list[str]: Command arguments for tmux list-sessions.
    """
    return [
        "tmux", "-L", SOCKET_NAME,
        "list-sessions", "-F", FORMAT_STRING,
    ]


def parse_list_output(raw: str) -> list[SessionInfo]:
    """Parse tmux list-sessions output into SessionInfo objects.

    Handles empty output gracefully by returning an empty list.

    Args:
        raw: Raw stdout from tmux list-sessions with FORMAT_STRING.

    Returns:
        list[SessionInfo]: Parsed session information.
    """
    sessions: list[SessionInfo] = []

    for line in raw.strip().splitlines():
        if not line.strip():
            continue

        fields = line.split(FIELD_SEPARATOR)
        if len(fields) < 8:
            continue

        name = fields[0]
        windows = int(fields[1])
        attached = int(fields[2])
        pane_path = fields[3]
        pane_cmd = fields[4]
        pane_pid = int(fields[5])
        pane_dead = fields[6] == "1"
        exit_status = int(fields[7]) if fields[7] and pane_dead else None

        sessions.append(
            SessionInfo(
                name=name,
                windows=windows,
                attached=attached,
                pane_path=pane_path,
                pane_cmd=pane_cmd,
                pane_pid=pane_pid,
                is_dead=pane_dead,
                exit_status=exit_status,
            )
        )

    return sessions


def build_new_cmd(
    name: str, cmd: str | None = None, directory: str | None = None
) -> list[str]:
    """Build the tmux command to create a new detached session.

    Args:
        name: Session name.
        cmd: Command to run in the session. If None, uses tmux default.
        directory: Working directory for the session.

    Returns:
        list[str]: Command arguments for tmux new-session.
    """
    result = ["tmux", "-L", SOCKET_NAME, "new-session", "-d", "-s", name]

    if directory:
        result.extend(["-c", directory])

    if cmd:
        result.extend(cmd.split())

    return result


def build_capture_cmd(session: str, lines: int | str = 30) -> list[str]:
    """Build the tmux command to capture pane output.

    Args:
        session: Target session name.
        lines: Number of lines to capture from scrollback.
            Use "-" for full scrollback history.

    Returns:
        list[str]: Command arguments for tmux capture-pane.
    """
    start = f"-{lines}" if isinstance(lines, int) else "-"
    return [
        "tmux", "-L", SOCKET_NAME,
        "capture-pane", "-p", "-t", session, "-S", start,
    ]


def build_send_keys_cmd(
    session: str, keys: list[str], raw: bool = False
) -> list[str]:
    """Build the tmux command to send keystrokes to a session.

    In default mode, automatically appends Enter after the last key.
    In raw mode, passes keys verbatim to tmux send-keys.

    Args:
        session: Target session name.
        keys: List of key strings to send.
        raw: If True, pass keys verbatim without appending Enter.

    Returns:
        list[str]: Command arguments for tmux send-keys.
    """
    result = ["tmux", "-L", SOCKET_NAME, "send-keys", "-t", session]
    result.extend(keys)

    if not raw:
        result.append("Enter")

    return result


def build_kill_cmd(session: str) -> list[str]:
    """Build the tmux command to kill a session.

    Args:
        session: Target session name.

    Returns:
        list[str]: Command arguments for tmux kill-session.
    """
    return [
        "tmux", "-L", SOCKET_NAME,
        "kill-session", "-t", session,
    ]
