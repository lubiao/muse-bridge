import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from muse_bridge import cli


@pytest.mark.parametrize('channel',cli.CHANNELS)
def test_init_creates_only_one_private_configuration_and_no_credentials(tmp_path,channel,capsys):
    folder=tmp_path/'config'
    assert cli.main(['init',channel,'--directory',str(folder)])==0
    config=json.loads((folder/'config.json').read_text())
    assert config['channel']==channel and not (folder/'secrets.json').exists()
    assert not (folder/'weixin.json').exists() and not (folder/'qq.json').exists()
    if channel=='qq':assert config['download_roots']==[] and Path(config['shared_dir']).is_dir()
    if os.name!='nt':
        assert (folder.stat().st_mode & 0o777)==0o700
        assert ((folder/'config.json').stat().st_mode & 0o777)==0o600
    assert cli.main(['init',channel,'--directory',str(folder)])==2


def test_channel_mismatch_rejected_before_service_or_network(tmp_path,monkeypatch):
    cli.initialize('weixin',tmp_path)
    monkeypatch.setattr(cli,'run',lambda *args:pytest.fail('must not start wrong adapter'))
    assert cli.main(['run','qq','--config',str(tmp_path/'config.json')])==2


@pytest.mark.parametrize('channel',cli.CHANNELS)
def test_status_is_offline_without_other_channel_dependencies(tmp_path,channel):
    cli.initialize(channel,tmp_path)
    assert cli.main(['status',channel,'--config',str(tmp_path/'config.json')])==0


@pytest.mark.skipif(os.name=='nt',reason='Linux credential file permissions')
def test_qq_configure_uses_hidden_credentials_and_prints_no_secrets(tmp_path,monkeypatch,capsys):
    cli.initialize('qq',tmp_path)
    answers=iter(['222','111'])
    monkeypatch.setattr('builtins.input',lambda *args:next(answers))
    hidden=iter(['hatch_sess=offline-cookie-placeholder','offline-onebot-placeholder'])
    monkeypatch.setattr(cli.getpass,'getpass',lambda *args:next(hidden))
    assert cli.main(['configure','qq','--config',str(tmp_path/'config.json')])==0
    config=json.loads((tmp_path/'config.json').read_text())
    assert json.loads((tmp_path/'qq.json').read_text())['owner_id']=='111'
    assert 'placeholder' not in capsys.readouterr().out
    assert (Path(config['qq_credentials_file']).stat().st_mode & 0o777)==0o600


def test_weixin_qr_flow_persists_confirmed_credentials_without_printing_tokens(tmp_path,monkeypatch,capsys):
    pytest.importorskip('qrcode')
    from muse_weixin_bridge import protocol
    cli.initialize('weixin',tmp_path)
    replies=iter([{'qrcode':'offline-code','qrcode_img_content':'https://example.invalid/qr'},
        {'status':'confirmed','bot_token':'offline-bot-placeholder','ilink_bot_id':'bot','ilink_user_id':'owner'}])
    monkeypatch.setattr(protocol,'call',lambda *args,**kwargs:next(replies))
    cli.weixin_login(tmp_path/'config.json')
    value=json.loads((tmp_path/'weixin.json').read_text())
    assert value['token']=='offline-bot-placeholder' and value['user_id']=='owner'
    assert 'offline-bot-placeholder' not in capsys.readouterr().out
    with pytest.raises(cli.ConfigurationError):cli.weixin_login(tmp_path/'config.json')


def test_qr_ascii_renderer_is_available_when_weixin_extra_is_installed():
    qrcode=pytest.importorskip('qrcode')
    qr=qrcode.QRCode(border=2);qr.add_data('https://example.invalid/qr');qr.make(fit=True)
    output=io.StringIO();qr.print_ascii(out=output,invert=True)
    assert output.getvalue().strip()
