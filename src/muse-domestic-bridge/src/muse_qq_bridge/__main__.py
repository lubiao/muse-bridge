# SPDX-License-Identifier: Apache-2.0
import json
import logging
import os
from pathlib import Path
import signal
import sys

from .config import Config,Secrets
from .muse import MainChatClient
from .qq_state import QQState
from .runtime import QQBridge
from .transport import QQClient


def main():
    import fcntl
    os.umask(0o077)
    logging.basicConfig(level=logging.WARNING,format='%(asctime)s %(levelname)s %(message)s')
    config=Config.load(sys.argv[1] if len(sys.argv)>1 else '/etc/muse-qq-bridge/config.json')
    config.data_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
    lock=(config.data_dir/'process.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    state=QQState(config.data_dir)
    raw=json.loads(config.path.read_text())
    channel=QQClient(Path(raw['qq_credentials_file']),state,shared_dir=raw['shared_dir'],download_roots=raw['download_roots'])
    channel.check_identity()
    bridge=QQBridge(config,state,None,channel)
    muse=MainChatClient(Secrets(config.secrets_file).cookie,bridge.receive_muse)
    bridge.muse=muse
    if state.get('baseline_seq') is None:
        history=muse.history(limit=config.history_limit)
        state.initialize(channel.credentials,max((e.get('seq') or 0 for e in history),default=0))
    elif not state.is_owner(channel.credentials['owner_id'],channel.credentials['owner_id']):
        raise ValueError('owner_change_rejected')
    for sig in (signal.SIGINT,signal.SIGTERM):
        signal.signal(sig,lambda *_:bridge.stop_event.set())
    bridge.start()
    try:
        while not bridge.stop_event.wait(2):
            if any(not t.is_alive() for t in bridge.threads):raise RuntimeError('worker_thread_exited')
    finally:
        bridge.stop()


if __name__=='__main__':
    try:main()
    except Exception as error:
        logging.error('service_failed type=%s',type(error).__name__)
        raise SystemExit(1)
