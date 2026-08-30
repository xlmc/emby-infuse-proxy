# -*- coding: utf-8 -*-
r"""
Cloudflare Worker API 部署脚本（Python，无 Node 依赖）
用法:
  python cf_deploy.py [--account ID] [--script NAME] [--domain HOST]
                      [--source FILE] [--backup-dir DIR]
                      [--dry-run] [--validate-version]
  API Token 优先读取环境变量 CF_API_TOKEN，未设置时安全提示输入。

流程:
  1. 列出账号 (account_id)
  2. 通过 --script 或 --domain 找到 worker 脚本名
  3. 验证当前活动版本的 compatibility / bindings
  4. 真正部署前备份当前线上主模块源码和活动版本号
  5. 上传 worker.js（module 格式，原样附带现有 bindings）
  6. 使用 --domain 指定的域名做只读探测
"""
import getpass, hashlib, json, os, sys, time, urllib.error, urllib.request
from email.parser import BytesParser
from email.policy import default as email_policy

API = 'https://api.cloudflare.com/client/v4'
WORKER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'worker.js')


def option_value(args, name, default=None):
    if name not in args:
        return default
    index = args.index(name)
    if index + 1 >= len(args) or args[index + 1].startswith('--'):
        die('%s 缺少参数' % name)
    return args[index + 1]

def call(token, method, path, body=None, content_type='application/json', raw=False):
    url = API + path
    data = None
    headers = {'Authorization': 'Bearer ' + token}
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            headers['Content-Type'] = content_type
        elif isinstance(body, bytes):
            data = body
            headers['Content-Type'] = content_type
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=60)
        payload = r.read()
        status = r.status
        response_headers = {key.lower(): value for key, value in r.headers.items()}
    except urllib.error.HTTPError as e:
        payload = e.read()
        status = e.code
        response_headers = {key.lower(): value for key, value in e.headers.items()}
    if raw:
        return status, payload, response_headers
    try:
        j = json.loads(payload.decode('utf-8', 'replace'))
    except Exception:
        return status, {'raw': payload[:500].decode('utf-8', 'replace')}
    return status, j

def die(msg):
    print('!! ' + msg)
    sys.exit(1)


def extract_main_module(payload, content_type):
    if 'multipart/' not in str(content_type).lower():
        return payload, ['worker.js']
    prefix = ('Content-Type: %s\r\nMIME-Version: 1.0\r\n\r\n' % content_type).encode()
    message = BytesParser(policy=email_policy).parsebytes(prefix + payload)
    modules = []
    for part in message.iter_parts():
        name = part.get_param('name', header='content-disposition') or ''
        filename = part.get_filename() or ''
        module_name = filename or name
        if not module_name.endswith(('.js', '.mjs')):
            continue
        modules.append((module_name, part.get_payload(decode=True) or b''))
    if len(modules) != 1:
        die('线上脚本包含 %d 个 JS 模块，无法生成可直接回滚的单模块备份，已停止部署' % len(modules))
    return modules[0][1], [modules[0][0]]


def write_source_backup(backup_dir, script_name, active_version_id, payload, content_type):
    source, module_names = extract_main_module(payload, content_type)
    if len(source) < 1000:
        die('线上主模块源码异常（%d B），已停止部署' % len(source))
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    base_name = 'worker-backup-%s-%s' % (script_name, stamp)
    source_path = os.path.join(backup_dir, base_name + '.js')
    manifest_path = os.path.join(backup_dir, base_name + '.json')
    with open(source_path, 'wb') as source_file:
        source_file.write(source)
    manifest = {
        'script': script_name,
        'active_version_id': active_version_id,
        'source_file': os.path.basename(source_path),
        'source_sha256': hashlib.sha256(source).hexdigest(),
        'source_bytes': len(source),
        'modules': module_names,
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }
    with open(manifest_path, 'w', encoding='utf-8') as manifest_file:
        json.dump(manifest, manifest_file, ensure_ascii=False, indent=2)
    return source_path, manifest_path, manifest

def main():
    args = sys.argv[1:]
    dry = '--dry-run' in args
    validate_version = '--validate-version' in args
    script_name = option_value(args, '--script')
    account_hint = option_value(args, '--account')
    domain_hint = str(option_value(args, '--domain', os.environ.get('CF_WORKER_DOMAIN', '')) or '').strip().lower()
    source_path = os.path.abspath(option_value(args, '--source', WORKER_FILE))
    backup_default = os.environ.get('CF_WORKER_BACKUP_DIR') or os.path.join(os.path.expanduser('~'), 'Desktop')
    backup_dir = os.path.abspath(option_value(args, '--backup-dir', backup_default))
    if not os.path.isfile(source_path) or not source_path.endswith(('.js', '.mjs')):
        die('源码文件不存在或不是 JS 模块: ' + source_path)
    token = None
    if len(args) > 0 and not args[0].startswith('--'):
        token = args[0]
    token = token or os.environ.get('CF_API_TOKEN')
    if not token:
        token = getpass.getpass('Cloudflare API Token: ').strip()
    if not token:
        die('缺少 API Token')

    # 1. 账号
    st, j = call(token, 'GET', '/accounts')
    if st != 200 or not j.get('success'):
        die('列账号失败 (HTTP %d): %s' % (st, json.dumps(j)[:300]))
    accounts = j.get('result', [])
    if not accounts:
        die('Token 可见的账号为空')
    print('== 账号 ==')
    for a in accounts:
        print('  %s | %s' % (a['id'], a['name']))
    account_ids = {a.get('id') for a in accounts}
    if account_hint:
        if account_hint not in account_ids:
            die('--account 不在 Token 可见账号中')
        account_id = account_hint
    elif len(accounts) == 1:
        account_id = accounts[0]['id']
    else:
        die('Token 可见多个账号，请用 --account <ID> 明确指定，禁止默认选择')

    # 2. 找脚本名
    if not script_name and domain_hint:
        st, j = call(token, 'GET', '/accounts/%s/workers/domains' % account_id)
        domains = j.get('result', []) if st == 200 else []
        for d in domains:
            host = str(d.get('hostname') or '').strip().lower()
            if host == domain_hint:
                script_name = d.get('service')
                print('== 域名匹配 ==\n  %s -> 脚本 "%s" (env: %s)' % (host, script_name, d.get('environment')))
                break
    if not script_name:
        st, j = call(token, 'GET', '/accounts/%s/workers/scripts' % account_id)
        scripts = [s['id'] for s in (j.get('result') or []) if isinstance(s, dict)]
        if len(scripts) == 1:
            script_name = scripts[0]
            print('== 唯一脚本 ==\n  ' + script_name)
        else:
            print('== 账号下脚本列表 ==\n  ' + ', '.join(scripts))
            die('请用 --script <名称> 或 --domain <完整域名> 明确指定目标脚本')

    # 3. 从当前活动版本读取资源。/settings 不会返回版本绑定，不能用于上传元数据。
    compat_date = ''
    compat_flags = []
    bindings = []
    st, j = call(token, 'GET', '/accounts/%s/workers/scripts/%s/deployments' % (account_id, script_name))
    deployments = (j.get('result') or {}).get('deployments') or []
    active_versions = deployments[0].get('versions') or [] if st == 200 and j.get('success') and deployments else []
    active_version_id = active_versions[0].get('version_id') if active_versions else ''
    if active_version_id:
        vst, vj = call(token, 'GET', '/accounts/%s/workers/scripts/%s/versions/%s' % (account_id, script_name, active_version_id))
        if vst == 200 and vj.get('success'):
            resources = (vj.get('result') or {}).get('resources') or {}
            script = resources.get('script') or {}
            compat_date = script.get('compatibility_date') or ''
            compat_flags = script.get('compatibility_flags') or []
            bindings = resources.get('bindings') or []
    if bindings:
        print('== 当前活动版本 ==\n  version=%s compat_date=%s flags=%s' % (active_version_id, compat_date or '(inherit)', compat_flags))
        print('  bindings: ' + ', '.join('%s(%s)' % (b.get('name'), b.get('type')) for b in bindings))
    else:
        die('无法读取当前活动版本的 bindings；为防止清空配置，已停止上传')

    with open(source_path, 'rb') as source_file:
        source = source_file.read()
    if len(source) < 1000:
        die('本地源码异常（%d B），已停止上传' % len(source))
    print('== 本地候选源码 ==\n  %s\n  %d B | sha256=%s' % (source_path, len(source), hashlib.sha256(source).hexdigest()))

    if dry:
        bst, backup_payload, backup_headers = call(token, 'GET', '/accounts/%s/workers/scripts/%s' % (account_id, script_name), raw=True)
        if bst != 200 or len(backup_payload) < 1000:
            die('线上源码备份预检失败 (HTTP %d, %d B)' % (bst, len(backup_payload)))
        online_source, online_modules = extract_main_module(backup_payload, backup_headers.get('content-type', ''))
        print('== 线上备份预检 ==\n  modules=%s | %d B | sha256=%s' % (
            ','.join(online_modules),
            len(online_source),
            hashlib.sha256(online_source).hexdigest(),
        ))
        print('== DRY-RUN 结束（未写备份、未创建版本、未上传）==')
        return

    # 5. 上传（正式版固定为单模块 worker.js；bindings 必须完整写入 metadata）
    metadata = {
        'main_module': 'worker.js',
        'bindings': bindings,
    }
    if compat_date:
        metadata['compatibility_date'] = compat_date
    if compat_flags:
        metadata['compatibility_flags'] = compat_flags
    boundary = '----cfdeploy' + str(int(time.time()))
    parts = []
    parts.append(('--%s\r\nContent-Disposition: form-data; name="metadata"\r\nContent-Type: application/json\r\n\r\n%s\r\n' % (boundary, json.dumps(metadata))).encode())
    parts.append(('--%s\r\nContent-Disposition: form-data; name="worker.js"; filename="worker.js"\r\nContent-Type: application/javascript+module\r\n\r\n' % boundary).encode() + source + b'\r\n')
    parts.append(('--%s--\r\n' % boundary).encode())
    body = b''.join(parts)
    if validate_version:
        st, j = call(token, 'POST', '/accounts/%s/workers/scripts/%s/versions?bindings_inherit=strict' % (account_id, script_name), body, 'multipart/form-data; boundary=' + boundary)
        print('== 仅验证版本（不部署流量）==')
        print('  HTTP %d | success=%s | version=%s' % (st, j.get('success'), (j.get('result') or {}).get('id', '')))
        if st != 200 or not j.get('success'):
            print('  ' + json.dumps(j, ensure_ascii=False)[:800])
            die('版本验证失败 —— 线上未变更')
        return

    # 真正切换流量前必须获得可读的单模块源码备份；失败即停止。
    bst, backup_payload, backup_headers = call(token, 'GET', '/accounts/%s/workers/scripts/%s' % (account_id, script_name), raw=True)
    if bst != 200 or len(backup_payload) < 1000:
        die('线上源码备份失败 (HTTP %d, %d B)，已停止部署' % (bst, len(backup_payload)))
    try:
        backup_source, backup_manifest, backup_info = write_source_backup(
            backup_dir,
            script_name,
            active_version_id,
            backup_payload,
            backup_headers.get('content-type', ''),
        )
    except (OSError, ValueError) as error:
        die('写入线上源码备份失败，已停止部署: ' + str(error))
    print('== 已备份线上正式模块 ==\n  %s\n  %s\n  version=%s | sha256=%s' % (
        backup_source,
        backup_manifest,
        backup_info['active_version_id'],
        backup_info['source_sha256'],
    ))

    st, j = call(token, 'PUT', '/accounts/%s/workers/scripts/%s' % (account_id, script_name), body, 'multipart/form-data; boundary=' + boundary)
    print('== 上传结果 ==')
    print('  HTTP %d | success=%s' % (st, j.get('success')))
    if st != 200 or not j.get('success'):
        print('  ' + json.dumps(j, ensure_ascii=False)[:800])
        die('上传失败 —— 线上仍是旧版本，可用刚生成的源码备份回滚')
    print('  部署成功: %s (entry=worker.js, modules=1)' % script_name)

    # 6. 验证
    if not domain_hint:
        print('== 未提供 --domain，跳过线上 HTTP 探测 ==')
        return
    print('== 8 秒后探测线上 ==')
    time.sleep(8)
    vpath = '/Items/vb-1034/Images/Primary?tag=ZTRhMmE3LnBuZw'
    req = urllib.request.Request('https://' + domain_hint + vpath, headers={'User-Agent': 'Infuse-Direct/8.5.2'})
    class NR(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a):
            return None
    try:
        r = urllib.request.build_opener(NR()).open(req, timeout=45)
        data = r.read()
        print('  vb-1034: HTTP %d, %d B, cover=%s' % (r.status, len(data), r.headers.get('x-emby-proxy-cover')))
    except urllib.error.HTTPError as e:
        print('  vb-1034: HTTP %d, cover=%s diag=%s' % (e.code, e.headers.get('x-emby-proxy-cover'), (e.headers.get('x-emby-proxy-cover-diag') or '')[:120]))
    except Exception as e:
        print('  vb-1034: ERR ' + str(e)[:80])

if __name__ == '__main__':
    main()
