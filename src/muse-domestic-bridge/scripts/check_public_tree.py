#!/usr/bin/env python3
"""Small publication check, not a complete security audit or secret scanner."""
import json
from pathlib import Path
import re
import sys

DIRECTORIES={'src','tests','examples','deploy','docs','scripts','licenses','.github'}
ROOT_FILES={'README.md','LICENSE','NOTICE','THIRD_PARTY_NOTICES.md','CHANGELOG.md','CONTRIBUTING.md',
            'SECURITY.md','pyproject.toml','.gitignore','.gitattributes'}


def publication_files(root):
    for path in sorted(root.rglob('*')):
        if not path.is_file():continue
        relative=path.relative_to(root)
        if '__pycache__' in relative.parts or path.suffix=='.pyc':continue
        if relative.parts[0] in DIRECTORIES or relative.as_posix() in ROOT_FILES:
            yield path


def check(root):
    issues=[];count=0
    for path in publication_files(root):
        count+=1;relative=path.relative_to(root).as_posix()
        if path.is_symlink():issues.append((relative,'symlink_not_allowed'));continue
        if path.suffix.lower() in {'.pem','.key','.log','.sqlite3','.zip','.png','.jpg','.webp'}:
            issues.append((relative,'private_or_binary_artifact'));continue
        try:text=path.read_text(encoding='utf-8')
        except UnicodeError:issues.append((relative,'unexpected_binary'));continue
        patterns=[('private_key',r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
                  ('cookie_value',r'hatch_sess=(?!offline|fake|test|placeholder)[A-Za-z0-9_+./=-]{24,}'),
                  ('literal_bearer',r'Bearer [A-Za-z0-9_+./=-]{32,}'),
                  ('personal_windows_path',r'[A-Za-z]:[/\\]Users[/\\](?!Public|example|USER)[^/\\\s]+'),
                  ('private_validation_path',r'/var/lib/' + r'napcat-muse-' + 'validation')]
        for category,pattern in patterns:
            if re.search(pattern,text):issues.append((relative,category))
        if path.suffix=='.json' and 'examples' in path.parts:
            try:value=json.loads(text)
            except ValueError:issues.append((relative,'invalid_example_json'));continue
            for key in ('muse_cookie','feishu_app_secret','api_token','token','bot_id','owner_id','user_id'):
                if value.get(key) not in (None,''):
                    issues.append((relative,'nonempty_example_credential'))
    for folder in ('validation','state','work'):
        if (root/folder).exists():issues.append((folder,'private_directory_at_project_root'))
    return {'files_checked':count,'passed':not issues,'issues':[{'file':name,'category':category} for name,category in issues]}


if __name__=='__main__':
    result=check(Path(__file__).resolve().parents[1]);print(json.dumps(result,indent=2))
    raise SystemExit(0 if result['passed'] else 1)
