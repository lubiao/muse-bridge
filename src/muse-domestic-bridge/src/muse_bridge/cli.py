# SPDX-License-Identifier: Apache-2.0
"""Single-owner setup. Credentials are hidden and never command-line options."""
from __future__ import annotations
import argparse
import getpass
import importlib
import json
import logging
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlencode

from . import __version__
from muse_feishu_bridge.config import Config, ConfigurationError, Secrets, write_private_json

CHANNELS = ('feishu', 'weixin', 'qq')


def load_raw(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ConfigurationError('configuration_missing_or_invalid')
    return value


def config_template(channel, directory, data_dir=None, shared_dir=None):
    directory = Path(directory).resolve()
    raw = {'channel':channel,'app_id':'', 'secrets_file':'secrets.json',
           'data_dir':str(Path(data_dir).resolve()) if data_dir else str(directory/'state'),
           'history_limit':500,'max_message_chars':20000,'outbound_chunk_chars':2000}
    if channel == 'weixin':
        raw['weixin_credentials_file'] = str(directory/'weixin.json')
    elif channel == 'qq':
        raw.update(qq_credentials_file=str(directory/'qq.json'),
                   shared_dir=str(Path(shared_dir).resolve()) if shared_dir else str(directory/'qq-media'),
                   download_roots=[])
    return raw


def initialize(channel, directory, data_dir=None, shared_dir=None):
    directory = Path(directory).absolute()
    if directory.is_symlink() or (directory/'config.json').exists():
        raise ConfigurationError('configuration_exists_or_path_rejected')
    directory = directory.resolve()
    raw = config_template(channel, directory, data_dir, shared_dir)
    write_private_json(directory/'config.json', raw)
    Path(raw['data_dir']).mkdir(mode=0o700, parents=True, exist_ok=True)
    if channel == 'qq':
        Path(raw['shared_dir']).mkdir(mode=0o750, parents=True, exist_ok=True)
    print('Configuration created. Run configure before starting the bridge.')


def configure(channel, path):
    if os.name == 'nt':
        raise ConfigurationError('configure_on_linux_with_private_permissions')
    config = Config.load(path)
    raw = load_raw(config.path)
    if channel == 'feishu' and not raw.get('app_id'):
        raw['app_id'] = input('Feishu App ID: ').strip()
        if not raw['app_id'].startswith('cli_'):
            raise ConfigurationError('feishu_app_id_invalid')
    cookie = getpass.getpass('Full Muse Cookie (hidden): ').strip()
    if 'hatch_sess=' not in cookie:
        raise ConfigurationError('muse_cookie_invalid')
    value = {'muse_cookie':cookie}
    if channel == 'feishu':
        secret = getpass.getpass('Feishu App Secret (hidden): ').strip()
        if not secret:
            raise ConfigurationError('feishu_secret_missing')
        value['feishu_app_secret'] = secret
    if channel == 'qq':
        bot = input('Spare/bot QQ number: ').strip()
        owner = input('Your daily/owner QQ number: ').strip()
        token = getpass.getpass('OneBot HTTP/WS token (hidden): ').strip()
        if not bot.isdecimal() or not owner.isdecimal() or bot == owner or not token:
            raise ConfigurationError('qq_credentials_invalid')
        write_private_json(Path(raw['qq_credentials_file']), {'api_token':token,'bot_id':bot,'owner_id':owner})
    write_private_json(config.secrets_file, value)
    write_private_json(config.path, raw)
    print('Credentials saved privately. No account request was sent.')


def weixin_login(path, replace=False):
    """Tencent QR flow; based on openclaw-weixin (MIT), see NOTICE."""
    from muse_weixin_bridge import protocol as p
    import qrcode
    config = Config.load(path)
    raw = load_raw(config.path)
    if raw.get('channel') != 'weixin':
        raise ConfigurationError('configuration_channel_mismatch')
    target = Path(raw['weixin_credentials_file'])
    if target.exists() and not replace:
        raise ConfigurationError('weixin_credentials_exist_use_replace_if_intended')
    reply = p.call('ilink/bot/get_bot_qrcode?bot_type=3', {'local_token_list':[]})
    qr, content = reply.get('qrcode'), reply.get('qrcode_img_content')
    if not isinstance(qr,str) or not qr or not isinstance(content,str) or not content:
        raise ConfigurationError('weixin_qr_missing')
    code = qrcode.QRCode(border=2);code.add_data(content);code.make(fit=True)
    print('Scan this temporary code with your WeChat mobile app. Do not share it.')
    code.print_ascii(invert=True)
    deadline, base = time.monotonic()+300, p.API
    while time.monotonic() < deadline:
        status = p.call('ilink/bot/get_qrcode_status?'+urlencode({'qrcode':qr}),base=base,get=True,timeout=38)
        if status.get('status') == 'scaned_but_redirect':
            base = p.checked_url('https://'+str(status.get('redirect_host','')))
        elif status.get('status') == 'expired':
            raise ConfigurationError('weixin_qr_expired_run_login_again')
        elif status.get('status') == 'confirmed':
            if not all(isinstance(status.get(key),str) and status[key] for key in ('bot_token','ilink_bot_id','ilink_user_id')):
                raise ConfigurationError('weixin_login_fields_missing')
            write_private_json(target, {'token':status['bot_token'],'bot_id':status['ilink_bot_id'],
                'user_id':status['ilink_user_id'],'base_url':p.checked_url(status.get('baseurl') or p.API),'cursor':''})
            print('Connected. Start the service, then send a message to the WeChat bot.')
            return
        time.sleep(1)
    raise ConfigurationError('weixin_qr_expired_run_login_again')


def run(channel, path):
    if os.name == 'nt':
        raise ConfigurationError('daemon_requires_linux')
    if channel == 'feishu':
        from muse_feishu_bridge.cli import run as feishu_run
        return feishu_run(Config.load(path))
    module = importlib.import_module('muse_'+channel+'_bridge.__main__')
    prior = sys.argv
    try:
        sys.argv = [prior[0], str(Path(path).resolve())]
        return module.main()
    finally:
        sys.argv = prior


def check(channel, path):
    config = Config.load(path)
    namespace = 'muse_'+channel+'_bridge'
    module = importlib.import_module(namespace+'.muse')
    muse = module.MainChatClient(Secrets(config.secrets_file).cookie, lambda event:None)
    history = muse.history(limit=20)
    result = {'muse_history_ok':True,'events_checked':len(history),'message_contents_logged':False}
    if channel == 'feishu':
        if not config.app_id.startswith('cli_'):
            raise ConfigurationError('feishu_app_id_invalid')
        Secrets(config.secrets_file).app_secret()
        result['feishu_permissions_checked'] = False
    elif channel == 'weixin':
        from muse_weixin_bridge.protocol import checked_url
        credentials = Secrets(Path(load_raw(config.path)['weixin_credentials_file'])).read()
        if not all(isinstance(credentials.get(k),str) and credentials[k] for k in ('token','bot_id','user_id','base_url')):
            raise ConfigurationError('weixin_credentials_invalid')
        checked_url(credentials['base_url'])
        result['weixin_login_live_checked'] = False
    else:
        from muse_qq_bridge.qq_state import QQState
        from muse_qq_bridge.transport import QQClient
        raw = load_raw(config.path);state = QQState(config.data_dir)
        try:
            client = QQClient(Path(raw['qq_credentials_file']),state,shared_dir=raw['shared_dir'],download_roots=raw['download_roots'])
            result['qq_bot_identity_ok'] = client.check_identity()
        finally:
            state.close()
    print(json.dumps(result))


def state_command(channel, path, command):
    config = Config.load(path)
    if channel == 'feishu':
        from muse_feishu_bridge.state import State
    else:
        module = importlib.import_module('muse_'+channel+'_bridge.'+('weixin_state' if channel=='weixin' else 'qq_state'))
        State = module.WeixinState if channel=='weixin' else module.QQState
    state = State(config.data_dir)
    try:
        if command == 'pair':
            if state.owner():
                raise ConfigurationError('already_bound_to_owner')
            print('Send to your Feishu bot within 30 minutes: /bind '+state.new_pairing_token())
        else:
            result = {'owner_bound':bool(state.owner()),'queues':state.counts()}
            health = config.data_dir/'health.json'
            if health.exists():
                value=json.loads(health.read_text());result['health']=value
                result['health_age_seconds']=round(time.time()-value['updated_at'],1)
            print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        state.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Unofficial single-owner Muse bridge. Run services on Linux.')
    parser.add_argument('--version', action='version', version=__version__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init',help='create configuration without overwriting existing files')
    init.add_argument('channel',choices=CHANNELS);init.add_argument('--directory',required=True)
    init.add_argument('--data-dir');init.add_argument('--shared-dir')
    for name in ('configure','run','check','status','pair'):
        cmd = sub.add_parser(name)
        cmd.add_argument('channel',choices=('feishu',) if name=='pair' else CHANNELS)
        cmd.add_argument('--config',required=True)
    login = sub.add_parser('login-weixin',help='interactive Tencent QR login, no bot polling')
    login.add_argument('--config',required=True);login.add_argument('--replace',action='store_true')
    args = parser.parse_args(argv)
    if os.name != 'nt':
        os.umask(0o077)
    logging.basicConfig(level=logging.WARNING,format='%(asctime)s %(levelname)s %(message)s')
    # The per-channel runners ask for INFO, but basicConfig is a no-op once the
    # root logger already has a handler, so that request is silently ignored and
    # the service ends up logging nothing at all. Honour MUSEBRIDGE_LOG_LEVEL.
    level_name = os.environ.get('MUSEBRIDGE_LOG_LEVEL', '')
    if level_name:
        logging.getLogger().setLevel(getattr(logging, level_name.upper(), logging.WARNING))

    try:
        if args.command not in ('init','login-weixin'):
            if load_raw(args.config).get('channel') != args.channel:
                raise ConfigurationError('configuration_channel_mismatch')
        if args.command=='init':initialize(args.channel,args.directory,args.data_dir,args.shared_dir)
        elif args.command=='configure':configure(args.channel,args.config)
        elif args.command=='login-weixin':weixin_login(args.config,args.replace)
        elif args.command=='run':run(args.channel,args.config)
        elif args.command=='check':check(args.channel,args.config)
        else:state_command(args.channel,args.config,args.command)
        return 0
    except KeyboardInterrupt:
        return 130
    except ConfigurationError as error:
        print('Configuration error: '+str(error),file=sys.stderr)
        return 2
    except Exception as error:
        print('Operation failed: '+type(error).__name__,file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
