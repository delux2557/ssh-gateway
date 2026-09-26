"""Command line entry point: ``ssh-gateway`` / ``python -m ssh_gateway``.

Configuration comes from SSHGW_* environment variables (see config.py); every
flag here is a convenience override for the value you would otherwise export.
Secrets deliberately have no plain ``--password`` flag: a command-line
argument is readable by every local process, so use the environment, a key
file, or ``--ask-pass``.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from .center import GatewayCenter
from .config import Config, ConfigError
from .server import build_router, make_server


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ssh-gateway",
        description="Expose one SSH device as an HTTP API for scripts and agents.",
        epilog="Environment: SSHGW_TARGET, SSHGW_REMOTE_KEY, SSHGW_REMOTE_PASS, "
               "SSHGW_TOKEN, SSHGW_POLICY_MODE, SSHGW_WHITELIST_FILE, ... "
               "(full list in ssh_gateway/config.py)")

    g = p.add_argument_group("target")
    g.add_argument("--target", metavar="[user@]host[:port]",
                   help="SSH target; overrides SSHGW_TARGET")
    g.add_argument("--key", metavar="PATH", help="private key file (SSHGW_REMOTE_KEY)")
    g.add_argument("--ask-pass", action="store_true",
                   help="prompt for the SSH password instead of putting it in the environment")
    g.add_argument("--shell", metavar="NAME",
                   help="remote shell used to launch commands (default: sh)")
    g.add_argument("--known-hosts", metavar="PATH",
                   help="reject unknown host keys using this known_hosts file")

    g = p.add_argument_group("listener")
    g.add_argument("--host", help="bind address (default 127.0.0.1)")
    g.add_argument("--port", type=int, help="bind port (default 8023)")
    g.add_argument("--token", help="require Authorization: Bearer <token>")
    g.add_argument("--tls-cert", metavar="PATH", help="serve HTTPS with this certificate")
    g.add_argument("--tls-key", metavar="PATH", help="private key for --tls-cert")

    g = p.add_argument_group("behaviour")
    g.add_argument("--policy", choices=("off", "blacklist", "whitelist"),
                   help="command admission mode (default blacklist)")
    g.add_argument("--rules", metavar="FILE",
                   help="policy rule file: one prefix per line, 'rx:' for regex, '#' to comment")
    g.add_argument("--max-channels", type=int, metavar="N",
                   help="concurrent SSH channels; keep at or below sshd MaxSessions (default 8)")
    g.add_argument("--state-dir", metavar="DIR", help="where audit logs and job output live")
    g.add_argument("--print-config", action="store_true",
                   help="show the resolved configuration with secrets redacted, then exit")

    p.add_argument("-v", "--verbose", action="store_true", help="debug logging, including paramiko")
    return p


def _apply(cfg: Config, args: argparse.Namespace) -> Config:
    if args.target:
        from .config import _parse_target
        cfg.remote_host, cfg.remote_port, user = _parse_target(args.target)
        cfg.remote_user = user or cfg.remote_user
    if args.key:
        cfg.remote_key_file = args.key
    if args.ask_pass:
        cfg.remote_pass = getpass.getpass("SSH password: ")
    if args.shell:
        cfg.remote_shell = args.shell
    if args.known_hosts:
        cfg.known_hosts = args.known_hosts
    if args.host:
        cfg.listen_host = args.host
    if args.port:
        cfg.listen_port = args.port
    if args.token:
        cfg.api_token = args.token
    if args.tls_cert:
        cfg.tls_cert, cfg.tls_enabled = args.tls_cert, True
    if args.tls_key:
        cfg.tls_key = args.tls_key
    if args.policy:
        cfg.policy_mode = args.policy
    if args.rules:
        cfg.whitelist_file = args.rules
    if args.max_channels:
        cfg.max_channels = args.max_channels
    if args.state_dir:
        cfg.state_dir = args.state_dir
    return cfg


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="[%(asctime)s] %(levelname)-7s %(name)s: %(message)s")
    if not args.verbose:
        logging.getLogger("paramiko.transport").setLevel(logging.WARNING)

    cfg = _apply(Config(), args)

    if args.print_config:
        print("\n".join(cfg.safe_env_lines()))
        return 0

    try:
        cfg.validate()
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2

    center = GatewayCenter(cfg)
    server = make_server(center, cfg, build_router(center))
    center.start()

    scheme = "https" if cfg.tls_enabled else "http"
    print("=" * 70)
    print(f"  SSH Gateway v{cfg.version}")
    print(f"  listening : {scheme}://{cfg.listen_host}:{cfg.listen_port}")
    print(f"  target    : {cfg.remote_user}@{cfg.remote_host}:{cfg.remote_port}")
    print(f"  auth      : {'bearer token' if cfg.api_token else 'none'}"
          f"   policy: {cfg.policy_mode}   channels: {cfg.max_channels}")
    print(f'  try       : curl -s -X POST {scheme}://{cfg.listen_host}:{cfg.listen_port}'
          f'/run -d \'{{"cmd":"uname -a"}}\'')
    print("  docs      : GET / and GET /openapi.json")
    print("=" * 70)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nexiting, closing transport...", file=sys.stderr)
        center.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
