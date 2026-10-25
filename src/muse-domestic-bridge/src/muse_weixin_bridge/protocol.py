# SPDX-License-Identifier: Apache-2.0
# Tencent iLink protocol adaptation; Copyright (C) 2026 Tencent.
# Modified Python implementation; see licenses/openclaw-weixin-MIT.txt.
from __future__ import annotations
import base64,hashlib,json,os,secrets,sys,time
from pathlib import Path
from urllib.parse import urlencode,urlparse
import requests
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from cryptography.hazmat.primitives.padding import PKCS7

API='https://ilinkai.weixin.qq.com'
CDN='https://novac2c.cdn.weixin.qq.com/c2c'
VERSION='2.4.10'
LIMIT=200*1024*1024

class ProbeError(Exception):
    def __init__(self,kind,code=None):
        self.kind=kind;self.code=code;super().__init__(kind)

def checked_url(url,cdn=False):
    parsed=urlparse(url)
    allowed=('weixin.qq.com',) if cdn else ('ilinkai.weixin.qq.com',)
    host=(parsed.hostname or '').lower()
    if parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ProbeError('url_rejected')
    if not any(host==domain or host.endswith('.'+domain) for domain in allowed):
        raise ProbeError('url_rejected')
    return url

def headers(token=None,get=False):
    result={'iLink-App-Id':'bot','iLink-App-ClientVersion':str((2<<16)|(4<<8)|10)}
    if not get:
        result.update({'Content-Type':'application/json','AuthorizationType':'ilink_bot_token',
            'X-WECHAT-UIN':base64.b64encode(str(secrets.randbits(32)).encode()).decode()})
        if token:result['Authorization']='Bearer '+token
    return result

def bounded_response(response,limit):
    if not 200<=response.status_code<300:raise ProbeError('http',response.status_code)
    raw_length=response.headers.get('Content-Length')
    if raw_length and int(raw_length)>limit:raise ProbeError('response_too_large')
    chunks=[];total=0;deadline=time.monotonic()+600
    for chunk in response.iter_content(65536):
        total+=len(chunk)
        if total>limit:raise ProbeError('response_too_large')
        if time.monotonic()>deadline:raise ProbeError('download_deadline')
        chunks.append(chunk)
    return b''.join(chunks)

def call(endpoint,body=None,*,token=None,base=API,get=False,timeout=40):
    url=checked_url(base.rstrip('/')+'/'+endpoint.lstrip('/'))
    session=requests.Session();session.trust_env=False
    try:
        with session.request('GET' if get else 'POST',url,headers=headers(token,get),
                json=None if get else body,timeout=(10,timeout),allow_redirects=False,stream=True) as response:
            data=json.loads(bounded_response(response,2*1024*1024))
        for field in ('ret','errcode'):
            if data.get(field) not in (None,0):raise ProbeError('api',data[field])
        return data
    except requests.RequestException:raise ProbeError('network') from None
    except (ValueError,TypeError):raise ProbeError('invalid_response') from None
    finally:session.close()

def encrypt(data,key):
    padding=PKCS7(128).padder();padded=padding.update(data)+padding.finalize()
    cipher=Cipher(algorithms.AES(key),modes.ECB()).encryptor()
    return cipher.update(padded)+cipher.finalize()

def decrypt(data,encoded_key):
    key=base64.b64decode(encoded_key,validate=True)
    if len(key)==32:
        try:key=bytes.fromhex(key.decode('ascii'))
        except (ValueError,UnicodeError):raise ProbeError('invalid_media_key') from None
    if len(key)!=16:raise ProbeError('invalid_media_key')
    cipher=Cipher(algorithms.AES(key),modes.ECB()).decryptor();padded=cipher.update(data)+cipher.finalize()
    padding=PKCS7(128).unpadder();return padding.update(padded)+padding.finalize()

