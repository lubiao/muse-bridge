# SPDX-License-Identifier: Apache-2.0
from contextlib import nullcontext
import json
import logging
import threading
import time
from websockets.sync.client import connect

from .bridge import Bridge
from .config import write_private_json
from .files import FileError, MAX_FILE_BYTES
from .muse import AuthExpired, MuseError
from .transport import ChannelError

LOG = logging.getLogger(__name__)


class QQBridge(Bridge):
    def _receive(self):
        while not self.stop_event.is_set():
            if not self.baseline_ready.wait(1):
                continue
            try:
                self.feishu.check_identity()
                with connect('ws://127.0.0.1:3301',
                        additional_headers={'Authorization':'Bearer '+self.feishu.credentials['api_token']},
                        proxy=None,open_timeout=10,close_timeout=3,max_size=2*1024*1024) as ws:
                    self.health['qq'] = 'connected'
                    next_history = 0
                    while not self.stop_event.is_set():
                        try:
                            message = json.loads(ws.recv(timeout=1))
                        except TimeoutError:
                            message = None
                        self.health['last_qq_receive_activity'] = time.time()
                        if isinstance(message,dict):
                            self.state.ingest(message,self.feishu.credentials,self.config.max_message_chars)
                        if time.monotonic() >= next_history:
                            self._history_repair()
                            next_history = time.monotonic()+30
            except Exception as error:
                self.health['qq'] = 'reconnecting_or_login_required'
                LOG.warning('qq_receive_interrupted type=%s',type(error).__name__)
                self.stop_event.wait(5)

    def _history_repair(self):
        # Only this owner's latest 50 private messages, filtered by onboarding
        # time and durable message ID. Covers short WS disconnects/restarts.
        data = self.feishu.api('get_friend_msg_history',{
            'user_id':self.feishu.credentials['owner_id'],'count':50,'reverseOrder':False})
        for message in (data or {}).get('messages',[]):
            sender=str((message.get('sender') or {}).get('user_id',message.get('user_id','')))
            if sender != self.feishu.credentials['owner_id']:
                continue
            normalized=dict(message,post_type='message',message_type='private',user_id=sender,
                self_id=self.feishu.credentials['bot_id'])
            self.state.ingest(normalized,self.feishu.credentials,self.config.max_message_chars)
        self.state.put('last_qq_history_at',time.time())

    def _outgoing(self):
        while not self.stop_event.is_set():
            row=self.state.claim_outgoing()
            if row is None:
                self.stop_event.wait(0.5)
                continue
            submitting=False
            try:
                with self.file_transfer_lock if row['kind']=='file' else nullcontext():
                    if row['kind']=='file':
                        payload=json.loads(row['payload'])
                        item=payload.get('qq_file')
                        if item is None:
                            data=self.files.read('outbox:'+row['id'])
                            if data is None:
                                data=self.muse.read_file(payload['path'],payload.get('size'))
                                self.files.write('outbox:'+row['id'],data)
                            upload=self.feishu.upload_image if payload.get('media_kind')=='image' else self.feishu.upload_file
                            item=upload(payload['filename'],data)
                            payload['qq_file']=item
                            self.state.file_payload('outbox',row['id'],payload)
                            data=None
                    self.state.outgoing_submitting(row['id'])
                    submitting=True
                    if row['kind']=='file':
                        send=self.feishu.send_image if payload.get('media_kind')=='image' else self.feishu.send_file
                        remote_id=send(row['chat_id'],item,row['id'])
                    else:
                        remote_id=self.feishu.send_text(row['chat_id'],row['text'],row['id'])
                    self.state.outgoing_sent(row['id'],remote_id)
                    self.health['last_qq_delivery_at']=time.time()
            except ChannelError as error:
                if submitting:
                    # A failed OneBot call can wrap an uncertain native QQ send.
                    # Without a server dedup contract never automatically replay.
                    self.state.outgoing_unknown(row['id'])
                else:
                    self.state.outgoing_failed(row['id'],error.retryable,error.category,15)
                LOG.warning('qq_delivery_failed category=%s submitted=%s',error.category,submitting)
            except FileError as error:
                self.state.outgoing_failed(row['id'],False,error.code)
                self.file_failed('out:'+row['id'],error.code)
            except AuthExpired:
                self.state.outgoing_failed(row['id'],True,'authentication_required',60)
                self.authentication_failed()
            except MuseError as error:
                self.state.outgoing_failed(row['id'],error.retryable,error.code,15)
                if not error.retryable:self.file_failed('out:'+row['id'],error.code)
            except Exception as error:
                if submitting:self.state.outgoing_unknown(row['id'])
                else:self.state.outgoing_failed(row['id'],True,'preparation_interrupted',30)
                LOG.warning('qq_delivery_interrupted type=%s',type(error).__name__)

    def _health(self):
        next_cleanup=0
        while not self.stop_event.is_set():
            snapshot=dict(self.health)
            snapshot.pop('last_feishu_delivery_at',None)
            snapshot.update(updated_at=time.time(),queues=self.state.counts(),owner_bound=bool(self.state.owner()),
                max_file_bytes=MAX_FILE_BYTES,last_incoming=self.state.get('last_qq_incoming_at'),
                last_history_repair=self.state.get('last_qq_history_at'))
            write_private_json(self.config.data_dir/'health.json',snapshot)
            if time.monotonic()>=next_cleanup:
                self.files.cleanup(self.state.retained_files())
                self.feishu.cleanup(self.state.retained_media())
                next_cleanup=time.monotonic()+60
            self.stop_event.wait(5)

    def start(self):
        super().start()
        thread=threading.Thread(target=self._receive,name='qq-receive',daemon=True)
        self.threads.append(thread)
        thread.start()
