# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
from contextlib import contextmanager
import getpass
import json
import logging
import os
from pathlib import Path
import signal
import sys
import time

from .config import Config, ConfigurationError, Secrets, private_directory, write_private_json
from .state import State


@contextmanager
def process_lock(directory: Path):
    private_directory(directory)
    stream = (directory / "run.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise ConfigurationError("another_bridge_process_is_running") from None
    try:
        yield
    finally:
        stream.close()


def configure(config: Config):
    if os.name == "nt":
        raise ConfigurationError("configure_on_linux_target_with_private_permissions")
    print("Credentials are saved only to the configured private secrets file.")
    cookie = getpass.getpass("Full Muse Cookie (hidden): ").strip()
    secret = getpass.getpass("Feishu App Secret (hidden): ").strip()
    if "hatch_sess=" not in cookie or not secret:
        raise ConfigurationError("credentials_missing_or_invalid")
    write_private_json(config.secrets_file, {"muse_cookie": cookie, "feishu_app_secret": secret})
    # Atomic replacement by root must preserve the service user's access.
    if os.geteuid() == 0:
        parent = config.secrets_file.parent.stat()
        os.chown(config.secrets_file, parent.st_uid, parent.st_gid)
    print("Credentials saved with mode 0600. Restart the service after updating them.")


def run(config: Config):
    if not config.app_id.startswith("cli_"):
        raise ConfigurationError("feishu_app_id_not_configured")
    from .bridge import Bridge
    from .feishu import FeishuClient
    from .muse import MainChatClient
    from .reader import FeishuReader

    credentials = Secrets(config.secrets_file)
    credentials.cookie()
    app_secret = credentials.app_secret()
    with process_lock(config.data_dir):
        state = State(config.data_dir)
        holder = {}
        muse = MainChatClient(credentials.cookie, lambda event: holder["bridge"].receive_muse(event))
        feishu = FeishuClient(config.app_id, app_secret, lambda event: holder["bridge"].receive(event))
        bridge = Bridge(config, state, muse, feishu)
        # Read-only context for /read. It reuses the same app credentials and
        # only answers when the owner asks, so it is built eagerly but never polled.
        bridge.reader = FeishuReader(config.app_id, app_secret)
        holder["bridge"] = bridge

        def terminate(signum, frame):
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        bridge.start()
        try:
            # Official Feishu SDK owns its asyncio loop on the main thread.
            feishu.start()
            raise RuntimeError("feishu_connection_stopped")
        finally:
            bridge.stop()
            # Daemon transports may still be unwinding. Process exit closes DB
            # handles; don't race their last durable transaction with close().


def main() -> int:
    parser = argparse.ArgumentParser(description="Personal Muse main-chat bridge to Feishu")
    parser.add_argument("--config", default="config.json")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("configure", "run", "pair", "status", "check"):
        commands.add_parser(command)
    args = parser.parse_args()
    if os.name != "nt":
        os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.load(args.config)
        if args.command == "configure":
            configure(config)
        elif args.command == "run":
            run(config)
        elif args.command == "check":
            from .muse import MainChatClient
            credentials = Secrets(config.secrets_file)
            credentials.app_secret()
            history = MainChatClient(credentials.cookie, lambda event: None).history(limit=20)
            print(json.dumps({"muse_history_ok": True, "events": len(history),
                              "message_contents_logged": False}))
        else:
            state = State(config.data_dir)
            try:
                if args.command == "pair":
                    if state.owner():
                        raise ConfigurationError("already_bound_to_owner")
                    print("Send this to the Feishu bot within 30 minutes:")
                    print("/bind " + state.new_pairing_token())
                else:
                    snapshot = {"owner_bound": bool(state.owner()), "queues": state.counts()}
                    health_file = config.data_dir / "health.json"
                    if health_file.exists():
                        health = json.loads(health_file.read_text(encoding="utf-8"))
                        snapshot["health"] = health
                        snapshot["health_age_seconds"] = round(time.time() - health["updated_at"], 1)
                    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
            finally:
                state.close()
        return 0
    except KeyboardInterrupt:
        return 0
    except ConfigurationError as error:
        print("Configuration error: " + str(error), file=sys.stderr)
        return 2
    except Exception as error:
        # Avoid exception repr/tracebacks containing HTTP headers or private text.
        print("Operation failed: " + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
